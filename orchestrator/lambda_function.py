"""
company-intelligence orchestrator

Pipeline per run:
  1. Fetch general AI funding news (Google News RSS)
  2. Classify which articles are funding events (Bedrock)
  3. For each event:
       a. fetch company-specific news
       b. call the research Lambda (grounded research packet)
       c. verify sources, reconcile the event with research
       d. generate the Markdown report (Bedrock)
       e. save the Markdown report to S3
       f. render a formatted PDF and email it via SES
  4. Return a per-company summary
"""

import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from html import escape
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
import urllib.request

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from pdf_report import render_report_pdf


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

MODEL_ID = os.environ["BEDROCK_MODEL_ID"]

S3_BUCKET = os.environ["S3_BUCKET"]

RESEARCH_LAMBDA_NAME = os.environ.get(
    "RESEARCH_LAMBDA_NAME",
    "company-intelligence-web-research",
)

SES_SENDER_EMAIL = os.environ["SES_SENDER_EMAIL"]

# Recipients normally come from the S3 file at RECIPIENTS_S3_KEY (see
# get_recipients() below), so the list can be updated without redeploying.
# SES_RECIPIENT_EMAIL (one address, or several separated by commas) is kept
# as a fallback for when that file doesn't exist yet.
SES_RECIPIENT_EMAILS_ENV = [
    e.strip()
    for e in os.environ.get("SES_RECIPIENT_EMAIL", "").split(",")
    if e.strip()
]

RECIPIENTS_S3_KEY = os.environ.get("RECIPIENTS_S3_KEY", "config/recipients.txt")

# Optional: SES configuration set for delivery/bounce event tracking.
SES_CONFIGURATION_SET = os.environ.get("SES_CONFIGURATION_SET", "")

NEWS_QUERY = os.environ.get("NEWS_QUERY", "AI startup funding")
GENERAL_NEWS_LIMIT = int(os.environ.get("GENERAL_NEWS_LIMIT", "5"))
COMPANY_NEWS_LIMIT = int(os.environ.get("COMPANY_NEWS_LIMIT", "10"))

REPORT_PREFIX = os.environ.get("REPORT_PREFIX", "reports").strip("/")

# Also keep a copy of each PDF in S3 next to the Markdown report.
SAVE_PDF_TO_S3 = os.environ.get("SAVE_PDF_TO_S3", "false").lower() == "true"

# Skip funding events that were already reported in an earlier run.
DEDUPE_EVENTS = os.environ.get("DEDUPE_EVENTS", "false").lower() == "true"

# Output budget for the report model. Raise it if reports end abruptly
# (stay within your model's maximum output tokens).
REPORT_MAX_TOKENS = int(os.environ.get("REPORT_MAX_TOKENS", "7000"))

# Add every research record to the PDF as an evidence appendix.
INCLUDE_RESEARCH_APPENDIX = (
    os.environ.get("INCLUDE_RESEARCH_APPENDIX", "true").lower() == "true"
)

# Print full research payloads (large) to CloudWatch.
DEBUG_LOGS = os.environ.get("DEBUG_LOGS", "false").lower() == "true"


# ============================================================
# AWS CLIENTS
# ============================================================

bedrock = boto3.client(
    "bedrock-runtime",
    config=Config(
        read_timeout=300,
        connect_timeout=30,
        retries={"max_attempts": 3, "mode": "standard"},
    ),
)

s3 = boto3.client("s3")

ses = boto3.client(
    "ses",
    region_name=os.environ.get("AWS_REGION", "ap-south-1"),
)

# The research Lambda runs for minutes. boto3's default 60s read timeout
# would give up and re-invoke it, so wait longer and never auto-retry.
research_lambda = boto3.client(
    "lambda",
    region_name=os.environ.get("RESEARCH_LAMBDA_REGION", "us-west-2"),
    config=Config(
        read_timeout=900,
        connect_timeout=30,
        retries={"total_max_attempts": 1},
    ),
)


# ============================================================
# SMALL HELPERS
# ============================================================

NOT_DISCLOSED = "Not publicly disclosed"

_EMPTY_VALUES = {
    "", "none", "null", "n/a", "na", "unknown", "not disclosed",
    "not publicly disclosed", "undisclosed",
}

_TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_src$)")


def is_missing(value):
    if value is None:
        return True
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return str(value).strip().lower() in _EMPTY_VALUES


def safe_name(text):
    return "".join(
        c if c.isalnum() or c in "-_" else "_"
        for c in (text or "unknown")
    ).strip("_") or "unknown"


def normalise_url(url):
    """Comparable form of a URL (host case, www, fragment, tracking
    parameters and trailing slash ignored)."""

    if not url:
        return ""

    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()

    query = urlencode([
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _TRACKING_PARAMS.match(k)
    ])

    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    return urlunsplit(
        (parts.scheme.lower() or "https", host, parts.path.rstrip("/"),
         query, "")
    )


def domain_of(url):
    try:
        host = urlsplit(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def money_key(text):
    """Digits and decimal point only, to compare amounts loosely."""
    return re.sub(r"[^0-9.]", "", str(text or ""))


def today_utc():
    return datetime.now(timezone.utc)


# ============================================================
# BEDROCK
# ============================================================

def ask_bedrock(prompt, max_tokens=4000, label="bedrock"):
    """
    Returns (text, stop_reason). Joins every text block in the response
    (not just the first) and warns when output was cut off.
    """

    response = bedrock.converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
    )

    content = response["output"]["message"]["content"]
    text = "".join(block["text"] for block in content if "text" in block)
    stop_reason = response.get("stopReason")

    if stop_reason == "max_tokens":
        print(
            f"WARNING: {label} output hit maxTokens ({max_tokens}) "
            "and is truncated"
        )

    return text, stop_reason


def extract_json_object(text):
    """Parse a JSON object from model output, tolerating fences and
    surrounding commentary."""

    text = (text or "").strip()

    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    start, end = text.find("{"), text.rfind("}")

    if start < 0 or end <= start:
        raise ValueError(
            "Model output contains no complete JSON object: "
            + text[:300]
        )

    return json.loads(text[start:end + 1])


# ============================================================
# S3 PROMPTS
# ============================================================

_PROMPT_CACHE = {}


def get_prompt(prompt_name):
    """Prompts are read from S3 once per container and cached."""

    if prompt_name not in _PROMPT_CACHE:

        response = s3.get_object(
            Bucket=S3_BUCKET,
            Key=f"prompts/{prompt_name}",
        )

        _PROMPT_CACHE[prompt_name] = response["Body"].read().decode("utf-8")

    return _PROMPT_CACHE[prompt_name]


# ============================================================
# S3 RECIPIENT LIST
# ============================================================

_RECIPIENTS_CACHE = None


def get_recipients():
    """
    Report recipients: one email address per line in the S3 text file at
    RECIPIENTS_S3_KEY (blank lines and lines starting with '#' are
    ignored). Cached per container, like prompts, so the list can be
    edited without redeploying. Falls back to SES_RECIPIENT_EMAIL if the
    file doesn't exist or is empty.
    """

    global _RECIPIENTS_CACHE

    if _RECIPIENTS_CACHE is not None:
        return _RECIPIENTS_CACHE

    recipients = []

    try:
        response = s3.get_object(Bucket=S3_BUCKET, Key=RECIPIENTS_S3_KEY)
        body = response["Body"].read().decode("utf-8")
        recipients = [
            line.strip()
            for line in body.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("NoSuchKey", "404"):
            raise
        print(
            f"No recipients file at s3://{S3_BUCKET}/{RECIPIENTS_S3_KEY}, "
            "falling back to SES_RECIPIENT_EMAIL"
        )

    if not recipients:
        recipients = SES_RECIPIENT_EMAILS_ENV

    if not recipients:
        raise RuntimeError(
            "No email recipients configured: add addresses to "
            f"s3://{S3_BUCKET}/{RECIPIENTS_S3_KEY} (one per line) or set "
            "SES_RECIPIENT_EMAIL."
        )

    _RECIPIENTS_CACHE = recipients
    return recipients


# ============================================================
# NEWS (GOOGLE NEWS RSS)
# ============================================================

def fetch_rss(query, limit):

    url = (
        "https://news.google.com/rss/search?q="
        + quote(query)
    )

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
    )

    with urllib.request.urlopen(request, timeout=15) as response:
        xml_data = response.read()

    root = ET.fromstring(xml_data)

    articles = []

    for item in root.findall(".//item")[:limit]:

        source = item.find("source")

        articles.append({
            "title": item.findtext("title"),
            "url": item.findtext("link"),
            "published": item.findtext("pubDate"),
            "description": item.findtext("description"),
            # Publisher name and site, which the Google News link hides.
            "publisher": source.text if source is not None else None,
            "publisher_url": (
                source.get("url") if source is not None else None
            ),
        })

    return articles


def get_news():
    """General funding news."""
    return fetch_rss(NEWS_QUERY, GENERAL_NEWS_LIMIT)


def get_company_news(company):
    """Company-specific news. The name is quoted so multi-word names
    match as a phrase."""
    return fetch_rss(f'"{company}"', COMPANY_NEWS_LIMIT)


# ============================================================
# FUNDING CLASSIFICATION
# ============================================================

def classify_articles(articles):

    prompt_template = get_prompt("funding_classifier.md")

    prompt = (
        prompt_template
        + "\n\nARTICLES:\n"
        + json.dumps(articles, indent=2, ensure_ascii=False)
    )

    result, stop_reason = ask_bedrock(prompt, 2000, "classifier")

    if stop_reason == "max_tokens":
        raise ValueError(
            "Classifier output was truncated; raise its maxTokens"
        )

    return extract_json_object(result)


# ============================================================
# DE-DUPLICATION (optional, S3 markers)
# ============================================================

def event_marker_key(event):

    parts = [
        safe_name(event.get("company", "")).lower(),
        safe_name(event.get("round", "")).lower(),
    ]

    return f"state/reported/{'__'.join(parts)}.json"


def already_reported(event):

    try:
        s3.head_object(Bucket=S3_BUCKET, Key=event_marker_key(event))
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def mark_reported(event, s3_key):

    s3.put_object(
        Bucket=S3_BUCKET,
        Key=event_marker_key(event),
        Body=json.dumps({
            "reported_at": today_utc().isoformat(),
            "report_key": s3_key,
            "event": event,
        }).encode("utf-8"),
        ContentType="application/json",
    )


# ============================================================
# WEB RESEARCH LAMBDA
# ============================================================

RESEARCH_COLLECTIONS = [
    "executive_facts",
    "claims",
    "customers",
    "technology",
    "competitors",
    "funding",
]


def merge_sources(research_packet, grounded_sources):
    """
    One source register for the report. Grounded sources (returned by the
    research Lambda's web search) are marked verified. Any other URL the
    packet cites is added as unverified, so nothing silently disappears.
    """

    register = []
    by_key = {}

    def add(url, domain, verified, source_id=None):

        key = normalise_url(url)

        if not key:
            return

        if key in by_key:
            if verified:
                by_key[key]["verified"] = True
            return

        entry = {
            "id": source_id or f"S{len(register) + 1}",
            "url": url,
            "domain": domain or domain_of(url),
            "verified": verified,
        }

        by_key[key] = entry
        register.append(entry)

    for src in grounded_sources:
        add(src.get("url"), src.get("domain"), True, src.get("id"))

    taken = {e["id"] for e in register}

    def next_id():
        n = len(register) + 1
        while f"S{n}" in taken:
            n += 1
        taken.add(f"S{n}")
        return f"S{n}"

    for src in research_packet.get("sources", []) or []:
        if src.get("url") and normalise_url(src["url"]) not in by_key:
            add(src["url"], src.get("domain"), False, next_id())

    for collection in RESEARCH_COLLECTIONS:
        for record in research_packet.get(collection, []) or []:
            if not isinstance(record, dict):
                continue
            for url in record.get("source_urls", []) or []:
                if normalise_url(url) not in by_key:
                    add(url, None, False, next_id())

    return register


def validate_research_sources(research_packet, grounded_sources):
    """List every cited URL that web search did not actually return."""

    valid_urls = {
        normalise_url(source.get("url"))
        for source in grounded_sources
        if source.get("url")
    }

    problems = []

    for collection_name in RESEARCH_COLLECTIONS:

        records = research_packet.get(collection_name, []) or []

        for index, record in enumerate(records):

            if not isinstance(record, dict):
                continue

            for url in record.get("source_urls", []) or []:

                if normalise_url(url) not in valid_urls:

                    problems.append({
                        "collection": collection_name,
                        "index": index,
                        "url": url,
                    })

    if problems:
        print("WARNING: Research contains unverified source URLs:")
        print(json.dumps(problems, indent=2, ensure_ascii=False))

    return problems


def get_web_research(company, company_articles):

    print(f"Calling research Lambda for: {company}")

    payload = {
        "company": company,
        "news_articles": company_articles,
    }

    response = research_lambda.invoke(
        FunctionName=RESEARCH_LAMBDA_NAME,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )

    raw_payload = response["Payload"].read().decode("utf-8")

    if response.get("FunctionError"):
        raise Exception(
            "Research Lambda execution failed "
            f"({response['FunctionError']}): " + raw_payload[:2000]
        )

    print("Raw research Lambda response received")

    invoked_response = json.loads(raw_payload)

    if DEBUG_LOGS:
        print("Research Lambda invoked response:")
        print(json.dumps(invoked_response, indent=2, ensure_ascii=False))

    status_code = invoked_response.get("statusCode", 200)

    body = invoked_response.get("body", invoked_response)

    if isinstance(body, str):
        body = json.loads(body)

    if status_code != 200:
        raise Exception(
            f"Research Lambda returned statusCode {status_code}: "
            + json.dumps(body, ensure_ascii=False)
        )

    research_packet = body.get("research_packet", {}) or {}

    # Real web-search sources. Older versions of the research Lambda put
    # them in research_packet.grounding_sources instead.
    grounded_sources = (
        body.get("sources")
        or research_packet.get("grounding_sources")
        or []
    )

    problems = validate_research_sources(research_packet, grounded_sources)

    sources = merge_sources(research_packet, grounded_sources)

    meta = body.get("research_meta", {}) or {}

    print("Structured research packet received")
    for collection in RESEARCH_COLLECTIONS + ["unknowns"]:
        print(
            f"Research {collection}: "
            f"{len(research_packet.get(collection, []) or [])}"
        )
    print(f"Grounded sources received: {len(grounded_sources)}")
    print(f"Sources in register: {len(sources)}")
    print(f"Unverified citations: {len(problems)}")

    return {
        "research_packet": research_packet,
        "sources": sources,
        "grounded_source_count": len(grounded_sources),
        "unverified_citations": problems,
        "research_meta": meta,
    }


# ============================================================
# EVENT RECONCILIATION
# ============================================================

def reconcile_event(event, research_packet):
    """
    The classifier only sees a headline. Fill gaps in the event from the
    research packet's funding record, keep amount raised and valuation
    apart, and record every change so it is visible in the report.
    """

    event = dict(event)
    changes = []

    funding = [
        f for f in (research_packet.get("funding", []) or [])
        if isinstance(f, dict)
    ]

    # Prefer the research record for the same round, else the first one.
    record = None
    event_round = str(event.get("round", "")).lower().replace("-", "")

    for f in funding:
        if event_round and event_round in str(f.get("round", "")).lower().replace("-", ""):
            record = f
            break

    if record is None and funding:
        record = funding[0]

    field_map = [
        ("round", "round"),
        ("amount", "amount"),
        ("valuation", "valuation"),
        ("lead_investor", "lead_investor"),
        ("event_date", "date"),
    ]

    if record:

        for event_field, research_field in field_map:

            if (
                is_missing(event.get(event_field))
                and not is_missing(record.get(research_field))
            ):
                event[event_field] = record[research_field]
                changes.append(
                    f"{event_field} taken from web research"
                )

        if is_missing(event.get("investors")) and not is_missing(
            record.get("other_investors")
        ):
            event["investors"] = record["other_investors"]
            changes.append("investors taken from web research")

        if not event.get("supporting_urls"):
            event["supporting_urls"] = record.get("source_urls", [])

    # Headlines like "raises funding at Rs 16.7 Cr valuation" state a
    # valuation. Equal amount and valuation is almost always that mix-up.
    title = str(event.get("source_title", "")).lower()
    amount = event.get("amount")
    valuation = event.get("valuation")

    if not is_missing(amount):

        same_as_valuation = (
            not is_missing(valuation)
            and money_key(amount)
            and money_key(amount) == money_key(valuation)
        )

        valuation_only_headline = (
            is_missing(valuation)
            and "valuation" in title
            and money_key(amount)
            and money_key(amount) in money_key(title)
        )

        if same_as_valuation or valuation_only_headline:

            if is_missing(valuation):
                event["valuation"] = amount

            event["amount"] = NOT_DISCLOSED
            changes.append(
                "amount raised set to not disclosed: the reported figure "
                "is the valuation"
            )

    for field in ("amount", "valuation", "lead_investor", "event_date"):
        if is_missing(event.get(field)):
            event[field] = NOT_DISCLOSED

    event["reconciliation"] = changes

    if changes:
        print("Event reconciled: " + "; ".join(changes))

    return event


def build_key_facts(event):
    """Label/value pairs for the summary panel at the top of the PDF."""

    def fmt_date(value):
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime(
                "%d %b %Y"
            )
        except ValueError:
            return value

    lead = event.get("lead_investor")
    others = [
        i for i in (event.get("investors") or [])
        if i and str(i) != str(lead)
    ]

    source_urls = event.get("supporting_urls") or []
    primary = (
        domain_of(source_urls[0]) if source_urls
        else event.get("publisher") or "Google News"
    )

    facts = [
        ("Round", event.get("round") or NOT_DISCLOSED),
        ("Amount raised", event.get("amount")),
        ("Valuation", event.get("valuation")),
        ("Lead investor", lead),
        ("Announced", fmt_date(event.get("event_date"))),
        ("Primary source", primary),
    ]

    if others:
        facts.append(("Other investors", ", ".join(map(str, others))))

    return facts


# ============================================================
# SOURCE ID MAPPING
# ============================================================

def attach_source_ids(research_packet, sources):
    """Give each record S-numbers matching the source register."""

    source_map = {normalise_url(s["url"]): s["id"] for s in sources}

    numbered_sources = [
        {
            "id": s["id"],
            "domain": s.get("domain"),
            "url": s["url"],
            "verified": s.get("verified", False),
        }
        for s in sources
    ]

    research_with_ids = json.loads(json.dumps(research_packet))

    for collection_name in RESEARCH_COLLECTIONS:

        for record in research_with_ids.get(collection_name, []) or []:

            if not isinstance(record, dict):
                continue

            source_ids = []

            for url in record.get("source_urls", []) or []:
                sid = source_map.get(normalise_url(url))
                if sid and sid not in source_ids:
                    source_ids.append(sid)

            record["source_ids"] = source_ids

    return research_with_ids, numbered_sources


# ============================================================
# FINAL REPORT GENERATION
# ============================================================

def select_report_articles(general_articles, company_articles, event):
    """
    Company-specific news plus the general article that triggered the
    event. Other companies' headlines are left out so they cannot leak
    into this company's report.
    """

    selected = list(company_articles)
    seen = {a.get("url") for a in selected}

    trigger_title = str(event.get("source_title", "")).strip().lower()
    trigger_url = event.get("source_url")

    for article in general_articles:

        title = str(article.get("title", "")).strip().lower()

        is_trigger = (
            (trigger_url and article.get("url") == trigger_url)
            or (trigger_title and title.startswith(trigger_title[:60]))
        )

        if is_trigger and article.get("url") not in seen:
            selected.insert(0, article)
            seen.add(article.get("url"))

    return selected


REPORT_FORMAT_GUIDE = """

==================================================
OUTPUT FORMAT (the report is rendered to PDF)
==================================================
- This is the full, detailed report, not a summary. Cover every section
  the instructions above ask for, in full, using everything relevant in
  the research. Where evidence is missing, say what is unknown rather
  than dropping the section.
- Use "##" for sections and "###" for sub-sections.
- Use Markdown tables for funding rounds, competitors and customers.
- Cite sources inline as [S1] or [S1, S3], using only IDs in the SOURCE
  REGISTER. Never write raw URLs in the body.
- Sources marked "verified": false were not confirmed by web search;
  say so when a key point rests only on them.
- Do not add a sources section; the PDF appends the source register.
- Keep the amount raised and the valuation separate.
"""


def generate_report(company, event, articles, web_research):

    prompt_template = get_prompt("company_report.md")

    research_packet = web_research.get("research_packet", {})
    sources = web_research.get("sources", [])

    research_with_ids, numbered_sources = attach_source_ids(
        research_packet,
        sources,
    )

    # The report model needs IDs, not long URL lists.
    for collection in RESEARCH_COLLECTIONS:
        for record in research_with_ids.get(collection, []) or []:
            if isinstance(record, dict):
                record.pop("source_urls", None)

    research_with_ids.pop("grounding_sources", None)
    research_with_ids.pop("sources", None)

    event_for_prompt = {
        k: v for k, v in event.items()
        if k not in ("supporting_urls",)
    }

    prompt = (
        prompt_template
        + REPORT_FORMAT_GUIDE

        + "\n\n"
        + "==================================================\n"
        + "COMPANY\n"
        + "==================================================\n"
        + company

        + "\n\n"
        + "==================================================\n"
        + "LATEST DETECTED FUNDING EVENT\n"
        + "==================================================\n"
        + json.dumps(event_for_prompt, indent=2, ensure_ascii=False)

        + "\n\n"
        + "==================================================\n"
        + "GOOGLE NEWS MATERIAL\n"
        + "==================================================\n"
        + json.dumps(articles, indent=2, ensure_ascii=False)

        + "\n\n"
        + "==================================================\n"
        + "STRUCTURED WEB RESEARCH\n"
        + "==================================================\n"
        + json.dumps(research_with_ids, indent=2, ensure_ascii=False)

        + "\n\n"
        + "==================================================\n"
        + "SOURCE REGISTER\n"
        + "==================================================\n"
        + json.dumps(numbered_sources, indent=2, ensure_ascii=False)
    )

    report, stop_reason = ask_bedrock(prompt, REPORT_MAX_TOKENS, "report")

    return report, numbered_sources, stop_reason, research_with_ids


# ============================================================
# DATA QUALITY WARNINGS
# ============================================================

def build_warnings(web_research, event, report_stop_reason):

    warnings = []

    sources = web_research.get("sources", [])
    unverified = [s for s in sources if not s.get("verified")]
    grounded = web_research.get("grounded_source_count", 0)

    if sources and grounded == 0:
        warnings.append(
            f"None of the {len(sources)} cited sources were returned by "
            "web search. Treat all figures as unverified."
        )
    elif unverified:
        warnings.append(
            f"{len(unverified)} of {len(sources)} sources were not "
            "confirmed by web search (marked Unconfirmed in the source "
            "register)."
        )
    elif not sources:
        warnings.append(
            "Web research returned no sources. The report rests on news "
            "headlines only."
        )

    downgraded = web_research.get("research_meta", {}).get(
        "downgraded_records", 0
    )

    if downgraded:
        warnings.append(
            f"{downgraded} research "
            + ("statement" if downgraded == 1 else "statements")
            + " had no supporting source and "
            + ("was" if downgraded == 1 else "were")
            + " downgraded from CONFIRMED."
        )

    if any(
        c.startswith("amount raised set to not disclosed")
        for c in event.get("reconciliation", [])
    ):
        warnings.append(
            "The headline figure is a valuation; the amount raised "
            "was not disclosed."
        )

    if report_stop_reason == "max_tokens":
        warnings.append(
            "The report hit the model's output limit and may end "
            "abruptly."
        )

    return warnings


# ============================================================
# SAVE REPORT
# ============================================================

def report_key(company, extension, when=None):

    when = when or today_utc()

    return (
        f"{REPORT_PREFIX}/{when:%Y-%m-%d}/"
        f"{safe_name(company)}.{extension}"
    )


def save_report(company, report, when=None):
    """Save the Markdown report. Keys are dated so earlier reports for
    the same company are kept, not overwritten."""

    key = report_key(company, "md", when)

    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=report.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )

    return key


def save_pdf(company, pdf_bytes, when=None):

    key = report_key(company, "pdf", when)

    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=pdf_bytes,
        ContentType="application/pdf",
    )

    return key


# ============================================================
# BUILD PDF
# ============================================================

def build_pdf(
    company,
    report,
    key_facts,
    numbered_sources,
    warnings,
    research_packet=None,
):
    """Render the PDF. Returns None (and logs) if rendering fails, so the
    email can still go out with the Markdown attached."""

    try:
        pdf = render_report_pdf(
            report,
            company,
            key_facts=key_facts,
            sources=numbered_sources,
            warnings=warnings,
            research_packet=(
                research_packet if INCLUDE_RESEARCH_APPENDIX else None
            ),
        )
        print(f"PDF rendered: {len(pdf) / 1024:.0f} KB")
        return pdf

    except Exception as e:
        print(f"WARNING: PDF rendering failed ({type(e).__name__}: {e}).")
        return None


# ============================================================
# SEND REPORT VIA SES
# ============================================================

def _email_bodies(company, s3_key, key_facts, warnings, attached_pdf):

    attachment_note = (
        "The full report is attached as a PDF."
        if attached_pdf
        else "PDF rendering failed, so the Markdown report is attached."
    )

    plain_facts = "\n".join(
        f"  {label}: {value}"
        for label, value in key_facts
        if not is_missing(value) or label == "Amount raised"
    )

    plain_warnings = (
        "\nData quality notes:\n"
        + "\n".join(f"  - {w}" for w in warnings)
        + "\n"
        if warnings else ""
    )

    plain = (
        f"Company Intelligence Report\n\n"
        f"Company: {company}\n\n"
        f"{plain_facts}\n"
        f"{plain_warnings}\n"
        f"{attachment_note}\n\n"
        f"S3 location:\ns3://{S3_BUCKET}/{s3_key}\n"
    )

    rows = "".join(
        f'<tr><td style="padding:4px 16px 4px 0;color:#6B7280">'
        f"{escape(str(label))}</td>"
        f'<td style="padding:4px 0;font-weight:600">'
        f"{escape(str(value))}</td></tr>"
        for label, value in key_facts
    )

    warn_html = ""
    if warnings:
        items = "".join(f"<li>{escape(w)}</li>" for w in warnings)
        warn_html = (
            '<div style="background:#FFF7E6;border-left:3px solid #F59E0B;'
            'padding:8px 12px;margin:16px 0;font-size:13px">'
            f"<b>Data quality notes</b><ul style=\"margin:4px 0 0 16px;"
            f"padding:0\">{items}</ul></div>"
        )

    html = (
        '<div style="font-family:Arial,Helvetica,sans-serif;color:#1F2933;'
        'max-width:600px">'
        '<div style="font-size:11px;letter-spacing:1px;color:#6B7280">'
        "COMPANY INTELLIGENCE REPORT</div>"
        f'<h2 style="margin:4px 0 12px;color:#14213D">{escape(company)}</h2>'
        f'<table style="font-size:14px;border-collapse:collapse">{rows}'
        "</table>"
        f"{warn_html}"
        f'<p style="font-size:14px">{escape(attachment_note)}</p>'
        f'<p style="font-size:12px;color:#6B7280">'
        f"s3://{escape(S3_BUCKET)}/{escape(s3_key)}</p>"
        "</div>"
    )

    return plain, html


def send_report_email(
    company,
    report,
    s3_key,
    pdf_bytes=None,
    key_facts=None,
    warnings=None,
):

    print(f"Sending report email for {company}...")

    recipients = get_recipients()
    key_facts = key_facts or []
    warnings = warnings or []
    date_tag = today_utc().strftime("%Y-%m-%d")

    subject = f"Company Intelligence Report - {company}"

    if any("web search" in w for w in warnings):
        subject += " (unverified sources)"

    plain, html = _email_bodies(
        company, s3_key, key_facts, warnings, pdf_bytes is not None
    )

    message = MIMEMultipart("mixed")

    message["Subject"] = subject
    message["From"] = SES_SENDER_EMAIL
    message["To"] = ", ".join(recipients)
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(
        domain=SES_SENDER_EMAIL.split("@")[-1]
    )

    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(plain, "plain", "utf-8"))
    alternative.attach(MIMEText(html, "html", "utf-8"))
    message.attach(alternative)

    if pdf_bytes is not None:
        attachment = MIMEApplication(pdf_bytes, _subtype="pdf")
        filename = f"{safe_name(company)}_{date_tag}.pdf"
    else:
        attachment = MIMEText(report, "markdown", "utf-8")
        filename = f"{safe_name(company)}_{date_tag}.md"

    attachment.add_header(
        "Content-Disposition",
        "attachment",
        filename=filename,
    )

    message.attach(attachment)

    send_args = {
        "Source": SES_SENDER_EMAIL,
        "Destinations": recipients,
        "RawMessage": {"Data": message.as_bytes()},
    }

    if SES_CONFIGURATION_SET:
        send_args["ConfigurationSetName"] = SES_CONFIGURATION_SET

    response = ses.send_raw_email(**send_args)

    print("Email accepted by SES.")
    print(f"SES MessageId: {response.get('MessageId')}")

    return response.get("MessageId")


# ============================================================
# MAIN LAMBDA HANDLER
# ============================================================

def process_event(funding_event, general_articles, run_time):
    """Research, report, store and email one funding event."""

    company = funding_event["company"]

    # Company news
    print(f"Fetching company news for {company}...")
    company_articles = get_company_news(company)
    print(f"Found {len(company_articles)} company-specific articles")

    # Web research
    web_research = get_web_research(company, company_articles)
    research_packet = web_research["research_packet"]

    # Reconcile the classifier's event with research findings
    event = reconcile_event(funding_event, research_packet)
    key_facts = build_key_facts(event)

    # Final report
    print(f"Generating final report for {company}...")

    report_articles = select_report_articles(
        general_articles, company_articles, event
    )

    report, numbered_sources, report_stop, research_with_ids = generate_report(
        company,
        event,
        report_articles,
        web_research,
    )

    # Save Markdown report to S3
    s3_key = save_report(company, report, run_time)
    print(f"Report saved: s3://{S3_BUCKET}/{s3_key}")

    # PDF
    warnings = build_warnings(web_research, event, report_stop)
    pdf_bytes = build_pdf(
        company, report, key_facts, numbered_sources, warnings,
        research_packet=research_with_ids,
    )

    pdf_key = None
    if pdf_bytes is not None and SAVE_PDF_TO_S3:
        pdf_key = save_pdf(company, pdf_bytes, run_time)
        print(f"PDF saved: s3://{S3_BUCKET}/{pdf_key}")

    # Email
    email_message_id = send_report_email(
        company,
        report,
        s3_key,
        pdf_bytes=pdf_bytes,
        key_facts=key_facts,
        warnings=warnings,
    )

    if DEDUPE_EVENTS:
        mark_reported(funding_event, s3_key)

    print(f"Report emailed successfully: {company}")

    def count(name):
        return len(research_packet.get(name, []) or [])

    return {
        "company": company,
        "event": event,
        "s3_key": s3_key,
        "pdf_s3_key": pdf_key,
        "email_sent": True,
        "email_attachment": "pdf" if pdf_bytes is not None else "markdown",
        "email_message_id": email_message_id,
        "web_research_sources": web_research["grounded_source_count"],
        "sources_in_register": len(web_research["sources"]),
        "unverified_citations": len(web_research["unverified_citations"]),
        "research_executive_facts": count("executive_facts"),
        "research_claims": count("claims"),
        "research_customers": count("customers"),
        "research_technology_records": count("technology"),
        "research_competitors": count("competitors"),
        "research_funding": count("funding"),
        "research_unknowns": count("unknowns"),
        "warnings": warnings,
    }


def lambda_handler(event, context):

    event = event or {}
    run_time = today_utc()

    # Test events can set {"force": true} to ignore de-duplication.
    force = bool(event.get("force"))

    print("Starting company intelligence pipeline...")

    print("Fetching general funding news...")
    articles = get_news()
    print(f"Fetched {len(articles)} general articles")

    print("Classifying articles...")
    classification = classify_articles(articles)
    events = classification.get("events", []) or []

    if not events:
        print("No relevant funding events found.")
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "No new funding events found",
                "events": [],
            }),
        }

    print(f"Found {len(events)} relevant funding events")

    reports = []
    skipped = []
    seen_companies = set()

    for funding_event in events:

        company = (funding_event.get("company") or "").strip()

        if not company:
            print("Skipping event without company name")
            continue

        if company.lower() in seen_companies:
            print(f"Skipping duplicate event in this run: {company}")
            continue

        seen_companies.add(company.lower())
        funding_event["company"] = company

        if DEDUPE_EVENTS and not force and already_reported(funding_event):
            print(f"Already reported earlier, skipping: {company}")
            skipped.append(company)
            continue

        print("==========================================")
        print(f"Processing company: {company}")
        print("==========================================")

        try:
            reports.append(
                process_event(funding_event, articles, run_time)
            )

        except Exception as e:

            print(
                f"Error processing {company}: "
                f"{type(e).__name__}: {str(e)}"
            )

            reports.append({
                "company": company,
                "error": f"{type(e).__name__}: {str(e)}",
            })

        remaining = (
            context.get_remaining_time_in_millis()
            if context and hasattr(context, "get_remaining_time_in_millis")
            else None
        )

        if remaining is not None and remaining < 300_000:
            print(
                "Less than 5 minutes left; stopping before the next "
                "company to avoid a timeout."
            )
            break

    succeeded = [r for r in reports if "s3_key" in r]
    failed = [r for r in reports if "error" in r]

    # 200 all good, 207 partial failure, 500 everything failed.
    if failed and not succeeded:
        status_code = 500
    elif failed:
        status_code = 207
    else:
        status_code = 200

    return {
        "statusCode": status_code,
        "body": json.dumps({
            "reports_created": len(succeeded),
            "reports_failed": len(failed),
            "skipped_already_reported": skipped,
            "reports": reports,
        }, ensure_ascii=False),
    }

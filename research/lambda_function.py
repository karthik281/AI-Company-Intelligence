"""
company-intelligence-web-research

Two-step research for one company:

  1. GROUNDED RESEARCH  Nova with nova_grounding writes research notes in
     prose. Its citation blocks are captured as the source register, and a
     marker such as [S3] is inserted where each citation appears.
  2. FORMATTING         Nova without grounding converts the notes into the
     structured JSON packet, citing sources by ID only. The code then maps
     IDs back to the real URLs search returned.

Why: asking a grounded model to type URLs inside JSON broke generation
mid-string, and URLs it typed itself could not be verified. Now every URL
in the packet comes from an actual grounding citation.

Response contract (unchanged for the orchestrator):
  {"statusCode": 200, "body": json({
      "company", "research_packet", "sources", "research_meta"})}
"""

import json
import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import boto3
from botocore.config import Config


# ============================================================
# AWS CLIENT
# ============================================================

bedrock = boto3.client(
    "bedrock-runtime",
    region_name=os.environ.get("BEDROCK_REGION", "us-west-2"),
    config=Config(
        read_timeout=300,
        connect_timeout=30,
        retries={"max_attempts": 2, "mode": "standard"},
    ),
)


# ============================================================
# CONFIGURATION
# ============================================================

MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID",
    "us.amazon.nova-2-lite-v1:0",
)

# Model for the JSON formatting step. Defaults to the same model; it runs
# without web grounding, so any Bedrock text model works here.
FORMAT_MODEL_ID = os.environ.get("FORMAT_MODEL_ID", MODEL_ID)

RESEARCH_MAX_TOKENS = int(os.environ.get("RESEARCH_MAX_TOKENS", "8000"))
FORMAT_MAX_TOKENS = int(os.environ.get("FORMAT_MAX_TOKENS", "10000"))

# Grounded notes shorter than this are treated as a failed research call.
MIN_NOTES_CHARS = int(os.environ.get("MIN_NOTES_CHARS", "600"))

COLLECTIONS = [
    "executive_facts",
    "claims",
    "customers",
    "technology",
    "competitors",
    "funding",
]

REQUIRED_LISTS = [
    "claims",
    "customers",
    "technology",
    "competitors",
    "funding",
    "unknowns",
]


# ============================================================
# URL HELPERS
# ============================================================

_TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_src$)")


def normalise_url(url):
    """Comparable form of a URL: lower-case host, no fragment, no
    tracking parameters, no trailing slash."""

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

    path = parts.path.rstrip("/")

    return urlunsplit((parts.scheme.lower() or "https", host, path, query, ""))


def domain_of(url):
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


# ============================================================
# EXTRACT NOVA GROUNDED OUTPUT
# ============================================================

def extract_grounded_output(response):
    """
    Walk the response content in order. Text is appended as it comes;
    each citation is registered once (S1, S2, ...) and a [S#] marker is
    placed where it appears, so later steps know which source supports
    which statement.

    Handles both shapes seen in the Converse API: text and
    citationsContent in separate content items, or in the same item, and
    citation text nested under citationsContent.content.
    """

    content_list = (
        response
        .get("output", {})
        .get("message", {})
        .get("content", [])
    )

    text_parts = []
    register = []
    by_url = {}

    for content in content_list:

        if "text" in content:
            text_parts.append(content["text"])

        citations_block = content.get("citationsContent")

        if not citations_block:
            continue

        # Some responses carry the cited text inside the citation block.
        for generated in citations_block.get("content", []) or []:
            if isinstance(generated, dict) and generated.get("text"):
                text_parts.append(generated["text"])

        markers = []

        for citation in citations_block.get("citations", []) or []:

            web = citation.get("location", {}).get("web", {}) or {}
            url = web.get("url")

            if not url:
                continue

            key = normalise_url(url)

            if key not in by_url:
                source_id = f"S{len(register) + 1}"
                by_url[key] = source_id
                register.append({
                    "id": source_id,
                    "url": url,
                    "domain": web.get("domain") or domain_of(url),
                    "title": citation.get("title"),
                })

            sid = by_url[key]

            if sid not in markers:
                markers.append(sid)

        if markers:
            text_parts.append(" [" + ", ".join(markers) + "]")

    block_types = [
        "+".join(sorted(k for k in c.keys()))
        for c in content_list
    ]

    return {
        "text": "".join(text_parts).strip(),
        "sources": register,
        "stop_reason": response.get("stopReason"),
        "block_types": block_types,
        "usage": response.get("usage", {}),
    }


# ============================================================
# CLEAN MODEL JSON
# ============================================================

def clean_json_response(text):

    if not text:
        raise Exception("Model returned empty output")

    text = text.strip()

    # Remove Markdown fences if the model adds them.
    if text.startswith("```"):

        lines = text.splitlines()

        if lines:
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        text = "\n".join(lines).strip()

    # Isolate the outermost JSON object if there is text around it.
    first_brace = text.find("{")
    last_brace = text.rfind("}")

    if first_brace < 0:
        raise Exception("Model output contains no JSON object")

    if last_brace > first_brace:
        text = text[first_brace:last_brace + 1]
    else:
        # Opening brace but no closing one: the output was cut off.
        raise Exception(
            "Model output is an incomplete JSON object "
            f"({len(text)} chars, no closing brace)"
        )

    return text


# ============================================================
# VALIDATE RESEARCH PACKET
# ============================================================

def validate_research_packet(packet):

    if not isinstance(packet, dict):
        raise Exception("Research output is not a JSON object")

    if "company" not in packet:
        raise Exception("Research packet missing field: company")

    for field in REQUIRED_LISTS:

        if field not in packet:
            raise Exception(f"Research packet missing field: {field}")

        if not isinstance(packet[field], list):
            raise Exception(f"{field} must be a list")

    # Optional in the original schema; normalise to a list.
    if not isinstance(packet.get("executive_facts"), list):
        packet["executive_facts"] = []

    return packet


# ============================================================
# SHARED RESEARCH GUIDANCE
# ============================================================

RESEARCH_GUIDANCE = """
============================================================
RESEARCH AREAS
============================================================

1. Company overview
2. Company history
3. Founders and leadership
4. Headquarters and geography
5. Products
6. Customers
7. Customer deployments
8. Partnerships
9. Funding
10. Investors
11. Valuation
12. Market
13. Business model
14. Go-to-market
15. Pricing
16. Revenue signals
17. Technology
18. AI / ML
19. Foundation models
20. Agentic AI
21. Software architecture
22. Data
23. Automation
24. APIs
25. Integrations
26. Infrastructure
27. Security
28. Proprietary technology
29. Engineering organization
30. Hiring
31. Competitors
32. Recent developments
33. Risks
34. Important unknowns

============================================================
EVIDENCE DISCIPLINE
============================================================

Every important fact must be connected to evidence.

Use these statuses:

CONFIRMED  The source directly supports the claim.
INFERRED   A reasonable conclusion from multiple pieces of evidence.
UNKNOWN    Public evidence does not establish the claim.

Never convert UNKNOWN into CONFIRMED.
Never use industry norms as company-specific evidence.

BAD:  "The company likely uses AWS."
GOOD: "The company's cloud provider is not publicly established."

BAD:  "The company uses an agentic architecture because it automates
       workflows."
GOOD: "Public evidence describes workflow automation, but does not
       establish an agentic architecture."

BAD:  "Revenue likely comes from SaaS subscriptions."
GOOD: "The company's pricing and revenue model are not publicly
       established."

============================================================
CUSTOMER DISCIPLINE
============================================================

For every organization mentioned as a customer, determine the strongest
supported classification:

confirmed_customer | named_customer | case_study | partnership | pilot |
company_claim | unknown

Do not automatically classify a company as a customer.
Prefer evidence from the customer's own website.

============================================================
TECHNOLOGY DISCIPLINE
============================================================

Only report specific technologies when publicly evidenced. Look for
evidence in company engineering pages, technical, API, developer and
product documentation, engineering job descriptions, technical blogs,
founder and CTO interviews, and customer implementation material.

Do not infer AWS, Azure, GCP, Kubernetes, Docker, PostgreSQL, MongoDB,
vector databases, RAG, specific LLMs, specific foundation models,
microservices, event-driven architecture or agent frameworks unless
evidence supports them.

============================================================
AGENTIC AI
============================================================

Do not classify automation as agentic AI. Evidence for agentic AI
involves autonomous task execution, multi-step reasoning, tool use,
planning, stateful execution, autonomous decisions or agent
orchestration. If such evidence is absent, the status is UNKNOWN.

============================================================
COMPETITORS
============================================================

Identify direct competitors, adjacent competitors, incumbent
alternatives and internal build. For each, explain why it overlaps, the
capability overlap and important differences. Do not list companies
merely because they are in the same broad industry.

============================================================
FUNDING
============================================================

Keep the amount raised and the valuation separate. A headline such as
"raises funding at a Rs 16.7 Cr valuation" states a valuation, not the
amount raised. If the amount raised is not stated, say so.

============================================================
SOURCE QUALITY
============================================================

Prefer, in order: company website, customer website, official
announcements, regulatory or government sources, major financial
publications, major newspapers, industry publications, technical
publications, founder or executive interviews, reputable startup
publications, job postings, other secondary sources.

Use multiple sources for major claims where possible.
"""


# ============================================================
# STEP 1: GROUNDED RESEARCH NOTES
# ============================================================

def build_research_prompt(company, news_articles, retry=False):

    news_context = json.dumps(news_articles, indent=2, ensure_ascii=False)

    retry_instruction = ""

    if retry:
        retry_instruction = """

============================================================
RETRY INSTRUCTION
============================================================

Your previous attempt ended before the notes were complete. Write the
complete notes this time, keeping each bullet short. Search the web as
needed. Do not stop until every section below has been written.
"""

    return f"""
You are the research engine for a professional company-intelligence
system.

COMPANY
=======
{company}

INITIAL GOOGLE NEWS CONTEXT
===========================
{news_context}

Use Web Grounding extensively. The Google News material is only an
initial lead. Do NOT assume it is accurate.

Your objective is to write research notes that another step will turn
into a structured evidence packet.
{RESEARCH_GUIDANCE}
============================================================
OUTPUT FORMAT
============================================================

Write plain-text notes under exactly these headings:

## Executive facts
## Company, leadership and history
## Products, market and business model
## Customers
## Technology
## Funding
## Competitors
## Recent developments and risks
## Unknowns

Under each heading, write one short bullet per fact, ending with its
status and confidence in brackets, for example:

- Unveilr AI raised a pre-seed round led by AJVC. (CONFIRMED, HIGH)

Customer bullets also state the classification. Competitor bullets state
the category (direct, adjacent, incumbent, internal_build), why it
overlaps and the main difference. Funding bullets state date, round,
amount raised, lead investor, other investors and valuation, writing
"Not publicly disclosed" for anything unknown. Unknown bullets state the
question and why public evidence does not answer it.

IMPORTANT:
- Do NOT write URLs or a source list. Sources are recorded
  automatically from your web searches.
- Do NOT write JSON.
- NO INVENTION. NO GENERIC INDUSTRY ASSUMPTIONS. NO UNSUPPORTED
  CUSTOMER, TECHNOLOGY, BUSINESS MODEL OR AGENTIC AI CLAIMS.
{retry_instruction}
"""


def call_nova_grounded(company, news_articles, retry=False):

    prompt = build_research_prompt(company, news_articles, retry)

    print(
        "Calling Nova Web Grounding"
        + (" (retry)" if retry else "")
        + "..."
    )

    response = bedrock.converse(
        modelId=MODEL_ID,
        system=[{
            "text": (
                "You are a conservative company intelligence researcher. "
                "Use Web Grounding extensively. Every material claim must "
                "be supported by evidence. Never invent facts."
            )
        }],
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        toolConfig={"tools": [{"systemTool": {"name": "nova_grounding"}}]},
        inferenceConfig={
            "maxTokens": RESEARCH_MAX_TOKENS,
            "temperature": 0,
        },
    )

    grounded = extract_grounded_output(response)

    print(f"Nova stopReason: {grounded['stop_reason']}")
    print(f"Nova content blocks: {len(grounded['block_types'])}")
    print(f"Nova block types: {sorted(set(grounded['block_types']))}")
    print(f"Nova notes length: {len(grounded['text'])}")
    print(f"Grounding citations (unique sources): {len(grounded['sources'])}")
    print(f"Nova usage: {grounded['usage']}")
    print("Notes preview (first 1500 chars):")
    print(grounded["text"][:1500])

    return grounded


def research_notes(company, news_articles):
    """Grounded notes, retried once if they came back unusably short."""

    grounded = call_nova_grounded(company, news_articles)

    too_short = len(grounded["text"]) < MIN_NOTES_CHARS

    if too_short or grounded["stop_reason"] == "max_tokens":

        print(
            "Grounded notes incomplete "
            f"(length {len(grounded['text'])}, "
            f"stopReason {grounded['stop_reason']}). Retrying once."
        )

        retry = call_nova_grounded(company, news_articles, retry=True)

        # Keep whichever attempt produced more usable material.
        if len(retry["text"]) > len(grounded["text"]):
            grounded = retry

    if len(grounded["text"]) < MIN_NOTES_CHARS:
        raise Exception(
            "Nova Web Grounding returned too little text to research "
            f"from ({len(grounded['text'])} chars, "
            f"stopReason {grounded['stop_reason']})"
        )

    return grounded


# ============================================================
# STEP 2: FORMAT NOTES INTO THE JSON PACKET
# ============================================================

def build_format_prompt(company, notes, sources, retry=False):

    register = json.dumps(
        [{"id": s["id"], "domain": s["domain"], "url": s["url"]}
         for s in sources],
        indent=2,
        ensure_ascii=False,
    )

    retry_instruction = ""

    if retry:
        retry_instruction = """

RETRY INSTRUCTION
Your previous response could not be parsed as JSON. This time return ONLY
one complete JSON object, starting with { and ending with }, with no
Markdown, no ``` fences and no commentary. Close every string, array and
object. Keep text fields short so the whole object fits.
"""

    return f"""
Convert the research notes below into a structured evidence packet.

COMPANY
=======
{company}

RESEARCH NOTES
==============
Markers such as [S2] show which source supports the text just before
them.

{notes}

SOURCE REGISTER
===============
{register}

RULES
=====
- Use only facts in the notes. Do not add knowledge of your own.
- Cite sources ONLY by ID in "source_ids", using IDs that appear next to
  that fact in the notes. Never write URLs.
- If a fact has no marker, use "source_ids": [] and do not mark it
  CONFIRMED.
- Keep statuses, confidence and customer classifications as the notes
  state them.
- Funding "amount" is money raised. Never copy the valuation into
  "amount"; if the amount raised is not stated use
  "Not publicly disclosed".
- Keep evidence descriptions concise.

Return ONLY valid JSON with this exact structure:

{{
  "company": "{company}",
  "executive_facts": [],
  "claims": [],
  "customers": [],
  "technology": [],
  "competitors": [],
  "funding": [],
  "unknowns": []
}}

executive_facts and claims records:
{{"claim": "Specific factual statement",
  "status": "CONFIRMED | INFERRED | UNKNOWN",
  "confidence": "HIGH | MEDIUM | LOW",
  "evidence": "What the source actually establishes",
  "source_ids": ["S1"]}}

customers records:
{{"organization": "Organization name",
  "classification": "confirmed_customer | named_customer | case_study | partnership | pilot | company_claim | unknown",
  "evidence": "What establishes the relationship",
  "source_ids": ["S1"]}}

technology records:
{{"area": "AI / ML / Agentic AI / Architecture / Data / API / Infrastructure / Security / etc.",
  "claim": "Specific technology statement",
  "status": "CONFIRMED | INFERRED | UNKNOWN",
  "confidence": "HIGH | MEDIUM | LOW",
  "evidence": "What establishes it",
  "source_ids": ["S1"]}}

competitors records:
{{"name": "Company",
  "category": "direct | adjacent | incumbent | internal_build",
  "why_relevant": "Specific capability overlap",
  "difference": "Important difference",
  "source_ids": ["S1"]}}

funding records:
{{"date": "YYYY-MM-DD or Not publicly disclosed",
  "round": "Round or Not publicly disclosed",
  "amount": "Amount raised or Not publicly disclosed",
  "lead_investor": "Investor or Not publicly disclosed",
  "other_investors": [],
  "valuation": "Valuation or Not publicly disclosed",
  "source_ids": ["S1"]}}

unknowns records:
{{"question": "Important unanswered question",
  "reason": "Why public evidence does not establish it"}}
{retry_instruction}
"""


def call_formatter(company, notes, sources, retry=False):

    print(
        "Formatting research notes into JSON"
        + (" (retry)" if retry else "")
        + "..."
    )

    response = bedrock.converse(
        modelId=FORMAT_MODEL_ID,
        system=[{
            "text": (
                "You convert research notes into JSON exactly as "
                "instructed. You never add facts and never write URLs."
            )
        }],
        messages=[{
            "role": "user",
            "content": [{
                "text": build_format_prompt(company, notes, sources, retry)
            }],
        }],
        inferenceConfig={
            "maxTokens": FORMAT_MAX_TOKENS,
            "temperature": 0,
        },
    )

    content = response.get("output", {}).get("message", {}).get("content", [])
    text = "".join(c["text"] for c in content if "text" in c)
    stop_reason = response.get("stopReason")

    print(f"Formatter stopReason: {stop_reason}")
    print(f"Formatter output length: {len(text)}")

    if stop_reason == "max_tokens":
        print("WARNING: formatter output hit maxTokens and is truncated")

    return text


def parse_research_output(raw_text):

    cleaned = clean_json_response(raw_text)

    try:
        packet = json.loads(cleaned)

    except json.JSONDecodeError as e:

        print("JSON parsing failed.")
        print(f"JSON error: {str(e)}")
        print("First 2000 characters of model output:")
        print(cleaned[:2000])
        print("Last 1000 characters of model output:")
        print(cleaned[-1000:])

        raise

    return validate_research_packet(packet)


def format_packet(company, notes, sources):
    """Format the notes; on a parse failure retry the formatter only
    (the grounded research is reused, not repeated)."""

    raw = call_formatter(company, notes, sources)

    try:
        return parse_research_output(raw)

    except Exception as first_error:

        print(
            "First formatter output could not be parsed: "
            f"{type(first_error).__name__}: {first_error}"
        )
        print("Retrying formatter with stricter JSON instructions...")

    raw = call_formatter(company, notes, sources, retry=True)

    return parse_research_output(raw)


# ============================================================
# ATTACH REAL URLS TO RECORDS
# ============================================================

def attach_source_urls(packet, sources):
    """
    Replace source IDs with the grounding URLs they stand for. Unknown IDs
    are dropped. A CONFIRMED claim left with no real source is downgraded
    to INFERRED / LOW, because nothing retrieved backs it.
    """

    by_id = {s["id"]: s for s in sources}
    stats = {"dropped_ids": 0, "downgraded": 0}

    for collection in COLLECTIONS:

        records = packet.get(collection, [])

        for record in records:

            if not isinstance(record, dict):
                continue

            ids = record.get("source_ids") or []

            if isinstance(ids, str):
                ids = [ids]

            valid = []

            for sid in ids:
                sid = str(sid).strip().strip("[]")
                if sid in by_id and sid not in valid:
                    valid.append(sid)
                else:
                    stats["dropped_ids"] += 1

            record["source_ids"] = valid
            record["source_urls"] = [by_id[sid]["url"] for sid in valid]

            if (
                not valid
                and str(record.get("status", "")).upper() == "CONFIRMED"
            ):
                record["status"] = "INFERRED"
                record["confidence"] = "LOW"
                record["evidence"] = (
                    (record.get("evidence") or "").rstrip(". ")
                    + ". No retrieved source supports this directly."
                ).lstrip(". ")
                stats["downgraded"] += 1

    used = {
        sid
        for collection in COLLECTIONS
        for record in packet.get(collection, [])
        if isinstance(record, dict)
        for sid in record.get("source_ids", [])
    }

    packet["sources"] = [
        {"id": s["id"], "url": s["url"], "domain": s["domain"],
         "cited_in_packet": s["id"] in used}
        for s in sources
    ]

    # Kept for compatibility with earlier packets.
    packet["grounding_sources"] = [
        {"id": s["id"], "url": s["url"], "domain": s["domain"]}
        for s in sources
    ]

    return packet, stats


# ============================================================
# RESEARCH COMPANY
# ============================================================

def research_company(company, news_articles):

    grounded = research_notes(company, news_articles)

    sources = grounded["sources"]

    if not sources:
        print(
            "WARNING: Nova returned no grounding citations. Every claim "
            "will be unsourced and none can be CONFIRMED."
        )

    packet = format_packet(company, grounded["text"], sources)

    packet["company"] = company

    packet, stats = attach_source_urls(packet, sources)

    print(
        f"Source mapping: {stats['dropped_ids']} unknown IDs dropped, "
        f"{stats['downgraded']} unsourced CONFIRMED records downgraded"
    )

    meta = {
        "grounded": bool(sources),
        "grounding_source_count": len(sources),
        "research_stop_reason": grounded["stop_reason"],
        "notes_length": len(grounded["text"]),
        "dropped_source_ids": stats["dropped_ids"],
        "downgraded_records": stats["downgraded"],
    }

    return {
        "packet": packet,
        "sources": sources,
        "notes": grounded["text"],
        "meta": meta,
    }


# ============================================================
# MAIN HANDLER
# ============================================================

def lambda_handler(event, context):

    print("Starting Nova Web Grounding research Lambda")

    company = (event or {}).get("company")

    if not company or not str(company).strip():
        print("Research error: no company supplied in the event")
        return {
            "statusCode": 400,
            "body": json.dumps({
                "company": company,
                "error": "Event must include a non-empty 'company'",
            }),
        }

    company = str(company).strip()
    news_articles = event.get("news_articles", [])

    print(f"Researching company: {company}")
    print(f"Google News articles received: {len(news_articles)}")

    try:

        result = research_company(company, news_articles)

        packet = result["packet"]

        print("Nova Web Grounding completed")
        print(f"Grounding sources found: {len(result['sources'])}")
        print(f"Executive facts: {len(packet['executive_facts'])}")
        print(f"Research claims: {len(packet['claims'])}")
        print(f"Customer records: {len(packet['customers'])}")
        print(f"Technology records: {len(packet['technology'])}")
        print(f"Competitors: {len(packet['competitors'])}")
        print(f"Funding records: {len(packet['funding'])}")
        print(f"Unknowns: {len(packet['unknowns'])}")

        return {
            "statusCode": 200,
            "body": json.dumps({
                "company": company,
                "research_packet": packet,
                "sources": [
                    {"id": s["id"], "url": s["url"], "domain": s["domain"]}
                    for s in result["sources"]
                ],
                "research_notes": result["notes"],
                "research_meta": result["meta"],
            }, ensure_ascii=False),
        }

    except Exception as e:

        print(f"Research error: {type(e).__name__}: {str(e)}")

        return {
            "statusCode": 500,
            "body": json.dumps({
                "company": company,
                "error": f"{type(e).__name__}: {str(e)}",
            }),
        }

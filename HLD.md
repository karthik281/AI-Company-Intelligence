# High-Level Design

## 1. Purpose

**Dawnbrief** (the product name from v2) sends each registered user one
deeply researched brief per day. Once a day, the pipeline scans funding and
investor news for the regions and industries its users follow, picks out
genuine events, matches at most one unseen event to each user, researches
each chosen company or investor with grounded web search, writes a detailed
report, and emails it as a PDF. It runs unattended on a schedule; users
manage their own profile on a website; an operator occasionally tunes the
prompts. See DESIGN_V2.md for the v2 design and its rationale.

Status: the pipeline code (phase 1) and the profile API / website code
(phases 2-3) are written and tested. The AWS resources for phases 2-3 are
created by `scripts/setup_v2.sh` (see TODO.md) and are **not yet deployed**;
until then production runs the previous single-recipient-list behaviour.

## 2. Architecture

```
Website (S3 + CloudFront) ─► Cognito sign-in ─► HTTP API ─► Profile Lambda
                                                       │
                                                       ▼
                                            DynamoDB `dawnbrief-users`
                                  (email, roles, regions, industries, active)
                                                       │ scanned daily
EventBridge Scheduler (cron, daily 06:00 IST)          │
        │  invokes, with retries + DLQ on failure      │
        ▼                                              ▼
Orchestrator Lambda (ap-south-1 / Mumbai) ◄── users and profiles
        │
        ├─ Google News RSS (fixed US edition; region/industry terms
        │   built from the users' profiles) ──► classify via Bedrock
        │                          (kind, region, industry per event)
        ├─ assign at most ONE unseen event per user (shared events
        │   preferred, capped at MAX_ITEMS_PER_RUN distinct companies)
        │
        ├─ per distinct chosen event ─────────► Research Lambda
        │                                        (us-west-2 / Oregon)
        │                                             │
        │                                             ├─ Nova + web grounding
        │                                             │   → research notes
        │                                             │     + real citations
        │                                             └─ Nova (ungrounded)
        │                                                 → structured JSON,
        │                                                   cites sources by ID
        │
        ├─ reconcile event with research, generate report (Bedrock)
        ├─ render PDF (pure Python: markdown + reportlab)
        ├─ save Markdown (+ optionally PDF) to S3
        └─ email the PDF via SES, one message per user, then record it in
           that user's history (S3 `state/sent/`)
```

Supporting AWS resources: an S3 bucket for prompts, the recipient list,
reports, and de-duplication markers; an SQS dead-letter queue and
CloudWatch alarms + SNS topics for failure visibility.

## 3. Why two Lambdas, two regions

- **Research runs in us-west-2** because that's where Nova's web-grounding
  tool (`nova_grounding`) is available at the time this was built.
- **Orchestrator runs in ap-south-1** because that's where the user
  operates from and where SES is set up.
- Splitting them also isolates the long-running, retry-free research call
  (research runs for minutes; the orchestrator's client is configured not
  to auto-retry it, since the research Lambda has no cheap way to resume
  a half-finished grounded-research call).

## 4. Why research is two Bedrock calls, not one

A single grounded call that both searches the web *and* emits JSON tended
to break mid-string when the model tried to type a URL inside a JSON
value, and a URL the model typed itself can't be verified against what
search actually returned. So:

1. **Grounded pass** (`nova_grounding` tool): the model writes prose notes
   and cites as it goes; every citation it returns is captured verbatim
   as the source register (S1, S2, ...).
2. **Formatting pass** (no grounding): converts the notes into the
   structured JSON packet, citing sources **by ID only**. The orchestrator
   then maps IDs back to the real, search-returned URLs.

Any ID the formatter invents that wasn't in the register is dropped, and
any `CONFIRMED` claim that ends up with no real source is downgraded to
`INFERRED`/`LOW`. This is the mechanism behind the project rule that
report URLs must come from grounding citations, never model-typed text.

## 5. Key design decisions

- **Amount raised vs. valuation are always kept separate** - headlines
  like "raises funding at a Rs 16.7 Cr valuation" state a valuation, not an
  amount. `reconcile_event()` detects when a headline's figure matches the
  valuation (or when the amount only appears next to the word
  "valuation" in the title) and re-labels it, in both the orchestrator and
  the research model's instructions.
- **Recipients come from an S3 file, not just an env var** - so the
  distribution list can grow without a redeploy. `SES_RECIPIENT_EMAIL`
  is kept as a fallback if the file is missing or unreadable.
- **PDF rendering failure never blocks the email** - `build_pdf()` catches
  any rendering exception and the email goes out with the Markdown report
  attached instead.
- **De-duplication is a flag, not a hard requirement** - `DEDUPE_EVENTS`
  controls whether an S3 marker (`state/reported/<company>__<round>.json`)
  is checked/written, so the same funding event isn't reported twice
  across daily runs.
- **One report per user per day, chosen by profile** - roles decide the
  kind of event (founders: `startup_round`; investors: `investor_activity`;
  both: either), then region and industry must match the user's choices.
  Events the classifier could not tag match everyone rather than being
  dropped. Many users with overlapping interests share one research run, so
  cost scales with distinct companies per day, not with users.
- **Region comes from the profile, not from where the Lambda runs** - the
  Google News edition is pinned to US English and regional coverage comes
  from region words in the query. (Previously the Mumbai Lambda silently
  produced the India edition.)
- **Investor coverage reuses the research contract** - an investor or fund
  is researched through the same Lambda and packet shape with a `kind`
  hint that re-reads the headings (customers = portfolio, technology =
  thesis, funding = fund raises and deals); only the report prompt differs.
- **Email is sent per recipient** - SES rejects a whole call if any one
  destination is unauthorised or unverified, so addresses are never batched
  and one bad address cannot block the others. `To` shows the sender, so
  recipients never see each other (newsletter style).
- **Per-company failure isolation** - one company's research/report/email
  failure is caught, logged, and recorded in the response; it doesn't stop
  the rest of that day's events from being processed. The handler also
  watches its own remaining Lambda time and stops cleanly before a
  15-minute timeout, rather than being killed mid-company.

## 6. Non-functional posture (current state)

| Concern | Status |
|---|---|
| Secrets | No secrets in code; SES/Bedrock access is via IAM role, not keys. A leftover, unused API key was found and removed from the research Lambda's env vars during the Oct 2026 audit. |
| Least privilege | Each Lambda's IAM role is scoped to the specific S3 prefixes, SES identity, and Bedrock model/inference-profile ARNs it needs (tightened from a `Resource: "*"` Bedrock grant during the same audit). |
| Reliability | EventBridge Scheduler retries a failed run twice before sending it to an SQS dead-letter queue, so a bad day doesn't just vanish. |
| Observability | CloudWatch alarms on both Lambdas' `Errors` metric notify an SNS topic (email) in each region. Logs retain 30 days. |
| Data durability | The reports bucket has versioning on (an overwrite is recoverable) and a lifecycle rule that expires old versions after 90 days rather than growing forever. |
| Email deliverability | SES is still in **sandbox mode** - only verified addresses can receive mail, so public signup cannot deliver until SES production access is granted (which needs a sending domain). The profile API asks SES to verify each new user's address as a stopgap. Sending from a gmail.com address also fails DMARC alignment (Gmail shows an impersonation warning); a domain you own, verified with DKIM, fixes both. |
| Authentication | Cognito user pool (email/username + password; Google can be added), PKCE code flow from the site, API Gateway JWT authorizer. The profile API reads the caller's email from verified token claims only - never from the request body. |

## 7. Known limitations / deliberate non-goals

- **Scale ceiling of the single orchestrator Lambda.** Distinct companies
  are researched sequentially (about 1-2.5 minutes each) and emails are sent
  one at a time within a 15-minute invocation. That is fine up to roughly
  5-8 companies and a few hundred users per run; beyond that the run must
  fan out (SQS or Step Functions: one message per company, one per batch of
  recipients). Tracked in BACKLOG.md.
- No VPC, no provisioned/reserved concurrency, no X-Ray tracing - the
  workload is a single daily batch job, not a latency-sensitive service.
- The CloudWatch alarms watch the Lambda `Errors` metric (unhandled
  exceptions/throttles). A *partial* failure - one company's report
  failing while others succeed, reported as HTTP 207 in the response body
  - does not currently trigger an alarm; it's only visible by reading the
  response or the logs.
- No CI pipeline runs the tests automatically on push; `tests/test_*.py`
  are run manually (or by an agent) before deploying.

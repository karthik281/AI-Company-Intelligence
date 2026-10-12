# Low-Level Design

Companion to HLD.md. This is the reference for exact contracts, schemas,
AWS resource names, and IAM permissions as they exist in the account
today (last verified 2026-10-01/02) plus the v2 resources defined in
`scripts/setup_v2.sh` that are **not yet created** (section 10).

## 1. Deployed AWS resources

| Resource | Name / ARN | Notes |
|---|---|---|
| Orchestrator Lambda | `company-intelligence-agent` (ap-south-1) | python3.14, x86_64, 512 MB, 900s timeout, role `CompanyIntelligenceLambdaRole` |
| Research Lambda | `company-intelligence-web-research` (us-west-2) | python3.14, x86_64, 256 MB, 600s timeout, role `CompanyIntelligenceResearchRole` |
| Trigger | EventBridge Scheduler `company-intelligence-daily` | `cron(0 6 * * ? *)` in `Asia/Calcutta`, 60 min flexible window, invokes the orchestrator with `{}` |
| Scheduler retry/DLQ | `MaximumRetryAttempts: 2`, DLQ = SQS `company-intelligence-dlq` (ap-south-1) | Added 2026-10-02; was 0 retries / no DLQ before |
| Profile API Lambda (v2, **not yet created**) | `dawnbrief-profile-api` (ap-south-1) | python3.14, 256 MB, 15s, role `DawnbriefProfileApiRole` |
| S3 bucket | `company-intelligence-2026` (ap-south-1) | SSE-AES256, public access fully blocked, versioning **on**, lifecycle: noncurrent versions expire after 90 days, incomplete multipart uploads abort after 7 |
| Alerting | SNS topic `company-intelligence-alerts` in **both** ap-south-1 and us-west-2, email-subscribed to the operator | CloudWatch alarms `company-intelligence-orchestrator-errors` (ap-south-1) and `company-intelligence-research-errors` (us-west-2) fire on `AWS/Lambda` `Errors` >= 1/day per function |
| Log retention | 30 days on both Lambdas' log groups | Was unlimited before 2026-10-02 |
| SES | Sandbox mode; one verified identity (operator's address), ap-south-1 | 200 msgs/day, 1/sec cap while sandboxed |

## 2. IAM policies (as of 2026-10-02)

**`CompanyIntelligenceLambdaRole`** (orchestrator), in addition to the
managed `AWSLambdaBasicExecutionRole`:

| Inline policy | Grants |
|---|---|
| `CompanyIntelligenceBedrockS3` | `bedrock:InvokeModel` on the orchestrator's application-inference-profile ARN and `arn:aws:bedrock:*::foundation-model/*`; `s3:PutObject` on `company-intelligence-2026/*` |
| `CompanyIntelligencePromptRead` | `s3:GetObject` on `company-intelligence-2026/prompts/*` |
| `CompanyIntelligenceConfigRead` | `s3:GetObject` on `company-intelligence-2026/config/*` (added 2026-10-02 for `recipients.txt`) |
| `CompanyIntelligenceSESPolicy` | `ses:SendEmail` / `ses:SendRawEmail`, scoped to specific verified identity ARNs (rao.kar@ and aayush.jain@leopathway.com as of 2026-10-12). **v2 replaces this with `identity/*`** because every user is their own identity. |
| `DawnbriefOrchestratorAccess` (v2, **to be added by setup_v2.sh**) | `dynamodb:Scan` on the users table; `s3:GetObject` on `state/*` (needed to read per-user history; the role could previously only write there). **Deploying the phase-1 orchestrator before this exists makes every run fail.** |
| `InvokeCompanyResearchLambda` | `lambda:InvokeFunction` on the research Lambda's ARN |

**`CompanyIntelligenceResearchRole`** (research), in addition to the
managed `AWSLambdaBasicExecutionRole`:

- `bedrock:InvokeModel` scoped to specific inference-profile/foundation-model ARNs
- `bedrock-websearch:InvokeSearch` / `InvokeFetch`
- `bedrock:InvokeTool` on the `nova_grounding` system tool

**`Amazon_EventBridge_Scheduler_LAMBDA_cb8b89385f`** (scheduler's execution
role): `lambda:InvokeFunction` on the orchestrator (managed policy), plus
inline `CompanyIntelligenceDLQAccess` granting `sqs:SendMessage` on the DLQ
(added 2026-10-02).

## 3. S3 key layout

```
company-intelligence-2026/
  prompts/
    funding_classifier.md      # read by orchestrator, cached per container
    company_report.md
    investor_report.md         # v2; falls back to company_report.md if absent
  config/
    recipients.txt             # one email per line, '#' comments; see get_recipients()
    users.json                 # optional v2 profile list (used when USERS_TABLE is unset)
  reports/
    YYYY-MM-DD/
      <Company_Name>.md
      <Company_Name>.pdf       # only if SAVE_PDF_TO_S3=true
  state/
    reported/
      <company>__<round>.json  # de-dupe marker; only written if DEDUPE_EVENTS=true
    sent/
      <sha256(email)[:24]>.json  # {"sent": [event keys]} emailed to that user
```

## 4. Request/response contracts

### 4.1 Orchestrator -> Research Lambda (synchronous `Invoke`)

Request (event passed to the research Lambda):
```json
{"company": "Acme Inc", "kind": "startup_round", "news_articles": [{"title": "...", "url": "...", "published": "...", "description": "...", "publisher": "...", "publisher_url": "..."}]}
```
`kind` is optional (`startup_round` default, or `investor_activity`); an
older research Lambda ignores it and an older orchestrator simply omits it,
so the two can be deployed in either order.

Response body (JSON-encoded string inside `{"statusCode": 200, "body": "..."}`):
```json
{
  "company": "Acme Inc",
  "research_packet": {
    "executive_facts": [...], "claims": [...], "customers": [...],
    "technology": [...], "competitors": [...], "funding": [...],
    "unknowns": [...], "sources": [...], "grounding_sources": [...]
  },
  "sources": [{"id": "S1", "url": "...", "domain": "..."}],
  "research_notes": "raw grounded notes text",
  "research_meta": {
    "grounded": true, "grounding_source_count": 3,
    "research_stop_reason": "end_turn", "notes_length": 1121,
    "dropped_source_ids": 1, "downgraded_records": 1
  }
}
```
`statusCode: 400` means the event was missing `company`; `500` means the
research call failed (notes too short, JSON unparseable after retry,
etc.) -- `error` holds a short description. The orchestrator treats any
non-200 as a per-company failure (caught, logged, reported, not fatal to
the run). **This contract is shared by both Lambdas; CLAUDE.md requires
changing both sides together if it changes.**

Every record in each `research_packet` collection carries `source_ids`
(validated against the register) and `source_urls` (resolved real URLs) --
never a raw URL typed by the formatting model.

### 4.2 Orchestrator Lambda handler

Input: `{}` normally; `{"force": true}` to bypass de-duplication **and**
the per-user "already sent" history for a manual/test invocation (this
re-sends to everyone, so use it sparingly).

Output:
```json
{
  "statusCode": 200,             // 200 all succeeded, 207 partial, 500 all failed
  "body": "{\"reports_created\": N, \"reports_failed\": N, \"skipped_already_reported\": [...], \"users\": N, \"users_without_match\": [...], \"assignments\": {\"email\": \"Company\"}, \"reports\": [...]}"
}
```
Each entry in `reports` is either a success record (see
`process_event()`'s return value: company, event, s3_key, pdf_s3_key,
email_sent, delivered_to, research/source counters, warnings) or `{"company": ..., "error": "ExceptionType: message"}`.

### 4.3 Funding event shape (classifier output, then reconciled)

```json
{
  "relevant": true, "kind": "startup_round | investor_activity",
  "region": "North America | LATAM | EMEA | APAC | Global/Other",
  "industry": "one of the 18 in orchestrator/taxonomy.py",
  "company": "Acme Inc (the investor/fund name for investor_activity)", "event_type": "funding",
  "round": "Seed", "amount": "Not publicly disclosed",
  "investors": ["..."], "lead_investor": "...",
  "event_date": "YYYY-MM-DD", "source_title": "...", "source_url": "...",
  "valuation": "...", "supporting_urls": ["..."],
  "reconciliation": ["... taken from web research", "..."]
}
```
`amount` and `valuation` are always distinct fields -- never conflated,
per the project rule and `reconcile_event()`'s headline-vs-valuation
correction (see HLD.md section 5).

## 5. Environment variables reference

See README.md for the full, user-facing table (it's kept current there
rather than duplicated). Two additions since the README was first
written:

- `RECIPIENTS_S3_KEY` (orchestrator, default `config/recipients.txt`):
  override the S3 key `get_recipients()` reads.
- v2 orchestrator variables: `USERS_TABLE` (DynamoDB table of profiles; if
  empty, `USERS_S3_KEY` default `config/users.json`, then `recipients.txt`),
  `MAX_ITEMS_PER_RUN` (default 10, distinct companies researched per run),
  `MAX_NEWS_QUERIES` (24), `GENERAL_NEWS_LIMIT` (now per query, default 10),
  `CLASSIFIER_CHUNK` (25 articles per classifier call),
  `CLASSIFIER_MAX_TOKENS` (4000). `NEWS_QUERY` is now an optional *extra*
  founder query (default empty); the old default `AI startup funding` is gone
  because queries are built from profiles.
- Profile API: `USERS_TABLE` (required), `VERIFY_IN_SES` (default `true`:
  ask SES to verify a user's address on first save; set `false` once SES has
  production access), `SES_REGION`.
- Research Lambda env vars no longer include `OPENAI_API_KEY` /
  `OPENAI_BASE_URL` -- these were leftover/unused and removed 2026-10-02.

## 6. Error handling matrix

| Failure | Behavior |
|---|---|
| Research Lambda returns non-200, or invocation itself errors | Caught in `process_event()` via the raise in `get_web_research()`; surfaces as a per-company `error` entry in the orchestrator's response. Rest of the run continues. |
| Classifier/report Bedrock call hits `max_tokens` | Classifier: raises (fatal for that run -- no events are safe to act on from truncated JSON). Report: recorded as a warning in `build_warnings()`, report still sent. |
| PDF rendering throws any exception | Caught in `build_pdf()`; email sends with the Markdown report attached instead, noting this in the message body. |
| Research JSON formatter output unparseable | Retried once with a stricter prompt (`format_packet()`); if that also fails, the exception propagates as a per-company failure. |
| Per-user history read fails (`AccessDenied` on `state/*`) | Raises, failing the run before anything is sent. Deliberate: continuing would re-send yesterday's items. Fix the IAM grant. |
| One user's email rejected by SES | Logged; the rest of that report's recipients still get it; that user is not marked as sent, so they are retried next run. If every recipient of a report fails, that company is a per-company failure (207). |
| A news query fails | Logged and skipped; the run continues with the other queries. |
| `investor_report.md` missing from S3 | Warning logged; investor items use `company_report.md` until the prompt is pushed. |
| Classifier did not tag region/industry | Event matches any user (never silently dropped). |
| Recipients file missing or `AccessDenied` on S3 | `get_recipients()` falls back to `SES_RECIPIENT_EMAIL`; logs a `WARNING` (missing) or `ERROR` (access denied) line. Any other S3 error is re-raised. |
| No recipients configured at all (no file, no env var) | `RuntimeError`, surfaces as a per-company failure when the email step runs. |
| Lambda about to hit its 15-minute timeout mid-run | `lambda_handler()` checks `context.get_remaining_time_in_millis()` and stops before starting the next company once under 5 minutes remain, returning what's done so far as a partial (207) result. |
| Scheduled invocation fails twice | EventBridge Scheduler sends the failed event to the `company-intelligence-dlq` SQS queue (added 2026-10-02); nothing alerts on queue depth yet, so this queue should be checked periodically or a CloudWatch alarm added on `ApproximateNumberOfMessagesVisible`. |

## 7. Testing

| File | Covers |
|---|---|
| `tests/test_pipeline.py` | Full mocked end-to-end run of both Lambdas together: classification, grounded research (incl. a formatter-JSON-retry path), source verification, PDF+email generation, S3 de-dupe across two runs. |
| `tests/test_taxonomy.py` | Profile normalisation/validation, role-to-kind rules, region/industry matching and scoring, one-event-per-user assignment, shared events, the distinct-event cap, query building (no India bias, capped), users from S3 JSON / recipients fallback, per-user sent log. |
| `tests/test_multiuser.py` | The handler with news/classifier/research stubbed: each user gets one matching event, no repeats on re-run, `force`, shared reports, retry of failed deliveries, failure isolation, the cap. |
| `tests/test_api_unit.py` | Profile API: public `/options`, 401 without a verified email, save/read round trip, email taken from the token not the body, users cannot read each other, validation, SES verification on first save only, DynamoDB item shape readable by the orchestrator. |
| `tests/test_recipients.py` | `get_recipients()`: S3 file parsing, fallback to env var on missing file / `AccessDenied`, error propagation for other S3 errors, "nothing configured" failure, per-container caching. |
| `tests/test_orchestrator_unit.py` | URL/string helpers, `reconcile_event()` (including the amount-vs-valuation correction), `merge_sources()`, `validate_research_sources()`, `attach_source_ids()`, `build_warnings()`, `event_marker_key()`, `build_key_facts()`. |
| `tests/test_research_unit.py` | `extract_grounded_output()`'s citation parsing/dedup, `clean_json_response()`, `validate_research_packet()`, `attach_source_urls()` (ID dropping, CONFIRMED downgrade). |
| `tests/test_pdf_report_unit.py` | `clean_model_markdown()`, `_drop_title_line()`, and a full `render_report_pdf()` smoke test asserting valid PDF bytes. |

All unit test files use only the stdlib (`unittest` + `unittest.mock`
where needed); only `test_pipeline.py` requires `boto3`, `markdown`, and
`reportlab`, matching the project's existing dependency footprint.

## 8. Build & deploy

- `scripts/build.sh [x86_64|arm64]`: installs orchestrator deps
  (`markdown`, `reportlab` -> Pillow, platform-specific) into a temp dir,
  bundles fonts, zips both Lambdas with Python's stdlib `zipfile` (not the
  external `zip` binary, so it works on machines without one installed).
- `scripts/deploy.sh [orchestrator|research]`: builds, then
  `lambda update-function-code`; converts the zip path with `cygpath` when
  run under an MSYS/Git Bash shell on Windows, since the native `aws.exe`
  doesn't understand `/c/...`-style paths.
- `scripts/deploy.sh [orchestrator|research|api]`: also builds `dist/api.zip`
  (profile API + the shared `taxonomy.py`) and, for `all`, deploys the API
  Lambda only if it already exists.
- `scripts/setup_v2.sh [--no-orchestrator] [--site-only]`: idempotent
  creation of the v2 AWS resources (section 10) and publishing of `site/`.
- `scripts/pull_prompts.sh` / `push_prompts.sh`: sync `prompts/*.md` with
  S3; live without a redeploy.
- `scripts/pull_recipients.sh` / `push_recipients.sh`: sync
  `config/recipients.txt` with S3; also live without a redeploy.

## 9. Open follow-ups (not yet done)

See TODO.md for the operator checklist and BACKLOG.md for engineering gaps.

- SES is still sandboxed -- verify additional recipient addresses
  individually, or request production access, before relying on
  `recipients.txt` growing beyond the one verified address.
- No alarm on the DLQ's `ApproximateNumberOfMessagesVisible`, so a
  twice-failed scheduled run is captured but not actively alerted on.
- No alarm/metric distinguishes a *partial* failure (HTTP 207, some
  companies failed) from full success; only the `Errors` metric (crashes)
  is alarmed on.
- No CI runs `tests/test_*.py` automatically on push.

## 10. v2 data model, API and resources (defined, not yet deployed)

### 10.1 DynamoDB `dawnbrief-users` (PAY_PER_REQUEST, PITR on)

Partition key `email` (lower-case string). Attributes: `roles` (list of
`founder`/`investor`), `regions` (list), `industries` (list), `active`
(bool, false = paused), `created`, `updated` (ISO-8601 UTC). Values are
the canonical names from `orchestrator/taxonomy.py`, which is the single
source of truth shared by the orchestrator and the API (copied into both
zips by `build.sh`).

Taxonomy: roles `founder`, `investor`; regions North America, LATAM, EMEA,
APAC (includes India), Global/Other; 18 industries (AI/ML, Deep Tech, SaaS &
Enterprise, Fintech, Healthtech & Biotech, Climate & Energy, Construction
Tech & Proptech, Consumer & Retail, Mobility & Logistics, Cybersecurity,
Semiconductors & Hardware, Robotics & Industrial, Agritech & Food, Edtech,
Media & Gaming, Space & Defence, Web3 & Crypto, Other). A missing or empty
regions/industries list is read as "all".

### 10.2 Profile API (HTTP API `dawnbrief-api`, JWT authorizer `cognito`)

| Route | Auth | Behaviour |
|---|---|---|
| `GET /options` | none | `{"roles": [...], "regions": [...], "industries": [...]}` |
| `GET /profile` | Cognito ID token (`Authorization: Bearer ...`) | the caller's profile, or `{"exists": false, ...empty}` |
| `PUT /profile` | same | body `{"roles": [...], "regions": [...], "industries": [...], "active": true}`; each list must be non-empty and contain known values (400 with `problems` otherwise). Response is the saved profile plus `exists` and `created`. On first save the Lambda calls SES `VerifyEmailIdentity` (if `VERIFY_IN_SES`). |

The caller's email comes only from the verified `email` / `email_verified`
claims; a body `email` is ignored. Throttle: 10 req/s, burst 20, on the
`$default` stage. CORS allows the CloudFront site origin and
`http://localhost:8080`.

### 10.3 Sign-in (Cognito user pool `dawnbrief-users`)

Sign-in with email or preferred username + password (aliases), email
auto-verified, password min 10 chars with upper/lower/digit. Hosted-login
domain prefix `dawnbrief` (falls back to `dawnbrief-app`, then an
account-suffixed name if taken). Public app client `dawnbrief-web`, no
secret, authorization-code + PKCE, scopes `openid email profile`, ID/access
tokens 60 min, refresh 30 days (the site does not use refresh tokens yet:
after an hour the user is sent through sign-in again). Google sign-in is not
configured: it needs a Google OAuth client (TODO.md).

### 10.4 Website

Private S3 bucket `dawnbrief-site-<account>` behind CloudFront (origin
access control, redirect to HTTPS, managed security-headers policy, price
class 100). `site/index.html` is one self-contained page (landing, sign-in,
profile setup/edit). `site/config.js` is generated by `setup_v2.sh`; with it
empty the page runs in a local preview mode (choices kept in the browser).
The default `*.cloudfront.net` hostname is random; a custom name needs a
domain.

### 10.5 Selection algorithm (taxonomy.assign_events)

For each user, among events not yet in their sent history: skip those whose
kind their roles do not want, whose explicit region is not among their
regions (Global/Other and untagged match anyone), or whose explicit
industry is not among theirs; score 1 + 2 for an explicit region match + 2
for an explicit industry match, +1 if the event is already chosen for
another user; take the best. New distinct events are refused once
`MAX_ITEMS_PER_RUN` is reached, so late users reuse chosen ones or get
nothing that day.


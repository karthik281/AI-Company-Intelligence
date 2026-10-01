# Low-Level Design

Companion to HLD.md. This is the reference for exact contracts, schemas,
AWS resource names, and IAM permissions as they exist in the account
today (last verified 2026-10-01/02).

## 1. Deployed AWS resources

| Resource | Name / ARN | Notes |
|---|---|---|
| Orchestrator Lambda | `company-intelligence-agent` (ap-south-1) | python3.14, x86_64, 512 MB, 900s timeout, role `CompanyIntelligenceLambdaRole` |
| Research Lambda | `company-intelligence-web-research` (us-west-2) | python3.14, x86_64, 256 MB, 600s timeout, role `CompanyIntelligenceResearchRole` |
| Trigger | EventBridge Scheduler `company-intelligence-daily` | `cron(0 6 * * ? *)` in `Asia/Calcutta`, 60 min flexible window, invokes the orchestrator with `{}` |
| Scheduler retry/DLQ | `MaximumRetryAttempts: 2`, DLQ = SQS `company-intelligence-dlq` (ap-south-1) | Added 2026-10-02; was 0 retries / no DLQ before |
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
| `CompanyIntelligenceSESPolicy` | `ses:SendEmail` / `ses:SendRawEmail`, scoped to the one verified identity ARN |
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
  config/
    recipients.txt             # one email per line, '#' comments; see get_recipients()
  reports/
    YYYY-MM-DD/
      <Company_Name>.md
      <Company_Name>.pdf       # only if SAVE_PDF_TO_S3=true
  state/
    reported/
      <company>__<round>.json  # de-dupe marker; only written if DEDUPE_EVENTS=true
```

## 4. Request/response contracts

### 4.1 Orchestrator -> Research Lambda (synchronous `Invoke`)

Request (event passed to the research Lambda):
```json
{"company": "Acme Inc", "news_articles": [{"title": "...", "url": "...", "published": "...", "description": "...", "publisher": "...", "publisher_url": "..."}]}
```

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

Input: `{}` normally; `{"force": true}` to bypass de-duplication for a
manual/test invocation.

Output:
```json
{
  "statusCode": 200,             // 200 all succeeded, 207 partial, 500 all failed
  "body": "{\"reports_created\": N, \"reports_failed\": N, \"skipped_already_reported\": [...], \"reports\": [...]}"
}
```
Each entry in `reports` is either a success record (see
`process_event()`'s return value: company, event, s3_key, pdf_s3_key,
email_sent, research/source counters, warnings) or `{"company": ..., "error": "ExceptionType: message"}`.

### 4.3 Funding event shape (classifier output, then reconciled)

```json
{
  "relevant": true, "company": "Acme Inc", "event_type": "funding",
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
- Research Lambda env vars no longer include `OPENAI_API_KEY` /
  `OPENAI_BASE_URL` -- these were leftover/unused and removed 2026-10-02.

## 6. Error handling matrix

| Failure | Behavior |
|---|---|
| Research Lambda returns non-200, or invocation itself errors | Caught in `process_event()` via the raise in `get_web_research()`; surfaces as a per-company `error` entry in the orchestrator's response. Rest of the run continues. |
| Classifier/report Bedrock call hits `max_tokens` | Classifier: raises (fatal for that run -- no events are safe to act on from truncated JSON). Report: recorded as a warning in `build_warnings()`, report still sent. |
| PDF rendering throws any exception | Caught in `build_pdf()`; email sends with the Markdown report attached instead, noting this in the message body. |
| Research JSON formatter output unparseable | Retried once with a stricter prompt (`format_packet()`); if that also fails, the exception propagates as a per-company failure. |
| Recipients file missing or `AccessDenied` on S3 | `get_recipients()` falls back to `SES_RECIPIENT_EMAIL`; logs a `WARNING` (missing) or `ERROR` (access denied) line. Any other S3 error is re-raised. |
| No recipients configured at all (no file, no env var) | `RuntimeError`, surfaces as a per-company failure when the email step runs. |
| Lambda about to hit its 15-minute timeout mid-run | `lambda_handler()` checks `context.get_remaining_time_in_millis()` and stops before starting the next company once under 5 minutes remain, returning what's done so far as a partial (207) result. |
| Scheduled invocation fails twice | EventBridge Scheduler sends the failed event to the `company-intelligence-dlq` SQS queue (added 2026-10-02); nothing alerts on queue depth yet, so this queue should be checked periodically or a CloudWatch alarm added on `ApproximateNumberOfMessagesVisible`. |

## 7. Testing

| File | Covers |
|---|---|
| `tests/test_pipeline.py` | Full mocked end-to-end run of both Lambdas together: classification, grounded research (incl. a formatter-JSON-retry path), source verification, PDF+email generation, S3 de-dupe across two runs. |
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
- `scripts/pull_prompts.sh` / `push_prompts.sh`: sync `prompts/*.md` with
  S3; live without a redeploy.
- `scripts/pull_recipients.sh` / `push_recipients.sh`: sync
  `config/recipients.txt` with S3; also live without a redeploy.

## 9. Open follow-ups (not yet done)

- SES is still sandboxed -- verify additional recipient addresses
  individually, or request production access, before relying on
  `recipients.txt` growing beyond the one verified address.
- No alarm on the DLQ's `ApproximateNumberOfMessagesVisible`, so a
  twice-failed scheduled run is captured but not actively alerted on.
- No alarm/metric distinguishes a *partial* failure (HTTP 207, some
  companies failed) from full success; only the `Errors` metric (crashes)
  is alarmed on.
- No CI runs `tests/test_*.py` automatically on push.

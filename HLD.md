# High-Level Design

## 1. Purpose

Once a day, the pipeline scans AI-startup funding news, picks out genuine
funding events, researches each company with grounded web search, writes a
detailed report, and emails it as a PDF. It runs unattended on a schedule;
a human only looks at the email (or the stored reports) and occasionally
tunes the prompts or recipient list.

## 2. Architecture

```
EventBridge Scheduler (cron, daily 06:00 IST)
        │  invokes, with retries + DLQ on failure
        ▼
Orchestrator Lambda (ap-south-1 / Mumbai)
        │
        ├─ Google News RSS ──────────────────► classify via Bedrock
        │                                        (funding events?)
        │
        ├─ per funding event ─────────────────► Research Lambda
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
        └─ email the PDF via SES
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
| Email deliverability | SES is still in **sandbox mode** - only the one verified sender/recipient address can receive mail. Adding more recipients to `recipients.txt` requires verifying each address (or requesting SES production access) first. |

## 7. Known limitations / deliberate non-goals

- No VPC, no provisioned/reserved concurrency, no X-Ray tracing - the
  workload is a single daily batch job, not a latency-sensitive service.
- The CloudWatch alarms watch the Lambda `Errors` metric (unhandled
  exceptions/throttles). A *partial* failure - one company's report
  failing while others succeed, reported as HTTP 207 in the response body
  - does not currently trigger an alarm; it's only visible by reading the
  response or the logs.
- No CI pipeline runs the tests automatically on push; `tests/test_*.py`
  are run manually (or by an agent) before deploying.

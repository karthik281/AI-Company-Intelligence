# Backlog

Known gaps and follow-ups, in priority order. This file accumulates over
time rather than being rewritten -- when an item is done, mark its status
and leave it (or move it to a "Done" section below) rather than deleting
it, so there's a record of what was considered and when.

Priority meaning:
- **P0** -- security exposure or something that will actively break /
  silently fail; do soon.
- **P1** -- real gap in reliability/observability/process; do when
  there's a natural opportunity.
- **P2** -- improvement, not urgent; nice to have.

Status: Open | In progress | Done

## P0

(none open)

## P1

| Item | Why it matters | Added | Status |
|---|---|---|---|
| SES is still in sandbox mode | Only one address is verified. Any email added to `config/recipients.txt` beyond it will silently fail to deliver until it's individually verified or SES production access is requested. Explicitly deferred by the user on 2026-10-02. | 2026-10-02 | Open |
| Sending from gmail.com fails DMARC alignment | Gmail shows an "impersonating your account" warning and other providers may spam-fold the mail. Needs a domain you own, verified in SES with DKIM; also required for SES production access. User has no domain yet (2026-10-12). | 2026-10-12 | Open |
| Multi-user personalisation (v2) | See DESIGN_V2.md: profiles, per-user region/industry, one report per user/day, investor coverage, India-edition bias removed. | 2026-10-12 | In progress |
| Single-Lambda run cannot scale past a few hundred users | Distinct companies are researched sequentially (1-2.5 min each) and emails are sent one at a time inside a 15-minute invocation. Needs fan-out (SQS/Step Functions: one message per company, batches of recipients). | 2026-10-12 | Open |
| No unsubscribe link or `List-Unsubscribe` header | Bulk email without one is non-compliant in many jurisdictions and is required by Gmail/Yahoo for bulk senders. Link to the profile page's pause switch. | 2026-10-12 | Open |
| v2 prompts and classifier only tested against mocks | Region/industry/kind tagging and the investor report prompt have never run against the live model. Review the first real emails and tune `prompts/*.md`. | 2026-10-12 | Open |
| Cognito default email limit (about 50/day) | Signup verification emails fail past that. Needs Cognito email routed through SES (requires a verified domain). | 2026-10-12 | Open |
| No alarm when a run sends zero emails | A run can succeed technically while delivering nothing (this is what hid the SES `AccessDenied` for days). Add a metric filter on `Error processing` / `ERROR: email to` and on zero deliveries. | 2026-10-12 | Open |
| No alarm on the DLQ's depth | A twice-failed scheduled run now lands in `company-intelligence-dlq` (added 2026-10-02) instead of vanishing, but nothing currently alerts on `ApproximateNumberOfMessagesVisible` -- it has to be checked manually. | 2026-10-02 | Open |
| No alerting on partial failures | The CloudWatch alarms added 2026-10-02 watch the Lambda `Errors` metric (hard crashes). A partial failure -- some companies succeed, one fails, HTTP 207 -- raises no alarm; it's only visible by reading the response body or logs. | 2026-10-02 | Open |

## P2

| Item | Why it matters | Added | Status |
|---|---|---|---|
| Cache research and finished report per event | Same event re-matched to a later user is researched again at about $0.06. Cache by event key; also cache investor/fund research about 30 days. See COSTS.md. | 2026-10-12 | Open |
| Try Nova Lite v1 for the research formatting pass | `FORMAT_MODEL_ID` already supports it; about 11x cheaper output tokens. Needs a JSON-validity test. | 2026-10-12 | Open |
| Site does not use Cognito refresh tokens | Users are sent through sign-in again after an hour. Fine for a profile page; revisit if it annoys. | 2026-10-12 | Open |
| Unrelated costly resources in the account | VPC endpoints, Redis and a WAF for a FlightSearch project cost about $57 a month. Not Dawnbrief; owner to decide. | 2026-10-12 | Open |
| No CI pipeline | `tests/test_*.py` are run manually before deploying, not automatically on push/PR. | 2026-10-02 | Open |
| No infrastructure-as-code | The AWS changes made 2026-10-02 (IAM policies, SQS DLQ, SNS topics, CloudWatch alarms, S3 lifecycle, scheduler retry policy) were all applied directly via the AWS CLI. Nothing codifies this state, so it isn't reproducible or diffable (e.g. via Terraform/CDK) if the account needs to be rebuilt or audited later. | 2026-10-02 | Open |

## Done

| Item | Why it mattered | Added | Done |
|---|---|---|---|
| Revoke the leftover Bedrock API key | The key removed from the research Lambda's env vars on 2026-10-02 was still live as an IAM service-specific credential (`bedrock.amazonaws.com`, user `BedrockAPIKey-wlbm`, set to expire in the year 2126 -- effectively permanent). Deleted via `aws iam delete-service-specific-credential`; the now-empty IAM user itself was then detached from `AmazonBedrockLimitedAccess` and deleted (`aws iam delete-user`) -- nothing of it remains. | 2026-10-02 | 2026-10-02 |

See also HLD.md/LLD.md for what else was fixed in the same
production-readiness pass: IAM least-privilege, log retention, S3
versioning/lifecycle, DLQ + scheduler retries, error alarms, runtime
version alignment, and the `get_recipients()` resilience fix.

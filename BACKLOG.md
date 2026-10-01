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
| No alarm on the DLQ's depth | A twice-failed scheduled run now lands in `company-intelligence-dlq` (added 2026-10-02) instead of vanishing, but nothing currently alerts on `ApproximateNumberOfMessagesVisible` -- it has to be checked manually. | 2026-10-02 | Open |
| No alerting on partial failures | The CloudWatch alarms added 2026-10-02 watch the Lambda `Errors` metric (hard crashes). A partial failure -- some companies succeed, one fails, HTTP 207 -- raises no alarm; it's only visible by reading the response body or logs. | 2026-10-02 | Open |

## P2

| Item | Why it matters | Added | Status |
|---|---|---|---|
| No CI pipeline | `tests/test_*.py` are run manually before deploying, not automatically on push/PR. | 2026-10-02 | Open |
| No infrastructure-as-code | The AWS changes made 2026-10-02 (IAM policies, SQS DLQ, SNS topics, CloudWatch alarms, S3 lifecycle, scheduler retry policy) were all applied directly via the AWS CLI. Nothing codifies this state, so it isn't reproducible or diffable (e.g. via Terraform/CDK) if the account needs to be rebuilt or audited later. | 2026-10-02 | Open |

## Done

| Item | Why it mattered | Added | Done |
|---|---|---|---|
| Revoke the leftover Bedrock API key | The key removed from the research Lambda's env vars on 2026-10-02 was still live as an IAM service-specific credential (`bedrock.amazonaws.com`, user `BedrockAPIKey-wlbm`, set to expire in the year 2126 -- effectively permanent). Deleted via `aws iam delete-service-specific-credential`; the IAM user now has zero active credentials. | 2026-10-02 | 2026-10-02 |

See also HLD.md/LLD.md for what else was fixed in the same
production-readiness pass: IAM least-privilege, log retention, S3
versioning/lifecycle, DLQ + scheduler retries, error alarms, runtime
version alignment, and the `get_recipients()` resilience fix.

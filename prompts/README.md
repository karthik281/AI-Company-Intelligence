# Prompts

The orchestrator reads these from `s3://<bucket>/prompts/` at run time:

- `funding_classifier.md`: decides which news articles are funding events
- `company_report.md`: instructions for the final company report

Get the current versions from S3 with `scripts/pull_prompts.sh`.
After editing, publish with `scripts/push_prompts.sh` (no Lambda redeploy
needed).

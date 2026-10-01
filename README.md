# Company Intelligence Pipeline

Finds AI startup funding news, researches each company with grounded web
search, writes a detailed report, saves it to S3 and emails it as a PDF.

## How it works

```
Google News RSS ─> classify funding events (Bedrock)
                 └> per company:
                      company news ─> research Lambda (Oregon, Nova grounding)
                      ─> report (Bedrock) ─> S3 (.md) ─> PDF ─> SES email
```

| Part | Where | Code |
|---|---|---|
| Orchestrator | Lambda, ap-south-1 (Mumbai) | `orchestrator/` |
| Research | Lambda, us-west-2 (Oregon) | `research/` |
| Prompts | `s3://company-intelligence-2026/prompts/` | `prompts/` |
| Reports | `s3://company-intelligence-2026/reports/YYYY-MM-DD/` | generated |

## Setup (once)

```bash
cp config.example.env config.env      # fill in function names
scripts/pull_prompts.sh               # copy prompts from S3
```

Requires the AWS CLI, configured with access to both Lambdas and the bucket.

## Everyday workflow

| Change | Command |
|---|---|
| Orchestrator code | `scripts/deploy.sh orchestrator` |
| Research code | `scripts/deploy.sh research` |
| Prompts | `scripts/push_prompts.sh` |
| Run tests (mocked AWS) | `python3 tests/test_pipeline.py` |

Then commit: `git add -A && git commit -m "..." && git push`.

Tests need `pip install boto3 markdown reportlab`.

## Orchestrator environment variables

| Variable | Default | Purpose |
|---|---|---|
| `BEDROCK_MODEL_ID` | required | Model for classification and reports |
| `S3_BUCKET` | required | Prompts and reports bucket |
| `SES_SENDER_EMAIL` | required | Verified SES sender |
| `SES_RECIPIENT_EMAIL` | required | Recipient(s), comma-separated |
| `RESEARCH_LAMBDA_NAME` | `company-intelligence-web-research` | Research function |
| `RESEARCH_LAMBDA_REGION` | `us-west-2` | Research region |
| `NEWS_QUERY` | `AI startup funding` | Google News search |
| `GENERAL_NEWS_LIMIT` | `5` | Headlines checked per run |
| `COMPANY_NEWS_LIMIT` | `10` | Articles per company |
| `REPORT_MAX_TOKENS` | `7000` | Report length budget |
| `INCLUDE_RESEARCH_APPENDIX` | `true` | Evidence tables in the PDF |
| `SAVE_PDF_TO_S3` | `false` | Also store the PDF |
| `DEDUPE_EVENTS` | `false` | Skip events already reported |
| `SES_CONFIGURATION_SET` | empty | SES delivery/bounce tracking |
| `DEBUG_LOGS` | `false` | Log full research payloads |

Lambda settings: orchestrator 512 MB / 15 min; research 256 MB / 10 min.
Both run Python 3.14.

## Research Lambda environment variables

| Variable | Default |
|---|---|
| `BEDROCK_MODEL_ID` | `us.amazon.nova-2-lite-v1:0` |
| `FORMAT_MODEL_ID` | same as above |
| `BEDROCK_REGION` | `us-west-2` |
| `RESEARCH_MAX_TOKENS` | `8000` |
| `FORMAT_MAX_TOKENS` | `10000` |

## Known issues

- Sending from a gmail.com address through SES fails DMARC; mail may land
  in spam. Fix: send from a domain you own.
- Google News edition follows the caller's location (India for Mumbai).

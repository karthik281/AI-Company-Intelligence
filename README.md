# Company Intelligence Pipeline

**Dawnbrief**: finds startup funding and investor news for the regions and
industries each user follows, researches one company or investor per user per
day with grounded web search, and emails a detailed PDF brief. Users register
on a website and choose their role (founder, investor, or both), regions and
industries. Design: `DESIGN_V2.md`. Operator checklist: `TODO.md`. Costs:
`COSTS.md`.

## How it works

```
users (DynamoDB) ─> news queries per region/industry (Google News RSS)
  ─> classify events + tag kind/region/industry (Bedrock)
  ─> pick one unseen event per user
  └> per distinct event:
       company news ─> research Lambda (Oregon, Nova grounding)
       ─> report (Bedrock) ─> S3 (.md) ─> PDF ─> SES email to each user
```

| Part | Where | Code |
|---|---|---|
| Orchestrator | Lambda, ap-south-1 (Mumbai) | `orchestrator/` |
| Research | Lambda, us-west-2 (Oregon) | `research/` |
| Profile API | Lambda + HTTP API + Cognito, ap-south-1 | `api/`, `orchestrator/taxonomy.py` |
| Website | S3 + CloudFront | `site/` |
| Prompts | `s3://company-intelligence-2026/prompts/` | `prompts/` |
| Reports | `s3://company-intelligence-2026/reports/YYYY-MM-DD/` | generated |

## Setup (once)

```bash
cp config.example.env config.env      # fill in function names
scripts/pull_prompts.sh               # copy prompts from S3
scripts/pull_recipients.sh            # copy the recipient list from S3
```

Requires the AWS CLI, configured with access to both Lambdas and the bucket.

## Everyday workflow

| Change | Command |
|---|---|
| Orchestrator code | `scripts/deploy.sh orchestrator` |
| Profile API code | `scripts/deploy.sh api` |
| Website only | `scripts/setup_v2.sh --site-only` |
| First-time v2 AWS setup | `scripts/setup_v2.sh` (see TODO.md for the order) |
| Research code | `scripts/deploy.sh research` |
| Prompts | `scripts/push_prompts.sh` |
| Recipient list | `scripts/push_recipients.sh` |
| Run the end-to-end test (mocked AWS) | `python3 tests/test_pipeline.py` |
| Run all tests | `for f in tests/test_*.py; do python3 "$f" || break; done` |

Then commit: `git add -A && git commit -m "..." && git push`.

Tests need `pip install boto3 markdown reportlab`. `tests/test_pipeline.py` is the
full mocked end-to-end run; `tests/test_*_unit.py` and `tests/test_recipients.py`
are focused unit tests (stdlib `unittest`, no extra dependencies) for the
business-logic functions in each Lambda and in `pdf_report.py`.

## Orchestrator environment variables

| Variable | Default | Purpose |
|---|---|---|
| `BEDROCK_MODEL_ID` | required | Model for classification and reports |
| `S3_BUCKET` | required | Prompts and reports bucket |
| `SES_SENDER_EMAIL` | required | Verified SES sender |
| `SES_RECIPIENT_EMAIL` | required | Recipient(s), comma-separated |
| `RESEARCH_LAMBDA_NAME` | `company-intelligence-web-research` | Research function |
| `RESEARCH_LAMBDA_REGION` | `us-west-2` | Research region |
| `USERS_TABLE` | empty | DynamoDB users table; empty = `config/users.json`, then `recipients.txt` |
| `MAX_ITEMS_PER_RUN` | `10` | Distinct companies researched per run (cost cap) |
| `MAX_NEWS_QUERIES` | `24` | News queries per run |
| `CLASSIFIER_CHUNK` | `25` | Articles per classifier call |
| `CLASSIFIER_MAX_TOKENS` | `4000` | Classifier output budget per call |
| `NEWS_QUERY` | empty | Optional extra founder query |
| `GENERAL_NEWS_LIMIT` | `10` | Headlines kept per news query |
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
- Fixed: the Google News edition no longer follows the Lambda's location
  (it was India for Mumbai). Queries use the US edition plus region words.
- SES is sandboxed: only verified addresses receive mail, so public signup
  cannot deliver until SES production access (needs a domain).
- One orchestrator run is sequential; see HLD section 7 for the scale
  ceiling and BACKLOG.md for the fan-out item.

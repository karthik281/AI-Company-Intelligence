# Design: multi-user personalised reports (v2)

Status 2026-10-12: **code complete and tested; AWS resources not yet
created.** Product name: **Dawnbrief** ("one funding brief at dawn"; matches
the 06:00 IST run). Decisions taken with the user: AWS-native stack, SES stays
in sandbox for now, shared-pool selection, investor-centric items.

What exists now vs what is pending:

| Phase | Code | Tests | In AWS |
|---|---|---|---|
| 1 Pipeline (taxonomy, matching, per-user send, investor items) | done (`orchestrator/`, `research/`, `prompts/`) | `test_taxonomy.py`, `test_multiuser.py`, `test_pipeline.py` | **not deployed** |
| 2 Data + API (DynamoDB, profile Lambda, HTTP API) | done (`api/`, `scripts/setup_v2.sh`) | `test_api_unit.py` | **not created** |
| 3 Auth + UI (Cognito, website, CloudFront) | done (`site/`, `scripts/setup_v2.sh`) | rendered and checked visually in headless Edge, desktop and 500px wide; not yet exercised against a live Cognito | **not created** |
| 4 Cutover | built in: with `USERS_TABLE` set, an empty table falls back to `recipients.txt` | `test_multiuser.py`, `test_taxonomy.py` | pending |

The operator checklist to finish is TODO.md; costs are in COSTS.md.

### Changes from the first proposal

- **Per-user history lives in S3** (`state/sent/<hash>.json`), not a second
  DynamoDB table: one fewer resource, same behaviour, and it can move to
  DynamoDB later without changing the matching code.
- **Regional news uses a fixed US Google News edition plus region words in the
  query.** Per-country editions would need non-English parsing; the original
  India bias came from the Lambda's location, which is now irrelevant.
- **Industries drive dedicated news queries** (one per industry users follow),
  interleaved with region and investor queries under a cap of 24 queries.
- **Untagged events match everyone** instead of being dropped, so an older
  classifier prompt degrades gracefully.
- **The profile API asks SES to verify a new user's email** on first save, as a
  stopgap for sandbox mode; turn it off with `VERIFY_IN_SES=false` once SES
  has production access.
- **Cognito sign-in with email or username**, hosted login (PKCE), no custom
  login form; Google sign-in is a later config step.
- **A free `*.cloudfront.net` URL**: the name "Dawnbrief" appears on the page,
  in the Cognito hosted-login domain (`dawnbrief.auth...`) and in bucket and
  resource names, but the CloudFront hostname itself is random. A readable
  URL needs a domain.

## Goals

1. Users sign up (Google or username/password), set a profile once, and can
   edit it later.
2. Profile = role(s) (investor, founder, or both) + regions + industries.
3. Each user gets **one** report per day matched to their profile (was: up to
   5 for everyone).
4. No India-only bias: region comes from each user's profile.
5. Investor coverage: where investors/funds are putting money, not only
   startup rounds.

## Standard taxonomies (single source of truth: `orchestrator/taxonomy.py`)

**Regions:** North America, LATAM, EMEA, APAC (includes India), Global/Other.

**Industries:** AI/ML, Deep Tech, SaaS & Enterprise, Fintech, Healthtech &
Biotech, Climate & Energy, Construction Tech & Proptech, Consumer & Retail,
Mobility & Logistics, Cybersecurity, Semiconductors & Hardware, Robotics &
Industrial, Agritech & Food, Edtech, Media & Gaming, Space & Defence, Web3 &
Crypto, Other.

## Architecture

```
Cognito (Google + user/pass) -> static site (S3+CloudFront) -> API Gateway
   -> Profile Lambda -> DynamoDB  users(email PK: roles, regions[], industries[],
                                         created, updated)
                                  sent(email PK, item_key SK) per-user history

EventBridge daily -> Orchestrator
  1. Gather pool: per-region Google News editions (hl/gl/ceid) x industry
     queries, plus investor-activity queries. No hard-coded India edition.
  2. Classify (extended prompt): each event tagged region, industry,
     kind = startup_round | investor_activity.
  3. Match: for each user, rank unsent pool items by region/industry/role
     overlap; pick the top 1. Users with no match get nothing that day.
  4. Research + report once per distinct selected item (cached in S3), using
     the startup or the investor report prompt. Cap on distinct items/day.
  5. Email each user their one item (individual SES send), record in `sent`.
```

Research cost scales with distinct companies/day, not with users.

## Investor items

New classifier kind `investor_activity` (fund close, deal led, portfolio move,
new fund thesis). Separate prompt `prompts/investor_report.md`. Investors get
those items; founders get startup rounds; "both" gets both pools.

## Phases (as originally planned)

1. **Pipeline (local, tested, no AWS changes):** taxonomy, classifier tags,
   per-region news, matching, one-per-user send, investor prompt. Users read
   from a local/S3 JSON stand-in with the same shape as the DynamoDB item.
2. **Data + API (needs AWS resources, asks first):** DynamoDB tables, profile
   Lambda, API Gateway, IAM.
3. **Auth + UI:** Cognito (Google IdP needs a Google OAuth client), static
   site with signup, profile setup, edit-profile.
4. **Cutover:** switch orchestrator from `recipients.txt` to DynamoDB users;
   keep `recipients.txt` as fallback.

## Constraints and risks

- **SES sandbox:** every recipient must be verified, so public signup cannot
  actually deliver until production access (needs a sending domain). Signup
  can still be built and tested with verified addresses.
- **Sequential run:** a single 15-minute Lambda researches and sends one at a
  time, which limits v2 to roughly 5-8 distinct companies and a few hundred
  users per run. Fan-out is the next architecture step (BACKLOG.md).
- **Untested against the live model:** the classifier tagging and investor
  prompts have only run against mocks. Expect to tune them after the first
  real run.
- **Email compliance:** emails need an unsubscribe mechanism before any
  external signup (BACKLOG.md, TODO.md).
- **Google sign-in** requires creating a Google OAuth client (user action).
- **Email privacy:** one individual SES send per user; the `To` header shows the sender, so recipients never see each other.
- Existing rules still apply: grounded-citation URLs only, amount and
  valuation as separate fields, shared research contract changed on both
  Lambdas together.

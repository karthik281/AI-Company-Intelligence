# Design: multi-user personalised reports (v2)

Status: proposed 2026-10-12. Decisions taken with the user: AWS-native stack,
SES stays in sandbox for now, shared-pool selection, investor-centric items.

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

## Phases

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
- **Google sign-in** requires creating a Google OAuth client (user action).
- **Email privacy:** one individual send per user, `To` = that user.
- Existing rules still apply: grounded-citation URLs only, amount and
  valuation as separate fields, shared research contract changed on both
  Lambdas together.

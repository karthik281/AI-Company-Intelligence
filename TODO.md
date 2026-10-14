# Operator TODO: Dawnbrief go-live

Everything below needs **you** (a human with AWS and account access). The code,
tests, prompts, site and infrastructure script are written and committed;
nothing in AWS has been changed for v2. Tick items off as you go.

Status on 2026-10-12: production is still running the *old* code. The BCC /
per-recipient email fix (commit `b14cd0c`, `f57c377`) **is** deployed; the v2
pipeline, profile API and website are **not**.

## A. Go live (do these in order)

The order matters: deploying the new orchestrator before its IAM grant makes
every daily run fail (it must read `state/sent/*`, which the role cannot do
yet).

- [ ] **A1. Allow the commands to run.** An automatic safety check blocked
      the infrastructure script when I tried it. Either run the commands in
      A2 to A4 yourself (prefix with `! ` in the Claude Code prompt, or run
      them in a terminal), or add permission rules for them in `/permissions`
      and tell me to run them. Writing the rules myself is blocked.
- [ ] **A2. Create the AWS resources and publish the site.**
      `bash scripts/setup_v2.sh`
      This creates the DynamoDB table, profile Lambda and role, Cognito pool,
      HTTP API, private S3 bucket and CloudFront distribution, grants the
      orchestrator role the new permissions (including `state/*` read and SES
      `identity/*`), and sets `USERS_TABLE` on the orchestrator. It prints the
      site URL at the end. Safe to re-run. If it fails part-way, fix the
      message and run it again.
      (Use `--no-orchestrator` to create everything except the orchestrator
      changes, then do those later with another run.)
- [ ] **A3. Push the new prompts.** `bash scripts/push_prompts.sh`
      Updates `funding_classifier.md` (adds kind, region, industry tags) and
      adds `investor_report.md`. Until this is done, events are untagged and
      match everyone, and investor items fall back to the company prompt.
- [ ] **A4. Deploy the new code.** `bash scripts/deploy.sh`
      Deploys the orchestrator, research Lambda and profile API. Only after A2.
- [ ] **A5. Register yourselves.** Open the site URL, sign up with
      `rao.kar@gmail.com` and again for `aayush.jain@leopathway.com` (each
      person signs up themselves), pick role, regions and industries, save.
      **Until at least one real user exists in the table, the pipeline falls
      back to `recipients.txt`; the moment the first user saves a profile,
      only registered users get emails**, so both of you need to register.
- [ ] **A6. Confirm the SES verification emails.** After the first save, AWS
      sends each new user a "verify this address" email. Click the link
      (check spam). Without it SES refuses to deliver while in sandbox.
- [ ] **A7. Run it once and read the result.**
      `aws lambda invoke --function-name company-intelligence-agent --region ap-south-1 --invocation-type Event --payload '{}' --cli-binary-format raw-in-base64-out out.json`
      (asynchronous, because a run can take several minutes and the CLI
      would time out), then check the CloudWatch logs and your inbox. Do **not** use `{"force": true}`
      unless you want everyone re-sent their history.
      **The new classifier and investor prompts have only been tested against
      mocks, never the live model.** Read the first few emails critically:
      are the tags (region, industry, kind) sensible, and is the investor
      report good? Tune `prompts/*.md` and re-run A3.
- [ ] **A8. Look at the site on a phone and a laptop.** Sign-in, the profile
      form, "Select all", pausing, signing out, and signing back in.

## B. Before you invite real users

- [ ] **B1. Buy a domain** (about $12 a year). It unlocks everything below.
- [ ] **B2. Verify the domain in SES with DKIM**, then set `SES_SENDER_EMAIL`
      to an address on it (for example `brief@yourdomain`) and update the SES
      IAM policy for that identity. This fixes the Gmail "impersonating your
      account" warning (the screenshot you sent).
- [ ] **B3. Request SES production access.** Needs the domain and a use-case
      description (mention: opt-in signup, one email per day, unsubscribe,
      bounce handling). Until granted only verified addresses receive mail.
      Once granted, set `VERIFY_IN_SES=false` on the profile Lambda.
- [ ] **B4. Add an unsubscribe link and `List-Unsubscribe` header** to the
      emails (see BACKLOG.md). Legally required for bulk email in most places
      and required by Gmail/Yahoo for bulk senders.
- [ ] **B5. Add a privacy policy and terms page** to the site and link them in
      the footer (you collect email addresses and preferences). Have someone
      qualified review them; I have not written legal text.
- [ ] **B6. Point Cognito's email at SES.** Cognito's built-in sender is
      limited to about 50 emails a day, so signup verification emails fail
      after that. Needs the verified domain.
- [ ] **B7. Custom domain for the site** (`dawnbrief.<yourdomain>`): ACM
      certificate in us-east-1, attach to CloudFront, Route 53 record, then add
      the new origin to Cognito's callback URLs and the API's CORS list.
- [ ] **B8. Optional: Google sign-in.** Create an OAuth client in Google Cloud
      Console, add it as an identity provider in the Cognito user pool, and add
      `Google` to the app client's supported providers. Then add a "Continue
      with Google" option (the hosted login shows it automatically once the
      provider is enabled).
- [ ] **B9. Check the product name.** I chose "Dawnbrief". I did not search for
      trademarks or domain availability.

## C. Cost and risk (see COSTS.md)

- [ ] **C1. Check your AWS credits** (Billing > Credits): balance and expiry.
      The last 30 days of usage ($62 at list price) were fully covered by
      credits.
- [x] **C2. FlightSearch stack deleted on 2026-10-14** (at your request, no backups taken): Lambdas, REST/HTTP APIs, CloudFront, WAF, Redis, VPC endpoints, VPC, three DynamoDB tables and roles. Saves about $57 a month.
- [ ] **C3. Create an AWS Budget (about $25 a month) with an email alert** and
      turn on Cost Anomaly Detection (free).
- [ ] **C4. Review COSTS.md section 3** and tell me which optimisations to do
      first (suggested: the report cache, then the cheaper formatter model).

## D. Operations and monitoring

- [ ] **D1. Alarm when a run sends zero emails** or has partial failures (only
      hard crashes alarm today).
- [ ] **D2. Alarm on the DLQ depth** (`company-intelligence-dlq`).
- [ ] **D3. Watch the first week**: user count, distinct companies per day,
      grounding requests, bounces/complaints in SES. Raise
      `MAX_ITEMS_PER_RUN` only when many users are getting no match.
- [ ] **D4. Revisit scale** when you pass a few hundred users: the run is
      sequential and limited to 15 minutes (HLD section 7, BACKLOG.md).

## E. Housekeeping

- [ ] **E1. Review and push the commits**: `git log --oneline` (nothing from
      this work has been pushed; `config.env` and `dist/` stay uncommitted).
- [ ] **E2. Review the committed docs**: `DESIGN_V2.md`, `HLD.md`, `LLD.md`,
      `README.md`, `COSTS.md`, this file.
- [ ] **E3. Consider infrastructure-as-code** for the AWS state (BACKLOG.md
      already tracks it); `setup_v2.sh` is a stopgap, not a replacement.

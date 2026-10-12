# Dawnbrief: monthly OpEx and cost optimisation

Prepared 2026-10-12 from this account's real Cost Explorer data (last 30
days) and CloudWatch usage, plus published pricing. Figures are USD,
approximate, and meant for decisions, not accounting. Where something is an
assumption it is marked **(assumed)**.

## 1. What you pay today

### 1.1 The whole AWS account (last 30 days)

| | Amount |
|---|---|
| Usage at list price | **about $62** |
| AWS credits applied | **about -$62** (Sep: -$45.02, Oct 1-12: -$22.16) |
| Net bill | **$0** |

So nothing has been invoiced, but **credits are paying for everything and
their balance and expiry are not visible to me**. Check Billing > Credits
before treating the net bill as free.

Where the $62 goes:

| Item | 30-day cost | Belongs to |
|---|---|---|
| VPC interface endpoints (Lambda, Bedrock-runtime) | **$36.95** | **Another project** (see 1.3) |
| ElastiCache `cache.t4g.micro` (`flight-cache`) | **$14.20** | **Another project** |
| WAFv2 web ACL + rules (`FlightSearch-Firewall`) | **$5.85** | **Another project** |
| Nova 2 Lite web grounding (100 requests) | $3.00 | Dawnbrief |
| Nova 2 Lite output tokens | $1.69 | Dawnbrief |
| Nova 2 Lite input tokens | $0.28 | Dawnbrief |
| Nova Lite (orchestrator) input + output | $0.18 | Dawnbrief |
| SES, S3, Lambda, CloudWatch, SQS, SNS | under $0.05 | Dawnbrief |

**Dawnbrief itself costs about $5 a month at today's volume. About 92% of
the account's usage is a different project.**

### 1.2 Dawnbrief per-company unit cost (measured)

| Component | Per company |
|---|---|
| Web grounding: $0.03 per request, an estimated 1.4 requests per company (100 requests billed in the month, divided by an approximate company count taken from one week of logs; retries and test runs are included) | **$0.043** |
| Research tokens, Nova 2 Lite ($0.33 in / $2.75 out per 1M): about 7.8K in, 3.9K out | $0.013 |
| Classifier + report tokens, Nova Lite (about $0.07 in / $0.28 out per 1M in Mumbai) | $0.002 |
| Lambda, S3, logs | under $0.001 (inside free tier) |
| **Total** | **about $0.06** |

Grounding is about 70% of every company's cost. The token prices above
reproduce the bill exactly (for example $2.75 per 1M output tokens gives
the $1.69 billed), so they can be trusted. Grounding was confirmed at $0.03
per request from the bill ($3.00 for 100 requests); AWS's own pricing page
did not show the figure, and a third-party calculator quotes the same $30
per 1,000.

### 1.3 Not part of Dawnbrief, but costing money

The account also runs Lambdas `PartnerSearch`, `DeltaSearch`,
`AutocompleteService`, `SearchFlights`, a Redis cache `flight-cache`, a WAF
`FlightSearch-Firewall`, and two interface VPC endpoints created 2026-09-08
and 2026-09-10. I did not touch any of it. If that project is still wanted,
leave it; if it is a leftover, **deleting it saves about $57 a month**, more
than 10 times the cost of running Dawnbrief.

## 2. Projected monthly OpEx for v2

Variable cost = distinct companies researched per day x 30 x $0.06, plus
email. Distinct companies per day depends on how varied the users' profiles
are, and is capped by `MAX_ITEMS_PER_RUN` (default 10), which doubles as a
hard budget guard: at the default the research bill cannot exceed about
**$18 a month** (10 x 30 x $0.06).

| Scenario | Distinct cos/day **(assumed)** | Research + grounding | Email (SES) | Other infra | **Total / month** |
|---|---|---|---|---|---|
| Today: 2 users | 1 | $2 | under $0.10 | under $1 | **about $3** |
| 100 users | 8 | $14 | $0.50 | $1.50 | **about $16** |
| 1,000 users, cap 10 | 10 | $18 | $4-5 | $4 | **about $27** |
| 1,000 users, cap raised to 25 | 25 | $45 | $4-5 | $4 | **about $55** |
| 10,000 users | 50 | $90 | $35-54 | $10 | **about $140-155** |

Notes on the assumptions and what is excluded:

- **Email**: one email per user per day, 30 a month, PDF about 150 KB.
  SES is $0.10 per 1,000 emails plus $0.12 per GB of attachments; new
  accounts since July 2026 may instead be on a plan at $0.16 per 1,000, so
  confirm the plan in the console. 10,000 users is about 300,000 emails.
- **Other infra** is CloudWatch logs, DynamoDB (on-demand, a few cents),
  S3, API Gateway (HTTP API $1 per million requests), Lambda beyond the
  free tier, and the two existing CloudWatch alarms. CloudFront (1 TB and 10
  million requests free every month) and Cognito (first 10,000 monthly
  active users free) cost nothing at these sizes.
- **Not in the totals**: a domain (about $12 a year, roughly $1 a month,
  plus $0.50 a month for a Route 53 zone), tax, and support. Cognito above
  10,000 active users is a per-user charge.
- **Beyond a few hundred users the architecture, not the bill, is the
  limit.** One Lambda run is capped at 15 minutes and does research and
  sends sequentially, so thousands of users need a fan-out redesign (HLD
  section 7, BACKLOG.md). That redesign adds small SQS/Step Functions cost.
- **Cognito's default email sender is limited to about 50 emails a day**, so
  signup verification emails will start failing once more than roughly 50
  people register in a day. Routing Cognito's email through SES (needs a
  domain) lifts that.

## 3. Cost optimisation research

Ordered by saving per unit of effort. "Saving" is monthly and relative to the
scenarios above.

| # | Action | Estimated saving | Effort | Notes |
|---|---|---|---|---|
| 1 | **Decide the FlightSearch resources** (endpoints, Redis, WAF) | **up to $57** | Low | Largest item by far, and unrelated to Dawnbrief. Delete if abandoned; if kept, consider one AZ per endpoint, or call Bedrock/Lambda without interface endpoints from a NAT-less design. |
| 2 | **Cache research and the finished report per event** and reuse it when the same event is matched to a user later (new signups, other profiles) | Large as users grow; avoids a repeat $0.06 per reuse | Medium | Today the same event could be re-researched on another day. Key the cache by the event key already used for history. |
| 3 | **Cache investor/fund research for about 30 days** and research only the new deal | Roughly half of investor-item cost | Medium | Fund facts (size, partners, thesis) change slowly; grounding is the expensive part. |
| 4 | **Cut grounding retries**: the notes retry and any formatter retry; lower `MAX_TOKENS` waste | Up to 25% of grounding (about $0.01 per company) | Low | The 1.4 requests per company is an estimate, but anything above 1.0 is extra requests, some of which are likely retries. Check the logs for `Retrying once` to measure it. |
| 5 | **Use Nova Lite v1 for the formatting pass** (`FORMAT_MODEL_ID`, already supported) instead of Nova 2 Lite | About $0.0075 per company; about $11 at 1,500 companies a month | Low | Output tokens drop from $2.75 to $0.24 per 1M. Needs a quality test on JSON validity; keep Nova 2 Lite for the grounded step. |
| 6 | **Keep `MAX_ITEMS_PER_RUN` as the budget dial** | Hard ceiling of about $18 at 10 | None | Raise it deliberately as users grow; it is the per-day cost guard. |
| 7 | **AWS Budgets alert** (first two are free) at about $25, plus Cost Anomaly Detection (free) | Prevents surprises | Low | Set this before opening signup. |
| 8 | **Link to the PDF instead of attaching it** (presigned S3 URL) | Small cost ($0.12 per GB), better deliverability | Medium | Attachments from a new domain are a spam signal; also lets you track opens. |
| 9 | **Reduce orchestrator memory** from 512 MB to 256 MB | Under $0.10 | Low | It uses about 145 MB; PDF rendering may slow slightly. Not worth doing until after the free tier is exceeded. |
| 10 | **Stay within the free tiers** (Cognito 10K MAU, CloudFront 1 TB, Lambda 1M requests, DynamoDB on-demand) | Already $0 | None | Avoid Cognito advanced security and CloudFront extras. |
| 11 | **Skip Graviton / Savings Plans / provisioned throughput** | n/a | n/a | Compute is a rounding error, and these need steady, large spend to pay off. |
| 12 | **Product lever: a weekly digest for free users, daily for paid** | Cuts email and research for the free tier | Product decision | Only if you decide to charge. |

### Options considered and not recommended now

- **A cheaper search API instead of Nova grounding** (for example a
  commodity search API at a few dollars per 1,000 queries, with Nova Lite
  reading the results): potentially 3 to 10 times cheaper per company, but it
  breaks the project rule that source URLs come from grounding citations, and
  needs a new vendor and a key to manage. Revisit if grounding exceeds about
  $100 a month. AWS also lists Web Search on Bedrock AgentCore at $7 per
  1,000 queries, which is a different unit from Nova's per-request grounding
  and should be priced against real call counts before any switch.
- **Bedrock batch inference** (50% off) does not apply: the research step
  needs live web grounding through the Converse API.
- **Prompt caching**: the report prompt is large, but input tokens are
  already under $0.30 per month, so there is nothing to save yet.

## 4. Recommended actions, in order

1. Check credit balance and expiry (Billing > Credits).
2. Decide what to do with the FlightSearch resources ($57 a month).
3. Create a Budget alert (about $25) and enable Cost Anomaly Detection.
4. Keep `MAX_ITEMS_PER_RUN` at 10 until you have real signup numbers.
5. Implement the event/report cache (item 2), then the cheaper formatter
   (item 5) with a JSON-validity test.
6. Buy a domain: it is needed for SES production access, for Cognito email
   beyond 50 a day, and to fix the Gmail impersonation warning, so it
   unblocks more than it costs.

## Sources

- Nova Lite and Nova 2 Lite token prices: [getmaxim Nova 2 Lite](https://www.getmaxim.ai/bifrost/llm-cost-calculator/provider/bedrock/model/us.amazon.nova-2-lite-v1-0.md) and [Nova Lite](https://www.getmaxim.ai/bifrost/llm-cost-calculator/provider/bedrock/model/amazon.nova-lite-v1-0.md) (cross-checked against this account's bill)
- Web grounding is an extra cost and US-only: [Amazon Nova Web Grounding docs](https://docs.aws.amazon.com/nova/latest/nova2-userguide/web-grounding.html); price confirmed from this account's Cost Explorer data
- Bedrock pricing: [aws.amazon.com/bedrock/pricing](https://aws.amazon.com/bedrock/pricing/); AgentCore web search at $7 per 1,000 queries: [AWS blog](https://aws.amazon.com/blogs/aws/announcing-web-search-on-amazon-bedrock-agentcore-ground-your-ai-agents-in-current-accurate-web-knowledge/)
- SES: [Amazon SES pricing](https://aws.amazon.com/ses/pricing/)
- Cognito free tier: [Amazon Cognito pricing](https://aws.amazon.com/cognito/pricing/)
- CloudFront free tier: [CloudFront pricing](https://aws.amazon.com/cloudfront/pricing/pay-as-you-go)
- API Gateway HTTP API: [API Gateway pricing](https://aws.amazon.com/api-gateway/pricing/)
- DynamoDB on-demand: [AWS announcement](https://aws.amazon.com/about-aws/whats-new/2024/11/amazon-dynamo-db-reduces-prices-on-demand-throughput-and-global-tables)
- Lambda ap-south-1 and free tier: [AWS Lambda pricing](https://amazonaws.cn/en/lambda/pricing)

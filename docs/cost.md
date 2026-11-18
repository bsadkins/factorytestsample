# Cost

This document explains what Support Case Insights costs to run: the cost drivers of each AWS service it uses and how they scale, an illustrative monthly estimate for a modest organisation, the settings that control cost, and how to set up an AWS Budgets alert. Every price below is an approximate us-east-1 list price at the time of writing (November 2026), rounded, and shown only to make the arithmetic visible. Prices vary by Region, change over time, and free tier allowances depend on your account, so check the [AWS Pricing Calculator](https://calculator.aws/) and the service pricing pages before relying on any figure. Nothing has been deployed, so the usage figures are estimates from the code and the synthesised templates, not measurements. The resources themselves are listed in [resources](resources.md) and the design in [architecture](architecture.md).

## Summary

- The fixed monthly cost is dominated by AWS WAF (only with MCP) and CloudWatch alarms, metrics and dashboards.
- The variable cost is dominated by Amazon Bedrock tokens: three model calls per case, plus any MCP Bedrock tool calls.
- The first run after deployment backfills up to `lookbackMonths` (default 12) of resolved cases, so its Bedrock cost is a one-off multiple of a normal month.
- Everything else (Lambda, Step Functions, SQS, DynamoDB, S3, Glue, Athena, API Gateway, X-Ray) is small at the volumes this application handles.
- The illustrative scenario below comes to roughly $25 per month with MCP and roughly $15 without it. It is an estimate, not a guarantee.

## Cost drivers by service

### AWS Lambda

Ten Core functions, plus `Lambda-McpServer-<uniqueId>` with MCP, all Python 3.12 on x86_64 with `operations.functions.memorySizeMb` (default 512 MB). Cost is about $0.20 per million requests plus about $0.0000167 per GB-second.

| Function | How often it runs |
| --- | --- |
| Account Lookup, Account Reader | Once per daily run |
| Case Retrieval | Once per member account per run |
| Case Annotation, Workflow Starter | Once per new case (batched) |
| Case Analysis Stages | Three times per case (Summary, RCA, Lifecycle), each waiting on one or two Bedrock calls |
| Update Case Metadata | Once per case |
| Case Cleanup | Once a day |
| Run Completion | Once an hour |
| MCP server | Once per MCP request |

Most invocations take seconds, so a few hundred cases a month use a few thousand GB-seconds, which is cents and often inside the free tier. Bedrock latency is the main contributor to stage duration. Reserved concurrency (`bedrockMaxConcurrency` for the stages, 20 for the MCP function) costs nothing by itself.

### AWS Step Functions

`CaseAnalysis-<uniqueId>` is a **Standard** workflow (not Express), billed at about $0.025 per 1,000 state transitions after a free tier of 4,000 transitions a month. A successful execution enters four task states (`Summary`, `RCA`, `Lifecycle`, `UpdateCaseMetadata`); each retry adds a transition, and a failed execution adds `HandleFailure` and `AnalysisFailed`. A few hundred cases a month is a few thousand transitions, so cents at most.

### Amazon SQS

Eight queues (three stage queues, three dead-letter queues, two terminal queues), billed at about $0.40 per million requests after the first million a month. Message volume is a few requests per case. The larger part is the idle polling of the three Lambda event source mappings, which long-poll their queues continuously even when they are empty; expect a total of the order of one to two million requests a month, which is under $1. SSE-SQS adds no charge.

### Amazon DynamoDB

The idempotency table is on demand (`PAY_PER_REQUEST`), with a few small writes and reads per account and case. Point-in-time recovery is enabled and billed per GB of table size, which stays tiny because records expire after 7 days. Expect cents a month.

### Amazon S3

All buckets are S3 Standard (about $0.023 per GB-month), versioned, with noncurrent versions kept for `operations.storage.noncurrentVersionRetentionDays` (default 30). Requests cost about $0.005 per 1,000 PUT or LIST and $0.0004 per 1,000 GET.

| Bucket | Volume driver | Lifecycle rule (verified in the templates) |
| --- | --- | --- |
| Account-list | One small list per day | `ExpireAccountLists`: expire after `rawRetentionDays` |
| Raw | Per case: metadata, the full conversation, three checkpoints, an ownership marker and run markers | `ExpireRawCaseData`: expire after `rawRetentionDays` |
| Processed | One record of a few KB per case | `ProcessedToInfrequentAccess`: Standard-IA after `operations.storage.processedInfrequentAccessDays` (default 90); never expires |
| Access-log | S3 server access logs of the other buckets (many small objects) | `ExpireAccessLogs`: expire after `operations.storage.accessLogRetentionDays` (default 365) |
| Athena results | Query results under `query-results/` | `ExpireQueryResults`: expire after `operations.athena.resultsRetentionDays` (default 30) |

Every bucket also aborts incomplete multipart uploads after 7 days.

**The Standard-IA transition does little for processed records.** The processed bucket's lifecycle configuration keeps the S3 default `TransitionDefaultMinimumObjectSize` of `all_storage_classes_128K`, so objects smaller than 128 KB are not transitioned at all. Processed records are a few KB, so in practice they stay in S3 Standard. That is the cheaper outcome: S3 Standard-IA bills every object as at least 128 KB, plus a per-object transition request, so moving a 5 KB record to Standard-IA would raise its storage charge by more than ten times. The rule only affects unusually large records. At a few KB per case, even 10,000 processed records occupy well under 100 MB, which is cents a month.

Raw objects are larger (the conversation is stored in full), but they expire after `rawRetentionDays`, so raw storage stays bounded by the cases of that period. The request charges from Run Completion (hourly reads of run markers for the last 72 hours) and Case Cleanup (a daily listing of the raw bucket) grow with the number of accounts and cases but remain small for a modest organisation.

### Amazon CloudWatch

| Item | Count | Approximate price |
| --- | --- | --- |
| Alarms | 23 in Core without MCP, 27 with MCP (shipped prod 23, shipped dev 27). All are standard resolution. `WorkflowFailuresRecorded` uses four metrics and `FailureDeliveryErrors` three, so with MCP the alarms bill as 32 alarm metrics (28 without MCP) | About $0.10 per alarm metric per month; 10 alarm metrics are in the always-free tier |
| Dashboard | 1 per environment (`CaseInsights-<uniqueId>`), with more than 50 metrics (53 in the shipped prod template, more with MCP) | About $3 per dashboard per month. The always-free tier covers 3 dashboards of up to 50 metrics each, so this dashboard is billed |
| Custom metrics | Namespace `SupportCaseInsights`, one `service` dimension, so the number of metric streams is fixed by the code (roughly 120 names across eleven services, plus `McpAuthFailures`). It does not grow with accounts or cases | About $0.30 per metric per month, prorated by the hour and charged only for hours in which the metric is sent. Run Completion sends four metrics every hour; most other metrics are sent only during the daily run |
| Logs | Ten Lambda log groups (eleven with MCP), the Step Functions log group and, with MCP, the API Gateway access log group, each kept for `logRetentionDays` (default 90). Embedded Metric Format records count as log ingestion | About $0.50 per GB ingested and $0.03 per GB-month stored; 5 GB of ingestion and storage a month are in the always-free tier |

The metrics are counts and durations only, which keeps both cardinality and log volume low. A modest organisation ingests well under 1 GB of logs a month. See [monitoring](monitoring.md) for every metric, alarm and widget, and [logging](logging.md) for the log groups.

### AWS X-Ray

Active tracing is enabled on every function, the state machine and the MCP API stage. X-Ray charges about $5 per million traces recorded after 100,000 free traces a month, and about $0.50 per million traces retrieved or scanned. A few thousand cases a month stays inside the free tier.

### Amazon Bedrock

Each case makes three calls through the configured global inference profile: Summary, RCA and Lifecycle. Each stage may add one corrective follow-up when the first reply fails validation, which repeats the conversation so far. At the time of writing, global cross-Region inference for Claude Haiku 4.5 costs about $1 per million input tokens and $5 per million output tokens; Claude Sonnet 4.5 costs about $3 and $15, so roughly three times as much for the same tokens. Regional and geographic inference profiles carry a premium over global profiles, but the application accepts only the global ones.

Approximate tokens per case, assuming about 4 characters per token and a 20,000-character rendered conversation:

| Call | Input | Output |
| --- | --- | --- |
| Summary | Template (about 1,100 characters) plus the full conversation: about 5,300 tokens | 600 to 650 characters: about 160 tokens |
| RCA | Template, 16 category definitions and the summary (about 3,100 characters): about 800 tokens | One JSON object with a category and a reason of at most 500 characters: about 100 to 150 tokens |
| Lifecycle | Template, 20 category definitions and the summary (about 3,400 characters): about 850 tokens | About 100 to 150 tokens |
| **Total** | **about 7,000 tokens** | **about 450 tokens** |

With Haiku 4.5 that is about $0.007 for input and $0.002 for output, so roughly **$0.01 per case**; with Sonnet 4.5 roughly $0.03 per case. Input dominates, and it scales with conversation length: the summary prompt can be up to 400,000 characters (roughly 95,000 to 100,000 tokens, about $0.10 with Haiku), while longer conversations are reported as too large and not sent. `bedrockMaxTokens` (default 1000, range 256 to 4096) caps the output of each call; because output is billed only for tokens actually generated and the summary and reasons have fixed length limits, raising it does not normally raise cost. Retries after an uncertain outcome can rarely repeat a call. The *Bedrock tokens* dashboard widget (`BedrockInputTokens`, `BedrockOutputTokens`) shows actual usage.

The two MCP Bedrock tools add their own charges. `analyze_case_summaries` sends up to `maxCases` summaries (default 100, at most 200, and at most 200,000 characters), so a default call is of the order of 25,000 input tokens, about $0.03 with Haiku. `analyze_with_bedrock` sends a question and up to 60,000 characters of data. Both use `bedrockMaxTokens` for output.

Anthropic models on Bedrock may also need an AWS Marketplace subscription; usage is billed through it at the Bedrock prices.

### AWS Glue

The crawler `case-insights-partition-crawler-<uniqueId>` runs once a day (`schedules.crawler`). Crawlers are billed at about $0.44 per DPU-hour, per second, with a **10-minute minimum per crawl**. A daily crawl of this table usually finishes well within that minimum, so expect roughly $0.07 to $0.15 per run (one or two DPUs for 10 minutes), about $2 to $4.50 a month. The Glue Data Catalog stores one partition per case; the first million catalogue objects and requests a month are free.

### Amazon Athena

Athena charges about $5 per TB scanned, with a 10 MB minimum per query. Processed records are a few KB each, so a query over even 10,000 cases scans well under 100 MB and costs a fraction of a cent. The workgroup `CaseInsights-<uniqueId>` cancels any query that scans more than `operations.athena.bytesScannedCutoffPerQuery` (default 10 GiB, about $0.05), so a single runaway query cannot cost more than that. MCP Athena tools run in the same workgroup. Results are stored in the results bucket and expire after `operations.athena.resultsRetentionDays`.

### Amazon API Gateway (MCP only)

The REST API costs about $3.50 per million requests. Stage throttling (`operations.mcp.throttleRateLimit` 10 per second, burst 20) and the WAF rate rule limit how fast requests can arrive. Access logs go to `/aws/apigateway/CaseInsights-Mcp-<uniqueId>/access` and are billed as CloudWatch Logs ingestion and storage. The shared `CaseInsightsApiLoggingStack-<sharedId>` (an IAM role and the account setting) costs nothing itself.

### AWS WAF (MCP only)

The web ACL `CaseInsights-Mcp-<uniqueId>` has three rules: the per-IP rate rule `RateLimitPerIp`, the size rule `BlockBodyOver32KiB` and the AWS managed rule group `AWSManagedRulesCommonRuleSet`.

| Charge | Approximate price | This deployment |
| --- | --- | --- |
| Web ACL | $5 per month | $5 |
| Rules and managed rule groups | $1 per rule per month | 3 rules: $3 |
| Requests | $0.60 per million | Light MCP use: cents |
| Body inspection beyond 16 KB | $0.30 per million requests per extra 16 KB, only for requests whose body is larger than 16 KB | The web ACL inspects up to 32 KiB; most MCP requests are smaller |

That is about **$8 a month fixed**, whether or not the endpoint is used. The AWS managed Core rule set has no subscription fee beyond the rule charge, and sampled requests and WAF logging are off.

### Other services

| Service | Cost |
| --- | --- |
| EventBridge Scheduler | Three schedules, about 800 invocations a month: inside the free tier of 14 million |
| Amazon SNS | Alarm notifications by email: the first 1,000 email notifications a month are free |
| AWS CloudFormation and StackSets | No charge for the stacks or for the service-managed StackSet and its stack instances (only third-party resource types are charged) |
| IAM, AWS Organizations, AWS Support API | No charge. The Support API needs a Business, Enterprise On-Ramp or Enterprise Support plan in the member accounts, which is billed separately and is not a cost of this application |
| AWS KMS | Not used; no customer managed KMS keys exist |

### Optional pipeline

Only with `-c pipeline=true`; see [pipeline](pipeline.md).

| Component | Approximate price | Notes |
| --- | --- | --- |
| CodePipeline | V2 pipeline: about $0.002 per action execution minute, with 100 free action minutes a month per account | The manual approval `PromoteToProd` is not billed, however long it waits. There is no per-pipeline monthly fee for V2 |
| CodeBuild | About $0.01 per build minute for the `Synth` step (medium compute, `npm run verify` plus synth) and about $0.005 per minute for small compute (self-mutation and asset publishing) | A release is of the order of 20 to 40 build minutes, so a few tens of cents |
| Artefact bucket and its access-log bucket | S3 Standard; artefacts expire after 90 days, logs after 365 days | Cents |

A handful of releases a month costs well under $5.

## Illustrative monthly scenario

This scenario shows the shape of the cost for a modest organisation. **It is an estimate, not a guarantee**; your figures depend on case volume, conversation length, MCP use, Region, free tier eligibility and current prices.

Assumptions:

- One environment in us-east-1 with Analytics and MCP enabled, Claude Haiku 4.5 (the default), default `operations` settings and an alarm email.
- 50 member accounts and 300 newly resolved cases a month after the initial backfill, with an average rendered conversation of 20,000 characters.
- Light MCP use: about 1,000 requests a month, including 30 `analyze_case_summaries` and 30 `analyze_with_bedrock` calls, and about 100 Athena queries in total.
- The CloudWatch always-free tier (10 alarm metrics, 10 custom metrics, 5 GB of logs) is available to this deployment, because nothing else in the account uses it. Other free tiers are ignored except where noted.

| Item | Approximate monthly cost |
| --- | --- |
| AWS WAF web ACL and three rules | $8.00 |
| CloudWatch alarms (32 alarm metrics, 10 free) | $2.20 |
| CloudWatch custom metrics (prorated by the hour, 10 free) | $2.70 |
| CloudWatch Logs (inside the 5 GB free tier) | $0 |
| CloudWatch dashboard | $3.00 |
| Bedrock, pipeline stages (300 cases x about $0.01) | $3.00 |
| Bedrock, MCP tools (60 calls) | $1.50 |
| Glue crawler (30 daily crawls) | $3.00 |
| SQS (mostly event source polling) | $0.75 |
| S3 storage and requests | $0.75 |
| Lambda, Step Functions, DynamoDB, X-Ray, Athena, API Gateway, Scheduler, SNS | $0.50 |
| **Total** | **about $25** |

What the scenario shows:

- **WAF is the largest single item** and is fixed: about a third of the total, even with almost no MCP traffic. Without MCP (no WAF, MCP alarms, MCP metrics, API Gateway or MCP Bedrock calls) the same scenario is about **$15**.
- **CloudWatch** is the next largest fixed block, about $8. Without the free tier (for example when other workloads in the account already use it) it is about $1.50 higher. A second environment in the same account adds its own alarms, metrics and dashboard.
- **Bedrock** is the main variable cost and scales linearly with cases and conversation length: 1,000 cases a month adds about $10, and Sonnet 4.5 roughly triples every Bedrock line.
- **First backfill.** The first run collects every resolved case created in the last `lookbackMonths` months. At 300 cases a month with the default 12 months, that is about 3,600 cases, roughly $35 of Bedrock with Haiku 4.5 or about $110 with Sonnet 4.5, once.
- The optional pipeline is not included; add a few dollars at most if you use it.

## Cost controls

| Control | Effect |
| --- | --- |
| `lookbackMonths` (1 to 12, default 12) | Limits how far back the first run reaches, and therefore the one-off backfill cost. Later runs only find newly resolved cases, so it hardly affects steady-state cost |
| `bedrockModelId` | Haiku 4.5 (default) costs roughly a third of Sonnet 4.5 per token. Changing the model does not reprocess committed cases |
| `bedrockMaxConcurrency` (1 to 50, default 5) | Caps concurrent Bedrock calls. It controls the rate of spending and throttling, not the total, because every case is analysed eventually |
| `bedrockMaxTokens` (256 to 4096, default 1000) | Caps output per call, including MCP Bedrock tools |
| `enableMcp` | Turning it off removes the WAF web ACL, API Gateway, the MCP function and its four alarms, about $10 a month in the scenario. Destroy the MCP stack explicitly; see [Changing configuration safely](configuration.md) |
| `enableAnalytics` | Turning it off removes the crawler, Glue catalogue, workgroup and results bucket (it requires MCP to be off first) |
| `schedules.crawler` | Each crawl bills at least 10 minutes; a less frequent schedule lowers Glue cost but delays when new records become queryable |
| `operations.athena.bytesScannedCutoffPerQuery` (default 10 GiB) | Upper bound on the scan cost of any single query in the workgroup |
| `rawRetentionDays`, `logRetentionDays`, `operations.storage.noncurrentVersionRetentionDays`, `operations.storage.accessLogRetentionDays`, `operations.athena.resultsRetentionDays` | Bound storage in S3 and CloudWatch Logs. Shorter retention costs less but removes evidence and recovery inputs sooner; see [configuration](configuration.md) for the constraints |
| `operations.mcp.wafRateLimitPer5Minutes`, `throttleRateLimit`, `throttleBurstLimit` | Limit how fast MCP requests, and therefore MCP Bedrock and Athena calls, can arrive |
| Opt-in reprocessing | `scripts/reprocess-cases.py` re-analyses committed cases and pays the Bedrock cost again; select the narrowest date range you need |

## Budgets and cost allocation tags

### Cost allocation tags

Every resource that supports tags carries `Project=SupportCaseInsights`, `Environment=<env>` and `UniqueId=<uniqueId>` (applied with `Tags.of` in `lib/app.ts`); the shared logging stack carries `Project` and `SharedId=<sharedId>`, and the pipeline stack `Project`. To use them in Cost Explorer and AWS Budgets, activate them as cost allocation tags once, in the management account (payer account), from the repository root or any directory:

```bash
aws ce update-cost-allocation-tags-status \
  --cost-allocation-tags-status TagKey=Project,Status=Active TagKey=UniqueId,Status=Active TagKey=Environment,Status=Active
```

Tags appear in billing data only after the tagged resources have been used, and activation can take up to 24 hours to take effect. Tag-based views do not include usage that cannot be tagged, notably on-demand Bedrock inference through the inference profile, CloudWatch custom metrics and some request charges, so a tag filter undercounts the total. Use the account filter below for the full cost.

### AWS Budgets alert

This creates a monthly cost budget for the management account with email alerts at 80% of actual spend and 100% of forecast spend. Because the deployment runs in the management account, an unfiltered budget there would cover the whole organisation's consolidated bill, so the budget is filtered to the management account itself (it then also includes anything else running in that account). Prerequisites: AWS CLI v2 and credentials for the management account with `budgets:ModifyBudget` and `budgets:ViewBudget`. Run the commands in an empty temporary directory, replacing `<account-id>`, `<uniqueId>`, the amount and the email address:

```bash
cat > budget.json <<'JSON'
{
  "BudgetName": "CaseInsights-<uniqueId>-monthly",
  "BudgetLimit": { "Amount": "40", "Unit": "USD" },
  "TimeUnit": "MONTHLY",
  "BudgetType": "COST",
  "CostFilters": { "LinkedAccount": ["<account-id>"] }
}
JSON

cat > notifications.json <<'JSON'
[
  {
    "Notification": {
      "NotificationType": "ACTUAL",
      "ComparisonOperator": "GREATER_THAN",
      "Threshold": 80,
      "ThresholdType": "PERCENTAGE"
    },
    "Subscribers": [{ "SubscriptionType": "EMAIL", "Address": "cost-alerts@example.com" }]
  },
  {
    "Notification": {
      "NotificationType": "FORECASTED",
      "ComparisonOperator": "GREATER_THAN",
      "Threshold": 100,
      "ThresholdType": "PERCENTAGE"
    },
    "Subscribers": [{ "SubscriptionType": "EMAIL", "Address": "cost-alerts@example.com" }]
  }
]
JSON

aws budgets create-budget --account-id <account-id> \
  --budget file://budget.json \
  --notifications-with-subscribers file://notifications.json

aws budgets describe-budget --account-id <account-id> --budget-name CaseInsights-<uniqueId>-monthly \
  --query 'Budget.[BudgetName,BudgetLimit.Amount,TimeUnit]' --output text
```

To budget only the tagged resources of one deployment instead, replace the filter with `"CostFilters": { "TagKeyValue": ["user:UniqueId$<uniqueId>"] }` after activating the tag, keeping in mind that it excludes Bedrock inference. Set the first month's amount with the backfill in mind. Budget alerts are informational; they do not stop any resource.

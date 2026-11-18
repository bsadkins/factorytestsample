# Using Support Case Insights

This document describes day-to-day use of a deployed environment: what the daily schedule does, how to start a run by hand and follow its progress, where results are stored and how to read them with Athena, the CSV exporter, MCP clients and Amazon QuickSight, how long data is kept, and where to go when something needs recovery. It assumes the stacks are deployed as described in [installation.md](installation.md). Nothing has been deployed from this repository yet, so the commands and outputs here are derived from the code and the synthesised templates; running them against a live account is part of the deferred checks. Run every command from the repository root with credentials for the management account, in the Region of `config/<env>.json`.

## Daily schedule

The Core stack creates three EventBridge Scheduler schedules, and the Analytics stack schedules the partition crawler. Times are UTC.

| Schedule | Default | Configuration field | What it does |
| --- | --- | --- | --- |
| `CaseInsights-AccountLookup-<uniqueId>` | `cron(0 0 * * ? *)`, 00:00 | `schedules.accountLookup` | Starts the daily run |
| `CaseInsights-CaseCleanup-<uniqueId>` | `cron(0 22 * * ? *)`, 22:00 | `schedules.cleanup` | Resets abandoned, uncommitted cases so a later run collects them again |
| `CaseInsights-RunCompletion-<uniqueId>` | `rate(1 hour)` | Fixed | Checks recent runs and emits `DailyRunCompleted` when one has finished |
| Crawler `case-insights-partition-crawler-<uniqueId>` | `cron(0 9 * * ? *)`, 09:00 | `schedules.crawler` | Adds new processed-record partitions to the Glue Data Catalog so Athena can see them |

Change the schedules and the collection window in `config/<env>.json` (see [configuration.md](configuration.md)), then deploy with `npx cdk deploy --all -c env=<env>`.

## What a run does

1. **Account discovery.** `Lambda-AccountLookup-<uniqueId>` lists the Organization's accounts and keeps those with status `ACTIVE`, excluding the management account. It writes the list to `account-lists/<runId>.json` in the account-list bucket.
2. **Account fan-out.** `Lambda-AccountReader-<uniqueId>` reads the list and sends one message per account to the active-accounts queue.
3. **Case retrieval.** `Lambda-CaseRetrieval-<uniqueId>` assumes `Support-Case-Analysis-Role-<uniqueId>` in the member account and calls `DescribeCases` for cases created in the last `lookbackMonths` calendar months (default 12). It keeps resolved cases that have no processed record and writes their metadata to the raw bucket. Accounts without the role, with access denied or without a Support plan that allows the AWS Support API (`SubscriptionRequiredException`) are counted as skipped for this run and evaluated again the next day.
4. **Case annotation.** `Lambda-CaseAnnotation-<uniqueId>` reads the full conversation with `DescribeCommunications`, stores it in the raw bucket and queues the case for analysis. No AWS Support write API is ever called.
5. **Analysis.** `Lambda-WorkflowStarter-<uniqueId>` starts one execution of the `CaseAnalysis-<uniqueId>` state machine per case. The execution summarises the case, classifies it for root cause (RCA) and operational lifecycle with Amazon Bedrock, and the Update Case Metadata step commits the processed record. See [prompts.md](prompts.md) for the prompts and validation.
6. **Completion.** Run Completion emits `DailyRunCompleted` once every account and case of the run has reached a terminal outcome.
7. **Visibility.** The next crawler run makes the new records visible to Athena and the MCP tools. The CSV exporter reads the processed bucket directly and does not wait for the crawler.

A case with a committed processed record is never analysed again automatically. See [architecture.md](architecture.md) for the full design.

## Starting a run manually

Invoke account lookup without a payload. With no `scheduledTime` in the event, the function creates a new, unique run ID of the form `YYYYMMDDTHHMMSSZ-<8 hex digits>` (scheduled runs use an ID derived from the scheduled time, so a repeated delivery of the same schedule does not start a second run):

```bash
aws lambda invoke --function-name Lambda-AccountLookup-<uniqueId> \
  --region <region> account-lookup.json
cat account-lookup.json
```

The response file contains `runId`, `accountListKey`, `accountsSelected` and `created`. A manual run collects and analyses the same cases a scheduled run would: cases that are already committed are skipped, and a case that is already being analysed does not get a second execution. Use it after deployment, after fixing a discovery failure, or to pick up recently resolved cases before the next schedule.

## Following progress

| Where | What to look at |
| --- | --- |
| CloudWatch dashboard `CaseInsights-<uniqueId>` | Daily run heartbeat, workflow success rate, alarm status, accounts discovered and skipped, cases retrieved and processed, CaseAnalysis executions, Bedrock latency, tokens, throttles and failures, run completion, cleanup |
| Step Functions state machine `CaseAnalysis-<uniqueId>` | One execution per case; failed executions are recorded on the workflow failure queue |
| Alarm topic `CaseInsights-Alarms-<uniqueId>` | Alarm notifications, delivered by email when `alarmEmail` is set (required in prod). Each alarm is described in [runbook.md](runbook.md) |
| Log groups `/aws/lambda/Lambda-<Function>-<uniqueId>` | Structured JSON logs without case content (see [logging.md](logging.md)) |

To list running and failed executions:

```bash
STATE_MACHINE_ARN=$(aws cloudformation describe-stacks \
  --stack-name CaseInsightsCoreStack-<uniqueId> --region <region> \
  --query "Stacks[0].Outputs[?OutputKey=='CaseAnalysisStateMachineArn'].OutputValue" --output text)
aws stepfunctions list-executions --state-machine-arn "$STATE_MACHINE_ARN" \
  --status-filter RUNNING --region <region> --max-items 20
aws stepfunctions list-executions --state-machine-arn "$STATE_MACHINE_ARN" \
  --status-filter FAILED --region <region> --max-items 20
```

A run with many new cases takes longer, because Bedrock invocations are limited to `bedrockMaxConcurrency` at a time; Run Completion keeps checking a run for 72 hours. If no run completes within 30 hours, the `MissingDailyRunCompletion` alarm fires; follow [Daily completion](runbook.md#daily-completion). See [monitoring.md](monitoring.md) for every metric and alarm.

## Where results are stored

Each analysed case has one processed record, written once, at `account_number=<id>/case_number=<displayId>/data.json` in the processed bucket. The bucket name is the `ProcessedBucketName` output of `CaseInsightsCoreStack-<uniqueId>` (see also [Find the processed bucket](csv_export.md#find-the-processed-bucket)). A record has exactly 18 fields:

| Group | Fields |
| --- | --- |
| Original case metadata, as `DescribeCases` returned it | `caseId`, `displayId`, `subject`, `serviceCode`, `categoryCode`, `severityCode`, `submittedBy`, `timeCreated` |
| Retrieval | `Case_Retrieval_Date` |
| Summary | `Case_Summary` (600 to 650 characters) |
| Root cause | `RCA_Category`, `RCA_Reason`, `RCA_Retrieval_Date` |
| Operational lifecycle | `Lifecycle_Category`, `Lifecycle_Reason`, `Lifecycle_Retrieval_Date` |
| Provenance | `Model_Id`, `Prompt_Version` |

`subject`, `serviceCode`, `categoryCode`, `severityCode` and `submittedBy` can be `null` when `DescribeCases` returned no value. Dates are UTC timestamps such as `2026-03-01T09:30:00Z`. The categories are listed in [Taxonomies](prompts.md#taxonomies); `Unclassified` means the classifier's reply could not be accepted.

To read one record:

```bash
aws s3 cp "s3://<processed-bucket>/account_number=111111111111/case_number=<displayId>/data.json" - \
  --region <region>
```

Records contain case subjects, summaries and the `submittedBy` identity. Handle them, and anything derived from them (query results, CSV files, MCP answers), as sensitive data; see [security.md](security.md).

The raw bucket holds the working inputs for each case (`data.json`, `communications.json`, stage checkpoints and markers). It is not intended for analysis and expires after `rawRetentionDays`.

## Analysing results

| Method | Best for | Requires | Guide |
| --- | --- | --- | --- |
| Athena | SQL over all records, scheduled reporting | `enableAnalytics` | [athena.md](athena.md) |
| CSV export | Spreadsheets, Power BI, Excel, sharing a filtered extract | Read access to the processed bucket | [csv_export.md](csv_export.md) |
| MCP client (Kiro, Claude Desktop) | Questions in natural language | `enableAnalytics`, `enableMcp`, principal in `mcpAllowedPrincipals` | [mcp-server-deployment.md](mcp-server-deployment.md) |
| Amazon QuickSight | Dashboards for a wider audience | `enableAnalytics`, a QuickSight subscription | [Optional: Amazon QuickSight](athena.md#optional-amazon-quicksight) |

### Athena

Use the workgroup `CaseInsights-<uniqueId>`, the data source `AwsDataCatalog` and the database `case_insights_<uniqueId>` (hyphens in `uniqueId` become underscores), table `case_summary`. Column names are the record fields in lower case. The query library in [athena.md](athena.md#query-library) covers RCA distribution, lifecycle categories by service, temporal patterns, severity trends, top accounts and provenance checks, and [Running queries](athena.md#running-queries) shows how to run them from the AWS CLI.

New records are visible after the next crawler run. To see them sooner, start the crawler; the command is also the `ManualCrawlerStartCommand` output of the Analytics stack:

```bash
aws glue start-crawler --name case-insights-partition-crawler-<uniqueId> --region <region>
```

See [Making new records visible](athena.md#making-new-records-visible).

### CSV export

`scripts/generate_csv_insights.py` reads the processed bucket directly and needs only `s3:ListBucket` and `s3:GetObject`. For example, to export the cases created in the first quarter of 2026 for one account:

```bash
.venv/bin/python scripts/generate_csv_insights.py \
  --bucket <processed-bucket> --account-id 111111111111 \
  --start-date 2026-01-01 --end-date 2026-03-31 \
  --output case-insights-2026-q1.csv --region <region>
```

Other filters are `--date-field`, `--rca-category`, `--lifecycle-category`, `--service-code` and `--severity-code`. Formula-like cell values are escaped by default for spreadsheet safety, and `--encoding utf-8-sig` adds a byte-order mark for Excel. See [Usage](csv_export.md#usage) and [Options](csv_export.md#options) for every option.

### MCP clients

The MCP server offers eight read-only tools to a client such as Kiro or Claude Desktop, through the SigV4 bridge `scripts/mcp_aws_client.py`. Set up the client as described in [mcp-server-deployment.md](mcp-server-deployment.md); the tools and their arguments are listed in [Tools](mcp-server-deployment.md#tools). Typical questions and the tool a client is likely to choose:

| Question | Tool |
| --- | --- |
| "Which services generate the most support cases?" | `get_service_trends`, or `get_rca_analysis` grouped by service |
| "What were the top root causes this quarter?" | `get_case_summary` with `startDate` and `endDate` |
| "Do more cases start on Mondays?" | `get_temporal_patterns` with the `dayOfWeek` dimension |
| "Which lifecycle practices would have prevented most incidents?" | `get_lifecycle_analysis` |
| "Summarise last month's cases for Amazon EC2." | `analyze_case_summaries` |
| A question the fixed tools do not cover | `query_athena` with a read-only `SELECT` |

`analyze_with_bedrock` and `analyze_case_summaries` call Amazon Bedrock and incur model charges; the other tools run Athena queries, which are charged by data scanned. See [cost.md](cost.md).

### Amazon QuickSight

QuickSight can use the Athena workgroup and table as a data source. It is not deployed by this repository; see [Optional: Amazon QuickSight](athena.md#optional-amazon-quicksight).

## Data retention

| Data | Retention | Configuration field |
| --- | --- | --- |
| Raw bucket (case metadata, conversations, checkpoints) | Expires after 90 days by default | `rawRetentionDays` |
| Account-list bucket | Expires after `rawRetentionDays` | `rawRetentionDays` |
| Processed records | Kept; moved to S3 Standard-IA after 90 days by default (objects smaller than 128 KB are not moved) | `operations.storage.processedInfrequentAccessDays` |
| Noncurrent object versions (versioned buckets) | Expire after 30 days by default | `operations.storage.noncurrentVersionRetentionDays` |
| S3 server access logs | 365 days by default | `operations.storage.accessLogRetentionDays` |
| Athena query results | 30 days by default | `operations.athena.resultsRetentionDays` |
| CloudWatch log groups | 90 days by default (the shipped dev configuration uses 30) | `logRetentionDays` |

The shipped dev configuration sets `rawRetentionDays` to 30. In prod, the buckets, the idempotency table and the log groups are retained when the stacks are destroyed; see [resources.md](resources.md).

## Recovery and re-analysis

| Situation | Where to go |
| --- | --- |
| An alarm fired | The alarm's section in [runbook.md](runbook.md) |
| Messages in a stage dead-letter queue after the cause is fixed | [Redrive a stage dead-letter queue](runbook.md#redrive-a-stage-dead-letter-queue) |
| A failed case to retry before the next run | [QueueMessages-WorkflowFailures](runbook.md#queuemessages-workflowfailures) |
| Committed cases to analyse again, for example after a prompt or model change | [Reprocess committed cases](runbook.md#reprocess-committed-cases) |
| Something else does not work | [troubleshooting.md](troubleshooting.md) |

Failed cases that have not been committed are retried by later scheduled runs. Re-analysing a committed case always requires the explicit, confirmed `scripts/reprocess-cases.py` run; see [Recovery tools](runbook.md#recovery-tools).

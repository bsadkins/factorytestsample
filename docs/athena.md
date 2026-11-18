# Athena analytics

The optional Analytics stack makes processed case records queryable with Amazon Athena. It catalogues the records that the Core pipeline writes to the processed bucket and runs every query in an isolated Athena workgroup that enforces where results go, how they are encrypted and how much data a query may scan.

This guide covers enabling the stack, the table schema, how new records become visible, the permissions analysts need, the example query library and an optional manual Amazon QuickSight setup. To export records without Athena, use the [CSV exporter](csv_export.md) instead.

Nothing in this guide has been run against a real AWS account. Local checks cover the synthesised templates and the query text only; see [Live verification (deferred)](#live-verification-deferred).

## What the Analytics stack creates

`CaseInsightsAnalyticsStack-<uniqueId>` is created only when `enableAnalytics` is `true`. It is deployed after `CaseInsightsCoreStack-<uniqueId>` and reads the Core processed and access-log buckets; Core never depends on it.

| Resource | Name | Purpose |
| --- | --- | --- |
| Glue database | `case_insights_<uniqueId>` (hyphens in `uniqueId` become underscores, for example `sci-dev2` gives `case_insights_sci_dev2`) | Holds the analytics table |
| Glue external table | `case_summary` | One row per processed case, read from `s3://<processed bucket>/` with the OpenX JSON SerDe |
| Glue crawler | `case-insights-partition-crawler-<uniqueId>` | Adds partitions for newly processed cases on `schedules.crawler` |
| Crawler IAM role | Generated name | Trusted only by `glue.amazonaws.com`; reads processed records, refreshes partitions of this table and writes to the `/aws-glue/crawlers` log group. It cannot write or delete records |
| Athena workgroup | `CaseInsights-<uniqueId>` | Athena engine version 3; enforces the results location, SSE-S3 result encryption and the per-query scan cutoff |
| Results bucket | Generated name | Private, versioned, SSL-only, SSE-S3 bucket holding query results under `query-results/`; logs to the Core access-log bucket under `athena-results/` |

The stack has these outputs:

| Output | Value |
| --- | --- |
| `GlueDatabaseName` | `case_insights_<uniqueId>` |
| `GlueTableName` | `case_summary` |
| `CrawlerName` | `case-insights-partition-crawler-<uniqueId>` |
| `CrawlerSchedule` | The configured `schedules.crawler` expression (UTC) |
| `ManualCrawlerStartCommand` | `aws glue start-crawler --name case-insights-partition-crawler-<uniqueId> --region <region>` |
| `AthenaWorkgroupName` | `CaseInsights-<uniqueId>` |
| `AthenaResultsBucketName` | The generated results bucket name |

## Enable and deploy Analytics

1. In `config/<env>.json`, set `enableAnalytics` to `true`. Both shipped configurations (`config/dev.json` and `config/prod.json`) already do; the schema default is `false`. MCP (`enableMcp`) requires Analytics.
2. Optionally adjust the related settings:

   | Setting | Default | Effect |
   | --- | --- | --- |
   | `schedules.crawler` | `cron(0 9 * * ? *)` (09:00 UTC daily) | When the crawler adds new partitions |
   | `operations.athena.bytesScannedCutoffPerQuery` | `10737418240` (10 GiB); minimum `10485760` (10 MiB), maximum 1 TiB | Athena cancels any query in the workgroup that scans more |
   | `operations.athena.resultsRetentionDays` | `30` (1 to 3650) | Days query results are kept in the results bucket |
   | `operations.storage.noncurrentVersionRetentionDays` | Shared storage setting | Days noncurrent versions of result objects are kept |

3. Verify locally, then review and deploy (these commands need AWS credentials for the management account and were not run during development):

   ```bash
   npm run verify
   npx cdk diff --all -c env=prod
   npx cdk deploy --all -c env=prod --require-approval broadening
   aws cloudformation describe-stacks --stack-name CaseInsightsAnalyticsStack-<uniqueId> \
     --query 'Stacks[0].Outputs' --output table
   ```

4. After the first processed records exist, run the crawler once (see [Making new records visible](#making-new-records-visible)) so that the first queries return data.

### Disabling or removing Analytics

`cdk deploy` does not delete a stack that is no longer part of the app. Remove Analytics in this order:

1. If MCP is deployed, remove it first: MCP depends on Analytics and configuration validation rejects `enableMcp` without `enableAnalytics`.
2. Delete QuickSight data sets and data sources that use the workgroup.
3. While `enableAnalytics` is still `true`, destroy the stack: `npx cdk destroy CaseInsightsAnalyticsStack-<uniqueId> -c env=<env>`.
4. Set `enableAnalytics` to `false` and run `npx cdk deploy --all -c env=<env>`. This removes the `AllowAthenaResultsAccessLogs` statement that Analytics added to the Core access-log bucket policy.

Destroying the stack deletes the Glue database, table, crawler and workgroup (including its query history). The processed records in the Core bucket are not touched. In dev the results bucket is emptied and deleted; in prod it is retained and must be emptied and deleted manually if it is no longer needed.

## Table schema

The table location is the root of the processed bucket. Each processed case is one JSON object at `account_number=<id>/case_number=<displayId>/data.json`, written once by `Lambda-UpdateCaseMetadata-<uniqueId>` after the Summary, RCA and Lifecycle stages succeed. Records are never partially written, so every row has all 18 fields.

Glue and Athena store column names in lower case, while the stored JSON keys are mixed case. The table therefore uses the OpenX JSON SerDe with `case.insensitive` set to `false` and an explicit `mapping.<column>` property for every column. `ignore.malformed.json` is `false`, so an invalid object fails the query instead of being skipped silently. Athena identifiers are case-insensitive, so `RCA_Category` and `rca_category` both work in queries; result headers are always lower case.

### Columns

Every column has the Glue type `string`. Dates are ISO-8601 text in UTC; parse them with `from_iso8601_timestamp`.

| Column | JSON key | Nullable | Description |
| --- | --- | --- | --- |
| `caseid` | `caseId` | No | Support case ID returned by `DescribeCases` |
| `displayid` | `displayId` | No | Case number shown in the Support Center; the same value as the `case_number` partition |
| `subject` | `subject` | Yes | Case subject as entered by the requester |
| `servicecode` | `serviceCode` | Yes | Support service code, for example `amazon-elastic-compute-cloud-linux` |
| `categorycode` | `categoryCode` | Yes | Support category code within the service |
| `severitycode` | `severityCode` | Yes | Support severity: `low`, `normal`, `high`, `urgent` or `critical` |
| `submittedby` | `submittedBy` | Yes | Email address or identity of the submitter (personal data) |
| `timecreated` | `timeCreated` | No | Case creation time as returned by Support, for example `2026-06-01T10:00:00.000Z` |
| `case_retrieval_date` | `Case_Retrieval_Date` | No | When the case was retrieved, `YYYY-MM-DDTHH:MM:SSZ` |
| `case_summary` | `Case_Summary` | No | Plain-prose summary of four to six sentences and 600 to 650 characters |
| `rca_category` | `RCA_Category` | No | One of the 16 RCA categories, or `Unclassified` |
| `rca_reason` | `RCA_Reason` | No | Model explanation of the RCA category (at most 500 characters) |
| `rca_retrieval_date` | `RCA_Retrieval_Date` | No | When the RCA classification completed, `YYYY-MM-DDTHH:MM:SSZ` |
| `lifecycle_category` | `Lifecycle_Category` | No | One of the 20 lifecycle categories, or `Unclassified` |
| `lifecycle_reason` | `Lifecycle_Reason` | No | Model explanation of the lifecycle category (at most 500 characters) |
| `lifecycle_retrieval_date` | `Lifecycle_Retrieval_Date` | No | When the lifecycle classification completed, `YYYY-MM-DDTHH:MM:SSZ` |
| `model_id` | `Model_Id` | No | Bedrock inference profile used: `global.anthropic.claude-haiku-4-5-20251001-v1:0` or `global.anthropic.claude-sonnet-4-5-20250929-v1:0` |
| `prompt_version` | `Prompt_Version` | No | `pv-` followed by a 16-character digest of the prompt templates and taxonomies |

The column `case_summary` has the same name as the table. That is valid SQL; qualify it (`c.case_summary`) if a query becomes hard to read.

The table columns are tested against `common.processed_record.PROCESSED_FIELDS` in `test/cdk/stacks/analytics-stack.test.ts`, so the schema and the stored records cannot drift apart unnoticed.

### Partitions

| Partition key | Type | Value |
| --- | --- | --- |
| `account_number` | `string` | 12-digit member account ID (keep it as text: account IDs can start with zero) |
| `case_number` | `string` | Case display ID, equal to `displayid` |

Filtering on `account_number` (and `case_number`) prunes partitions, so Athena reads only the matching records. There is one partition per case, so very large histories mean many small partitions; query planning takes longer as the number grows, but each record is only a few kilobytes.

### Classification values

`rca_category` and `lifecycle_category` hold exactly these labels (case and spacing matter in comparisons), or `Unclassified`. The table is generated from `src/lambda/common/taxonomy.py`, the single source of the categories.

<!-- taxonomy:start -->
<!-- Generated by scripts/render_taxonomy_docs.py; edit src/lambda/common/taxonomy.py. -->

### Root cause analysis (RCA) categories (16)

JSON keys: `RCA_Category`, `RCA_Reason`.

| Category | Definition |
| --- | --- |
| Customer Question | The customer needed guidance, clarification or how-to information and no fault occurred. |
| Limit reached | An adjustable service quota or account limit was reached and needed an increase. |
| Development Issue | A defect in the customer's own code, configuration or infrastructure definition caused the issue. |
| Customer Release | A deployment or change released by the customer introduced the issue. |
| Lack Monitoring | Missing or inadequate customer monitoring or alerting let the issue go undetected or escalate. |
| Customer Dependency | A third-party or customer-managed dependency outside AWS caused or contributed to the issue. |
| Throttling | API or service request-rate limits throttled the customer's requests. |
| Feature Request | The customer asked for functionality that the service does not currently provide. |
| Exceeding Capability | The workload exceeded the designed capability of the chosen service or resource type, not an adjustable quota. |
| Security Issue | The case concerned a security event, exposed credentials, abuse report or security misconfiguration. |
| Transient Issues | A short-lived problem resolved without intervention and no lasting root cause was identified. |
| ICE Error | An insufficient capacity error: AWS lacked capacity for the requested resource type in that location. |
| Service Event | A broader AWS service event or outage affecting multiple customers caused the issue. |
| AWS Release | A change or deployment released by AWS caused or contributed to the issue. |
| AWS Single Component Failure | Failure of one AWS-managed component, such as a host, volume or node, affected the customer's resource. |
| Upgrade Management | Planning or performing a version upgrade, end-of-support migration or scheduled maintenance caused the issue. |

### Operational lifecycle categories (20)

JSON keys: `Lifecycle_Category`, `Lifecycle_Reason`.

| Category | Definition |
| --- | --- |
| Anomaly Detection | Automatically detecting unusual patterns in metrics or behaviour before they become incidents. |
| Alarming | Configuring actionable alarms and notifications on key metrics and thresholds. |
| AB Testing | Comparing a change against a control group with a controlled traffic split before full rollout. |
| Architecture | Designing workloads for scale, fault isolation and appropriate service selection. |
| Backup/DR | Maintaining tested backups and disaster recovery plans to restore data and service. |
| CICD | Using automated build, test and deployment pipelines with safe rollout and rollback. |
| Code Review | Peer-reviewing code and configuration changes to catch defects before release. |
| Dependency Mapping | Understanding and documenting dependencies between components, services and third parties. |
| Event Management | Tracking AWS Health events, scheduled maintenance and announcements and acting on them in time. |
| Load Testing | Testing workloads at expected and peak load to validate capacity, scaling and quotas. |
| Logging | Capturing and retaining the application and service logs needed for diagnosis. |
| Monitoring | Observing workload health and performance continuously with metrics and dashboards. |
| Performance Benchmarking | Measuring baseline performance to select and size resources correctly. |
| Resilience | Building redundancy, retries and graceful degradation so the workload withstands component failure. |
| Run Books | Maintaining documented, repeatable procedures for operating and recovering the workload. |
| Training | Building team knowledge of AWS services, features and best practices. |
| Fault Injection and GameDay | Deliberately injecting failures and running game days to validate detection and recovery. |
| Ops Reviews | Regularly reviewing operational metrics, incidents and lessons learned to drive improvements. |
| Tracing | Using distributed tracing to follow requests across components and locate failures. |
| Risk Evaluation | Assessing the risk of changes, limits and design decisions before they affect production. |

`Unclassified` is not a category. It is recorded only when a classifier reply is still invalid after one corrective prompt, with a short explanation as the reason.
<!-- taxonomy:end -->

## Making new records visible

Athena sees a processed case only after its partition is in the Glue catalogue. The crawler adds partitions on `schedules.crawler`, by default `cron(0 9 * * ? *)` (09:00 UTC). Account discovery starts at 00:00 UTC by default, so most records from a daily run are visible after that day's crawl; records committed after the crawl starts appear after the next one. The crawler only logs schema changes (`UpdateBehavior` and `DeleteBehavior` are `LOG`) and new partitions inherit the table's SerDe and mappings, so a crawl never alters the declared schema.

To make new records visible sooner, start the crawler manually. The command is also the `ManualCrawlerStartCommand` stack output:

```bash
aws glue start-crawler --name case-insights-partition-crawler-<uniqueId> --region <region>
aws glue get-crawler --name case-insights-partition-crawler-<uniqueId> --region <region> \
  --query 'Crawler.State' --output text
aws glue get-crawler --name case-insights-partition-crawler-<uniqueId> --region <region> \
  --query 'Crawler.LastCrawl.[Status,ErrorMessage,StartTime]' --output text
```

The state moves from `RUNNING` through `STOPPING` back to `READY`. Starting a crawler that is already running fails with `CrawlerRunningException`; wait for `READY`. The crawler logs to the `/aws-glue/crawlers` CloudWatch log group, in a stream named after the crawler. To confirm partitions exist:

```bash
aws glue get-partitions --database-name case_insights_<uniqueId> --table-name case_summary \
  --region <region> --max-items 5 --query 'Partitions[].Values'
```

The CSV exporter reads the processed bucket directly and is not affected by this delay.

## Workgroup and query results

`CaseInsights-<uniqueId>` sets `EnforceWorkGroupConfiguration`, so client-side settings cannot change its behaviour:

- **Results location.** Every query writes results to `s3://<results bucket>/query-results/`, with the expected bucket owner set to the management account. A results location supplied by a client is ignored.
- **Encryption.** Results are encrypted with `SSE_S3` (Amazon S3 managed keys). The bucket default is also SSE-S3. No AWS KMS key exists for Analytics, so analysts need no key permissions.
- **Scan cutoff.** Athena cancels any query that scans more than `operations.athena.bytesScannedCutoffPerQuery` bytes (default 10 GiB) with a "Bytes scanned limit was exceeded" error.
- **Retention.** Result objects expire after `operations.athena.resultsRetentionDays` (default 30 days); noncurrent versions expire after `operations.storage.noncurrentVersionRetentionDays`; incomplete multipart uploads are aborted after 7 days.
- **Engine and metrics.** Athena engine version 3. The workgroup publishes CloudWatch query metrics in the `AWS/Athena` namespace.

Query results can contain case summaries, subjects and submitter identities. Treat downloaded results with the same care as the processed bucket. The results bucket blocks public access, requires TLS 1.2 or later and logs access to the Core access-log bucket.

## Permissions for analysts

The stack does not create analyst roles. Grant the people or roles that run queries a policy like the one below, in the management account where Analytics is deployed. Replace `<region>`, `<managementAccountId>`, `<uniqueId>`, `<processedBucket>` and `<resultsBucket>` (the `AthenaResultsBucketName` output). Find the processed bucket name with the command in [CSV export](csv_export.md#find-the-processed-bucket).

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RunQueriesInCaseInsightsWorkgroup",
      "Effect": "Allow",
      "Action": [
        "athena:StartQueryExecution",
        "athena:StopQueryExecution",
        "athena:GetQueryExecution",
        "athena:BatchGetQueryExecution",
        "athena:ListQueryExecutions",
        "athena:GetQueryResults",
        "athena:GetQueryResultsStream",
        "athena:GetWorkGroup"
      ],
      "Resource": ["arn:aws:athena:<region>:<managementAccountId>:workgroup/CaseInsights-<uniqueId>"]
    },
    {
      "Sid": "BrowseAthenaConsole",
      "Effect": "Allow",
      "Action": ["athena:ListWorkGroups", "athena:ListEngineVersions", "athena:ListDataCatalogs"],
      "Resource": ["*"]
    },
    {
      "Sid": "ReadCaseInsightsCatalog",
      "Effect": "Allow",
      "Action": [
        "glue:GetDatabase",
        "glue:GetTable",
        "glue:GetPartition",
        "glue:GetPartitions",
        "glue:BatchGetPartition"
      ],
      "Resource": [
        "arn:aws:glue:<region>:<managementAccountId>:catalog",
        "arn:aws:glue:<region>:<managementAccountId>:database/case_insights_<uniqueId>",
        "arn:aws:glue:<region>:<managementAccountId>:table/case_insights_<uniqueId>/case_summary"
      ]
    },
    {
      "Sid": "ReadProcessedRecords",
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": ["arn:aws:s3:::<processedBucket>/account_number=*"]
    },
    {
      "Sid": "ListProcessedRecords",
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": ["arn:aws:s3:::<processedBucket>"]
    },
    {
      "Sid": "WriteAndReadQueryResults",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
      "Resource": ["arn:aws:s3:::<resultsBucket>/query-results/*"]
    },
    {
      "Sid": "ListQueryResults",
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads"],
      "Resource": ["arn:aws:s3:::<resultsBucket>"]
    }
  ]
}
```

The policy grants no write or delete access to processed records and no Glue changes. Omit `BrowseAthenaConsole` for principals that use only the CLI or SDK. Listing every database and table in the console's catalogue browser can need further Glue list permissions; queries against `case_summary` do not. If AWS Lake Formation manages Data Catalog permissions in this account, IAM alone is not enough: grant the analysts `SELECT` and `DESCRIBE` on `case_insights_<uniqueId>.case_summary`, and the crawler role permission to add partitions, in Lake Formation. This application does not configure Lake Formation.

## Running queries

In the Athena console, choose the workgroup `CaseInsights-<uniqueId>`, the data source `AwsDataCatalog` and the database `case_insights_<uniqueId>`, then run a query from the library. The examples refer to the table as `case_summary`, so they rely on that database being selected.

From the AWS CLI:

```bash
QUERY_ID=$(aws athena start-query-execution \
  --work-group CaseInsights-<uniqueId> \
  --query-execution-context Database=case_insights_<uniqueId> \
  --query-string "SELECT rca_category, count(*) AS cases FROM case_summary GROUP BY 1 ORDER BY 2 DESC" \
  --query 'QueryExecutionId' --output text)
aws athena get-query-execution --query-execution-id "$QUERY_ID" \
  --query 'QueryExecution.[Status.State,Status.StateChangeReason,Statistics.DataScannedInBytes]' --output text
aws athena get-query-results --query-execution-id "$QUERY_ID" --output table
```

No `--result-configuration` is needed; the workgroup supplies and enforces it.

## Query library

Every query reads `case_summary` and uses only the columns and partitions described above. Timestamps are parsed inline with `from_iso8601_timestamp`, and multi-step queries use inline common table expressions (`WITH ... AS`) rather than views, so nothing has to be created in the catalogue. Day, week and month boundaries are in UTC; to use another time zone, add `AT TIME ZONE 'Europe/London'` (or another zone name) after the parsed timestamp. Add `WHERE account_number = '<accountId>'` to any query to limit it, and the data it scans, to one account.

### RCA distribution

Share of cases in each RCA category:

```sql
SELECT rca_category,
       count(*) AS cases,
       round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS percent_of_cases
FROM case_summary
GROUP BY rca_category
ORDER BY cases DESC;
```

RCA distribution for cases created in the current calendar quarter:

```sql
WITH cases AS (
  SELECT rca_category,
         from_iso8601_timestamp(timecreated) AS created
  FROM case_summary
)
SELECT rca_category,
       count(*) AS case_count
FROM cases
WHERE created >= date_trunc('quarter', current_timestamp)
GROUP BY rca_category
ORDER BY case_count DESC;
```

### Lifecycle categories by service

Cases per service and lifecycle category, for a matrix or heat map:

```sql
SELECT coalesce(servicecode, 'unknown') AS service,
       lifecycle_category,
       count(*) AS cases
FROM case_summary
GROUP BY 1, 2
ORDER BY service, cases DESC;
```

The most common lifecycle category for each service, busiest services first:

```sql
WITH counts AS (
  SELECT coalesce(servicecode, 'unknown') AS service,
         lifecycle_category,
         count(*) AS cases
  FROM case_summary
  GROUP BY 1, 2
)
SELECT service,
       sum(cases) AS total_cases,
       max_by(lifecycle_category, cases) AS top_lifecycle_category
FROM counts
GROUP BY service
ORDER BY total_cases DESC;
```

### Temporal patterns

#### Day-of-week pattern

Cases by the weekday they were opened. `day_of_week` returns 1 for Monday to 7 for Sunday:

```sql
WITH cases AS (
  SELECT from_iso8601_timestamp(timecreated) AS created
  FROM case_summary
)
SELECT day_of_week(created) AS iso_day,
       format_datetime(created, 'EEEE') AS day_name,
       count(*) AS case_count
FROM cases
GROUP BY 1, 2
ORDER BY iso_day;
```

#### Week-of-month pattern

Cases by week of the month: days 1 to 7 are week 1, days 8 to 14 week 2, and so on. Week 5 covers only days 29 to 31, so compare it per day rather than in total:

```sql
WITH cases AS (
  SELECT from_iso8601_timestamp(timecreated) AS created
  FROM case_summary
)
SELECT (day_of_month(created) - 1) / 7 + 1 AS week_of_month,
       count(*) AS case_count
FROM cases
GROUP BY 1
ORDER BY week_of_month;
```

#### Monthly trend

Cases per month over the last 12 months:

```sql
WITH cases AS (
  SELECT from_iso8601_timestamp(timecreated) AS created
  FROM case_summary
)
SELECT format_datetime(created, 'yyyy-MM') AS case_month,
       count(*) AS case_count
FROM cases
WHERE created >= date_trunc('month', current_timestamp) - INTERVAL '12' MONTH
GROUP BY 1
ORDER BY 1;
```

Monthly trend per RCA category, for a stacked chart:

```sql
WITH cases AS (
  SELECT from_iso8601_timestamp(timecreated) AS created,
         rca_category
  FROM case_summary
)
SELECT format_datetime(created, 'yyyy-MM') AS case_month,
       rca_category,
       count(*) AS case_count
FROM cases
GROUP BY 1, 2
ORDER BY 1, 3 DESC;
```

### Severity trends

Cases per month and severity:

```sql
WITH cases AS (
  SELECT from_iso8601_timestamp(timecreated) AS created,
         coalesce(severitycode, 'unknown') AS severity
  FROM case_summary
)
SELECT format_datetime(created, 'yyyy-MM') AS case_month,
       severity,
       count(*) AS case_count
FROM cases
GROUP BY 1, 2
ORDER BY 1, 2;
```

The same trend with one column per severity:

```sql
WITH cases AS (
  SELECT from_iso8601_timestamp(timecreated) AS created,
         severitycode
  FROM case_summary
)
SELECT format_datetime(created, 'yyyy-MM') AS case_month,
       count_if(severitycode = 'critical') AS critical,
       count_if(severitycode = 'urgent') AS urgent,
       count_if(severitycode = 'high') AS high,
       count_if(severitycode = 'normal') AS normal,
       count_if(severitycode = 'low') AS low,
       count(*) AS total
FROM cases
GROUP BY 1
ORDER BY 1;
```

### Top accounts

The ten accounts with the most cases, with their urgent or critical cases, services involved and latest case:

```sql
SELECT account_number,
       count(*) AS case_count,
       count_if(severitycode IN ('urgent', 'critical')) AS urgent_or_critical,
       approx_distinct(servicecode) AS services,
       max(from_iso8601_timestamp(timecreated)) AS latest_case
FROM case_summary
GROUP BY account_number
ORDER BY case_count DESC
LIMIT 10;
```

The most common RCA category for each of those accounts:

```sql
WITH counts AS (
  SELECT account_number,
         rca_category,
         count(*) AS cases
  FROM case_summary
  GROUP BY 1, 2
)
SELECT account_number,
       sum(cases) AS case_count,
       max_by(rca_category, cases) AS top_rca_category
FROM counts
GROUP BY account_number
ORDER BY case_count DESC
LIMIT 10;
```

### Data quality and provenance

Cases whose classification fell back to `Unclassified` after one corrective attempt:

```sql
SELECT account_number,
       case_number,
       rca_category,
       lifecycle_category,
       rca_reason,
       lifecycle_reason
FROM case_summary
WHERE rca_category = 'Unclassified'
   OR lifecycle_category = 'Unclassified';
```

Records by model and prompt version, useful before and after a model change or opt-in reprocessing:

```sql
SELECT model_id,
       prompt_version,
       count(*) AS cases,
       min(lifecycle_retrieval_date) AS first_processed,
       max(lifecycle_retrieval_date) AS last_processed
FROM case_summary
GROUP BY 1, 2
ORDER BY last_processed DESC;
```

A single case, by account and case number (both partitions, so only one record is read):

```sql
SELECT displayid,
       subject,
       servicecode,
       severitycode,
       case_summary,
       rca_category,
       rca_reason,
       lifecycle_category,
       lifecycle_reason
FROM case_summary
WHERE account_number = '<accountId>'
  AND case_number = '<displayId>';
```

## Optional: Amazon QuickSight

QuickSight dashboards are optional and are not created by this application. There is no QuickSight resource in any stack, and this setup has not been tested: QuickSight provisioning, account access, data sets and dashboards are deferred with the rest of the live AWS acceptance. QuickSight subscription, user and SPICE capacity charges are separate from the solution's own costs. Console labels can differ slightly between QuickSight versions.

1. **Prerequisites.** Analytics is deployed, the crawler has run at least once, and QuickSight is subscribed in the management account and in the same Region as the workgroup.
2. **Grant QuickSight access.** In QuickSight, open **Manage QuickSight**, then **Security & permissions**, then **AWS resources**. Enable **Amazon Athena**. Under **Amazon S3**, select the processed bucket (read) and the Athena results bucket from the `AthenaResultsBucketName` output, with **Write permission for Athena Workgroup** enabled for the results bucket. This updates the QuickSight service role in the management account. No AWS KMS permissions are needed because both buckets use SSE-S3. If Lake Formation manages the catalogue, also grant the QuickSight role `SELECT` and `DESCRIBE` on `case_summary`.
3. **Create the data source.** Choose **Datasets**, **New dataset**, **Athena**. Name the data source (for example `CaseInsights-<uniqueId>`), choose the Athena workgroup `CaseInsights-<uniqueId>`, validate the connection and create it.
4. **Create the data set.** Choose the catalogue `AwsDataCatalog`, the database `case_insights_<uniqueId>` and the table `case_summary`, or choose **Use custom SQL** and paste the query below. It converts the text dates to timestamps that QuickSight can use for date hierarchies:

   ```sql
   SELECT account_number,
          case_number,
          servicecode,
          categorycode,
          severitycode,
          rca_category,
          lifecycle_category,
          model_id,
          CAST(from_iso8601_timestamp(timecreated) AS timestamp) AS created_utc,
          CAST(from_iso8601_timestamp(lifecycle_retrieval_date) AS timestamp) AS processed_utc
   FROM case_summary
   ```

   Add `case_summary`, `subject` or `submittedby` only if dashboard readers are allowed to see case content and submitter identities.
5. **Import into SPICE.** SPICE runs the query on refresh instead of on every visual, which keeps scanning bounded by the workgroup cutoff and makes dashboards faster. Schedule a daily refresh after the crawler, for example at 10:00 UTC with the default 09:00 UTC crawl. Direct query also works, but each visual then starts its own Athena query.
6. **Build visuals.** Useful starting points mirror the [query library](#query-library): RCA distribution (bar or donut on `rca_category`), lifecycle categories by service (heat map of `servicecode` against `lifecycle_category`), day-of-week and week-of-month patterns (calculated fields such as `extract('WD', created_utc)`), monthly trend (line chart of `created_utc` by month), severity trends (stacked bar of `severitycode` by month) and top accounts (table of `account_number` sorted by count).
7. **Share carefully.** Share dashboards only with people allowed to see Support case data. QuickSight Enterprise edition supports row-level security, for example by `account_number`.
8. **Removal.** Delete the QuickSight data sets and data source before destroying the Analytics stack, and remove the buckets from QuickSight's S3 access list.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| Queries return no rows, or omit recent cases | The crawler has not run since the records were written, or no case has been processed yet. Start the crawler and check `get-partitions` as shown in [Making new records visible](#making-new-records-visible). |
| `Bytes scanned limit was exceeded` | The query scanned more than `operations.athena.bytesScannedCutoffPerQuery`. Filter on `account_number`, narrow the date range, or raise the limit in configuration and redeploy. |
| `Access Denied` writing to the results location | The caller lacks the results-bucket statements in [Permissions for analysts](#permissions-for-analysts). The location cannot be overridden because the workgroup enforces it. |
| `Access Denied` reading data, or `Insufficient Lake Formation permission(s)` | The caller lacks processed-bucket or Glue read access, or Lake Formation manages the catalogue and has no grant for the caller. |
| `HIVE_BAD_DATA` or `HIVE_CURSOR_ERROR` mentioning JSON | An object under `account_number=*/case_number=*/` is not valid JSON. The pipeline writes complete records only, so check for objects written by something else; the CSV exporter's `--skip-invalid` report lists invalid keys. |
| `CrawlerRunningException` | A crawl is already running; wait until `Crawler.State` is `READY`. |
| Day or month totals differ from local time | Calculations are in UTC. Add `AT TIME ZONE '<zone>'` after `from_iso8601_timestamp(...)`. |

## Live verification (deferred)

None of the following has been run. Local evidence is limited to the synthesised Analytics template (Jest assertions and snapshots in `test/cdk/stacks/analytics-stack.test.ts`, synth and cdk-nag) and to schema checks of this guide's queries (`test/docs/test_analytics_docs.py`). Each item below is deferred until a real deployment:

- [ ] The Analytics stack deploys and its outputs match the names in this guide.
- [ ] The crawler adds partitions for real processed records on schedule and when started manually.
- [ ] Every query in the library runs in `CaseInsights-<uniqueId>` and returns valid categories, with results encrypted in the results bucket.
- [ ] A query that exceeds the scan cutoff is cancelled.
- [ ] The analyst policy above is sufficient and the Athena console works with it.
- [ ] The optional QuickSight data source, SPICE data set and dashboard work with the enforced workgroup.

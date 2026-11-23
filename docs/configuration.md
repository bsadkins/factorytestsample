# Configuration

This guide is the reference for every setting that controls a Support Case Insights deployment: the files in `config/`, how the CDK app selects and validates them, each field with its type, allowed values, default and effect, the cross-field rules with the exact errors they produce, the environment variables the stacks set on each Lambda function, the values that are fixed in code, and the consequences of changing a setting after deployment. Installation steps are in [installation](installation.md), the resources each setting creates are in [resources](resources.md), and the optional pipeline settings are summarised here and documented in full in [pipeline](pipeline.md#configuration). Nothing described here has been deployed to a real account; the behaviour comes from the code, the JSON schemas and the synthesised templates in `cdk.out/`.

## Configuration files

| File | Purpose | Read by the CDK app |
| --- | --- | --- |
| `config/schema.json` | JSON Schema (draft 2020-12) for `config/<env>.json`, with defaults and a `$comment` listing the cross-field rules | Yes, for validation |
| `config/dev.json` | Shipped dev environment (`uniqueId` `scidev`) | With `-c env=dev`, and as the "other" environment when synthesising prod |
| `config/prod.json` | Shipped prod environment (`uniqueId` `sciprod`) | With `-c env=prod`, and as the "other" environment when synthesising dev |
| `config/example.json` | Reference file that sets every field, including the whole `operations` block, to its default | Never; copy from it |
| `config/pipeline.schema.json` | JSON Schema for the optional pipeline file | Only with `-c pipeline=true` |
| `config/pipeline.example.json` | Example pipeline settings | Never; copy it to `config/pipeline.json` |
| `config/pipeline.json` | Optional pipeline settings (not shipped) | Only with `-c pipeline=true` |

Each environment file may start with `"$schema": "./schema.json"` so that editors can validate and complete it. Every object in both schemas sets `additionalProperties: false`, so a misspelt or unknown field is an error rather than being ignored.

The values in the shipped files are invented: account `111111111111`, organisation `o-exampleorg1`, root `r-ab12` and OU `ou-ab12-cdefgh34`. Replace them with your own before any deployment.

## Selecting an environment

Every command takes the environment from the CDK context value `env`, which must be `dev` or `prod`. Run the commands from the repository root:

```bash
npx cdk diff --all -c env=dev
npx cdk deploy --all -c env=dev
npx cdk destroy --all -c env=dev
```

`-c env=<env>` loads `config/<env>.json`. The file's `environment` field must equal the context value, so a file cannot be deployed under the wrong name. Only the two names exist: a third environment needs a code change in `lib/config/types.ts` (`ENVIRONMENT_NAMES`).

When one environment is synthesised, the app also loads and fully validates the other environment's file if it exists, in order to check that both use the same `apiGatewayLogging` settings (see [Cross-field rules](#cross-field-rules)). An invalid `config/prod.json` therefore also stops `-c env=dev`, and the other way round. Delete or fix the other file if you only use one environment.

The optional pipeline app is selected with `-c pipeline=true` instead of `-c env=<env>`; the two cannot be combined. See [Pipeline configuration](#pipeline-configuration).

## How validation works

Configuration is validated before any stack is created, in this order:

1. The file is parsed as JSON and validated against `config/schema.json`. Omitted optional fields receive the defaults shown in the tables below, including every field of `operations`.
2. Cross-field rules that JSON Schema cannot express are checked (`lib/config/load.ts`).
3. The `environment` field is compared with `-c env=<env>`.
4. The other environment file, if present, is validated the same way and the shared API Gateway logging settings are compared (`lib/config/api-logging.ts`).

Any problem stops synthesis: the CDK app prints every issue it found for that file and exits with status 1, and nothing is synthesised or deployed. The output has this form:

```text
Invalid configuration in config/dev.json:
  - /uniqueId must match pattern "^[a-z][a-z0-9]*(-[a-z0-9]+)*$"
```

Schema errors name the JSON pointer of the field. A conditional rule produces two lines, the failing keyword and the rule description, for example:

```text
Invalid configuration in config/dev.json:
  - /enableAnalytics must be true
  - rule violated: enableMcp requires enableAnalytics to be true and at least one mcpAllowedPrincipals entry.
```

Other examples produced by the loader:

| Problem | Message |
| --- | --- |
| Unknown field `foo` at the top level | `(root) has unsupported property "foo"` |
| Model not in the approved list | `/bedrockModelId must be one of "global.anthropic.claude-haiku-4-5-20251001-v1:0", "global.anthropic.claude-sonnet-4-5-20250929-v1:0"` |
| MCP reserved concurrency changed | `/operations/mcp/reservedConcurrency must be 20` |
| Prod without `alarmEmail` | `(root) must have required property 'alarmEmail'` and `rule violated: alarmEmail is required when environment is prod.` |
| `-c env` missing | `Invalid configuration in CDK context "env":` followed by `expected one of dev, prod (pass -c env=<name>); got missing` |
| `-c env=test` | `expected one of dev, prod (pass -c env=<name>); got "test"` |

The messages in this guide were produced by running the loader locally against modified copies of `config/dev.json`.

## Top-level settings

| Field | Type and allowed values | Default | Effect |
| --- | --- | --- | --- |
| `uniqueId` | Required. 3 to 12 characters matching `^[a-z][a-z0-9]*(-[a-z0-9]+)*$` (lower-case letters, digits and single hyphens, starting with a letter) | None | Namespace appended to every stack and physical resource name, for example `CaseInsightsCoreStack-<uniqueId>` and `Support-Case-Analysis-Role-<uniqueId>`. Hyphens become underscores in the Glue database name |
| `orgId` | Required. `^o-[a-z0-9]{10,32}$` | None | AWS Organizations ID used in the `aws:PrincipalOrgID` condition of the member role trust policy and the MCP API resource policy, and the `aws:ResourceOrgID` condition on `sts:AssumeRole` in the collector roles |
| `managementAccountId` | Required. 12 digits | None | Account every stack deploys to. It is excluded from case collection, and service-managed StackSets do not deploy the member role to it |
| `region` | `^[a-z]{2}(-[a-z]+)+-[0-9]$` | `us-east-1` | Region of every stack and the only Region of the member role StackSet. The Bedrock policies are scoped to the global inference profile ARN in this Region |
| `environment` | Required. `dev` or `prod` | None | Must match `-c env=<env>`. `prod` retains buckets, the idempotency table and log groups when stacks are deleted, enables DynamoDB deletion protection and requires `alarmEmail`; `dev` deletes them and empties buckets automatically |
| `enableAnalytics` | Boolean | `false` | Adds `CaseInsightsAnalyticsStack-<uniqueId>` (Glue database, table, crawler, Athena workgroup, results bucket). See [athena](athena.md#what-the-analytics-stack-creates) |
| `enableMcp` | Boolean | `false` | Adds `CaseInsightsMcpStack-<uniqueId>` and, in managed logging mode, `CaseInsightsApiLoggingStack-<sharedId>`. Requires `enableAnalytics` and at least one `mcpAllowedPrincipals` entry |
| `memberRoleTargets` | Required. Array of at least one unique ID, each `r-[0-9a-z]{4,32}` (root) or `ou-[0-9a-z]{4,32}-[a-z0-9]{8,32}` (OU) | None | Deployment targets of the StackSet `CaseInsights-MemberRole-<uniqueId>`. Every account under these targets receives `Support-Case-Analysis-Role-<uniqueId>`, including accounts that join later (automatic deployment) |
| `bedrockModelId` | `global.anthropic.claude-haiku-4-5-20251001-v1:0` or `global.anthropic.claude-sonnet-4-5-20250929-v1:0` | Haiku 4.5 | Global cross-Region inference profile used by the analysis stages and the MCP `analyze_with_bedrock` tool. The IAM policies allow only this profile and deny every other model |
| `bedrockMaxTokens` | Integer 256 to 4096 | `1000` | `max_tokens` sent with every Bedrock request (`BEDROCK_MAX_TOKENS`) |
| `bedrockMaxConcurrency` | Integer 1 to 50 | `5` | Reserved concurrency of `Lambda-CaseAnalysisStages-<uniqueId>`, which is the upper bound on concurrent Bedrock invocations by the pipeline |
| `lookbackMonths` | Integer 1 to 12 | `12` | Trailing calendar months of case creation time collected from each account (`LOOKBACK_MONTHS`). The AWS Support API keeps case data for 12 months |
| `rawRetentionDays` | Integer 7 to 3650 | `90` | Days before objects expire from the raw bucket and the account-list bucket. Must exceed `operations.cleanup.graceHours` |
| `logRetentionDays` | One of 1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653 | `90` | Retention of every log group the stacks create (Lambda, Step Functions and MCP access logs) |
| `alarmEmail` | Email address, at most 254 characters. Required when `environment` is `prod` | None | Creates an SNS email subscription on `CaseInsights-Alarms-<uniqueId>`. The recipient must confirm it. Notifications carry operational metadata, never case content; see [monitoring](monitoring.md) |
| `mcpAllowedPrincipals` | Array of unique IAM role or user ARNs (`arn:<partition>:iam::<12 digits>:role/...` or `:user/...`) | `[]` | The only principals the MCP API resource policy allows, in addition to the organisation condition. At least one entry is required when `enableMcp` is `true` |
| `apiGatewayLogging` | Object, see [apiGatewayLogging](#apigatewaylogging) | `{"mode": "managed", "sharedId": "shared"}` | Owner of the account and Region API Gateway CloudWatch role setting |
| `schedules` | Object, see [schedules](#schedules) | See below | Daily schedules, in UTC |
| `operations` | Object, see [operations](#operations) | Every field defaulted | Bounded operational controls |

## apiGatewayLogging

These settings matter only when `enableMcp` is `true`, but they are validated, and compared across environments, in every case.

| Field | Type and allowed values | Default | Effect |
| --- | --- | --- | --- |
| `apiGatewayLogging.mode` | `managed` or `existing` | `managed` | `managed`: the shared stack `CaseInsightsApiLoggingStack-<sharedId>` creates an API Gateway logging role and the `AWS::ApiGateway::Account` setting, both retained when stacks are deleted. `existing`: the setting is already owned by someone else; nothing is created or changed |
| `apiGatewayLogging.sharedId` | 3 to 12 characters, same pattern as `uniqueId` | `shared` (the shipped files use `scishared`) | Namespace of the shared stack, independent of `uniqueId` so that dev and prod share one owner |
| `apiGatewayLogging.existingRoleArn` | IAM role ARN. Required in `existing` mode and rejected in `managed` mode | None | The role already set as the API Gateway CloudWatch role in this account and Region. It must be in `managementAccountId` |

Read [Shared API Gateway logging prerequisite](mcp-server-deployment.md#shared-api-gateway-logging-prerequisite) before the first MCP deployment: managed mode replaces whatever role the account setting currently names.

## schedules

Each value is a six-field AWS cron expression, `cron(minutes hours day-of-month month day-of-week year)`, evaluated in UTC. The schema accepts only `cron(...)`, not `rate(...)`, and one of day-of-month or day-of-week must be `?`. Each field must hold values EventBridge Scheduler and AWS Glue accept: minutes 0-59, hours 0-23, day-of-month 1-31 (or `L`, `LW`, `<day>W`), month 1-12 or `JAN`-`DEC`, day-of-week 1-7 or `SUN`-`SAT` (or `L`, `<day>L`, `<day>#<1-5>`) and year 1970-2199, as single values, comma-separated lists, ranges (`MON-FRI`) or increments (`0/15`, `*/5`).

| Field | Default | Runs | Effect |
| --- | --- | --- | --- |
| `schedules.accountLookup` | `cron(0 0 * * ? *)` (00:00 UTC daily) | EventBridge Scheduler schedule `CaseInsights-AccountLookup-<uniqueId>` | Starts each daily run by listing member accounts |
| `schedules.cleanup` | `cron(0 22 * * ? *)` (22:00 UTC daily) | EventBridge Scheduler schedule `CaseInsights-CaseCleanup-<uniqueId>` | Makes abandoned incomplete cases eligible for the next run |
| `schedules.crawler` | `cron(0 9 * * ? *)` (09:00 UTC daily) | Schedule of the Glue crawler `case-insights-partition-crawler-<uniqueId>` (Analytics only) | Adds partitions for newly processed cases |

The Run Completion schedule `CaseInsights-RunCompletion-<uniqueId>` is fixed at `rate(1 hour)` and is not configurable.

## operations

Every `operations` field is optional and has a default, so `config/dev.json` and `config/prod.json` omit the whole block. `config/example.json` shows it in full.

### operations.queues

| Field | Allowed values | Default | Effect |
| --- | --- | --- | --- |
| `maxReceiveCount` | 1 to 10 | `3` | Receives before a message on `CaseInsights-ActiveAccounts-<uniqueId>`, `CaseInsights-CaseAnnotation-<uniqueId>` or `CaseInsights-CaseSummary-<uniqueId>` moves to its `-DLQ` queue |
| `visibilityTimeoutSeconds` | 180 to 43200, optional | 6 x `functions.consumerTimeoutSeconds` (1800 with defaults) | Visibility timeout of the three stage queues. Must be at least 6 x `consumerTimeoutSeconds` |
| `dlqRetentionDays` | 5 to 14 | `14` | Retention of the three dead-letter queues and the terminal queues `CaseInsights-WorkflowFailures-<uniqueId>` and `CaseInsights-AccountDiscoveryFailures-<uniqueId>`. SQS keeps a message's original enqueue time when it moves to a dead-letter queue, so the minimum exceeds the fixed 4-day source-queue retention |

### operations.functions

| Field | Allowed values | Default | Effect |
| --- | --- | --- | --- |
| `consumerTimeoutSeconds` | 30 to 900 | `300` | Timeout of the SQS-consuming and scheduled functions: Account Lookup, Account Reader, Case Retrieval, Case Annotation, Workflow Starter, Case Cleanup and Run Completion. Also passed to the Workflow Starter as `CLAIM_SETTLE_SECONDS` |
| `analysisTimeoutSeconds` | 30 to 900 | `180` | Timeout of the workflow tasks: Case Analysis Stages, Update Case Metadata and Handle Failure |
| `memorySizeMb` | 128 to 10240 | `512` | Memory of every function built by this application, including the MCP function |

### operations.workflow

| Field | Allowed values | Default | Effect |
| --- | --- | --- | --- |
| `executionTimeoutMinutes` | 5 to 1440 | `60` | Timeout of one `CaseAnalysis-<uniqueId>` Standard workflow execution. Must be less than `cleanup.graceHours` x 60 |
| `starterMaxConcurrency` | 2 to 50 | `2` | Maximum concurrency of the SQS event source that feeds `Lambda-WorkflowStarter-<uniqueId>` (SQS does not accept less than 2) |
| `taskMaxAttempts` | 1 to 10 | `3` | Attempts of each workflow task for Bedrock and transient errors before the execution routes to failure handling |

### operations.cleanup

| Field | Allowed values | Default | Effect |
| --- | --- | --- | --- |
| `graceHours` | 1 to 720 | `24` | Minimum age of unfinished raw work before Case Cleanup may treat it as abandoned (`CLEANUP_GRACE_HOURS`). Must exceed `workflow.executionTimeoutMinutes` and be less than `rawRetentionDays` x 24 |

### operations.storage

| Field | Allowed values | Default | Effect |
| --- | --- | --- | --- |
| `processedInfrequentAccessDays` | 30 to 3650 | `90` | Days before processed records move to S3 Standard-IA. The rule uses the S3 default minimum transition size of 128 KB, so objects smaller than that, which includes typical processed records of a few kilobytes, stay in S3 Standard. Processed records never expire |
| `noncurrentVersionRetentionDays` | 1 to 3650 | `30` | Days noncurrent object versions are kept in every versioned bucket, including the Athena results bucket |
| `accessLogRetentionDays` | 30 to 3650 | `365` | Days S3 server access logs are kept in the access-log bucket |

### operations.athena

| Field | Allowed values | Default | Effect |
| --- | --- | --- | --- |
| `bytesScannedCutoffPerQuery` | 10485760 (10 MiB) to 1099511627776 (1 TiB) | `10737418240` (10 GiB) | Athena cancels any query in the workgroup that scans more |
| `resultsRetentionDays` | 1 to 3650 | `30` | Days query results, which can contain case content, are kept in the results bucket |

### operations.mcp

| Field | Allowed values | Default | Effect |
| --- | --- | --- | --- |
| `reservedConcurrency` | Only `20` | `20` | Reserved concurrency of `Lambda-McpServer-<uniqueId>`. The value is fixed; any other value is rejected |
| `throttleRateLimit` | 1 to 1000 | `10` | API Gateway stage steady-state requests per second |
| `throttleBurstLimit` | 1 to 2000 | `20` | API Gateway stage burst limit |
| `wafRateLimitPer5Minutes` | 100 to 100000 | `300` | AWS WAF rate-based rule limit per client IP over five minutes (requests over the limit receive `429`) |

## Cross-field rules

These rules are checked after schema validation. Every message below was produced by the loader.

| Rule | Example message |
| --- | --- |
| Each cron schedule uses `?` in exactly one of day-of-month and day-of-week | `/schedules/accountLookup must use "?" in exactly one of day-of-month or day-of-week` |
| Each cron field is in range for its position | `/schedules/accountLookup minutes field "99" is invalid: expected 0-59, got "99"` |
| `operations.queues.visibilityTimeoutSeconds`, when set, is at least 6 x `consumerTimeoutSeconds` | `/operations/queues/visibilityTimeoutSeconds (600) must be at least 6 x consumerTimeoutSeconds (1800)` |
| `operations.cleanup.graceHours` x 60 exceeds `workflow.executionTimeoutMinutes` | `/operations/cleanup/graceHours (1h) must exceed workflow.executionTimeoutMinutes (60min) so cleanup never targets running work` |
| `rawRetentionDays` x 24 exceeds `operations.cleanup.graceHours` | `/rawRetentionDays (7d) must exceed operations.cleanup.graceHours (200h) so raw inputs do not expire before cleanup can reconcile them` |
| `apiGatewayLogging.existingRoleArn` is in `managementAccountId` | `/apiGatewayLogging/existingRoleArn is in account 222222222222 but API Gateway needs a role in the management account 111111111111` |
| `existingRoleArn` is present exactly when `mode` is `existing` (schema rule) | `/apiGatewayLogging must have required property 'existingRoleArn'` or `/apiGatewayLogging must NOT be valid`, each followed by `rule violated: existingRoleArn is required when mode is existing and not allowed when mode is managed.` |
| `enableMcp` needs `enableAnalytics` and at least one `mcpAllowedPrincipals` entry (schema rule) | `/enableAnalytics must be true` or `/mcpAllowedPrincipals must NOT have fewer than 1 items`, each followed by `rule violated: enableMcp requires enableAnalytics to be true and at least one mcpAllowedPrincipals entry.` |
| `alarmEmail` is required in prod (schema rule) | `(root) must have required property 'alarmEmail'` |
| `environment` matches `-c env=<env>` | `/environment is "dev" but the file was loaded for -c env=prod` |
| Environments in the same account and Region use identical `apiGatewayLogging` settings | `Invalid configuration in shared API Gateway logging:` followed by `dev (scidev) and prod (sciprod) deploy to the same account and Region must use the same apiGatewayLogging settings (<dev settings> versus <prod settings>)` |

The shared logging rule compares `mode`, `sharedId` and `existingRoleArn` after defaults are applied. Omitting `apiGatewayLogging` from one file while the other sets `sharedId` to `scishared` is therefore a mismatch.

## Shipped configurations

| Field | `config/dev.json` | `config/prod.json` | `config/example.json` |
| --- | --- | --- | --- |
| `uniqueId` | `scidev` | `sciprod` | `sciprod` |
| `environment` | `dev` | `prod` | `prod` |
| `enableAnalytics` | `true` | `true` | `true` |
| `enableMcp` | `true` | `false` | `false` |
| `memberRoleTargets` | `["r-ab12"]` (whole organisation) | `["ou-ab12-cdefgh34"]` | `["r-ab12"]` |
| `bedrockModelId` | Haiku 4.5 | Haiku 4.5 | Haiku 4.5 |
| `bedrockMaxConcurrency` | `2` | `5` | `5` |
| `rawRetentionDays` | `30` | `90` | `90` |
| `logRetentionDays` | `30` | `90` | `90` |
| `alarmEmail` | Not set (no subscription) | `case-insights-alerts@example.com` | `case-insights-alerts@example.com` |
| `mcpAllowedPrincipals` | `["arn:aws:iam::111111111111:role/CaseInsightsAnalyst"]` | `[]` | `[]` |
| `apiGatewayLogging` | `managed`, `scishared` | `managed`, `scishared` | `managed`, `scishared` |
| `schedules` | Defaults | Defaults | Defaults |
| `operations` | Omitted (defaults) | Omitted (defaults) | Every field at its default |

All three use `orgId` `o-exampleorg1`, `managementAccountId` `111111111111`, `region` `us-east-1`, `bedrockMaxTokens` `1000` and `lookbackMonths` `12`.

To start a new environment file from the reference, copy it and edit at least `uniqueId`, `orgId`, `managementAccountId`, `memberRoleTargets` and `alarmEmail`:

```bash
cp config/example.json config/prod.json
```

## Pipeline configuration

`config/pipeline.json` is read only with `-c pipeline=true` and is validated against `config/pipeline.schema.json`. The full guide is [pipeline](pipeline.md#configuration).

| Field | Allowed values | Default |
| --- | --- | --- |
| `pipelineName` | `^[A-Za-z0-9.@_-]{1,100}$` | `CaseInsights-Pipeline` |
| `account` | Required. 12 digits | None |
| `region` | Required. Region pattern | None |
| `connectionArn` | Required. CodeConnections or CodeStar Connections connection ARN in `account` and `region` | None |
| `repository` | Only `bsadkins/factorytestsample` | `bsadkins/factorytestsample` |
| `branch` | Only `main` | `main` |

The pipeline app also loads `config/dev.json` and `config/prod.json` and stops with these errors when they cannot be deployed side by side:

| Rule | Message |
| --- | --- |
| Connection in the pipeline account | `/connectionArn is in account <account> but the pipeline account is <account>` |
| Connection in the pipeline Region | `/connectionArn is in Region <region> but the pipeline Region is <region>; CodePipeline source actions cannot use a connection in another Region` |
| Connection in the partition of the pipeline Region | `/connectionArn is in partition <partition> but the pipeline Region <region> is in partition <partition>` |
| Environment names | `dev stage configuration declares environment "<name>"`, `prod stage configuration declares environment "<name>"` |
| Distinct namespaces | `dev and prod must use different uniqueId values (both are "<uniqueId>")` |
| Same organisation | `dev orgId <id> differs from prod orgId <id>` |
| Same account and Region as the pipeline | `<env> managementAccountId <id> differs from pipeline account <id>`, `<env> region <region> differs from pipeline region <region>` |
| Context | `-c pipeline=true deploys both environments through the pipeline; do not also pass -c env=<env>` |

## Lambda environment variables

The stacks set these variables; the functions read them at start-up. They are part of the CloudFormation templates, so a change made in the Lambda console is overwritten by the next deployment. Change the configuration file instead.

Every function built by this application (all functions except the two CDK helper functions) receives:

| Variable | Value |
| --- | --- |
| `POWERTOOLS_SERVICE_NAME` | Per function: `account-lookup`, `account-reader`, `case-retrieval`, `case-annotation`, `case-analysis-stages`, `update-case-metadata`, `handle-failure`, `workflow-starter`, `case-cleanup`, `run-completion` or `mcp-server` |
| `POWERTOOLS_METRICS_NAMESPACE` | `SupportCaseInsights` |
| `POWERTOOLS_LOG_LEVEL` | `INFO` |
| `POWERTOOLS_LOGGER_LOG_EVENT` | `false` (events are never logged) |
| `POWERTOOLS_LOGGER_SAMPLE_RATE` | `0` (no debug sampling) |
| `POWERTOOLS_TRACER_CAPTURE_RESPONSE` | `false` |
| `POWERTOOLS_TRACER_CAPTURE_ERROR` | `false` |
| `CASE_INSIGHTS_UNIQUE_ID` | The `uniqueId` |

The Powertools values are fixed in `lib/constructs/pipeline-function.ts` and cannot be overridden from configuration, because logging events or traced responses could record case content. See [logging](logging.md).

Function-specific variables (function names are `Lambda-<Name>-<uniqueId>`):

| Function | Variables |
| --- | --- |
| `AccountLookup` | `ACCOUNT_LIST_BUCKET`, `MANAGEMENT_ACCOUNT_ID` |
| `AccountReader` | `ACCOUNT_LIST_BUCKET`, `ACTIVE_ACCOUNTS_QUEUE_URL`, `IDEMPOTENCY_TABLE_NAME`, `MANAGEMENT_ACCOUNT_ID` |
| `CaseRetrieval` | `RAW_BUCKET`, `PROCESSED_BUCKET`, `IDEMPOTENCY_TABLE_NAME`, `MEMBER_ROLE_NAME` (`Support-Case-Analysis-Role-<uniqueId>`), `MEMBER_ROLE_PARTITION`, `ANNOTATION_QUEUE_URL`, `LOOKBACK_MONTHS` |
| `CaseAnnotation` | `RAW_BUCKET`, `PROCESSED_BUCKET`, `IDEMPOTENCY_TABLE_NAME`, `MEMBER_ROLE_NAME`, `MEMBER_ROLE_PARTITION`, `SUMMARY_QUEUE_URL`, `FAILURE_QUEUE_URL` |
| `CaseAnalysisStages` | `RAW_BUCKET`, `PROCESSED_BUCKET`, `CASE_INSIGHTS_TEMPLATES_DIR` (`/opt/templates`, from the templates layer), `BEDROCK_MODEL_ID`, `BEDROCK_MAX_TOKENS` |
| `UpdateCaseMetadata` | `RAW_BUCKET`, `PROCESSED_BUCKET`, `CASE_INSIGHTS_TEMPLATES_DIR`, `BEDROCK_MODEL_ID` |
| `HandleFailure` | `FAILURE_QUEUE_URL`, `RAW_BUCKET` |
| `WorkflowStarter` | `RAW_BUCKET`, `PROCESSED_BUCKET`, `STATE_MACHINE_ARN`, `CLAIM_SETTLE_SECONDS` (`operations.functions.consumerTimeoutSeconds`), `IDEMPOTENCY_TABLE_NAME` |
| `CaseCleanup` | `RAW_BUCKET`, `PROCESSED_BUCKET`, `STATE_MACHINE_ARN`, `CLEANUP_GRACE_HOURS`, `FAILURE_QUEUE_URL` |
| `RunCompletion` | `ACCOUNT_LIST_BUCKET`, `RAW_BUCKET`, `STATE_MACHINE_ARN`, `FAILURE_QUEUE_URL`, `MANAGEMENT_ACCOUNT_ID`, `RUN_LOOKBACK_HOURS` (`72`; the code rejects values under 24) |
| `McpServer` | `ATHENA_WORKGROUP`, `ATHENA_DATABASE`, `BEDROCK_MODEL_ID`, `BEDROCK_MAX_TOKENS` |

`FAILURE_QUEUE_URL` is the URL of `CaseInsights-WorkflowFailures-<uniqueId>`.

## Local environment variables

These variables affect tools on a workstation or in the pipeline build, not deployed functions.

| Variable | Used by | Effect |
| --- | --- | --- |
| `CASE_INSIGHTS_PYTHON` | Layer bundling during synth (`lib/packaging/python-assets.ts`) | Python interpreter used to install the hash-locked wheels; defaults to `.venv/bin/python` |
| `MCP_SERVER_URL` | `scripts/mcp_aws_client.py` | Default for `--url` |
| `AWS_REGION`, `AWS_PROFILE`, `AWS_CA_BUNDLE` | `scripts/mcp_aws_client.py` (and the AWS SDK in every script) | Defaults for `--region`, `--profile` and `--ca-bundle` |
| `CASE_INSIGHTS_LIVE_BEDROCK` | `test/prompts/test_live_bedrock_prompts.py` | Set to `1` to opt in to the live Bedrock prompt regression; skipped otherwise |
| `CASE_INSIGHTS_LIVE_MODEL_ID` | Same test | Model for the live test; defaults to Haiku 4.5 |
| `CASE_INSIGHTS_LIVE_REGION` | Same test | Region for the live test; defaults to `us-east-1` |
| `PIP_CACHE_DIR`, `PIP_DISABLE_PIP_VERSION_CHECK` | Pipeline Build step | Set to `.cache/pip` and `1` |

## Fixed values

These values are set in code and cannot be changed through configuration.

| Value | Setting | Where |
| --- | --- | --- |
| MCP reserved concurrency | 20 | `lib/config/types.ts` |
| MCP function timeout | 28 seconds (API Gateway allows 29) | `lib/constructs/mcp-server.ts` |
| MCP request budget | 25 seconds, with 1 second kept as Lambda margin | `src/lambda/mcp_tools/limits.py` |
| MCP request body limit | 32,768 bytes, enforced by AWS WAF and by the function | `lib/constructs/mcp-web-acl.ts`, `src/lambda/mcp_server/transport.py` |
| `McpAuthFailures` alarm threshold | 10 refused requests | Core observability |
| Run Completion schedule | `rate(1 hour)`, 72-hour lookback, 30-hour heartbeat window for `MissingDailyRunCompletion` | `lib/constructs/observability.ts` |
| Stage queue retention | 4 days | `lib/constructs/stage-queue.ts` |
| Visibility timeout multiplier | 6 x `consumerTimeoutSeconds` | `lib/config/load.ts` |
| SQS batch sizes | Case Retrieval 1, Case Annotation 5, Workflow Starter 10; maximum concurrency 5 for Retrieval and Annotation | `lib/constructs/case-collection.ts`, `lib/constructs/case-analysis.ts` |
| Workflow retries | Bedrock errors: 15 s, backoff 2, up to `taskMaxAttempts`, at most 2 minutes. Transient errors: 5 s, backoff 2, up to `taskMaxAttempts`, at most 1 minute. Lambda throttling: 5 s, backoff 2, 8 attempts, at most 5 minutes, full jitter. Failure handler: 5 s, backoff 2, 6 attempts, at most 2 minutes | `lib/constructs/case-analysis.ts` |
| Bedrock client retries | 4 attempts, 1 to 16 seconds | Python common layer |
| Idempotency records | 7-day expiry, DynamoDB key `id`, TTL attribute `expiration` | Python handlers, `lib/constructs/core-storage.ts` |
| Summary shape | Four to six sentences, 600 to 650 characters | `src/lambda/common/summary.py` |
| Classification reason | At most 500 characters; `Unclassified` fallback | `src/lambda/common/classification.py` |
| Taxonomies | 16 RCA and 20 lifecycle categories | `src/lambda/common/taxonomy.py` |
| Maximum prompt size | 400,000 characters | Python common layer |
| Lambda runtime | Python 3.12 on x86_64 | `lib/constructs/pipeline-function.ts` |
| Member role session | 1 hour (`MaxSessionDuration` 3600) | Member role template |
| StackSet operations | Up to 25% of accounts concurrently, 10% failure tolerance, Regions sequentially | `lib/stacks/member-role-stack.ts` |
| Incomplete multipart uploads | Aborted after 7 days in every bucket | `lib/constructs/core-storage.ts`, `lib/constructs/analytics.ts` |

## Changing configuration safely

Edit `config/<env>.json`, then review the change before applying it:

```bash
npx cdk diff --all -c env=<env>
npx cdk deploy --all -c env=<env>
```

| Change | Consequence |
| --- | --- |
| `uniqueId` | Creates a complete, separate deployment with new names. The old stacks are not deleted by `cdk deploy`; destroy them first with the old value (see [Teardown](resources.md#teardown)), or keep both. Processed records are not copied |
| `managementAccountId`, `region` | Same as a new deployment in another account or Region. Bootstrap the target first |
| `orgId` | Changes the trust and resource policy conditions. A wrong value stops every member account role assumption and every MCP call |
| `environment` | Must match the file name, so it cannot change in place |
| `memberRoleTargets` | Updates the StackSet deployment targets: accounts under new targets receive the role, and CloudFormation is expected to remove stack instances for targets that were dropped (not verified against a real organisation; check with the commands in [Confirm the member roles are gone](resources.md#confirm-the-member-roles-are-gone)). Accounts that later join a target receive the role automatically, and accounts that leave lose it (`RetainStacksOnAccountRemoval` is `false`) |
| `bedrockModelId` | New analyses use the new model and the IAM policies allow only the new profile. Committed records keep their `Model_Id` and are not analysed again; stage checkpoints from the old model are ignored. Re-analyse deliberately with [Reprocess committed cases](runbook.md#reprocess-committed-cases). The new model may need its own Anthropic first-time-use form and AWS Marketplace subscription |
| `bedrockMaxTokens` | Applies to new requests only |
| `bedrockMaxConcurrency` | Changes the reserved concurrency of the analysis stages function. The account must keep enough unreserved concurrency, together with the MCP function's 20 |
| `lookbackMonths` | Applies from the next run. Raising it collects older resolved cases that are not yet processed |
| `rawRetentionDays` | Updates the lifecycle rules of the raw and account-list buckets; existing objects expire according to the new rule. Must stay above `graceHours` |
| `logRetentionDays` | Updates the retention of every managed log group |
| `alarmEmail` | Replaces the subscription; the new address must confirm it |
| `enableAnalytics: false` | `cdk deploy` does not delete a stack dropped from the app. Follow [Disabling or removing Analytics](athena.md#disabling-or-removing-analytics) |
| `enableMcp: false` | Same pattern: while `enableMcp` is still `true`, run `npx cdk destroy CaseInsightsMcpStack-<uniqueId> -c env=<env>`, then set it to `false` and deploy. The shared logging role and account setting remain; see [Teardown and final clean-up](mcp-server-deployment.md#teardown-and-final-clean-up) |
| `mcpAllowedPrincipals` | Replaces the API resource policy and creates a new API deployment so that the policy takes effect |
| `apiGatewayLogging` | Must change identically in both environment files. Switching from `managed` to `existing` stops managing the setting but leaves the retained role and setting in place |
| `schedules.*` | Takes effect at the next scheduled time; keep account lookup well before cleanup so a run can finish first |
| `operations.queues.*`, `operations.functions.*` | Applied in place. Keep `visibilityTimeoutSeconds` at least six function timeouts |
| `operations.workflow.executionTimeoutMinutes` | Applies to new executions. Keep it below `graceHours` x 60 |
| `operations.storage.*`, `operations.athena.resultsRetentionDays` | Updates lifecycle rules; existing objects follow the new rules |
| `operations.athena.bytesScannedCutoffPerQuery` | Applies to new queries in the workgroup |
| `operations.mcp.*` (except reserved concurrency) | Updates the stage throttling and the WAF rate rule |

Prompt templates and taxonomies are not configuration: they are part of the code, and a change produces a new `Prompt_Version`. See [prompts](prompts.md).

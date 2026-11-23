# Resources

This guide lists every AWS resource that Support Case Insights creates: the stacks and the order in which they deploy, the physical names, what each resource is for, the S3 object layouts, retention and deletion behaviour in dev and prod, the stack outputs, and a complete teardown procedure including the resources that are retained or left behind. It is derived from the synthesised templates in `cdk.out/dev` and `cdk.out/prod` and the CDK code in `lib/`; nothing has been deployed to a real account, so the teardown commands have not been run. Settings that change these resources are in [configuration](configuration.md), IAM roles and policies are in [permissions](permissions.md), and the data flow is in [architecture](architecture.md).

## Stacks

All stacks deploy to `managementAccountId` in `region`. Every name includes `uniqueId`, so dev and prod can coexist in the same account.

| Stack | Created when | Contents |
| --- | --- | --- |
| `CaseInsightsCoreStack-<uniqueId>` | Always | Buckets, idempotency table, queues, Lambda functions and layers, the `CaseAnalysis-<uniqueId>` workflow, schedules, alarms, alarm topic and dashboard |
| `CaseInsightsMemberRoleStack-<uniqueId>` | Always | One service-managed StackSet, `CaseInsights-MemberRole-<uniqueId>`, that deploys `Support-Case-Analysis-Role-<uniqueId>` to member accounts |
| `CaseInsightsAnalyticsStack-<uniqueId>` | `enableAnalytics` is `true` | Glue database, table and crawler, Athena workgroup and results bucket |
| `CaseInsightsMcpStack-<uniqueId>` | `enableMcp` is `true` (requires Analytics) | REST API, AWS WAF web ACL, MCP function and its log groups |
| `CaseInsightsApiLoggingStack-<sharedId>` | `enableMcp` is `true` and `apiGatewayLogging.mode` is `managed` | API Gateway logging role and the account and Region `AWS::ApiGateway::Account` setting, both retained |
| `CaseInsightsPipelineStack` | Only with `-c pipeline=true` | Optional CDK pipeline; see [Pipeline stack](#pipeline-stack) |

The shipped dev configuration produces Core, MemberRole, Analytics, MCP and `CaseInsightsApiLoggingStack-scishared`. The shipped prod configuration produces Core, MemberRole and Analytics.

Every resource that supports tags carries `Project=SupportCaseInsights`, `Environment=<env>` and `UniqueId=<uniqueId>`. The shared logging stack carries `Project` and `SharedId=<sharedId>` instead, because it belongs to no single environment.

### Deployment order and dependencies

`npx cdk deploy --all -c env=<env>` deploys the stacks in dependency order:

| Stack | Depends on | Reason |
| --- | --- | --- |
| MemberRole | Core | The member role trusts `CaseInsights-CaseRetrieval-<uniqueId>` and `CaseInsights-CaseAnnotation-<uniqueId>`, and IAM rejects a trust policy that names roles that do not exist yet |
| Analytics | Core | Reads the processed bucket and logs to the access-log bucket |
| MCP | Core, Analytics, ApiLogging (managed mode) | Uses the Core common layer and processed bucket, queries the Analytics workgroup, and needs the API Gateway CloudWatch role setting before its stage is created |

Core never depends on the optional stacks. Cross-stack references use weak references (`@aws-cdk/core:defaultCrossStackReferences` is `weak` in `cdk.json`), resolved with `Fn::GetStackOutput`. The CDK therefore adds outputs whose names start with `PublishOutput` to the producing stack; they are internal plumbing, not part of the interface.

`cdk deploy` never deletes a stack that is no longer part of the app. Turning an optional stack off needs an explicit destroy; see [Disabling or removing Analytics](athena.md#disabling-or-removing-analytics) and the same pattern for MCP in [Changing configuration safely](configuration.md#changing-configuration-safely).

## Core stack

### S3 buckets

The Core stack creates four buckets with generated names. Use the outputs `RawBucketName` and `ProcessedBucketName`, or the commands in [Before you start](#before-you-start), to find them.

| Bucket | Holds | Lifecycle | Access logs |
| --- | --- | --- | --- |
| Access-log bucket | S3 server access logs of the other buckets | Rule `ExpireAccessLogs`: expire after `operations.storage.accessLogRetentionDays` (default 365) | None (it is the destination) |
| Account-list bucket | One account list per run | Rule `ExpireAccountLists`: expire after `rawRetentionDays` | Prefix `account-list/` |
| Raw bucket | Case metadata, conversations, workflow markers, stage checkpoints and run markers | Rule `ExpireRawCaseData`: expire after `rawRetentionDays` | Prefix `raw/` |
| Processed bucket | One enriched record per case, the output of the application | Rule `ProcessedToInfrequentAccess`: move to S3 Standard-IA after `operations.storage.processedInfrequentAccessDays` (default 90), with the S3 default 128 KB minimum object size, so records smaller than 128 KB stay in S3 Standard. Never expires | Prefix `processed/` |

Every bucket, including the Analytics results bucket and the pipeline buckets:

- uses SSE-S3 default encryption (Amazon S3 managed keys);
- blocks all public access and uses `BucketOwnerEnforced` object ownership, so ACLs are disabled;
- denies requests without TLS and requests with TLS earlier than 1.2;
- is versioned, keeps noncurrent versions for `operations.storage.noncurrentVersionRetentionDays` (default 30) and aborts incomplete multipart uploads after 7 days;
- in dev is deleted with the stack and emptied first by a CDK custom resource; in prod is retained.

Access-log delivery is authorised in the access-log bucket policy, for the `logging.s3.amazonaws.com` service principal with `aws:SourceAccount` and the source bucket ARN. When Analytics is deployed, the statement `AllowAthenaResultsAccessLogs` adds the `athena-results/` prefix.

An S3 event notification on the account-list bucket invokes `Lambda-AccountReader-<uniqueId>` for objects created under `account-lists/` with the suffix `.json`.

### Idempotency table

One DynamoDB table with a generated name, used only as AWS Lambda Powertools idempotency storage by Account Reader, Case Retrieval, Case Annotation and Workflow Starter. It holds no case content and does not coordinate work; SQS and Step Functions do.

| Property | Value |
| --- | --- |
| Key | Partition key `id` (string) |
| Billing | On demand (`PAY_PER_REQUEST`) |
| Expiry | TTL attribute `expiration`; records expire after 7 days |
| Encryption | DynamoDB default encryption with an AWS owned key |
| Point-in-time recovery | Enabled |
| Deletion protection | Enabled in prod, disabled in dev |
| On stack deletion | Retained in prod, deleted in dev |

### Queues

All queues use SSE-SQS encryption, deny requests without TLS and are deleted with the stack in both environments.

| Queue | Consumer | Settings |
| --- | --- | --- |
| `CaseInsights-ActiveAccounts-<uniqueId>` | `Lambda-CaseRetrieval-<uniqueId>` (batch size 1, maximum concurrency 5) | Visibility 1800 s (6 x `consumerTimeoutSeconds`), retention 4 days, `maxReceiveCount` 3 |
| `CaseInsights-CaseAnnotation-<uniqueId>` | `Lambda-CaseAnnotation-<uniqueId>` (batch size 5, maximum concurrency 5) | As above |
| `CaseInsights-CaseSummary-<uniqueId>` | `Lambda-WorkflowStarter-<uniqueId>` (batch size 10, maximum concurrency `starterMaxConcurrency`, default 2) | As above |
| `CaseInsights-ActiveAccounts-<uniqueId>-DLQ`, `CaseInsights-CaseAnnotation-<uniqueId>-DLQ`, `CaseInsights-CaseSummary-<uniqueId>-DLQ` | None; redrive with [Redrive a stage dead-letter queue](runbook.md#redrive-a-stage-dead-letter-queue) | Retention `dlqRetentionDays` (default 14 days) |
| `CaseInsights-WorkflowFailures-<uniqueId>` | None (terminal) | Retention 14 days. Receives failure records from the workflow, cleanup and run completion |
| `CaseInsights-AccountDiscoveryFailures-<uniqueId>` | None (terminal) | Retention 14 days. Receives failed discovery schedule deliveries and failed asynchronous Account Lookup and Account Reader invocations |

The event source mappings report partial batch failures, so only failed messages are retried.

### Lambda functions

Every function built by this application runs Python 3.12 on x86_64 with active X-Ray tracing, `operations.functions.memorySizeMb` of memory (default 512 MB) and its own log group `/aws/lambda/<function name>` with `logRetentionDays` retention.

| Function | Trigger | Timeout | Reserved concurrency |
| --- | --- | --- | --- |
| `Lambda-AccountLookup-<uniqueId>` | Schedule `CaseInsights-AccountLookup-<uniqueId>` (asynchronous; 2 retries, 6-hour maximum event age, failures to `AccountDiscoveryFailures`) | `consumerTimeoutSeconds` (300 s) | None |
| `Lambda-AccountReader-<uniqueId>` | S3 notification on `account-lists/*.json` (asynchronous; same retry and failure settings) | 300 s | None |
| `Lambda-CaseRetrieval-<uniqueId>` | `CaseInsights-ActiveAccounts-<uniqueId>` | 300 s | None |
| `Lambda-CaseAnnotation-<uniqueId>` | `CaseInsights-CaseAnnotation-<uniqueId>` | 300 s | None |
| `Lambda-WorkflowStarter-<uniqueId>` | `CaseInsights-CaseSummary-<uniqueId>` | 300 s | None |
| `Lambda-CaseAnalysisStages-<uniqueId>` | Summary, RCA and Lifecycle tasks of `CaseAnalysis-<uniqueId>` | `analysisTimeoutSeconds` (180 s) | `bedrockMaxConcurrency` (2 in shipped dev, 5 in prod) |
| `Lambda-UpdateCaseMetadata-<uniqueId>` | Update task of the workflow | 180 s | None |
| `Lambda-HandleFailure-<uniqueId>` | Failure path of the workflow | 180 s | None |
| `Lambda-CaseCleanup-<uniqueId>` | Schedule `CaseInsights-CaseCleanup-<uniqueId>` | 300 s | None |
| `Lambda-RunCompletion-<uniqueId>` | Schedule `CaseInsights-RunCompletion-<uniqueId>` | 300 s | None |

The functions use two layers, `case-insights-common-<uniqueId>` (shared Python code and the hash-locked dependencies) and `case-insights-templates-<uniqueId>` (prompt templates, mounted at `/opt/templates`). Old layer versions are deleted when replaced, in both environments.

### Workflow

`CaseAnalysis-<uniqueId>` is a Standard Step Functions state machine with X-Ray tracing. It logs at level `ALL` without execution data (`IncludeExecutionData` is `false`, so inputs and outputs, which can contain case content, are not logged) to `/aws/vendedlogs/states/CaseAnalysis-<uniqueId>`. Each execution times out after `operations.workflow.executionTimeoutMinutes` (default 60). The workflow is described in [architecture](architecture.md).

### Schedules

The three EventBridge Scheduler schedules are in the `default` schedule group, use the `Etc/UTC` time zone and have no flexible time window.

| Schedule | Expression | Target retries |
| --- | --- | --- |
| `CaseInsights-AccountLookup-<uniqueId>` | `schedules.accountLookup` (default `cron(0 0 * * ? *)`) | 3 retries, 6-hour maximum event age, dead-letter queue `CaseInsights-AccountDiscoveryFailures-<uniqueId>` |
| `CaseInsights-CaseCleanup-<uniqueId>` | `schedules.cleanup` (default `cron(0 22 * * ? *)`) | 3 retries, 6-hour maximum event age |
| `CaseInsights-RunCompletion-<uniqueId>` | `rate(1 hour)` (fixed) | 2 retries, 30-minute maximum event age |

### Observability

| Resource | Name | Notes |
| --- | --- | --- |
| SNS topic | `CaseInsights-Alarms-<uniqueId>` | Not encrypted at rest (see [Encryption at rest](#encryption-at-rest)). Its policy denies `sns:Publish` without TLS and allows `cloudwatch.amazonaws.com` to publish only for alarms named `CaseInsights-<uniqueId>-*` in this account; principals in the account whose own IAM policies allow `sns:Publish` can also publish over TLS |
| Email subscription | `alarmEmail` | Only when `alarmEmail` is set (required in prod). Must be confirmed by the recipient |
| CloudWatch alarms | `CaseInsights-<uniqueId>-<purpose>` | 23 Core alarms, plus 4 MCP alarms when `enableMcp` is `true` (27 in the shipped dev, 23 in prod). Every alarm is described in [runbook](runbook.md) |
| Dashboard | `CaseInsights-<uniqueId>` | See [monitoring](monitoring.md) |

The MCP alarms live in the Core stack and use literal resource names, so the MCP stack can be removed without changing Core references.

### CDK helper resources

The CDK adds helper resources that this application does not write itself:

| Helper | Environments | Purpose |
| --- | --- | --- |
| `Custom::S3AutoDeleteObjects` provider (Node.js 24 function and role) | Dev only (Core and Analytics) | Empties buckets before CloudFormation deletes them |
| `BucketNotificationsHandler` (Python 3.13 function and role) | Both | Applies the account-list bucket notification during deployment |

These functions have no explicit log group; if they write logs, Lambda creates log groups for them that are not deleted with the stack.

### Core outputs

| Output | Value |
| --- | --- |
| `RawBucketName` | Raw bucket name |
| `ProcessedBucketName` | Processed bucket name |
| `CaseAnalysisStateMachineArn` | ARN of `CaseAnalysis-<uniqueId>` |
| `CaseSummaryQueueUrl` | URL of `CaseInsights-CaseSummary-<uniqueId>` |

Outputs starting with `PublishOutput` are weak cross-stack references used by the Analytics and MCP stacks.

## Member role stack

The stack contains one `AWS::CloudFormation::StackSet`, `CaseInsights-MemberRole-<uniqueId>`, and has no outputs.

| Property | Value |
| --- | --- |
| Permission model | `SERVICE_MANAGED`, called as `SELF` from the management account. StackSets trusted access with AWS Organizations must be activated first |
| Targets | `memberRoleTargets` (root or OU IDs), Region `region` only |
| Automatic deployment | Enabled; `RetainStacksOnAccountRemoval` is `false`, so an account that leaves the targets loses the role |
| Operation preferences | Soft failure tolerance 10%, up to 25% of accounts concurrently, Regions sequentially; managed execution active |
| Capabilities | `CAPABILITY_NAMED_IAM` |

Each stack instance creates one IAM role, `Support-Case-Analysis-Role-<uniqueId>`, with a one-hour maximum session, trust in only the two collector roles of the management account and only `support:DescribeCases` and `support:DescribeCommunications`. Details are in [permissions](permissions.md). Service-managed StackSets never deploy to the management account, and the account lookup excludes it, so management account cases are not collected.

## Analytics stack

Present when `enableAnalytics` is `true`. [What the Analytics stack creates](athena.md#what-the-analytics-stack-creates) lists every resource and output, and [Workgroup and query results](athena.md#workgroup-and-query-results) describes the workgroup. In summary: the Glue database `case_insights_<uniqueId>`, the external table `case_summary`, the crawler `case-insights-partition-crawler-<uniqueId>` with its role, the Athena workgroup `CaseInsights-<uniqueId>` (engine version 3, enforced configuration, SSE_S3 results, scan cutoff), and a results bucket with the common bucket settings, results under `query-results/` expiring after `operations.athena.resultsRetentionDays`. The workgroup is created with recursive delete, so destroying the stack also deletes its query history. In prod the results bucket is retained.

## MCP stack

Present when `enableMcp` is `true`. [Infrastructure and security controls](mcp-server-deployment.md#infrastructure-and-security-controls) describes it in full.

| Resource | Name |
| --- | --- |
| REST API (regional), stage `v1`, method `POST /mcp` with `AWS_IAM` authorisation | `CaseInsights-Mcp-<uniqueId>` |
| AWS WAF web ACL and its association with the stage | `CaseInsights-Mcp-<uniqueId>` |
| Lambda function (reserved concurrency 20, timeout 28 s) and role | `Lambda-McpServer-<uniqueId>` |
| Function log group | `/aws/lambda/Lambda-McpServer-<uniqueId>` |
| Access log group, with the metric filter `AuthFailures` | `/aws/apigateway/CaseInsights-Mcp-<uniqueId>/access` |

Outputs: `McpEndpointUrl`, `McpRestApiId`, `McpFunctionName`, `McpAccessLogGroupName`, `McpWebAclArn`, and either `ApiGatewayLoggingStack` (managed mode) or `ApiGatewayCloudWatchRoleArn` (existing mode). The CDK also adds its default REST API endpoint output, whose name starts with `McpServerApiEndpoint`.

## Shared API logging stack

`CaseInsightsApiLoggingStack-<sharedId>` contains an IAM role trusted by `apigateway.amazonaws.com` (generated name, inline policy `ApiGatewayAccessLogs`) and one `AWS::ApiGateway::Account` resource that sets it as the account and Region CloudWatch role. Both have `DeletionPolicy: Retain`. Outputs: `ApiGatewayCloudWatchRoleArn` and `ApiGatewayCloudWatchRoleName`. See [Managed mode](mcp-server-deployment.md#managed-mode).

## Pipeline stack

Only with `-c pipeline=true`; see [pipeline](pipeline.md). `CaseInsightsPipelineStack` contains a CodePipeline V2 pipeline (`pipelineName`, default `CaseInsights-Pipeline`) in `QUEUED` mode with self-mutation, a CodeBuild synth step (standard 8.0 image, Node.js 24, Python 3.12, medium compute, 60-minute timeout), asset publishing with a pinned `cdk-assets` release, the manual approval `PromoteToProd`, and roles generated by CDK Pipelines. It has two retained buckets with the common settings: the artifact bucket (objects expire after 90 days, access logs under `pipeline-artifacts/`) and its access-log bucket (logs expire after 365 days). The stage stacks it deploys are the same stacks as above.

## S3 object layouts

### Account-list bucket

| Key | Written by | Content |
| --- | --- | --- |
| `account-lists/<runId>.json` | Account Lookup | Active member accounts of one run. Creating it starts the run |

### Raw bucket

| Key | Written by | Content |
| --- | --- | --- |
| `account_number=<accountId>/case_number=<displayId>/data.json` | Case Retrieval | Case metadata from `DescribeCases` |
| `account_number=<accountId>/case_number=<displayId>/communications.json` | Case Annotation | Case conversation from `DescribeCommunications` |
| `account_number=<accountId>/case_number=<displayId>/workflow.json` | Workflow Starter, Case Cleanup | Ownership marker naming the execution that owns the case |
| `account_number=<accountId>/case_number=<displayId>/checkpoints/<stage>.json` | Case Analysis Stages | Validated output of the `summary`, `rca` or `lifecycle` stage, reused on retry only for the same case, model, prompt version and input |
| `account_number=<accountId>/case_number=<displayId>/terminal-failure.json` | Case Annotation | Marker for a conversation too large to summarise, so an unchanged case is not reported again |
| `account_number=<accountId>/case_number=<displayId>/failure-deliveries/<executionName>.json` | Workflow Starter, Handle Failure, Case Cleanup, Run Completion | Pending or confirmed delivery of a failure record |
| `runs/<runId>/accounts/<accountId>.json` | Case Retrieval | How an account was handled in a run (identifiers and counts only) |
| `runs/<runId>/cases/<accountId>/<displayId>.json` | Case Annotation, Workflow Starter | A case's outcome or hand-off in a run (identifiers only) |
| `runs/<runId>/completion.json` | Run Completion | Written once when every account and case of the run has reached a terminal outcome |
| `cleanup-state/cursor.json` | Case Cleanup | Resume position when a cleanup run runs out of time |

Every raw object, including run markers, expires after `rawRetentionDays`.

### Processed bucket

| Key | Written by | Content |
| --- | --- | --- |
| `account_number=<accountId>/case_number=<displayId>/data.json` | Update Case Metadata | The processed record, written once with a conditional create after all three stages succeed |

The processed record is one JSON object with exactly 18 fields: `caseId`, `displayId`, `subject`, `serviceCode`, `categoryCode`, `severityCode`, `submittedBy`, `timeCreated`, `Case_Retrieval_Date`, `Case_Summary`, `RCA_Category`, `RCA_Reason`, `RCA_Retrieval_Date`, `Lifecycle_Category`, `Lifecycle_Reason`, `Lifecycle_Retrieval_Date`, `Model_Id` and `Prompt_Version`. Their meanings, nullability and formats are in [Columns](athena.md#columns). An existing processed record stops further automatic inference for that case; only [Reprocess committed cases](runbook.md#reprocess-committed-cases) replaces it deliberately.

### Access-log and results buckets

| Bucket | Key prefix | Content |
| --- | --- | --- |
| Access-log bucket | `account-list/`, `raw/`, `processed/`, `athena-results/` | S3 server access logs of each source bucket |
| Athena results bucket | `query-results/` | Athena query results and metadata |
| Pipeline artifact access-log bucket | `pipeline-artifacts/` | Access logs of the pipeline artifact bucket |

## Retention and deletion

| Resource | Retention | Dev on stack deletion | Prod on stack deletion |
| --- | --- | --- | --- |
| Raw and account-list objects | `rawRetentionDays` (dev 30, prod 90) | Bucket emptied and deleted | Bucket retained |
| Processed records | Never expire | Bucket emptied and deleted | Bucket retained |
| Access logs | `accessLogRetentionDays` (365) | Bucket emptied and deleted | Bucket retained |
| Athena results | `resultsRetentionDays` (30) | Bucket emptied and deleted | Bucket retained |
| Noncurrent object versions | `noncurrentVersionRetentionDays` (30) | Deleted with the bucket | Retained with the bucket |
| Idempotency records | 7 days (TTL) | Table deleted | Table retained, deletion protection on |
| Lambda, Step Functions and MCP log groups | `logRetentionDays` (dev 30, prod 90) | Deleted | Retained |
| Stage queue messages | 4 days | Deleted | Deleted |
| Dead-letter and terminal queue messages | `dlqRetentionDays` (14) | Deleted | Deleted |
| Glue catalogue, crawler, workgroup and query history | Until deleted | Deleted | Deleted |
| Member roles in member accounts | Until removed | StackSet and instances deleted | StackSet and instances deleted |
| Shared API logging role and account setting | Until removed by hand | Retained | Retained |
| Pipeline buckets | 90-day artifacts, 365-day logs | Retained | Retained |

Queues, functions, layers, schedules, the state machine, alarms, the topic, the dashboard and the Glue and Athena resources are deleted with their stacks in both environments.

## Encryption at rest

The application creates no customer managed KMS keys, and no KMS permissions are needed by any role or operator.

| Data | Encryption |
| --- | --- |
| Every S3 bucket | SSE-S3 |
| Athena query results | `SSE_S3`, enforced by the workgroup |
| SQS queues | SSE-SQS |
| DynamoDB idempotency table | Default encryption with an AWS owned key |
| CloudWatch Logs | CloudWatch Logs default service-side encryption |
| Alarm topic `CaseInsights-Alarms-<uniqueId>` | **Not encrypted at rest.** CloudWatch alarms can publish to an encrypted topic only when it uses a customer managed KMS key, which the approved design excludes. The topic policy denies publishing without TLS and allows CloudWatch to publish only for this deployment's alarms; it does not block principals in the account whose own IAM policies allow publishing. Notifications contain no case content, but they do contain operational metadata (alarm names and descriptions, states, metric values, the account ID, the Region and resource names), and that metadata leaves the account through the confirmed email subscriptions |

See [security](security.md) for the full data protection description.

## Teardown

These steps remove a deployment completely. Run them from the repository root with credentials for the management account. They have not been run against a real account. Data deleted from the processed bucket cannot be recovered; export it first with [csv_export](csv_export.md) if you need it.

### Before you start

1. Record the physical names of the resources that prod retains, because they have generated names:

   ```bash
   aws cloudformation describe-stack-resources --stack-name CaseInsightsCoreStack-<uniqueId> --region <region> \
     --query "StackResources[?ResourceType=='AWS::S3::Bucket' || ResourceType=='AWS::DynamoDB::Table'].[LogicalResourceId,PhysicalResourceId]" \
     --output table
   aws cloudformation describe-stack-resources --stack-name CaseInsightsAnalyticsStack-<uniqueId> --region <region> \
     --query "StackResources[?ResourceType=='AWS::S3::Bucket'].[LogicalResourceId,PhysicalResourceId]" \
     --output table
   ```

   After the stacks are gone, the retained resources can still be found by tag:

   ```bash
   aws resourcegroupstaggingapi get-resources --tag-filters Key=UniqueId,Values=<uniqueId> --region <region> \
     --query 'ResourceTagMappingList[].ResourceARN' --output text
   ```

2. Record the member role stack instances, so you can confirm their removal:

   ```bash
   aws cloudformation list-stack-instances --stack-set-name CaseInsights-MemberRole-<uniqueId> --region <region> \
     --query 'Summaries[].[Account,Status]' --output table
   ```

### Destroy the stacks

```bash
npx cdk destroy --all -c env=<env>
```

The CDK deletes the stacks in reverse dependency order (MCP, Analytics, MemberRole, Core) and also deletes `CaseInsightsApiLoggingStack-<sharedId>` when it is part of the app; its role and account setting are retained. Deleting the MemberRole stack deletes the StackSet; CloudFormation is expected to delete its stack instances, and therefore the member roles, first. Confirm it with the next step.

In dev the buckets are emptied and deleted, and the table and log groups are deleted. In prod the four Core buckets, the Analytics results bucket, the idempotency table and every managed log group remain.

### Confirm the member roles are gone

```bash
aws cloudformation describe-stack-set --stack-set-name CaseInsights-MemberRole-<uniqueId> --region <region>
```

A `StackSetNotFoundException` error means the StackSet and its instances are deleted. To spot-check a member account, run this with credentials for that account; it should fail with `NoSuchEntity`:

```bash
aws iam get-role --role-name Support-Case-Analysis-Role-<uniqueId>
```

If the MemberRole stack failed to delete because stack instances remain, delete them, wait for the operation to finish, then delete the StackSet and run `npx cdk destroy --all -c env=<env>` again:

```bash
aws cloudformation delete-stack-instances --stack-set-name CaseInsights-MemberRole-<uniqueId> \
  --deployment-targets OrganizationalUnitIds=<root-or-ou-id> --regions <region> --no-retain-stacks --region <region>
aws cloudformation list-stack-set-operations --stack-set-name CaseInsights-MemberRole-<uniqueId> --region <region> \
  --query 'Summaries[0].[OperationId,Status]' --output text
aws cloudformation delete-stack-set --stack-set-name CaseInsights-MemberRole-<uniqueId> --region <region>
```

Use the same root or OU IDs as `memberRoleTargets`, separated by commas.

### Delete retained prod resources

Every bucket is versioned, so deleting it requires deleting every object version and delete marker first. This command does that for one bucket with the repository's Python environment, then deletes the bucket. Delete the access-log bucket last, because the other buckets deliver logs to it until they are gone:

```bash
for bucket in <raw-bucket> <account-list-bucket> <processed-bucket> <athena-results-bucket> <access-log-bucket>; do
  .venv/bin/python -c 'import sys, boto3; boto3.resource("s3").Bucket(sys.argv[1]).object_versions.delete()' "$bucket" \
    && aws s3api delete-bucket --bucket "$bucket" --region <region>
done
```

S3 delivers server access logs with a delay, so the access-log bucket can receive late log objects after the others are deleted. If its deletion fails with `BucketNotEmpty`, run the command again for that bucket.

Turn off deletion protection on the idempotency table, then delete it:

```bash
aws dynamodb update-table --table-name <idempotency-table> --no-deletion-protection-enabled --region <region>
aws dynamodb delete-table --table-name <idempotency-table> --region <region>
```

Delete the retained log groups (a group that does not exist reports `ResourceNotFoundException`, which is harmless):

```bash
for name in AccountLookup AccountReader CaseRetrieval CaseAnnotation CaseAnalysisStages UpdateCaseMetadata HandleFailure WorkflowStarter CaseCleanup RunCompletion McpServer; do
  aws logs delete-log-group --log-group-name "/aws/lambda/Lambda-${name}-<uniqueId>" --region <region>
done
aws logs delete-log-group --log-group-name /aws/vendedlogs/states/CaseAnalysis-<uniqueId> --region <region>
aws logs delete-log-group --log-group-name /aws/apigateway/CaseInsights-Mcp-<uniqueId>/access --region <region>
```

### Shared API logging, pipeline and trusted access

- **Shared API Gateway logging.** After every MCP environment in the account and Region is destroyed, follow [Teardown and final clean-up](mcp-server-deployment.md#teardown-and-final-clean-up) to remove the retained role and account setting. Do not delete them while another API depends on them.
- **Pipeline.** If you deployed it, delete the pipeline stack with `npx cdk destroy -c pipeline=true CaseInsightsPipelineStack`, then empty and delete its two retained buckets with the bucket command above. Delete the CodeConnections connection if nothing else uses it.
- **StackSets trusted access.** Deactivate it only if no other service-managed StackSet in the organisation needs it. Check the status, then deactivate:

  ```bash
  aws cloudformation describe-organizations-access --region <region>
  aws cloudformation deactivate-organizations-access --region <region>
  ```

### What remains

These are not created or deleted by the application stacks:

- The CDK bootstrap stack `CDKToolkit` and its assets bucket (`cdk-hnb659fds-assets-<account-id>-<region>`), which holds uploaded Lambda assets. Keep them if other CDK apps use the account.
- Log groups that Lambda creates for the CDK helper functions (find them with `aws logs describe-log-groups --log-group-name-prefix /aws/lambda/CaseInsights --region <region>`) and the Glue crawler log group `/aws-glue/crawlers`, which Glue creates and other crawlers may share.
- Any AWS Marketplace subscription and Anthropic first-time-use submission for the Bedrock model, which belong to the account.
- Local files: `cdk.out/`, `.cache/pip`, `.venv` and `node_modules`.
- Case data in AWS Support itself, which this application only reads.

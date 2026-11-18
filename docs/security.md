# Security

This guide describes how Support Case Insights protects AWS Support case data: who is responsible for what, which data the solution handles and where it flows, the stacks and entry points of each shipped environment, encryption at rest and in transit for every service (including the one approved exception, the alarm SNS topic), the identity and trust boundaries, a threat model, the use of Amazon Bedrock global cross-Region inference, the limits of retries and idempotency, every cdk-nag acknowledgement, the current verification status and how to report a vulnerability. Every statement is taken from the code under `lib/` and `src/` and from the synthesised templates in `cdk.out/dev` and `cdk.out/prod`. Nothing has been deployed, so every live control listed in [Security verification status](#security-verification-status) is still unverified. The full IAM detail is in [permissions](permissions.md) and the full resource inventory in [resources](resources.md).

## Shared responsibility

AWS secures the infrastructure that runs the services this solution uses (Amazon S3, Amazon SQS, AWS Lambda, AWS Step Functions, Amazon DynamoDB, Amazon Bedrock, AWS Glue, Amazon Athena, Amazon API Gateway, AWS WAF, Amazon SNS, Amazon CloudWatch and the AWS Support API), including their physical security, service-side encryption and the isolation between customers. The solution's code configures those services securely by default. The operator remains responsible for everything below.

| Area | What the operator must do |
| --- | --- |
| Management account access | The stacks must be deployed in the AWS Organizations management account, because the member role StackSet is called as `SELF` and account discovery calls `organizations:ListAccounts`. The management account is the most privileged account in the organisation: keep human access to it minimal, require MFA, and remember that anyone with broad rights there can read case data of every environment deployed in it (the shipped dev and prod configurations use the same account). |
| Deployment pipeline and repository | Protect the branch you deploy from. With the optional pipeline, write access to `main` is deployment access to the management account; see [Trust boundary](pipeline.md#trust-boundary). Review `npx cdk diff --all -c env=<env>` before every deployment. |
| `mcpAllowedPrincipals` hygiene | List only the IAM role or user ARNs that may query case data through the MCP endpoint, remove principals that no longer need access, and grant those principals `execute-api:Invoke` only on the MCP route. Every listed principal can read every processed case and can cause Amazon Bedrock charges. |
| Data access grants | Read access to the raw, processed or Athena results buckets, the log groups or the Glue table is access to case data from every member account. Grant it only to people who may see all of those cases; see [Operator and analyst permissions](permissions.md#operator-and-analyst-permissions). |
| Alarm email recipients | `alarmEmail` receives operational metadata about the deployment (see [Alarm SNS topic exception](#alarm-sns-topic-exception)). Use a monitored group address inside your organisation, confirm the subscription, and review who else subscribes to the topic. |
| Amazon Bedrock model access | Submit the Anthropic first-time use (FTU) form and allow the AWS Marketplace subscription for the configured model; IAM cannot complete either. Decide whether global cross-Region inference is acceptable under your data residency policy before deploying (see [Amazon Bedrock and global cross-Region inference](#amazon-bedrock-and-global-cross-region-inference)). |
| Reviewing outputs | Summaries, classifications and MCP analysis answers are model output. Review them before acting on them or sharing them, and treat them as untrusted text in any downstream system. |
| Data classification | Classify AWS Support case content under your own data policy (it can contain personal data and secrets that users pasted into cases), set `rawRetentionDays`, `operations.athena.resultsRetentionDays` and `logRetentionDays` accordingly, and handle CSV exports as data of the same classification. |
| Audit and detection | The solution creates no AWS CloudTrail trail, AWS Config rules, Amazon GuardDuty detectors or AWS Budgets. Use your organisation's existing controls, and consider a budget alert as described in [cost](cost.md). |
| Teardown | Delete retained prod data deliberately when it is no longer needed; see [Teardown](resources.md#teardown). |

## Data classification and personal data

### What the solution handles

| Data | Content | Classification guidance |
| --- | --- | --- |
| Case metadata (`data.json` in the raw bucket) | Case ID, display ID, subject, service, category and severity codes, submitter (`submittedBy`, usually an email address), creation time | Confidential; contains personal data |
| Case conversations (`communications.json` in the raw bucket) | Every communication body of the case as returned by `support:DescribeCommunications` | Confidential. Can contain anything a user or AWS Support engineer wrote, including names, email addresses, phone numbers, resource identifiers, log extracts and secrets that were pasted into the case |
| Stage checkpoints (raw bucket, `checkpoints/`) | The generated summary and classifications of a case | Confidential; derived from case content |
| Processed records (`data.json` in the processed bucket) | The 18 processed fields, including `subject`, `submittedBy` and `Case_Summary` | Confidential; contains personal data |
| Athena query results, MCP tool results and CSV exports | Whatever a query or export selects from the processed records | Same as the processed records |
| Account lists, run markers, ownership and failure markers | Account IDs and names, run IDs, case display IDs, execution names, counts | Internal; identifies member accounts and cases |
| Logs, metrics, traces and access logs | Identifiers and counts only (see below) | Internal |
| Alarm notifications | Operational metadata only | Internal; leaves the account by email |

The summary prompt asks the model not to include names, email addresses, phone numbers, credentials or other personal data, and the summary validation rejects headers, markdown and lists. This reduces, but does not guarantee the absence of, personal data in `Case_Summary`. `subject` and `submittedBy` are stored as retrieved.

### Where the data flows

1. **Member accounts to the management account.** `Lambda-CaseRetrieval-<uniqueId>` and `Lambda-CaseAnnotation-<uniqueId>` assume `Support-Case-Analysis-Role-<uniqueId>` in each member account and call the AWS Support API (`DescribeCases`, `DescribeCommunications`) over TLS. Nothing is written to AWS Support.
2. **Raw storage.** Metadata and the full conversation are written to the raw bucket under `account_number=<id>/case_number=<displayId>/`. SQS messages carry only references (account ID, display ID, run ID), never case content.
3. **Amazon Bedrock.** `Lambda-CaseAnalysisStages-<uniqueId>` sends the conversation (Summary stage) and then the summary (RCA and Lifecycle stages) to the configured global inference profile; see [Amazon Bedrock and global cross-Region inference](#amazon-bedrock-and-global-cross-region-inference). Step Functions state holds only references.
4. **Processed storage.** `Lambda-UpdateCaseMetadata-<uniqueId>` writes one processed record per case with a conditional create.
5. **Analytics.** With `enableAnalytics`, the Glue crawler adds partitions (account ID and display ID values) to `case_insights_<uniqueId>.case_summary`, and Athena reads the processed records and writes results to the results bucket under `query-results/`.
6. **Consumers.** Analysts query with Athena ([athena](athena.md)), export CSV files to their own machine with `scripts/generate_csv_insights.py` ([csv_export](csv_export.md)), or call the MCP endpoint, whose `analyze_with_bedrock` and `analyze_case_summaries` tools send selected data or processed records to Bedrock again ([mcp-server-deployment](mcp-server-deployment.md)).

No case content is sent to any service outside AWS, and the application has no telemetry of its own. `cdk.json` sets `versionReporting` and `cli-telemetry` to `false`.

### What is logged and what is not

Lambda functions log structured JSON records through AWS Lambda Powertools with `POWERTOOLS_LOGGER_LOG_EVENT=false` and `POWERTOOLS_TRACER_CAPTURE_RESPONSE` and `POWERTOOLS_TRACER_CAPTURE_ERROR` set to `false`. Records contain identifiers, outcomes, counts, durations, token counts and controlled reason codes. Exceptions are logged through `common.safe_logging.error_fields`, which keeps only the exception class, the AWS error code and the HTTP status, because AWS error messages can repeat request content.

Never logged: case subjects, descriptions, communications or attachments; summaries, classification replies, prompts and other model output; invocation events, SQS message bodies, Step Functions state input and output (`IncludeExecutionData` is `false`) and HTTP bodies; MCP tool arguments, SQL text and query results; AWS error messages; credentials, tokens and signatures. The MCP access log format omits headers, bodies, caller ARNs and `$context.error.message`; API Gateway execution logging is off, and the web ACL has sampled requests turned off.

Logged and identifying: account IDs, case display IDs, run IDs, SQS message IDs, S3 keys (which contain the account ID and display ID), and in the MCP access log the source IP address and caller account ID. Protect the log groups like the operational data. The full field vocabulary is in [logging](logging.md).

### Retention

| Data | Retention | Setting |
| --- | --- | --- |
| Raw objects (metadata, conversations, checkpoints, markers) and account lists | Expire after `rawRetentionDays` (dev 30, prod 90, default 90) | `rawRetentionDays` |
| Processed records | Never expire; move to S3 Standard-IA after `operations.storage.processedInfrequentAccessDays` (default 90) when larger than the 128 KB minimum transition size | `operations.storage.processedInfrequentAccessDays` |
| Athena query results | Expire after `operations.athena.resultsRetentionDays` (default 30) | `operations.athena.resultsRetentionDays` |
| Noncurrent object versions in every bucket | Expire after `operations.storage.noncurrentVersionRetentionDays` (default 30) | `operations.storage.noncurrentVersionRetentionDays` |
| S3 server access logs | 365 days by default | `operations.storage.accessLogRetentionDays` |
| Idempotency records | 7-day TTL | Fixed |
| Log groups created by the stacks | `logRetentionDays` (dev 30, prod 90) | `logRetentionDays` |

Because buckets are versioned, deleting an object (for example by Case Cleanup or the reprocess script) leaves a noncurrent version until the noncurrent-version rule expires it. In prod, buckets, the table and log groups are retained when the stacks are destroyed; in dev they are deleted. See [Retention and deletion](resources.md#retention-and-deletion).

## Environment inventory

The shipped configurations are `config/dev.json` (`uniqueId` `scidev`) and `config/prod.json` (`uniqueId` `sciprod`). Both use the example management account 111111111111, Region `us-east-1` and organisation o-exampleorg1.

| | Dev (`scidev`) | Prod (`sciprod`) | Shared (`scishared`, deployed with dev) |
| --- | --- | --- | --- |
| Stacks | `CaseInsightsCoreStack-scidev`, `CaseInsightsMemberRoleStack-scidev`, `CaseInsightsAnalyticsStack-scidev`, `CaseInsightsMcpStack-scidev` | `CaseInsightsCoreStack-sciprod`, `CaseInsightsMemberRoleStack-sciprod`, `CaseInsightsAnalyticsStack-sciprod` | `CaseInsightsApiLoggingStack-scishared` |
| Member role | `Support-Case-Analysis-Role-scidev` in accounts under `r-ab12` (StackSet `CaseInsights-MemberRole-scidev`) | `Support-Case-Analysis-Role-sciprod` in accounts under `ou-ab12-cdefgh34` (StackSet `CaseInsights-MemberRole-sciprod`) | None |
| Data stores | Access-log, account-list, raw and processed buckets; idempotency table; 8 SQS queues; Athena results bucket; Glue database `case_insights_scidev`; log groups | The same, with database `case_insights_sciprod` | None |
| Deletion behaviour | Buckets emptied and deleted, table and log groups deleted | Buckets, table and log groups retained; table deletion protection on | Role and `AWS::ApiGateway::Account` setting retained |
| Network entry points | One regional REST API, `CaseInsights-Mcp-scidev`, route `POST /v1/mcp`, `AWS_IAM` authorisation, resource policy and web ACL `CaseInsights-Mcp-scidev` | None | None (account and Region API Gateway setting only) |
| Outbound | AWS service APIs only (Lambda functions are not attached to a VPC); alarm email when `alarmEmail` is set (not set in dev, so the dev topic has no subscription) | AWS service APIs only; alarm email to `alarmEmail` (required in prod) | None |
| Bedrock | `bedrockMaxConcurrency` 2, Haiku 4.5 global profile | `bedrockMaxConcurrency` 5, Haiku 4.5 global profile | None |

The application exposes no other inbound endpoint: S3 buckets block all public access, queues and topics accept only TLS and are reachable only through IAM, and Lambda functions have no function URLs. The optional pipeline stack (`CaseInsightsPipelineStack`, `-c pipeline=true`) adds a CodePipeline sourced from an existing CodeConnections connection; see [pipeline](pipeline.md).

## Encryption at rest and in transit

Every resource uses AWS managed or service-managed encryption, and the application creates no customer managed KMS keys. No KMS permissions are needed by the deployer, the runtime roles, operators or analysts. Not every resource is encrypted at rest: the alarm SNS topic is the approved exception described in the next section.

| Service and resource | At rest | In transit |
| --- | --- | --- |
| S3: access-log, account-list, raw, processed and Athena results buckets | SSE-S3 (`AES256`) default bucket encryption | Bucket policies deny every action when `aws:SecureTransport` is false and when `s3:TlsVersion` is below 1.2 (`enforceSSL`) |
| SQS: 3 stage queues, 3 dead-letter queues and 2 terminal queues | SSE-SQS (`SqsManagedSseEnabled`) | Queue policies deny every action without TLS |
| DynamoDB idempotency table | Default encryption with an AWS owned key (`SSEEnabled` false selects the AWS owned key) | AWS SDK calls over HTTPS |
| CloudWatch Logs: Lambda, Step Functions and MCP access log groups, and `/aws-glue/crawlers` | CloudWatch Logs default service-side encryption; no KMS key is associated with any log group | HTTPS |
| Athena query results | `SSE_S3`, enforced by the workgroup (`EnforceWorkGroupConfiguration`), in the SSE-S3 results bucket | HTTPS |
| Step Functions `CaseAnalysis-<uniqueId>` | Step Functions default encryption with an AWS owned key; no encryption configuration is set. State holds references only | HTTPS |
| Lambda environment variables | Lambda default encryption with an AWS managed key; no customer managed key is set. The variables hold resource names and settings, never secrets | Not applicable |
| Lambda layers and function code | Stored by Lambda and in the CDK bootstrap assets bucket with their default encryption | HTTPS during asset upload |
| Glue Data Catalog database and table | The application does not change the account's Data Catalog encryption settings, so whatever account-level setting is in place applies. The catalogue holds the table schema and partition locations (account IDs and display IDs), not case content | HTTPS |
| SNS alarm topic `CaseInsights-Alarms-<uniqueId>` | **Not encrypted at rest**; see [Alarm SNS topic exception](#alarm-sns-topic-exception) | The topic policy denies `sns:Publish` without TLS; email delivery to subscribers is outside AWS's control |
| API Gateway and AWS WAF | Not applicable (no stored payloads; sampled requests off) | Regional HTTPS endpoint with SigV4 |
| AWS Support API, AWS STS, Amazon Bedrock and Athena calls | Not applicable | HTTPS from the AWS SDK |

The local MCP bridge `scripts/mcp_aws_client.py` accepts only `https://` URLs (plain HTTP only for loopback test hosts) and cannot disable certificate verification.

## Alarm SNS topic exception

The alarm topic `CaseInsights-Alarms-<uniqueId>` is **not encrypted at rest**. This is an explicit, user-approved exception to the encryption design and applies to this topic only.

**Why.** CloudWatch alarms can publish to an encrypted SNS topic only when the topic uses a customer managed KMS key whose key policy lets CloudWatch use it. The AWS managed key `alias/aws/sns` does not allow CloudWatch to publish, and the approved design uses no customer managed KMS keys. Encrypting the topic with the AWS managed key would silently stop every alarm notification, so the topic has no at-rest encryption.

**Controls that remain** (statements of the synthesised topic policy):

| Statement | Effect |
| --- | --- |
| `AllowPublishThroughSSLOnly` | Denies `sns:Publish` to every principal when `aws:SecureTransport` is false |
| `AllowCloudWatchAlarmsToPublish` | Allows only the service principal `cloudwatch.amazonaws.com` to call `sns:Publish`, with `aws:SourceArn` like `arn:aws:cloudwatch:<region>:<account-id>:alarm:CaseInsights-<uniqueId>-*` and `aws:SourceAccount` equal to the deployment account |

No other principal is granted access by the topic policy; principals in the account can still use the topic if their own IAM policies allow it. The only subscription the stack creates is one email subscription to `alarmEmail` when it is set (always in prod, never in the shipped dev configuration), and nothing is delivered until the recipient confirms it.

**What the notifications contain.** No case content: alarm names, descriptions and dimensions contain no case data. They do contain operational metadata: alarm names and descriptions, the old and new states and the state change reason, metric names, thresholds and values, the AWS account ID, the Region and resource names such as function and queue names. That metadata leaves the account through the confirmed email subscriptions and is then stored by the recipients' mail systems.

**cdk-nag acknowledgement.** The finding `AwsSolutions-SNS2` is acknowledged on `CaseInsightsCoreStack-<uniqueId>/Observability/AlarmTopic` only, with this reason:

> CloudWatch alarms can publish to an encrypted topic only through a customer managed KMS key, and the approved design uses no customer managed keys; notifications carry alarm names and states, never case content, and the topic enforces TLS.

**If your policy requires encryption of every topic.** This is an option, not something the repository implements: create a customer managed KMS key whose key policy allows `cloudwatch.amazonaws.com` to generate data keys and decrypt for this topic, set it as the topic's master key in `lib/constructs/observability.ts`, remove the `AwsSolutions-SNS2` acknowledgement, add the key's cost and management to your operations, and update this guide, [monitoring](monitoring.md) and [resources](resources.md). Then test that a test alarm (`aws cloudwatch set-alarm-state`) still delivers a notification.

## Identity and access

The full list of roles, statements and resource policies is in [permissions](permissions.md). The trust boundaries are:

| Boundary | Control |
| --- | --- |
| Management account to member accounts | The collector roles `CaseInsights-CaseRetrieval-<uniqueId>` and `CaseInsights-CaseAnnotation-<uniqueId>` may call `sts:AssumeRole` only on `arn:aws:iam::*:role/Support-Case-Analysis-Role-<uniqueId>` with `aws:ResourceOrgID` equal to `orgId`. The account is a wildcard because member accounts are discovered at run time. |
| Member account role | `Support-Case-Analysis-Role-<uniqueId>` trusts exactly those two role ARNs, with `aws:PrincipalOrgID` equal to `orgId` (statement `TrustCaseInsightsCollectors`). No `sts:ExternalId` is used, because the trusted principals are named roles of the same organisation, not a third party. The role holds only `support:DescribeCases` and `support:DescribeCommunications` (the Support API has no resource-level permissions), with a one-hour maximum session. It cannot create, update or resolve cases or add communications. If a collector role is deleted and recreated, IAM no longer matches the old trust principal, so the trust policy of every member role must be re-applied before collection works again. |
| Function roles | One role per function, scoped to the S3 prefixes, queues, state machine, workgroup and model it uses. Wildcards that remain are acknowledged one by one; see [cdk-nag](#cdk-nag). |
| Bedrock | Allows only the configured global inference profile and its foundation model through that profile, and explicitly denies `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on every other resource (`DenyUnapprovedModels`). `aws-marketplace:Subscribe` is limited to the model's product ID. |
| MCP endpoint | `AWS_IAM` (SigV4) authorisation on the only method, `POST /mcp`; unsigned requests receive `403` from API Gateway before reaching the function. The resource policy allows `execute-api:Invoke` on `/v1/POST/mcp` only to `mcpAllowedPrincipals` while `aws:PrincipalOrgID` equals `orgId`, and denies callers outside the organisation (`DenyOutsideOrganization`), principals not in the list by `aws:PrincipalArn` (`DenyUnlistedPrincipals`) and every other route (`DenyOtherRoutes`). The caller also needs `execute-api:Invoke` in its own identity policy. |
| MCP function | Its role, not the caller's, runs the tools: Athena only in workgroup `CaseInsights-<uniqueId>`, Glue reads on `case_insights_<uniqueId>`, read-only access to processed records, results under `query-results/`, and Bedrock only for the configured model. The workgroup, database and model are fixed in its environment; requests cannot choose them. |
| AWS WAF | Web ACL `CaseInsights-Mcp-<uniqueId>` on the `v1` stage: per-IP rate rule (`operations.mcp.wafRateLimitPer5Minutes`, default 300 per five minutes, `429`), a block of bodies over 32,768 bytes (`413`, oversize bodies match) and the AWS managed Core rule set. |
| Service principals | Scheduler roles trust `scheduler.amazonaws.com` with `aws:SourceAccount` and `aws:SourceArn` of the default schedule group; the state machine role trusts `states.amazonaws.com` with `aws:SourceAccount` and `aws:SourceArn`; the MCP function can be invoked by API Gateway only from `/v1/POST/mcp`. |

## Threat model

The table follows STRIDE (Spoofing, Tampering, Repudiation, Information disclosure, Denial of service, Elevation of privilege). "Residual" names what the controls do not cover.

| Threat | STRIDE | Controls | Residual |
| --- | --- | --- | --- |
| A compromised member account or a user tampers with case content (for example a crafted subject or conversation) | T, S | The member role is read-only and can be assumed only by the two collector roles of the same organisation. Case content is stored as data and is never interpreted as code, SQL or configuration. Oversized conversations fail explicitly instead of being truncated. | A member account administrator can write any text into its cases, and that text reaches Bedrock, the processed records and analysts. |
| Prompt injection through case text into the pipeline's Bedrock calls | T | The templates place the conversation inside `<case_annotation>` tags and the summary inside `<case_summary>` tags and tell the model to treat them as untrusted data and ignore instructions in them. Model output is treated as untrusted data: summaries must pass validation (600 to 650 characters, 4 to 6 plain-prose sentences, no headers or markdown), classifications must name an exact taxonomy label with a reason of at most 500 characters, one correction is attempted and then the category falls back to `Unclassified`. The request has no tool definitions, so the model cannot call tools, and nothing in its reply is executed. | An injected instruction can still bias a summary or classification within the validated format. Review outputs before relying on them. |
| Prompt injection through the MCP analyse tools (`analyze_with_bedrock`, `analyze_case_summaries`), including second-order injection from stored summaries | T, I | The system prompt marks content inside `<data>` or `<cases>` tags as data and tells the model to ignore instructions in it. Input sizes are bounded (question 2,000 characters, data 60,000 characters, rendered prompt 200,000 characters), output by `bedrockMaxTokens`. The tools are read-only and the model has no tools. The Kiro example has an empty `autoApprove`, so a person approves each tool call. | The answer is model text that may follow injected instructions; MCP clients that feed tool results to their own model inherit that risk. Treat answers as untrusted. |
| SQL injection or abuse through `query_athena` | T, I, E | `src/lambda/mcp_tools/sql_guard.py` tokenises the SQL (literals and quoted identifiers recognised with their escapes) and checks it structurally: exactly one statement with at most one trailing semicolon; no comments, `?` placeholders, backticks, non-ASCII characters outside literals, `U&` escapes or empty quoted identifiers; the statement is `SELECT` or `WITH` (optionally parenthesised), or one of `SHOW TABLES`, `SHOW COLUMNS`, `SHOW PARTITIONS` and `DESCRIBE` over the configured table or database; none of the forbidden keywords anywhere in a query (`ALTER`, `ANALYZE`, `CALL`, `COMMENT`, `COMMIT`, `COPY`, `CREATE`, `DEALLOCATE`, `DELETE`, `DENY`, `DESCRIBE`, `DROP`, `EXECUTE`, `EXPLAIN`, `EXPORT`, `EXTERNAL`, `FUNCTION`, `GRANT`, `IMPORT`, `INSERT`, `INTO`, `LAMBDA`, `LOAD`, `MERGE`, `MSCK`, `OPTIMIZE`, `PREPARE`, `REFRESH`, `RENAME`, `REPAIR`, `RESET`, `REVOKE`, `ROLLBACK`, `SAGEMAKER`, `SET`, `SHOW`, `START`, `TABLE`, `TRANSACTION`, `TRUNCATE`, `UNLOAD`, `UPDATE`, `USE`, `VACUUM`); no recursive CTEs and no qualified function calls; every relation in every `FROM` and `JOIN` (including subqueries, CTE bodies and comma joins) is `case_summary`, optionally qualified by the configured database and `AwsDataCatalog`, or a CTE in scope; at most 8,000 characters, 4,000 tokens and 32 levels of nesting. Rejections return a stable reason code and never echo the SQL. Fixed tools pass filters as Athena execution parameters, never as SQL text. Results are capped (`maxRows` 1 to 1,000, default 100; 256 KiB per response). The workgroup enforces its result location, SSE-S3 and `bytesScannedCutoffPerQuery` (default 10 GiB). The function role can read only the processed records and the one table, and write only query results. | An allowed principal can read every processed record, by design. |
| Unauthorised access to the MCP endpoint | S, I | SigV4 `AWS_IAM` authorisation (unsigned requests `403`), the organisation and principal-list resource policy, the caller's own `execute-api:Invoke` permission, WAF, and the `McpAuthFailures` alarm (10 refused requests in five minutes). The bridge never writes credentials or signatures to output, and API Gateway error bodies, which can echo the canonical request, are never copied. | A stolen credential of a listed principal grants the same access as that principal until it expires or is revoked. |
| Denial of service and cost exhaustion | D | MCP: reserved concurrency fixed at 20, stage throttling (default 10 requests per second, burst 20), WAF rate rule, 32 KiB body limit, 25-second request budget, row, byte and prompt limits, workgroup bytes-scanned cutoff. Pipeline: `Lambda-CaseAnalysisStages-<uniqueId>` has reserved concurrency `bedrockMaxConcurrency`, so concurrent Bedrock requests never exceed it; `bedrockMaxTokens` bounds output; retries are bounded; idempotency and processed-record checks prevent re-analysis of committed cases. | Listed MCP principals can still run up Athena and Bedrock charges within these limits; a large organisation with many cases incurs proportional Bedrock cost. Set an AWS Budgets alert ([cost](cost.md)). |
| Data exfiltration through CSV export, Athena results or MCP | I | All reads require IAM permissions in the management account; buckets block public access and deny non-TLS requests; Athena results expire after `resultsRetentionDays`; S3 server access logs record bucket access. | CSV files are written to the operator's machine, outside the solution's controls, and contain case content. Anyone who can read the results bucket can read results of other users' queries. Limit `s3:GetObject` on these buckets. |
| Log and telemetry leakage | I | Safe structured fields only, no event, response or error capture in traces, `IncludeExecutionData` false, payload-free API Gateway access log format, execution logging off, WAF sampled requests off, AWS error messages never logged. | Logs still contain account IDs, display IDs, S3 keys and, for MCP, source IPs. |
| Supply chain | T, E | npm dependencies are pinned exactly and installed with `npm ci` from `package-lock.json`; `npm run security:npm` scans the whole lockfile against the advisory database, with dated exceptions in `scripts/security/npm-advisory-exceptions.json`. Python Lambda dependencies are installed only from hash-locked CPython 3.12 manylinux x86_64 wheels (`--require-hashes --only-binary=:all:`); `npm run security:py` runs `pip-audit` on both locks and `bandit` on `src` and `scripts`. No Docker image or container build is used. | The first synthesis downloads wheels from the package index; a compromised package with a matching published hash is not detected by hashing alone. `npm run security` is not part of `npm run verify`; run it separately. |
| Pipeline compromise (optional pipeline) | T, E | Source through an existing, human-authorised AWS CodeConnections connection (no tokens or webhooks), `main` only, a mandatory manual approval `PromoteToProd` before prod, SSE-S3 artifact buckets that deny non-TLS. See [pipeline](pipeline.md). | Self-mutation means anyone who can push to `main` can change the pipeline and what deploys to dev; the approval is a release gate, not a security boundary. Protect `main` and limit `codepipeline:PutApprovalResult`. |
| Operational metadata exposure through alarm email | I | No case content in alarms; TLS-only publishing restricted to this deployment's alarms. | Account ID, Region, resource and alarm names leave the account by email; the topic is not encrypted at rest. |
| Repudiation of MCP queries | R | The MCP access log records request ID, time, status, source IP and caller account ID for every request; Athena and Bedrock calls appear in CloudTrail under the MCP function role if your trail records them. | The caller's role ARN is deliberately not logged, so individual MCP calls cannot be attributed to a principal from the solution's own logs. |
| Elevation through overly broad roles | E | Least-privilege roles per function with explicit denies for Bedrock; no `iam:*` actions in any runtime role; cdk-nag fails synthesis on unacknowledged findings. | The CDK bootstrap CloudFormation execution role has `AdministratorAccess` by default; narrow it if your policy requires ([permissions](permissions.md#cloudformation-execution-role)). |

## Amazon Bedrock and global cross-Region inference

- **Allowed models.** Synthesis accepts only `global.anthropic.claude-haiku-4-5-20251001-v1:0` (default) and `global.anthropic.claude-sonnet-4-5-20250929-v1:0`, and the Python layer refuses any other model ID. There is no fallback model. The IAM policy allows only the configured profile and denies every other model.
- **Global routing is approved.** A global cross-Region inference profile may route each request to any AWS commercial Region worldwide, not only the deployment Region. The request and response stay within AWS infrastructure. Stored application data (raw inputs, checkpoints, processed records, query results and logs) stays in the deployment account and Region. If your data residency policy does not allow worldwide inference, do not deploy with these profiles; the repository supports no Regional alternative.
- **Data use by Bedrock.** AWS documents that Amazon Bedrock does not store or log prompts and model responses and does not use them to train AWS or third-party models, or share them with model providers. Confirm the current terms in the Amazon Bedrock data protection documentation before deployment; this repository does not change that behaviour.
- **No external services.** No case content is sent to any service outside AWS, and the solution has no telemetry of its own.
- **Model access prerequisites.** Anthropic models can require the one-time first-time use (FTU) form and an AWS Marketplace subscription with a valid payment method. IAM cannot complete or waive either. The function roles hold `aws-marketplace:Subscribe` only for the configured model's product (`prod-xdkflymybwmvi` for Haiku 4.5, `prod-mxcfnwvpd6kb4` for Sonnet 4.5) and `aws-marketplace:ViewSubscriptions`, so the first invocation can complete the automatic subscription.
- **Model lifecycle.** Claude Sonnet 4.5 is marked Legacy by AWS with an end-of-life date of 8 April 2027. Changing to any other model requires a code change and review.

## Retry and idempotency limitations

- **No exactly-once inference.** Amazon Bedrock `InvokeModel` has no idempotency token. When an outcome is uncertain, for example a read timeout after the request was sent, a Lambda timeout after Bedrock answered but before the checkpoint was written, or a partial failure followed by a Step Functions retry, the same stage can invoke the model again. Such repeated invocations are rare and are billed. The solution does not promise exactly-once inference.
- **What limits repetition.** The Bedrock client retries throttling and transient errors at most 4 times, and Step Functions retries each task at most `operations.workflow.taskMaxAttempts` times (default 3). Before each retried call, a guard checks for a durable result and stops the retry if one exists. Stage checkpoints are reused when the case, model, prompt version and input digest match. SQS consumers use Powertools idempotency, stored in the DynamoDB table, which is used only for that purpose; SQS and Step Functions coordinate the work.
- **A committed processed record stops further automatic inference.** The Workflow Starter, every stage and case retrieval check for the processed record before doing work, and the record is written with a conditional create, so an existing record always wins. Redrives and duplicate deliveries never re-analyse a committed case.
- **The only exception is explicit reprocessing**, run from the repository root with credentials for the management account:

  ```bash
  .venv/bin/python scripts/reprocess-cases.py --env <env> --from <YYYY-MM-DD> --to <YYYY-MM-DD> --dry-run
  .venv/bin/python scripts/reprocess-cases.py --env <env> --from <YYYY-MM-DD> --to <YYYY-MM-DD> --confirm <plan-token>
  ```

  The dates are inclusive UTC days; `--date-field`, `--account-id`, `--case` and `--max-cases` narrow the scope. Each confirmed case causes new Bedrock invocations. See [Reprocess committed cases](runbook.md#reprocess-committed-cases).
- **MCP time budget.** Each `tools/call` works within a 25-second request budget inside the function's 28-second timeout and API Gateway's 29-second limit (`src/lambda/mcp_tools/limits.py`). SDK retries are off, and each Athena or Bedrock call gets a read timeout chosen to end before the deadline. botocore applies `read_timeout` to each socket read, not to the whole response, so the budget relies on the Athena and non-streaming `InvokeModel` APIs sending their response at once. A response that arrived slowly in many small reads could take longer than the budget; the Lambda timeout and API Gateway's limit (`504`) still end the request. The Bedrock tools may make a second attempt when a full minimum attempt still fits, so a timed-out first attempt can also be billed.

## cdk-nag

`lib/nag.ts` registers the cdk-nag `AwsSolutionsChecks` rule pack as a CDK validation plugin on every app, so it runs on every synthesis (`npm run synth`, `npx cdk synth`, `cdk diff` and `cdk deploy`). No other cdk-nag rule pack is used. Any unacknowledged finding fails synthesis; `test/app.test.ts` checks this with an insecure bucket. Acknowledgements are made on the narrowest construct, with finding-level IDs (for example `AwsSolutions-IAM5[Resource::...]`) and reasons of at least 40 characters. Wildcards in policies that aws-cdk-lib generates are acknowledged by exact finding ID, and an acknowledgement that no longer matches a finding fails synthesis. One aws-cdk-lib warning is acknowledged the same way.

The dev app (`-c env=dev`: Core, MemberRole, Analytics, MCP and the shared logging stack) has **81 acknowledgements**: 66 in Core, 4 in Analytics, 9 in MCP and 2 in the shared logging stack. `CaseInsightsMemberRoleStack-<uniqueId>` has none; cdk-nag evaluates the StackSet resource, not the role inside its template body, which `test/stacks/member-role-stack.test.ts` checks instead. The optional pipeline stack has its own acknowledgements in `lib/stacks/pipeline-stack.ts` and is not counted here.

| Finding | Count |
| --- | --- |
| `AwsSolutions-IAM5` (wildcard permissions) | 61 |
| `AwsSolutions-L1` (not the latest Lambda runtime) | 11 |
| `AwsSolutions-SQS3` (queue without a dead-letter queue) | 2 |
| `AwsSolutions-IAM4` (AWS managed policy) | 1 |
| `AwsSolutions-SNS2` (topic not encrypted at rest) | 1 |
| `AwsSolutions-GL1` (Glue crawler logs without a KMS key) | 1 |
| `AwsSolutions-APIG2` (no API Gateway request validation) | 1 |
| `AwsSolutions-APIG6` (no API Gateway execution logging) | 1 |
| `AwsSolutions-COG4` (no Cognito authoriser) | 1 |
| `@aws-cdk/aws-s3:accessLogsPolicyNotAdded` (aws-cdk-lib warning) | 1 |

The tables below list every acknowledgement of the dev app. Paths are relative to the stack; account, Region and namespace are replaced by placeholders, and logical IDs in finding IDs are shortened to the resource they name (for example `<raw bucket ARN>`). Reasons are quoted from the code.

### `CaseInsightsCoreStack-<uniqueId>` (66)

| # | Resource path | Finding | Reason |
| --- | --- | --- | --- |
| 1 | `Queues/WorkflowFailures` | `AwsSolutions-SQS3` | Terminal dead-letter destination written by the workflow failure handler after task retries are exhausted; it has no consumer, so a further DLQ would never receive messages. Operators redrive it explicitly. |
| 2 | `Queues/AccountDiscoveryFailures` | `AwsSolutions-SQS3` | Terminal dead-letter destination for the discovery schedule and the asynchronous account lookup and reader invocations after their retries; it has no consumer, so a further DLQ would never receive messages. |
| 3 | `MemberAccess/CaseRetrievalRoleMemberRoleAccess` | `AwsSolutions-IAM5[Resource::arn:aws:iam::*:role/Support-Case-Analysis-Role-<uniqueId>]` | Member account IDs are discovered at run time from AWS Organizations, so the account segment is a wildcard; the role name is fixed and aws:ResourceOrgID limits the target to accounts in the configured organization. |
| 4 | `MemberAccess/CaseAnnotationRoleMemberRoleAccess` | `AwsSolutions-IAM5[Resource::arn:aws:iam::*:role/Support-Case-Analysis-Role-<uniqueId>]` | Member account IDs are discovered at run time from AWS Organizations, so the account segment is a wildcard; the role name is fixed and aws:ResourceOrgID limits the target to accounts in the configured organization. |
| 5 | `AccountDiscovery/AccountLookup/TelemetryPolicy` | `AwsSolutions-IAM5[Resource::*]` | xray:PutTraceSegments and xray:PutTelemetryRecords do not support resource-level permissions; this policy holds only those two actions plus log writes scoped to the function log group. |
| 6 | `AccountDiscovery/AccountLookup/Function` | `AwsSolutions-L1` | Python 3.12 is the approved runtime: the shared layer is built from hash-locked CPython 3.12 (cp312) Linux x86_64 wheels, and changing runtime requires re-locking those wheels. |
| 7 | `AccountDiscovery/AccountLookupAccess` | `AwsSolutions-IAM5[Resource::*]` | organizations:ListAccounts does not support resource-level permissions; this statement grants only that read-only action in the management account. |
| 8 | `AccountDiscovery/AccountLookupAccess` | `AwsSolutions-IAM5[Resource::<account-list bucket ARN>/account-lists/*]` | Account lists are named by run ID at run time; s3:PutObject is limited to the account-lists/ prefix of the dedicated account-list bucket. |
| 9 | `AccountDiscovery/AccountReader/TelemetryPolicy` | `AwsSolutions-IAM5[Resource::*]` | xray:PutTraceSegments and xray:PutTelemetryRecords do not support resource-level permissions; this policy holds only those two actions plus log writes scoped to the function log group. |
| 10 | `AccountDiscovery/AccountReader/Function` | `AwsSolutions-L1` | Python 3.12 is the approved runtime: the shared layer is built from hash-locked CPython 3.12 (cp312) Linux x86_64 wheels, and changing runtime requires re-locking those wheels. |
| 11 | `AccountDiscovery/AccountReaderAccess` | `AwsSolutions-IAM5[Resource::<account-list bucket ARN>/account-lists/*]` | Account lists are named by run ID at run time; object reads are limited to the account-lists/ prefix of the dedicated account-list bucket. |
| 12 | `AccountDiscovery/AccountLookupScheduleRole` | `AwsSolutions-IAM5[Resource::<AccountLookup function ARN>:*]` | The CDK Lambda grant covers the account lookup function and its versions or aliases (:*); the role holds only lambda:InvokeFunction on that one function plus sending failed deliveries to the discovery failure queue. |
| 13 | `BucketNotificationsHandler<hash>/Role` | `AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]` | CDK-managed custom resource that applies the bucket notification configuration at deployment; the AWS managed basic execution policy only allows it to write its own CloudWatch Logs. |
| 14 | `CaseCollection/CaseRetrieval/TelemetryPolicy` | `AwsSolutions-IAM5[Resource::*]` | xray:PutTraceSegments and xray:PutTelemetryRecords do not support resource-level permissions; this policy holds only those two actions plus log writes scoped to the function log group. |
| 15 | `CaseCollection/CaseRetrieval/Function` | `AwsSolutions-L1` | Python 3.12 is the approved runtime: the shared layer is built from hash-locked CPython 3.12 (cp312) Linux x86_64 wheels, and changing runtime requires re-locking those wheels. |
| 16 | `CaseCollection/CaseRetrievalAccess` | `AwsSolutions-IAM5[Resource::<raw bucket ARN>/runs/*/accounts/*]` | Retrieval writes, and re-reads on retry, one run marker per account under runs/<runId>/accounts/; run and account IDs are discovered at run time and the markers hold identifiers only. |
| 17 | `CaseCollection/CaseRetrievalAccess` | `AwsSolutions-IAM5[Resource::<raw bucket ARN>/account_number=*]` | Raw case objects are keyed by member account and case display ID discovered at run time; s3:PutObject is limited to the account_number= partitions of the dedicated raw bucket. |
| 18 | `CaseCollection/CaseAnnotation/TelemetryPolicy` | `AwsSolutions-IAM5[Resource::*]` | xray:PutTraceSegments and xray:PutTelemetryRecords do not support resource-level permissions; this policy holds only those two actions plus log writes scoped to the function log group. |
| 19 | `CaseCollection/CaseAnnotation/Function` | `AwsSolutions-L1` | Python 3.12 is the approved runtime: the shared layer is built from hash-locked CPython 3.12 (cp312) Linux x86_64 wheels, and changing runtime requires re-locking those wheels. |
| 20 | `CaseCollection/CaseAnnotationAccess` | `AwsSolutions-IAM5[Resource::<raw bucket ARN>/runs/*/cases/*]` | Annotation writes a run marker for each case whose work ends at annotation, under runs/<runId>/cases/; run, account and case IDs are discovered at run time. |
| 21 | `CaseCollection/CaseAnnotationAccess` | `AwsSolutions-IAM5[Resource::<raw bucket ARN>/account_number=*/case_number=*/terminal-failure.json]` | Annotation reads only the terminal-failure.json marker of a case partition to avoid re-reporting unchanged oversized conversations; keys are discovered at run time. |
| 22 | `CaseCollection/CaseAnnotationAccess` | `AwsSolutions-IAM5[Resource::<raw bucket ARN>/account_number=*]` | Case conversations are keyed by member account and case display ID discovered at run time; s3:PutObject is limited to the account_number= partitions of the dedicated raw bucket. |
| 23 | `CaseAnalysis/AnalysisStages/TelemetryPolicy` | `AwsSolutions-IAM5[Resource::*]` | xray:PutTraceSegments and xray:PutTelemetryRecords do not support resource-level permissions; this policy holds only those two actions plus log writes scoped to the function log group. |
| 24 | `CaseAnalysis/AnalysisStages/Function` | `AwsSolutions-L1` | Python 3.12 is the approved runtime: the shared layer is built from hash-locked CPython 3.12 (cp312) Linux x86_64 wheels, and changing runtime requires re-locking those wheels. |
| 25 | `CaseAnalysis/AnalysisStagesAccess` | `AwsSolutions-IAM5[Resource::<raw bucket ARN>/account_number=*]` | Stages read the raw metadata and conversation of the case named in the workflow input; keys are account and case IDs discovered at run time. |
| 26 | `CaseAnalysis/AnalysisStagesAccess` | `AwsSolutions-IAM5[Resource::<raw bucket ARN>/account_number=*/case_number=*/checkpoints/*]` | Stage checkpoints are written only under each case partition checkpoints/ prefix; account and case IDs are discovered at run time. |
| 27 | `CaseAnalysis/AnalysisStagesAccess` | `AwsSolutions-IAM5[Resource::<processed bucket ARN>/account_number=*]` | Stages only read (HEAD) processed records to stop before inference once a case is committed; keys are account and case IDs discovered at run time. |
| 28 | `CaseAnalysis/AnalysisStagesBedrock/Policy` | `AwsSolutions-IAM5[Resource::*]` | AWS Marketplace Subscribe and ViewSubscriptions have no resource-level permissions. Subscribe is limited by aws-marketplace:ProductId to the approved model products; ViewSubscriptions supports no condition keys and only lists subscriptions. |
| 29 | `CaseAnalysis/UpdateCaseMetadata/TelemetryPolicy` | `AwsSolutions-IAM5[Resource::*]` | xray:PutTraceSegments and xray:PutTelemetryRecords do not support resource-level permissions; this policy holds only those two actions plus log writes scoped to the function log group. |
| 30 | `CaseAnalysis/UpdateCaseMetadata/Function` | `AwsSolutions-L1` | Python 3.12 is the approved runtime: the shared layer is built from hash-locked CPython 3.12 (cp312) Linux x86_64 wheels, and changing runtime requires re-locking those wheels. |
| 31 | `CaseAnalysis/UpdateCaseMetadataAccess` | `AwsSolutions-IAM5[Resource::<raw bucket ARN>/account_number=*]` | The update task reads raw metadata and stage checkpoints of the case named in the workflow input; keys are account and case IDs discovered at run time. |
| 32 | `CaseAnalysis/UpdateCaseMetadataAccess` | `AwsSolutions-IAM5[Resource::<processed bucket ARN>/account_number=*]` | Processed records are keyed by account and case ID discovered at run time; the task reads and conditionally creates only account_number= partition records. |
| 33 | `CaseAnalysis/HandleFailure/TelemetryPolicy` | `AwsSolutions-IAM5[Resource::*]` | xray:PutTraceSegments and xray:PutTelemetryRecords do not support resource-level permissions; this policy holds only those two actions plus log writes scoped to the function log group. |
| 34 | `CaseAnalysis/HandleFailure/Function` | `AwsSolutions-L1` | Python 3.12 is the approved runtime: the shared layer is built from hash-locked CPython 3.12 (cp312) Linux x86_64 wheels, and changing runtime requires re-locking those wheels. |
| 35 | `CaseAnalysis/HandleFailureAccess` | `AwsSolutions-IAM5[Resource::<raw bucket ARN>/account_number=*/case_number=*/failure-deliveries/*]` | The failure handler reads and confirms the failure-delivery marker of the failed execution only, under the case partition failure-deliveries/ prefix; keys are discovered at run time. |
| 36 | `CaseAnalysis/StateMachine/Role` | `AwsSolutions-IAM5[Resource::*]` | CloudWatch Logs delivery actions and X-Ray segment writes used by Step Functions logging and tracing do not support resource-level permissions; the role otherwise only invokes the workflow functions. |
| 37 | `CaseAnalysis/StateMachine/Role` | `AwsSolutions-IAM5[Resource::<AnalysisStages function ARN>:*]` | The CDK Lambda task grant covers this workflow function and its versions or aliases (:*); the state machine invokes only its own task functions. |
| 38 | `CaseAnalysis/StateMachine/Role` | `AwsSolutions-IAM5[Resource::<UpdateCaseMetadata function ARN>:*]` | The CDK Lambda task grant covers this workflow function and its versions or aliases (:*); the state machine invokes only its own task functions. |
| 39 | `CaseAnalysis/StateMachine/Role` | `AwsSolutions-IAM5[Resource::<HandleFailure function ARN>:*]` | The CDK Lambda task grant covers this workflow function and its versions or aliases (:*); the state machine invokes only its own task functions. |
| 40 | `CaseAnalysis/WorkflowStarter/TelemetryPolicy` | `AwsSolutions-IAM5[Resource::*]` | xray:PutTraceSegments and xray:PutTelemetryRecords do not support resource-level permissions; this policy holds only those two actions plus log writes scoped to the function log group. |
| 41 | `CaseAnalysis/WorkflowStarter/Function` | `AwsSolutions-L1` | Python 3.12 is the approved runtime: the shared layer is built from hash-locked CPython 3.12 (cp312) Linux x86_64 wheels, and changing runtime requires re-locking those wheels. |
| 42 | `CaseAnalysis/WorkflowStarterAccess` | `AwsSolutions-IAM5[Resource::<raw bucket ARN>/account_number=*/case_number=*/workflow.json]` | Ownership markers are one workflow.json per case partition, keyed by account and case IDs discovered at run time; only that file is read or written. |
| 43 | `CaseAnalysis/WorkflowStarterAccess` | `AwsSolutions-IAM5[Resource::<raw bucket ARN>/account_number=*/case_number=*/failure-deliveries/*]` | Before replacing the ownership marker of a closed failed execution the starter preserves a pending failure-delivery marker under the case failure-deliveries/ prefix; keys are discovered at run time. |
| 44 | `CaseAnalysis/WorkflowStarterAccess` | `AwsSolutions-IAM5[Resource::<raw bucket ARN>/runs/*/cases/*]` | The starter records which execution owns each case for the run under runs/<runId>/cases/; run, account and case IDs are discovered at run time. |
| 45 | `CaseAnalysis/WorkflowStarterAccess` | `AwsSolutions-IAM5[Resource::<processed bucket ARN>/account_number=*]` | The starter only reads (HEAD) processed records so completed cases are never started again; keys are account and case IDs discovered at run time. |
| 46 | `CaseAnalysis/WorkflowStarterAccess` | `AwsSolutions-IAM5[Resource::arn:aws:states:<region>:<account-id>:execution:CaseAnalysis-<uniqueId>:*]` | Execution names are derived per case attempt at run time; DescribeExecution is limited to executions of this deployment CaseAnalysis state machine. |
| 47 | `CaseAnalysis/CaseCleanup/TelemetryPolicy` | `AwsSolutions-IAM5[Resource::*]` | xray:PutTraceSegments and xray:PutTelemetryRecords do not support resource-level permissions; this policy holds only those two actions plus log writes scoped to the function log group. |
| 48 | `CaseAnalysis/CaseCleanup/Function` | `AwsSolutions-L1` | Python 3.12 is the approved runtime: the shared layer is built from hash-locked CPython 3.12 (cp312) Linux x86_64 wheels, and changing runtime requires re-locking those wheels. |
| 49 | `CaseAnalysis/CaseCleanupAccess` | `AwsSolutions-IAM5[Resource::<raw bucket ARN>/account_number=*]` | Cleanup reads ownership markers of case partitions discovered by listing the raw bucket; it never reads processed output from the raw bucket. |
| 50 | `CaseAnalysis/CaseCleanupAccess` | `AwsSolutions-IAM5[Resource::<raw bucket ARN>/account_number=*/case_number=*/data.json]` | Cleanup may delete only data.json, communications.json and workflow.json of abandoned cases (conditional on the checked ETag), and may write only its reset claim to workflow.json (conditional on the judged marker) before doing so; checkpoints and terminal markers are never deletable. |
| 51 | `CaseAnalysis/CaseCleanupAccess` | `AwsSolutions-IAM5[Resource::<raw bucket ARN>/account_number=*/case_number=*/communications.json]` | Cleanup may delete only data.json, communications.json and workflow.json of abandoned cases (conditional on the checked ETag), and may write only its reset claim to workflow.json (conditional on the judged marker) before doing so; checkpoints and terminal markers are never deletable. |
| 52 | `CaseAnalysis/CaseCleanupAccess` | `AwsSolutions-IAM5[Resource::<raw bucket ARN>/account_number=*/case_number=*/workflow.json]` | Cleanup may delete only data.json, communications.json and workflow.json of abandoned cases (conditional on the checked ETag), and may write only its reset claim to workflow.json (conditional on the judged marker) before doing so; checkpoints and terminal markers are never deletable. |
| 53 | `CaseAnalysis/CaseCleanupAccess` | `AwsSolutions-IAM5[Resource::<raw bucket ARN>/account_number=*/case_number=*/failure-deliveries/*]` | Cleanup preserves and delivers failure-delivery markers of closed failed executions under the case failure-deliveries/ prefix; it can write but never delete them. |
| 54 | `CaseAnalysis/CaseCleanupAccess` | `AwsSolutions-IAM5[Resource::<raw bucket ARN>/cleanup-state/*]` | The cleanup resume cursor lives under the cleanup-state/ prefix of the raw bucket; its name is fixed but the policy keeps the prefix for future cursor versions. |
| 55 | `CaseAnalysis/CaseCleanupAccess` | `AwsSolutions-IAM5[Resource::<processed bucket ARN>/account_number=*]` | Cleanup only lists and reads (HEAD) processed records so committed cases are never reset; keys are account and case IDs discovered at run time. |
| 56 | `CaseAnalysis/CaseCleanupAccess` | `AwsSolutions-IAM5[Resource::arn:aws:states:<region>:<account-id>:execution:CaseAnalysis-<uniqueId>:*]` | Cleanup describes the execution named by a case ownership marker; DescribeExecution is limited to executions of this deployment CaseAnalysis state machine. |
| 57 | `CaseAnalysis/CaseCleanupScheduleRole` | `AwsSolutions-IAM5[Resource::<CaseCleanup function ARN>:*]` | The CDK Lambda grant covers the case cleanup function and its versions or aliases (:*); the role holds only lambda:InvokeFunction on that one function. |
| 58 | `Observability/AlarmTopic` | `AwsSolutions-SNS2` | CloudWatch alarms can publish to an encrypted topic only through a customer managed KMS key, and the approved design uses no customer managed keys; notifications carry alarm names and states, never case content, and the topic enforces TLS. |
| 59 | `Observability/RunCompletion/TelemetryPolicy` | `AwsSolutions-IAM5[Resource::*]` | xray:PutTraceSegments and xray:PutTelemetryRecords do not support resource-level permissions; this policy holds only those two actions plus log writes scoped to the function log group. |
| 60 | `Observability/RunCompletion/Function` | `AwsSolutions-L1` | Python 3.12 is the approved runtime: the shared layer is built from hash-locked CPython 3.12 (cp312) Linux x86_64 wheels, and changing runtime requires re-locking those wheels. |
| 61 | `Observability/RunCompletionAccess` | `AwsSolutions-IAM5[Resource::<account-list bucket ARN>/account-lists/*]` | Run Completion reads account lists named by run ID at run time; reads are limited to the account-lists/ prefix of the dedicated account-list bucket. |
| 62 | `Observability/RunCompletionAccess` | `AwsSolutions-IAM5[Resource::<raw bucket ARN>/runs/*]` | Run Completion reads run markers under the runs/ prefix of the raw bucket; run, account and case IDs in their keys are discovered at run time. |
| 63 | `Observability/RunCompletionAccess` | `AwsSolutions-IAM5[Resource::<raw bucket ARN>/runs/*/completion.json]` | Run Completion writes only the completion.json marker of each run (with IfNoneMatch); run IDs are discovered at run time. |
| 64 | `Observability/RunCompletionAccess` | `AwsSolutions-IAM5[Resource::<raw bucket ARN>/account_number=*/case_number=*/failure-deliveries/*]` | Run Completion preserves and delivers failure-delivery markers of closed failed executions under each case failure-deliveries/ prefix; it cannot delete them. |
| 65 | `Observability/RunCompletionAccess` | `AwsSolutions-IAM5[Resource::arn:aws:states:<region>:<account-id>:execution:CaseAnalysis-<uniqueId>:*]` | Run Completion describes the executions named by run case markers; DescribeExecution is limited to executions of this deployment CaseAnalysis state machine. |
| 66 | `Observability/RunCompletionScheduleRole` | `AwsSolutions-IAM5[Resource::<RunCompletion function ARN>:*]` | The CDK Lambda grant covers the run completion function and its versions or aliases (:*); the role holds only lambda:InvokeFunction on that one function. |

### `CaseInsightsAnalyticsStack-<uniqueId>` (4)

| # | Resource path | Finding | Reason |
| --- | --- | --- | --- |
| 1 | `Analytics/CrawlerAccess` | `AwsSolutions-IAM5[Resource::<processed bucket ARN (Core output)>/account_number=*]` | The crawler reads processed records whose account and case partitions are created at run time; it has no write or delete access. |
| 2 | `Analytics/CrawlerAccess` | `AwsSolutions-IAM5[Resource::arn:aws:logs:<region>:<account-id>:log-group:/aws-glue/crawlers:log-stream:*]` | Glue names crawler log streams itself at run time; access is limited to streams of the fixed /aws-glue/crawlers log group. |
| 3 | `Analytics/PartitionCrawler` | `AwsSolutions-GL1` | Glue CloudWatch log encryption needs a customer-managed KMS key, which the approved AWS-managed encryption design excludes; crawler logs hold only object paths and partition names under CloudWatch default encryption. |
| 4 | `Analytics/ResultsBucket` | `Annotation::@aws-cdk/aws-s3:accessLogsPolicyNotAdded` | Log delivery for the results bucket is granted on the Core access-log bucket policy (statement AllowAthenaResultsAccessLogs), scoped to the athena-results/ prefix and this account. |

### `CaseInsightsMcpStack-<uniqueId>` (9)

| # | Resource path | Finding | Reason |
| --- | --- | --- | --- |
| 1 | `McpServer/McpFunction/TelemetryPolicy` | `AwsSolutions-IAM5[Resource::*]` | xray:PutTraceSegments and xray:PutTelemetryRecords do not support resource-level permissions; this policy holds only those two actions plus log writes scoped to the function log group. |
| 2 | `McpServer/McpFunction/Function` | `AwsSolutions-L1` | Python 3.12 is the approved runtime: the shared layer is built from hash-locked CPython 3.12 (cp312) Linux x86_64 wheels, and changing runtime requires re-locking those wheels. |
| 3 | `McpServer/McpServerAccess` | `AwsSolutions-IAM5[Resource::<processed bucket ARN (Core output)>/account_number=*]` | Athena reads processed records with the caller credentials; account and case partitions are created at run time, and the role cannot write or delete them. |
| 4 | `McpServer/McpServerAccess` | `AwsSolutions-IAM5[Resource::<results bucket ARN (Analytics output)>/query-results/*]` | Athena writes and reads query results under the enforced query-results/ prefix of the dedicated results bucket with the caller credentials; result object names are generated per query. |
| 5 | `McpServer/McpServerAccess` | `AwsSolutions-IAM5[Resource::arn:aws:glue:<region>:<account-id>:table/case_insights_<uniqueId>/*]` | SHOW TABLES of the configured database needs glue:GetTables over its tables; the statement holds only that read action and the database contains only case_summary. |
| 6 | `McpServer/Bedrock/Policy` | `AwsSolutions-IAM5[Resource::*]` | AWS Marketplace Subscribe and ViewSubscriptions have no resource-level permissions. Subscribe is limited by aws-marketplace:ProductId to the approved model products; ViewSubscriptions supports no condition keys and only lists subscriptions. |
| 7 | `McpServer/Api` | `AwsSolutions-APIG2` | The MCP function validates the JSON-RPC envelope, content type, 32 KiB body limit and every tool argument schema; API Gateway draft-04 models cannot express these rules and would replace JSON-RPC error replies. |
| 8 | `McpServer/Api/DeploymentStage.v1` | `AwsSolutions-APIG6` | Execution logs can contain request and response data and would add per-method log groups; each request is recorded in the payload-free JSON access log, which uses the shared account/Region CloudWatch role, and traced with X-Ray instead. |
| 9 | `McpServer/Api/Default/mcp/POST` | `AwsSolutions-COG4` | The approved authentication is SigV4 with AWS_IAM authorisation plus a resource policy limited to configured principals in the Organization; no Cognito user pool is used. |

### `CaseInsightsApiLoggingStack-<sharedId>` (2)

| # | Resource path | Finding | Reason |
| --- | --- | --- | --- |
| 1 | `ApiGatewayLogging/CloudWatchRole` | `AwsSolutions-IAM5[Resource::arn:aws:logs:<region>:<account-id>:log-group:/aws/apigateway/CaseInsights-Mcp-*]` | API Gateway writes REST access logs only to the Support Case Insights MCP access log groups of every environment namespace in this account and Region; their streams are created by API Gateway at run time. |
| 2 | `ApiGatewayLogging/CloudWatchRole` | `AwsSolutions-IAM5[Resource::arn:aws:logs:<region>:<account-id>:log-group:*]` | logs:DescribeLogGroups is a list operation that cannot be limited to named log groups; API Gateway uses it to find access log destinations, and it is limited to this account and Region and grants no read or write access. |


## Security verification status

### Local checks (run by `npm run verify`)

| Check | What it covers |
| --- | --- |
| cdk-nag on the dev and prod synth | Every AwsSolutions rule; unacknowledged findings fail |
| `test/app.test.ts` | Every acknowledgement listed with a specific reason; cdk-nag failure stops synthesis; scheduler-trusted roles use the schedule-group condition |
| `test/constructs/core-storage.test.ts` | Every bucket private, SSE-S3, TLS 1.2 only, versioned and bucket-owner enforced; access logging without self-logging; lifecycle rules; prod retention and deletion protection; idempotency table with service-owned encryption |
| `test/constructs/observability.test.ts` | Alarm topic TLS enforcement, CloudWatch-only publishing and subscriptions; payload-free telemetry settings on every function and the workflow; every alarm notifies the topic |
| `test/constructs/bedrock-invoke-policy.test.ts`, `account-discovery.test.ts`, `case-collection.test.ts`, `case-analysis.test.ts`, `pipeline-queues.test.ts` | Exact Bedrock allow and deny statements and product IDs; each role holds only the access its function uses; no management-account Support permissions; every queue denies requests without TLS and has its dead-letter settings |
| `test/stacks/member-role-stack.test.ts` | Member role trust (two role ARNs and `aws:PrincipalOrgID`) and read-only Support permissions |
| `test/stacks/mcp-stack.test.ts` | `AWS_IAM` only on `POST /mcp`, no CORS, resource policy, route-scoped invoke permission, WAF rules, payload-free access logs, reserved concurrency 20, role scope |
| `test/stacks/api-logging-stack.test.ts`, `analytics-stack.test.ts`, `pipeline-stack.test.ts` | Logging role scope and retention; workgroup enforcement and SSE_S3 results; pipeline approval and buckets |
| `test/lambda/mcp/test_sql_guard.py`, `test_query_athena.py`, `test_time_budget.py`, `test_bedrock_tools.py` | SQL guard acceptance and rejection, result limits, time budget and Bedrock tool behaviour |
| `test/lambda/test_support_read_only.py`, `test/lambda/test_handler_tracing.py` and the handler tests | No Lambda source references a Support write operation; emitted logs, metrics and traces contain no case content under the deployed capture settings |
| `test/scripts/test_mcp_aws_client.py` | Bridge SigV4 signing against a verifying loopback server, credential hygiene in stdout and stderr |

Separately, `npm run security` runs the npm advisory scan, `pip-audit` and `bandit`. Synthesised-template assertions show what CloudFormation will be asked to create; they do not prove how API Gateway, WAF, IAM or SNS behave in a live account.

### Deferred live AWS security checklist (not yet run)

None of these checks has been run, because nothing has been deployed:

- [ ] An unsigned request to `https://<api-id>.execute-api.<region>.amazonaws.com/v1/mcp` returns `403`.
- [ ] A signed request from a principal not in `mcpAllowedPrincipals`, or from outside the organisation, returns `403`; a listed principal's `tools/list` succeeds and shows eight tools.
- [ ] `query_athena` rejects a non-`SELECT` statement (for example `DROP TABLE case_summary`) with `INVALID_ARGUMENTS`, and a stacked statement is rejected.
- [ ] A 32,769-byte body returns `413` from WAF and requests above the per-IP rate return `429`.
- [ ] Requests to each bucket and to the alarm topic without TLS are denied.
- [ ] `aws cloudformation list-stack-instances --stack-set-name CaseInsights-MemberRole-<uniqueId> --call-as SELF` shows every instance `CURRENT`, and the collector roles can assume `Support-Case-Analysis-Role-<uniqueId>` while a role outside the organisation cannot.
- [ ] The prod alarm email subscription is confirmed and a test notification arrives.
- [ ] Bedrock invocation succeeds through the global profile and invoking any other model is denied.
- [ ] Log groups, Step Functions logs and MCP access logs contain no case content after a real run.
- [ ] The DynamoDB table, queues and buckets show the expected encryption settings in the console or CLI.

## Reporting security issues

Do not open a public GitHub issue, pull request or discussion for a suspected vulnerability, and never include case data, credentials or account details in a report. Report it privately to the repository owner through GitHub's private vulnerability reporting ("Report a vulnerability" on the repository's **Security** tab, which creates a private security advisory). If that option is not available, contact the repository owner privately and ask for a private channel. General contribution guidance is in [CONTRIBUTING](../CONTRIBUTING.md).

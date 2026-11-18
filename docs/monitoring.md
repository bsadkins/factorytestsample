# Monitoring

This guide describes how a Support Case Insights deployment is monitored: the custom and AWS metrics each component publishes, the CloudWatch alarms the core stack creates and how they map to the [alarm runbook](runbook.md), the dashboard and its widgets, the daily-completion heartbeat, X-Ray tracing, and the SNS topic that delivers alarm notifications. It applies to every environment (`-c env=<env>`); names use the `<uniqueId>` of `config/<env>.json`. Everything here is derived from the CDK code and the Lambda source in this repository. Nothing has been deployed, so the live behaviour of the alarms, dashboard and notifications described here is unverified until a real deployment (see [Live verification status](#live-verification-status)). For log groups, log format and example queries see [Logging](logging.md); for symptom-led diagnosis see [Troubleshooting](troubleshooting.md).

## Overview

| Item | Name | Created by |
| --- | --- | --- |
| Metrics namespace | `SupportCaseInsights` | Lambda Powertools in every function (`POWERTOOLS_METRICS_NAMESPACE`) and one log metric filter |
| Dashboard | `CaseInsights-<uniqueId>` | Core stack |
| Alarms | `CaseInsights-<uniqueId>-<purpose>` (23 without MCP, 27 with MCP) | Core stack |
| Alarm topic | `CaseInsights-Alarms-<uniqueId>` | Core stack |
| Heartbeat schedule | `CaseInsights-RunCompletion-<uniqueId>` (`rate(1 hour)`) | Core stack |
| Tracing | AWS X-Ray, active on every function, the state machine and the MCP API stage | Core and MCP stacks |

All alarms live in the core stack, including the four MCP alarms, which refer to the MCP function, API and access log group by name. The MCP alarms and dashboard rows exist only when `enableMcp` is true (it is true in `config/dev.json` and false in `config/prod.json` as shipped). See [Architecture](architecture.md) for how the components fit together and [Configuration](configuration.md) for the settings named below.

## Metrics design

- **Counts and durations only.** Metrics never carry account IDs, case IDs, display IDs, run IDs, subjects or any case content. Identifiers appear only in logs (see [Logging](logging.md)).
- **One bounded dimension.** Every custom metric has exactly one dimension, `service`, whose value is the Powertools service name of the emitting function. There are eleven possible values, listed in the next section. The one exception is `McpAuthFailures`, which has the single dimension `ApiName` with the fixed value `CaseInsights-Mcp-<uniqueId>`.
- **AWS metrics use fixed dimensions.** AWS-published metrics are read with dimensions that name a resource of this deployment (`FunctionName`, `StateMachineArn`, `QueueName`, `ApiName` and `Stage`, `WebACL`, `Region` and `Rule`). No dimension value is derived from data.
- **Cardinality is constant.** Because no dimension depends on accounts, cases or runs, the number of distinct metric streams is fixed by the code and does not grow with the organisation. See [Cost](cost.md) for what this means for CloudWatch charges.
- **Emitted with Embedded Metric Format.** Functions write metrics into their log stream with Powertools, so CloudWatch extracts them from the logs. A metric that a function did not emit in a period has no data point; the alarms state how missing data is treated.
- **Units.** Every custom metric is `Count` except `BedrockLatency`, `McpToolDuration` (both `Milliseconds`) and `RunDuration` (`Seconds`).

## Custom metrics by service

Each table lists the metrics a service publishes, in the `SupportCaseInsights` namespace with dimension `service=<service>`. "When" says when the data point is written; a metric whose value is 1 counts one event.

### account-lookup (`Lambda-AccountLookup-<uniqueId>`)

| Metric | When |
| --- | --- |
| `AccountsListed` | A new account list is written: accounts returned by Organizations |
| `AccountsDiscovered` | A new account list is written: active member accounts selected for the run |
| `AccountsExcludedInactive` | A new account list is written: accounts excluded because they are not active |
| `AccountsExcludedManagement` | A new account list is written: the management account, which is excluded |
| `AccountListsWritten` | A new account list is written (value 1) |
| `DuplicateDiscoveryInvocations` | The account list for this run already existed, so nothing was written |

### account-reader (`Lambda-AccountReader-<uniqueId>`)

| Metric | When |
| --- | --- |
| `AccountListsProcessed` | One or more account lists were processed in the invocation |
| `AccountsEnqueued` | One or more account lists were processed: account references sent to the active-accounts queue |
| `DuplicateAccountListEvents` | Non-zero only: duplicate S3 notifications for a list already handled |
| `UnrelatedObjectEventsIgnored` | Non-zero only: notifications for objects that are not account lists |
| `InvalidAccountLists` | Non-zero only: lists that did not match the contract |
| `AccountListFailures` | Non-zero only: lists that could not be processed |

### case-retrieval (`Lambda-CaseRetrieval-<uniqueId>`)

| Metric | When |
| --- | --- |
| `AccountsProcessed` | An account's cases were retrieved (value 1) |
| `CasesListed` | Per account retrieved: cases returned by the Support API |
| `CasesExcludedUnresolved` | Per account retrieved: cases skipped because they are not resolved |
| `CasesExcludedOutsideWindow` | Per account retrieved: cases created outside the collection window |
| `CasesInvalid` | Per account retrieved: cases with unusable fields |
| `CasesAlreadyProcessed` | Per account retrieved: cases that already have a processed record |
| `CasesRetrieved` | Per account retrieved: cases stored and sent for annotation |
| `CasesRedispatched` | Cases recorded in an earlier attempt of the same run that were sent for annotation again |
| `AccountsSkipped` | An account was skipped for this run (value 1) |
| `AccountsSkippedAccessDenied` | The skip reason was `AccessDenied` |
| `AccountsSkippedSubscriptionRequired` | The skip reason was `SubscriptionRequired` (no Business, Enterprise On-Ramp or Enterprise Support plan) |
| `InvalidMessages` | A queue message was malformed (also counted in `CaseRetrievalFailures`) |
| `CaseRetrievalFailures` | A message failed and will be retried by SQS |
| `DuplicateAccountMessages` | A duplicate delivery of an account message was skipped |

### case-annotation (`Lambda-CaseAnnotation-<uniqueId>`)

| Metric | When |
| --- | --- |
| `CasesAnnotated` | The conversation was stored and the case was sent to the summary queue |
| `CommunicationsRetrieved` | With `CasesAnnotated`: number of communications retrieved |
| `CasesAlreadyProcessed` | The case already has a processed record |
| `CasesNotFound` | The Support API no longer returns the case |
| `OversizedConversations` | The conversation exceeds the summary input limit; it is stored and reported as a failure record |
| `OversizedConversationsUnchanged` | A conversation already reported as too large is unchanged and is not reported again |
| `CasesSkippedAccessDenied` | The member role was refused for this case's account |
| `CasesSkippedSubscriptionRequired` | The account has no qualifying Support plan |
| `InvalidMessages` | A queue message was malformed (also counted in `CaseAnnotationFailures`) |
| `CaseAnnotationFailures` | A message failed and will be retried by SQS |
| `DuplicateAnnotationMessages` | A duplicate delivery was skipped |

### workflow-starter (`Lambda-WorkflowStarter-<uniqueId>`)

| Metric | When |
| --- | --- |
| `WorkflowsStarted` | A `CaseAnalysis-<uniqueId>` execution was started |
| `WorkflowsAlreadyActive` | An execution for the case is already running |
| `CasesAlreadyProcessed` | The case already has a processed record |
| `FailedAttemptsRecovered` | A closed failed attempt was recovered under the next attempt name |
| `FailureDeliveriesPreserved` | A pending failure-delivery marker was kept for reconciliation |
| `StaleResetClaimsTakenOver` | A Case Cleanup reset claim older than an hour was taken over |
| `OwnershipClaimsCompleted` | An interrupted ownership claim was completed |
| `ExecutionNameConflicts` | `StartExecution` reported that the execution name already exists |
| `InvalidMessages` | A queue message was malformed (also counted in `WorkflowStartFailures`) |
| `WorkflowStartFailures` | A message failed and will be retried by SQS |
| `DuplicateSummaryMessages` | A duplicate delivery was skipped |

### case-analysis-stages (`Lambda-CaseAnalysisStages-<uniqueId>`)

This function runs the summary, root cause analysis (RCA) and lifecycle stages of the workflow.

| Metric | When |
| --- | --- |
| `BedrockInvocations` | A Bedrock `InvokeModel` call completed |
| `BedrockLatency` | Milliseconds of a completed invocation, including retries |
| `BedrockInputTokens` | Input tokens of a completed invocation |
| `BedrockOutputTokens` | Output tokens of a completed invocation |
| `BedrockThrottles` | Throttled attempts within one invocation (written on success, failure and when a retry is superseded) |
| `BedrockInvocationFailures` | An invocation failed after its retries |
| `StagesSkippedAlreadyProcessed` | The case already has a processed record, so the stage did nothing |
| `CheckpointsReused` | A valid stage checkpoint from an earlier attempt was reused instead of calling Bedrock |
| `OversizedConversations` | Summary stage: the conversation exceeds the summary input limit |
| `SummaryCorrections` | Summary stage: the first reply failed validation and one correction was requested |
| `SummariesGenerated` | Summary stage: a valid summary was stored |
| `InvalidSummaries` | Summary stage: a reply failed validation |
| `SummaryValidationFailures` | Summary stage: the reply was still invalid after the correction; the stage failed |
| `ClassificationCorrections` | RCA or lifecycle stage: one correction was requested |
| `InvalidClassifications` | RCA or lifecycle stage: a reply failed validation |
| `ClassificationFallbacks` | RCA or lifecycle stage: recorded as `Unclassified` after the correction |
| `CasesClassified` | RCA or lifecycle stage: a classification was stored |

### update-case-metadata (`Lambda-UpdateCaseMetadata-<uniqueId>`)

| Metric | When |
| --- | --- |
| `StagesSkippedAlreadyProcessed` | A processed record already exists |
| `IncompleteRecords` | A checkpoint was missing or invalid, so nothing was committed |
| `DuplicateCommitsPrevented` | A processed record was committed concurrently and was kept |
| `CasesProcessed` | A processed record was committed |
| `UnclassifiedFields` | With `CasesProcessed`: how many of the two classifications are `Unclassified` (0 to 2) |

### handle-failure (`Lambda-HandleFailure-<uniqueId>`)

| Metric | When |
| --- | --- |
| `WorkflowFailuresRecorded` | A failure record was sent to the workflow failure queue |
| `FailureDeliveriesAlreadyRecorded` | A failure record for this execution was already confirmed and was not sent again |
| `WorkflowFailureDeliveryErrors` | The failure record could not be sent; Step Functions retries the task |
| `FailureDeliveryMarkerErrors` | The failure-delivery marker could not be read or confirmed |
| `OversizedConversations` | The failure type is `ConversationTooLarge` |

### case-cleanup (`Lambda-CaseCleanup-<uniqueId>`)

| Metric | When |
| --- | --- |
| `CleanupCasesEvaluated` | Every run: cases examined |
| `CleanupCasesReset` | Every run: abandoned cases reset so a later run collects them again |
| `CleanupErrors` | Every run: cases that could not be checked or reset (also 1 when the whole invocation fails) |
| `CleanupObjectsDeleted` | Raw objects deleted while resetting cases |
| `CleanupSkippedProcessed`, `CleanupSkippedRecent`, `CleanupSkippedActive`, `CleanupSkippedUnknown` | Cases left untouched because they are committed, newer than the grace period, have active work, or have an unknown status |
| `CleanupDeleteConflicts` | A conditional delete found the object had changed, so it was kept |
| `CleanupSkippedNothingToReset` | A case had nothing left to reset |
| `CleanupIncomplete` | The run stopped at its time budget; the next run resumes from its saved position |
| `FailureDeliveriesSent` | Non-zero only: failure records sent by reconciliation |
| `FailureDeliveriesUnconfirmed` | Non-zero only: records sent whose marker could not be confirmed |
| `FailureDeliveriesPreserved` | Non-zero only: pending markers kept for a later run |
| `FailureDeliveryErrors` | Non-zero only: records that could not be sent |

### run-completion (`Lambda-RunCompletion-<uniqueId>`)

| Metric | When |
| --- | --- |
| `DailyRunCompleted` | Every invocation: runs that completed in this invocation (often 0) |
| `RunsIncomplete` | Every invocation: runs checked that are not complete yet |
| `RunCasesPending` | Every invocation: cases still pending across those runs |
| `RunCompletionErrors` | Every invocation: markers, executions or deliveries that could not be checked (also 1 when the whole invocation fails) |
| `RunAccountsRetrieved`, `RunAccountsSkipped` | Per completed run: accounts retrieved and skipped |
| `RunCasesProcessed`, `RunCasesSkipped`, `RunCasesFailed` | Per completed run: case outcomes |
| `RunDuration` | Per completed run: seconds from the account list's `generatedAt` to completion |
| `FailureDeliveriesSent`, `FailureDeliveriesUnconfirmed`, `FailureDeliveriesPreserved`, `FailureDeliveryErrors` | Non-zero only: failure-record reconciliation, as for case-cleanup |

### mcp-server (`Lambda-McpServer-<uniqueId>`, MCP only)

| Metric | When |
| --- | --- |
| `McpRequests` | Every request that reaches the function |
| `McpToolCalls` | A `tools/call` request that named a tool and ended `ok`, `tool_error` or `internal_error` |
| `McpToolDuration` | Milliseconds spent in the tool for those calls |
| `McpProtocolErrors` | The request was not valid JSON-RPC or MCP |
| `McpToolErrors` | The tool rejected its arguments or reported a tool error |
| `McpInternalErrors` | An unexpected failure answered with JSON-RPC `-32603` |
| `McpErrors` | Any of the three error kinds above |

### Access-log metric filter (MCP only)

| Metric | Dimension | Source |
| --- | --- | --- |
| `McpAuthFailures` | `ApiName=CaseInsights-Mcp-<uniqueId>` | A metric filter on `/aws/apigateway/CaseInsights-Mcp-<uniqueId>/access` that counts 1 for each entry whose `errorType` is `ACCESS_DENIED`, `EXPIRED_TOKEN`, `INVALID_SIGNATURE`, `MISSING_AUTHENTICATION_TOKEN` or `UNAUTHORIZED` |

## AWS metrics used

| Namespace | Metric | Statistic | Dimension | Used by |
| --- | --- | --- | --- | --- |
| `AWS/Lambda` | `Errors` | Sum | `FunctionName` | `LambdaErrors-*` alarms, *Lambda errors* and *MCP requests and errors* widgets |
| `AWS/States` | `ExecutionsStarted`, `ExecutionsSucceeded`, `ExecutionsFailed`, `ExecutionsTimedOut`, `ExecutionsAborted` | Sum | `StateMachineArn` of `CaseAnalysis-<uniqueId>` | Workflow alarms, success-rate and executions widgets |
| `AWS/SQS` | `ApproximateNumberOfMessagesVisible` | Maximum | `QueueName` | `QueueMessages-*` alarms and the queue-depth widget |
| `AWS/ApiGateway` | `Count`, `4XXError`, `5XXError` | Sum | `ApiName=CaseInsights-Mcp-<uniqueId>`, `Stage=v1` | `McpApiServerErrors` alarm and *MCP API responses (stage v1)* widget |
| `AWS/WAFV2` | `BlockedRequests` | Sum | `WebACL=CaseInsights-Mcp-<uniqueId>`, `Region`, `Rule` (`ALL`, `RateLimitPerIp`, `BlockBodyOver32KiB`, `AWSManagedRulesCommonRuleSet`) | *MCP WAF blocked requests* widget |

API Gateway `Latency` and `IntegrationLatency` are published by AWS for the stage but are not on the dashboard or alarmed; per-request latency is in the access log (`responseLatency`, `integrationLatency`). Athena publishes query metrics in `AWS/Athena` for the workgroup `CaseInsights-<uniqueId>` (see [Athena analytics](athena.md#workgroup-and-query-results)); none of them is alarmed.

## Monitoring requirements and where they are met

| What an operator needs to know | Metrics | Alarm | Dashboard widget |
| --- | --- | --- | --- |
| A daily run finished end to end | `DailyRunCompleted` | `MissingDailyRunCompletion` | *Daily runs completed (heartbeat)*, *Run completion* |
| A function is crashing, timing out or out of memory | `AWS/Lambda Errors` | `LambdaErrors-<function>` | *Lambda errors* |
| Case analyses fail, time out or are stopped | `AWS/States Executions*` | `WorkflowExecutionsFailed`, `WorkflowExecutionsTimedOut`, `WorkflowExecutionsAborted` | *Workflow success rate*, *CaseAnalysis executions* |
| Messages are stuck after their retries | `ApproximateNumberOfMessagesVisible` | `QueueMessages-*` | *Dead-letter and terminal queue depth* |
| Individual cases failed and need attention | `WorkflowFailuresRecorded`, `OversizedConversations`, `FailureDeliveriesSent` | `WorkflowFailuresRecorded` | *Cases retrieved, processed and failed*, *Cleanup and failure-record reconciliation* |
| A failure could not be recorded | `WorkflowFailureDeliveryErrors`, `FailureDeliveryErrors` | `FailureDeliveryErrors` | *Cleanup and failure-record reconciliation* |
| Cleanup or completion checks are failing | `CleanupErrors`, `RunCompletionErrors` | `CleanupErrors`, `RunCompletionErrors` | *Cleanup and failure-record reconciliation*, *Run completion* |
| Accounts are being skipped | `AccountsSkipped`, `AccountsSkippedAccessDenied`, `AccountsSkippedSubscriptionRequired` | None (expected for accounts without a qualifying plan) | *Accounts discovered and skipped* |
| Bedrock is slow, throttled or failing | `BedrockLatency`, `BedrockThrottles`, `BedrockInvocationFailures` | None directly; persistent failures surface through `WorkflowExecutionsFailed` and `WorkflowFailuresRecorded` | *Bedrock latency (ms)*, *Bedrock throttles and failures* |
| Bedrock usage for cost tracking | `BedrockInputTokens`, `BedrockOutputTokens` | None | *Bedrock tokens* |
| The MCP server is failing | `McpInternalErrors`, `AWS/Lambda Errors`, `5XXError` | `McpInternalErrors`, `LambdaErrors-McpServer`, `McpApiServerErrors` | *MCP requests and errors*, *MCP API responses (stage v1)* |
| Someone is probing or misusing the MCP endpoint | `McpAuthFailures`, `BlockedRequests` | `McpAuthFailures` | *MCP API responses (stage v1)*, *MCP WAF blocked requests* |

## Alarms

Every alarm:

- is named `CaseInsights-<uniqueId>-<purpose>`;
- notifies `CaseInsights-Alarms-<uniqueId>` when it enters ALARM. There are no OK or insufficient-data actions, so no message is sent when an alarm recovers; check the dashboard *Alarms* widget for the current state;
- uses `DatapointsToAlarm` equal to its evaluation periods (every evaluated period must breach);
- ends its description with `Runbook: docs/runbook.md#<purpose in lower case>`, which is the anchor of its section in the [alarm runbook](runbook.md).

Unless stated otherwise, missing data is treated as not breaching, so an alarm stays OK while nothing happens.

### Lambda function errors

`AWS/Lambda` `Errors`, Sum, 300-second period, 1 of 1 period, threshold 1 or more, missing data not breaching. One alarm per pipeline function:

| Alarm purpose | Function | Runbook |
| --- | --- | --- |
| `LambdaErrors-AccountLookup` | `Lambda-AccountLookup-<uniqueId>` | [LambdaErrors-AccountLookup](runbook.md#lambdaerrors-accountlookup) |
| `LambdaErrors-AccountReader` | `Lambda-AccountReader-<uniqueId>` | [LambdaErrors-AccountReader](runbook.md#lambdaerrors-accountreader) |
| `LambdaErrors-CaseRetrieval` | `Lambda-CaseRetrieval-<uniqueId>` | [LambdaErrors-CaseRetrieval](runbook.md#lambdaerrors-caseretrieval) |
| `LambdaErrors-CaseAnnotation` | `Lambda-CaseAnnotation-<uniqueId>` | [LambdaErrors-CaseAnnotation](runbook.md#lambdaerrors-caseannotation) |
| `LambdaErrors-WorkflowStarter` | `Lambda-WorkflowStarter-<uniqueId>` | [LambdaErrors-WorkflowStarter](runbook.md#lambdaerrors-workflowstarter) |
| `LambdaErrors-AnalysisStages` | `Lambda-CaseAnalysisStages-<uniqueId>` | [LambdaErrors-AnalysisStages](runbook.md#lambdaerrors-analysisstages) |
| `LambdaErrors-UpdateCaseMetadata` | `Lambda-UpdateCaseMetadata-<uniqueId>` | [LambdaErrors-UpdateCaseMetadata](runbook.md#lambdaerrors-updatecasemetadata) |
| `LambdaErrors-HandleFailure` | `Lambda-HandleFailure-<uniqueId>` | [LambdaErrors-HandleFailure](runbook.md#lambdaerrors-handlefailure) |
| `LambdaErrors-CaseCleanup` | `Lambda-CaseCleanup-<uniqueId>` | [LambdaErrors-CaseCleanup](runbook.md#lambdaerrors-casecleanup) |
| `LambdaErrors-RunCompletion` | `Lambda-RunCompletion-<uniqueId>` | [LambdaErrors-RunCompletion](runbook.md#lambdaerrors-runcompletion) |

SQS consumers report failed messages individually (partial-batch responses), so a failing message is not a Lambda error; it is retried and, after `operations.queues.maxReceiveCount` receives (default 3), moves to the stage dead-letter queue, which has its own alarm.

### Workflow executions

`AWS/States` on `CaseAnalysis-<uniqueId>`, Sum, 300-second period, 1 of 1, threshold 1 or more, missing data not breaching.

| Alarm purpose | Metric | Runbook |
| --- | --- | --- |
| `WorkflowExecutionsFailed` | `ExecutionsFailed` | [WorkflowExecutionsFailed](runbook.md#workflowexecutionsfailed) |
| `WorkflowExecutionsTimedOut` | `ExecutionsTimedOut` | [WorkflowExecutionsTimedOut](runbook.md#workflowexecutionstimedout) |
| `WorkflowExecutionsAborted` | `ExecutionsAborted` | [WorkflowExecutionsAborted](runbook.md#workflowexecutionsaborted) |

### Dead-letter and terminal queues

`AWS/SQS` `ApproximateNumberOfMessagesVisible`, Maximum, 300-second period, 1 of 1, threshold 1 or more, missing data not breaching. None of these queues has a consumer, so the alarm stays in ALARM until the messages are redriven or deleted.

| Alarm purpose | Queue | Runbook |
| --- | --- | --- |
| `QueueMessages-ActiveAccountsDLQ` | `CaseInsights-ActiveAccounts-<uniqueId>-DLQ` | [QueueMessages-ActiveAccountsDLQ](runbook.md#queuemessages-activeaccountsdlq) |
| `QueueMessages-AnnotationDLQ` | `CaseInsights-CaseAnnotation-<uniqueId>-DLQ` | [QueueMessages-AnnotationDLQ](runbook.md#queuemessages-annotationdlq) |
| `QueueMessages-SummaryDLQ` | `CaseInsights-CaseSummary-<uniqueId>-DLQ` | [QueueMessages-SummaryDLQ](runbook.md#queuemessages-summarydlq) |
| `QueueMessages-WorkflowFailures` | `CaseInsights-WorkflowFailures-<uniqueId>` | [QueueMessages-WorkflowFailures](runbook.md#queuemessages-workflowfailures) |
| `QueueMessages-AccountDiscoveryFailures` | `CaseInsights-AccountDiscoveryFailures-<uniqueId>` | [QueueMessages-AccountDiscoveryFailures](runbook.md#queuemessages-accountdiscoveryfailures) |

### Failure records and reconciliation

| Alarm purpose | Metric or expression | Period, statistic | Threshold, periods | Runbook |
| --- | --- | --- | --- | --- |
| `WorkflowFailuresRecorded` | `SUM` with `FILL(0)` of handle-failure `WorkflowFailuresRecorded`, case-annotation `OversizedConversations`, case-cleanup `FailureDeliveriesSent` and run-completion `FailureDeliveriesSent` | 300 s, Sum | 1 or more, 1 of 1 | [WorkflowFailuresRecorded](runbook.md#workflowfailuresrecorded) |
| `FailureDeliveryErrors` | `SUM` of handle-failure `WorkflowFailureDeliveryErrors`, case-cleanup `FailureDeliveryErrors` and run-completion `FailureDeliveryErrors` | 3600 s, Sum | 1 or more, 1 of 1 | [FailureDeliveryErrors](runbook.md#failuredeliveryerrors) |
| `CleanupErrors` | case-cleanup `CleanupErrors` | 3600 s, Sum | 1 or more, 1 of 1 | [CleanupErrors](runbook.md#cleanuperrors) |
| `RunCompletionErrors` | run-completion `RunCompletionErrors` | 3600 s, Sum | 1 or more, 3 of 3 | [RunCompletionErrors](runbook.md#runcompletionerrors) |

`RunCompletionErrors` needs three consecutive hours with errors, because Run Completion runs hourly and a single transient error is repaired by the next invocation.

### Daily completion

| Alarm purpose | Metric | Period, statistic | Threshold, periods | Missing data | Runbook |
| --- | --- | --- | --- | --- | --- |
| `MissingDailyRunCompletion` | run-completion `DailyRunCompleted` | 3600 s, Sum | less than 1, 30 of 30 | Breaching | [MissingDailyRunCompletion](runbook.md#missingdailyruncompletion) |

See [Daily-completion heartbeat](#daily-completion-heartbeat) for what this means.

### MCP server alarms (MCP only)

All four use a 300-second period, 1 of 1, missing data not breaching.

| Alarm purpose | Metric | Threshold | Runbook |
| --- | --- | --- | --- |
| `LambdaErrors-McpServer` | `AWS/Lambda` `Errors`, `FunctionName=Lambda-McpServer-<uniqueId>`, Sum | 1 or more | [LambdaErrors-McpServer](runbook.md#lambdaerrors-mcpserver) |
| `McpInternalErrors` | `McpInternalErrors`, `service=mcp-server`, Sum | 1 or more | [McpInternalErrors](runbook.md#mcpinternalerrors) |
| `McpApiServerErrors` | `AWS/ApiGateway` `5XXError`, `ApiName=CaseInsights-Mcp-<uniqueId>`, `Stage=v1`, Sum | 1 or more | [McpApiServerErrors](runbook.md#mcpapiservererrors) |
| `McpAuthFailures` | `McpAuthFailures`, `ApiName=CaseInsights-Mcp-<uniqueId>`, Sum | 10 or more | [McpAuthFailures](runbook.md#mcpauthfailures) |

Missing MCP access logs have no alarm of their own; see [MCP access logs are missing](runbook.md#mcp-access-logs-are-missing).

### Alarm count

| Group | Count |
| --- | --- |
| Lambda function errors | 10 |
| Workflow executions | 3 |
| Dead-letter and terminal queues | 5 |
| Failure records and reconciliation | 4 |
| Daily completion | 1 |
| MCP (only with `enableMcp`) | 4 |
| **Total** | 23 without MCP, 27 with MCP |

With the shipped configurations, dev has 27 alarms and prod has 23.

## Dashboard

The dashboard `CaseInsights-<uniqueId>` opens on the last seven days. Graphs use five-minute periods unless noted. Widgets appear in this order:

| # | Widget | Contents |
| --- | --- | --- |
| 1 | Header (text) | Deployment name and the note "Counts only; no account or case content is published. Alarm runbook: docs/runbook.md." |
| 2 | *Daily runs completed (heartbeat)* | Single value: `DailyRunCompleted`, Sum over one day |
| 3 | *Workflow success rate* | Single value: `100 * succeeded / (succeeded + failed + timedOut + aborted)` for `CaseAnalysis-<uniqueId>` |
| 4 | *Alarms* | Alarm status of every alarm |
| 5 | *Accounts discovered and skipped* | `AccountsDiscovered`, `AccountsExcludedInactive`, `AccountsExcludedManagement`, `AccountsProcessed`, `AccountsSkipped` |
| 6 | *Cases retrieved, processed and failed* | `CasesRetrieved`, `CasesAnnotated`, `CasesProcessed`, `WorkflowFailuresRecorded`, `OversizedConversations` (annotation), `CaseRetrievalFailures`, `CaseAnnotationFailures` |
| 7 | *Bedrock latency (ms)* | `BedrockLatency` average and p90 |
| 8 | *Bedrock tokens* | `BedrockInputTokens`, `BedrockOutputTokens` |
| 9 | *Bedrock throttles and failures* | `BedrockThrottles`, `BedrockInvocationFailures` |
| 10 | *CaseAnalysis executions* | `ExecutionsStarted`, `ExecutionsSucceeded`, `ExecutionsFailed`, `ExecutionsTimedOut`, `ExecutionsAborted` |
| 11 | *Dead-letter and terminal queue depth* | Maximum visible messages of the five queues above |
| 12 | *Run completion* | `DailyRunCompleted`, `RunsIncomplete` (Maximum), `RunCasesPending` (Maximum), `RunCompletionErrors`, one-hour periods |
| 13 | *Cleanup and failure-record reconciliation* | `CleanupCasesReset`, `CleanupErrors`, `FailureDeliveriesSent` from cleanup and from run completion, starter `FailureDeliveriesPreserved`, `WorkflowFailureDeliveryErrors` |
| 14 | *Lambda errors* | `Errors` of every pipeline function (full width) |
| 15 | *MCP requests and errors* (MCP only) | `McpRequests`, `McpToolCalls`, `McpErrors`, `McpProtocolErrors`, `McpToolErrors`, `McpInternalErrors`, MCP function `Errors` |
| 16 | *MCP API responses (stage v1)* (MCP only) | `Count`, `4XXError`, `5XXError`, `McpAuthFailures` |
| 17 | *MCP WAF blocked requests* (MCP only) | `BlockedRequests` for all rules, `RateLimitPerIp`, `BlockBodyOver32KiB` and `AWSManagedRulesCommonRuleSet` |

To open it:

```bash
aws cloudwatch get-dashboard --dashboard-name CaseInsights-<uniqueId> --region <region> \
  --query DashboardName --output text
```

or open **CloudWatch, Dashboards, CaseInsights-<uniqueId>** in the console for the deployment Region.

## Daily-completion heartbeat

The heartbeat answers one question: did a whole daily run finish? Account discovery alone never counts.

- **Schedule.** EventBridge Scheduler invokes `Lambda-RunCompletion-<uniqueId>` through `CaseInsights-RunCompletion-<uniqueId>` at `rate(1 hour)`, with two retries and a maximum event age of 30 minutes. Account discovery runs at `cron(0 0 * * ? *)` UTC by default.
- **What is checked.** Each invocation examines the account lists of runs from the last 72 hours. A run is complete when every account in its list has an account marker under `runs/<runId>/accounts/` in the raw bucket, every case those accounts queued has a case marker under `runs/<runId>/cases/`, and every case reached a terminal outcome (skipped, already processed, a terminal oversized conversation, a succeeded execution, or a closed failed, timed-out or aborted execution whose failure record is confirmed on the workflow failure queue). Runs with no accounts, only skipped accounts or no cases complete on the next hourly check.
- **Counted once.** The first invocation to see a complete run writes `runs/<runId>/completion.json` with a conditional write (`IfNoneMatch`) and counts that run in `DailyRunCompleted`. Later invocations do not count it again. Every invocation also writes `DailyRunCompleted`, so the value is usually 0.
- **Alarm.** `MissingDailyRunCompletion` fires when the hourly sum of `DailyRunCompleted` is below 1 for 30 consecutive hours. Missing data counts as breaching, so the alarm also fires if Run Completion stops running altogether or its metrics stop arriving. A daily schedule leaves six hours of slack for a run to drain before the alarm fires.
- **Recovery.** When the backlog drains, the next hourly check completes the run and the alarm returns to OK. Runs older than 72 hours are no longer checked, so a run that never completes stops mattering and the following day's run restores the heartbeat.

Because missing data counts as breaching, the alarm may enter ALARM soon after a fresh deployment, before the first daily run has had time to complete. If it does, it returns to OK in the hour after the first run completes. This first-day behaviour is unverified.

## Tracing

AWS X-Ray active tracing is enabled on every Lambda function (`TracingConfig: Active`), on the `CaseAnalysis-<uniqueId>` state machine and on the MCP API stage `v1`. Traces show the AWS calls each invocation made, their duration and which failed.

What traces contain, by design:

- **No payloads.** Handlers use `capture_lambda_handler(capture_response=False)`, and the functions set `TRACER_CAPTURE_RESPONSE=false` and `TRACER_CAPTURE_ERROR=false`, so no handler response or request body is recorded.
- **Exception type, not message.** A shared helper (`src/lambda/common/tracing.py`) records each exception's type, stack and fault flags but drops its message, because AWS error messages can repeat SQL, prompts or case text. The function's logs give the controlled error code.
- **MCP annotations.** The MCP function also disables error capture and adds the annotations `mcp_method`, `mcp_outcome` and `mcp_tool`, which can be used in X-Ray filter expressions, for example `annotation.mcp_outcome = "internal_error"`.

Step Functions execution data is not logged (see [Logging](logging.md)); the input and output of a state are visible only in the execution history in the Step Functions console.

## Alarm notifications

### SNS topic

| Property | Value |
| --- | --- |
| Name | `CaseInsights-Alarms-<uniqueId>` |
| Display name | `Support Case Insights alarms (<uniqueId>)` |
| Subscription | One email subscription to `alarmEmail`, when set. `alarmEmail` is required in prod by the configuration schema and optional in dev. |
| Publishers | `cloudwatch.amazonaws.com`, allowed `sns:Publish` only for alarms whose ARN matches `arn:aws:cloudwatch:<region>:<account-id>:alarm:CaseInsights-<uniqueId>-*` and only from the deployment account (`aws:SourceAccount`) |
| Transport | A deny statement (`AllowPublishThroughSSLOnly`) refuses `sns:Publish` when `aws:SecureTransport` is false |

AWS sends a confirmation email to `alarmEmail` after the first deployment. Nothing is delivered until the recipient selects the confirmation link. Check the subscription state with:

```bash
aws sns list-subscriptions-by-topic --region <region> \
  --topic-arn arn:aws:sns:<region>:<account-id>:CaseInsights-Alarms-<uniqueId> \
  --query 'Subscriptions[].[Protocol,SubscriptionArn]' --output text
```

`PendingConfirmation` in place of an ARN means the email has not been confirmed.

### Encryption of the alarm topic

The alarm topic `CaseInsights-Alarms-<uniqueId>` is **not** encrypted at rest. This is a deliberate exception that the user explicitly approved, recorded as the cdk-nag acknowledgement `AwsSolutions-SNS2` on this topic only:

- CloudWatch alarms can publish to an encrypted topic only when the topic uses a customer managed KMS key whose key policy lets CloudWatch use it. The AWS managed key `alias/aws/sns` does not allow CloudWatch to publish, and this solution uses no customer managed KMS keys.
- Notifications contain no case content: metrics are counts and durations with a `service` or resource-name dimension, and no case identifier or case text is part of any alarm. They do contain operational metadata: alarm names and descriptions, old and new states and the state-change reason, metric names, thresholds and values, the AWS account ID, the Region and resource names such as function and queue names. That metadata leaves the account through the confirmed email subscriptions.
- The topic policy denies any request that does not use TLS, and only `cloudwatch.amazonaws.com` may publish, for this account's alarms named `CaseInsights-<uniqueId>-*`.

The SNS acknowledgement applies to this topic alone. The storage and queue encryption used by the pipeline is described in [Security](security.md). If your organisation requires encryption at rest for every SNS topic, the change needed is a customer managed KMS key whose key policy allows CloudWatch to use it, set as the topic's key; this repository does not implement that.

## Production guidance

- **Confirm the subscription.** Confirm the alarm email after the first prod deployment and test it, for example by setting an alarm state temporarily:

  ```bash
  aws cloudwatch set-alarm-state --region <region> \
    --alarm-name CaseInsights-<uniqueId>-QueueMessages-WorkflowFailures \
    --state-value ALARM --state-reason "Notification test"
  ```

  The alarm returns to its real state at its next evaluation, without a second notification.
- **Use a group address.** Set `alarmEmail` to a monitored distribution list or ticketing address rather than a person. Other subscribers (chat integrations, paging tools) can be added to the topic outside this stack; they receive the same content.
- **Treat the heartbeat as the primary signal.** `MissingDailyRunCompletion` covers failures that no other alarm sees, such as a schedule that stopped or a run that silently stalls.
- **Do not leave queue alarms in ALARM.** A dead-letter or terminal queue alarm stays in ALARM until its messages are handled, which hides a new failure. Follow the [recovery tools](runbook.md#recovery-tools) and delete handled failure records.
- **Expect some failures.** Accounts without a qualifying Support plan are skipped, and a few cases may fail validation or exceed the summary input limit. Review `WorkflowFailuresRecorded` trends rather than treating every record as an incident.
- **Watch Bedrock capacity.** Rising `BedrockThrottles` with `WorkflowExecutionsTimedOut` usually means `bedrockMaxConcurrency` or the account's Bedrock quota is too small for the volume.
- **Keep logs in prod.** Prod log groups are retained when the stack is deleted and kept for `logRetentionDays` (90 days in `config/prod.json`); see [Logging](logging.md).
- **Change thresholds in code.** Thresholds and periods are defined in `lib/constructs/observability.ts` and `lib/constructs/mcp-observability.ts`, not in configuration. A change is deployed with `npx cdk deploy --all -c env=<env>` and should be reflected in this guide and the runbook.

## Live verification status

Not verified, because nothing has been deployed:

- [ ] Every metric listed here appears in the `SupportCaseInsights` namespace after a run.
- [ ] Each alarm reaches the expected state, and a test notification arrives by email after confirmation.
- [ ] The dashboard widgets show data for a run, including the MCP rows in dev.
- [ ] `DailyRunCompleted` is 1 for the hour in which a daily run completes, and `MissingDailyRunCompletion` stays OK.
- [ ] `McpAuthFailures` counts refused requests from the access log.

The alarm inventory is checked locally against the synthesised template by `test/cdk/constructs/observability.test.ts`.

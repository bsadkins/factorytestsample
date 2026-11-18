# Logging

This guide describes the logs a Support Case Insights deployment writes: every CloudWatch Logs log group with its retention, removal and encryption, the structured JSON format of the Lambda logs and the field names they use, what is never logged, the main messages of each pipeline stage, ready-to-use CloudWatch Logs Insights queries, and the S3 server access logs. Facts come from the CDK code and Lambda source in this repository; nothing has been deployed, so the exact appearance of log records in a real account is unverified. For metrics, alarms and the dashboard see [Monitoring](monitoring.md); for symptom-led diagnosis see [Troubleshooting](troubleshooting.md).

## Log groups

| Log group | Source | Created by |
| --- | --- | --- |
| `/aws/lambda/Lambda-AccountLookup-<uniqueId>` | Account lookup | Core stack |
| `/aws/lambda/Lambda-AccountReader-<uniqueId>` | Account reader | Core stack |
| `/aws/lambda/Lambda-CaseRetrieval-<uniqueId>` | Case retrieval | Core stack |
| `/aws/lambda/Lambda-CaseAnnotation-<uniqueId>` | Case annotation | Core stack |
| `/aws/lambda/Lambda-WorkflowStarter-<uniqueId>` | Workflow starter | Core stack |
| `/aws/lambda/Lambda-CaseAnalysisStages-<uniqueId>` | Summary, RCA and lifecycle stages, including Bedrock calls | Core stack |
| `/aws/lambda/Lambda-UpdateCaseMetadata-<uniqueId>` | Processed-record commit | Core stack |
| `/aws/lambda/Lambda-HandleFailure-<uniqueId>` | Failure recording | Core stack |
| `/aws/lambda/Lambda-CaseCleanup-<uniqueId>` | Daily cleanup | Core stack |
| `/aws/lambda/Lambda-RunCompletion-<uniqueId>` | Hourly run completion | Core stack |
| `/aws/lambda/Lambda-McpServer-<uniqueId>` | MCP server (only with `enableMcp`) | MCP stack |
| `/aws/vendedlogs/states/CaseAnalysis-<uniqueId>` | `CaseAnalysis-<uniqueId>` state machine | Core stack |
| `/aws/apigateway/CaseInsights-Mcp-<uniqueId>/access` | MCP REST API access log, stage `v1` (only with `enableMcp`) | MCP stack |
| `/aws-glue/crawlers` | Glue partition crawler `case-insights-partition-crawler-<uniqueId>` (only with `enableAnalytics`), in a stream named after the crawler | Glue, on first crawl; not managed by any stack |

CDK also deploys small helper functions of its own (for example the bucket-notification handler and, in dev, the automatic deletion of bucket objects). They write to Lambda's default log groups, which this solution does not create or manage; CloudWatch Logs keeps such groups indefinitely unless someone sets a retention. They run only during stack deployments.

### Retention, removal and encryption

| Setting | Value |
| --- | --- |
| Retention | `logRetentionDays` from `config/<env>.json` for every log group the stacks create (default 90; `config/dev.json` uses 30 and `config/prod.json` uses 90). Only the retention periods CloudWatch Logs supports are accepted. |
| Removal | In prod the log groups are retained when a stack is deleted (`DeletionPolicy: Retain`). In dev they are deleted with the stack. |
| Encryption | No customer managed KMS key is associated with the log groups. CloudWatch Logs applies its default server-side encryption. |
| `/aws-glue/crawlers` | Not created by the stack. The crawler role may create it and write to it. Its retention is not set by this solution; set one yourself if your account requires it. The crawler logs only crawl progress and schema change notices (`UpdateBehavior` and `DeleteBehavior` are `LOG`). |

To check retention after deployment:

```bash
aws logs describe-log-groups --region <region> --log-group-name-prefix /aws/lambda/Lambda- \
  --query 'logGroups[?ends_with(logGroupName, `-<uniqueId>`)].[logGroupName,retentionInDays]' --output text
```

See [Resources](resources.md) for the full resource inventory and [Security](security.md) for the data-handling rules behind the settings below.

## Lambda log format

Every function uses the Lambda JSON log format (`LoggingFormat: JSON`) with the AWS Lambda Powertools logger. Each application log record is one JSON object. The functions run with:

| Variable | Value | Effect |
| --- | --- | --- |
| `LOG_LEVEL` | `INFO` | `DEBUG` records are not written |
| `LOGGER_LOG_EVENT` | `false` | The invocation event is never logged |
| `LOGGER_SAMPLE_RATE` | `0` | No debug sampling |
| `POWERTOOLS_SERVICE_NAME` | the service name, for example `case-retrieval` | Written as `service` in every record |

Handlers use `inject_lambda_context(log_event=False, clear_state=True)`, so each record carries the Lambda context keys and nothing from a previous invocation. Metrics are written into the same log streams in Embedded Metric Format; those records have an `_aws` key and can be excluded with `filter ispresent(message)`.

An illustrative record (invented values; real records also contain Powertools keys such as `location` and `function_arn`):

```json
{
  "level": "WARNING",
  "message": "Account skipped for this run",
  "timestamp": "2026-11-14 00:04:12,345+0000",
  "service": "case-retrieval",
  "cold_start": false,
  "function_name": "Lambda-CaseRetrieval-scidev",
  "function_request_id": "6f1c2c8e-example",
  "xray_trace_id": "1-6735f0a4-example",
  "run_id": "20261114T000000Z-ab12cd34",
  "account_id": "111122223333",
  "reason": "SubscriptionRequired"
}
```

Lambda's own platform records (start, end, report, and timeouts) are also written as JSON in this format. Their exact shape follows the AWS Lambda documentation and has not been checked against a deployment.

### Field vocabulary

Standard keys: `level`, `message`, `timestamp`, `service`, `cold_start`, `function_name`, `function_request_id`, `xray_trace_id`.

Application keys (snake case unless noted):

| Field | Meaning |
| --- | --- |
| `run_id` | Run identifier, one per account discovery (`<timestamp>-<random>`) |
| `account_id` | Member account ID |
| `display_id` | Support case display ID |
| `message_id` | SQS message ID |
| `object_key`, `version_id` | S3 object key and version of an account list or record |
| `bucket`, `key` | S3 bucket and key of a failed S3 read, write or delete |
| `reason` | Controlled reason code, for example `AccessDenied` or `SubscriptionRequired` |
| `error_type` | Exception class name |
| `error_code` | AWS error code, when there is one (never the AWS error message) |
| `http_status` | HTTP status of a failed AWS call |
| `outcome` | Stage or handler outcome |
| `stage` | Workflow stage (`summary`, `rca`, `lifecycle` or `update`) |
| `attempt`, `attempts`, `max_attempts`, `delay_seconds`, `operation` | Retry details |
| `in_progress`, `redrive`, `same_run` | Duplicate and redrive handling flags |
| `execution_name`, `recovered_from`, `owner_run_id`, `lost_claims` | Workflow ownership details |
| `failure_type`, `delivery_error` | Failure-record type and the class of a delivery error |
| `communications`, `annotation_chars`, `limit_chars` | Conversation size details |
| `summary_chars`, `sentences`, `issues` | Summary validation details (counts and issue codes, never text) |
| `field`, `fallback`, `rca_fallback`, `lifecycle_fallback`, `reused_concurrent` | Classification and commit details |
| `model_id`, `latency_ms`, `input_tokens`, `output_tokens`, `stop_reason`, `throttles`, `prompt_chars`, `limit` | Bedrock invocation details |
| `failed`, `failed_entries`, `error_codes` | SQS batch send failures |
| `accounts`, `listed`, `selected`, `excludedInactive`, `excludedManagement` | Account discovery counts (camel case) |
| `casesListed`, `casesRetrieved` and the other case counts, `afterTime`, `beforeTime` | Case retrieval counts and the collection window (camel case) |
| `evaluated`, `reset`, `errors`, `skipped`, `complete`, `failureDeliveries` | Cleanup totals (camel case) |
| `runsChecked`, `runsCompleted`, `runsIncomplete`, `pendingAccounts`, `pendingCases`, `timedOut`, `accountsRetrieved`, `accountsSkipped`, `casesProcessed`, `casesSkipped`, `casesFailed` | Run completion counts (camel case) |
| `mcp_method`, `mcp_outcome`, `status_code`, `mcp_tool`, `mcp_error_code`, `tool_duration_ms` | MCP request details |
| `query_execution_id`, `state` | Athena query details (MCP server) |

`mcp_method` is one of `initialize`, `ping`, `tools/list`, `tools/call`, `notification`, `response` or `unknown`. `mcp_outcome` is one of `ok`, `accepted`, `protocol_error`, `tool_error` or `internal_error`.

### What is never logged

- Case subjects, descriptions, communications or attachments.
- Case summaries, classification replies, prompts or other model output.
- The invocation event, SQS message bodies, Step Functions state input and output, or HTTP request and response bodies.
- MCP tool arguments, SQL text and query results.
- AWS error messages, which can repeat SQL, prompts or case text. Only the error code, HTTP status and exception class are logged, and traces keep the exception type without its message.
- Credentials, tokens and request signatures.

What is logged and can identify a customer or case: account IDs, case display IDs, run IDs, SQS message IDs and S3 keys. S3 keys include the account ID and display ID (for example `account_number=<id>/case_number=<displayId>/...`). Treat the log groups with the same access controls as the operational data; see [Permissions](permissions.md).

## Messages by stage

The tables list every application message and its main keys. Records written while handling a case reference also carry `run_id`, `account_id` and `display_id`, and records of SQS consumers carry `message_id`; those are not repeated below. Search for a message with `filter message = "<text>"`.

### Account lookup and reader

| Level | Message | Key fields |
| --- | --- | --- |
| INFO | `Account list written` | `run_id`, `object_key`, `listed`, `selected`, `excludedInactive`, `excludedManagement` |
| WARNING | `Account list for this run already exists` | `run_id`, `object_key` |
| INFO | `Account references enqueued` | `object_key`, `version_id`, `run_id`, `accounts` |
| INFO | `Duplicate account-list event skipped` | `object_key`, `version_id` |
| INFO | `Account list is already being processed` | `object_key`, `version_id` |
| ERROR | `Account list is malformed` | `object_key`, `version_id`, `reason` |
| ERROR | `Account list processing failed` | `object_key`, `version_id`, `error_type`, `error_code` |

### Case retrieval and annotation

| Level | Message | Key fields |
| --- | --- | --- |
| INFO | `Account cases retrieved` | case counts (`casesListed`, `casesRetrieved` and others), `afterTime`, `beforeTime` |
| WARNING | `Account skipped for this run` | `reason` (`AccessDenied` or `SubscriptionRequired`) |
| WARNING | `Member role assumption failed` | `account_id`, `error_type`, `error_code` |
| ERROR | `Active-account message is malformed` | `message_id`, `reason` |
| ERROR | `Case retrieval failed` | `error_type`, `error_code` |
| INFO | `Duplicate account message skipped` | `in_progress` |
| INFO | `Case conversation stored and queued for summary` | `outcome`, `communications`, `annotation_chars` |
| ERROR | `Case conversation exceeds the summary input limit; stored and reported as failed` | `outcome`, `communications`, `annotation_chars` |
| WARNING | `Case conversation is still too large and unchanged since it was reported; not resent` | `outcome`, `annotation_chars` |
| WARNING | `Case not annotated` | `outcome` |
| ERROR | `Annotation message is malformed` | `message_id`, `reason` |
| ERROR | `Case annotation failed` | `error_type`, `error_code` |
| INFO | `Duplicate annotation message skipped` | `in_progress` |

### Workflow starter

| Level | Message | Key fields |
| --- | --- | --- |
| INFO | `Case workflow start reconciled` | `outcome`, `execution_name`, `attempt`, `recovered_from`, `owner_run_id`, `same_run`, `lost_claims` (when present) |
| ERROR | `Summary message is malformed` | `message_id`, `reason` |
| ERROR | `Case workflow start failed` | `error_type`, `error_code` |
| INFO | `Duplicate summary message skipped` | `in_progress` |

### Analysis stages and Bedrock

| Level | Message | Key fields |
| --- | --- | --- |
| INFO | `Summary stored` | `stage`, `attempts`, `summary_chars`, `sentences` |
| INFO | `Valid summary checkpoint reused` | `stage` |
| INFO | `Case already processed; summary skipped` | `stage` |
| WARNING | `Summary failed validation` | `stage`, `attempt`, `issues`, `summary_chars`, `sentences`, `stop_reason` |
| ERROR | `Summary still invalid after the correction; stage failed` | `stage`, `issues` |
| ERROR | `Case conversation exceeds the summary input limit; not summarised` | `stage`, `annotation_chars`, `limit_chars` |
| INFO | `Classification stored` | `stage`, `fallback`, `reused_concurrent` |
| INFO | `Valid classification checkpoint reused` | `stage` |
| INFO | `Case already processed; classification skipped` | `stage` |
| WARNING | `Classification reply failed validation` | `stage`, `attempt`, `reason` |
| WARNING | `Classification recorded as Unclassified after one correction` | `stage`, `reason` |
| INFO | `Stage checkpoint is not reusable` | `stage`, `reason` |
| INFO | `Bedrock invocation complete` | `model_id`, `latency_ms`, `input_tokens`, `output_tokens`, `stop_reason`, `attempts`, `throttles` |
| WARNING | `Bedrock invocation failed` | `model_id`, `attempts`, `throttles`, `error_type`, `error_code` |
| WARNING | `Prompt exceeds the configured size and was not sent` | `model_id`, `prompt_chars`, `limit` |
| INFO | `Bedrock retry skipped; a durable result already exists` | `model_id`, `attempts`, `throttles` |

The Bedrock records come from a shared module and do not carry `display_id` or `stage`; correlate them with the stage records of the same `function_request_id`.

### Commit and failure handling

| Level | Message | Key fields |
| --- | --- | --- |
| INFO | `Processed record committed` | `stage`, `rca_fallback`, `lifecycle_fallback` |
| INFO | `Case already processed; existing record kept` | `stage` |
| ERROR | `Processed record failed validation; nothing written` | `stage`, `reason`, `field` |
| INFO | `A processed record was committed concurrently; it was kept` | `stage` |
| ERROR | `Case analysis attempt failed and was recorded` | `failure_type`, `stage`, `error_type`, `error_code`, `execution_name` |
| ERROR | `Workflow failure could not be recorded` | as above, plus `delivery_error` |
| INFO | `Failure record already confirmed for this execution; not resent` | as above |
| WARNING | `Failure-delivery marker could not be read` | `error_type`, `error_code` |
| WARNING | `Failure record sent but not confirmed; reconciliation may send it again` | as above, plus `error_type`, `error_code` |

### Cleanup and run completion

| Level | Message | Key fields |
| --- | --- | --- |
| INFO | `Cleanup finished` | `evaluated`, `reset`, `errors`, `skipped`, `complete`, `failureDeliveries` |
| INFO | `Abandoned case reset for a later run` | `account_id`, `display_id` |
| WARNING | `Case status could not be checked; left untouched` | `account_id`, `display_id`, `error_type`, `error_code` |
| WARNING | `Case ownership is unknown; left untouched` | `account_id`, `display_id` |
| WARNING | `Pending failure record could not be delivered; kept` | `account_id`, `display_id` |
| ERROR | `Cleanup failed; nothing further was deleted` | `error_type`, `error_code` |
| INFO | `Run complete` | `run_id`, run counts (`accounts`, `accountsRetrieved`, `accountsSkipped`, `cases`, `casesProcessed`, `casesSkipped`, `casesFailed`) |
| INFO | `Run is not complete yet` | `run_id`, run counts, `pendingAccounts`, `pendingCases`, `errors`, `timedOut` |
| INFO | `Run completion check finished` | `runsChecked`, `runsCompleted`, `runsIncomplete`, `errors` |
| WARNING | `Run could not be checked` | `run_id`, `error_type`, `error_code` |
| WARNING | `Account run marker is unreadable` | `run_id`, `account_id`, `error_type`, `error_code` |
| WARNING | `Case outcome could not be reconciled; run stays incomplete` | `run_id`, `account_id`, `display_id`, `error_type`, `error_code` |
| ERROR | `Run completion check failed` | `error_type`, `error_code` |

### Shared helpers

| Level | Message | Key fields |
| --- | --- | --- |
| WARNING | `Retrying after retryable error` | `operation`, `attempt`, `max_attempts`, `delay_seconds`, `error_type`, `error_code` |
| WARNING | `Retrying failed SQS batch entries` | `attempt`, `failed_entries`, `error_codes` |
| ERROR | `SQS messages were not sent` | `failed`, `error_codes` |
| WARNING | `S3 read failed`, `S3 write failed`, `S3 delete failed` | `bucket`, `key`, `error_type`, `error_code` |

### MCP server

| Level | Message | Key fields |
| --- | --- | --- |
| INFO | `MCP request handled` | `mcp_method`, `mcp_outcome` (`ok` or `accepted`), `status_code`, `mcp_tool`, `tool_duration_ms` |
| WARNING | `MCP request rejected` | `mcp_method`, `mcp_outcome` (`protocol_error` or `tool_error`), `status_code`, `mcp_tool`, `mcp_error_code` |
| ERROR | `MCP request failed unexpectedly` | `mcp_method`, `mcp_outcome` (`internal_error`), `status_code`, `mcp_tool`, `error_type`, `error_code` |
| INFO | `Athena query started` | `query_execution_id` |
| INFO | `Athena query did not succeed` | `query_execution_id`, `state` |
| INFO | `Athena query still running at the time budget` | `query_execution_id` |
| INFO | `Athena results cut short at the time budget` | none |
| WARNING | `Athena request failed` | `operation`, `error_type`, `error_code` |
| WARNING | `No time left to stop Athena query`, `Could not stop Athena query` | `query_execution_id`, error fields |
| WARNING | `Bedrock analysis failed` | `model_id`, `attempts`, `error_type`, `error_code` |

`error_code` and `http_status` appear only when the failure was an AWS service error. Message texts are copied from the Lambda source; the queries below depend on them.

## Step Functions logs

`/aws/vendedlogs/states/CaseAnalysis-<uniqueId>` receives execution history events at level `ALL` with `IncludeExecutionData` set to false. Records show the execution ARN, event type (for example `TaskStateEntered`, `TaskFailed`, `ExecutionFailed`) and state name, and error names for failures, but no state input, output or error cause text. Open an execution in the Step Functions console to see its input and output in the execution history. X-Ray tracing is enabled for the state machine.

## API Gateway access logs

`/aws/apigateway/CaseInsights-Mcp-<uniqueId>/access` receives one JSON record per request to stage `v1` with these fields:

`requestId`, `extendedRequestId`, `requestTime`, `apiName`, `stage`, `httpMethod`, `resourcePath`, `protocol`, `status`, `responseLength`, `responseLatency`, `errorType`, `integrationStatus`, `integrationLatency`, `wafResponseCode`, `sourceIp`, `callerAccountId`, `xrayTraceId`.

The format deliberately omits `$context.error.message`, headers, bodies and caller ARNs. Execution logging is off (`LoggingLevel: OFF`, `DataTraceEnabled: false`), so API Gateway writes no execution log group. Access logs need the account-wide API Gateway CloudWatch role setting; see [Shared API Gateway logging prerequisite](mcp-server-deployment.md#shared-api-gateway-logging-prerequisite) and [MCP access logs are missing](runbook.md#mcp-access-logs-are-missing). The web ACL has sampled requests turned off and no AWS WAF logging is configured, so blocked requests appear only as `BlockedRequests` metrics and, when blocked, as access log entries with `wafResponseCode`.

## CloudWatch Logs Insights queries

Run these in **CloudWatch, Logs Insights** in the deployment Region, selecting the log groups named in each heading. Queries across several Lambda log groups can select every group whose name starts with `/aws/lambda/Lambda-` and ends with `-<uniqueId>`. They can also run from the command line, for example:

```bash
aws logs start-query --region <region> \
  --log-group-names /aws/lambda/Lambda-CaseRetrieval-<uniqueId> \
  --start-time $(date -u -d '24 hours ago' +%s) --end-time $(date -u +%s) \
  --query-string 'filter level in ["ERROR", "WARNING"] | stats count() by message'
aws logs get-query-results --region <region> --query-id <query-id>
```

(`date -d` is GNU syntax; on macOS use `date -u -v-24H +%s`.)

### 1. Errors and warnings across the pipeline

Log groups: all Lambda log groups.

```
fields @timestamp, service, level, message, run_id, account_id, display_id, error_type, error_code
| filter level in ["ERROR", "WARNING"]
| sort @timestamp desc
| limit 200
```

### 2. Error counts by service and message

Log groups: all Lambda log groups.

```
filter level = "ERROR"
| stats count() as errors by service, message, error_type, error_code
| sort errors desc
```

### 3. Follow one run

Log groups: all Lambda log groups.

```
fields @timestamp, service, message, account_id, display_id, outcome
| filter run_id = "<runId>"
| sort @timestamp asc
| limit 1000
```

### 4. Follow one case

Log groups: all Lambda log groups.

```
fields @timestamp, service, level, message, stage, outcome, execution_name, failure_type, error_type
| filter display_id = "<displayId>"
| sort @timestamp asc
```

### 5. Skipped accounts by reason

Log groups: `/aws/lambda/Lambda-CaseRetrieval-<uniqueId>`.

```
filter message = "Account skipped for this run"
| stats count() as skips by reason, account_id
| sort skips desc
```

### 6. Bedrock latency, tokens and throttles per hour

Log groups: `/aws/lambda/Lambda-CaseAnalysisStages-<uniqueId>`.

```
filter message = "Bedrock invocation complete"
| stats count() as calls, avg(latency_ms) as avg_ms, pct(latency_ms, 90) as p90_ms,
        sum(input_tokens) as input_tokens, sum(output_tokens) as output_tokens,
        sum(throttles) as throttles by bin(1h), model_id
| sort bin(1h) desc
```

### 7. Bedrock failures by error code

Log groups: `/aws/lambda/Lambda-CaseAnalysisStages-<uniqueId>`.

```
filter message in ["Bedrock invocation failed", "Prompt exceeds the configured size and was not sent"]
| stats count() as failures by message, error_code, model_id
| sort failures desc
```

### 8. Failed case analyses

Log groups: `/aws/lambda/Lambda-HandleFailure-<uniqueId>`.

```
fields @timestamp, run_id, account_id, display_id, failure_type, stage, error_type, error_code, execution_name
| filter message = "Case analysis attempt failed and was recorded"
| sort @timestamp desc
| limit 100
```

### 9. Summary and classification validation problems

Log groups: `/aws/lambda/Lambda-CaseAnalysisStages-<uniqueId>`.

```
filter message in ["Summary failed validation", "Summary still invalid after the correction; stage failed",
                   "Classification reply failed validation",
                   "Classification recorded as Unclassified after one correction"]
| stats count() as occurrences by message, stage, reason
```

### 10. Runs that are not complete

Log groups: `/aws/lambda/Lambda-RunCompletion-<uniqueId>`.

```
fields @timestamp, run_id, pendingAccounts, pendingCases
| filter message = "Run is not complete yet"
| sort @timestamp desc
| limit 50
```

### 11. Cleanup results per run

Log groups: `/aws/lambda/Lambda-CaseCleanup-<uniqueId>`.

```
fields @timestamp, evaluated, reset, errors, skipped, complete
| filter message = "Cleanup finished"
| sort @timestamp desc
```

### 12. Retries of AWS calls

Log groups: all Lambda log groups.

```
filter message = "Retrying after retryable error"
| stats count() as retries by service, operation, error_code
| sort retries desc
```

### 13. MCP requests by method and outcome

Log groups: `/aws/lambda/Lambda-McpServer-<uniqueId>`.

```
filter ispresent(mcp_method)
| stats count() as requests, avg(tool_duration_ms) as avg_tool_ms by mcp_method, mcp_tool, mcp_outcome
| sort requests desc
```

### 14. Refused MCP requests

Log groups: `/aws/apigateway/CaseInsights-Mcp-<uniqueId>/access`.

```
fields @timestamp, status, errorType, wafResponseCode, sourceIp, callerAccountId
| filter status >= 400
| stats count() as requests by status, errorType, wafResponseCode, callerAccountId, sourceIp
| sort requests desc
```

### 15. Lambda timeouts

Log groups: any Lambda log group.

```
fields @timestamp, @log, @message
| filter @message like /Task timed out/ or @message like /"status":"timeout"/
| sort @timestamp desc
```

The pattern for timeouts depends on how Lambda writes platform records in JSON format; check it against a real timeout before relying on it.

## S3 server access logs

The core stack creates an access-log bucket (logical ID `StorageAccessLogs`) and enables S3 server access logging on the data buckets:

| Source bucket | Log prefix |
| --- | --- |
| Account-list bucket | `account-list/` |
| Raw bucket | `raw/` |
| Processed bucket | `processed/` |
| Analytics query-results bucket (only with `enableAnalytics`) | `athena-results/` |
| Pipeline artifact bucket (only with the optional pipeline, in its own access-log bucket) | `pipeline-artifacts/` |

- Log objects expire after `operations.storage.accessLogRetentionDays` (default 365).
- The access-log bucket does not log access to itself.
- S3 delivers server access logs on a best-effort basis, usually within a few hours; they are suitable for audit and investigation, not for alarms.
- Records include the requester, operation, key and status. Keys include account IDs and display IDs, so treat the logs like the data buckets.

No Athena table is created for the access logs. To query them, follow the AWS guide for querying S3 server access logs with Athena and point the table at `s3://<access-log bucket>/<prefix>`.

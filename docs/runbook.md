# Alarm runbook

> **Provisional.** This runbook lists every alarm the core stack creates, with first diagnosis and repair steps, and the [recovery tools](#recovery-tools) for dead-letter queue redrive and opt-in reprocessing. The full operations documentation set will extend it; the alarm inventory here is checked against the synthesised alarms by `test/constructs/observability.test.ts`.

All alarms are named `CaseInsights-<uniqueId>-<purpose>` and notify the SNS topic `CaseInsights-Alarms-<uniqueId>`. When `alarmEmail` is set (it is required in prod), AWS sends a confirmation email to that address after deployment; nothing is delivered until the recipient selects the confirmation link. The dashboard `CaseInsights-<uniqueId>` shows every metric referenced below and the state of every alarm.

Metrics are in the `SupportCaseInsights` namespace with a single `service` dimension (the Powertools service name of the emitting function). They are counts and durations only. Logs are structured JSON and include account IDs, case display IDs and run IDs where useful for diagnosis, but never case content, conversations, prompts or model replies.

## General procedure

1. Open the alarm in CloudWatch and note the period in which it fired.
2. Open the dashboard for the same period to see which stage changed.
3. Query the log group of the function involved with CloudWatch Logs Insights, for example:

   ```
   fields @timestamp, level, message, run_id, account_id, display_id, error_type
   | filter level in ["ERROR", "WARNING"]
   | sort @timestamp desc
   | limit 100
   ```

4. Use the X-Ray trace of a failing invocation to see which AWS call failed. Traces hold no request or response payloads. Every function's traces record the type of each exception, such as `InvalidRequestException` or `ClientError`, but not its message, because AWS error messages can repeat SQL, prompts or case text; the function's logs give the controlled error code.
5. Fix the cause, then let the next scheduled run retry: completed cases are never processed again, and unfinished cases are retried by the next run or reset by daily cleanup.

## Lambda errors

Each pipeline function has an alarm that fires when it reports one or more invocation errors (unhandled exceptions, timeouts or out-of-memory) in a five-minute period. Partial-batch failures that a consumer reports for individual messages are not invocation errors; those messages are retried by SQS and land in the stage's dead-letter queue when their receive count is exhausted.

For every function: check the function's log group (`/aws/lambda/<function name>`) for `ERROR` entries and the `error_type` field, check recent deployments and configuration changes, and check service quotas (Lambda concurrency, S3, SQS, Step Functions, Bedrock).

### LambdaErrors-AccountLookup

`Lambda-AccountLookup-<uniqueId>` could not list Organizations accounts or write the account list. Check that the stack is deployed in the management account, that Organizations is reachable, and that the account-list bucket exists. Failed scheduled invocations land in `CaseInsights-AccountDiscoveryFailures-<uniqueId>` after retries. To start the run again, invoke the function manually (a manual invocation gets its own run ID).

### LambdaErrors-AccountReader

`Lambda-AccountReader-<uniqueId>` could not read or validate an account list or enqueue accounts. An `InvalidAccountList` error means the object did not match the contract; inspect it in `account-lists/`. Failed events land in `CaseInsights-AccountDiscoveryFailures-<uniqueId>`.

### LambdaErrors-CaseRetrieval

`Lambda-CaseRetrieval-<uniqueId>` failed for a whole batch. Check for errors assuming `Support-Case-Analysis-Role-<uniqueId>` (StackSet instance missing in a new account or OU), Support API throttling, or raw-bucket write errors. Accounts without a Business, Enterprise On-Ramp or Enterprise Support plan are skipped, not errors.

### LambdaErrors-CaseAnnotation

`Lambda-CaseAnnotation-<uniqueId>` failed for a whole batch. Check member-role access, `DescribeCommunications` throttling and raw-bucket writes.

### LambdaErrors-WorkflowStarter

`Lambda-WorkflowStarter-<uniqueId>` could not start or describe executions. Check `states:StartExecution` throttling, the ownership markers (`workflow.json`) and the idempotency table. `CaseResetInProgressError` means Case Cleanup is resetting the case; the record is retried and starts once the reset finishes or its claim is an hour old. Messages are retried and land in the summary dead-letter queue when their receive count is exhausted.

### LambdaErrors-AnalysisStages

`Lambda-CaseAnalysisStages-<uniqueId>` raised an error that the workflow did not handle. Bedrock throttling and transient errors are normally retried inside the function and by the workflow; look for `UnsupportedModelError`, model access errors (model access or Marketplace subscription missing) or prompt errors. The workflow records failed attempts on the workflow failure queue.

### LambdaErrors-UpdateCaseMetadata

`Lambda-UpdateCaseMetadata-<uniqueId>` could not commit a processed record. `IncompleteAnalysisError` means a checkpoint was missing or invalid; the attempt is recorded and retried by a later run.

### LambdaErrors-HandleFailure

`Lambda-HandleFailure-<uniqueId>` could not send a failure record. Step Functions retries it; if retries are exhausted the execution fails without a confirmed record, and run completion or cleanup reconciles it (see [FailureDeliveryErrors](#failuredeliveryerrors)). Check `sqs:SendMessage` on the workflow failure queue.

### LambdaErrors-CaseCleanup

`Lambda-CaseCleanup-<uniqueId>` failed. Cleanup never deletes anything when a status check fails, so nothing is lost; the next daily run resumes from its saved position under `cleanup-state/`.

### LambdaErrors-RunCompletion

`Lambda-RunCompletion-<uniqueId>` could not list account lists. It runs hourly, so a transient error is repaired by the next invocation; a persistent one also triggers [MissingDailyRunCompletion](#missingdailyruncompletion). Check access to the account-list and raw buckets.

## Workflow executions

### WorkflowExecutionsFailed

One or more `CaseAnalysis-<uniqueId>` executions failed. Each failed attempt should produce a record on the workflow failure queue (see [QueueMessages-WorkflowFailures](#queuemessages-workflowfailures)). Open a failed execution in the Step Functions console: the failing state and error type show which stage failed (execution data is not logged, so the input and output are visible only in the execution history). A later run starts the next attempt automatically.

### WorkflowExecutionsTimedOut

Executions exceeded `operations.workflow.executionTimeoutMinutes`. A whole-execution timeout bypasses the task error handling, so HandleFailure does not run. Run Completion (hourly) and Case Cleanup (daily) preserve a pending failure-delivery marker under `account_number=<id>/case_number=<displayId>/failure-deliveries/<executionName>.json` and send a `WorkflowExecutionClosed` record to the workflow failure queue. Check for a large backlog with a small `bedrockMaxConcurrency` (executions wait for stage capacity) and consider raising the timeout or the concurrency cap.

### WorkflowExecutionsAborted

Executions were stopped manually or by a deployment. Their failures are recorded by reconciliation in the same way as timeouts. If the stop was deliberate, no action is needed; the next run retries the cases.

## Dead-letter and terminal queues

Every dead-letter queue and terminal queue alarms when it holds at least one visible message. The queues have no consumer. Messages contain references (account ID, case ID, display ID, run ID, object keys), never case content. Keep messages until the cause is fixed, then redrive or delete them explicitly.

### QueueMessages-ActiveAccountsDLQ

Account messages for which case retrieval failed `operations.queues.maxReceiveCount` times. Check the [LambdaErrors-CaseRetrieval](#lambdaerrors-caseretrieval) causes, then redrive the messages to `CaseInsights-ActiveAccounts-<uniqueId>` with `scripts/redrive-dlq.sh --env <env> --queue active-accounts` (see [Redrive a stage dead-letter queue](#redrive-a-stage-dead-letter-queue)). The account's run stays incomplete until it is retrieved or the run leaves the run-completion lookback window.

### QueueMessages-AnnotationDLQ

Case references for which annotation failed repeatedly. Fix the cause and redrive to `CaseInsights-CaseAnnotation-<uniqueId>` with `scripts/redrive-dlq.sh --env <env> --queue annotation`.

### QueueMessages-SummaryDLQ

Annotated case references the workflow starter could not start. Fix the cause and redrive to `CaseInsights-CaseSummary-<uniqueId>` with `scripts/redrive-dlq.sh --env <env> --queue summary`. Committed cases among them are answered as already processed.

### QueueMessages-WorkflowFailures

`CaseInsights-WorkflowFailures-<uniqueId>` holds one record per failed case-analysis attempt and per conversation too large to summarise. Each record has `failureType` (`WorkflowTaskFailed`, `InvalidWorkflowInput`, `ConversationTooLarge` or `WorkflowExecutionClosed`), `stage`, `errorType`, `retryable`, the case reference fields and, for workflow attempts, `executionName` and `deliveryKey`.

- Delivery is at least once: a record can appear twice with the same `deliveryKey` (the execution name) after a crash or an unconfirmed marker update. Treat records with the same `deliveryKey` as one failure.
- `ConversationTooLarge` (`retryable: false`) is not retried automatically while the conversation is unchanged.
- Other failures are retried by the next scheduled run. To retry sooner, send the record's case reference fields to the summary queue with a new `redriveId` (see the README section *Failure records and redrive*).
- After handling a record, delete it from the queue so the alarm returns to OK. A growing backlog with `WorkflowExecutionsTimedOut` usually means the Bedrock concurrency cap is too small for the volume.

### QueueMessages-AccountDiscoveryFailures

Schedule deliveries or asynchronous invocations of account lookup or account reader that exhausted their retries. Each message includes the original event. Fix the cause (see [LambdaErrors-AccountLookup](#lambdaerrors-accountlookup) and [LambdaErrors-AccountReader](#lambdaerrors-accountreader)) and invoke account lookup manually to start a fresh run.

## Recovery tools

Two scripts change pipeline state on purpose. Both check that they target the right deployment before reading anything, show what they would do, and change nothing until you confirm with the token from that preview. Both run from the repository root with credentials for the management account, in the Region of `config/<env>.json`. Neither script deletes any S3 object version, writes to AWS Support, or calls Bedrock.

| Situation | Tool |
| --- | --- |
| Messages in a stage dead-letter queue (active accounts, annotation or summary) after the cause is fixed | `scripts/redrive-dlq.sh` |
| A record on the workflow failure queue to retry before the next scheduled run | Send its case reference with a new `redriveId` to the summary queue (see [QueueMessages-WorkflowFailures](#queuemessages-workflowfailures)) |
| A discovery failure | Invoke account lookup (see [QueueMessages-AccountDiscoveryFailures](#queuemessages-accountdiscoveryfailures)) |
| Committed cases to analyse again, for example after a prompt or model change | `scripts/reprocess-cases.py` |

Redrive and the failure-record retry never analyse a committed case again: the starter, every stage and case retrieval answer a reference to a case with a processed record as already processed. Reprocessing is the only deliberate exception.

### Redrive a stage dead-letter queue

`scripts/redrive-dlq.sh --env <env> --queue <active-accounts|annotation|summary>` starts one SQS message move task (`StartMessageMoveTask`) from `CaseInsights-<Stage>-<uniqueId>-DLQ` to `CaseInsights-<Stage>-<uniqueId>`. It needs the AWS CLI v2 and `python3`. Run `scripts/redrive-dlq.sh --help` for every option.

Before anything moves, the script:

1. reads `config/<env>.json` and refuses unknown queues; the workflow-failures and account-discovery-failures terminal queues have no source queue and are refused with the right procedure;
2. checks that the credentials belong to `managementAccountId`;
3. looks up both queues by their deployment names in that account and Region, checks both ARNs, and checks that the dead-letter queue is the stage queue's `RedrivePolicy` target, so a queue of another deployment or environment is never used;
4. refuses to start while a move task from the dead-letter queue is RUNNING or CANCELLING;
5. prints both queues, the approximate number of visible and in-flight messages and a redrive token. The token is derived from the account, Region and queue pair, so a token from another environment or queue is rejected.

`--dry-run` stops after the preview. Without it, the script asks for the token, or takes it from `--confirm` (non-interactive use requires `--confirm`). An empty dead-letter queue needs no redrive and nothing is started.

```text
$ scripts/redrive-dlq.sh --env dev --queue summary --dry-run
Redrive preview for deployment scidev (env dev, account 111111111111, us-east-1)
From dead-letter queue: CaseInsights-CaseSummary-scidev-DLQ (arn:aws:sqs:us-east-1:111111111111:CaseInsights-CaseSummary-scidev-DLQ)
To stage queue:         CaseInsights-CaseSummary-scidev (arn:aws:sqs:us-east-1:111111111111:CaseInsights-CaseSummary-scidev)
Messages in the dead-letter queue (approximate): 7 visible, 1 in flight
Move rate: chosen by SQS
Redrive token: rd-<token>
Dry run: no changes were made. To start the redrive, rerun with --confirm rd-<token>.

$ scripts/redrive-dlq.sh --env dev --queue summary --confirm rd-<token>
...
Started move task 0b1d7c1e-example-task-handle.
Task status: RUNNING (2 of about 8 messages moved)
Moving a message back to CaseInsights-CaseSummary-scidev does not mean it was processed. ...
Progress: aws sqs list-message-move-tasks --source-arn arn:aws:sqs:us-east-1:111111111111:CaseInsights-CaseSummary-scidev-DLQ --region us-east-1
Cancel:   aws sqs cancel-message-move-task --task-handle 0b1d7c1e-example-task-handle --region us-east-1
```

(Account and task handle are invented; `rd-<token>` stands for the token the preview prints.)

Messages are moved unchanged, so every reference keeps its account, case, run and redrive identity; for a summary reference whose earlier attempt closed, the starter recovers it under the next attempt name of the same run. A started task only means SQS is moving the messages: watch the dead-letter queue depth, the stage function's logs and the alarms. Messages that fail again return to the dead-letter queue after `operations.queues.maxReceiveCount` receives.

Exit status: 0 started (task RUNNING or COMPLETED) or nothing to redrive; 1 an AWS request failed, the task was not started, the task reports FAILED or CANCELLED, or its status could not be read after starting (do not start another; check with the printed command); 2 invalid options, configuration or target; 3 cancelled or not confirmed. AWS error messages are not printed, only their error codes.

Permissions: `sts:GetCallerIdentity`; `sqs:GetQueueUrl` and `sqs:GetQueueAttributes` on both queues; `sqs:ListMessageMoveTasks`, `sqs:StartMessageMoveTask`, `sqs:ReceiveMessage`, `sqs:DeleteMessage` and `sqs:GetQueueAttributes` on the dead-letter queue; `sqs:SendMessage` on the stage queue; `sqs:CancelMessageMoveTask` on the dead-letter queue to cancel. The queues use SSE-SQS, so no KMS permissions are needed.

### Reprocess committed cases

`scripts/reprocess-cases.py --env <env> --from YYYY-MM-DD --to YYYY-MM-DD` makes committed cases in an explicit date range eligible for analysis again. Run it with the repository's virtual environment (`.venv/bin/python scripts/reprocess-cases.py ...`); `--help` lists every option.

Selection:

- **Date field.** `--date-field timeCreated` (default) uses the Support case creation time stored in the processed record; `--date-field Case_Retrieval_Date` uses the time the pipeline retrieved the case for the committed analysis.
- **Time zone and bounds.** Values are converted to UTC. `--from` and `--to` are UTC calendar days and both are inclusive: the range is `[--from 00:00:00Z, day after --to 00:00:00Z)`. A record at `2026-03-31T23:59:59Z` is inside `--to 2026-03-31`; one at `2026-04-01T00:00:00Z` is not.
- **No implicit scope.** Both dates are required and must be valid; a reversed range is refused. There is no "all records" mode, and a range that matches nothing changes nothing. `--account-id` (repeatable) and `--case` (with exactly one `--account-id`) narrow the scope further. Plans that would change more than `--max-cases` cases (default 100, at most 5000) are refused.
- **Targets.** The credentials must belong to `managementAccountId`. Buckets, the summary queue and the state machine come from the `RawBucketName`, `ProcessedBucketName`, `CaseSummaryQueueUrl` and `CaseAnalysisStateMachineArn` outputs of `CaseInsightsCoreStack-<uniqueId>`; the queue and state machine must carry this deployment's names, and both buckets must have versioning enabled.

For each selected case the preview shows one action:

- **requeue**: the raw `data.json` and `communications.json` are still present and consistent, so a new execution can analyse them.
- **rediscover**: the raw inputs have expired, but the case was created inside the collection lookback window (with two days to spare), so the next scheduled run collects it again.
- **skip**: anything else, left unchanged. This includes a RUNNING execution of the case, an execution in PENDING_REDRIVE, an ownership marker naming an execution that is being started (claimed less than an hour ago), a Case Cleanup reset in progress, an unreadable marker or a failed status lookup (status unknown), and expired raw inputs outside the lookback window.

`--dry-run` stops after the preview and prints the plan token. Without it, the script asks for the token, or takes it from `--confirm`. The token covers the exact records and processed-record versions in the preview, so if anything changed, run a new preview.

After confirmation, each case is handled on its own:

1. The script checks again that the processed record has the previewed version and that no work became active; otherwise the case is skipped.
2. For **requeue**, it sends a summary reference with a delivery delay (`--delay-seconds`, default 60) under a fresh run ID (`<timestamp>-<random>`) and a redrive ID `reprocess-...`. The new run makes the Workflow Starter start attempt 1 of a new execution name, so a closed execution name is never reused.
3. It removes the stage checkpoints, then the processed record (conditional on the previewed ETag, so a record rewritten meanwhile is kept). If removal finished close to or after the delay, it sends one follow-up reference (redrive ID `<redrive ID>-f`); duplicate references never start a second execution.

A reference that arrives while the processed record still exists is answered as already processed, so a run that stops before removing a record leaves that case as it was. If a follow-up reference cannot be sent, the report prints the exact message to send to the summary queue. If the script is killed after removing a record but before a needed follow-up, the case has no processed record and may have no pending reference: send its case reference to the summary queue with a new `redriveId`, or let Case Cleanup and the next scheduled run collect it again while it is inside the lookback window. The report lists every case as `requeued`, `rediscover`, `skipped` or `failed`, with its previous processed version ID, and exits 1 when any case failed. A queued reference only starts a new execution: check the CaseAnalysis executions, the workflow failure queue and the processed record for the result.

Versioned objects: the processed record and checkpoints are removed with plain deletes, which add S3 delete markers. No version is deleted by version ID, so an older record can never become current again. The previous processed version stays restorable until `operations.storage.noncurrentVersionRetentionDays` expires it: to restore one, copy that version over the current key (`aws s3api copy-object --copy-source '<bucket>/<key>?versionId=<id>' --bucket <bucket> --key <key>`). Until a case is committed again it has no processed record, so Athena and the MCP server do not return it, and Case Cleanup may reset it like any other uncommitted case once its inputs are older than the cleanup grace; the next scheduled run then collects it again while it is inside the lookback window.

```text
$ .venv/bin/python scripts/reprocess-cases.py --env dev --from 2026-03-01 --to 2026-03-31 --dry-run
Reprocess preview for deployment scidev (env dev, account 111111111111, us-east-1)
Date field: timeCreated (UTC) from 2026-03-01T00:00:00Z inclusive to 2026-04-01T00:00:00Z exclusive (2026-03-01 to 2026-03-31 inclusive)
Scope: every account
Examined 4 processed records: 3 in range, 1 outside the range (unchanged), 0 unreadable (unchanged).
ACTION      ACCOUNT      CASE                 timeCreated          PROMPT              PROCESSED VERSION                  REASON
requeue     111122223333 170000000001         2026-03-01T00:00:00Z pv-0123456789abcdef 49d7bc84-9fa0-4923-b0b3-b314a94b53cc raw inputs available; a new execution is queued
skip        111122223333 170000000004         2026-03-15T09:30:00Z pv-0123456789abcdef 666631b3-55bf-42ee-8fc8-32fa54394e92 a RUNNING CaseAnalysis execution belongs to the case
requeue     444455556666 170000000002         2026-03-31T23:59:59Z pv-0123456789abcdef 4b9af83f-7ba1-4f1b-9c01-05429aecd6bb raw inputs available; a new execution is queued
Plan token: rp-<token> (2 case(s) to change)
Dry run: no changes were made. To execute this plan, rerun with --confirm rp-<token>.

$ .venv/bin/python scripts/reprocess-cases.py --env dev --from 2026-03-01 --to 2026-03-31 --confirm rp-<token>
...
Reprocess run ID 20261114T120000Z-ef77f0ed, redrive ID reprocess-20261114120000-ef77f0ed
requeued    111122223333 170000000001 previous version 49d7bc84-9fa0-4923-b0b3-b314a94b53cc: record removed; new execution queued
requeued    444455556666 170000000002 previous version 4b9af83f-7ba1-4f1b-9c01-05429aecd6bb: record removed; new execution queued
Totals: requeued 2
Queued references only start new executions; check the CaseAnalysis executions, the workflow failure queue and the processed records for the actual results.
```

(Accounts, cases and versions are invented; `rp-<token>` stands for the token the preview prints.)

Exit status: 0 done or nothing to do; 1 some cases failed; 2 invalid options, configuration or target; 3 cancelled or not confirmed.

Permissions: `sts:GetCallerIdentity`; `cloudformation:DescribeStacks` on `CaseInsightsCoreStack-<uniqueId>`; `s3:GetBucketVersioning` on the raw and processed buckets; `s3:ListBucket`, `s3:GetObject` and `s3:DeleteObject` on the processed bucket; `s3:GetObject` on the raw bucket (`data.json`, `communications.json`, `workflow.json`, `checkpoints/*`) and `s3:DeleteObject` on `account_number=*/case_number=*/checkpoints/*`; `sqs:GetQueueAttributes` and `sqs:SendMessage` on the summary queue; `states:ListExecutions` on the state machine and `states:DescribeExecution` on its executions. The buckets use SSE-S3, so no KMS permissions are needed.

## Failure records and reconciliation

### WorkflowFailuresRecorded

Failure records were written to the workflow failure queue: by HandleFailure (`WorkflowFailuresRecorded`), by annotation for conversations too large to summarise (`OversizedConversations`), or by reconciliation for timed-out, aborted or unrecorded failed executions (`FailureDeliveriesSent` from cleanup and run completion). Follow [QueueMessages-WorkflowFailures](#queuemessages-workflowfailures).

### FailureDeliveryErrors

A failure record could not be sent (`WorkflowFailureDeliveryErrors` from HandleFailure, `FailureDeliveryErrors` from cleanup or run completion). The evidence is kept: HandleFailure is retried by Step Functions, and reconciliation leaves the pending marker under `failure-deliveries/` and retries it on the next run. Check `sqs:SendMessage` on the workflow failure queue and SQS availability. A pending marker never stops a later run from retrying the case.

### CleanupErrors

Case Cleanup could not evaluate or reset some cases, or could not reconcile a failure record before removing ownership of a closed failed execution. Nothing is deleted when a check fails. Check the cleanup log for the `error_type` of each failure; the next daily run retries.

### RunCompletionErrors

For three consecutive hours, Run Completion could not read some run markers, describe an execution named by a case marker, or deliver a failure record. Affected runs stay incomplete. Look for `Run could not be checked` or `Case outcome could not be reconciled` warnings with `run_id`, `account_id` and `display_id`. A case marker naming an execution that no longer exists (Step Functions keeps closed executions for 90 days) keeps its run incomplete until the run leaves the lookback window.

## Daily completion

### MissingDailyRunCompletion

No daily run completed in the last 30 hours. Run Completion emits `DailyRunCompleted` only when every account in a run's account list has an account marker (`runs/<runId>/accounts/`), every case those accounts queued has a case marker (`runs/<runId>/cases/`), and every case reached a terminal outcome: skipped, already processed, a terminal oversized conversation, a succeeded execution, or a closed failed, timed-out or aborted execution whose failure record is confirmed on the workflow failure queue. Account discovery alone never emits it. Runs with no accounts, only skipped accounts or no cases complete on the next hourly check.

1. Check that an account list was written for the expected run in `account-lists/` of the account-list bucket. If not, follow [LambdaErrors-AccountLookup](#lambdaerrors-accountlookup) and the discovery failure queue.
2. Check the dashboard *Run completion* widget: `RunsIncomplete` and `RunCasesPending` show whether work is still in progress.
3. Look for messages waiting in the stage queues or dead-letter queues and for RUNNING `CaseAnalysis` executions.
4. Check the Run Completion log for `Run is not complete yet` entries, which give pending account and case counts per run, and for [RunCompletionErrors](#runcompletionerrors).
5. When the backlog drains, the next hourly check completes the run and the alarm returns to OK. Runs older than 72 hours are no longer checked; the following day's run restores the heartbeat.

## MCP server

These alarms exist only when `enableMcp` is true. They live in the core stack and watch the MCP function `Lambda-McpServer-<uniqueId>`, the REST API `CaseInsights-Mcp-<uniqueId>` (stage `v1`) and its access log group `/aws/apigateway/CaseInsights-Mcp-<uniqueId>/access` by name. The MCP function logs the JSON-RPC method, tool name, outcome, status and duration of each request (`mcp_method`, `mcp_tool`, `mcp_outcome`, `status_code`, `tool_duration_ms`), never request bodies, tool arguments, SQL text or results. The dashboard rows *MCP requests and errors*, *MCP API responses (stage v1)* and *MCP WAF blocked requests* show the related metrics.

### LambdaErrors-McpServer

`Lambda-McpServer-<uniqueId>` reported invocation errors: an unhandled exception, a timeout (28 seconds) or running out of memory. Callers receive a 502 or 504 response from API Gateway. Check the function log group for `ERROR` entries and `Task timed out`, check that the Core common layer and the Analytics stack are deployed, and check the reserved concurrency, which is fixed at 20: when all 20 executions are busy, API Gateway returns 429 or 5xx throttling responses instead.

### McpInternalErrors

The MCP server caught an unexpected failure and answered with a JSON-RPC internal error (`-32603`) without details. Query the function log for `MCP request failed unexpectedly` entries and their `mcp_method`, `mcp_tool` and `error_type` fields. Common causes are Athena errors in the workgroup `CaseInsights-<uniqueId>` (missing Glue table or partitions, results bucket access), Bedrock model access or Marketplace subscription problems, and throttling. Tool argument errors are reported as tool errors (`McpToolErrors`), not internal errors.

### McpApiServerErrors

The MCP API returned 5xx responses on stage `v1`. Compare with [LambdaErrors-McpServer](#lambdaerrors-mcpserver): integration failures and timeouts appear as 502 or 504 with `integrationStatus` in the access log. A 5xx without an integration status means API Gateway itself failed or the function was throttled; check the access log `errorType` field and the AWS Health Dashboard.

### McpAuthFailures

At least 10 requests in five minutes were refused before reaching the function because SigV4 authentication or the resource policy failed (`McpAuthFailures`, from access log entries whose `errorType` is `ACCESS_DENIED`, `EXPIRED_TOKEN`, `INVALID_SIGNATURE`, `MISSING_AUTHENTICATION_TOKEN` or `UNAUTHORIZED`). Requests blocked by AWS WAF are counted separately in the WAF widget.

1. Query the access log group for refused requests, for example `fields @timestamp, errorType, sourceIp, callerAccountId, status | filter ispresent(errorType) and status in [401, 403] | stats count() by errorType, callerAccountId, sourceIp`.
2. `EXPIRED_TOKEN` or `INVALID_SIGNATURE` from a known account usually means a client with stale credentials or a clock skew; refresh the credentials used by `scripts/mcp_aws_client.py`.
3. `ACCESS_DENIED` means the caller is not in `mcpAllowedPrincipals` or not in the Organization `orgId`. Add an approved principal to the configuration and redeploy; never widen the policy to an account root or `*`.
4. A stream of refusals from unknown source IPs is probing. The WAF rate rule limits each IP to `operations.mcp.wafRateLimitPer5Minutes` requests per five minutes; no further action is needed unless the volume affects legitimate callers.

## MCP access logs are missing

No alarm covers this directly; an empty access log group also leaves `McpAuthFailures` without data. REST API access logs need the account- and Region-wide API Gateway CloudWatch role setting described in [Shared API Gateway logging prerequisite](mcp-server-deployment.md#shared-api-gateway-logging-prerequisite).

1. Run `aws apigateway get-account --region <region> --query cloudwatchRoleArn --output text`. In managed mode it must equal the `ApiGatewayCloudWatchRoleArn` output of `CaseInsightsApiLoggingStack-<sharedId>`; in existing mode it must equal `apiGatewayLogging.existingRoleArn` (also the MCP stack's `ApiGatewayCloudWatchRoleArn` output).
2. If it shows another role, someone changed the account setting. Agree ownership with that party before changing it back: either redeploy the shared stack (managed mode) or switch the configuration to existing mode with their role and ask for write access to `/aws/apigateway/CaseInsights-Mcp-*`.
3. If it is empty, redeploy with managed mode, or ask the owner of the existing role to set it.
4. Check that the role still exists, trusts `apigateway.amazonaws.com` and allows `logs:CreateLogStream` and `logs:PutLogEvents` on `/aws/apigateway/CaseInsights-Mcp-*`, and that AWS STS is active in the Region.

# Alarm runbook

This runbook lists every alarm the application creates (23 without MCP, 27 with MCP, all in the Core stack), with first diagnosis and repair steps, the [operational procedures](#operational-procedures) for adding an OU, changing quotas, changing the model and recovering a failed deployment, and the [recovery tools](#recovery-tools) for dead-letter queue redrive and opt-in reprocessing. The alarm inventory here is checked against the synthesised alarms by `test/cdk/constructs/observability.test.ts` and `test/cdk/stacks/mcp-stack.test.ts`. Thresholds, metrics and the dashboard are described in [Monitoring](monitoring.md), log fields and Logs Insights queries in [Logging](logging.md), and symptoms that do not raise an alarm in [Troubleshooting](troubleshooting.md).

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

For every function: check the function's log group (`/aws/lambda/<function name>`) for `ERROR` entries and the `error_type` field, check recent deployments and configuration changes (a failed or partial deployment is covered in [Recover from a failed deployment or update](#recover-from-a-failed-deployment-or-update)), and check service quotas (Lambda concurrency, S3, SQS, Step Functions, Bedrock) as in [Change a Bedrock or Lambda quota](#change-a-bedrock-or-lambda-quota).

### LambdaErrors-AccountLookup

`Lambda-AccountLookup-<uniqueId>` could not list Organizations accounts or write the account list. Check that the stack is deployed in the management account, that Organizations is reachable, and that the account-list bucket exists. Failed scheduled invocations land in `CaseInsights-AccountDiscoveryFailures-<uniqueId>` after retries. To start the run again, invoke the function manually (a manual invocation gets its own run ID).

### LambdaErrors-AccountReader

`Lambda-AccountReader-<uniqueId>` could not read or validate an account list or enqueue accounts. An `InvalidAccountList` error means the object did not match the contract; inspect it in `account-lists/`. Failed events land in `CaseInsights-AccountDiscoveryFailures-<uniqueId>`.

### LambdaErrors-CaseRetrieval

`Lambda-CaseRetrieval-<uniqueId>` failed for a whole batch. Check for errors assuming `Support-Case-Analysis-Role-<uniqueId>` (StackSet instance missing in a new account or OU; see [Collect from a new organisational unit or account](#collect-from-a-new-organisational-unit-or-account)), Support API throttling, or raw-bucket write errors. Accounts without a Business, Enterprise On-Ramp or Enterprise Support plan are skipped, not errors.

### LambdaErrors-CaseAnnotation

`Lambda-CaseAnnotation-<uniqueId>` failed for a whole batch. Check member-role access, `DescribeCommunications` throttling and raw-bucket writes.

### LambdaErrors-WorkflowStarter

`Lambda-WorkflowStarter-<uniqueId>` could not start or describe executions. Check `states:StartExecution` throttling, the ownership markers (`workflow.json`) and the idempotency table. `CaseResetInProgressError` means Case Cleanup or [the reprocess script](#reprocess-committed-cases) holds a reset claim on the case; the record is retried and starts once the reset finishes or its claim is an hour old. Each such retry uses up one receive, so with `operations.queues.maxReceiveCount` 1 the reference goes to the summary dead-letter queue instead (see [QueueMessages-SummaryDLQ](#queuemessages-summarydlq)). Messages are retried and land in the summary dead-letter queue when their receive count is exhausted.

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

Executions exceeded `operations.workflow.executionTimeoutMinutes`. A whole-execution timeout bypasses the task error handling, so HandleFailure does not run. Run Completion (hourly) and Case Cleanup (daily) preserve a pending failure-delivery marker under `account_number=<id>/case_number=<displayId>/failure-deliveries/<executionName>.json` and send a `WorkflowExecutionClosed` record to the workflow failure queue. Check for a large backlog with a small `bedrockMaxConcurrency` (executions wait for stage capacity). Raise `operations.workflow.executionTimeoutMinutes` (keeping it below `operations.cleanup.graceHours` x 60), or raise the concurrency cap within the Bedrock quota as described in [Change a Bedrock or Lambda quota](#change-a-bedrock-or-lambda-quota), then deploy.

### WorkflowExecutionsAborted

Executions were stopped manually or by a deployment. Their failures are recorded by reconciliation in the same way as timeouts. If the stop was deliberate, no action is needed; the next run retries the cases.

## Dead-letter and terminal queues

Every dead-letter queue and terminal queue alarms when it holds at least one visible message. The queues have no consumer. Messages contain references (account ID, case ID, display ID, run ID, object keys), never case content. Keep messages until the cause is fixed, then redrive or delete them explicitly.

### QueueMessages-ActiveAccountsDLQ

Account messages for which case retrieval failed `operations.queues.maxReceiveCount` times. Check the [LambdaErrors-CaseRetrieval](#lambdaerrors-caseretrieval) causes, then redrive the messages to `CaseInsights-ActiveAccounts-<uniqueId>` with `scripts/redrive-dlq.sh --env <env> --queue active-accounts` (see [Redrive a stage dead-letter queue](#redrive-a-stage-dead-letter-queue)). The account's run stays incomplete until it is retrieved or the run leaves the run-completion lookback window.

### QueueMessages-AnnotationDLQ

Case references for which annotation failed repeatedly. Fix the cause and redrive to `CaseInsights-CaseAnnotation-<uniqueId>` with `scripts/redrive-dlq.sh --env <env> --queue annotation`.

### QueueMessages-SummaryDLQ

Annotated case references the workflow starter could not start. Fix the cause and redrive to `CaseInsights-CaseSummary-<uniqueId>` with `scripts/redrive-dlq.sh --env <env> --queue summary`. Committed cases among them are answered as already processed. A reference that the starter failed while a reset claim was held (`CaseResetInProgressError`) can be redriven once the claim is released; if a reprocess run has already queued its own reference for that case, the redriven one is a harmless duplicate and never starts a second execution.

### QueueMessages-WorkflowFailures

`CaseInsights-WorkflowFailures-<uniqueId>` holds one record per failed case-analysis attempt and per conversation too large to summarise. Each record has `failureType` (`WorkflowTaskFailed`, `InvalidWorkflowInput`, `ConversationTooLarge` or `WorkflowExecutionClosed`), `stage`, `errorType`, `retryable`, the case reference fields and, for workflow attempts, `executionName` and `deliveryKey`.

- Delivery is at least once: a record can appear twice with the same `deliveryKey` (the execution name) after a crash or an unconfirmed marker update. Treat records with the same `deliveryKey` as one failure.
- `ConversationTooLarge` (`retryable: false`) is not retried automatically while the conversation is unchanged.
- Other failures are retried by the next scheduled run. To retry sooner, send the record's case reference fields (`accountId`, `caseId`, `displayId`, `runId`, `rawKey`, `communicationsKey`, `communicationsVersionId`, `communicationCount`) to `CaseInsights-CaseSummary-<uniqueId>` with a new `redriveId` (1 to 64 letters, digits or hyphens). The workflow starter then starts the next attempt; the processed record still prevents any rework of a committed case.
- After handling a record, delete it from the queue so the alarm returns to OK. A growing backlog with `WorkflowExecutionsTimedOut` usually means the Bedrock concurrency cap is too small for the volume.

### QueueMessages-AccountDiscoveryFailures

Schedule deliveries or asynchronous invocations of account lookup or account reader that exhausted their retries. Each message includes the original event. Fix the cause (see [LambdaErrors-AccountLookup](#lambdaerrors-accountlookup) and [LambdaErrors-AccountReader](#lambdaerrors-accountreader)) and invoke account lookup manually to start a fresh run.

## Operational procedures

These procedures change the deployment itself. Each one edits `config/<env>.json` or the account, then runs the normal review and deployment. Run them from the repository root with credentials for the management account. Replace `<env>`, `<uniqueId>` and `<region>` with the values from `config/<env>.json`. Try each change in dev before prod. If the [optional pipeline](pipeline.md) deploys your environments, commit the configuration change to `main` instead of running `npx cdk deploy`. The pipeline then deploys dev, and prod after approval. None of these procedures has been run against a real account.

### Collect from a new organisational unit or account

`memberRoleTargets` accepts only the organisation root (`r-...`) and OU IDs (`ou-...`). An OU target includes its child OUs. An account that joins a targeted OU later receives the member role automatically, so it needs no change here. To collect from a new OU:

1. Find the OU ID, then list its accounts. Organizations commands need no Region:

   ```bash
   aws organizations list-roots --query 'Roots[].Id' --output text
   aws organizations list-organizational-units-for-parent --parent-id <parent-id> \
     --query 'OrganizationalUnits[].[Id,Name]' --output table
   aws organizations list-accounts-for-parent --parent-id <ou-id> \
     --query 'Accounts[].[Id,Name,State]' --output table
   ```

2. Check the prerequisites in the new accounts. Each account needs a Business, Enterprise On-Ramp or Enterprise Support plan; accounts without one are skipped and counted under `AccountsSkippedSubscriptionRequired`. No service control policy may deny `iam:CreateRole` to the StackSet (needed to create the role) or `sts:AssumeRole` and the AWS Support API to the member role (see [Accounts skipped: AccessDenied](troubleshooting.md#accounts-skipped-accessdenied)).
3. Add the OU ID to `memberRoleTargets` in `config/<env>.json`. Keep the existing entries. A target that you remove loses its member roles.
4. Review and deploy:

   ```bash
   npm run verify
   npx cdk diff --all -c env=<env>
   npx cdk deploy --all -c env=<env> --require-approval broadening
   ```

   The diff should change only the deployment targets of the StackSet `CaseInsights-MemberRole-<uniqueId>` in `CaseInsightsMemberRoleStack-<uniqueId>`.
5. Confirm that every new account has a `CURRENT` instance:

   ```bash
   aws cloudformation list-stack-instances --region <region> \
     --stack-set-name CaseInsights-MemberRole-<uniqueId> --call-as SELF \
     --query 'Summaries[].[Account,Status,StackInstanceStatus.DetailedStatus]' --output table
   ```

   The StackSet tolerates failures in up to 10% of accounts, so the stack deployment can succeed while some instances fail. If an instance is not `CURRENT`, or a new account has no instance at all, follow the StackSet steps in [Recover from a failed deployment or update](#recover-from-a-failed-deployment-or-update), which compare the expected accounts with the existing instances.
6. After the next daily run, check that the new accounts were retrieved, not skipped: the [skipped accounts query](logging.md#5-skipped-accounts-by-reason) on the Case Retrieval log group lists each skipped `account_id` with its reason, which the aggregate `AccountsSkippedAccessDenied` metric cannot show per account. Their first run collects up to `lookbackMonths` of resolved cases. A large OU therefore causes a one-off backlog and Bedrock cost, like the first backfill (see [cost](cost.md#illustrative-monthly-scenario)).

To add a single account, move it into a targeted OU in AWS Organizations. The member role is created there automatically. An account outside every target is never collected.

### Change a Bedrock or Lambda quota

The analysis stages make at most `bedrockMaxConcurrency` concurrent Bedrock calls (1 to 50) through the global inference profile in `bedrockModelId`. The MCP function reserves a further 20 concurrent executions when MCP is enabled. Change the quota first, then the configuration:

1. Look up the current Bedrock quotas for the configured model. The tokens-per-minute and requests-per-minute quotas for global cross-Region inference are the ones that throttle the application:

   ```bash
   aws service-quotas list-service-quotas --region <region> --service-code bedrock \
     --query "Quotas[?contains(QuotaName, 'Claude Haiku 4.5')].[QuotaName,QuotaCode,Value,Adjustable]" --output table
   ```

   Use `Claude Sonnet 4.5` in the filter for the Sonnet model.
2. Request an increase for an adjustable quota, then follow the request until it is approved. If the quota is not adjustable, open a case with AWS Support:

   ```bash
   aws service-quotas request-service-quota-increase --region <region> --service-code bedrock \
     --quota-code <quota-code> --desired-value <value>
   aws service-quotas list-requested-service-quota-change-history --region <region> --service-code bedrock \
     --query 'RequestedQuotas[].[QuotaName,DesiredValue,Status]' --output table
   ```

3. Check that Lambda has room for the reserved concurrency. Lambda keeps at least 100 concurrent executions unreserved, so the account's `ConcurrentExecutions` must be at least `bedrockMaxConcurrency` plus 20 (with MCP) plus 100, plus any reserved concurrency of other functions. Raise the Lambda quota `L-B99A9384` (concurrent executions) the same way, with `--service-code lambda`, if needed:

   ```bash
   aws lambda get-account-settings --region <region> \
     --query 'AccountLimit.[ConcurrentExecutions,UnreservedConcurrentExecutions]' --output text
   ```

4. Once the new quota is in place, change `bedrockMaxConcurrency` in `config/<env>.json`. To start analyses faster, you can also raise `operations.workflow.starterMaxConcurrency` (2 to 50). Keep `operations.workflow.executionTimeoutMinutes` below `operations.cleanup.graceHours` x 60. Validation rejects any other combination (see [configuration](configuration.md)).
5. Run `npm run verify`, `npx cdk diff --all -c env=<env>` and `npx cdk deploy --all -c env=<env>`. The diff changes the reserved concurrency of `Lambda-CaseAnalysisStages-<uniqueId>` and, if you changed it, the event source of the workflow starter.
6. Watch `BedrockThrottles` and the *Bedrock tokens* widget on the dashboard during the next run. If throttling rises, lower `bedrockMaxConcurrency` again. Throttled calls are retried, so a too-high setting slows analysis but loses no cases.

To lower a quota, for example after a quota reduction by AWS, lower `bedrockMaxConcurrency` and deploy first.

### Change the Bedrock model

`bedrockModelId` accepts only `global.anthropic.claude-haiku-4-5-20251001-v1:0` (default) and `global.anthropic.claude-sonnet-4-5-20250929-v1:0`. Using another model needs a code change. Sonnet 4.5 costs roughly three times as much per token (see [cost](cost.md#amazon-bedrock)).

1. Prepare the new model in the management account before you deploy. Complete its Anthropic first-time use form if the account has not submitted one, and confirm a valid payment method for its AWS Marketplace subscription (see [Step 4 of the installation](installation.md#step-4-prepare-the-account-once)). Check its quotas as in [Change a Bedrock or Lambda quota](#change-a-bedrock-or-lambda-quota). Confirm that the profile is offered in the Region:

   ```bash
   aws bedrock list-inference-profiles --region <region> \
     --query "inferenceProfileSummaries[].inferenceProfileId" --output text
   ```

2. Change `bedrockModelId` in `config/<env>.json`.
3. Run `npm run verify`, then `npx cdk diff --all -c env=<env>`. Expect changes to the Bedrock policy of the stages and MCP function roles and to their model setting. Deploy with `npx cdk deploy --all -c env=<env> --require-approval broadening`.
4. Watch the next run. `AccessDeniedException` failures in the [Bedrock failures query](logging.md#7-bedrock-failures-by-error-code) mean that model access or the subscription is still missing. Fix them, and the next run retries the affected cases.
5. Committed cases keep their `Model_Id` and are not analysed again. Stage checkpoints written by the old model are ignored. To analyse earlier cases with the new model, use [Reprocess committed cases](#reprocess-committed-cases) with `--dry-run` first, and choose the narrowest date range you need, because each case incurs Bedrock charges again.

To go back, set the previous `bedrockModelId` and deploy again. Cases committed in the meantime keep the model that analysed them.

### Recover from a failed deployment or update

CloudFormation rolls a failed update back to the previous version of the stack, so the previous application normally keeps running. Committed cases and processed records are not affected by a rollback.

1. Find the failing stack, the failed operation and the first resource that failed in it. The `npx cdk deploy` output names the stack; use its full name, for example `CaseInsightsCoreStack-<uniqueId>`. Do this before you retry, continue a rollback or deploy again, because each of those starts a new operation. Stack events cover the stack's whole history, newest first, so a failure from an earlier deployment can appear next to the current one; isolate the failed operation first:

   ```bash
   aws cloudformation describe-stacks --region <region> --stack-name <stack-name> \
     --query 'Stacks[0].[StackStatus,StackStatusReason]' --output text
   aws cloudformation describe-stack-events --region <region> --stack-name <stack-name> --max-items 1 \
     --query 'StackEvents[0].[Timestamp,LogicalResourceId,ResourceStatus,ClientRequestToken]' --output text
   ```

   The newest event is the final status of the failed operation (for example `UPDATE_ROLLBACK_COMPLETE` on the stack itself), and its last field is that operation's client request token. CloudFormation gives every event of an operation, including its automatic rollback, the same token: `npx cdk deploy` uses `exec<uuid>`, the console `Console-<operation>-<uuid>`. List only that operation's failures, oldest first, replacing `<client-request-token>` with the token:

   ```bash
   aws cloudformation describe-stack-events --region <region> --stack-name <stack-name> \
     --query "sort_by(StackEvents[?ClientRequestToken=='<client-request-token>' && ends_with(ResourceStatus, 'FAILED')], &Timestamp)[].[Timestamp,LogicalResourceId,ResourceStatus,ResourceStatusReason]" \
     --output table
   ```

   The first row is the first resource failure of the operation and normally the cause; later rows are usually resources that CloudFormation cancelled or could not roll back because of it. Use `--output table` (or `json`) as shown: with `--output text`, the AWS CLI applies `--query` to each page of events separately, so the sorting would not cover the whole operation. If the token is `None`, the operation was started without one. Then find where it began instead: list the stack's own events with `aws cloudformation describe-stack-events --region <region> --stack-name <stack-name> --query "StackEvents[?LogicalResourceId=='<stack-name>'].[Timestamp,ResourceStatus,ResourceStatusReason]" --output table`. The newest `CREATE_IN_PROGRESS` or `UPDATE_IN_PROGRESS` row with the reason `User Initiated` is the start of the failed operation, and only failures with a later timestamp belong to it. [Troubleshooting](troubleshooting.md#deployment) lists the common causes.
2. Act on the stack status:

   | Status | Meaning | Action |
   | --- | --- | --- |
   | `UPDATE_ROLLBACK_COMPLETE` | The update failed and the previous version is back in place | Fix the cause and deploy again. To return the repository to the deployed version instead, check out the last deployed commit and run `npm run verify` and `npx cdk deploy --all -c env=<env>`, so the code and the deployment agree |
   | `UPDATE_ROLLBACK_FAILED` | CloudFormation could not restore a resource, often one that was changed or deleted outside CloudFormation | Fix that resource, then run `aws cloudformation continue-update-rollback --region <region> --stack-name <stack-name>`. Add `--resources-to-skip <logical-id>` only for a resource you have checked is already in the intended state, because a skipped resource is no longer in step with the template |
   | `ROLLBACK_COMPLETE` or `ROLLBACK_FAILED` | The first creation failed. The stack cannot be updated | Delete the stack and deploy again. In dev, run `npx cdk destroy <stack-name> -c env=dev`. In prod, run `aws cloudformation delete-stack --region <region> --stack-name <stack-name>`. Prod keeps its buckets, table and log groups, so a new creation can fail with *already exists*. A prod stack whose first creation failed has processed no cases. Check that the named resource holds no data before you delete it. Never delete retained resources of a prod stack that has run |
   | `UPDATE_IN_PROGRESS` for much longer than usual | A resource is waiting, for example on a StackSet operation | Wait. To give up, run `aws cloudformation cancel-update-stack --region <region> --stack-name <stack-name>`, which starts a rollback |

3. **Member role StackSet.** `CaseInsightsMemberRoleStack-<uniqueId>` can succeed while instances fail in some accounts, because the StackSet tolerates failures in up to 10% of accounts. List recent operations and the accounts that failed:

   ```bash
   aws cloudformation list-stack-set-operations --region <region> \
     --stack-set-name CaseInsights-MemberRole-<uniqueId> --call-as SELF --max-items 5 \
     --query 'Summaries[].[OperationId,Action,Status,StatusReason]' --output table
   aws cloudformation list-stack-set-operation-results --region <region> \
     --stack-set-name CaseInsights-MemberRole-<uniqueId> --operation-id <operation-id> --call-as SELF \
     --query "Summaries[?Status!='SUCCEEDED'].[Account,Status,StatusReason]" --output table
   ```

   Typical causes are a service control policy that denies `iam:CreateRole`, or a role with the same name created by hand. Fix the cause first. The repair then depends on whether each expected account already has a stack instance, because `update-stack-instances` works only on instances that exist. A failed rollout can leave both kinds: instances that exist but are `OUTDATED`, and accounts that never received one, for example when failures in other accounts exceeded the failure tolerance and CloudFormation cancelled the remaining accounts before creating their instances.

   Compare the accounts each target should cover with the accounts that have an instance. `<target-id>` is an OU ID (`ou-...`) or the root ID (`r-...`) from `memberRoleTargets`. `list-accounts-for-parent` returns only the accounts directly in that parent, so repeat it for each child OU of an OU target (`aws organizations list-organizational-units-for-parent --parent-id <ou-id>`) and append to the same file; for a root target, use `aws organizations list-accounts --query "Accounts[?State=='ACTIVE'].Id" --output text` instead. The management account never receives an instance, so it is removed:

   ```bash
   : > expected-accounts.txt
   aws organizations list-accounts-for-parent --parent-id <target-id> \
     --query "Accounts[?State=='ACTIVE'].Id" --output text | tr '\t' '\n' >> expected-accounts.txt
   aws cloudformation list-stack-instances --region <region> \
     --stack-set-name CaseInsights-MemberRole-<uniqueId> --call-as SELF \
     --query 'Summaries[].Account' --output text | tr '\t' '\n' | sort -u > instance-accounts.txt
   sort -u expected-accounts.txt | grep -vx '<managementAccountId>' > expected-sorted.txt
   comm -23 expected-sorted.txt instance-accounts.txt
   aws cloudformation list-stack-instances --region <region> \
     --stack-set-name CaseInsights-MemberRole-<uniqueId> --call-as SELF \
     --query "Summaries[?Status!='CURRENT'].[Account,Region,Status,StackInstanceStatus.DetailedStatus,StatusReason]" --output table
   ```

   `comm` prints the accounts that have **no instance**; the last command lists the **existing instances that are not `CURRENT`**. Repair each group for its own accounts, in the deployment Region only, with the StackSet's own operation preferences (failure tolerance 10%, at most 25% of accounts at once). Neither command changes the template or the targets declared in `CaseInsightsMemberRoleStack-<uniqueId>`. Use only targets that are already in `memberRoleTargets`; to add a new OU, follow [Collect from a new organisational unit or account](#collect-from-a-new-organisational-unit-or-account) instead. `AccountFilterType` `INTERSECTION` limits the operation to the listed accounts within that target, so other accounts in the OU are not touched. List the accounts as JSON strings, for example `"111111111111","222222222222"`.

   - Accounts with no instance: create the missing instances.

     ```bash
     aws cloudformation create-stack-instances --region <region> \
       --stack-set-name CaseInsights-MemberRole-<uniqueId> --call-as SELF \
       --deployment-targets '{"OrganizationalUnitIds":["<target-id>"],"Accounts":["<account-id>"],"AccountFilterType":"INTERSECTION"}' \
       --regions <region> \
       --operation-preferences ConcurrencyMode=SOFT_FAILURE_TOLERANCE,FailureTolerancePercentage=10,MaxConcurrentPercentage=25
     ```

   - Existing instances that are `OUTDATED`: apply the current template to them again.

     ```bash
     aws cloudformation update-stack-instances --region <region> \
       --stack-set-name CaseInsights-MemberRole-<uniqueId> --call-as SELF \
       --deployment-targets '{"OrganizationalUnitIds":["<target-id>"],"Accounts":["<account-id>"],"AccountFilterType":"INTERSECTION"}' \
       --regions <region> \
       --operation-preferences ConcurrencyMode=SOFT_FAILURE_TOLERANCE,FailureTolerancePercentage=10,MaxConcurrentPercentage=25
     ```

   Each command prints an `OperationId`. The StackSet uses managed execution, so an operation that conflicts with one still running waits in the queue (`QUEUED`). Follow it until it is `SUCCEEDED`, then run the comparison again: `comm` must print nothing and every instance must be `CURRENT`:

   ```bash
   aws cloudformation describe-stack-set-operation --region <region> \
     --stack-set-name CaseInsights-MemberRole-<uniqueId> --operation-id <operation-id> --call-as SELF \
     --query 'StackSetOperation.[Action,Status,StatusReason]' --output text
   ```

   If it is `FAILED`, list its per-account results with `list-stack-set-operation-results` as above, fix the cause and repeat the repair for the accounts that still need it. An `INOPERABLE` instance is left over from a failed deletion and cannot be updated; delete it for that account and Region with `delete-stack-instances`, then create it again as a missing instance.

4. **Pipeline.** A failed `Build` stage deploys nothing. Fix it with a new commit on `main`, which starts a new release. A failed `Dev` or `Prod` stage leaves that environment in one of the states above. Recover the stack first, then retry the failed actions of the same release:

   ```bash
   aws codepipeline list-pipeline-executions --region <region> --pipeline-name <pipelineName> --max-items 5 \
     --query 'pipelineExecutionSummaries[].[pipelineExecutionId,status,startTime]' --output table
   aws codepipeline retry-stage-execution --region <region> --pipeline-name <pipelineName> \
     --stage-name <stage-name> --pipeline-execution-id <pipeline-execution-id> --retry-mode FAILED_ACTIONS
   ```

5. Validate the recovered environment as in [Step 7 of the installation](installation.md#step-7-validate-the-deployment). Check that every stack is `CREATE_COMPLETE` or `UPDATE_COMPLETE` and that no alarm is in `ALARM`.
## Recovery tools

Two scripts change pipeline state on purpose. Both check that they target the right deployment before reading anything, show what they would do, and change nothing until you confirm with the token from that preview. Both run from the repository root with credentials for the management account, in the Region of `config/<env>.json`. Neither script deletes any S3 object version, writes to AWS Support, or calls Bedrock.

| Situation | Tool |
| --- | --- |
| Messages in a stage dead-letter queue (active accounts, annotation or summary) after the cause is fixed | `scripts/redrive-dlq.sh` |
| A record on the workflow failure queue to retry before the next scheduled run | Send its case reference with a new `redriveId` to the summary queue (see [QueueMessages-WorkflowFailures](#queuemessages-workflowfailures)) |
| A discovery failure | Invoke account lookup (see [QueueMessages-AccountDiscoveryFailures](#queuemessages-accountdiscoveryfailures)) |
| Committed cases to analyse again, for example after a prompt or model change | `scripts/reprocess-cases.py` (see also [Change the Bedrock model](#change-the-bedrock-model)) |
| A deployment or update that failed, left StackSet instances `OUTDATED` or left accounts without an instance | [Recover from a failed deployment or update](#recover-from-a-failed-deployment-or-update) |

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

**Uncertain starts.** Only an `AccessDenied` refusal proves that no move task started; the script then reports `the move task was not started (AccessDenied). No messages were moved.` Any other failed, timed-out or unreadable `StartMessageMoveTask` response, including an error from a retry the AWS CLI made on its own after the first response was lost, is reported as `the move task start is UNKNOWN (<code>)`: SQS may have accepted the request and be moving messages already. The script then prints the `Progress:` (`list-message-move-tasks`) and `DLQ depth:` (`get-queue-attributes`) commands and exits 1. Every command the script prints carries the same `--region` and, if you passed one, the same `--profile` (shell-quoted) as the redrive, so it inspects the queues with the identity that started the task. Run both before retrying. If a task is RUNNING, or one completed after your attempt, do not start another. Start a new redrive only when no task is RUNNING and the messages are still in the dead-letter queue.

Exit status: 0 started (task RUNNING or COMPLETED) or nothing to redrive; 1 an AWS request failed, the task was not started, its start is unknown, the task reports FAILED or CANCELLED, or its status could not be read after starting (do not start another; check with the printed commands); 2 invalid options, configuration or target; 3 cancelled or not confirmed. AWS error messages are not printed, only their error codes.

Permissions: `sts:GetCallerIdentity`; `sqs:GetQueueUrl` and `sqs:GetQueueAttributes` on both queues; `sqs:ListMessageMoveTasks`, `sqs:StartMessageMoveTask`, `sqs:ReceiveMessage`, `sqs:DeleteMessage` and `sqs:GetQueueAttributes` on the dead-letter queue; `sqs:SendMessage` on the stage queue; `sqs:CancelMessageMoveTask` on the dead-letter queue to cancel. The queues use SSE-SQS, so no KMS permissions are needed.

### Reprocess committed cases

`scripts/reprocess-cases.py --env <env> --from YYYY-MM-DD --to YYYY-MM-DD` makes committed cases in an explicit date range eligible for analysis again. Run it with the repository's virtual environment (`.venv/bin/python scripts/reprocess-cases.py ...`); `--help` lists every option.

Selection:

- **Date field.** `--date-field timeCreated` (default) uses the Support case creation time stored in the processed record; `--date-field Case_Retrieval_Date` uses the time the pipeline retrieved the case for the committed analysis.
- **Time zone and bounds.** Values are converted to UTC. `--from` and `--to` are UTC calendar days and both are inclusive: the range is `[--from 00:00:00Z, day after --to 00:00:00Z)`. A record at `2026-03-31T23:59:59Z` is inside `--to 2026-03-31`; one at `2026-04-01T00:00:00Z` is not. The latest accepted `--to` is `9999-12-30`; a later day has no following midnight, so the script refuses it with exit code 2 before contacting AWS.
- **No implicit scope.** Both dates are required and must be valid; a reversed range is refused. There is no "all records" mode, and a range that matches nothing changes nothing. `--account-id` (repeatable) and `--case` (with exactly one `--account-id`) narrow the scope further. Plans that would change more than `--max-cases` cases (default 100, at most 5000) are refused.
- **Targets.** The credentials must belong to `managementAccountId`. Buckets, the summary queue and the state machine come from the `RawBucketName`, `ProcessedBucketName`, `CaseSummaryQueueUrl` and `CaseAnalysisStateMachineArn` outputs of `CaseInsightsCoreStack-<uniqueId>`; the queue and state machine must carry this deployment's names, and both buckets must have versioning enabled.

For each selected case the preview shows one action:

- **requeue**: the raw `data.json` and `communications.json` are still present and consistent, so a new execution can analyse them.
- **rediscover**: the raw inputs have expired, but the case was created inside the collection lookback window (with two days to spare), so the next scheduled run collects it again.
- **skip**: anything else, left unchanged. This includes a RUNNING execution of the case, an execution in PENDING_REDRIVE, an ownership marker naming an execution that is being started (claimed less than an hour ago), a Case Cleanup reset or another reprocess in progress (a reset claim less than an hour old), a permission error (403), an unreadable marker or a failed status lookup (status unknown), and expired raw inputs outside the lookback window.

`--dry-run` stops after the preview and prints the plan token. Without it, the script asks for the token, or takes it from `--confirm`. The token covers the exact records and processed-record versions in the preview, so if anything changed, run a new preview.

After confirmation, each case is handled on its own:

1. The script checks again that the processed record has the previewed version and that no work became active; otherwise the case is skipped.
2. It claims the case: it replaces exactly the `workflow.json` ownership marker it checked (or its absence) with a reset claim, conditional on that object's ETag, as Case Cleanup does. If the Workflow Starter, Case Cleanup or another reprocess changed `workflow.json` in the meantime, the write fails and the case is skipped unchanged. While the claim is held, the starter starts nothing for the case: it fails a reference for the case as a retryable record failure (`CaseResetInProgressError`), which uses up one receive, so with `operations.queues.maxReceiveCount` 1 that reference goes to the summary dead-letter queue. Case Cleanup and other reprocesses skip a claimed case. Under the claim, the script checks the processed record version and the previous owner's execution status again.
3. For **requeue**, it sends a summary reference with a delivery delay (`--delay-seconds`, default 60) under a fresh run ID (`<timestamp>-<random>`) and a redrive ID `reprocess-...`. The new run makes the Workflow Starter start attempt 1 of a new execution name, so a closed execution name is never reused.
4. Immediately before removing anything, it checks the processed record version once more. It then removes the stage checkpoints, each conditional on the ETag it has just read, and finally the processed record, conditional on the previewed ETag. These conditional deletes use one SDK attempt: an accepted delete whose response is lost is reported as uncertain, not retried into a misleading 404/412 no-op. A record or checkpoint confirmed intact after a conditional refusal is kept; a processed record that is absent or cannot be checked is reported as `UNCONFIRMED`, never as `skipped; record kept`.
5. It puts the checked `workflow.json` back in place of its claim, so the starter reconciles the case exactly as before and the queued reference starts the new execution. If the time from just before the first send until the claim was released came within 5 seconds of the delay, or exceeded it, the first reference may have been delivered too early: answered as already processed before the record was removed, or failed under the claim. The script then sends one follow-up reference (redrive ID `<redrive ID>-f`) without delay, and only after the claim is released, so the starter accepts it. Timing from before the send covers a slowly acknowledged send whose message SQS had already accepted and delivered. Duplicate references never start a second execution; an early first reference that reached the summary dead-letter queue is a harmless duplicate, as the report says.

**Claim safety margin.** The reset claim is a lease, not a lock: the Workflow Starter, Case Cleanup and other reprocesses take over a claim that is an hour old, whether or not the process that wrote it has stopped. Before every change under the claim (the summary reference, each checkpoint delete, the processed-record delete and any undo), the script checks that `workflow.json` is still its own claim, by version ID and ETag, and that the claim is younger than a 10-minute safety margin. The age is the larger of the wall-clock time and the monotonic time since the claim was written, because the monotonic clock stops while a computer sleeps. If either check fails, the script stops changing the case, reports it as `failed` with the reason (`taken over by another process`, `older than the 10-minute safety margin` or `could not be verified`) and lists anything it had already changed. It does not put back a claim that was taken over. Do not suspend the computer, stop the process (for example with Ctrl+Z) or run the script over a connection that may stall for long periods while it runs. A stall of most of an hour between the last check and a single request can still overlap a takeover, so keep the computer's clock synchronised, and run large plans from a host that stays awake.

**Concurrent reprocesses.** Concurrent reprocesses of the same scope, for example two operators confirming the same preview, are safe: they never change a case at the same time. Whichever run claims a case first handles it. The other run skips it, because the claim or the new execution's ownership marker defeats its conditional write, or because the processed record it previewed has gone. The second run never removes the checkpoints of the execution the first run queued, and never queues a second reference for it. Such a case is reported as `skipped`, not as an error.

A reference that arrives while the processed record still exists is answered as already processed, so a run that stops before removing a record leaves that case as it was. If a follow-up reference is definitively refused, the report prints the exact message to send to the summary queue. A transport or server failure after SQS may have accepted it is instead reported as `UNCONFIRMED delivery`: check the CaseAnalysis execution and processed record before resending the same printed reference, because it may already have started the execution. If the claim cannot be released after the record was removed, the report prints the reference to send once the claim is released or an hour old, because until then the starter fails it.

**Removal that stops part way.** A permission error (403), another failed delete, a changed checkpoint or record, or a lost claim can stop a removal after some checkpoints were already hidden by delete markers. The report then names exactly which checkpoints this run hid. While its claim is still verifiably held and the processed record is known to be kept, the script removes the delete markers it has just created (by version ID), which makes the previous checkpoint versions current again, and says that they were restored; this needs `s3:DeleteObjectVersion` on the checkpoints. Otherwise (no permission, a lost claim, or a processed-record delete whose effect is unknown) it leaves them and prints indented recovery steps: a `head-object` command for the processed record and a `get-object` command for `workflow.json`, then one `aws s3api delete-object --region <region> --bucket <raw bucket> --key <checkpoint key> --version-id <delete marker>` command per hidden checkpoint, all with the deployment's Region and the `--profile` you used. Run the delete commands only while the processed record is still the previous version shown in the report and `workflow.json` is not a reset claim; otherwise another reprocess or a new execution owns the case and the delete markers must stay. A checkpoint delete whose effect is unknown (an error other than 403) stays in the removal report and is listed with an `aws s3api list-object-versions` command: if the latest entry is a delete marker created at the time of the run, remove it the same way. An uncertain processed-record delete is reported as `UNCONFIRMED` with a processed-bucket version-listing command; check the execution and current record before resending or restoring. If the script is killed while it holds a claim, the reset claim stays in `workflow.json`. The starter waits until the claim is an hour old and then takes it over, and Case Cleanup also treats it as abandoned after an hour. If the script is killed after removing a record but before a needed follow-up, the case has no processed record and may have no pending reference: send its case reference to the summary queue with a new `redriveId`, or let Case Cleanup and the next scheduled run collect it again while it is inside the lookback window. The report lists every case as `requeued`, `rediscover`, `skipped` or `failed`, with its previous processed version ID, and exits 1 when any case failed. A queued reference only starts a new execution: check the CaseAnalysis executions, the workflow failure queue and the processed record for the result.

Versioned objects: the processed record and checkpoints are removed with plain deletes, which add S3 delete markers. No version is deleted by version ID, except a checkpoint delete marker the same run has just created when it undoes a removal that stopped part way, so an older record can never become current again. The previous processed version stays restorable until `operations.storage.noncurrentVersionRetentionDays` expires it: to restore one, copy that version over the current key (`aws s3api copy-object --region <region> --copy-source '<bucket>/<key>?versionId=<id>' --bucket <bucket> --key <key>`). Restoring is a separate, manual operation with its own permissions, which the reprocess role does not need: `s3:ListBucketVersions` on the processed bucket to find the version (`aws s3api list-object-versions --region <region> --bucket <bucket> --prefix <key>`), `s3:GetObjectVersion` on the source version (and `s3:GetObjectVersionTagging` if it has tags), and `s3:PutObject` on the key. Until a case is committed again it has no processed record, so Athena and the MCP server do not return it, and Case Cleanup may reset it like any other uncommitted case once its inputs are older than the cleanup grace; the next scheduled run then collects it again while it is inside the lookback window.

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

Permissions:

- `sts:GetCallerIdentity`; `cloudformation:DescribeStacks` on `CaseInsightsCoreStack-<uniqueId>`; `s3:GetBucketVersioning` on the raw and processed buckets.
- Processed bucket: `s3:ListBucket` on the bucket; `s3:GetObject` and `s3:DeleteObject` on `account_number=*`.
- Raw bucket: `s3:ListBucket` on the raw bucket ARN itself. Without it, S3 answers 403 instead of 404 for an absent ownership marker, raw input or checkpoint, and the script cannot tell missing objects from denied ones. Manual recovery from an uncertain checkpoint delete also needs `s3:ListBucketVersions` on the raw bucket ARN for the printed `list-object-versions` step. This is separate from `s3:ListBucket`; scope it with an `s3:prefix` condition to `account_number=*/case_number=*/checkpoints/*` where practical.
- Raw bucket objects under `account_number=*/case_number=*/`: `s3:GetObject` on `data.json`, `communications.json`, `workflow.json` and `checkpoints/*`; `s3:GetObjectVersion` on `communications.json`, because the script reads the exact conversation version it requeues when the ownership marker does not record that version; `s3:PutObject` and `s3:DeleteObject` on `workflow.json`, for the reset claim and for putting the checked marker back; and `s3:DeleteObject` on `checkpoints/*`. Optionally `s3:DeleteObjectVersion` on `checkpoints/*`, so the script can undo a removal that stopped part way by removing its own delete markers; without it, the script prints the commands instead.
- `sqs:GetQueueAttributes` and `sqs:SendMessage` on the summary queue; `states:ListExecutions` on the state machine and `states:DescribeExecution` on its executions.

The buckets use SSE-S3, so no KMS permissions are needed. A 403 is reported as `permission denied (<code>; check <permission>, see docs/runbook.md)`. In the preview the case is skipped and nothing changes. During execution the case is reported as failed: a 403 before the first delete changes nothing, and a 403 after some checkpoints were hidden (for example `s3:DeleteObject` missing on the processed record) is handled as a [removal that stops part way](#reprocess-committed-cases): the report names those checkpoints and either restores them or prints the commands to do so. A 403 is never treated as a missing object or as expired raw inputs. Restoring a previous processed version needs the separate permissions under *Versioned objects* above.

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

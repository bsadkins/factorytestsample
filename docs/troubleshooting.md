# Troubleshooting

This guide lists common problems with Support Case Insights, from local installation through deployment and daily operation, as symptom, likely cause and fix. It covers installation and synthesis, deployment, `AccessDenied` and `SubscriptionRequired` skips, missing cases, empty Athena results, Amazon Bedrock errors, the MCP server and performance. For an alarm that has fired, start with the [alarm runbook](runbook.md), which has one section per alarm; for metrics and log queries see [Monitoring](monitoring.md) and [Logging](logging.md). Commands run from the repository root, with credentials for the management account and the Region of `config/<env>.json` unless stated otherwise.

**Status of this guide.** The local causes and fixes (installation, configuration and synthesis) come from the code and its tests. Nothing has been deployed, so the AWS-side symptoms below are derived from the code and AWS documentation and have not been observed in a real account. Error texts quoted from AWS services may differ slightly in practice.

## Before you start

1. Note the environment (`dev` or `prod`), its `uniqueId` and Region from `config/<env>.json`.
2. Check the dashboard `CaseInsights-<uniqueId>` and the *Alarms* widget for the period in question.
3. Look for `ERROR` and `WARNING` records with the cross-pipeline query in [Logging](logging.md#1-errors-and-warnings-across-the-pipeline).
4. Follow the [general procedure](runbook.md#general-procedure) in the runbook. After a fix, the next scheduled run normally repairs the state: completed cases are never processed again, and unfinished cases are retried or reset by daily cleanup.

## Installation and local commands

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `npm ci` warns `EBADENGINE`, or ESLint fails to start in `npm run lint` | Node.js is too old. ESLint 10 needs Node.js 20.19 or later on the 20 line, 22.13 or later on the 22 line, or 24 or later. | Use the version in `.nvmrc` (24.22.3), for example `nvm install && nvm use`, then rerun `npm ci`. |
| `npm ci` fails because `package-lock.json` and `package.json` disagree | The lock file was edited or not committed with a dependency change. | Restore both files from version control. Do not run `npm install` to "fix" the lock unless you mean to change dependencies. |
| `python3.12: command not found` | Python 3.12 is not installed or not on `PATH`. | Install Python 3.12 and create the environment with `python3.12 -m venv .venv`. Other Python versions are not supported because the Lambda runtime is `python3.12`. |
| `pip install` fails with `THESE PACKAGES DO NOT MATCH THE HASHES` or `Hashes are required` | The development lock is installed without `--require-hashes`, a mirror serves different files, or the lock was edited. | Run `.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt`. If a package index mirror is in use, check that it serves the same files as PyPI. |
| Python tests or scripts fail with `ModuleNotFoundError` | A system Python was used instead of the virtual environment. | Run tools through the environment, for example `.venv/bin/pytest` and `.venv/bin/python scripts/<script>`. |
| `npx cdk ...` prints a different CDK version from the pinned one, or the command is not found | A globally installed `cdk` is used, or `npm ci` has not been run. | Run `npm ci` and always call the CLI as `npx cdk`. |

## Configuration and synthesis

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `CDK context "env": expected one of dev, prod (pass -c env=<name>); got missing` | The environment was not passed. | Add `-c env=dev` or `-c env=prod` to every `npx cdk` command. |
| Synthesis stops with a list of configuration problems | `config/<env>.json` fails the schema or a cross-field rule: unknown field, missing `alarmEmail` in prod, `enableMcp` without `enableAnalytics` or without `mcpAllowedPrincipals`, an unsupported `bedrockModelId`, a schedule without `?` in exactly one of the day fields, a visibility timeout under six consumer timeouts, cleanup grace not longer than the workflow timeout, raw retention not longer than cleanup grace, or an `existingRoleArn` outside `managementAccountId`. | Fix each listed field. [Configuration](configuration.md) describes every field and rule. |
| Synthesis fails because dev and prod `apiGatewayLogging` settings disagree | Both environments share one account and Region and must use identical settings. | Make `apiGatewayLogging` identical in `config/dev.json` and `config/prod.json` (see [Shared API Gateway logging prerequisite](mcp-server-deployment.md#shared-api-gateway-logging-prerequisite)). |
| Synthesis fails while bundling the Python layer with a hash mismatch or "no matching distribution" | `requirements.txt` was edited by hand, a package has no `manylinux_2_28_x86_64` wheel for CPython 3.12, or the package index is unreachable. The bundler installs wheels only and never compiles on the host. | Regenerate the lock with the `uv pip compile` command at the top of `requirements.in`, choose a package version with a compatible wheel, and make sure the first synthesis can reach the package index. Later syntheses reuse `.cache/pip`. |
| Synthesis cannot find the bundler's Python | `.venv` does not exist in the repository root. | Create it as in the setup steps, or set `CASE_INSIGHTS_PYTHON` to a Python 3.12 interpreter that has the development requirements. |
| Synthesis fails with a cdk-nag finding | A change introduced a resource that breaks an AWS Solutions rule without an acknowledged reason. | Fix the resource. Add an acknowledgement only with a specific, reviewed reason; see [Development](development.md). |

## Deployment

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `This stack uses assets, so the toolkit stack must be deployed` or a missing bootstrap version | The account and Region are not bootstrapped for CDK. | Run `npx cdk bootstrap aws://<account-id>/<region> -c env=<env>` once, then `npx cdk deploy --all -c env=<env>`. See [Installation](installation.md). |
| `CaseInsightsMemberRoleStack-<uniqueId>` fails to create the StackSet, mentioning trusted access or service-managed permissions | Trusted access between CloudFormation StackSets and AWS Organizations is not active. | An Organizations administrator runs `aws cloudformation activate-organizations-access --region <region>` in the management account, then redeploy. |
| Deployment fails with access errors from Organizations or StackSets | The stacks are not being deployed from the management account, or the deploying principal lacks Organizations permissions. | Deploy with credentials for `managementAccountId`. See [Permissions](permissions.md). |
| `CaseInsightsMcpStack-<uniqueId>` fails creating stage `v1` with a CloudWatch Logs role error | The account-wide API Gateway CloudWatch role setting is missing in existing mode, or points to a role that cannot write to the access log group. | Follow [MCP access logs are missing](runbook.md#mcp-access-logs-are-missing) and the [shared API Gateway logging prerequisite](mcp-server-deployment.md#shared-api-gateway-logging-prerequisite). |
| Setting the API Gateway CloudWatch role fails with an STS error | AWS STS is not active in an opt-in Region. | Activate STS for the Region (see [Regional STS](mcp-server-deployment.md#regional-sts)) and deploy again. |
| `npx cdk diff --all -c env=<env>` shows a change to `AWS::ApiGateway::Account` you did not expect | Managed mode would take over an account-wide setting owned by someone else. | Stop and check ownership as described in [Managed mode](mcp-server-deployment.md#managed-mode). Use existing mode if another owner has configured it. |
| A redeploy of dev fails because a log group or bucket already exists | A previous dev stack was deleted while a resource with the same name remained, for example after a failed deletion. | Delete the leftover dev resource after checking it contains nothing you need, then redeploy. Prod resources are retained on purpose; never delete them to unblock a deployment. |
| The alarm email never arrives | The subscription has not been confirmed, or `alarmEmail` is not set in dev. | Check the subscription as described in [Monitoring](monitoring.md#sns-topic) and confirm it from the email AWS sent. |

## Accounts skipped: AccessDenied

Log record: `Account skipped for this run` with `reason` `AccessDenied` in `/aws/lambda/Lambda-CaseRetrieval-<uniqueId>`, metric `AccountsSkippedAccessDenied`; or `Case not annotated` with metric `CasesSkippedAccessDenied` in case annotation. The pipeline treats `AccessDenied` and `AccessDeniedException` from the member role or the Support API as a skip for that run only; the account is tried again on the next run.

| Likely cause | Check | Fix |
| --- | --- | --- |
| `Support-Case-Analysis-Role-<uniqueId>` does not exist in the account: the account is outside `memberRoleTargets`, joined recently, or its StackSet instance failed | `aws cloudformation list-stack-instances --stack-set-name CaseInsights-MemberRole-<uniqueId> --region <region> --query 'Summaries[].[Account,Status,StackInstanceStatus.DetailedStatus]' --output text` | Add the account's OU (or the root) to `memberRoleTargets` and redeploy, or fix the failed instance. Automatic deployment adds the role to accounts that join a targeted OU. |
| The role exists but its trust policy no longer allows the case-retrieval and case-annotation roles of this deployment | `Member role assumption failed` warnings with `account_id` and `error_code` | Undo the manual change to the role; drift detection on the StackSet shows what changed. The role must only be managed by the StackSet. |
| A service control policy denies `sts:AssumeRole` or the AWS Support API in the member account | The same warning appears for every account under one OU | Exempt `Support-Case-Analysis-Role-<uniqueId>` from the SCP, or accept that those accounts are skipped. |
| The account was suspended or is closing | Organizations shows the account as not active | No action. Inactive accounts are excluded from later account lists. |

See [Permissions](permissions.md) for the role's trust and permissions and [Security](security.md) for why it is read-only.

## Accounts skipped: SubscriptionRequired

Log record: `Account skipped for this run` with `reason` `SubscriptionRequired`, metric `AccountsSkippedSubscriptionRequired`. The AWS Support API answers `SubscriptionRequiredException` for accounts without a Business, Enterprise On-Ramp or Enterprise Support plan, so their cases cannot be read.

- This is expected for accounts on the Basic or Developer plan and needs no action; there is no alarm for it.
- To confirm from inside the member account: `aws support describe-severity-levels --region us-east-1`. A `SubscriptionRequiredException` confirms the plan does not include API access.
- If the account should have a qualifying plan, check the plan in the AWS Support console of that account. A plan change is picked up on the next run.

## Cases are missing

Work through these in order for a case you expected to see.

| Check | Why the case is missing | Fix |
| --- | --- | --- |
| Is the case resolved? | Only resolved cases are collected (`CasesExcludedUnresolved`). | None. The case is collected by a later run after it is resolved, while it is still inside the collection window. |
| Was it created inside the collection window? | Cases created outside the window set by `lookbackMonths` (default 12, the history the Support API keeps) are excluded (`CasesExcludedOutsideWindow`). The `Account cases retrieved` record gives the window as `afterTime` and `beforeTime`. | Raise `lookbackMonths` only within what the Support API returns. |
| Is the account in the account list? | The management account and accounts that are not active are excluded (`AccountsExcludedManagement`, `AccountsExcludedInactive`). | None for the management account. Check the account status in Organizations otherwise. |
| Was the account skipped? | See [AccessDenied](#accounts-skipped-accessdenied) and [SubscriptionRequired](#accounts-skipped-subscriptionrequired). | As described there. |
| Did the analysis fail? | A failure record is on `CaseInsights-WorkflowFailures-<uniqueId>`; use the [failed case analyses query](logging.md#8-failed-case-analyses). Conversations too large to summarise are reported once and not retried while unchanged. | Follow [QueueMessages-WorkflowFailures](runbook.md#queuemessages-workflowfailures). |
| Is it stuck in a dead-letter queue? | A stage failed `operations.queues.maxReceiveCount` times. | Fix the cause and [redrive the dead-letter queue](runbook.md#redrive-a-stage-dead-letter-queue). |
| Is it still in progress? | The run has not finished; `RunCasesPending` is above zero or an execution is RUNNING. | Wait for the run to drain. Check with `aws stepfunctions list-executions --region <region> --state-machine-arn arn:aws:states:<region>:<account-id>:stateMachine:CaseAnalysis-<uniqueId> --status-filter RUNNING --max-items 20`. |
| Did discovery run at all? | No account list was written for the day; the [MissingDailyRunCompletion](runbook.md#missingdailyruncompletion) alarm fires later. | Follow [QueueMessages-AccountDiscoveryFailures](runbook.md#queuemessages-accountdiscoveryfailures), or start a run by hand (below). |
| Was it already processed with an older prompt or model? | Committed cases are never analysed again automatically. | Use [Reprocess committed cases](runbook.md#reprocess-committed-cases). |

To start a run outside the schedule (it gets its own run ID):

```bash
aws lambda invoke --region <region> --function-name Lambda-AccountLookup-<uniqueId> \
  --invocation-type Event --cli-binary-format raw-in-base64-out --payload '{}' /tmp/account-lookup-response.json
```

Cases appear in Athena and the MCP server only after their processed record is committed and the crawler has added the partition; the CSV exporter reads the processed bucket directly (see [CSV export](csv_export.md#troubleshooting)).

## Athena returns no rows

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| No rows, or recent cases missing | The crawler has not run since the records were committed. It runs at `cron(0 9 * * ? *)` UTC by default. | Start it and check partitions as in [Making new records visible](athena.md#making-new-records-visible). |
| No rows at all after the first deployment | No case has been committed yet, or Analytics is disabled. | Check `CasesProcessed` on the dashboard and that `enableAnalytics` is true. |
| `Access Denied`, `Insufficient Lake Formation permission(s)` or `Bytes scanned limit was exceeded` | Permissions, Lake Formation or the scan cutoff. | See [Athena troubleshooting](athena.md#troubleshooting) and [Permissions for analysts](athena.md#permissions-for-analysts). |
| Totals per day or month look shifted | Times are in UTC. | Convert with `AT TIME ZONE '<zone>'` as described in [Athena troubleshooting](athena.md#troubleshooting). |

## Amazon Bedrock errors

Bedrock is called by `Lambda-CaseAnalysisStages-<uniqueId>` (summary, RCA and lifecycle) and by the MCP server's analysis tool. Use the [Bedrock failures query](logging.md#7-bedrock-failures-by-error-code) to find the `error_code`.

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `UnsupportedModelError` (`UnsupportedModel`) | `bedrockModelId` is not one of the two accepted global inference profiles. Configuration validation normally stops this before deployment. | Use `global.anthropic.claude-haiku-4-5-20251001-v1:0` (default) or `global.anthropic.claude-sonnet-4-5-20250929-v1:0` and redeploy. |
| `error_code` `AccessDeniedException` on every invocation | Model access has not been granted for the account, the Anthropic first-use details or the AWS Marketplace subscription that the model may require are missing, or an SCP denies Bedrock in a Region the global profile routes to. | Enable access to the model in the Bedrock console of the management account, complete any required subscription, and review SCPs that restrict Regions. See [Permissions](permissions.md). |
| `error_code` `ValidationException` | The model ID or request is not accepted in this Region, or the inference profile is not available to the account. | Check that the profile exists: `aws bedrock list-inference-profiles --region <region> --query "inferenceProfileSummaries[].inferenceProfileId" --output text`. |
| `BedrockThrottles` rising, `ThrottlingException` | The account's tokens-per-minute or requests-per-minute quota for the profile is lower than the configured concurrency needs. Throttled calls are retried inside the function and by the workflow. | Lower `bedrockMaxConcurrency`, or request a quota increase in Service Quotas. |
| `Prompt exceeds the configured size and was not sent`, or `ConversationTooLarge` failure records | The case conversation is longer than the summary input limit. | None automatically: the record is reported once. Such cases cannot be summarised with the current limits; see [Prompts](prompts.md). |
| `SummaryValidationFailures` or `ClassificationFallbacks` rising | The model's replies do not meet the summary format or category rules. | Review the prompts and model choice in [Prompts](prompts.md). Classification falls back to `Unclassified` rather than failing. |
| Transient errors (`ServiceUnavailableException`, `ModelNotReadyException`, `ModelTimeoutException`) | Bedrock is temporarily unavailable. | None. The function and workflow retry; persistent failures produce failure records that the next run retries. |

## MCP server

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| The bridge exits with status `2` or `3`, `403` with any `errorType`, `429`, or `-32001` | Client configuration, credentials, authorisation or rate limits. | See [MCP troubleshooting](mcp-server-deployment.md#troubleshooting). |
| `-32603` internal error from a tool | An Athena or Bedrock failure inside the function; `McpInternalErrors` alarm. | Follow [McpInternalErrors](runbook.md#mcpinternalerrors). |
| `502` or `504` | The function failed or exceeded its 28-second timeout. | Follow [LambdaErrors-McpServer](runbook.md#lambdaerrors-mcpserver). Long Athena queries return `PENDING` and can be resumed with the `queryExecutionId`. |
| `413` | The request body is over 32 KiB and was blocked by AWS WAF. | Send smaller requests. |
| Many `403` responses from unknown addresses | Probing; `McpAuthFailures` alarm. | Follow [McpAuthFailures](runbook.md#mcpauthfailures). |
| The access log group is empty | The account-wide API Gateway CloudWatch role setting is missing or wrong. | Follow [MCP access logs are missing](runbook.md#mcp-access-logs-are-missing). |
| Tools return no data | Analytics has no partitions yet. | See [Athena returns no rows](#athena-returns-no-rows). |

## Performance and backlog

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| The first run takes many hours or days | The first run collects up to `lookbackMonths` of resolved cases for every account, and analysis is limited by `bedrockMaxConcurrency` (reserved concurrency of the stages function; 2 in `config/dev.json`, 5 by default) and `operations.workflow.starterMaxConcurrency` (default 2). | Expect it. `RunCasesPending` falls as the run drains; later runs only collect new cases. Raise the concurrency limits only within the Bedrock quota. |
| `WorkflowExecutionsTimedOut` with a growing backlog | Executions wait for stage capacity longer than `operations.workflow.executionTimeoutMinutes` (default 60). | Raise the timeout or `bedrockMaxConcurrency` (keeping cleanup grace longer than the timeout), then let reconciliation record the timed-out attempts. See [WorkflowExecutionsTimedOut](runbook.md#workflowexecutionstimedout). |
| `MissingDailyRunCompletion` while work is still progressing | The run takes longer than 30 hours to drain. | Follow [MissingDailyRunCompletion](runbook.md#missingdailyruncompletion). The alarm clears after the run completes. |
| `CleanupIncomplete` above zero | Cleanup reached its time budget. | None. The next daily run resumes from its saved position under `cleanup-state/`. |
| Support API throttling in retrieval or annotation logs (`Retrying after retryable error` with a throttling `error_code`) | Many accounts or cases at once. | None in most cases: calls are retried with backoff and failed messages are retried by SQS. Persistent throttling ends in a dead-letter queue; redrive it later. |

## Recovery tools

When the cause is fixed, use the [recovery tools](runbook.md#recovery-tools): `scripts/redrive-dlq.sh` for stage dead-letter queues and `.venv/bin/python scripts/reprocess-cases.py` for committed cases that must be analysed again. Both preview their changes and ask for confirmation. Cost effects of reprocessing are described in [Cost](cost.md); typical day-to-day use is in [Usage](usage.md).

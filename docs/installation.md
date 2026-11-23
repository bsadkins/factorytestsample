# Installation

This guide takes you from a fresh clone to a deployed, validated Support Case Insights environment in the management account of an AWS Organization, and covers updates, reprocessing, the optional Kiro, QuickSight and pipeline set-ups, and removal. It follows the deployment flow in the [README](../README.md#deployment). Every command runs from the repository root unless stated otherwise. Commands that call AWS have **not** been run against a real account during development; local verification (`npm run verify`) is the only part that has been executed, and the live checks are listed in [Deferred live verification](#deferred-live-verification).

## What you will deploy

| Stack | When | Contents |
| --- | --- | --- |
| `CaseInsightsCoreStack-<uniqueId>` | Always | Schedules, collection and analysis functions, queues, Step Functions workflow, S3 buckets, idempotency table, dashboard, alarms and the alarm topic |
| `CaseInsightsMemberRoleStack-<uniqueId>` | Always | Service-managed StackSet `CaseInsights-MemberRole-<uniqueId>`, which deploys `Support-Case-Analysis-Role-<uniqueId>` to every member account under `memberRoleTargets` |
| `CaseInsightsAnalyticsStack-<uniqueId>` | `enableAnalytics: true` | Glue database and table, partition crawler, Athena workgroup and results bucket |
| `CaseInsightsMcpStack-<uniqueId>` | `enableMcp: true` (needs Analytics) | API Gateway REST endpoint `POST /v1/mcp` with IAM authorisation, AWS WAF and the MCP function with eight read-only tools |
| `CaseInsightsApiLoggingStack-<sharedId>` | MCP enabled and `apiGatewayLogging.mode` `managed` | The account and Region API Gateway CloudWatch role setting, shared by every environment in that account and Region |
| `CaseInsightsPipelineStack` | Only with `-c pipeline=true` | Optional approval-gated CDK pipeline (see [pipeline](pipeline.md)) |

Everything is deployed into the management account and one Region, except the member role, which the StackSet creates in each member account. The [architecture](architecture.md) guide explains how the parts fit together, and [resources](resources.md) lists every resource.

Three decisions differ from earlier designs and are reflected throughout:

- **No Docker or Finch.** Python Lambda assets are bundled on your machine from hash-locked CPython 3.12 manylinux x86_64 wheels; no container runtime is needed at any step.
- **AWS managed encryption only.** S3, SQS, DynamoDB, CloudWatch Logs and Athena results use AWS managed or AWS owned encryption, and no customer managed KMS key is created, so no key needs to be retained or deleted. The alarm topic is the approved exception: it is not encrypted at rest, its policy denies publishing without TLS, and it carries no case content (see [security](security.md#alarm-sns-topic-exception)).
- **Suffixed member role.** The member role is always `Support-Case-Analysis-Role-<uniqueId>`, so dev and prod can coexist in the same member accounts.

## Prerequisites

| Requirement | Detail |
| --- | --- |
| Node.js | 20 LTS from 20.19, 22 from 22.13, or 24 and later (`package.json` engines `^20.19.0 \|\| ^22.13.0 \|\| >=24`). `.nvmrc` pins 24.22.3, the version used for development |
| AWS CDK v2 | Pinned in `package.json` (`aws-cdk` 2.1148.1, `aws-cdk-lib` 2.276.0) and installed by `npm ci`. Always run it as `npx cdk`; a global CDK installation is not needed |
| Python | 3.12 (`python3.12` on `PATH`), the Lambda runtime version |
| AWS CLI v2 | Authenticated with credentials for the **management account** of the organisation (check with `aws sts get-caller-identity`) |
| Deployment permissions | As described in [permissions](permissions.md#deployer), plus an administrator for the [one-time account set-up](#step-4-prepare-the-account-once) |
| AWS Organizations | All features enabled; deployment from the management account itself (a delegated administrator is not supported) |
| AWS Support plan | Business, Enterprise On-Ramp or Enterprise Support on each member account whose cases you want. The AWS Support API refuses other accounts with `SubscriptionRequiredException`; they are skipped and counted, not failed |
| StackSets trusted access | Enabled for AWS Organizations before the first deployment (see [Step 4](#step-4-prepare-the-account-once)) |
| Amazon Bedrock | Access to the configured Anthropic model in the deployment Region, including the Anthropic first-time use (FTU) form (console, or `aws bedrock put-use-case-for-model-access`; see [Step 4](#step-4-prepare-the-account-once)) and the AWS Marketplace subscription where required |
| Network | The first `npm ci`, `pip install` and synthesis need access to the npm registry and the Python package index; wheels are then cached in `.cache/pip` |

Docker, Finch and AWS credentials are not needed for local verification.

## Step 1: Clone and install

```bash
git clone https://github.com/bsadkins/factorytestsample.git && cd factorytestsample
npm ci
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt
```

`python3` must be Python 3.12; use `python3.12 -m venv .venv` if your default `python3` is another version. The npm scripts call the tools in `.venv/bin/` directly, so the virtual environment must be at `.venv`. `requirements-dev.txt` is hash-locked, so `pip` verifies every package (add `--require-hashes` to make that explicit). If you work from a fork, replace `bsadkins` with your GitHub owner.

## Step 2: Configure

Create the environment file from the example, which sets every field, then look up your organisation and account IDs:

```bash
cp config/example.json config/prod.json
aws organizations describe-organization --query 'Organization.Id' --output text
aws sts get-caller-identity --query Account --output text
aws organizations list-roots --query 'Roots[].Id' --output text
```

`cp` replaces the shipped `config/prod.json`, which holds invented values. Edit `config/prod.json`: `uniqueId`, `orgId`, `managementAccountId`, `region`, `memberRoleTargets`, `enableAnalytics`, `enableMcp`, `alarmEmail` and `bedrockModelId`. To target organisational units instead of the whole organisation, list them with `aws organizations list-organizational-units-for-parent --parent-id <root-id>`.

The fields, with their defaults (the full reference, with allowed values, effects and validation errors, is [configuration](configuration.md)):

| Field | Default | Set it to |
| --- | --- | --- |
| `uniqueId` | None (required) | 3 to 12 lower-case letters, digits and single hyphens, unique per environment, for example `sciprod`. It suffixes every stack and resource name |
| `orgId` | None (required) | Your organisation ID, `o-...` |
| `managementAccountId` | None (required) | The 12-digit management account ID |
| `region` | `us-east-1` | The Region for every stack; the configured Bedrock model must be available there |
| `environment` | None (required) | `prod` or `dev`, matching the file name and `-c env=<env>` |
| `enableAnalytics` | `false` | `true` for Glue and Athena (both shipped files and the example use `true`) |
| `enableMcp` | `false` | `true` for the MCP endpoint (needs Analytics and at least one `mcpAllowedPrincipals` entry) |
| `memberRoleTargets` | None (required) | Root ID (`r-...`) or OU IDs (`ou-...`) that receive the member role |
| `bedrockModelId` | `global.anthropic.claude-haiku-4-5-20251001-v1:0` | That value or `global.anthropic.claude-sonnet-4-5-20250929-v1:0`, the only alternative |
| `bedrockMaxTokens` | `1000` | Maximum output tokens per Bedrock request (256 to 4096) |
| `bedrockMaxConcurrency` | `5` | Maximum concurrent Bedrock invocations by the pipeline (1 to 50), within your Bedrock quota |
| `lookbackMonths` | `12` | Months of case history to collect (1 to 12) |
| `rawRetentionDays` | `90` | Days raw case data and account lists are kept |
| `logRetentionDays` | `90` | Retention of every log group |
| `alarmEmail` | None (required in prod) | Address that receives alarm notifications |
| `mcpAllowedPrincipals` | `[]` | IAM role or user ARNs allowed to call the MCP endpoint (role ARNs, not session ARNs) |
| `apiGatewayLogging.mode` | `managed` | `managed`, or `existing` if another owner already set the API Gateway CloudWatch role |
| `apiGatewayLogging.sharedId` | `shared` (shipped files use `scishared`) | Namespace of `CaseInsightsApiLoggingStack-<sharedId>`; identical in every environment of the account and Region |
| `apiGatewayLogging.existingRoleArn` | None | In existing mode only, the role already set in the account |
| `schedules.accountLookup` | `cron(0 0 * * ? *)` | Daily run start, UTC |
| `schedules.cleanup` | `cron(0 22 * * ? *)` | Daily cleanup of abandoned work, UTC |
| `schedules.crawler` | `cron(0 9 * * ? *)` | Daily partition crawl, UTC |
| `operations.queues` | `maxReceiveCount` 3, `dlqRetentionDays` 14 | Receives before a message moves to its dead-letter queue, and how long dead-letter messages are kept |
| `operations.functions` | `consumerTimeoutSeconds` 300, `analysisTimeoutSeconds` 180, `memorySizeMb` 512 | Lambda timeouts and memory |
| `operations.workflow` | `executionTimeoutMinutes` 60, `starterMaxConcurrency` 2, `taskMaxAttempts` 3 | Step Functions limits and retries |
| `operations.cleanup` | `graceHours` 24 | Age before abandoned work is reset |
| `operations.storage` | `processedInfrequentAccessDays` 90, `noncurrentVersionRetentionDays` 30, `accessLogRetentionDays` 365 | S3 lifecycle |
| `operations.athena` | `bytesScannedCutoffPerQuery` 10737418240 (10 GiB), `resultsRetentionDays` 30 | Per-query scan limit and result retention |
| `operations.mcp` | `reservedConcurrency` 20 (fixed; no other value is accepted), `throttleRateLimit` 10, `throttleBurstLimit` 20, `wafRateLimitPer5Minutes` 300 | MCP function concurrency and request limits |

For a dev environment, edit `config/dev.json` the same way with a different `uniqueId` and `environment` `dev` (see [Dev and prod](#dev-and-prod)). When one environment is synthesised, the app also validates the other environment file if it exists and requires both to use identical `apiGatewayLogging` settings when they share an account and Region.

## Step 3: Verify locally

```bash
npm run verify
```

This runs linting (ESLint, Ruff), strict type checks (tsc, mypy), the Jest and pytest suites with coverage, and `cdk synth` for both `dev` and `prod` with cdk-nag, so an invalid configuration or an unacknowledged security finding fails here, before anything reaches AWS. It never calls AWS. The first run downloads the Lambda layer wheels into `.cache/pip`. To synthesise a single environment, run `npx cdk synth -c env=prod --no-lookups`.

## Step 4: Prepare the account once

These steps need an administrator of the management account. None of them is performed by the stacks.

1. **Enable StackSets trusted access** for AWS Organizations, then confirm it:

   ```bash
   aws organizations enable-aws-service-access --service-principal member.org.stacksets.cloudformation.amazonaws.com
   aws cloudformation activate-organizations-access --region <region>
   aws cloudformation describe-organizations-access --region <region>
   ```

   The last command must report `ENABLED`. `activate-organizations-access` is the CloudFormation API for the same setting and also creates the service-linked role StackSets needs; running both is harmless. Without trusted access the MemberRole stack fails to create the StackSet.

2. **Enable Amazon Bedrock model access.** A separately authorised operator activates the configured model once, before the first workload run. Both supported models (Claude Haiku 4.5 and Claude Sonnet 4.5) require the Anthropic first-time use (FTU) form, which is submitted once per account, or once in the organisation's management account for every account in the organisation, and covers all commercial Regions except opt-in Regions, where it must be submitted again. The account also needs a valid payment method for AWS Marketplace purchases, because Amazon Bedrock starts an AWS Marketplace subscription on the model's first invocation in the account. The identity that makes that first invocation needs permission to invoke the configured model plus `aws-marketplace:Subscribe`, `aws-marketplace:Unsubscribe` and `aws-marketplace:ViewSubscriptions`; only `aws-marketplace:Subscribe` can be scoped to the model's product with the `aws-marketplace:ProductId` condition key (see [Grant IAM permissions to request access to Amazon Bedrock foundation models with a product ID](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html#model-access-permissions)). The deployed function roles are least-privilege runtime roles and hold no `aws-marketplace:Unsubscribe`, so they are not sufficient to complete the first subscription. After submitting the form, the operator invokes the configured model once, for example in the Amazon Bedrock console playground, and confirms that it answers; AWS documents that later invocations need no AWS Marketplace permissions. The stacks cannot complete any of these steps; until they are done, analysis fails with access errors (see [troubleshooting](troubleshooting.md)). Submit the form in one of two ways:

   - **Console.** In the Amazon Bedrock console in `region`, open the model catalogue entry for the configured Anthropic model and complete the form when it is requested.
   - **CLI or API.** Call `PutUseCaseForModelAccess` with the form answers. Your organisation supplies the answers; this repository does not provide them. Keep them outside the repository: first choose the file's absolute path in the shell you will submit from (the example uses your home directory; any absolute path works):

     ```bash
     FTU_FORM="$HOME/bedrock-ftu-form.json"
     ```

     Write the answers to that file, using the fields of the official form (`intendedUsers` is `"0"` for internal, `"1"` for external or `"2"` for both):

     ```json
     {
       "companyName": "<company-name>",
       "companyWebsite": "<company-website>",
       "intendedUsers": "0",
       "industryOption": "<industry>",
       "otherIndustryOption": "<other-industry-or-empty>",
       "useCases": "<description-of-intended-use>"
     }
     ```

     Then, in the same shell, check that the file is there, submit it and read it back with AWS CLI 2.27.42 or later. `fileb://` sends the file as the binary form data, so no manual Base64 encoding is needed. The path is absolute and quoted, so it works from the repository root or any other directory, and with spaces in the path:

     ```bash
     ls -l -- "$FTU_FORM"
     aws bedrock put-use-case-for-model-access --region <region> --form-data "fileb://$FTU_FORM"
     aws bedrock get-use-case-for-model-access --region <region>
     ```

     If you open a new shell, set `FTU_FORM` again first.

     The caller needs `bedrock:PutUseCaseForModelAccess` (and `bedrock:GetUseCaseForModelAccess` to read it back), the official guide attaches the `AmazonBedrockFullAccess` managed policy for these steps. The agreement can stay pending for a while after a successful submission, during which invocations still return `AccessDeniedException`; AWS sends an email when it is ready. The field list, limits and the `GetFoundationModelAvailability` status check are in [Manage model access using SDK and CLI](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html#model-access-modify) and the [`PutUseCaseForModelAccess` API reference](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_PutUseCaseForModelAccess.html). Delete the form file afterwards (`rm -- "$FTU_FORM"`) if your organisation treats the answers as internal.

   Neither path has been exercised during development; it is listed in [Deferred live verification](#deferred-live-verification). Global inference profiles can route requests to any AWS commercial Region; see [security](security.md#amazon-bedrock-and-global-cross-region-inference). The official overview is [Access Amazon Bedrock foundation models](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html).

3. **Check API Gateway logging ownership** (only when `enableMcp` is `true`). The CloudWatch role used for API Gateway logs is one setting per account and Region:

   ```bash
   aws apigateway get-account --region <region> --query cloudwatchRoleArn --output text
   ```

   `None` means managed mode is appropriate. If another application already owns the setting, use `existing` mode with that role ARN. Read [Shared API Gateway logging prerequisite](mcp-server-deployment.md#shared-api-gateway-logging-prerequisite) before choosing.

4. **Bootstrap the CDK** once per account and Region:

   ```bash
   npx cdk bootstrap aws://<managementAccountId>/<region> -c env=prod
   ```

   The CDK synthesises the app before it bootstraps, so the command needs the same `-c env=<env>` context as a deployment (`-c env=dev` works equally well; the context only selects which configuration is validated). Without it the app stops with a configuration error before anything is created. Review the CloudFormation execution policy the bootstrap stack uses (by default `AdministratorAccess`) as described in [permissions](permissions.md#cloudformation-execution-role).

## Step 5: Review and deploy

```bash
npx cdk diff --all -c env=prod
npx cdk deploy --all -c env=prod --require-approval broadening
```

Always pass `--all`: the app contains several stacks and the CDK deploys them in dependency order (shared API logging when applicable, Core, MemberRole, Analytics, MCP). `--require-approval broadening` asks you to confirm any change that widens IAM permissions or security group rules.

Review the diff before every deployment. In managed logging mode, an added or changed `AWS::ApiGateway::Account` in `CaseInsightsApiLoggingStack-<sharedId>` changes an account- and Region-wide setting that affects every REST API in that Region, including APIs outside this solution. The first deployment of the second environment finds the shared stack unchanged.

The StackSet creates `Support-Case-Analysis-Role-<uniqueId>` in every member account under `memberRoleTargets` and automatically in accounts that join those targets later. The management account itself never receives the role and is not collected. No per-member manual deployment is needed.

## Step 6: Confirm the alarm email subscription

When `alarmEmail` is set, AWS sends a confirmation message to that address after deployment. Select the link in it; nothing is delivered until the subscription is confirmed. Check it:

```bash
aws sns list-subscriptions-by-topic --region <region> \
  --topic-arn arn:aws:sns:<region>:<managementAccountId>:CaseInsights-Alarms-<uniqueId> \
  --query 'Subscriptions[].[Endpoint,SubscriptionArn]' --output table
```

`PendingConfirmation` in place of an ARN means the email has not been confirmed. Notifications contain no case content but do contain operational metadata such as alarm names, metric values, the account ID, the Region and resource names, which leaves the account by email (see [monitoring](monitoring.md#alarm-notifications)).

## Step 7: Validate the deployment

The pipeline is asynchronous: the account lookup returns as soon as it has written the account list, and cases are retrieved, annotated and analysed afterwards through the queues and Step Functions. Run the checks below in order and wait where a step says so; a crawl or query started before processed records exist proves nothing. Replace `<region>` and `<uniqueId>` with the values from `config/<env>.json`. Every regional command names the Region, so the result does not depend on your AWS CLI profile's default Region. Run the steps in one shell, because later steps use the variables set by earlier ones.

1. **Stacks and member role instances.**

   ```bash
   aws cloudformation list-stacks --region <region> --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
     --query "StackSummaries[?starts_with(StackName,'CaseInsights')].StackName"
   aws cloudformation list-stack-instances --region <region> --stack-set-name CaseInsights-MemberRole-<uniqueId> \
     --call-as SELF --query 'Summaries[].[Account,Status]' --output table
   ```

   Every stack of the environment is listed (`CaseInsightsCoreStack-<uniqueId>`, `CaseInsightsMemberRoleStack-<uniqueId>` and, when enabled, the Analytics, MCP and shared API logging stacks). Every targeted member account has a stack instance with status `CURRENT`. `OUTDATED` with a failure reason usually means trusted access or the target IDs are wrong, and an account with no instance at all needs the StackSet steps in [Recover from a failed deployment or update](runbook.md#recover-from-a-failed-deployment-or-update); see also [troubleshooting](troubleshooting.md).

2. **Start a run.**

   ```bash
   aws lambda invoke --region <region> --function-name Lambda-AccountLookup-<uniqueId> account-lookup.json
   cat account-lookup.json
   ```

   The response contains `runId`, `accountListKey`, `accountsSelected` and `created`. Note the `runId` (of the form `YYYYMMDDTHHMMSSZ-<8 hex digits>`) for the next step. Follow the run on the dashboard `CaseInsights-<uniqueId>` as described in [usage](usage.md#following-progress).

3. **Wait until the run has finished processing.** Run Completion checks every hour and, once every account and case of the run has reached a final outcome, writes `runs/<runId>/completion.json` to the raw bucket and emits `DailyRunCompleted`. Look up the bucket, then read the marker:

   ```bash
   RAW_BUCKET=$(aws cloudformation describe-stacks --region <region> --stack-name CaseInsightsCoreStack-<uniqueId> \
     --query "Stacks[0].Outputs[?OutputKey=='RawBucketName'].OutputValue" --output text)
   aws s3 cp --region <region> "s3://$RAW_BUCKET/runs/<runId>/completion.json" -
   ```

   Until the run is complete, the copy fails with a `404` (`Not Found` or `does not exist`); repeat it after the next hourly check. A first run with a backlog of cases can take several hours, because Bedrock calls are limited to `bedrockMaxConcurrency` at a time. If it fails with `403`, your credentials need `s3:GetObject` on `runs/*` in the raw bucket (and `s3:ListBucket` on the bucket, so that S3 reports a missing marker as `404`). The marker's `counts` say what happened:

   - `casesProcessed` greater than 0: processed records exist (it counts succeeded analyses and cases that were already committed). Continue with step 4.
   - `casesProcessed` is 0 (for example `accountsSelected`, `accounts` or `cases` is 0, or every account was skipped): this run committed no records, so there is nothing for Glue or Athena to show yet. This is the expected result for an organisation with no resolved cases in the lookback window, or whose member accounts lack a supported Support plan (`accountsSkipped`; see [SubscriptionRequired](troubleshooting.md#accounts-skipped-subscriptionrequired) and [AccessDenied](troubleshooting.md#accounts-skipped-accessdenied)). Skip steps 4 and 5, complete step 6, and repeat steps 2 to 5 after a later run reports `casesProcessed` above 0.
   - `casesFailed` greater than 0: some analyses failed. Follow [QueueMessages-WorkflowFailures](runbook.md#queuemessages-workflowfailures); the records that did succeed can still be validated with steps 4 and 5.

   If no marker appears within 30 hours, the `MissingDailyRunCompletion` alarm fires; follow [Daily completion](runbook.md#daily-completion).

4. **Crawl the new partitions and wait for the crawl to succeed** (only when `enableAnalytics` is `true`). Start the crawler only now, after processed records exist:

   ```bash
   aws glue start-crawler --region <region> --name case-insights-partition-crawler-<uniqueId>
   sleep 60
   while [ "$(aws glue get-crawler --region <region> --name case-insights-partition-crawler-<uniqueId> \
       --query 'Crawler.State' --output text)" != READY ]; do sleep 30; done
   aws glue get-crawler --region <region> --name case-insights-partition-crawler-<uniqueId> \
     --query 'Crawler.LastCrawl.[Status,StartTime,ErrorMessage]' --output text
   ```

   The state moves from `RUNNING` through `STOPPING` back to `READY`, usually within a few minutes. The last command must show `SUCCEEDED` with a `StartTime` later than your `start-crawler` call. If `start-crawler` fails with `CrawlerRunningException`, a scheduled crawl is running and may have started before the records existed: wait for `READY` with the loop, then run all the commands again. `FAILED` or `CANCELLED` gives the reason in `ErrorMessage` and the `/aws-glue/crawlers` log group; see [Athena troubleshooting](athena.md#troubleshooting). If a command in the loop keeps failing (for example with an access error), the loop keeps waiting: stop it with Ctrl+C and fix the error.

5. **Query Athena and wait for the query to succeed.** The database name is the `GlueDatabaseName` output of the Analytics stack (`case_insights_` followed by `uniqueId` with hyphens replaced by underscores):

   ```bash
   DATABASE=$(aws cloudformation describe-stacks --region <region> --stack-name CaseInsightsAnalyticsStack-<uniqueId> \
     --query "Stacks[0].Outputs[?OutputKey=='GlueDatabaseName'].OutputValue" --output text)
   QUERY_ID=$(aws athena start-query-execution --region <region> --work-group CaseInsights-<uniqueId> \
     --query-execution-context Database="$DATABASE" \
     --query-string 'SELECT rca_category, count(*) AS cases FROM case_summary GROUP BY 1 ORDER BY 2 DESC' \
     --query QueryExecutionId --output text)
   STATE=QUEUED
   while [ "$STATE" = QUEUED ] || [ "$STATE" = RUNNING ]; do
     sleep 5
     STATE=$(aws athena get-query-execution --region <region> --query-execution-id "$QUERY_ID" \
       --query 'QueryExecution.Status.State' --output text) || break
   done
   aws athena get-query-execution --region <region> --query-execution-id "$QUERY_ID" \
     --query 'QueryExecution.Status.[State,StateChangeReason]' --output text
   if [ "$STATE" = SUCCEEDED ]; then
     aws athena get-query-results --region <region> --query-execution-id "$QUERY_ID" --output table
   fi
   ```

   `start-query-execution` returns only a query ID while the query is still `QUEUED` or `RUNNING`, so the loop waits for a final state before any results are read. With `SUCCEEDED`, the results table has a header row and then one row per RCA category, with counts that add up to the processed records the crawl made visible. A header row alone means no partitions are visible: check that step 4 reported `SUCCEEDED` after step 3 reported `casesProcessed` above 0. `FAILED` or `CANCELLED` prints the reason (for example a missing database or table, or Lake Formation permissions); see [Athena troubleshooting](athena.md#troubleshooting). The same query also runs in the Athena console with workgroup `CaseInsights-<uniqueId>` and the database from `GlueDatabaseName`.

6. **Queues and alarms.** Every dead-letter queue and terminal queue is empty, and no alarm is in `ALARM` (see [monitoring](monitoring.md)).
## Dev and prod

The repository ships `config/dev.json` (`uniqueId` `scidev`, Analytics and MCP enabled, no alarm email) and `config/prod.json` (`uniqueId` `sciprod`, Analytics enabled, MCP disabled, alarm email required). Both use invented IDs that you must replace. They can be deployed to the same management account and Region because every name carries the `uniqueId`, including the member role. Recommended practice:

1. Deploy dev first and validate it: `npx cdk diff --all -c env=dev`, then `npx cdk deploy --all -c env=dev`.
2. Use a narrower `memberRoleTargets` (for example a test OU) and a low `bedrockMaxConcurrency` in dev.
3. Keep `orgId`, `managementAccountId`, `region` and the `apiGatewayLogging` block identical in both files, and use different `uniqueId` values.
4. Deploy prod with the commands in [Step 5](#step-5-review-and-deploy).

Dev deletes its buckets, table and log groups when destroyed; prod retains them (see [Uninstall](#uninstall)).

## Connect Kiro to the MCP server

Only when `enableMcp` is `true`. Full details, the Claude Desktop set-up, awscurl and troubleshooting are in [MCP server deployment](mcp-server-deployment.md).

1. Make sure the IAM role or user you will use locally is listed in `mcpAllowedPrincipals` and deploy again if you changed it. `aws sts get-caller-identity` shows a session ARN for roles (`arn:aws:sts::<account-id>:assumed-role/<role-name>/<session>`); list the role ARN instead, including any path, as [permissions](permissions.md#mcp-callers) explains.
2. Get the endpoint (`McpEndpointUrl`, of the form `https://<api-id>.execute-api.<region>.amazonaws.com/v1/mcp`):

   ```bash
   aws cloudformation describe-stacks --region <region> --stack-name CaseInsightsMcpStack-<uniqueId> --query 'Stacks[0].Outputs'
   ```

3. Create the Kiro configuration, then set `MCP_SERVER_URL` to the endpoint and `AWS_REGION` to its Region in `.kiro/settings/mcp.json` (add `AWS_PROFILE` to `env` to use a named profile):

   ```bash
   cp .kiro/settings/mcp.json.example .kiro/settings/mcp.json
   ```

4. Open the repository in Kiro and confirm that the MCP panel lists eight tools. Ask, for example, "Which services drive most of our cases?" or "Summarise last month's cases".
5. Confirm that an unsigned request is refused with HTTP 403:

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' -X POST -d '{}' "https://<api-id>.execute-api.<region>.amazonaws.com/v1/mcp"
   ```

The Kiro configuration runs the local SigV4 bridge `scripts/mcp_aws_client.py`, which signs each request with your credentials; never put credentials in the file.

## Optional: Amazon QuickSight

QuickSight is not provisioned by the stacks. To build dashboards, create an Athena data source in QuickSight that uses the workgroup `CaseInsights-<uniqueId>` and the table `case_summary`, and give the QuickSight service role access to the processed and results buckets. The steps and permissions are in [Optional: Amazon QuickSight](athena.md#optional-amazon-quicksight).

## Optional: CDK pipeline

The pipeline deploys dev, then prod after a manual approval, from `main` through an existing AWS CodeConnections connection. Before its first run:

1. Create and authorise the CodeConnections connection, and bootstrap the account, as described in [Before the first deployment](pipeline.md#before-the-first-deployment-not-done-by-this-repository).
2. Create the pipeline configuration and set `account`, `region` and `connectionArn` (a connection in that account and Region). `repository` must stay `bsadkins/factorytestsample`:

   ```bash
   cp config/pipeline.example.json config/pipeline.json
   ```

3. **Commit `config/pipeline.json`, `config/dev.json` and `config/prod.json` to `main`** before the first pipeline run. The pipeline's Build step synthesises from the repository, so it cannot see files that exist only on your workstation. These files contain account IDs and a connection ARN, not secrets.
4. Check the pipeline app locally, then deploy it once from your workstation; afterwards it updates itself from `main`:

   ```bash
   npx cdk synth -c pipeline=true --no-lookups --output cdk.out/pipeline
   npx cdk deploy -c pipeline=true CaseInsightsPipelineStack
   ```

See [pipeline](pipeline.md) for its stages, trust boundary and costs.

## Update

After changing code, prompts or configuration:

```bash
npm run verify && npx cdk deploy --all -c env=prod
```

Run `npx cdk diff --all -c env=prod` first to review the change. Changing `uniqueId` creates a new, separate deployment rather than renaming the old one; see [Changing configuration safely](configuration.md#changing-configuration-safely). To collect from new accounts or OUs, add them to `memberRoleTargets` and deploy: the StackSet deploys the member role there. Accounts that join an existing target receive the role automatically. Step-by-step procedures for [a new OU or account](runbook.md#collect-from-a-new-organisational-unit-or-account), [quota changes](runbook.md#change-a-bedrock-or-lambda-quota), [model changes](runbook.md#change-the-bedrock-model) and [recovering a failed deployment](runbook.md#recover-from-a-failed-deployment-or-update) are in the runbook.

## Reprocess cases

A case with a committed processed record is never analysed again automatically. After a prompt, taxonomy or model change, reprocess a date range explicitly. Preview first, then run with the plan token the preview prints:

```bash
.venv/bin/python scripts/reprocess-cases.py --env prod --from <YYYY-MM-DD> --to <YYYY-MM-DD> --dry-run
.venv/bin/python scripts/reprocess-cases.py --env prod --from <YYYY-MM-DD> --to <YYYY-MM-DD>
```

Both dates are inclusive UTC days. The script refuses plans of more than 100 cases unless you raise `--max-cases`, and each reprocessed case is analysed again by Bedrock, so it incurs model charges. Options and permissions are in [Reprocess committed cases](runbook.md#reprocess-committed-cases).

## Uninstall

```bash
npx cdk destroy --all -c env=prod
```

The CDK deletes the stacks in reverse dependency order. Deleting the MemberRole stack deletes the StackSet, and CloudFormation removes its stack instances, and therefore every `Support-Case-Analysis-Role-<uniqueId>`, first. What remains and must be removed deliberately:

- **Retained prod data.** In prod the four Core buckets (account-list, raw, processed and access-log), the Athena results bucket, the idempotency table (with deletion protection) and every log group are retained. They hold case data, so empty and delete them manually once you no longer need it. There is no customer managed KMS key to retain or schedule for deletion. Dev deletes all of these.
- **StackSet instances.** Confirm that `aws cloudformation describe-stack-set --region <region> --stack-set-name CaseInsights-MemberRole-<uniqueId>` reports `StackSetNotFoundException`. If the MemberRole stack failed to delete because instances remain, delete them with `aws cloudformation delete-stack-instances` for the same targets, then delete the StackSet and run the destroy command again.
- **Shared API Gateway logging.** The logging role and account setting outlive every environment so that destroying one cannot break another's logs. Remove them by hand after every MCP environment in the account and Region is gone, following [Teardown and final clean-up](mcp-server-deployment.md#teardown-and-final-clean-up).
- **Pipeline, trusted access and bootstrap.** Destroy the pipeline stack and its retained buckets if you deployed it, deactivate StackSets trusted access only if nothing else uses it, and keep the `CDKToolkit` stack if other CDK apps use the account.

The complete, copy-paste teardown, including the commands that empty versioned buckets, is in [Teardown](resources.md#teardown).

## Deferred live verification

None of these checks has been run; each needs a real AWS deployment, and local tests, snapshots and synthesis are not evidence for them. The full list, including MCP and security checks, is in the [README](../README.md#deferred-live-verification).

- [ ] `npx cdk deploy --all -c env=dev` succeeds in a clean, bootstrapped account with trusted access, and a second `npx cdk diff --all -c env=dev` reports no differences.
- [ ] The StackSet instances reach `CURRENT` and the member role exists with the expected trust.
- [ ] A manual run processes real cases through Bedrock, writes processed records, leaves every dead-letter queue empty and emits `DailyRunCompleted`.
- [ ] The Anthropic FTU form is accepted through the console or `aws bedrock put-use-case-for-model-access`, and `aws bedrock get-use-case-for-model-access` returns it.
- [ ] The alarm email subscription is confirmed and a test notification arrives.
- [ ] The crawler adds partitions and the Athena query returns valid categories.
- [ ] Kiro lists the eight tools and runs each of them once. The seven Athena-backed tools (`query_athena`, `get_case_summary`, `get_rca_analysis`, `get_lifecycle_analysis`, `get_service_trends`, `get_temporal_patterns` and `analyze_case_summaries`) return correct results from the real dev Athena workgroup, and `analyze_case_summaries` selects its cases through Athena and then invokes the configured Bedrock model. `analyze_with_bedrock`, given a supplied question and data, invokes only the configured Bedrock model and starts no Athena query. Unsigned requests receive `403`.

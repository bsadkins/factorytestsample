# Support Case Insights

Support Case Insights is an AWS CDK (TypeScript) application that will collect resolved AWS Support cases from every member account of an AWS Organization, summarise and classify them with Amazon Bedrock, and make the results available for analytics and an MCP server.

## Project status

This branch currently contains the project foundation and the shared Python runtime:

- typed, schema-validated environment configuration (`config/`) and the optional pipeline configuration schema;
- the CDK entry point, which validates configuration, registers cdk-nag and synthesises `CaseInsightsCoreStack-<uniqueId>` and `CaseInsightsMemberRoleStack-<uniqueId>`;
- the core storage and coordination foundation: account-list, raw and processed buckets plus a dedicated access-log bucket, the Powertools idempotency table, the stage queues with their dead-letter queues, and the two execution roles that member accounts trust;
- the service-managed `CaseInsights-MemberRole-<uniqueId>` StackSet, which deploys the read-only `Support-Case-Analysis-Role-<uniqueId>` to every account under the configured root or OUs;
- scheduled account discovery (`AccountDiscovery`): an EventBridge Scheduler schedule (default 00:00 UTC) invokes `Lambda-AccountLookup-<uniqueId>`, which pages through Organizations `ListAccounts`, keeps `ACTIVE` member accounts other than the management account and writes `account-lists/<runId>.json`; the S3 notification invokes `Lambda-AccountReader-<uniqueId>`, which validates the list and sends one `{accountId, accountName, runId}` message per account to the active-accounts queue. Scheduled runs take their run ID from the schedule time, so a repeated delivery does not start a second run, and the reader uses Powertools idempotency so duplicate S3 events do not enqueue a run twice. Malformed lists fail visibly and, after retries, land in `CaseInsights-AccountDiscoveryFailures-<uniqueId>`. Writing the account list starts a run; it is not reported as pipeline completion;
- case collection (`CaseCollection`): `Lambda-CaseRetrieval-<uniqueId>` and `Lambda-CaseAnnotation-<uniqueId>` read resolved cases and their complete conversations through the member role into the raw bucket and queue references for analysis;
- case analysis and recovery (`CaseAnalysis`): the workflow starter, the Standard `CaseAnalysis-<uniqueId>` state machine, the failure handler and the daily case cleanup, described in [Case analysis and recovery](#case-analysis-and-recovery);
- observability (`Observability`): the hourly run completion check that emits the daily-completion heartbeat, the alarm topic, the alarms and the operations dashboard, described in [Observability and daily completion](#observability-and-daily-completion);
- reusable constructs for later pipeline stages: a standard Python function factory (`PipelineFunction`) and the Bedrock model allowlist policy (`BedrockInvokePolicy`);
- the shared Python package `src/lambda/common/` (taxonomies, prompt rendering, model-output validation, Bedrock, STS, S3, SQS and retry helpers) and the prompt templates in `src/templates/`, packaged as two Lambda layers;
- the optional Analytics stack (`enableAnalytics`) and the CSV exporter `scripts/generate_csv_insights.py`, described in [Analytics](#analytics);
- strict TypeScript and Python tooling, exact dependency pins, an npm lockfile and hash-locked Python requirements;
- local lint, type-check, test, synth and security commands.

The MCP server's CDK stack, CDK pipeline and full documentation are **not implemented yet**. Available guides: [docs/athena.md](docs/athena.md) (Athena analytics and optional QuickSight setup), [docs/csv_export.md](docs/csv_export.md) (CSV export for Power BI and Excel), [docs/mcp-server-deployment.md](docs/mcp-server-deployment.md) (the SigV4 stdio bridge `scripts/mcp_aws_client.py` with Kiro and Claude Desktop setup) and [docs/runbook.md](docs/runbook.md), a provisional alarm runbook. Nothing has been deployed to AWS; StackSet instance creation is untested until a real deployment.

## Security foundation

- Every bucket is private (`BLOCK_ALL`, bucket-owner enforced), versioned, SSE-S3 encrypted and denies requests without TLS 1.2 or later. The account-list, raw and processed buckets write server access logs to a dedicated bucket that does not log to itself. Raw data and account lists expire after `rawRetentionDays`; processed records move to Standard-IA after `operations.storage.processedInfrequentAccessDays` (S3 does not transition objects smaller than 128 KB by default, so small records stay in Standard).
- Prod retains buckets, the idempotency table and log groups on stack deletion and enables DynamoDB deletion protection. Dev deletes them and empties its buckets first.
- Queues use SSE-SQS and deny requests without TLS. Each work queue has its own dead-letter queue with the configured receive count, and its visibility timeout is at least six times the consumer timeout. Dead-letter retention (`operations.queues.dlqRetentionDays`, minimum 5) must exceed the 4-day source retention because SQS keeps a message's original enqueue time.
- The member role trusts only `CaseInsights-CaseRetrieval-<uniqueId>` and `CaseInsights-CaseAnnotation-<uniqueId>` in the management account, with an `aws:PrincipalOrgID` condition, and grants only `support:DescribeCases` and `support:DescribeCommunications`. Those two roles can assume only that role name, in accounts of the configured organization. Service-managed StackSets never deploy to the management account. StackSets trusted access must be enabled before deployment.
- `BedrockInvokePolicy` allows the configured global inference profile plus its Regional and global foundation-model ARNs (only through that profile), explicitly denies every other model, limits `aws-marketplace:Subscribe` to the approved product IDs (`prod-xdkflymybwmvi` for Claude Haiku 4.5, `prod-mxcfnwvpd6kb4` for Claude Sonnet 4.5) and allows `aws-marketplace:ViewSubscriptions`, which has no resource or condition scoping. IAM cannot complete Anthropic's first-time-use form; that remains an account prerequisite.
- Each schedule (account lookup, cleanup, run completion) has its own execution role that can invoke only its target function. The roles trust `scheduler.amazonaws.com` only with `aws:SourceAccount` equal to the management account and `aws:SourceArn` equal to the `default` schedule group ARN. Scheduler always sends the schedule group ARN, so a trust policy cannot name an individual schedule.
- cdk-nag acknowledgements are made with `acknowledgeNagFinding` on the narrowest construct and finding ID, each with a written reason; `test/app.test.ts` lists every Core acknowledgement and `test/stacks/analytics-stack.test.ts` every Analytics acknowledgement.

## Case analysis and recovery

- **Starting work.** `Lambda-WorkflowStarter-<uniqueId>` consumes the summary queue (batches of 10, partial-batch failure reporting, Powertools idempotency). It never starts a case that has a processed record. Each case attempt gets a stable execution name `<account>-<case>-<runId>-a<attempt>`, and the raw-bucket ownership marker `account_number=<id>/case_number=<displayId>/workflow.json` (written with S3 conditional writes) names the execution that owns the case. A RUNNING execution is left alone, so duplicate deliveries and overlapping runs never create a second active execution. `ExecutionAlreadyExists` is never treated as success: the starter describes the execution and decides. A closed FAILED, TIMED_OUT or ABORTED execution is recovered under the next attempt name, which avoids the 90-day reuse restriction on closed execution names.
- **Workflow.** `CaseAnalysis-<uniqueId>` is a Standard state machine: Summary → RCA → Lifecycle → UpdateCaseMetadata. State holds references only, and execution data is excluded from its logs. Every task retries Lambda throttling (8 attempts, full jitter, at most about 15 minutes), the stage's remaining Bedrock throttling or transient errors, and S3, network and timeout errors (each `operations.workflow.taskMaxAttempts`). Any other error, or exhausted retries, goes to HandleFailure, which sends one safe failure record (case reference fields, failure type, stage, error type; no case content or error text) to `CaseInsights-WorkflowFailures-<uniqueId>` and then fails the execution. If that send fails, the handler raises and Step Functions retries it. A task that is retried after an uncertain outcome may rarely repeat one inference; stage checkpoints and the processed-record check before each inference keep this rare, but exactly-once inference is not promised.
- **Bedrock concurrency.** Summary, RCA and Lifecycle run in one function, `Lambda-CaseAnalysisStages-<uniqueId>`, whose reserved concurrency equals `bedrockMaxConcurrency`. Each invocation runs one stage and makes its Bedrock calls one at a time, so concurrent Bedrock requests never exceed the configured value, including caps of 1 or 2. Starter concurrency (`operations.workflow.starterMaxConcurrency`) only limits how fast executions start; it is not a limit on running executions. When more executions are running than the cap allows, Lambda throttles the extra stage invocations and the workflow retries them. With a large backlog and a small cap, some attempts can exhaust that retry budget; they are recorded as failures and a later run retries them.
- **Failure records and redrive.** The failure queue has no consumer. To retry a recorded case before the next scheduled run, send the message's case reference fields (`accountId`, `caseId`, `displayId`, `runId`, `rawKey`, `communicationsKey`, `communicationsVersionId`, `communicationCount`) to the summary queue with a new `redriveId` (1 to 64 letters, digits or hyphens). The starter then starts the next attempt; the processed record still prevents any rework of a committed case.
- **Conversations too large to summarise.** Annotation stores the complete conversation but sends a `ConversationTooLarge` record (with `retryable: false`) to the failure queue instead of truncating it. It also writes `terminal-failure.json` with a digest of the conversation, so later runs report the case again only when its content changes; unchanged content is never retried automatically. A manual redrive of an unchanged oversized case fails again in the Summary stage and produces a new failure record; it cannot loop.
- **Timeouts, aborts and unrecorded failures.** A whole-execution timeout or abort bypasses every task Catch, and HandleFailure can exhaust its own retries, so some closed attempts never record a failure. Before the starter replaces, or cleanup deletes, the ownership marker of a closed FAILED, TIMED_OUT or ABORTED execution, it writes a pending failure-delivery marker `account_number=<id>/case_number=<displayId>/failure-deliveries/<executionName>.json`. Cleanup (daily) and run completion (hourly) send a `WorkflowExecutionClosed` record for each pending marker and then mark it `sent`; HandleFailure marks its own records `sent` too and does not resend a confirmed one. Delivery is at least once: a crash between sending and marking repeats the record with the same `deliveryKey` (the execution name). A failed send or S3 error leaves the marker pending for the next attempt. Markers only stop duplicate records for one execution; they never stop a later run from retrying the case, and cleanup cannot delete them.
- **Cleanup.** `Lambda-CaseCleanup-<uniqueId>` runs daily (`schedules.cleanup`, default 22:00 UTC) and pages through the raw bucket. It resets a case only when it has no processed record, all of its objects are older than `operations.cleanup.graceHours`, no RUNNING execution belongs to it (a paginated top-level listing; an owner waiting in PENDING_REDRIVE is caught through DescribeExecution) and its ownership marker names a closed or never-started execution. Before deleting anything, cleanup replaces exactly the marker it judged with a reset claim in `workflow.json` (a conditional write), so a starter that took ownership meanwhile wins and keeps the inputs. Starters never overwrite a recent claim; they fail the record and retry. Cleanup then deletes `data.json` and `communications.json`, each conditional on the ETag it checked, and finally its claim, so the next run collects and analyses the case again. If an input changed or a processed record appeared, the claim is released and the inputs stay. A claim left by an interrupted cleanup is completed by a later cleanup, or taken over by a starter, once it is an hour old. Checkpoints and terminal markers are kept. Unknown or failed status checks never lead to a deletion. When its time budget runs out, cleanup saves its position under `cleanup-state/` and the next run resumes there.

## Observability and daily completion

- **Telemetry.** Every function uses Powertools structured JSON logging, EMF metrics (namespace `SupportCaseInsights`, `service` dimension only) and active X-Ray tracing, with event, response and error capture disabled. Log groups keep `logRetentionDays` (default 90). The state machine has X-Ray tracing and excludes execution data from its logs. Metrics are counts and durations only, never account or case content.
- **Run markers.** Case retrieval writes `runs/<runId>/accounts/<accountId>.json` in the raw bucket: `dispatching` with the cases it is about to queue before sending any reference, then `retrieved` once all are queued, or `skipped`. The recorded cases never shrink within a run: a retry, or a retry that loses access, keeps and re-queues every case an earlier attempt recorded. Annotation writes `runs/<runId>/cases/<accountId>/<displayId>.json` for cases whose work ends there (not found, skipped, already processed, too large to summarise); the starter writes it with the owning execution name, or `alreadyProcessed`. These markers are evidence for completion only; SQS and Step Functions still coordinate the work, and no extra coordination service is used.
- **Daily-completion heartbeat.** `Lambda-RunCompletion-<uniqueId>` runs hourly (`CaseInsights-RunCompletion-<uniqueId>`) and checks every account list written in the last 72 hours. A run is complete only when every listed account has a settled account marker (`retrieved` or `skipped`), every case an account marker lists has a case marker, and every case is terminal: skipped, already processed, too large to summarise, a SUCCEEDED execution, or a closed failed execution whose failure record is confirmed (run completion delivers it itself when missing). Runs with no accounts, or only accounts without cases, complete on the next check. The first check that finds a run complete writes `runs/<runId>/completion.json` (conditional create) and emits `DailyRunCompleted`. Account discovery never emits it.
- **Alarms.** All alarms notify the SNS topic `CaseInsights-Alarms-<uniqueId>`: errors of every function, CaseAnalysis executions failed, timed out or aborted, any visible message in each dead-letter queue and in the workflow-failure and discovery-failure terminal queues, failure records written (including conversations too large to summarise and reconciled records), failure-delivery errors, cleanup errors, persistent run completion errors, and no `DailyRunCompleted` for 30 hours (missing data counts as breaching). Each alarm has an entry in [docs/runbook.md](docs/runbook.md).
- **Email notifications.** Set `alarmEmail` to subscribe an address (required in prod). AWS emails a confirmation link after deployment; notifications are delivered only after the recipient confirms. The topic requires TLS but is not encrypted at rest: CloudWatch alarms can publish to an encrypted topic only through a customer managed KMS key, which this solution does not use. Notifications carry alarm names and states, never case content. The topic policy lets `cloudwatch.amazonaws.com` publish only for alarms in the management account named `CaseInsights-<uniqueId>-*`.
- **Dashboard.** `CaseInsights-<uniqueId>` shows accounts discovered, excluded and skipped; cases retrieved, annotated, processed and failed; Bedrock latency, tokens, throttles and failures; workflow executions and success rate; dead-letter and terminal queue depth; run completion; cleanup and failure-record reconciliation; Lambda errors; and the state of every alarm.

## Analytics

- **Stack.** With `enableAnalytics` set (the default in both shipped configurations), `CaseInsightsAnalyticsStack-<uniqueId>` is deployed after Core. It reads the Core processed and access-log buckets through CDK weak cross-stack references (no exports); Core never references it. With the flag off no Glue, Athena or results-bucket resources exist anywhere.
- **Catalog.** Glue database `case_insights_<uniqueId>` (hyphens become underscores) holds the external table `case_summary` over `s3://<processed bucket>/`, partitioned by `account_number` and `case_number` (`account_number=<id>/case_number=<displayId>/data.json`). It uses the OpenX JSON SerDe with case-sensitive matching: Glue column names are lower case, and each column maps explicitly to its mixed-case JSON key (for example `rca_category` reads `RCA_Category`). Every column is a string; dates stay ISO-8601 text. A Jest test checks the columns against `common.processed_record.PROCESSED_FIELDS`.
- **Partition refresh.** The crawler `case-insights-partition-crawler-<uniqueId>` runs on `schedules.crawler` (default `cron(0 9 * * ? *)`, 09:00 UTC). It targets the existing table, adds new partitions and only logs schema changes, so newly processed cases become queryable after the next crawl. Run it sooner with the `ManualCrawlerStartCommand` stack output (`aws glue start-crawler --name case-insights-partition-crawler-<uniqueId> --region <region>`). Its role, trusted only by Glue, can read processed records and refresh partitions of this table; it cannot write or delete records.
- **Workgroup.** `CaseInsights-<uniqueId>` (Athena engine version 3) enforces its configuration: results go to `query-results/` in a dedicated private, versioned, SSE-S3 results bucket (`SSE_S3` result encryption, no KMS key) that expires results after `operations.athena.resultsRetentionDays` (default 30), and each query stops after `operations.athena.bytesScannedCutoffPerQuery` bytes (default 10 GiB, minimum 10 MiB). The results bucket logs to the Core access-log bucket under `athena-results/`.
- **Queries and QuickSight.** [docs/athena.md](docs/athena.md) documents the table schema and column mappings, crawler delay and manual refresh, analyst permissions, the example query library (RCA distribution, lifecycle categories by service, day-of-week, week-of-month and monthly patterns, severity trends and top accounts) and an optional manual QuickSight setup. QuickSight is not provisioned by this application.
- **CSV export.** `scripts/generate_csv_insights.py` exports processed records straight from the processed bucket to CSV with boto3 and pandas. It needs only `s3:ListBucket` on the bucket and `s3:GetObject` on `account_number=*`, does not use Athena and does not wait for the crawler. [docs/csv_export.md](docs/csv_export.md) covers permissions, every option, filtering, output, exit codes, scheduling, Power BI and Excel.

## Prerequisites

- Node.js 20 or later (`.nvmrc` pins the version used for development, 24.22.3).
- Python 3.12.
- No Docker, Finch or AWS credentials are needed for local verification. The pinned CDK CLI is installed as a development dependency; run it with `npx cdk`.

## Set up

```bash
npm ci
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
```

## Local commands

| Command | What it runs |
| --- | --- |
| `npm run lint` | ESLint (TypeScript and scripts) and Ruff lint/format checks |
| `npm run typecheck` | `tsc --noEmit` (strict) and mypy (strict) |
| `npm test` | Jest (config, CDK assertions and snapshot, scanner) and pytest with coverage (80% minimum for `src/lambda`) |
| `npm run synth` | `cdk synth` for `dev` and `prod` with cdk-nag; any unacknowledged finding fails |
| `npm run verify` | lint, typecheck, tests and synth |
| `npm run security` | Full-lockfile npm advisory scan, `pip-audit` of both Python locks, and Bandit |

Synthesise a single environment with:

```bash
npx cdk synth -c env=dev --no-lookups
```

The `env` context is required and must be `dev` or `prod`; the app reads `config/<env>.json`.

### What local verification proves

`npm run verify` runs entirely on the local machine with invented data; it never deploys, bootstraps or calls AWS. Besides the per-handler tests, `test/lambda/test_core_pipeline.py` runs the Core pipeline end to end through the real handlers: scheduled discovery, the account-list S3 notification, the account reader, case retrieval and annotation, the workflow starter, every CaseAnalysis task and the processed-record commit, plus run completion and cleanup. `test/support/pipeline_kit.py` stands in only for the AWS services: moto S3, SQS and DynamoDB, SQS event source mapping semantics (batches, partial-batch failures, visibility timeouts, dead-letter redrive), the state machine's task order, retries and Catch, and paginated synthetic Organizations, Support and Bedrock responses. It covers pagination, excluded and skipped accounts, unresolved and already-processed cases, complete conversations, duplicate deliveries, stage and commit faults, redrive and next-run recovery, cleanup of abandoned work next to running work, and when the daily-completion heartbeat is emitted.

`enableAnalytics` adds the Analytics stack; `enableMcp` (also `true` in `config/dev.json`, and only valid with Analytics) adds the MCP stack, whose alarms and dashboard widgets are added to Core. `test/stacks/analytics-stack.test.ts` synthesises Analytics on and MCP off, and Analytics off; `test/stacks/mcp-stack.test.ts` synthesises the dev MCP stack, prod with MCP enabled and both environments with MCP off, and runs the packaged MCP function with the Core layer for `initialize` and `tools/list`.

### Deferred live verification

None of the following has been run. Local tests, snapshots, synth and cdk-nag results are not evidence for them; each needs a real AWS deployment:

- [ ] Clean deployment of the dev configuration (`npx cdk deploy -c env=dev`) after bootstrapping and enabling StackSets trusted access.
- [ ] A second deployment with no changes reports no differences (`npx cdk diff -c env=dev`).
- [ ] The member-role StackSet reaches `CURRENT` and `Support-Case-Analysis-Role-<uniqueId>` exists in the targeted member accounts with the expected trust.
- [ ] Member accounts with Business, Enterprise On-Ramp or Enterprise Support return cases; accounts without that entitlement are skipped as `SubscriptionRequired`.
- [ ] A scheduled or manual run processes real cases through Bedrock (model access and Marketplace subscription in place) and writes processed records.
- [ ] Every dead-letter queue and the terminal failure queues stay empty after a normal run.
- [ ] The dashboard shows data, the alarms are in the expected state and the alarm email subscription is confirmed; `DailyRunCompleted` arrives after a run.
- [ ] The crawler adds partitions for real processed records, and Athena queries in `CaseInsights-<uniqueId>` (including the [query library](docs/athena.md#query-library)) return them with results encrypted in the results bucket.
- [ ] The MCP endpoint answers signed requests from an allowed principal in the Organization, refuses unsigned requests, unlisted principals and principals outside the Organization with `403`, and WAF returns `413` for bodies over 32 KiB and `429` above the per-IP rate (see [MCP server deployment](docs/mcp-server-deployment.md#infrastructure-and-security-controls)).
- [ ] The optional manual QuickSight setup works with the enforced workgroup, and a CSV export from the real processed bucket succeeds with only the documented S3 permissions.
- [ ] MCP client connectivity, including Kiro, and rejection of unsigned requests with HTTP 403 (once the MCP server exists).

## Configuration

`config/schema.json` is the published contract for `config/<env>.json`. The loader in `lib/config/` validates each file against it, applies its defaults, then checks the cross-field rules listed in the schema `$comment`. Synthesis stops with a list of problems if anything is invalid.

| File | Purpose |
| --- | --- |
| `config/example.json` | Template listing every field explicitly (copy it to create your own prod configuration) |
| `config/dev.json` | Fictitious dev configuration; Analytics and MCP enabled |
| `config/prod.json` | Fictitious prod configuration with an alarm email |
| `config/pipeline.schema.json`, `config/pipeline.example.json` | Optional CDK Pipelines settings, validated separately |

Key fields and defaults: `uniqueId`, `orgId`, `managementAccountId`, `region` (`us-east-1`), `environment` (`dev` or `prod`), `enableAnalytics`, `enableMcp`, `memberRoleTargets` (root or OU IDs), `bedrockModelId` (`global.anthropic.claude-haiku-4-5-20251001-v1:0`; the only alternative is `global.anthropic.claude-sonnet-4-5-20250929-v1:0`), `bedrockMaxTokens` (1000), `bedrockMaxConcurrency` (5), `lookbackMonths` (12), `rawRetentionDays` (90), `logRetentionDays` (90), `alarmEmail`, `mcpAllowedPrincipals` and `schedules` (account lookup `cron(0 0 * * ? *)`, cleanup `cron(0 22 * * ? *)`, crawler `cron(0 9 * * ? *)`, all UTC). The `operations` object holds bounded queue, function, workflow, cleanup, storage, Athena and MCP limits.

Rules enforced before synthesis include:

- `alarmEmail` is required in prod;
- `enableMcp` requires `enableAnalytics` and at least one MCP principal;
- only the two global Bedrock inference profiles above are accepted;
- queue visibility timeouts are at least six consumer timeouts, cleanup grace exceeds the workflow timeout, and raw retention exceeds cleanup grace;
- unknown fields are rejected.

The pipeline configuration requires an existing CodeConnections ARN in the pipeline account and the `main` branch. Dev and prod must use different `uniqueId` values in the same organization and management account.

## Python Lambda packaging

Lambda code is packaged locally during `cdk synth`; Docker, Finch and manual uploads are not used. CDK uploads the assets on deploy.

| Layer | Contents | Mounted at |
| --- | --- | --- |
| `case-insights-common-<uniqueId>` | Every package in `requirements.txt` plus `src/lambda/common/` | `/opt/python` (import `common`) |
| `case-insights-templates-<uniqueId>` | `src/templates/*.txt` | `/opt/templates` |

- `scripts/build/bundle_python_layer.py` installs the runtime lock with `pip --require-hashes --only-binary=:all:` for CPython 3.12 on Linux x86_64 (`manylinux_2_28_x86_64`). A missing wheel, an unhashed requirement or a hash mismatch fails synthesis; nothing is compiled on the host and there is no Docker fallback.
- Synthesis runs the bundler with `.venv/bin/python` (override with `CASE_INSIGHTS_PYTHON`). Downloaded wheels are cached in the ignored, repository-local `.cache/pip`; the first synth needs access to the package index.
- Asset hashes are computed from file contents: the shared layer from `src/lambda/common/`, `requirements.txt` and the bundler; the templates layer from the template files. Editing only a template changes only the templates layer, and `npx cdk deploy` publishes a new layer version.
- `common.templates.prompt_version()` returns a digest of the templates and taxonomies, recorded as `Prompt_Version`. The stage function and `Lambda-UpdateCaseMetadata-<uniqueId>` both attach the templates layer, because every commit recomputes it.

### Prompts and taxonomies

- `src/lambda/common/taxonomy.py` is the only place RCA and lifecycle categories and their definitions are written. Templates insert them through the `{categories}` placeholder, and `python scripts/render_taxonomy_docs.py` renders them as Markdown (`--check FILE` / `--write FILE` keep a marked documentation block in sync).
- Prompt regression fixtures live in `test/fixtures/prompts/` and run with the normal test suite. A regression against the real Bedrock API is opt-in and never runs by default:

  ```bash
  CASE_INSIGHTS_LIVE_BEDROCK=1 .venv/bin/pytest test/prompts/test_live_bedrock_prompts.py --no-cov
  ```

  It needs AWS credentials allowed to invoke the configured global inference profile (`CASE_INSIGHTS_LIVE_MODEL_ID`, `CASE_INSIGHTS_LIVE_REGION`) and sends only the invented fixtures.

## Dependencies and locks

- npm packages are pinned exactly in `package.json` and locked in `package-lock.json`. `overrides` move transitive `glob`, `minimatch` and `test-exclude` to patched releases.
- `requirements.txt` (Lambda layer) and `requirements-dev.txt` (development tools) are fully hash-locked. Regenerate them from the `.in` files with the `uv pip compile` commands at the top of each `.in` file. The runtime lock resolves for Python 3.12 on Linux x86_64 and contains only wheels.
- `scripts/security/npm-advisory-scan.cjs` replaces `npm audit`, which stalls in some environments. It posts every locked package name and version to the npm bulk advisory endpoint and fails on any finding unless an entry in `scripts/security/npm-advisory-exceptions.json` accepts it with a specific reason and review date. Two advisories are accepted at present: `minimatch` bundled inside `aws-cdk-lib` (no fixed release) and `deepmerge` used only by Jest (no fixed release). Scan errors never count as a clean result.

## Repository layout

```
bin/                 CDK app entry point
lib/config/          Typed configuration, validation and resource names
lib/constructs/      CDK constructs (storage, queues, roles, function factory, Bedrock policy, layers, pipeline stages)
lib/packaging/       Docker-free Python asset bundling and content hashing
lib/stacks/          CDK stacks
lib/nag.ts           cdk-nag registration
docs/                Operations documentation (Athena and CSV export guides, provisional alarm runbook)
config/              Schemas and environment configuration
src/lambda/<name>/   One directory per Lambda handler (app.py)
src/lambda/common/   Shared Python Lambda package (shared layer, /opt/python/common)
src/templates/       Prompt templates (templates layer, /opt/templates)
scripts/             CSV exporter (generate_csv_insights.py) and taxonomy documentation renderer
scripts/build/       Wheel-only layer bundler
scripts/security/    Dependency advisory scanner
test/                Jest and pytest suites; prompt fixtures in test/fixtures/prompts
```

## Licence

See [LICENSE](LICENSE).

# Support Case Insights

Support Case Insights is an AWS CDK (TypeScript) application that will collect resolved AWS Support cases from every member account of an AWS Organization, summarise and classify them with Amazon Bedrock, and make the results available for analytics and an MCP server.

## Project status

This branch currently contains the project foundation and the shared Python runtime:

- typed, schema-validated environment configuration (`config/`) and the optional pipeline configuration schema;
- the CDK entry point, which validates configuration, registers cdk-nag and synthesises `CaseInsightsCoreStack-<uniqueId>` and `CaseInsightsMemberRoleStack-<uniqueId>`;
- the core storage and coordination foundation: account-list, raw and processed buckets plus a dedicated access-log bucket, the Powertools idempotency table, the stage queues with their dead-letter queues, and the two execution roles that member accounts trust;
- the service-managed `CaseInsights-MemberRole-<uniqueId>` StackSet, which deploys the read-only `Support-Case-Analysis-Role-<uniqueId>` to every account under the configured root or OUs;
- reusable constructs for later pipeline stages: a standard Python function factory (`PipelineFunction`) and the Bedrock model allowlist policy (`BedrockInvokePolicy`);
- the shared Python package `src/lambda/common/` (taxonomies, prompt rendering, model-output validation, Bedrock, STS, S3, SQS and retry helpers) and the prompt templates in `src/templates/`, packaged as two Lambda layers;
- strict TypeScript and Python tooling, exact dependency pins, an npm lockfile and hash-locked Python requirements;
- local lint, type-check, test, synth and security commands.

The data pipeline Lambda functions, workflow, schedules, dashboard and alarms, Analytics stack, MCP server, CDK pipeline and full documentation are **not implemented yet**. The core stack currently contains storage, queues, roles and the two Lambda layers but no functions, so it is not a working deployment. Nothing has been deployed to AWS; StackSet instance creation is untested until a real deployment.

## Security foundation

- Every bucket is private (`BLOCK_ALL`, bucket-owner enforced), versioned, SSE-S3 encrypted and denies requests without TLS 1.2 or later. The account-list, raw and processed buckets write server access logs to a dedicated bucket that does not log to itself. Raw data and account lists expire after `rawRetentionDays`; processed records move to Standard-IA after `operations.storage.processedInfrequentAccessDays` (S3 does not transition objects smaller than 128 KB by default, so small records stay in Standard).
- Prod retains buckets, the idempotency table and log groups on stack deletion and enables DynamoDB deletion protection. Dev deletes them and empties its buckets first.
- Queues use SSE-SQS and deny requests without TLS. Each work queue has its own dead-letter queue with the configured receive count, and its visibility timeout is at least six times the consumer timeout. Dead-letter retention (`operations.queues.dlqRetentionDays`, minimum 5) must exceed the 4-day source retention because SQS keeps a message's original enqueue time.
- The member role trusts only `CaseInsights-CaseRetrieval-<uniqueId>` and `CaseInsights-CaseAnnotation-<uniqueId>` in the management account, with an `aws:PrincipalOrgID` condition, and grants only `support:DescribeCases` and `support:DescribeCommunications`. Those two roles can assume only that role name, in accounts of the configured organization. Service-managed StackSets never deploy to the management account. StackSets trusted access must be enabled before deployment.
- `BedrockInvokePolicy` allows the configured global inference profile plus its Regional and global foundation-model ARNs (only through that profile), explicitly denies every other model, limits `aws-marketplace:Subscribe` to the approved product IDs (`prod-xdkflymybwmvi` for Claude Haiku 4.5, `prod-mxcfnwvpd6kb4` for Claude Sonnet 4.5) and allows `aws-marketplace:ViewSubscriptions`, which has no resource or condition scoping. IAM cannot complete Anthropic's first-time-use form; that remains an account prerequisite.
- cdk-nag acknowledgements are made with `acknowledgeNagFinding` on the narrowest construct and finding ID, each with a written reason; `test/app.test.ts` lists every acknowledgement in the app.

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
- `common.templates.prompt_version()` returns a digest of the templates and taxonomies, recorded as `Prompt_Version`.

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
lib/constructs/      Reusable CDK constructs (storage, queues, roles, function factory, Bedrock policy, layers)
lib/packaging/       Docker-free Python asset bundling and content hashing
lib/stacks/          CDK stacks
lib/nag.ts           cdk-nag registration
config/              Schemas and environment configuration
src/lambda/common/   Shared Python Lambda package (shared layer, /opt/python/common)
src/templates/       Prompt templates (templates layer, /opt/templates)
scripts/build/       Wheel-only layer bundler
scripts/security/    Dependency advisory scanner
test/                Jest and pytest suites; prompt fixtures in test/fixtures/prompts
```

## Licence

See [LICENSE](LICENSE).

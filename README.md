# Support Case Insights

Support Case Insights is an AWS CDK (TypeScript) application that will collect resolved AWS Support cases from every member account of an AWS Organization, summarise and classify them with Amazon Bedrock, and make the results available for analytics and an MCP server.

## Project status

This branch currently contains the project foundation only:

- typed, schema-validated environment configuration (`config/`) and the optional pipeline configuration schema;
- the CDK entry point, which validates configuration, registers cdk-nag and synthesises an empty `CaseInsightsCoreStack-<uniqueId>` scaffold;
- strict TypeScript and Python tooling, exact dependency pins, an npm lockfile and hash-locked Python requirements;
- local lint, type-check, test, synth and security commands.

The data pipeline, Lambda functions, member-role StackSet, Analytics stack, MCP server, CDK pipeline and full documentation are **not implemented yet**. The core stack template has no resources and is not deployable. Nothing has been deployed to AWS.

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

## Dependencies and locks

- npm packages are pinned exactly in `package.json` and locked in `package-lock.json`. `overrides` move transitive `glob`, `minimatch` and `test-exclude` to patched releases.
- `requirements.txt` (Lambda layer) and `requirements-dev.txt` (development tools) are fully hash-locked. Regenerate them from the `.in` files with the `uv pip compile` commands at the top of each `.in` file. The runtime lock resolves for Python 3.12 on Linux x86_64 and contains only wheels.
- `scripts/security/npm-advisory-scan.cjs` replaces `npm audit`, which stalls in some environments. It posts every locked package name and version to the npm bulk advisory endpoint and fails on any finding unless an entry in `scripts/security/npm-advisory-exceptions.json` accepts it with a specific reason and review date. Two advisories are accepted at present: `minimatch` bundled inside `aws-cdk-lib` (no fixed release) and `deepmerge` used only by Jest (no fixed release). Scan errors never count as a clean result.

## Repository layout

```
bin/                 CDK app entry point
lib/config/          Typed configuration, validation and resource names
lib/stacks/          CDK stacks
lib/nag.ts           cdk-nag registration
config/              Schemas and environment configuration
src/lambda/common/   Shared Python Lambda package (placeholder)
scripts/security/    Dependency advisory scanner
test/                Jest and pytest suites
```

## Licence

See [LICENSE](LICENSE).

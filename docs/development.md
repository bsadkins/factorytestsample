# Development guide

This document is for contributors changing the code. It covers the toolchain and local set-up, the repository layout, the npm scripts that lint, type-check, test, synthesise and scan the project, how Python Lambda assets are packaged, how dependencies are pinned and updated, how cdk-nag findings are handled, and checklists for the most common changes: a new Lambda function, a new MCP tool, a new configuration field and a prompt or taxonomy change. Contribution rules such as branch and review policy belong in [CONTRIBUTING.md](../CONTRIBUTING.md). Run every command from the repository root.

## Toolchain

| Tool | Version | Notes |
| --- | --- | --- |
| Node.js | `.nvmrc` pins 24.22.3 | `package.json` declares `^20.19.0 \|\| ^22.13.0 \|\| >=24`: Node.js 20 LTS from 20.19, 22 from 22.13, or 24 and later, the range the locked dependencies (ESLint 10) support; development and the pipeline use the `.nvmrc` version |
| Python | 3.12 (`.python-version`; `pyproject.toml` requires `>=3.12,<3.13`) | Matches the Lambda runtime |
| AWS CDK | `aws-cdk-lib` 2.276.0, CLI `aws-cdk` 2.1148.1 | Use the project's CLI through `npx cdk`, not a global installation |
| TypeScript, Jest, ESLint | `typescript` 6.0.3, `jest` 30.5.2, `eslint` 10.14.0 | Exact versions in `package.json` |
| Python tooling | `ruff`, `mypy`, `pytest`, `pytest-cov`, `moto`, `bandit`, `pip-audit`, `uv` | Exact versions in `requirements-dev.in` |

Docker and Finch are not used. Python assets are bundled from hash-locked wheels on the local machine (see [Python asset packaging](#python-asset-packaging)).

## Set-up

```bash
nvm use   # or install the Node.js version in .nvmrc another way
npm ci
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
```

The npm scripts call the tools in `.venv/bin/` directly, so the virtual environment must be at `.venv`. No AWS credentials are needed for linting, tests or synthesis: synthesis uses `--no-lookups`, and the tests use local stand-ins for AWS. See [installation.md](installation.md) for deployment prerequisites.

## Repository layout

| Path | Content |
| --- | --- |
| `bin/case-insights.ts` | CDK app entry point |
| `lib/app.ts`, `lib/index.ts` | Builds the environment stacks for `-c env=<env>`; public API used by the optional pipeline |
| `lib/config/` | Configuration types (`types.ts`), loading and cross-field validation (`load.ts`), JSON Schema validation (`schema-validator.ts`), resource naming (`naming.ts`), API Gateway logging and pipeline settings |
| `lib/stacks/` | Core, member role, Analytics, MCP, shared API logging and pipeline stacks |
| `lib/constructs/` | Reusable constructs, for example `pipeline-function.ts`, `observability.ts`, `bedrock-invoke-policy.ts`, `mcp-server.ts` and `shared-layers.ts` |
| `lib/stages/`, `lib/pipeline-app.ts` | Optional CDK Pipelines stages and app (see [pipeline.md](pipeline.md)) |
| `lib/packaging/python-assets.ts` | Lambda handler and layer assets |
| `lib/nag.ts` | cdk-nag set-up and acknowledgement helpers |
| `src/lambda/<function>/app.py` | One package per Lambda handler: `account_lookup`, `account_reader`, `case_retrieval`, `case_annotation`, `workflow_starter`, `analysis_stages` (dispatches to `case_summary`, `case_rca` and `case_lifecycle`), `update_case_metadata`, `handle_failure`, `case_cleanup`, `run_completion`, `mcp_server` |
| `src/lambda/common/` | Shared code in the common layer: Bedrock client, templates, taxonomy, validation, checkpoints, S3 and SQS helpers |
| `src/lambda/mcp_tools/` | MCP tool definitions, schemas, SQL guard and Athena helpers |
| `src/templates/` | Prompt templates (see [prompts.md](prompts.md)) |
| `config/` | `schema.json`, `example.json` (every field), `dev.json`, `prod.json` |
| `scripts/` | Operator tools (`generate_csv_insights.py`, `mcp_aws_client.py`, `redrive-dlq.sh`, `reprocess-cases.py`), `render_taxonomy_docs.py`, `build/bundle_python_layer.py`, `security/` |
| `test/` | Jest tests (`*.test.ts`) and pytest tests (`test_*.py`): CDK assertions, cdk-nag and per-stack snapshots in `test/cdk/`, configuration tests in `test/config/`, Lambda handler tests in `test/lambda/`; fixtures in `test/fixtures/`, shared TypeScript helpers in `test/helpers/` and Python test helpers in `test/support/` |
| `.github/workflows/ci.yml` | Pull request CI (see [Continuous integration](#continuous-integration)) |

## Commands

| Command | Runs |
| --- | --- |
| `npm run lint` | `lint:ts` (`eslint . --max-warnings 0`) and `lint:py` (`ruff check .` and `ruff format --check .`) |
| `npm run typecheck` | `typecheck:ts` (`tsc --noEmit`) and `typecheck:py` (`mypy`, strict, over `src`, `test` and `scripts`) |
| `npm run test` | `test:ts` (Jest) and `test:py` (`pytest --cov`) |
| `npm run synth` | Synthesises dev and prod into `cdk.out/dev` and `cdk.out/prod` with `--no-lookups --quiet` |
| `npm run verify` | `lint`, `typecheck`, `test` and `synth` in that order |
| `npm run security` | `security:npm` (`node scripts/security/npm-advisory-scan.cjs`) and `security:py` (`pip-audit --strict` over both lock files, then `bandit` over `src` and `scripts`) |

Every pull request must pass `npm run verify`, which [continuous integration](#continuous-integration) runs. Run `npm run security` when dependencies change. To fix formatting, run `.venv/bin/ruff format .`; line length is 100.

## Tests

**TypeScript.** Jest runs `test/**/*.test.ts` with `ts-jest`, two workers and a 120-second timeout per test. Template snapshots are in `test/cdk/__snapshots__/app.test.ts.snap` and `test/cdk/stacks/__snapshots__/`. When an infrastructure change is intended, update them with `npx jest -u <test file>` and review the snapshot diff as carefully as the code: it is the record of what CloudFormation will change.

```bash
npx jest test/cdk/constructs/observability.test.ts
```

**Python.** `pytest` uses `src/lambda` and `test/support` as import paths, importlib import mode, strict markers and strict `xfail`. `npm run test:py` measures branch coverage of `src/lambda` and fails below 80 per cent. For a subset, skip coverage:

```bash
.venv/bin/pytest test/lambda/mcp --no-cov -q
```

Tests never call AWS. Handlers are exercised with `moto` and botocore stubbers; `test/support/` provides kits for the pipeline, workflow, analysis and MCP tests, an SQLite-backed Athena stand-in and a loopback SigV4 server for the bridge. The only test that calls AWS is the opt-in live Bedrock regression in `test/prompts/test_live_bedrock_prompts.py`, skipped unless `CASE_INSIGHTS_LIVE_BEDROCK=1` (see [Testing prompts](prompts.md#testing-prompts)). Local tests are not proof of live behaviour; the live checks for a deployment are listed in the documents for each feature and remain deferred.

## Continuous integration

`.github/workflows/ci.yml` runs on every pull request (and on demand through `workflow_dispatch`) on `ubuntu-24.04`:

1. Node.js 24.22.3 from `.nvmrc` and Python 3.12.3 (`PYTHON_VERSION` in the workflow), with every action pinned to a commit SHA.
2. `npm ci`, then `python -m venv .venv` and `.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt`.
3. `npm run verify`: lint, type checks, Jest (CDK assertions, cdk-nag and per-stack snapshots), pytest with the 80% aggregate coverage threshold for `src/lambda` (including the prompt fixtures), and dev and prod synthesis with cdk-nag.
4. A check that verification left the working tree unchanged, so a missing snapshot or a generated file that is not ignored fails the build.

The job has read-only repository permissions and uses no AWS credentials or secrets. `.cache/pip` is cached with a key derived from `requirements.txt` and `requirements-dev.txt`; the cache is only an optimisation, because every wheel is still installed with `--require-hashes` and a miss downloads from the package index. `test/build/test_ci_workflow.py` checks these properties of the workflow. When you change the Node.js or Python version, update `.nvmrc` or `PYTHON_VERSION` together with this guide, [CONTRIBUTING](../CONTRIBUTING.md) and the [README](../README.md#continuous-integration).
## Python asset packaging

`lib/packaging/python-assets.ts` builds each handler asset (`lambdaHandlerCode`) and the common layer (`lambdaPackagesCode`, mounted at `/opt/python`) during synthesis; prompt templates go into a separate templates layer at `/opt/templates`. Layer dependencies are installed by `scripts/build/bundle_python_layer.py` with `pip install --only-binary=:all: --require-hashes` for the `manylinux_2_28_x86_64` platform and CPython 3.12, so the result matches the Lambda runtime on any build machine without Docker.

- The first synthesis needs access to the package index; downloaded wheels are cached in `.cache/pip`.
- `CASE_INSIGHTS_PYTHON` selects a Python 3.12 interpreter other than `.venv/bin/python`.
- Assets are content-addressed: a template-only change rebuilds only the templates layer, and a change in `src/lambda/common/` rebuilds the common layer (`test/cdk/packaging/python-assets.test.ts`).

## Dependencies

All versions are pinned exactly. npm overrides pin `glob` 14.0.6, `minimatch` 11.0.8 and `test-exclude` 8.0.0 for transitive dependencies.

- **npm.** Change the version in `package.json`, run `npm install` to update `package-lock.json`, and commit both.
- **Python runtime** (packaged into the Lambda layer). Edit `requirements.in` (currently `aws-lambda-powertools[tracer]==3.40.0` and `boto3==1.44.2`), then compile the hashed lock with the command at the top of the file:

  ```bash
  .venv/bin/uv pip compile requirements.in --generate-hashes --python-version 3.12 \
    --python-platform x86_64-manylinux_2_28 --no-header --output-file requirements.txt
  ```

- **Python development tools.** Edit `requirements-dev.in`, then compile it constrained to the runtime lock so tests use the shipped versions:

  ```bash
  .venv/bin/uv pip compile requirements-dev.in --constraint requirements.txt --generate-hashes \
    --python-version 3.12 --python-platform x86_64-manylinux_2_28 --no-header --output-file requirements-dev.txt
  ```

  Reinstall with `.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt`.

`scripts/security/npm-advisory-exceptions.json` lists the npm advisories accepted because no fixed release is reachable (currently `minimatch` bundled inside `aws-cdk-lib`, and the development-only `deepmerge`). Each entry names the advisory, package and lockfile locations, a specific reason and a `reviewBy` date; expired or unused entries make `npm run security:npm` fail.

## cdk-nag

`enableNagChecks` in `lib/nag.ts` adds the `AwsSolutionsChecks` rule pack to every synthesis, so a new finding fails `npm run synth` and the template tests. Fix the finding where possible. When a finding is intended, acknowledge it on the narrowest construct with one of the helpers:

| Helper | Use |
| --- | --- |
| `acknowledgeNagFinding(scope, id, reason)` | One rule ID on one construct |
| `acknowledgeWildcardResourceFinding(scope, resource, reason)` | One specific IAM wildcard resource |
| `acknowledgeGeneratedPolicyWildcards(policy, rules)` | Wildcards in a policy generated by CDK grants |
| `acknowledgeCdkWarning(scope, id, reason)` | A CDK annotation warning |

Reasons must be at least 40 characters and explain why the finding is acceptable, not restate it. The tests keep an inventory of every acknowledgement (`test/cdk/app.test.ts` for Core, `test/cdk/stacks/*.test.ts` for the Analytics, MCP and logging stacks), so a new acknowledgement also needs a test update and a review. See [security.md](security.md) for the accepted exceptions.

## Lambda handler conventions

Every handler follows the same pattern; copy an existing handler rather than starting from scratch.

- **Powertools.** Module-level `Logger`, `Metrics` and `Tracer`. Metrics go to the `SupportCaseInsights` namespace with the function's service name as the `service` dimension.
- **No sensitive data in logs or traces.** Call `withhold_exception_messages()` (`common.tracing`) at import so X-Ray traces drop exception messages, and log errors with `error_fields(exc)` (`common.safe_logging`), which records the exception class, AWS error code and HTTP status but never the message. Never log case content, prompts, model replies, SQL or tool arguments. `PipelineFunction` sets `POWERTOOLS_TRACER_CAPTURE_RESPONSE` and `POWERTOOLS_TRACER_CAPTURE_ERROR` to `false`.
- **Clients.** Create boto3 clients once per execution environment in a `@cache` function `clients()`.
- **Idempotency and retries.** Pipeline handlers are invoked at least once. Make every write conditional or idempotent, and check for a committed processed record before doing work.

## Adding a Lambda function

1. Write the handler in `src/lambda/<name>/app.py` following the conventions above, with tests under `test/lambda/`.
2. Add a `PipelineFunction` construct (`lib/constructs/pipeline-function.ts`). It creates a Python 3.12 x86_64 function with its own role (unless one is passed), its own log group with `logRetentionDays`, JSON logging, active tracing and the safe Powertools environment, which callers cannot override. The function name must include `uniqueId` and be at most 64 characters.
3. Grant only the permissions the handler needs, scoped to this deployment's resources; see [permissions.md](permissions.md).
4. Add the function to the `functions` list passed to observability in `lib/stacks/core-stack.ts`. This creates the alarm `LambdaErrors-<constructId>`. Add it to `PIPELINE_FUNCTIONS` in `test/cdk/constructs/observability.test.ts`.
5. Add a `### LambdaErrors-<constructId>` section to [runbook.md](runbook.md); the test `every alarm has a runbook entry` fails without it.
6. Add the handler to `HANDLERS` in `test/support/handler_trace_probe.py`; `test/lambda/test_handler_tracing.py` checks that every module that creates a `Tracer` is covered.
7. Update the snapshots, then [resources.md](resources.md), [monitoring.md](monitoring.md) and [architecture.md](architecture.md).

## Adding an MCP tool

The tool set is a contract: clients, tests and documentation all expect exactly eight read-only tools in a fixed order. Add a tool only when the existing ones cannot answer the question safely.

1. Define the tool in a `src/lambda/mcp_tools/tool_*.py` module as a `ToolDefinition`, with an input schema built by `object_schema` (closed, `additionalProperties: false`) and helpers such as `integer_property`. `schema.py` supports only a fixed set of JSON Schema keywords, and `check_schema` rejects any other.
2. Reuse the shared filters in `filters.py` for account, dates, service, severity and categories.
3. Run SQL only through `run_fixed_query` with `?` parameters (passed as Athena `ExecutionParameters`); never build SQL from argument text. Fixed queries also pass the SQL guard.
4. Set the annotations: `readOnlyHint` true, `destructiveHint` false, `openWorldHint` false, and `idempotentHint` false only when the tool calls Bedrock.
5. Register it in `registry.build_registry` and add its name to `TOOL_NAMES` in the same order; the registry refuses to build if they differ.
6. Update the tests that pin the tool set: `test/lambda/mcp/test_registry.py`, `test/lambda/mcp/test_mcp_server.py`, `EXPECTED_TOOLS` in `test/scripts/test_mcp_aws_client.py`, the cross-surface integration tests in `test/integration/` and the stack snapshot whose description counts the tools.
7. Document it in [Tools](mcp-server-deployment.md#tools) and update every mention of the tool count.

## Adding a configuration field

1. Add the property, with description, bounds and default, to `config/schema.json`.
2. Add it to the types in `lib/config/types.ts` and to the inventory in `test/config/schema-parity.test.ts`, which checks that the schema and the types agree.
3. Add it to `config/example.json`, which sets every field explicitly; add it to `config/dev.json` and `config/prod.json` only when they need a non-default value.
4. Put cross-field rules in `semanticIssues` in `lib/config/load.ts` and describe them in the schema's `$comment`.
5. Document it in [configuration.md](configuration.md).

## Changing prompts or the taxonomy

Follow [Changing prompts, taxonomies or the model](prompts.md#changing-prompts-taxonomies-or-the-model). In short: keep template placeholders exactly as they are, edit categories only in `src/lambda/common/taxonomy.py`, regenerate the documentation blocks with `.venv/bin/python scripts/render_taxonomy_docs.py --write <file>` for `docs/prompts.md` and `docs/athena.md`, and run `.venv/bin/pytest test/prompts test/build -q`. Any such change produces a new `Prompt_Version`.

## Documentation

Documentation lives in `docs/` and in `README.md`. Some documents are checked by tests:

- `test/docs/test_analytics_docs.py` checks that `docs/athena.md` and `docs/csv_export.md` match the synthesised Analytics resources, the processed-record schema and the exporter's options, that their relative links and those in `README.md` resolve, and that they make no customer managed KMS key claims;
- `test/build/test_render_taxonomy_docs.py` checks the taxonomy renderer, and `--check` verifies that a document's generated block is current;
- `test/cdk/constructs/observability.test.ts` requires a runbook section for every alarm.

```bash
.venv/bin/pytest test/docs --no-cov -q
.venv/bin/python scripts/render_taxonomy_docs.py --check docs/prompts.md
.venv/bin/python scripts/render_taxonomy_docs.py --check docs/athena.md
```

Describe implemented behaviour only, and mark anything that depends on a live deployment as deferred until it has been checked.

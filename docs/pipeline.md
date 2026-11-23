# Optional CDK pipeline

The application deploys without a pipeline: `npx cdk deploy --all -c env=dev` (or `-c env=prod`) is the normal path and is not affected by anything on this page. The optional pipeline is a CDK Pipelines app in the management account that deploys the same stacks for both environments from the `main` branch:

1. **Source**: `main` of the configured GitHub repository through an existing AWS CodeConnections connection. No personal access token, OAuth token, Secrets Manager secret or webhook is used.
2. **Build** (`Synth`): installs `requirements-dev.txt` with `pip --require-hashes` and runs `npm ci`, runs `npm run verify` (lint, type checks, every test and the dev/prod synth with cdk-nag), then synthesises the pipeline app with `npx cdk synth -c pipeline=true` into `cdk.out/pipeline`. Docker is never used.
3. **UpdatePipeline** (self-mutation) and **Assets** (file asset publishing), with the CDK CLI version pinned in `package.json` and a pinned `cdk-assets` release.
4. **SharedApiLogging**: `CaseInsightsApiLoggingStack-<sharedId>`, only when an environment enables MCP in managed `apiGatewayLogging` mode (see below).
5. **Dev**: the dev stacks (`CaseInsightsCoreStack-<dev uniqueId>`, member role, Analytics and MCP as configured).
6. **Prod**: the manual approval action `PromoteToProd` runs first; the prod stacks deploy only after someone approves it. There is no setting that removes or bypasses the approval.

The pipeline is a V2 pipeline in `QUEUED` execution mode, so releases run one at a time in commit order. Its artifact bucket and the bucket that receives the artifact bucket's access logs are private, versioned, SSE-S3 encrypted, deny requests without TLS and are retained when the stack is deleted. No KMS key is created.

## Configuration

`config/pipeline.json` holds the pipeline settings. It is validated against `config/pipeline.schema.json`, separately from `config/<env>.json`. Start from the example:

```bash
cp config/pipeline.example.json config/pipeline.json
```

| Field | Meaning |
| --- | --- |
| `pipelineName` | CodePipeline name (default `CaseInsights-Pipeline`). |
| `account`, `region` | Management account and Region that host the pipeline and both environments. |
| `connectionArn` | ARN of an existing CodeConnections (or CodeStar Connections) connection in `account` and `region`. |
| `repository` | Must be `bsadkins/factorytestsample` (the default). |
| `branch` | Must be `main`. |

Unknown fields are rejected, including token fields such as `oauthToken` and approval switches such as `manualApproval`. With `-c pipeline=true` the app also loads `config/dev.json` and `config/prod.json` and checks, before creating any stack, that:

- `config/dev.json` declares `environment` `dev` and `config/prod.json` declares `prod`;
- the two files use different `uniqueId` values, so every stack, role (including `Support-Case-Analysis-Role-<uniqueId>` in member accounts), bucket, queue and other physical name differs;
- both use the same `orgId`, and `managementAccountId` and `region` equal the pipeline `account` and `region` (cross-account and cross-Region stages are not supported);
- both use the same `apiGatewayLogging` settings;
- the connection ARN is in the pipeline account and Region, and its partition (for example `aws`, or `aws-cn` for China Regions) is the partition of that Region.

The role CodePipeline assumes for the Source action may call `codestar-connections:UseConnection` and `codeconnections:UseConnection` on exactly this connection ARN; AWS requires both, whichever service prefix the ARN uses.

Any problem stops synthesis with a list of the errors. Because the Build step synthesises from the repository, `config/pipeline.json`, `config/dev.json` and `config/prod.json` must be committed on `main`. They contain account IDs and a connection ARN, not secrets.

Preview the pipeline locally (no AWS access needed):

```bash
npx cdk synth -c pipeline=true --no-lookups --output cdk.out/pipeline
```

`-c pipeline=true` cannot be combined with `-c env=<env>`. Without `-c pipeline=true` the app ignores `config/pipeline.json` and synthesises exactly the standalone stacks.

## Stacks, stages and names

Each stage contains only environment stacks, never another pipeline, so the pipeline cannot recurse. The stage stacks use the same stack names and templates as standalone deploy-all for that environment; `test/cdk/stacks/pipeline-stack.test.ts` compares them template by template. Some CDK constructs normally derive a logical ID from the full construct path (SQS event source mappings and the S3 notification permission), which would differ inside a stage. The stages rewrite those IDs relative to the stage, so a deployment made with `cdk deploy --all -c env=<env>` can later be taken over by the pipeline, or the other way round, without replacing resources.

**Shared API Gateway logging.** The account/Region API Gateway CloudWatch role setting has one owner, `CaseInsightsApiLoggingStack-<sharedId>`. The pipeline deploys it once, in its own `SharedApiLogging` stage before Dev, and the environment stages never contain an `AWS::ApiGateway::Account` resource or logging role. It is the same stack (same name and template) that standalone deploy-all creates, so both entry points reuse it. In `existing` mode, or when MCP is disabled in both environments, there is no shared stage and nothing changes the account setting. Read [Shared API Gateway logging prerequisite](mcp-server-deployment.md#shared-api-gateway-logging-prerequisite) before the first deployment.

## Wheel cache

Layer bundling downloads wheels into `.cache/pip` (repository-local and ignored by git). The Build step declares that directory as a CodeBuild local custom cache. The cache is only an optimisation: every wheel is installed with `--require-hashes --only-binary=:all:` against `requirements.txt`, a cache miss downloads from the package index, and neither local synth nor the Build step depends on a cache that already exists. The Build step therefore needs outbound access to PyPI and the npm registry.

## Before the first deployment (not done by this repository)

Nothing here has been deployed. These steps are deliberately left to an administrator of the management account:

1. **Create and authorise the connection.** Create a CodeConnections connection to GitHub in the pipeline account and Region, then complete the authorisation in the console (install or select the AWS Connector for GitHub app with access limited to this repository). A new connection is `PENDING` until a person completes this; the pipeline cannot activate it. Put its ARN in `config/pipeline.json`.
2. **Bootstrap.** Run `npx cdk bootstrap aws://<account>/<region>` with credentials for the management account. Dev and prod deploy into the same account as the pipeline, so no cross-account trust is needed. The pipeline deploys through the bootstrap roles; review the CloudFormation execution policy the bootstrap stack uses (by default `AdministratorAccess`) and narrow it with `--cloudformation-execution-policies` if required.
3. **Other prerequisites.** StackSets trusted access, Bedrock model access, API Gateway logging ownership and the other prerequisites of a standalone deployment apply unchanged.
4. **Deploy the pipeline once** from a workstation: `npx cdk deploy -c pipeline=true CaseInsightsPipelineStack`. After that the pipeline updates itself from `main`.

## Trust boundary

The approval action is a release gate, not a security boundary against people who can write to `main`. Self-mutation means each commit on `main` redefines the pipeline itself before any environment is deployed: a commit can change the Build commands, the stages or the approval step, and the Build step runs repository code with the permissions of its CodeBuild role. Anyone who can push to `main`, or who controls the GitHub connection or the repository's GitHub App installation, can therefore change what is deployed to dev and, through self-mutation, the pipeline itself. Protect `main` with branch protection and required reviews, limit who can approve `PromoteToProd` (`codepipeline:PutApprovalResult`) and who can change the connection, and treat write access to the repository as deployment access to the management account.

## Deferred live verification

None of the following has been run; local tests, snapshots and synth are not evidence for them:

- [ ] The connection is `AVAILABLE` and a push to `main` starts the pipeline.
- [ ] The Build step passes `npm run verify` in CodeBuild and produces the pipeline assembly.
- [ ] Self-mutation and asset publishing succeed with the bootstrap roles.
- [ ] The shared logging stage, Dev and, after approval, Prod deploy; rejecting the approval stops the release before any prod stack changes.

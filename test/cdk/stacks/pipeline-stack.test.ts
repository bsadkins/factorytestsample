import * as fs from 'node:fs';
import * as path from 'node:path';
import { type App, Stack, type Stage } from 'aws-cdk-lib';
import { Annotations, Match, Template } from 'aws-cdk-lib/assertions';
import {
  addCaseInsightsApp,
  addCaseInsightsPipeline,
  addCaseInsightsStacks,
  CaseInsightsApiLoggingStack,
  CaseInsightsEnvironmentStage,
  CaseInsightsPipelineStack,
  CaseInsightsSharedApiLoggingStage,
  CDK_ASSETS_CLI_VERSION,
  ConfigValidationError,
  PIPELINE_STACK_NAME,
  PIPELINE_SYNTH_OUTPUT,
  pinnedCdkCliVersion,
  REPO_ROOT,
  resolveAppMode,
  resourceNames,
} from '../../../lib';
import { type JsonObject, readConfigJson } from '../../config/helpers';
import { normaliseAssetHashes } from '../../helpers/assets';
import { acknowledgedRules, synthError, tempOutdir, testApp } from '../../helpers/cdk';

type ConfigFile = 'pipeline' | 'dev' | 'prod';
const SHIPPED: Record<ConfigFile, string> = { pipeline: 'pipeline.example.json', dev: 'dev.json', prod: 'prod.json' };
const CONNECTION_ARN = 'arn:aws:codeconnections:us-east-1:111111111111:connection/00000000-0000-4000-8000-000000000000';
const EXISTING_ROLE = 'arn:aws:iam::111111111111:role/platform/ApiGatewayCloudWatchLogs';
const EXISTING = { apiGatewayLogging: { mode: 'existing', sharedId: 'scishared', existingRoleArn: EXISTING_ROLE } } as const;

/** A config directory with the shipped files (pipeline.json from the example) plus overrides; `null` omits a file. */
function configDir(overrides: Partial<Record<ConfigFile, JsonObject | null>> = {}): string {
  const dir = tempOutdir();
  for (const file of ['pipeline', 'dev', 'prod'] as const) {
    const override = overrides[file];
    if (override !== null) {
      fs.writeFileSync(path.join(dir, `${file}.json`), JSON.stringify({ ...readConfigJson(SHIPPED[file]), ...override }));
    }
  }
  return dir;
}

interface PipelineJson {
  Properties: {
    Name: string;
    PipelineType: string;
    ExecutionMode: string;
    RestartExecutionOnUpdate: boolean;
    ArtifactStore: { Location: unknown; Type: string; EncryptionKey?: unknown };
    Stages: { Name: string; Actions: { Name: string; RunOrder: number; ActionTypeId: { Category: string; Provider: string }; Configuration?: Record<string, unknown> }[] }[];
  };
}

function pipelineResource(stack: Stack): PipelineJson['Properties'] {
  const pipelines = Object.values(Template.fromStack(stack).findResources('AWS::CodePipeline::Pipeline')) as PipelineJson[];
  expect(pipelines).toHaveLength(1);
  return required(pipelines[0], 'pipeline resource').Properties;
}

function required<T>(value: T | undefined, what: string): T {
  if (value === undefined) {
    throw new Error(`missing ${what}`);
  }
  return value;
}

function stagesOf(scope: Stage): Stack[] {
  return scope.node.findAll().filter((construct): construct is Stack => Stack.isStack(construct));
}

function buildSpecOf(stack: Stack, projectId: string): Record<string, unknown> {
  const project = Template.fromStack(stack).findResources('AWS::CodeBuild::Project')[projectId] as { Properties: { Source: { BuildSpec: string } } };
  return JSON.parse(project.Properties.Source.BuildSpec) as Record<string, unknown>;
}

function projectId(stack: Stack, construct: { node: { defaultChild?: unknown } }): string {
  return stack.getLogicalId(construct.node.defaultChild as never);
}

const PHYSICAL_NAME_KEYS = [
  'RoleName', 'QueueName', 'FunctionName', 'StateMachineName', 'TopicName', 'DashboardName', 'AlarmName', 'TableName',
  'StackSetName', 'LogGroupName', 'Name', 'LayerName', 'BucketName', 'ScheduleName', 'WorkGroup',
];

/** Literal physical names (resource type plus name) declared by the stacks of a stage. */
function physicalNames(stacks: readonly Stack[]): string[] {
  const names: string[] = [];
  for (const stack of stacks) {
    for (const [, resource] of Object.entries(Template.fromStack(stack).toJSON().Resources as Record<string, { Type: string; Properties?: Record<string, unknown> }>)) {
      for (const key of PHYSICAL_NAME_KEYS) {
        const value = resource.Properties?.[key];
        if (typeof value === 'string') {
          names.push(`${resource.Type}:${key}:${value}`);
        }
      }
    }
  }
  return names;
}

describe('opt-in app selection', () => {
  test('standalone is the default and the pipeline needs -c pipeline=true without env', () => {
    expect(resolveAppMode(undefined, 'prod')).toEqual({ kind: 'standalone', env: 'prod' });
    expect(resolveAppMode('true', undefined)).toEqual({ kind: 'pipeline' });
    expect(resolveAppMode(true, undefined)).toEqual({ kind: 'pipeline' });
    expect(() => resolveAppMode(undefined, undefined)).toThrow(/pass -c env=<name>/);
    expect(() => resolveAppMode('true', 'prod')).toThrow(/do not also pass -c env=prod/);
    for (const value of ['false', 'yes', 1, '']) {
      expect(() => resolveAppMode(value, 'dev')).toThrow(ConfigValidationError);
    }
  });

  test.each(['dev', 'prod'] as const)('%s standalone deploy-all is unchanged when config/pipeline.json exists', (env) => {
    const withPipeline = testApp({ env });
    const app = addCaseInsightsApp(withPipeline, { configDir: configDir() });
    const plain = testApp();
    const reference = addCaseInsightsStacks(plain, { env });
    expect(app.kind).toBe('standalone');
    const names = (scope: App): string[] =>
      scope.node.children.filter((child): child is Stack => Stack.isStack(child)).map((stack) => stack.stackName).sort();
    expect(names(withPipeline)).toEqual(names(plain));
    expect(names(withPipeline)).not.toContain(PIPELINE_STACK_NAME);
    if (app.kind === 'standalone') {
      expect(Template.fromStack(app.stacks.core).toJSON()).toEqual(Template.fromStack(reference.core).toJSON());
    }
    for (const stack of withPipeline.node.children.filter((child): child is Stack => Stack.isStack(child))) {
      Template.fromStack(stack).resourceCountIs('AWS::CodePipeline::Pipeline', 0);
    }
  });

  test('pipeline mode adds only the pipeline stack at the app root', () => {
    const app = testApp({ pipeline: 'true' });
    const result = addCaseInsightsApp(app, { configDir: configDir() });
    expect(result.kind).toBe('pipeline');
    const rootStacks = app.node.children.filter((child): child is Stack => Stack.isStack(child));
    expect(rootStacks.map((stack) => stack.stackName)).toEqual([PIPELINE_STACK_NAME]);
  });
});

describe('invalid pipeline configuration stops before any stack is created', () => {
  test.each<[string, Partial<Record<ConfigFile, JsonObject | null>>, RegExp]>([
    ['missing config/pipeline.json', { pipeline: null }, /pipeline\.json[\s\S]*cannot read file/],
    ['missing config/prod.json', { prod: null }, /prod\.json[\s\S]*cannot read file/],
    ['missing connection ARN', { pipeline: { connectionArn: undefined } }, /required property 'connectionArn'/],
    ['token in place of a connection', { pipeline: { connectionArn: 'ghp_notaconnection' } }, /\/connectionArn must match pattern/],
    ['OAuth token property', { pipeline: { oauthToken: 'x' } }, /unsupported property "oauthToken"/],
    ['non-main branch', { pipeline: { branch: 'release' } }, /\/branch must be "main"/],
    ['approval toggle', { pipeline: { manualApproval: false } }, /unsupported property "manualApproval"/],
    ['duplicate namespaces', { prod: { uniqueId: 'scidev' } }, /different uniqueId values/],
    ['prod in another account', { prod: { managementAccountId: '222222222222' } }, /prod managementAccountId 222222222222 differs from pipeline account/],
    ['dev in another Region', { dev: { region: 'eu-west-1' } }, /dev region eu-west-1 differs from pipeline region us-east-1/],
    ['different organizations', { prod: { orgId: 'o-otherorg123' } }, /orgId/],
    ['inconsistent shared logging', { prod: EXISTING }, /same apiGatewayLogging settings/],
    ['invalid stage configuration', { dev: { bedrockModelId: 'anthropic.claude-v2' } }, /\/bedrockModelId must be one of/],
    ['swapped stage files', { dev: { environment: 'prod', alarmEmail: 'ops@example.com' } }, /\/environment is "prod" but the file was loaded for -c env=dev/],
  ])('%s', (_name, overrides, expected) => {
    const app = testApp({ pipeline: 'true' });
    let error: unknown;
    try {
      addCaseInsightsApp(app, { configDir: configDir(overrides) });
    } catch (caught) {
      error = caught;
    }
    expect(error).toBeInstanceOf(ConfigValidationError);
    expect((error as Error).message).toMatch(expected);
    expect(app.node.children).toHaveLength(0);
  });
});

describe('approval-gated pipeline (shipped dev and prod configurations)', () => {
  let app: App;
  let stack: CaseInsightsPipelineStack;
  let pipeline: PipelineJson['Properties'];

  beforeAll(() => {
    app = testApp();
    stack = addCaseInsightsPipeline(app, { configDir: configDir() });
    pipeline = pipelineResource(stack);
  });

  test('synthesises with cdk-nag enforcing across the pipeline and every stage', () => {
    expect(synthError(app)).toBeUndefined();
    for (const scope of [stack, ...stagesOf(stack.devStage), ...stagesOf(stack.prodStage)]) {
      Annotations.fromStack(scope).hasNoError('*', Match.anyValue());
    }
  });

  test('V2 pipeline in the management account with queued releases and a private SSE-S3 artifact bucket', () => {
    expect(stack.account).toBe('111111111111');
    expect(stack.region).toBe('us-east-1');
    expect(pipeline).toMatchObject({ Name: 'CaseInsights-Pipeline', PipelineType: 'V2', ExecutionMode: 'QUEUED', RestartExecutionOnUpdate: true });
    expect(pipeline.ArtifactStore.Type).toBe('S3');
    expect(pipeline.ArtifactStore.EncryptionKey).toBeUndefined();
    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::KMS::Key', 0);
    const artifactId = stack.getLogicalId(stack.artifactBucket.node.defaultChild as never);
    const logsId = stack.getLogicalId(stack.accessLogsBucket.node.defaultChild as never);
    expect(pipeline.ArtifactStore.Location).toEqual({ Ref: artifactId });
    const buckets = template.findResources('AWS::S3::Bucket') as Record<string, { Properties: Record<string, unknown>; DeletionPolicy: string }>;
    expect(Object.keys(buckets).sort()).toEqual([artifactId, logsId].sort());
    for (const bucket of Object.values(buckets)) {
      expect(bucket.DeletionPolicy).toBe('Retain');
      expect(bucket.Properties).toMatchObject({
        BucketEncryption: { ServerSideEncryptionConfiguration: [{ ServerSideEncryptionByDefault: { SSEAlgorithm: 'AES256' } }] },
        PublicAccessBlockConfiguration: { BlockPublicAcls: true, BlockPublicPolicy: true, IgnorePublicAcls: true, RestrictPublicBuckets: true },
        VersioningConfiguration: { Status: 'Enabled' },
      });
    }
    expect(buckets[artifactId]?.Properties.LoggingConfiguration).toEqual({ DestinationBucketName: { Ref: logsId }, LogFilePrefix: 'pipeline-artifacts/' });
    expect(buckets[logsId]?.Properties.LoggingConfiguration).toBeUndefined();
    for (const id of [artifactId, logsId]) {
      template.hasResourceProperties('AWS::S3::BucketPolicy', {
        Bucket: { Ref: id },
        PolicyDocument: { Statement: Match.arrayWith([Match.objectLike({ Effect: 'Deny', Condition: { Bool: { 'aws:SecureTransport': 'false' } } })]) },
      });
    }
  });

  test('source is main of bsadkins/factorytestsample through the supplied CodeConnections ARN, never a token', () => {
    const [source] = pipeline.Stages;
    expect(source?.Name).toBe('Source');
    expect(source?.Actions).toHaveLength(1);
    const action = source?.Actions[0];
    expect(action?.ActionTypeId).toMatchObject({ Category: 'Source', Provider: 'CodeStarSourceConnection' });
    expect(action?.Configuration).toEqual({
      ConnectionArn: CONNECTION_ARN,
      FullRepositoryId: 'bsadkins/factorytestsample',
      BranchName: 'main',
      DetectChanges: true,
    });
    const json = JSON.stringify(Template.fromStack(stack).toJSON());
    expect(json).not.toMatch(/OAuthToken|secretsmanager|GitHubSourceAction|"Provider":"GitHub"|ghp_|webhook/i);
    Template.fromStack(stack).resourceCountIs('AWS::CodePipeline::Webhook', 0);
    Template.fromStack(stack).resourceCountIs('AWS::CodeStarConnections::Connection', 0);
    Template.fromStack(stack).resourceCountIs('AWS::CodeConnections::Connection', 0);
  });

  test('stage order is source, verify/synth, self-mutation, assets, shared logging, dev, then approval before prod', () => {
    expect(pipeline.Stages.map((stage) => stage.Name)).toEqual(['Source', 'Build', 'UpdatePipeline', 'Assets', 'SharedApiLogging', 'Dev', 'Prod']);
    const byName = Object.fromEntries(pipeline.Stages.map((stage) => [stage.Name, stage.Actions]));
    expect(byName.Build?.map((action) => action.Name)).toEqual(['Synth']);
    const prodActions = byName.Prod ?? [];
    const approvals = prodActions.filter((action) => action.ActionTypeId.Category === 'Approval');
    expect(approvals).toEqual([expect.objectContaining({ Name: 'PromoteToProd', RunOrder: 1, ActionTypeId: { Category: 'Approval', Owner: 'AWS', Provider: 'Manual', Version: '1' } })]);
    const prodDeploys = prodActions.filter((action) => action.ActionTypeId.Provider === 'CloudFormation');
    expect(prodDeploys.length).toBeGreaterThan(0);
    for (const action of prodDeploys) {
      expect(action.RunOrder).toBeGreaterThan(1);
    }
    // Exactly one approval in the whole pipeline, and nothing deploys a prod stack before it.
    const all = pipeline.Stages.flatMap((stage) => stage.Actions.map((action) => ({ stage: stage.Name, action })));
    expect(all.filter(({ action }) => action.ActionTypeId.Category === 'Approval')).toHaveLength(1);
    const stackOf = (action: { Configuration?: Record<string, unknown> }): string => {
      const name = action.Configuration?.StackName;
      return typeof name === 'string' ? name : '';
    };
    for (const { stage, action } of all.filter(({ action: candidate }) => candidate.ActionTypeId.Provider === 'CloudFormation')) {
      const name = stackOf(action);
      if (name.endsWith('-sciprod')) expect(stage).toBe('Prod');
      if (name.endsWith('-scidev')) expect(stage).toBe('Dev');
      if (name.startsWith('CaseInsightsApiLoggingStack-')) expect(stage).toBe('SharedApiLogging');
    }
    const deployed = (stageName: string): string[] =>
      [...new Set((byName[stageName] ?? []).filter((action) => action.ActionTypeId.Provider === 'CloudFormation').map(stackOf))].sort();
    expect(deployed('SharedApiLogging')).toEqual(['CaseInsightsApiLoggingStack-scishared']);
    const devNames = resourceNames(stack.devStage.stacks.config);
    expect(deployed('Dev')).toEqual([devNames.coreStack, devNames.memberRoleStack, devNames.analyticsStack, devNames.mcpStack].sort());
    const prodNames = resourceNames(stack.prodStage.stacks.config);
    expect(deployed('Prod')).toEqual([prodNames.coreStack, prodNames.memberRoleStack, prodNames.analyticsStack].sort());
  });

  test('verify/synth installs only locked dependencies, runs npm run verify, then synthesises the pipeline app without Docker', () => {
    const synthId = projectId(stack, stack.pipeline.synthProject);
    const spec = buildSpecOf(stack, synthId) as {
      phases: { install: { 'runtime-versions': Record<string, string>; commands: string[] }; build: { commands: string[] } };
      cache: { paths: string[] };
      artifacts: { 'base-directory': string };
    };
    expect(spec.phases.install['runtime-versions']).toEqual({ nodejs: '24', python: '3.12' });
    expect(spec.phases.install.commands).toEqual([
      'python3 -c "import sys; assert sys.version_info[:2] == (3, 12), sys.version"',
      'python3 -m venv .venv',
      '.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt',
      'npm ci',
    ]);
    expect(spec.phases.build.commands).toEqual(['npm run verify', `npx cdk synth -c pipeline=true --no-lookups --quiet --output ${PIPELINE_SYNTH_OUTPUT}`]);
    expect(spec.artifacts['base-directory']).toBe(PIPELINE_SYNTH_OUTPUT);
    // The wheel cache is declared and optional: bundling verifies every wheel against requirements.txt hashes.
    expect(spec.cache.paths).toEqual(['.cache/pip/**/*']);
    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::CodeBuild::Project', {
      Environment: Match.objectLike({
        Image: 'aws/codebuild/standard:8.0',
        ComputeType: 'BUILD_GENERAL1_MEDIUM',
        EnvironmentVariables: [
          { Name: 'PIP_CACHE_DIR', Type: 'PLAINTEXT', Value: '.cache/pip' },
          { Name: 'PIP_DISABLE_PIP_VERSION_CHECK', Type: 'PLAINTEXT', Value: '1' },
        ],
      }),
      Cache: { Type: 'LOCAL', Modes: ['LOCAL_CUSTOM_CACHE'] },
    });
    const projects = Object.values(template.findResources('AWS::CodeBuild::Project')) as { Properties: { Environment: { PrivilegedMode: boolean; Image: string } } }[];
    expect(projects).toHaveLength(3);
    for (const project of projects) {
      expect(project.Properties.Environment.PrivilegedMode).toBe(false);
      expect(project.Properties.Environment.Image).toBe('aws/codebuild/standard:8.0');
    }
    expect(JSON.stringify(template.toJSON())).not.toMatch(/docker (build|login|run)|--no-verify/);
  });

  test('the root package.json exists for the pinned CLI version', () => {
    expect(fs.existsSync(path.join(REPO_ROOT, 'package.json'))).toBe(true);
  });

  test('self-mutation and asset publishing install exactly pinned CDK tooling', () => {
    expect(pinnedCdkCliVersion()).toMatch(/^\d+\.\d+\.\d+$/);
    const selfMutation = buildSpecOf(stack, projectId(stack, stack.pipeline.selfMutationProject)) as { phases: { install: { commands: string[] }; build: { commands: string[] } } };
    expect(selfMutation.phases.install.commands).toEqual([`npm install -g aws-cdk@${pinnedCdkCliVersion()}`]);
    expect(selfMutation.phases.build.commands).toEqual([`cdk -a . deploy ${PIPELINE_STACK_NAME} --require-approval=never --verbose`]);
    // The asset project's buildspec is written to the assembly; its install command pins cdk-assets.
    const assembly = app.synth();
    const assetSpecFile = fs.readdirSync(assembly.directory).find((name) => name.endsWith('-FileAsset.yaml'));
    expect(assetSpecFile).toBeDefined();
    const assetSpec = JSON.parse(fs.readFileSync(path.join(assembly.directory, assetSpecFile ?? ''), 'utf8')) as { phases: { install: { commands: string[] } } };
    expect(assetSpec.phases.install.commands).toEqual([`npm install -g cdk-assets@${CDK_ASSETS_CLI_VERSION}`]);
  });

  test('stages hold only environment stacks: no pipeline is instantiated inside any stage', () => {
    expect(app.node.findAll().filter((construct) => construct instanceof CaseInsightsPipelineStack)).toEqual([stack]);
    const stages = [stack.sharedApiLoggingStage, stack.devStage, stack.prodStage];
    for (const stage of stages) {
      expect(stage).toBeDefined();
      for (const nested of stagesOf(stage as Stage)) {
        expect(nested).not.toBeInstanceOf(CaseInsightsPipelineStack);
        Template.fromStack(nested).resourceCountIs('AWS::CodePipeline::Pipeline', 0);
        Template.fromStack(nested).resourceCountIs('AWS::CodeBuild::Project', 0);
      }
    }
    expect(stack.devStage).toBeInstanceOf(CaseInsightsEnvironmentStage);
    expect(stack.sharedApiLoggingStage).toBeInstanceOf(CaseInsightsSharedApiLoggingStage);
  });

  test('each stage deploys exactly the templates of standalone deploy-all for its environment', () => {
    for (const [stage, env] of [[stack.devStage, 'dev'], [stack.prodStage, 'prod']] as const) {
      const standalone = addCaseInsightsStacks(testApp(), { env });
      const expected = [standalone.core, standalone.memberRole, standalone.analytics, standalone.mcp].filter((s) => s !== undefined);
      const actual = stagesOf(stage);
      expect(actual.map((s) => s.stackName).sort()).toEqual(expected.map((s) => s.stackName).sort());
      for (const reference of expected) {
        const inStage = required(actual.find((s) => s.stackName === reference.stackName), reference.stackName);
        expect(Template.fromStack(inStage).toJSON()).toEqual(Template.fromStack(reference).toJSON());
      }
    }
  });

  test('dev and prod namespaces, member roles and physical names never collide', () => {
    const dev = stagesOf(stack.devStage);
    const prod = stagesOf(stack.prodStage);
    for (const s of dev) expect(s.stackName).toMatch(/-scidev$/);
    for (const s of prod) expect(s.stackName).toMatch(/-sciprod$/);
    const devNames = physicalNames(dev);
    const prodNames = physicalNames(prod);
    expect(devNames.length).toBeGreaterThan(20);
    expect(devNames.filter((name) => prodNames.includes(name))).toEqual([]);
    expect(new Set(devNames).size).toBe(devNames.length);
    expect(new Set(prodNames).size).toBe(prodNames.length);
    const memberRoleTemplate = (stage: Stage): string => JSON.stringify(Template.fromStack(required(stagesOf(stage).find((s) => s.stackName.startsWith('CaseInsightsMemberRoleStack-')), 'member role stack')).toJSON());
    expect(memberRoleTemplate(stack.devStage)).toContain('Support-Case-Analysis-Role-scidev');
    expect(memberRoleTemplate(stack.devStage)).not.toContain('Support-Case-Analysis-Role-sciprod');
    expect(memberRoleTemplate(stack.prodStage)).toContain('Support-Case-Analysis-Role-sciprod');
    expect(memberRoleTemplate(stack.prodStage)).not.toContain('Support-Case-Analysis-Role-scidev');
  });

  test('one shared API Gateway logging stack and Account association, outside the environment stages', () => {
    const shared = required(stack.sharedApiLoggingStage, 'shared logging stage');
    const sharedStacks = stagesOf(shared);
    expect(sharedStacks).toHaveLength(1);
    expect(sharedStacks[0]).toBeInstanceOf(CaseInsightsApiLoggingStack);
    expect(sharedStacks[0]?.stackName).toBe('CaseInsightsApiLoggingStack-scishared');
    const accounts = (stacks: readonly Stack[]): number =>
      stacks.reduce((total, s) => total + Object.keys(Template.fromStack(s).findResources('AWS::ApiGateway::Account')).length, 0);
    expect(accounts(sharedStacks)).toBe(1);
    expect(accounts([...stagesOf(stack.devStage), ...stagesOf(stack.prodStage), stack])).toBe(0);
    expect(app.node.findAll().filter((construct) => construct instanceof CaseInsightsApiLoggingStack)).toHaveLength(1);
    // The shared stack is identical to the one standalone deploy-all creates, so both entry points reuse it.
    const standalone = addCaseInsightsStacks(testApp(), { env: 'dev' });
    expect(Template.fromStack(required(sharedStacks[0], 'shared logging stack')).toJSON()).toEqual(Template.fromStack(required(standalone.apiLogging, 'standalone shared logging stack')).toJSON());
    Template.fromStack(required(stack.devStage.stacks.mcp, 'dev MCP stack')).hasOutput('ApiGatewayLoggingStack', { Value: 'CaseInsightsApiLoggingStack-scishared' });
  });

  test('every cdk-nag acknowledgement in the pipeline stack is listed with a specific reason', () => {
    app.synth();
    const acknowledged = acknowledgedRules(stack)
      .filter(({ path: constructPath }) => !/\/(Dev|Prod|SharedApiLogging)\//.test(constructPath))
      .map(({ path: constructPath, id, reason }) => ({ path: constructPath.replace(`${stack.node.path}/`, ''), id: id.replace(/^.*::(?=AwsSolutions-)/, ''), reason }));
    const artifact = `<${stack.getLogicalId(stack.artifactBucket.node.defaultChild as never)}.Arn>/*`;
    const write = ['s3:Abort*', 's3:DeleteObject*', 's3:GetBucket*', 's3:GetObject*', 's3:List*'];
    const read = ['s3:GetBucket*', 's3:GetObject*', 's3:List*'];
    const artifactFindings = (actions: string[]): string[] => [...actions.map((action) => `AwsSolutions-IAM5[Action::${action}]`), `AwsSolutions-IAM5[Resource::${artifact}]`];
    const logs = 'arn:aws:logs:us-east-1:111111111111:log-group:/aws/codebuild/';
    const reports = 'arn:aws:codebuild:us-east-1:111111111111:report-group/';
    const synthId = projectId(stack, stack.pipeline.synthProject);
    const mutationId = projectId(stack, stack.pipeline.selfMutationProject);
    const at = (policyPath: string, ids: string[]): [string, string][] => ids.map((id) => [policyPath, id]);
    expect(acknowledged.map(({ path: constructPath, id }) => [constructPath, id]).sort()).toEqual(
      [
        ...at('Pipeline/Pipeline/Role/DefaultPolicy/Resource', artifactFindings(write)),
        ...at('Pipeline/Pipeline/Source/bsadkins_factorytestsample/CodePipelineActionRole/DefaultPolicy/Resource', artifactFindings(write)),
        ...at('Pipeline/Pipeline/Build/Synth/CdkBuildProject/Role/DefaultPolicy/Resource', [
          ...artifactFindings(write),
          `AwsSolutions-IAM5[Resource::${logs}<${synthId}>:*]`,
          `AwsSolutions-IAM5[Resource::${reports}<${synthId}>-*]`,
        ]),
        ...at('Pipeline/UpdatePipeline/SelfMutation/Role/DefaultPolicy/Resource', [
          ...artifactFindings(read),
          `AwsSolutions-IAM5[Resource::${logs}<${mutationId}>:*]`,
          `AwsSolutions-IAM5[Resource::${reports}<${mutationId}>-*]`,
          'AwsSolutions-IAM5[Resource::arn:*:iam::111111111111:role/*]',
          'AwsSolutions-IAM5[Resource::*]',
        ]),
        ...at('Pipeline/Assets/FileRole/DefaultPolicy/Resource', [
          ...artifactFindings(read),
          `AwsSolutions-IAM5[Resource::${logs}*]`,
          `AwsSolutions-IAM5[Resource::${reports}*]`,
          'AwsSolutions-IAM5[Resource::*]',
        ]),
        ['Pipeline/Pipeline/Build/Synth/CdkBuildProject/Resource', 'AwsSolutions-CB4'],
        ['Pipeline/UpdatePipeline/SelfMutation/Resource', 'AwsSolutions-CB4'],
        ['Pipeline/Assets/FileAsset/Resource', 'AwsSolutions-CB4'],
      ].sort(),
    );
    for (const { reason } of acknowledged) {
      expect(reason.length).toBeGreaterThanOrEqual(40);
    }
  });

  test('pipeline stack template snapshot', () => {
    expect(normaliseAssetHashes(Template.fromStack(stack).toJSON())).toMatchSnapshot();
  });
});

describe('shared API Gateway logging variants', () => {
  test('existing-role mode creates no shared stage, role or Account association and dev deploys first', () => {
    const app = testApp();
    const stack = addCaseInsightsPipeline(app, { configDir: configDir({ dev: EXISTING, prod: EXISTING }) });
    expect(stack.sharedApiLoggingStage).toBeUndefined();
    expect(pipelineResource(stack).Stages.map((stage) => stage.Name)).toEqual(['Source', 'Build', 'UpdatePipeline', 'Assets', 'Dev', 'Prod']);
    expect(app.node.findAll().filter((construct) => construct instanceof CaseInsightsApiLoggingStack)).toEqual([]);
    for (const nested of [...stagesOf(stack.devStage), ...stagesOf(stack.prodStage)]) {
      Template.fromStack(nested).resourceCountIs('AWS::ApiGateway::Account', 0);
    }
    Template.fromStack(required(stack.devStage.stacks.mcp, 'dev MCP stack')).hasOutput('ApiGatewayCloudWatchRoleArn', { Value: EXISTING_ROLE });
    expect(synthError(app)).toBeUndefined();
  });

  test('MCP disabled in both environments needs no shared logging stage', () => {
    const app = testApp();
    const stack = addCaseInsightsPipeline(app, { configDir: configDir({ dev: { enableMcp: false } }) });
    expect(stack.sharedApiLoggingStage).toBeUndefined();
    expect(stack.devStage.stacks.mcp).toBeUndefined();
    expect(pipelineResource(stack).Stages.map((stage) => stage.Name)).toEqual(['Source', 'Build', 'UpdatePipeline', 'Assets', 'Dev', 'Prod']);
  });

  test('MCP enabled in both environments still has exactly one shared stack, deployed before dev', () => {
    const app = testApp();
    const stack = addCaseInsightsPipeline(app, {
      configDir: configDir({ prod: { enableMcp: true, mcpAllowedPrincipals: ['arn:aws:iam::111111111111:role/CaseInsightsAnalyst'] } }),
    });
    expect(stack.prodStage.stacks.mcp).toBeDefined();
    expect(app.node.findAll().filter((construct) => construct instanceof CaseInsightsApiLoggingStack)).toHaveLength(1);
    const names = pipelineResource(stack).Stages.map((stage) => stage.Name);
    expect(names.indexOf('SharedApiLogging')).toBeLessThan(names.indexOf('Dev'));
  });
});

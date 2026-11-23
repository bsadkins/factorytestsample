import * as fs from 'node:fs';
import * as path from 'node:path';
import { type App, Stack, Stage } from 'aws-cdk-lib';
import { Annotations, Match, Template } from 'aws-cdk-lib/assertions';
import {
  addCaseInsightsEnvironmentStacks,
  addCaseInsightsStacks,
  addSharedApiLogging,
  type CaseInsightsAppStacks,
  CaseInsightsApiLoggingStack,
  CaseInsightsMcpStack,
  ConfigValidationError,
  MCP_ACCESS_LOG_GROUP_PREFIX,
  parseConfig,
  resourceNames,
  sharedApiLoggingNames,
} from '../../../lib';
import { FIXTURE_CONFIG_DIR, fixtureEnvironmentConfig, type JsonObject, readFixtureJson } from '../../config/helpers';
import { normaliseAssetHashes } from '../../helpers/assets';
import { acknowledgedRules, asArray, nagApp, type PolicyStatementJson, synthError, tempOutdir, testApp } from '../../helpers/cdk';

type Env = 'dev' | 'prod';

const PROD_PRINCIPALS = ['arn:aws:iam::111111111111:role/CaseInsightsAnalyst'];
const PROD_MCP = { enableMcp: true, mcpAllowedPrincipals: PROD_PRINCIPALS } as const;
const EXISTING_ROLE = 'arn:aws:iam::111111111111:role/platform/ApiGatewayCloudWatchLogs';
const EXISTING = { apiGatewayLogging: { mode: 'existing', sharedId: 'scishared', existingRoleArn: EXISTING_ROLE } } as const;

/** Writes dev.json and/or prod.json (fixtures plus overrides) to a fresh config directory. */
function configDir(files: Partial<Record<Env, JsonObject>>): string {
  const dir = tempOutdir();
  for (const [env, overrides] of Object.entries(files) as [Env, JsonObject][]) {
    fs.writeFileSync(path.join(dir, `${env}.json`), JSON.stringify({ ...readFixtureJson(env), ...overrides }));
  }
  return dir;
}

function stacksFor(env: Env, files?: Partial<Record<Env, JsonObject>>, app: App = testApp()): CaseInsightsAppStacks {
  return addCaseInsightsStacks(app, { env, configDir: files === undefined ? FIXTURE_CONFIG_DIR : configDir(files) });
}

function loggingStackOf(stacks: CaseInsightsAppStacks): CaseInsightsApiLoggingStack {
  if (stacks.apiLogging === undefined) {
    throw new Error('shared API logging stack missing');
  }
  return stacks.apiLogging;
}

function allStacks(scope: App | Stage): Stack[] {
  return scope.node.findAll().filter((construct): construct is Stack => Stack.isStack(construct));
}

function countResources(stacks: readonly Stack[], type: string): number {
  return stacks.reduce((total, stack) => total + Object.keys(Template.fromStack(stack).findResources(type)).length, 0);
}

function apiGatewayTrustedRoles(stack: Stack): string[] {
  return Object.entries(Template.fromStack(stack).findResources('AWS::IAM::Role'))
    .filter(([, role]) => JSON.stringify(role.Properties).includes('apigateway.amazonaws.com'))
    .map(([id]) => id);
}

describe('managed shared API Gateway logging (default dev)', () => {
  let app: App;
  let stacks: CaseInsightsAppStacks;
  let logging: CaseInsightsApiLoggingStack;
  let template: Template;

  beforeAll(() => {
    app = testApp();
    stacks = stacksFor('dev', undefined, app);
    logging = loggingStackOf(stacks);
    template = Template.fromStack(logging);
  });

  test('the dev fixture selects managed mode with the shared namespace', () => {
    expect(stacks.config.apiGatewayLogging).toEqual({ mode: 'managed', sharedId: 'scishared' });
    expect(fixtureEnvironmentConfig('prod').apiGatewayLogging).toEqual(stacks.config.apiGatewayLogging);
  });

  test('ordinary deploy-all synthesis includes CaseInsightsApiLoggingStack-<sharedId> in the management account and Region', () => {
    expect(logging).toBeInstanceOf(CaseInsightsApiLoggingStack);
    expect(logging.stackName).toBe('CaseInsightsApiLoggingStack-scishared');
    expect(logging.stackName).toBe(sharedApiLoggingNames('scishared').stack);
    expect(logging.account).toBe(stacks.config.managementAccountId);
    expect(logging.region).toBe(stacks.config.region);
    const names = resourceNames(stacks.config);
    const assembly = app.synth();
    expect(assembly.stacks.map((stack) => stack.stackName).sort()).toEqual(
      [names.coreStack, names.memberRoleStack, names.analyticsStack, names.mcpStack, logging.stackName].sort(),
    );
    Annotations.fromStack(logging).hasNoError('*', Match.anyValue());
  });

  test('exactly one AWS::ApiGateway::Account sets the CloudWatch role ARN of the shared role, and both are retained', () => {
    template.resourceCountIs('AWS::ApiGateway::Account', 1);
    template.resourceCountIs('AWS::IAM::Role', 1);
    template.resourceCountIs('AWS::IAM::Policy', 0);
    const roleId = logging.getLogicalId(logging.logging.role.node.defaultChild as never);
    const account = Object.values(template.findResources('AWS::ApiGateway::Account'))[0] as {
      Properties: { CloudWatchRoleArn: unknown };
      DeletionPolicy: string;
      UpdateReplacePolicy: string;
    };
    expect(account.Properties).toEqual({ CloudWatchRoleArn: { 'Fn::GetAtt': [roleId, 'Arn'] } });
    expect(account.DeletionPolicy).toBe('Retain');
    expect(account.UpdateReplacePolicy).toBe('Retain');
    const role = template.findResources('AWS::IAM::Role')[roleId] as { DeletionPolicy: string; UpdateReplacePolicy: string };
    expect(role.DeletionPolicy).toBe('Retain');
    expect(role.UpdateReplacePolicy).toBe('Retain');
  });

  test('the logging role is trusted only by API Gateway and writes only to solution access-log groups', () => {
    const roleId = logging.getLogicalId(logging.logging.role.node.defaultChild as never);
    const role = template.findResources('AWS::IAM::Role')[roleId]?.Properties as {
      RoleName?: string;
      ManagedPolicyArns?: unknown;
      AssumeRolePolicyDocument: { Statement: PolicyStatementJson[] };
      Policies: { PolicyName: string; PolicyDocument: { Statement: PolicyStatementJson[] } }[];
    };
    expect(role.AssumeRolePolicyDocument.Statement).toEqual([
      { Action: 'sts:AssumeRole', Effect: 'Allow', Principal: { Service: 'apigateway.amazonaws.com' } },
    ]);
    // Generated name: a retained role from a deleted shared stack never blocks re-creation.
    expect(role.RoleName).toBeUndefined();
    expect(role.ManagedPolicyArns).toBeUndefined();
    expect(role.Policies.map((policy) => policy.PolicyName)).toEqual(['ApiGatewayAccessLogs']);
    const { region, managementAccountId: account } = stacks.config;
    const logGroups = `arn:aws:logs:${region}:${account}:log-group:`;
    const statements = role.Policies[0]?.PolicyDocument.Statement ?? [];
    expect(statements).toEqual([
      {
        Sid: 'WriteSolutionApiAccessLogs',
        Effect: 'Allow',
        Action: ['logs:CreateLogStream', 'logs:DescribeLogStreams', 'logs:FilterLogEvents', 'logs:GetLogEvents', 'logs:PutLogEvents'],
        Resource: `${logGroups}${MCP_ACCESS_LOG_GROUP_PREFIX}*`,
      },
      {
        Sid: 'DescribeLogGroups',
        Effect: 'Allow',
        Action: 'logs:DescribeLogGroups',
        Resource: `${logGroups}*`,
      },
    ]);
    // Every environment's MCP access log group is inside the scoped prefix.
    for (const uniqueId of ['scidev', 'sciprod']) {
      expect(resourceNames({ uniqueId }).mcpAccessLogGroup.startsWith(MCP_ACCESS_LOG_GROUP_PREFIX)).toBe(true);
    }
    expect(MCP_ACCESS_LOG_GROUP_PREFIX).toBe('/aws/apigateway/CaseInsights-Mcp-');
    for (const statement of statements) {
      for (const action of asArray(statement.Action).map(String)) {
        expect(action).not.toMatch(/\*|CreateLogGroup|Delete|Put(Retention|ResourcePolicy)/);
      }
    }
  });

  test('environment stacks hold no Account resource or API Gateway logging role; MCP deploys after the shared stack', () => {
    const mcp = stacks.mcp;
    if (mcp === undefined) {
      throw new Error('MCP stack missing');
    }
    expect(mcp).toBeInstanceOf(CaseInsightsMcpStack);
    expect(mcp.dependencies).toContain(logging);
    expect(logging.dependencies).toEqual([]);
    for (const stack of [stacks.core, stacks.memberRole, stacks.analytics as Stack, mcp]) {
      Template.fromStack(stack).resourceCountIs('AWS::ApiGateway::Account', 0);
      expect(apiGatewayTrustedRoles(stack)).toEqual([]);
      // No reference couples an environment to the shared stack's outputs.
      expect(JSON.stringify(Template.fromStack(stack).toJSON())).not.toContain('Fn::ImportValue');
    }
    expect(JSON.stringify(Template.fromStack(mcp).toJSON())).not.toContain('CloudWatchRole');
  });

  test('outputs name the shared role and the owner; MCP records which stack owns its logging prerequisite', () => {
    const roleId = logging.getLogicalId(logging.logging.role.node.defaultChild as never);
    template.hasOutput('ApiGatewayCloudWatchRoleArn', { Value: { 'Fn::GetAtt': [roleId, 'Arn'] } });
    template.hasOutput('ApiGatewayCloudWatchRoleName', { Value: { Ref: roleId } });
    Template.fromStack(stacks.mcp as Stack).hasOutput('ApiGatewayLoggingStack', { Value: logging.stackName });
  });

  test('shared resources carry the project and shared namespace tags, never an environment tag', () => {
    const json = JSON.stringify(template.toJSON());
    expect(json).not.toContain('"Environment"');
    expect(json).not.toContain('"UniqueId"');
    template.hasResourceProperties('AWS::IAM::Role', {
      Tags: Match.arrayWith([
        { Key: 'Project', Value: 'SupportCaseInsights' },
        { Key: 'SharedId', Value: 'scishared' },
      ]),
    });
    Template.fromStack(stacks.core).hasResourceProperties('AWS::SQS::Queue', {
      Tags: Match.arrayWith([{ Key: 'Environment', Value: 'dev' }, { Key: 'UniqueId', Value: 'scidev' }]),
    });
  });

  test('every cdk-nag acknowledgement in the shared stack is listed with a specific reason', () => {
    // Wildcard-resource acknowledgements are recorded during synthesis (cached after the first call).
    app.synth();
    const acknowledged = acknowledgedRules(logging).map(({ path: constructPath, id, reason }) => ({
      path: constructPath.replace(`${logging.stackName}/`, ''),
      id: id.replace(/^.*::(?=AwsSolutions-)/, ''),
      reason,
    }));
    const { region, managementAccountId: account } = stacks.config;
    expect(acknowledged.map(({ path: constructPath, id }) => [constructPath, id]).sort()).toEqual(
      [
        ['ApiGatewayLogging/CloudWatchRole', `AwsSolutions-IAM5[Resource::arn:aws:logs:${region}:${account}:log-group:${MCP_ACCESS_LOG_GROUP_PREFIX}*]`],
        ['ApiGatewayLogging/CloudWatchRole', `AwsSolutions-IAM5[Resource::arn:aws:logs:${region}:${account}:log-group:*]`],
      ].sort(),
    );
    for (const { reason } of acknowledged) {
      expect(reason.length).toBeGreaterThanOrEqual(40);
    }
  });

  test('template snapshot', () => {
    expect(normaliseAssetHashes(template.toJSON())).toMatchSnapshot();
  });
});

describe('shared ownership across environments', () => {
  test('dev and prod (MCP enabled) synthesise the identical shared stack, so the second deployment reuses it', () => {
    const dev = loggingStackOf(stacksFor('dev', { dev: {}, prod: PROD_MCP }));
    const prod = loggingStackOf(stacksFor('prod', { dev: {}, prod: PROD_MCP }));
    expect(prod.stackName).toBe(dev.stackName);
    expect(Template.fromStack(prod).toJSON()).toEqual(Template.fromStack(dev).toJSON());
  });

  test('a root-owned shared stack serves dev and prod stages: one Account association in the whole assembly', () => {
    const app = nagApp();
    const dev = fixtureEnvironmentConfig('dev');
    const prod = parseConfig({ ...readFixtureJson('prod'), ...PROD_MCP }, 'prod with MCP');
    const shared = addSharedApiLogging(app, [dev, prod]);
    expect(shared?.stack).toBeInstanceOf(CaseInsightsApiLoggingStack);
    const devStage = new Stage(app, 'Dev', { env: { account: dev.managementAccountId, region: dev.region } });
    const prodStage = new Stage(app, 'Prod', { env: { account: prod.managementAccountId, region: prod.region } });
    const devStacks = addCaseInsightsEnvironmentStacks(devStage, dev, { sharedApiLogging: shared });
    const prodStacks = addCaseInsightsEnvironmentStacks(prodStage, prod, { sharedApiLogging: shared });
    expect(devStacks.mcp).toBeDefined();
    expect(prodStacks.mcp).toBeDefined();

    expect(countResources(allStacks(app), 'AWS::ApiGateway::Account')).toBe(1);
    expect(countResources(allStacks(devStage), 'AWS::ApiGateway::Account')).toBe(0);
    expect(countResources(allStacks(prodStage), 'AWS::ApiGateway::Account')).toBe(0);
    for (const stack of [...allStacks(devStage), ...allStacks(prodStage)]) {
      expect(apiGatewayTrustedRoles(stack)).toEqual([]);
    }
    expect(app.node.children.filter((child) => child instanceof CaseInsightsApiLoggingStack)).toHaveLength(1);
    // CDK cannot order stacks across stages; the deploying pipeline runs the root stack first.
    expect(devStacks.mcp?.dependencies).not.toContain(shared?.stack);
  });

  test('a stage whose configuration disagrees with the shared settings is rejected', () => {
    const app = testApp();
    const dev = fixtureEnvironmentConfig('dev');
    const shared = addSharedApiLogging(app, [dev]);
    const other = parseConfig({ ...readFixtureJson('dev'), apiGatewayLogging: { mode: 'managed', sharedId: 'other' } }, 'conflicting');
    expect(() => addCaseInsightsEnvironmentStacks(new Stage(app, 'Dev'), other, { sharedApiLogging: shared })).toThrow(
      /apiGatewayLogging.*differs from the shared API Gateway logging settings/,
    );
  });

  test('an MCP environment without a shared logging owner is rejected', () => {
    const app = testApp();
    expect(() => addCaseInsightsEnvironmentStacks(new Stage(app, 'Dev'), fixtureEnvironmentConfig('dev'), {})).toThrow(ConfigValidationError);
  });

  test('shared settings for a different account or Region are rejected', () => {
    const app = testApp();
    const dev = fixtureEnvironmentConfig('dev');
    const shared = addSharedApiLogging(app, [dev]);
    const elsewhere = parseConfig({ ...readFixtureJson('dev'), region: 'eu-west-1' }, 'other Region');
    expect(() => addCaseInsightsEnvironmentStacks(new Stage(app, 'Eu'), elsewhere, { sharedApiLogging: shared })).toThrow(/eu-west-1/);
  });

  test.each([
    ['different shared namespaces', { apiGatewayLogging: { mode: 'managed', sharedId: 'other' } }],
    ['existing mode in prod only', EXISTING],
  ])('dev and prod in the same account and Region with %s are rejected before any stack is created', (_name, prodOverrides) => {
    for (const env of ['dev', 'prod'] as const) {
      const app = testApp();
      expect(() => stacksFor(env, { dev: {}, prod: prodOverrides }, app)).toThrow(/same account and Region must use the same apiGatewayLogging/);
      expect(app.node.children).toHaveLength(0);
    }
  });

  test('environments in different Regions may choose different shared settings', () => {
    const stacks = stacksFor('dev', { dev: {}, prod: { region: 'eu-west-1', apiGatewayLogging: { mode: 'managed', sharedId: 'other' } } });
    expect(stacks.apiLogging?.stackName).toBe('CaseInsightsApiLoggingStack-scishared');
  });

  test('addSharedApiLogging rejects inconsistent configurations itself', () => {
    const dev = fixtureEnvironmentConfig('dev');
    const prod = parseConfig({ ...readFixtureJson('prod'), ...PROD_MCP, ...EXISTING }, 'prod existing');
    const app = testApp();
    expect(() => addSharedApiLogging(app, [dev, prod])).toThrow(ConfigValidationError);
    expect(app.node.children).toHaveLength(0);
  });
});

describe('existing-role mode and MCP disabled', () => {
  test('existing mode creates neither the shared role nor an Account association and records the supplied role', () => {
    const app = testApp();
    const stacks = stacksFor('dev', { dev: EXISTING, prod: EXISTING }, app);
    expect(stacks.apiLogging).toBeUndefined();
    const assembly = app.synth();
    expect(assembly.stacks.map((stack) => stack.stackName)).not.toContain('CaseInsightsApiLoggingStack-scishared');
    const envStacks = [stacks.core, stacks.memberRole, stacks.analytics as Stack, stacks.mcp as Stack];
    expect(countResources(envStacks, 'AWS::ApiGateway::Account')).toBe(0);
    for (const stack of envStacks) {
      expect(apiGatewayTrustedRoles(stack)).toEqual([]);
    }
    const mcp = Template.fromStack(stacks.mcp as Stack);
    mcp.hasOutput('ApiGatewayCloudWatchRoleArn', { Value: EXISTING_ROLE });
    expect(Object.keys(mcp.findOutputs('ApiGatewayLoggingStack'))).toEqual([]);
    expect(stacks.mcp?.dependencies.map((stack) => stack.stackName)).not.toContain('CaseInsightsApiLoggingStack-scishared');
  });

  test('existing mode synthesises with cdk-nag', () => {
    const app = testApp();
    stacksFor('dev', { dev: EXISTING, prod: EXISTING }, app);
    expect(synthError(app)).toBeUndefined();
  });

  test.each(['dev', 'prod'] as const)('%s with MCP disabled adds no shared stack, Account association or logging role', (env) => {
    const app = testApp();
    const stacks = stacksFor(env, { [env]: { enableMcp: false } }, app);
    expect(stacks.apiLogging).toBeUndefined();
    const assembly = app.synth();
    expect(assembly.stacks.map((stack) => stack.stackName).some((name) => name.startsWith('CaseInsightsApiLoggingStack'))).toBe(false);
    const envStacks = [stacks.core, stacks.memberRole, stacks.analytics as Stack];
    expect(countResources(envStacks, 'AWS::ApiGateway::Account')).toBe(0);
    for (const stack of envStacks) {
      expect(apiGatewayTrustedRoles(stack)).toEqual([]);
    }
  });

  test('the prod fixture (MCP disabled) adds no shared stack', () => {
    expect(stacksFor('prod').apiLogging).toBeUndefined();
  });
});

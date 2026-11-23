import { execFileSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { type App, type Stack } from 'aws-cdk-lib';
import { Annotations, Match, Template } from 'aws-cdk-lib/assertions';
import {
  addCaseInsightsStacks,
  bedrockModelArns,
  bundlingPython,
  type CaseInsightsAppStacks,
  CaseInsightsMcpStack,
  ConfigValidationError,
  MCP_ALARM_PURPOSES,
  MCP_AUTH_FAILURE_ALARM_THRESHOLD,
  MCP_AUTH_FAILURE_RESPONSE_TYPES,
  MCP_AUTH_FAILURES_METRIC,
  MCP_MAX_BODY_BYTES,
  MCP_SERVICE_NAME,
  METRICS_NAMESPACE,
  REPO_ROOT,
  resourceNames,
  SAFE_POWERTOOLS_ENVIRONMENT,
} from '../../../lib';
import { FIXTURE_CONFIG_DIR, type JsonObject, readFixtureJson } from '../../config/helpers';
import { normaliseAssetHashes } from '../../helpers/assets';
import { acknowledgedRules, asArray, iamPolicyStatements, type PolicyStatementJson, synthError, tempOutdir, testApp, trustStatements } from '../../helpers/cdk';
import { ownedTempDir } from '../../helpers/temp-dirs';

type Env = 'dev' | 'prod';

const PROD_PRINCIPALS = ['arn:aws:iam::111111111111:role/CaseInsightsAnalyst', 'arn:aws:iam::222222222222:role/analysts/CaseInsightsReader'];
/** Prod with MCP enabled for a fictitious organisation's analyst roles. */
const PROD_MCP = { enableMcp: true, mcpAllowedPrincipals: PROD_PRINCIPALS } as const;
const MCP_OFF = { enableAnalytics: true, enableMcp: false } as const;

function configDirWith(env: Env, overrides: JsonObject): string {
  const dir = tempOutdir();
  fs.writeFileSync(path.join(dir, `${env}.json`), JSON.stringify({ ...readFixtureJson(env), ...overrides }));
  return dir;
}

function stacksFor(env: Env, overrides?: JsonObject, app: App = testApp()): CaseInsightsAppStacks {
  return addCaseInsightsStacks(app, { env, configDir: overrides === undefined ? FIXTURE_CONFIG_DIR : configDirWith(env, overrides) });
}

/** The dev fixture, or the prod fixture with MCP enabled. */
function mcpStacks(env: Env, app: App = testApp()): CaseInsightsAppStacks {
  return env === 'dev' ? stacksFor('dev', undefined, app) : stacksFor('prod', PROD_MCP, app);
}

function mcpOf(stacks: CaseInsightsAppStacks): CaseInsightsMcpStack {
  if (stacks.mcp === undefined) {
    throw new Error('MCP stack missing');
  }
  return stacks.mcp;
}

function idOf(stack: Stack, construct: { node: { defaultChild?: unknown } }): string {
  // L1 constructs (such as the WAF web ACL) are their own CloudFormation element.
  return stack.getLogicalId((construct.node.defaultChild ?? construct) as never);
}

interface GetStackOutput {
  'Fn::GetStackOutput': { StackName: string; OutputName: string };
}

/** Resolves a weak cross-stack reference to the producer output value it reads. */
function producerOutputValue(producer: Stack, value: unknown): unknown {
  const ref = (value as Partial<GetStackOutput>)['Fn::GetStackOutput'];
  if (ref === undefined) {
    throw new Error(`not a cross-stack reference: ${JSON.stringify(value)}`);
  }
  expect(ref.StackName).toBe(producer.stackName);
  const outputs = Template.fromStack(producer).toJSON().Outputs as Record<string, { Value: unknown }>;
  return outputs[ref.OutputName]?.Value;
}

const sortedActions = (statement: PolicyStatementJson | undefined): string[] => asArray(statement?.Action).map(String).sort();

describe('MCP feature flag', () => {
  test('default dev configuration adds CaseInsightsMcpStack-<id> after Core and Analytics without a cycle', () => {
    const app = testApp();
    const stacks = stacksFor('dev', undefined, app);
    const mcp = mcpOf(stacks);
    const names = resourceNames(stacks.config);
    expect(stacks.config.enableMcp).toBe(true);
    expect(mcp).toBeInstanceOf(CaseInsightsMcpStack);
    expect(mcp.stackName).toBe(names.mcpStack);
    expect(mcp.account).toBe(stacks.config.managementAccountId);
    expect(mcp.region).toBe(stacks.config.region);
    expect(mcp.dependencies).toEqual(expect.arrayContaining([stacks.core, stacks.analytics]));
    for (const producer of [stacks.core, stacks.analytics]) {
      expect(producer?.dependencies).not.toContain(mcp);
      // Producers never read MCP outputs, so they deploy and update without the MCP stack.
      expect(JSON.stringify(Template.fromStack(producer as Stack).toJSON())).not.toContain(names.mcpStack);
    }
    const assembly = app.synth();
    // The shared API Gateway logging owner (test/cdk/stacks/api-logging-stack.test.ts) deploys first.
    expect(assembly.stacks.map((stack) => stack.stackName).sort()).toEqual(
      [names.coreStack, names.memberRoleStack, names.analyticsStack, names.mcpStack, `CaseInsightsApiLoggingStack-${stacks.config.apiGatewayLogging.sharedId}`].sort(),
    );
    expect(mcp.dependencies).toContain(stacks.apiLogging);
    Annotations.fromStack(mcp).hasNoError('*', Match.anyValue());
  });

  test('prod with MCP enabled synthesises with cdk-nag', () => {
    const app = testApp();
    const stacks = stacksFor('prod', PROD_MCP, app);
    expect(stacks.mcp).toBeDefined();
    expect(synthError(app)).toBeUndefined();
  });

  test.each(['dev', 'prod'] as const)('%s with MCP disabled creates no MCP endpoint, function, WAF or alarms anywhere', (env) => {
    const app = testApp();
    const stacks = stacksFor(env, MCP_OFF, app);
    expect(stacks.mcp).toBeUndefined();
    expect(stacks.analytics).toBeDefined();
    const assembly = app.synth();
    const names = resourceNames(stacks.config);
    expect(assembly.stacks.map((stack) => stack.stackName)).not.toContain(names.mcpStack);
    for (const stack of [stacks.core, stacks.memberRole, stacks.analytics as Stack]) {
      const template = JSON.stringify(Template.fromStack(stack).toJSON());
      expect(template).not.toMatch(/AWS::ApiGateway::|AWS::WAFv2::/);
      expect(template).not.toContain(names.mcpServerFunction);
      expect(template).not.toContain(names.mcpApi);
    }
    const purposes = stacks.core.observability.alarms.map(({ purpose }) => purpose);
    for (const purpose of Object.values(MCP_ALARM_PURPOSES)) {
      expect(purposes).not.toContain(purpose);
    }
  });

  test.each([
    ['MCP without Analytics', { enableAnalytics: false, enableMcp: true }],
    ['MCP with no allowed principals', { enableMcp: true, mcpAllowedPrincipals: [] }],
    ['MCP with a wildcard principal', { enableMcp: true, mcpAllowedPrincipals: ['*'] }],
    ['MCP with an account root principal', { enableMcp: true, mcpAllowedPrincipals: ['arn:aws:iam::111111111111:root'] }],
  ])('%s is rejected before any stack is created', (_name, overrides) => {
    const app = testApp();
    expect(() => addCaseInsightsStacks(app, { env: 'dev', configDir: configDirWith('dev', overrides) })).toThrow(ConfigValidationError);
    expect(app.node.children).toHaveLength(0);
  });
});

describe.each(['dev', 'prod'] as const)('%s MCP stack', (env) => {
  let stacks: CaseInsightsAppStacks;
  let mcp: CaseInsightsMcpStack;
  let template: Template;
  let principals: readonly string[];

  beforeAll(() => {
    stacks = mcpStacks(env);
    mcp = mcpOf(stacks);
    template = Template.fromStack(mcp);
    principals = stacks.config.mcpAllowedPrincipals;
  });

  const apiId = (): string => idOf(mcp, mcp.mcpServer.api);
  const fnId = (): string => idOf(mcp, mcp.mcpServer.handler.function);

  test('one regional REST API exposes only POST /mcp with AWS_IAM authorisation and a Lambda proxy integration', () => {
    template.resourceCountIs('AWS::ApiGateway::RestApi', 1);
    template.hasResourceProperties('AWS::ApiGateway::RestApi', {
      Name: resourceNames(stacks.config).mcpApi,
      EndpointConfiguration: { Types: ['REGIONAL'] },
    });
    const resources = Object.values(template.findResources('AWS::ApiGateway::Resource'));
    expect(resources.map((resource) => (resource.Properties as { PathPart: string }).PathPart)).toEqual(['mcp']);
    const methods = Object.values(template.findResources('AWS::ApiGateway::Method')).map(
      (method) => method.Properties as Record<string, unknown> & { Integration: Record<string, unknown> },
    );
    expect(methods).toHaveLength(1);
    const [method] = methods;
    expect(method).toMatchObject({ HttpMethod: 'POST', AuthorizationType: 'AWS_IAM' });
    expect(method?.ApiKeyRequired ?? false).toBe(false);
    expect(method?.AuthorizerId).toBeUndefined();
    expect(method?.Integration.Type).toBe('AWS_PROXY');
    expect(method?.Integration.IntegrationHttpMethod).toBe('POST');
    expect(JSON.stringify(method?.Integration.Uri)).toContain(fnId());
    // No public, mock or preflight method anywhere: no OPTIONS, ANY or NONE authorisation.
    template.resourceCountIs('AWS::ApiGateway::Authorizer', 0);
    template.resourceCountIs('AWS::ApiGateway::ApiKey', 0);
    template.resourceCountIs('AWS::ApiGateway::UsagePlan', 0);
  });

  test('CORS is disabled: no preflight method, CORS gateway responses or Access-Control headers', () => {
    const json = JSON.stringify(template.toJSON());
    expect(json).not.toMatch(/Access-Control-/i);
    expect(json).not.toContain('"HttpMethod":"OPTIONS"');
    template.resourceCountIs('AWS::ApiGateway::GatewayResponse', 0);
  });

  test('stage v1 has explicit throttling, X-Ray tracing and JSON access logs without payloads or execution data', () => {
    const { throttleRateLimit, throttleBurstLimit } = stacks.config.operations.mcp;
    template.resourceCountIs('AWS::ApiGateway::Stage', 1);
    const stage = Object.values(template.findResources('AWS::ApiGateway::Stage'))[0]?.Properties as {
      StageName: string;
      TracingEnabled: boolean;
      MethodSettings: Record<string, unknown>[];
      AccessLogSetting: { DestinationArn: unknown; Format: string };
    };
    expect(stage.StageName).toBe('v1');
    expect(stage.TracingEnabled).toBe(true);
    expect(stage.MethodSettings).toEqual([
      {
        HttpMethod: '*',
        ResourcePath: '/*',
        ThrottlingRateLimit: throttleRateLimit,
        ThrottlingBurstLimit: throttleBurstLimit,
        DataTraceEnabled: false,
        LoggingLevel: 'OFF',
        MetricsEnabled: false,
      },
    ]);
    expect(stage.AccessLogSetting.DestinationArn).toEqual({
      'Fn::Join': ['', [`arn:aws:logs:${stacks.config.region}:${stacks.config.managementAccountId}:log-group:`, { Ref: idOf(mcp, mcp.mcpServer.accessLogGroup) }]],
    });
    const format = JSON.parse(stage.AccessLogSetting.Format) as Record<string, string>;
    expect(format).toMatchObject({
      requestId: '$context.requestId',
      apiName: resourceNames(stacks.config).mcpApi,
      httpMethod: '$context.httpMethod',
      resourcePath: '$context.resourcePath',
      status: '$context.status',
      errorType: '$context.error.responseType',
      integrationStatus: '$context.integration.status',
      wafResponseCode: '$context.wafResponseCode',
      xrayTraceId: '$context.xrayTraceId',
    });
    // Bodies, request parameters, error messages (which can echo signed requests), caller ARNs and headers stay out.
    expect(stage.AccessLogSetting.Format).not.toMatch(/\$input|\$context\.error\.message|requestOverride|userArn|\$context\.identity\.caller|\$method\.request|authorization/i);
    template.resourceCountIs('AWS::ApiGateway::Account', 0);
  });

  test('access logs use a dedicated log group with the configured retention', () => {
    template.resourceCountIs('AWS::Logs::LogGroup', 2);
    const group = template.findResources('AWS::Logs::LogGroup')[idOf(mcp, mcp.mcpServer.accessLogGroup)] as {
      DeletionPolicy: string;
      Properties: { LogGroupName: string; RetentionInDays: number; KmsKeyId?: unknown; Tags?: unknown };
    };
    // No KMS key or data protection policy: only the name and retention besides the app-wide tags.
    expect({ ...group.Properties, Tags: undefined }).toEqual({
      Tags: undefined,
      LogGroupName: resourceNames(stacks.config).mcpAccessLogGroup,
      RetentionInDays: stacks.config.logRetentionDays,
    });
    expect(group.DeletionPolicy).toBe(env === 'prod' ? 'Retain' : 'Delete');
  });

  test('auth failures in the access log become the McpAuthFailures metric', () => {
    template.resourceCountIs('AWS::Logs::MetricFilter', 1);
    const filter = Object.values(template.findResources('AWS::Logs::MetricFilter'))[0]?.Properties as {
      LogGroupName: unknown;
      FilterPattern: string;
      MetricTransformations: Record<string, unknown>[];
    };
    expect(filter.LogGroupName).toEqual({ Ref: idOf(mcp, mcp.mcpServer.accessLogGroup) });
    for (const type of MCP_AUTH_FAILURE_RESPONSE_TYPES) {
      expect(filter.FilterPattern).toContain(`$.errorType = "${type}"`);
    }
    expect(filter.FilterPattern).not.toContain('WAF');
    expect(filter.MetricTransformations).toEqual([
      {
        MetricNamespace: METRICS_NAMESPACE,
        MetricName: MCP_AUTH_FAILURES_METRIC,
        MetricValue: '1',
        Dimensions: [{ Key: 'ApiName', Value: '$.apiName' }],
      },
    ]);
  });

  test('resource policy requires the Organisation AND a configured principal, and denies every other route', () => {
    const api = Object.values(template.findResources('AWS::ApiGateway::RestApi'))[0]?.Properties as {
      Policy: { Statement: PolicyStatementJson[] };
    };
    const invoke = 'execute-api:/v1/POST/mcp';
    const org = { 'aws:PrincipalOrgID': stacks.config.orgId };
    expect(api.Policy.Statement).toEqual([
      {
        Sid: 'AllowConfiguredPrincipalsInOrganization',
        Effect: 'Allow',
        Principal: { AWS: principals.length === 1 ? principals[0] : [...principals] },
        Action: 'execute-api:Invoke',
        Resource: invoke,
        Condition: { StringEquals: org },
      },
      {
        Sid: 'DenyOutsideOrganization',
        Effect: 'Deny',
        Principal: { AWS: '*' },
        Action: 'execute-api:Invoke',
        Resource: 'execute-api:/*',
        Condition: { StringNotEquals: org },
      },
      {
        Sid: 'DenyUnlistedPrincipals',
        Effect: 'Deny',
        Principal: { AWS: '*' },
        Action: 'execute-api:Invoke',
        Resource: 'execute-api:/*',
        Condition: { ArnNotEquals: { 'aws:PrincipalArn': [...principals] } },
      },
      {
        Sid: 'DenyOtherRoutes',
        Effect: 'Deny',
        Principal: { AWS: '*' },
        Action: 'execute-api:Invoke',
        NotResource: invoke,
      },
    ]);
  });

  test('a resource policy change produces a new deployment so it takes effect', () => {
    const deploymentIds = (overrides: JsonObject): string[] =>
      Object.keys(Template.fromStack(mcpOf(stacksFor(env, overrides))).findResources('AWS::ApiGateway::Deployment'));
    const base = env === 'prod' ? PROD_MCP : {};
    const before = deploymentIds(base);
    const after = deploymentIds({ ...base, mcpAllowedPrincipals: [...principals, 'arn:aws:iam::333333333333:user/ci-reader'] });
    expect(before).toHaveLength(1);
    expect(after).toHaveLength(1);
    expect(after).not.toEqual(before);
  });

  test('only the v1 POST /mcp route may invoke the function', () => {
    const permissions = Object.values(template.findResources('AWS::Lambda::Permission')).map(
      (permission) => permission.Properties as { Action: string; Principal: string; FunctionName: unknown; SourceArn: { 'Fn::Join': [string, unknown[]] } },
    );
    expect(permissions).toHaveLength(1);
    const [permission] = permissions;
    expect(permission?.Action).toBe('lambda:InvokeFunction');
    expect(permission?.Principal).toBe('apigateway.amazonaws.com');
    expect(permission?.FunctionName).toEqual({ 'Fn::GetAtt': [fnId(), 'Arn'] });
    const parts = permission?.SourceArn['Fn::Join'][1] ?? [];
    expect(parts).toContainEqual({ Ref: apiId() });
    // The stage reference resolves to the stage name v1.
    expect(parts.slice(-2)).toEqual([{ Ref: idOf(mcp, mcp.mcpServer.api.deploymentStage) }, '/POST/mcp']);
    template.hasResourceProperties('AWS::ApiGateway::Stage', { StageName: 'v1' });
    expect(JSON.stringify(permission?.SourceArn)).not.toContain('test-invoke-stage');
  });

  test('function: Python 3.12 MCP handler with reserved concurrency 20, tracing and server-side query configuration', () => {
    const fn = template.findResources('AWS::Lambda::Function')[fnId()]?.Properties as {
      FunctionName: string;
      Handler: string;
      Runtime: string;
      Architectures: string[];
      Timeout: number;
      ReservedConcurrentExecutions: number;
      TracingConfig: { Mode: string };
      LoggingConfig: { LogFormat: string; LogGroup: unknown };
      Layers: unknown[];
      Environment: { Variables: Record<string, unknown> };
    };
    const names = resourceNames(stacks.config);
    expect(fn.FunctionName).toBe(names.mcpServerFunction);
    expect(fn.Handler).toBe('mcp_server.app.handler');
    expect(fn.Runtime).toBe('python3.12');
    expect(fn.Architectures).toEqual(['x86_64']);
    expect(fn.ReservedConcurrentExecutions).toBe(20);
    expect(stacks.config.operations.mcp.reservedConcurrency).toBe(20);
    // Within API Gateway's 29-second integration timeout; the handler budgets 25 seconds per call.
    expect(fn.Timeout).toBeGreaterThan(25);
    expect(fn.Timeout).toBeLessThan(29);
    expect(fn.TracingConfig).toEqual({ Mode: 'Active' });
    expect(fn.LoggingConfig.LogFormat).toBe('JSON');
    expect(fn.Environment.Variables).toEqual({
      ATHENA_WORKGROUP: names.athenaWorkgroup,
      ATHENA_DATABASE: names.glueDatabase,
      BEDROCK_MODEL_ID: stacks.config.bedrockModelId,
      BEDROCK_MAX_TOKENS: String(stacks.config.bedrockMaxTokens),
      ...SAFE_POWERTOOLS_ENVIRONMENT,
      POWERTOOLS_SERVICE_NAME: MCP_SERVICE_NAME,
      CASE_INSIGHTS_UNIQUE_ID: stacks.config.uniqueId,
    });
    // The common layer is Core's: one layer, read through a Core output.
    expect(fn.Layers).toHaveLength(1);
    expect(producerOutputValue(stacks.core, fn.Layers[0])).toEqual({ Ref: idOf(stacks.core, stacks.core.layers.commonLayer) });
    template.resourceCountIs('AWS::Lambda::Function', 1);
    template.resourceCountIs('AWS::Lambda::LayerVersion', 0);
  });

  test('execution role: trusted by Lambda only, with read access to the analytics data, its own workgroup and the configured model', () => {
    const roleId = idOf(mcp, mcp.mcpServer.handler.role);
    expect(trustStatements(template, roleId)).toEqual([
      { Action: 'sts:AssumeRole', Effect: 'Allow', Principal: { Service: 'lambda.amazonaws.com' } },
    ]);
    template.resourceCountIs('AWS::IAM::Role', 1);
    template.hasResourceProperties('AWS::IAM::Role', { ManagedPolicyArns: Match.absent() });

    const { managementAccountId: account, region } = stacks.config;
    const names = resourceNames(stacks.config);
    const statements = iamPolicyStatements(template);
    for (const { roles } of statements) {
      expect(roles).toEqual([{ Ref: roleId }]);
    }
    const bySid = new Map(statements.map(({ statement }) => [statement.Sid, statement]));
    expect([...bySid.keys()].sort()).toEqual(
      [
        'WriteOwnLogGroup',
        'WriteXRaySegments',
        'QueryCaseInsightsWorkgroup',
        'ReadCaseSummaryCatalog',
        'ListCaseSummaryTables',
        'ReadProcessedRecords',
        'ListProcessedRecords',
        'WriteQueryResults',
        'ListQueryResults',
        'InvokeGlobalInferenceProfile1',
        'InvokeRegionalModelThroughProfile1',
        'InvokeGlobalModelThroughProfile1',
        'DenyUnapprovedModels',
        'SubscribeApprovedModelProducts',
        'ViewModelSubscriptions',
      ].sort(),
    );

    const athena = bySid.get('QueryCaseInsightsWorkgroup');
    expect(sortedActions(athena)).toEqual(['athena:GetQueryExecution', 'athena:GetQueryResults', 'athena:StartQueryExecution', 'athena:StopQueryExecution']);
    expect(athena?.Resource).toBe(`arn:aws:athena:${region}:${account}:workgroup/${names.athenaWorkgroup}`);

    const glue = (resource: string): string => `arn:aws:glue:${region}:${account}:${resource}`;
    const catalog = bySid.get('ReadCaseSummaryCatalog');
    expect(sortedActions(catalog)).toEqual(['glue:BatchGetPartition', 'glue:GetDatabase', 'glue:GetPartition', 'glue:GetPartitions', 'glue:GetTable']);
    expect(asArray(catalog?.Resource).sort()).toEqual(
      [glue('catalog'), glue(`database/${names.glueDatabase}`), glue(`table/${names.glueDatabase}/${names.glueTable}`)].sort(),
    );
    const tables = bySid.get('ListCaseSummaryTables');
    expect(sortedActions(tables)).toEqual(['glue:GetTables']);
    expect(asArray(tables?.Resource).sort()).toEqual([glue('catalog'), glue(`database/${names.glueDatabase}`), glue(`table/${names.glueDatabase}/*`)].sort());

    const processedArn = { 'Fn::GetAtt': [idOf(stacks.core, stacks.core.storage.processedBucket), 'Arn'] };
    const read = bySid.get('ReadProcessedRecords');
    expect(sortedActions(read)).toEqual(['s3:GetObject']);
    const readResource = read?.Resource as { 'Fn::Join': [string, unknown[]] };
    expect(producerOutputValue(stacks.core, readResource['Fn::Join'][1][0])).toEqual(processedArn);
    expect(readResource['Fn::Join'][1][1]).toBe('/account_number=*');
    const list = bySid.get('ListProcessedRecords');
    expect(sortedActions(list)).toEqual(['s3:GetBucketLocation', 's3:ListBucket']);
    expect(producerOutputValue(stacks.core, list?.Resource)).toEqual(processedArn);

    const analytics = stacks.analytics as Stack;
    const resultsArn = { 'Fn::GetAtt': [idOf(analytics, stacks.analytics?.analytics.resultsBucket as never), 'Arn'] };
    const write = bySid.get('WriteQueryResults');
    expect(sortedActions(write)).toEqual(['s3:AbortMultipartUpload', 's3:GetObject', 's3:ListMultipartUploadParts', 's3:PutObject']);
    const writeResource = write?.Resource as { 'Fn::Join': [string, unknown[]] };
    expect(producerOutputValue(analytics, writeResource['Fn::Join'][1][0])).toEqual(resultsArn);
    expect(writeResource['Fn::Join'][1][1]).toBe('/query-results/*');
    const listResults = bySid.get('ListQueryResults');
    expect(sortedActions(listResults)).toEqual(['s3:GetBucketLocation', 's3:ListBucket', 's3:ListBucketMultipartUploads']);
    expect(producerOutputValue(analytics, listResults?.Resource)).toEqual(resultsArn);

    const model = bedrockModelArns(stacks.config.bedrockModelId, 'aws', region, account);
    expect(bySid.get('InvokeGlobalInferenceProfile1')?.Resource).toBe(model.inferenceProfile);
    expect(bySid.get('InvokeRegionalModelThroughProfile1')?.Resource).toBe(model.regionalFoundationModel);
    expect(bySid.get('InvokeGlobalModelThroughProfile1')?.Resource).toBe(model.globalFoundationModel);
    expect(asArray(bySid.get('DenyUnapprovedModels')?.NotResource).sort()).toEqual(
      [model.inferenceProfile, model.regionalFoundationModel, model.globalFoundationModel].sort(),
    );

    // Read-only data access: no writes or deletes outside the Athena results prefix, no wildcard actions.
    const allows = statements.filter(({ statement }) => statement.Effect === 'Allow');
    for (const { statement } of allows) {
      for (const action of asArray(statement.Action).map(String)) {
        expect(action).not.toMatch(/\*/);
        if (/^s3:(Put|Delete|Abort)/.test(action)) {
          expect(statement.Sid).toBe('WriteQueryResults');
        }
        expect(action).not.toMatch(/^(glue:(Create|Update|Delete|Batch(Create|Update|Delete))|athena:(Create|Update|Delete))/);
      }
    }
  });

  test('WAF: rate limit, explicit block above 32 KiB with oversize matching, then the common rule set', () => {
    const names = resourceNames(stacks.config);
    template.resourceCountIs('AWS::WAFv2::WebACL', 1);
    const acl = Object.values(template.findResources('AWS::WAFv2::WebACL'))[0]?.Properties as {
      Name: string;
      Scope: string;
      DefaultAction: unknown;
      AssociationConfig: unknown;
      VisibilityConfig: { CloudWatchMetricsEnabled: boolean; SampledRequestsEnabled: boolean; MetricName: string };
      Rules: (Record<string, unknown> & { Name: string; Priority: number; Statement: Record<string, unknown>; VisibilityConfig: { SampledRequestsEnabled: boolean } })[];
    };
    expect(acl.Name).toBe(names.mcpWebAcl);
    expect(acl.Scope).toBe('REGIONAL');
    expect(acl.DefaultAction).toEqual({ Allow: {} });
    // Without this the API Gateway default inspects only the first 16 KiB of each body.
    expect(acl.AssociationConfig).toEqual({ RequestBody: { API_GATEWAY: { DefaultSizeInspectionLimit: 'KB_32' } } });
    expect(acl.VisibilityConfig).toMatchObject({ CloudWatchMetricsEnabled: true, SampledRequestsEnabled: false });

    const rules = [...acl.Rules].sort((a, b) => a.Priority - b.Priority);
    expect(rules.map((rule) => rule.Name)).toEqual(['RateLimitPerIp', 'BlockBodyOver32KiB', 'AWSManagedRulesCommonRuleSet']);
    for (const rule of rules) {
      // Sampled requests would retain signed headers, including session tokens.
      expect(rule.VisibilityConfig).toEqual({ CloudWatchMetricsEnabled: true, MetricName: rule.Name, SampledRequestsEnabled: false });
    }
    const [rate, size, common] = rules;
    expect(rate?.Statement).toEqual({
      RateBasedStatement: { Limit: stacks.config.operations.mcp.wafRateLimitPer5Minutes, AggregateKeyType: 'IP', EvaluationWindowSec: 300 },
    });
    expect(rate?.Action).toEqual({ Block: { CustomResponse: { ResponseCode: 429 } } });

    expect(MCP_MAX_BODY_BYTES).toBe(32768);
    expect(size?.Statement).toEqual({
      SizeConstraintStatement: {
        FieldToMatch: { Body: { OversizeHandling: 'MATCH' } },
        ComparisonOperator: 'GT',
        Size: 32768,
        TextTransformations: [{ Priority: 0, Type: 'NONE' }],
      },
    });
    expect(size?.Action).toEqual({ Block: { CustomResponse: { ResponseCode: 413 } } });

    // The managed 8 KB body-size rule only counts, so the application boundary stays 32 KiB.
    expect(common?.Statement).toEqual({
      ManagedRuleGroupStatement: {
        VendorName: 'AWS',
        Name: 'AWSManagedRulesCommonRuleSet',
        RuleActionOverrides: [{ Name: 'SizeRestrictions_BODY', ActionToUse: { Count: {} } }],
      },
    });
    expect(common?.OverrideAction).toEqual({ None: {} });
    expect(common?.Action).toBeUndefined();
  });

  test('WAF is associated with the v1 stage', () => {
    template.resourceCountIs('AWS::WAFv2::WebACLAssociation', 1);
    const association = Object.values(template.findResources('AWS::WAFv2::WebACLAssociation'))[0]?.Properties as {
      ResourceArn: { 'Fn::Join': [string, unknown[]] };
      WebACLArn: unknown;
    };
    expect(association.WebACLArn).toEqual({ 'Fn::GetAtt': [idOf(mcp, mcp.mcpServer.webAcl.webAcl as never), 'Arn'] });
    const arn = JSON.stringify(association.ResourceArn);
    expect(arn).toContain(`"Ref":"${apiId()}"`);
    expect(arn).toContain('/stages/');
    expect(arn).toContain(`"Ref":"${idOf(mcp, mcp.mcpServer.api.deploymentStage)}"`);
  });

  test('outputs give the endpoint URL and resource names', () => {
    template.hasOutput('McpEndpointUrl', {
      Value: Match.objectLike({ 'Fn::Join': ['', Match.arrayWith([{ Ref: apiId() }, { Ref: idOf(mcp, mcp.mcpServer.api.deploymentStage) }, '/mcp'])] }),
    });
    template.hasOutput('McpRestApiId', { Value: { Ref: apiId() } });
    template.hasOutput('McpFunctionName', { Value: { Ref: fnId() } });
    template.hasOutput('McpAccessLogGroupName', { Value: { Ref: idOf(mcp, mcp.mcpServer.accessLogGroup) } });
    template.hasOutput('McpWebAclArn', { Value: { 'Fn::GetAtt': [idOf(mcp, mcp.mcpServer.webAcl.webAcl), 'Arn'] } });
  });

  test('every cdk-nag acknowledgement in the MCP stack is listed with a specific reason', () => {
    const acknowledged = acknowledgedRules(mcp).map(({ path: constructPath, id, reason }) => ({
      path: constructPath.replace(`${mcp.stackName}/`, ''),
      id: id.replace(/^.*::(?=AwsSolutions-)/, ''),
      reason,
    }));
    const statement = (sid: string): PolicyStatementJson | undefined => iamPolicyStatements(template).find((entry) => entry.statement.Sid === sid)?.statement;
    const tokenOf = (sid: string): string => JSON.stringify((statement(sid)?.Resource as { 'Fn::Join': [string, unknown[]] })['Fn::Join'][1][0]);
    const names = resourceNames(stacks.config);
    expect(acknowledged.map(({ path: constructPath, id }) => [constructPath, id]).sort()).toEqual(
      [
        ['McpServer/McpFunction/TelemetryPolicy', 'AwsSolutions-IAM5[Resource::*]'],
        ['McpServer/McpFunction/Function', 'AwsSolutions-L1'],
        ['McpServer/Bedrock/Policy', 'AwsSolutions-IAM5[Resource::*]'],
        ['McpServer/McpServerAccess', `AwsSolutions-IAM5[Resource::${tokenOf('ReadProcessedRecords')}/account_number=*]`],
        ['McpServer/McpServerAccess', `AwsSolutions-IAM5[Resource::${tokenOf('WriteQueryResults')}/query-results/*]`],
        [
          'McpServer/McpServerAccess',
          `AwsSolutions-IAM5[Resource::arn:aws:glue:${stacks.config.region}:${stacks.config.managementAccountId}:table/${names.glueDatabase}/*]`,
        ],
        ['McpServer/Api', 'AwsSolutions-APIG2'],
        ['McpServer/Api/DeploymentStage.v1', 'AwsSolutions-APIG6'],
        ['McpServer/Api/Default/mcp/POST', 'AwsSolutions-COG4'],
      ].sort(),
    );
    for (const { reason } of acknowledged) {
      expect(reason.length).toBeGreaterThanOrEqual(40);
    }
    // Athena reads and writes S3 with the credentials of the role that starts the query: the MCP function role.
    const athenaReasons = acknowledged.filter(({ id }) => id.includes('/account_number=*]') || id.includes('/query-results/*]'));
    expect(athenaReasons).toHaveLength(2);
    for (const { reason } of athenaReasons) {
      expect(reason).toContain('MCP function role credentials');
      expect(reason).not.toMatch(/caller/i);
    }
  });

  test('template snapshot', () => {
    expect(normaliseAssetHashes(template.toJSON())).toMatchSnapshot();
  });
});

describe('MCP observability in Core', () => {
  let stacks: CaseInsightsAppStacks;
  let coreTemplate: Template;

  beforeAll(() => {
    stacks = mcpStacks('dev');
    coreTemplate = Template.fromStack(stacks.core);
  });

  interface AlarmJson {
    AlarmName: string;
    AlarmDescription: string;
    AlarmActions: unknown[];
    Threshold: number;
    ComparisonOperator: string;
    Namespace?: string;
    MetricName?: string;
    Statistic?: string;
    Period?: number;
    Dimensions?: { Name: string; Value: unknown }[];
  }

  const alarmFor = (purpose: string): AlarmJson => {
    const name = `${resourceNames(stacks.config).alarmPrefix}${purpose}`;
    const alarm = Object.values(coreTemplate.findResources('AWS::CloudWatch::Alarm'))
      .map((resource) => resource.Properties as AlarmJson)
      .find((candidate) => candidate.AlarmName === name);
    if (alarm === undefined) {
      throw new Error(`alarm ${name} not found`);
    }
    return alarm;
  };

  test('MCP alarms are added through core.observability and notify the alarm topic', () => {
    const purposes = stacks.core.observability.alarms.map(({ purpose }) => purpose);
    expect(purposes).toEqual(expect.arrayContaining(Object.values(MCP_ALARM_PURPOSES)));
    const topic = { Ref: idOf(stacks.core, stacks.core.observability.alarmTopic) };
    for (const purpose of Object.values(MCP_ALARM_PURPOSES)) {
      const alarm = alarmFor(purpose);
      expect(alarm.AlarmActions).toEqual([topic]);
      expect(alarm.AlarmDescription).toContain(`Runbook: docs/runbook.md#${purpose.toLowerCase()}`);
      expect(alarm.ComparisonOperator).toBe('GreaterThanOrEqualToThreshold');
    }
  });

  test('MCP alarms watch the function errors, internal errors, API 5xx responses and auth failures by literal names', () => {
    const names = resourceNames(stacks.config);
    expect(alarmFor(MCP_ALARM_PURPOSES.lambdaErrors)).toMatchObject({
      Namespace: 'AWS/Lambda',
      MetricName: 'Errors',
      Statistic: 'Sum',
      Period: 300,
      Threshold: 1,
      Dimensions: [{ Name: 'FunctionName', Value: names.mcpServerFunction }],
    });
    expect(alarmFor(MCP_ALARM_PURPOSES.internalErrors)).toMatchObject({
      Namespace: METRICS_NAMESPACE,
      MetricName: 'McpInternalErrors',
      Statistic: 'Sum',
      Threshold: 1,
      Dimensions: [{ Name: 'service', Value: MCP_SERVICE_NAME }],
    });
    expect(alarmFor(MCP_ALARM_PURPOSES.serverErrors)).toMatchObject({
      Namespace: 'AWS/ApiGateway',
      MetricName: '5XXError',
      Statistic: 'Sum',
      Threshold: 1,
      Dimensions: expect.arrayContaining([
        { Name: 'ApiName', Value: names.mcpApi },
        { Name: 'Stage', Value: 'v1' },
      ]) as unknown,
    });
    expect(alarmFor(MCP_ALARM_PURPOSES.authFailures)).toMatchObject({
      Namespace: METRICS_NAMESPACE,
      MetricName: MCP_AUTH_FAILURES_METRIC,
      Statistic: 'Sum',
      Threshold: MCP_AUTH_FAILURE_ALARM_THRESHOLD,
      Dimensions: [{ Name: 'ApiName', Value: names.mcpApi }],
    });
    // Literal metric names keep Core free of references to the MCP stack.
    expect(JSON.stringify(coreTemplate.toJSON())).not.toContain(names.mcpStack);
  });

  test('every MCP alarm has its exact runbook heading', () => {
    const runbook = fs.readFileSync(path.join(REPO_ROOT, 'docs', 'runbook.md'), 'utf8');
    const headings = new Set([...runbook.matchAll(/^### `?([A-Za-z0-9-]+)`?$/gm)].map((match) => match[1]));
    for (const purpose of Object.values(MCP_ALARM_PURPOSES)) {
      expect(headings).toContain(purpose);
    }
  });

  test('the dashboard shows MCP requests, errors, auth failures, API status and WAF blocks', () => {
    const dashboard = Object.values(coreTemplate.findResources('AWS::CloudWatch::Dashboard'))[0]?.Properties as {
      DashboardBody: { 'Fn::Join': [string, unknown[]] };
    };
    const body = dashboard.DashboardBody['Fn::Join'][1].map((part) => (typeof part === 'string' ? part : JSON.stringify(part))).join('');
    const names = resourceNames(stacks.config);
    for (const metric of ['McpRequests', 'McpErrors', 'McpToolErrors', 'McpProtocolErrors', 'McpInternalErrors', MCP_AUTH_FAILURES_METRIC, '5XXError', '4XXError', 'BlockedRequests']) {
      expect(body).toContain(`"${metric}"`);
    }
    expect(body).toContain(names.mcpApi);
    expect(body).toContain(names.mcpWebAcl);
  });
});

describe('configured MCP limits', () => {
  test('throttles and the WAF rate limit follow the configuration while reserved concurrency stays 20', () => {
    const stacks = stacksFor('dev', { operations: { mcp: { reservedConcurrency: 20, throttleRateLimit: 3, throttleBurstLimit: 6, wafRateLimitPer5Minutes: 150 } } });
    const template = Template.fromStack(mcpOf(stacks));
    template.hasResourceProperties('AWS::Lambda::Function', { Handler: 'mcp_server.app.handler', ReservedConcurrentExecutions: 20 });
    template.hasResourceProperties('AWS::ApiGateway::Stage', {
      MethodSettings: [Match.objectLike({ ThrottlingRateLimit: 3, ThrottlingBurstLimit: 6 })],
    });
    template.hasResourceProperties('AWS::WAFv2::WebACL', {
      Rules: Match.arrayWith([Match.objectLike({ Name: 'RateLimitPerIp', Statement: { RateBasedStatement: Match.objectLike({ Limit: 150 }) } })]),
    });
  });

  test.each([7, 1, 21, 100])('reserved concurrency %d is rejected before any stack is created', (value) => {
    const app = testApp();
    expect(() => stacksFor('dev', { operations: { mcp: { reservedConcurrency: value } } }, app)).toThrow(/reservedConcurrency must be 20/);
    expect(app.node.children).toHaveLength(0);
  });

  test('a different model and token limit reach the function and its Bedrock policy only', () => {
    const model = 'global.anthropic.claude-sonnet-4-5-20250929-v1:0';
    const stacks = stacksFor('dev', { bedrockModelId: model, bedrockMaxTokens: 2048 });
    const mcp = mcpOf(stacks);
    const template = Template.fromStack(mcp);
    template.hasResourceProperties('AWS::Lambda::Function', {
      Environment: { Variables: Match.objectLike({ BEDROCK_MODEL_ID: model, BEDROCK_MAX_TOKENS: '2048' }) },
    });
    const arns = bedrockModelArns(model, 'aws', stacks.config.region, stacks.config.managementAccountId);
    const profile = iamPolicyStatements(template).find(({ statement }) => statement.Sid === 'InvokeGlobalInferenceProfile1');
    expect(profile?.statement.Resource).toBe(arns.inferenceProfile);
    expect(JSON.stringify(template.toJSON())).not.toContain('claude-haiku');
  });
});

describe('packaged MCP function', () => {
  test('its staged code and Core common layer answer initialize and tools/list, and share the WAF 32 KiB boundary', () => {
    const app = testApp();
    const stacks = stacksFor('dev', undefined, app);
    const mcp = mcpOf(stacks);
    const assemblyDir = app.synth().directory;
    const fn = Template.fromStack(mcp).findResources('AWS::Lambda::Function')[idOf(mcp, mcp.mcpServer.handler.function)]?.Properties as {
      Handler: string;
      Code: { S3Key: string };
      Layers: unknown[];
      Environment: { Variables: Record<string, string> };
    };
    const staged = (key: string): string => path.join(assemblyDir, `asset.${key.replace(/\.zip$/, '')}`);
    const layerRef = producerOutputValue(stacks.core, fn.Layers[0]) as { Ref: string };
    const layer = Template.fromStack(stacks.core).findResources('AWS::Lambda::LayerVersion')[layerRef.Ref]?.Properties as { Content: { S3Key: string } };
    const codeDir = staged(fn.Code.S3Key);
    const optPython = path.join(staged(layer.Content.S3Key), 'python');
    expect(fs.readdirSync(codeDir).sort()).toEqual(['mcp_server', 'mcp_tools']);
    expect(fs.existsSync(path.join(optPython, 'common'))).toBe(true);

    const [moduleName, functionName] = [fn.Handler.slice(0, fn.Handler.lastIndexOf('.')), fn.Handler.slice(fn.Handler.lastIndexOf('.') + 1)];
    const script = [
      'import importlib, json, sys',
      `sys.path[:0] = [${JSON.stringify(codeDir)}, ${JSON.stringify(optPython)}]`,
      `handler = getattr(importlib.import_module(${JSON.stringify(moduleName)}), ${JSON.stringify(functionName)})`,
      'class Context:',
      '    function_name = "smoke"; memory_limit_in_mb = 512; aws_request_id = "smoke-1"',
      '    invoked_function_arn = "arn:aws:lambda:us-east-1:111111111111:function:smoke"',
      '    def get_remaining_time_in_millis(self): return 28000',
      'def post_raw(body):',
      '    event = {"httpMethod": "POST", "resource": "/mcp", "path": "/mcp", "isBase64Encoded": False,',
      '        "headers": {"Content-Type": "application/json", "Accept": "application/json"}, "body": body}',
      '    return handler(event, Context())',
      'def post(message):',
      '    return post_raw(json.dumps(message))',
      'def padded_ping(size):',
      '    prefix, suffix = \'{"jsonrpc":"2.0","id":"cap","method":"ping","params":{"_meta":{"pad":"\', \'"}}}\'',
      '    budget = size - len((prefix + suffix).encode())',
      '    body = prefix + "\\u20ac" * (budget // 3) + "a" * (budget % 3) + suffix',
      '    assert len(body.encode()) == size',
      '    return body',
      `limit = ${String(MCP_MAX_BODY_BYTES)}`,
      'at_cap, over_cap = post_raw(padded_ping(limit)), post_raw(padded_ping(limit + 1))',
      'transport = importlib.import_module("mcp_server.transport")',
      'init = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25",',
      '    "capabilities": {}, "clientInfo": {"name": "smoke", "version": "1"}}})',
      'tools = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})',
      'print("SMOKE-RESULT " + json.dumps({"init": init["statusCode"], "tools": [t["name"] for t in json.loads(tools["body"])["result"]["tools"]],',
      '    "atCap": at_cap["statusCode"], "overCap": over_cap["statusCode"], "maxBodyBytes": transport.MAX_BODY_BYTES,',
      '    "module": sys.modules[' + JSON.stringify(moduleName) + '].__file__}))',
    ].join('\n');
    const output = execFileSync(bundlingPython(), ['-I', '-c', script], {
      encoding: 'utf8',
      cwd: ownedTempDir('case-insights-mcp-smoke-'),
      env: { PATH: process.env.PATH ?? '', AWS_DEFAULT_REGION: 'us-east-1', POWERTOOLS_TRACE_DISABLED: 'true', ...fn.Environment.Variables },
    });
    const line = output.split('\n').find((candidate) => candidate.startsWith('SMOKE-RESULT '));
    if (line === undefined) {
      throw new Error(`no smoke result in output:\n${output}`);
    }
    const result = JSON.parse(line.slice('SMOKE-RESULT '.length)) as {
      init: number;
      tools: string[];
      module: string;
      atCap: number;
      overCap: number;
      maxBodyBytes: number;
    };
    expect(result.init).toBe(200);
    // WAF blocks bodies over MCP_MAX_BODY_BYTES; the packaged handler applies the same byte limit to multibyte bodies.
    expect(result.maxBodyBytes).toBe(MCP_MAX_BODY_BYTES);
    expect(result.atCap).toBe(200);
    expect(result.overCap).toBe(413);
    expect(result.tools).toEqual([
      'query_athena',
      'analyze_with_bedrock',
      'get_case_summary',
      'analyze_case_summaries',
      'get_temporal_patterns',
      'get_rca_analysis',
      'get_lifecycle_analysis',
      'get_service_trends',
    ]);
    expect(result.module.startsWith(codeDir)).toBe(true);
  });
});

import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { Duration } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { Role, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import { Code, LayerVersion } from 'aws-cdk-lib/aws-lambda';
import type { Stack } from 'aws-cdk-lib';
import { type CaseInsightsConfig, PipelineFunction, type PipelineFunctionProps, SAFE_POWERTOOLS_ENVIRONMENT } from '../../lib';
import { acknowledgedRules, iamPolicyStatements, nagApp, synthError, testApp, testConfig, testStack } from '../helpers/cdk';

function handlerDir(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'case-insights-handler-'));
  fs.writeFileSync(path.join(dir, 'app.py'), 'def handler(event, context):\n    return None\n');
  return dir;
}

let layerCount = 0;

function layer(stack: Stack): LayerVersion {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'case-insights-layer-'));
  fs.mkdirSync(path.join(dir, 'python'));
  fs.writeFileSync(path.join(dir, 'python', 'placeholder.py'), '');
  layerCount += 1;
  return new LayerVersion(stack, `Layer${String(layerCount)}`, { code: Code.fromAsset(dir) });
}

function props(config: CaseInsightsConfig, stack: Stack, overrides: Partial<PipelineFunctionProps> = {}): PipelineFunctionProps {
  return {
    config,
    functionName: `CaseInsights-Probe-${config.uniqueId}`,
    description: 'Factory test function',
    handlerDir: handlerDir(),
    serviceName: 'probe',
    layers: [layer(stack)],
    timeout: Duration.seconds(config.operations.functions.consumerTimeoutSeconds),
    environment: { EXTRA_SETTING: 'value' },
    ...overrides,
  };
}

describe('PipelineFunction', () => {
  const devConfig = testConfig({ logRetentionDays: 14 });
  const prodConfig = testConfig({ environment: 'prod', alarmEmail: 'alerts@example.com' });

  test('creates a Python 3.12 x86_64 function with tracing, JSON logs and safe Powertools settings', () => {
    const app = testApp();
    const stack = testStack(app, devConfig);
    const fn = new PipelineFunction(stack, 'Probe', props(devConfig, stack));
    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'CaseInsights-Probe-scitest',
      Runtime: 'python3.12',
      Architectures: ['x86_64'],
      Handler: 'app.handler',
      Timeout: 300,
      MemorySize: 512,
      TracingConfig: { Mode: 'Active' },
      LoggingConfig: { LogFormat: 'JSON', LogGroup: { Ref: stack.getLogicalId(fn.logGroup.node.defaultChild as never) } },
      Environment: {
        Variables: {
          ...SAFE_POWERTOOLS_ENVIRONMENT,
          EXTRA_SETTING: 'value',
          POWERTOOLS_SERVICE_NAME: 'probe',
          CASE_INSIGHTS_UNIQUE_ID: 'scitest',
        },
      },
      Layers: [{ Ref: Match.stringLikeRegexp('^Layer') }],
    });
    expect(SAFE_POWERTOOLS_ENVIRONMENT).toMatchObject({
      POWERTOOLS_LOGGER_LOG_EVENT: 'false',
      POWERTOOLS_TRACER_CAPTURE_RESPONSE: 'false',
      POWERTOOLS_TRACER_CAPTURE_ERROR: 'false',
    });
    template.hasResource('AWS::Logs::LogGroup', {
      DeletionPolicy: 'Delete',
      Properties: { LogGroupName: '/aws/lambda/CaseInsights-Probe-scitest', RetentionInDays: 14, KmsKeyId: Match.absent() },
    });
  });

  test('its own role has only log writes to its log group and X-Ray segment writes, with no managed policies', () => {
    const app = testApp();
    const stack = testStack(app, devConfig);
    const fn = new PipelineFunction(stack, 'Probe', props(devConfig, stack));
    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::IAM::Role', 1);
    template.hasResourceProperties('AWS::IAM::Role', {
      AssumeRolePolicyDocument: {
        Statement: [{ Action: 'sts:AssumeRole', Effect: 'Allow', Principal: { Service: 'lambda.amazonaws.com' } }],
      },
      ManagedPolicyArns: Match.absent(),
    });
    expect(iamPolicyStatements(template).map(({ statement }) => statement)).toEqual([
      {
        Sid: 'WriteOwnLogGroup',
        Effect: 'Allow',
        Action: ['logs:CreateLogStream', 'logs:PutLogEvents'],
        Resource: { 'Fn::GetAtt': [stack.getLogicalId(fn.logGroup.node.defaultChild as never), 'Arn'] },
      },
      { Sid: 'WriteXRaySegments', Effect: 'Allow', Action: ['xray:PutTelemetryRecords', 'xray:PutTraceSegments'], Resource: '*' },
    ]);
  });

  test('uses a supplied focused role instead of creating one, and retains prod log groups', () => {
    const app = testApp();
    const stack = testStack(app, prodConfig);
    const role = new Role(stack, 'FocusedRole', { assumedBy: new ServicePrincipal('lambda.amazonaws.com') });
    const fn = new PipelineFunction(stack, 'Probe', props(prodConfig, stack, { role, reservedConcurrentExecutions: 2, memorySize: 1024 }));
    const template = Template.fromStack(stack);
    expect(fn.role).toBe(role);
    template.resourceCountIs('AWS::IAM::Role', 1);
    template.hasResourceProperties('AWS::Lambda::Function', { ReservedConcurrentExecutions: 2, MemorySize: 1024 });
    template.hasResource('AWS::Logs::LogGroup', { DeletionPolicy: 'Retain', Properties: { RetentionInDays: 90 } });
  });

  test('rejects names without uniqueId, invalid service names and overrides of managed settings', () => {
    const stack = testStack(testApp(), devConfig);
    expect(() => new PipelineFunction(stack, 'A', props(devConfig, stack, { functionName: 'CaseInsights-Probe' }))).toThrow(/uniqueId/);
    expect(() => new PipelineFunction(stack, 'B', props(devConfig, stack, { serviceName: 'Bad Name' }))).toThrow(/service name/);
    expect(
      () => new PipelineFunction(stack, 'C', props(devConfig, stack, { environment: { POWERTOOLS_LOGGER_LOG_EVENT: 'true' } })),
    ).toThrow(/cannot be overridden/);
  });

  test('passes cdk-nag with only the X-Ray wildcard and runtime acknowledgements', () => {
    const app = nagApp();
    const stack = testStack(app, devConfig);
    const fn = new PipelineFunction(stack, 'Probe', props(devConfig, stack));
    expect(synthError(app)).toBeUndefined();
    expect(acknowledgedRules(fn).map(({ path, id }) => [path, id.replace(/^.*::(?=AwsSolutions-)/, '')])).toEqual([
      ['TestStack/Probe/TelemetryPolicy', 'AwsSolutions-IAM5[Resource::*]'],
      ['TestStack/Probe/Function', 'AwsSolutions-L1'],
    ]);
  });
});

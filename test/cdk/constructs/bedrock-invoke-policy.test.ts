import { Template } from 'aws-cdk-lib/assertions';
import { Role, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import {
  BEDROCK_MARKETPLACE_PRODUCT_IDS,
  type BedrockModelId,
  BedrockInvokePolicy,
  HAIKU_4_5_GLOBAL_PROFILE,
  SONNET_4_5_GLOBAL_PROFILE,
  SUPPORTED_BEDROCK_MODEL_IDS,
} from '../../../lib';
import { acknowledgedRules, asArray, iamPolicyStatements, nagApp, synthError, testApp, testConfig, testStack } from '../../helpers/cdk';

const config = testConfig();
const REGION = config.region;
const ACCOUNT = config.managementAccountId;
const HAIKU_MODEL = 'anthropic.claude-haiku-4-5-20251001-v1:0';
const SONNET_MODEL = 'anthropic.claude-sonnet-4-5-20250929-v1:0';

function profileArn(profile: string): string {
  return `arn:aws:bedrock:${REGION}:${ACCOUNT}:inference-profile/${profile}`;
}

function build(modelIds: readonly BedrockModelId[]) {
  const app = testApp();
  const stack = testStack(app, config);
  const role = new Role(stack, 'AnalysisRole', { assumedBy: new ServicePrincipal('lambda.amazonaws.com') });
  const access = new BedrockInvokePolicy(stack, 'Bedrock', { role, modelIds });
  return { template: Template.fromStack(stack), access };
}

describe('BedrockInvokePolicy', () => {
  test('Marketplace product IDs cover exactly the approved profiles and match the AWS model cards', () => {
    expect(Object.keys(BEDROCK_MARKETPLACE_PRODUCT_IDS).sort()).toEqual([...SUPPORTED_BEDROCK_MODEL_IDS].sort());
    expect(BEDROCK_MARKETPLACE_PRODUCT_IDS).toEqual({
      [HAIKU_4_5_GLOBAL_PROFILE]: 'prod-xdkflymybwmvi',
      [SONNET_4_5_GLOBAL_PROFILE]: 'prod-mxcfnwvpd6kb4',
    });
  });

  test('grants the three-part global inference access for the configured profile only', () => {
    const { template } = build([HAIKU_4_5_GLOBAL_PROFILE]);
    const statements = iamPolicyStatements(template).map(({ statement }) => statement);
    const haikuProfile = profileArn(HAIKU_4_5_GLOBAL_PROFILE);
    expect(statements).toEqual([
      {
        Sid: 'InvokeGlobalInferenceProfile1',
        Effect: 'Allow',
        Action: 'bedrock:InvokeModel',
        Resource: haikuProfile,
        Condition: { StringEquals: { 'aws:RequestedRegion': REGION } },
      },
      {
        Sid: 'InvokeRegionalModelThroughProfile1',
        Effect: 'Allow',
        Action: 'bedrock:InvokeModel',
        Resource: `arn:aws:bedrock:${REGION}::foundation-model/${HAIKU_MODEL}`,
        Condition: { StringEquals: { 'aws:RequestedRegion': REGION, 'bedrock:InferenceProfileArn': haikuProfile } },
      },
      {
        Sid: 'InvokeGlobalModelThroughProfile1',
        Effect: 'Allow',
        Action: 'bedrock:InvokeModel',
        Resource: `arn:aws:bedrock:::foundation-model/${HAIKU_MODEL}`,
        Condition: { StringEquals: { 'aws:RequestedRegion': 'unspecified', 'bedrock:InferenceProfileArn': haikuProfile } },
      },
      {
        Sid: 'DenyUnapprovedModels',
        Effect: 'Deny',
        Action: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        NotResource: [
          haikuProfile,
          `arn:aws:bedrock:${REGION}::foundation-model/${HAIKU_MODEL}`,
          `arn:aws:bedrock:::foundation-model/${HAIKU_MODEL}`,
        ],
      },
      {
        Sid: 'SubscribeApprovedModelProducts',
        Effect: 'Allow',
        Action: 'aws-marketplace:Subscribe',
        Resource: '*',
        Condition: { 'ForAnyValue:StringEquals': { 'aws-marketplace:ProductId': ['prod-xdkflymybwmvi'] } },
      },
      { Sid: 'ViewModelSubscriptions', Effect: 'Allow', Action: 'aws-marketplace:ViewSubscriptions', Resource: '*' },
    ]);
  });

  test('never allows the unconfigured model, other models, wildcards or Marketplace writes beyond Subscribe', () => {
    const { template } = build([HAIKU_4_5_GLOBAL_PROFILE]);
    const allows = iamPolicyStatements(template)
      .map(({ statement }) => statement)
      .filter((statement) => statement.Effect === 'Allow');
    const text = JSON.stringify(allows);
    expect(text).not.toContain(SONNET_MODEL);
    expect(text).not.toMatch(/bedrock:\*|foundation-model\/\*|inference-profile\/\*/);
    const actions = allows.flatMap((statement) => asArray(statement.Action));
    expect(new Set(actions)).toEqual(new Set(['bedrock:InvokeModel', 'aws-marketplace:Subscribe', 'aws-marketplace:ViewSubscriptions']));
    for (const statement of allows.filter((s) => asArray(s.Action).some((a) => a.startsWith('bedrock:')))) {
      expect(statement.Resource).not.toBe('*');
    }
  });

  test('supports both approved profiles with per-profile conditions and both product IDs', () => {
    const { template, access } = build([HAIKU_4_5_GLOBAL_PROFILE, SONNET_4_5_GLOBAL_PROFILE, HAIKU_4_5_GLOBAL_PROFILE]);
    expect(access.modelArns).toHaveLength(2);
    const statements = iamPolicyStatements(template).map(({ statement }) => statement);
    const sonnetGlobal = statements.find((statement) => statement.Sid === 'InvokeGlobalModelThroughProfile2');
    expect(sonnetGlobal).toMatchObject({
      Resource: `arn:aws:bedrock:::foundation-model/${SONNET_MODEL}`,
      Condition: { StringEquals: { 'bedrock:InferenceProfileArn': profileArn(SONNET_4_5_GLOBAL_PROFILE) } },
    });
    expect(statements.find((statement) => statement.Sid === 'SubscribeApprovedModelProducts')?.Condition).toEqual({
      'ForAnyValue:StringEquals': { 'aws-marketplace:ProductId': ['prod-xdkflymybwmvi', 'prod-mxcfnwvpd6kb4'] },
    });
    expect(asArray(statements.find((statement) => statement.Sid === 'DenyUnapprovedModels')?.NotResource)).toHaveLength(6);
  });

  test('rejects an empty or unsupported model list', () => {
    expect(() => build([])).toThrow(/at least one/);
    expect(() => build(['anthropic.claude-3-haiku-20240307-v1:0' as BedrockModelId])).toThrow(/Unsupported Bedrock model/);
  });

  test('passes cdk-nag with a single, specific wildcard acknowledgement on its own policy', () => {
    const app = nagApp();
    const stack = testStack(app, config);
    const role = new Role(stack, 'AnalysisRole', { assumedBy: new ServicePrincipal('lambda.amazonaws.com') });
    const access = new BedrockInvokePolicy(stack, 'Bedrock', { role, modelIds: [config.bedrockModelId] });
    expect(synthError(app)).toBeUndefined();
    const acknowledged = acknowledgedRules(access);
    expect(acknowledged).toHaveLength(1);
    expect(acknowledged[0]?.path).toBe('TestStack/Bedrock/Policy');
    expect(acknowledged[0]?.id).toMatch(/AwsSolutions-IAM5\[Resource::\*\]$/);
    expect(acknowledged[0]?.reason).toMatch(/ViewSubscriptions/);
  });
});

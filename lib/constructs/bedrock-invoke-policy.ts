import { Stack } from 'aws-cdk-lib';
import { Effect, type IRole, Policy, PolicyStatement } from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import {
  type BedrockModelId,
  HAIKU_4_5_GLOBAL_PROFILE,
  SONNET_4_5_GLOBAL_PROFILE,
  SUPPORTED_BEDROCK_MODEL_IDS,
} from '../config';
import { acknowledgeNagFinding } from '../nag';

/**
 * AWS Marketplace product IDs of the approved models, from the "Marketplace product ID" field of
 * each Amazon Bedrock model card (see library/aws-references.md for the source pages).
 */
export const BEDROCK_MARKETPLACE_PRODUCT_IDS: Readonly<Record<BedrockModelId, string>> = {
  [HAIKU_4_5_GLOBAL_PROFILE]: 'prod-xdkflymybwmvi',
  [SONNET_4_5_GLOBAL_PROFILE]: 'prod-mxcfnwvpd6kb4',
};

const GLOBAL_PROFILE_PREFIX = 'global.';

/** Actions that run inference. Converse and ConverseStream authorise with these same actions. */
export const BEDROCK_INVOKE_ACTIONS = ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'] as const;

/** Foundation model ID behind a global inference profile ID. */
export function foundationModelId(profileId: BedrockModelId): string {
  return profileId.slice(GLOBAL_PROFILE_PREFIX.length);
}

export interface BedrockModelArns {
  readonly inferenceProfile: string;
  /** Foundation model in the requesting Region. */
  readonly regionalFoundationModel: string;
  /** Region-agnostic foundation model evaluated for global cross-Region routing. */
  readonly globalFoundationModel: string;
}

export function bedrockModelArns(profileId: BedrockModelId, partition: string, region: string, account: string): BedrockModelArns {
  const model = foundationModelId(profileId);
  return {
    inferenceProfile: `arn:${partition}:bedrock:${region}:${account}:inference-profile/${profileId}`,
    regionalFoundationModel: `arn:${partition}:bedrock:${region}::foundation-model/${model}`,
    globalFoundationModel: `arn:${partition}:bedrock:::foundation-model/${model}`,
  };
}

export interface BedrockInvokePolicyProps {
  readonly role: IRole;
  /** Approved global inference profiles this role may invoke; normally only the configured one. */
  readonly modelIds: readonly BedrockModelId[];
}

/**
 * Least-privilege Amazon Bedrock access for one execution role:
 * - InvokeModel on the global inference profile and, only through that profile, the Regional and
 *   global foundation-model ARNs (the three-part policy AWS documents for global cross-Region inference);
 * - an explicit deny of every other model, profile or invocation resource;
 * - AWS Marketplace Subscribe only for the approved products, so the first invocation can complete
 *   the automatic subscription, plus ViewSubscriptions, which supports no resource or product scoping.
 * IAM cannot submit or waive Anthropic's first-time-use form; that remains an account prerequisite.
 */
export class BedrockInvokePolicy extends Construct {
  public readonly policy: Policy;
  public readonly modelArns: readonly BedrockModelArns[];

  public constructor(scope: Construct, id: string, props: BedrockInvokePolicyProps) {
    super(scope, id);
    const modelIds = [...new Set(props.modelIds)];
    if (modelIds.length === 0) {
      throw new Error('BedrockInvokePolicy needs at least one approved model ID');
    }
    for (const modelId of modelIds) {
      if (!(SUPPORTED_BEDROCK_MODEL_IDS as readonly string[]).includes(modelId)) {
        throw new Error(`Unsupported Bedrock model ID: ${modelId}`);
      }
    }

    const stack = Stack.of(this);
    const region = stack.region;
    this.modelArns = modelIds.map((modelId) => bedrockModelArns(modelId, stack.partition, region, stack.account));

    const statements: PolicyStatement[] = [];
    this.modelArns.forEach((arns, index) => {
      const suffix = String(index + 1);
      statements.push(
        new PolicyStatement({
          sid: `InvokeGlobalInferenceProfile${suffix}`,
          actions: ['bedrock:InvokeModel'],
          resources: [arns.inferenceProfile],
          conditions: { StringEquals: { 'aws:RequestedRegion': region } },
        }),
        new PolicyStatement({
          sid: `InvokeRegionalModelThroughProfile${suffix}`,
          actions: ['bedrock:InvokeModel'],
          resources: [arns.regionalFoundationModel],
          conditions: {
            StringEquals: { 'aws:RequestedRegion': region, 'bedrock:InferenceProfileArn': arns.inferenceProfile },
          },
        }),
        new PolicyStatement({
          sid: `InvokeGlobalModelThroughProfile${suffix}`,
          actions: ['bedrock:InvokeModel'],
          resources: [arns.globalFoundationModel],
          conditions: {
            StringEquals: { 'aws:RequestedRegion': 'unspecified', 'bedrock:InferenceProfileArn': arns.inferenceProfile },
          },
        }),
      );
    });

    statements.push(
      new PolicyStatement({
        sid: 'DenyUnapprovedModels',
        effect: Effect.DENY,
        actions: [...BEDROCK_INVOKE_ACTIONS],
        notResources: this.modelArns.flatMap((arns) => [
          arns.inferenceProfile,
          arns.regionalFoundationModel,
          arns.globalFoundationModel,
        ]),
      }),
      new PolicyStatement({
        sid: 'SubscribeApprovedModelProducts',
        actions: ['aws-marketplace:Subscribe'],
        resources: ['*'],
        conditions: {
          'ForAnyValue:StringEquals': {
            'aws-marketplace:ProductId': modelIds.map((modelId) => BEDROCK_MARKETPLACE_PRODUCT_IDS[modelId]),
          },
        },
      }),
      new PolicyStatement({
        sid: 'ViewModelSubscriptions',
        actions: ['aws-marketplace:ViewSubscriptions'],
        resources: ['*'],
      }),
    );

    this.policy = new Policy(this, 'Policy', { roles: [props.role], statements });
    acknowledgeNagFinding(
      this.policy,
      'AwsSolutions-IAM5[Resource::*]',
      'AWS Marketplace Subscribe and ViewSubscriptions have no resource-level permissions. Subscribe is limited by aws-marketplace:ProductId to the approved model products; ViewSubscriptions supports no condition keys and only lists subscriptions.',
    );
  }
}

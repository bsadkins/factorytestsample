import { CfnWebACL, CfnWebACLAssociation } from 'aws-cdk-lib/aws-wafv2';
import { Construct } from 'constructs';
import { type CaseInsightsConfig, resourceNames } from '../config';

/** Largest MCP request body in bytes; the handler enforces the same limit on decoded bytes. */
export const MCP_MAX_BODY_BYTES = 32 * 1024;
/** API Gateway web ACL body inspection limit that covers the whole permitted body. */
export const MCP_WAF_BODY_INSPECTION_LIMIT = 'KB_32';
export const MCP_WAF_RATE_WINDOW_SECONDS = 300;
/** AWS managed Core rule set rule that blocks bodies over 8 KB, below the MCP limit. */
export const COMMON_RULE_SET_BODY_SIZE_RULE = 'SizeRestrictions_BODY';

/** Rule names, which are also their CloudWatch metric names (the `Rule` dimension of AWS/WAFV2 metrics). */
export const MCP_WAF_RULES = {
  rateLimit: 'RateLimitPerIp',
  bodySize: 'BlockBodyOver32KiB',
  commonRuleSet: 'AWSManagedRulesCommonRuleSet',
} as const;

export interface McpWebAclProps {
  readonly config: CaseInsightsConfig;
  /** ARN of the API Gateway stage to protect. */
  readonly stageArn: string;
}

function visibility(metricName: string): CfnWebACL.VisibilityConfigProperty {
  // Sampled requests keep request headers for three hours, and SigV4 requests carry the
  // Authorization header and session token, so only metrics are recorded.
  return { cloudWatchMetricsEnabled: true, metricName, sampledRequestsEnabled: false };
}

/**
 * Regional AWS WAF web ACL for the MCP stage, evaluated in priority order:
 * 1. a per-IP rate-based rule;
 * 2. an explicit block of bodies over 32 KiB. The association inspects the first 32 KiB of the
 *    body, and a body larger than that is oversize; oversize handling MATCH makes the size
 *    constraint match it, so the block does not depend on the inspected portion;
 * 3. the AWS managed Core rule set, whose 8 KB body-size rule only counts so that it does not
 *    replace the 32 KiB application limit. The rule group's other body rules see at most the
 *    32 KiB the association inspects, because larger bodies were already blocked.
 * WAF is an L1-only service in CDK.
 */
export class McpWebAcl extends Construct {
  public readonly webAcl: CfnWebACL;
  public readonly association: CfnWebACLAssociation;

  public constructor(scope: Construct, id: string, props: McpWebAclProps) {
    super(scope, id);
    const { config } = props;
    const name = resourceNames(config).mcpWebAcl;

    this.webAcl = new CfnWebACL(this, 'WebAcl', {
      name,
      // WAF descriptions do not allow parentheses.
      description: `Support Case Insights MCP API protection for ${config.uniqueId}`,
      scope: 'REGIONAL',
      defaultAction: { allow: {} },
      visibilityConfig: visibility(name),
      associationConfig: { requestBody: { API_GATEWAY: { defaultSizeInspectionLimit: MCP_WAF_BODY_INSPECTION_LIMIT } } },
      rules: [
        {
          name: MCP_WAF_RULES.rateLimit,
          priority: 0,
          action: { block: { customResponse: { responseCode: 429 } } },
          statement: {
            rateBasedStatement: {
              limit: config.operations.mcp.wafRateLimitPer5Minutes,
              aggregateKeyType: 'IP',
              evaluationWindowSec: MCP_WAF_RATE_WINDOW_SECONDS,
            },
          },
          visibilityConfig: visibility(MCP_WAF_RULES.rateLimit),
        },
        {
          name: MCP_WAF_RULES.bodySize,
          priority: 1,
          action: { block: { customResponse: { responseCode: 413 } } },
          statement: {
            sizeConstraintStatement: {
              fieldToMatch: { body: { oversizeHandling: 'MATCH' } },
              comparisonOperator: 'GT',
              size: MCP_MAX_BODY_BYTES,
              textTransformations: [{ priority: 0, type: 'NONE' }],
            },
          },
          visibilityConfig: visibility(MCP_WAF_RULES.bodySize),
        },
        {
          name: MCP_WAF_RULES.commonRuleSet,
          priority: 2,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: 'AWS',
              name: 'AWSManagedRulesCommonRuleSet',
              ruleActionOverrides: [{ name: COMMON_RULE_SET_BODY_SIZE_RULE, actionToUse: { count: {} } }],
            },
          },
          visibilityConfig: visibility(MCP_WAF_RULES.commonRuleSet),
        },
      ],
    });

    this.association = new CfnWebACLAssociation(this, 'StageAssociation', {
      resourceArn: props.stageArn,
      webAclArn: this.webAcl.attrArn,
    });
  }
}

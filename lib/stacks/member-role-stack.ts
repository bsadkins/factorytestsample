import { Stack, type StackProps } from 'aws-cdk-lib';
import { CfnStackSet } from 'aws-cdk-lib/aws-cloudformation';
import type { Construct } from 'constructs';
import { type CaseInsightsConfig, resourceNames } from '../config';
import type { MemberRoleTrust } from '../constructs/member-access-roles';
import { memberRoleTemplate } from './member-role-template';

export interface CaseInsightsMemberRoleStackProps extends StackProps {
  readonly config: CaseInsightsConfig;
  /** Execution roles from the core stack that the member role trusts. */
  readonly trustedRoles: MemberRoleTrust;
}

/**
 * Service-managed StackSet `CaseInsights-MemberRole-<uniqueId>` that deploys
 * `Support-Case-Analysis-Role-<uniqueId>` to every account under the configured root/OUs in one
 * Region (IAM is global) and to accounts that join those OUs later. Service-managed StackSets never
 * deploy to the management account. Requires StackSets trusted access in AWS Organizations.
 */
export class CaseInsightsMemberRoleStack extends Stack {
  public readonly stackSet: CfnStackSet;
  /** The literal template deployed to member accounts. */
  public readonly memberTemplate: Record<string, unknown>;

  public constructor(scope: Construct, id: string, props: CaseInsightsMemberRoleStackProps) {
    super(scope, id, props);
    const { config } = props;
    const names = resourceNames(config);

    this.memberTemplate = memberRoleTemplate({
      uniqueId: config.uniqueId,
      memberRoleName: names.memberRole,
      orgId: config.orgId,
      trust: props.trustedRoles,
    });

    this.stackSet = new CfnStackSet(this, 'MemberRoleStackSet', {
      stackSetName: names.memberRoleStackSet,
      description: `Support Case Insights read-only member role ${names.memberRole}`,
      permissionModel: 'SERVICE_MANAGED',
      callAs: 'SELF',
      autoDeployment: { enabled: true, retainStacksOnAccountRemoval: false },
      capabilities: ['CAPABILITY_NAMED_IAM'],
      managedExecution: { Active: true },
      operationPreferences: {
        concurrencyMode: 'SOFT_FAILURE_TOLERANCE',
        failureTolerancePercentage: 10,
        maxConcurrentPercentage: 25,
        regionConcurrencyType: 'SEQUENTIAL',
      },
      stackInstancesGroup: [
        {
          deploymentTargets: { organizationalUnitIds: [...config.memberRoleTargets] },
          regions: [config.region],
        },
      ],
      templateBody: JSON.stringify(this.memberTemplate),
    });
  }
}

import { Stack, Token } from 'aws-cdk-lib';
import { Policy, PolicyStatement, Role, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { type CaseInsightsConfig, resourceNames } from '../config';
import { acknowledgeNagFinding } from '../nag';

/**
 * Management-account roles that member-account roles trust. Passed as plain names and an
 * account ID so the member-role StackSet needs no cross-stack export.
 */
export interface MemberRoleTrust {
  readonly account: string;
  readonly roleNames: readonly string[];
}

export interface MemberAccessResources {
  /** Execution role for the case-retrieval function (DescribeCases through the member role). */
  readonly retrievalRole: Role;
  /** Execution role for the case-annotation function (DescribeCommunications through the member role). */
  readonly annotationRole: Role;
  readonly trust: MemberRoleTrust;
}

export interface MemberAccessRolesProps {
  readonly config: CaseInsightsConfig;
}

/**
 * The only two execution roles allowed to assume `Support-Case-Analysis-Role-<uniqueId>` in member
 * accounts. Each role may assume only that role name, only in accounts of the configured
 * organization. Function-specific S3/SQS permissions are granted where each function is wired.
 */
export class MemberAccessRoles extends Construct implements MemberAccessResources {
  public readonly retrievalRole: Role;
  public readonly annotationRole: Role;
  public readonly trust: MemberRoleTrust;

  public constructor(scope: Construct, id: string, props: MemberAccessRolesProps) {
    super(scope, id);
    const { config } = props;
    const names = resourceNames(config);

    this.retrievalRole = collectorRole(this, 'CaseRetrievalRole', config, names.caseRetrievalRole, 'case retrieval');
    this.annotationRole = collectorRole(this, 'CaseAnnotationRole', config, names.caseAnnotationRole, 'case annotation');
    this.trust = { account: config.managementAccountId, roleNames: [names.caseRetrievalRole, names.caseAnnotationRole] };
  }
}

function collectorRole(scope: Construct, id: string, config: CaseInsightsConfig, roleName: string, purpose: string): Role {
  const memberRoleName = resourceNames(config).memberRole;
  const role = new Role(scope, id, {
    roleName,
    description: `Support Case Insights ${purpose} Lambda execution role (${config.uniqueId})`,
    assumedBy: new ServicePrincipal('lambda.amazonaws.com'),
  });
  const partition = Stack.of(scope).partition;
  const memberRoleArn = `arn:${partition}:iam::*:role/${memberRoleName}`;
  // cdk-nag renders an unresolved partition as <AWS::Partition> in finding IDs.
  const nagPartition = Token.isUnresolved(partition) ? '<AWS::Partition>' : partition;
  const policy = new Policy(scope, `${id}MemberRoleAccess`, {
    roles: [role],
    statements: [
      new PolicyStatement({
        sid: 'AssumeMemberSupportRole',
        actions: ['sts:AssumeRole'],
        resources: [memberRoleArn],
        conditions: { StringEquals: { 'aws:ResourceOrgID': config.orgId } },
      }),
    ],
  });
  acknowledgeNagFinding(
    policy,
    `AwsSolutions-IAM5[Resource::arn:${nagPartition}:iam::*:role/${memberRoleName}]`,
    'Member account IDs are discovered at run time from AWS Organizations, so the account segment is a wildcard; the role name is fixed and aws:ResourceOrgID limits the target to accounts in the configured organization.',
  );
  return role;
}

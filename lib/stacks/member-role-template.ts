import type { MemberRoleTrust } from '../constructs/member-access-roles';

/** The only actions the member role grants. Both are read-only and cannot change a case. */
export const MEMBER_ROLE_SUPPORT_ACTIONS = ['support:DescribeCases', 'support:DescribeCommunications'] as const;

const ROLE_NAME_PATTERN = /^[\w+=,.@-]{1,64}$/;

export interface MemberRoleTemplateProps {
  readonly uniqueId: string;
  readonly memberRoleName: string;
  readonly orgId: string;
  readonly trust: MemberRoleTrust;
}

/**
 * CloudFormation template deployed by the StackSet into every targeted member account. It is
 * fully literal (apart from the member account's own partition) so it can be reviewed as-is.
 */
export function memberRoleTemplate(props: MemberRoleTemplateProps): Record<string, unknown> {
  const { trust } = props;
  if (!/^[0-9]{12}$/.test(trust.account)) {
    throw new Error(`Trusted account must be a 12-digit account ID; got ${trust.account}`);
  }
  if (trust.roleNames.length === 0) {
    throw new Error('The member role must trust at least one management-account role');
  }
  for (const name of [props.memberRoleName, ...trust.roleNames]) {
    if (!ROLE_NAME_PATTERN.test(name)) {
      throw new Error(`Invalid IAM role name: ${name}`);
    }
  }

  return {
    AWSTemplateFormatVersion: '2010-09-09',
    Description: `Support Case Insights read-only member role (${props.uniqueId}). Managed by StackSet; do not edit in member accounts.`,
    Resources: {
      SupportCaseAnalysisRole: {
        Type: 'AWS::IAM::Role',
        Properties: {
          RoleName: props.memberRoleName,
          Description: `Read-only AWS Support access for Support Case Insights (${props.uniqueId})`,
          MaxSessionDuration: 3600,
          AssumeRolePolicyDocument: {
            Version: '2012-10-17',
            Statement: [
              {
                Sid: 'TrustCaseInsightsCollectors',
                Effect: 'Allow',
                Principal: {
                  AWS: trust.roleNames.map((name) => ({
                    'Fn::Sub': `arn:\${AWS::Partition}:iam::${trust.account}:role/${name}`,
                  })),
                },
                Action: 'sts:AssumeRole',
                Condition: { StringEquals: { 'aws:PrincipalOrgID': props.orgId } },
              },
            ],
          },
          Policies: [
            {
              PolicyName: 'SupportCaseReadOnly',
              PolicyDocument: {
                Version: '2012-10-17',
                Statement: [
                  {
                    Sid: 'ReadSupportCases',
                    Effect: 'Allow',
                    Action: [...MEMBER_ROLE_SUPPORT_ACTIONS],
                    // AWS Support actions do not support resource-level permissions.
                    Resource: '*',
                  },
                ],
              },
            },
          ],
          Tags: [
            { Key: 'Project', Value: 'SupportCaseInsights' },
            { Key: 'UniqueId', Value: props.uniqueId },
          ],
        },
      },
    },
    Outputs: {
      RoleArn: { Value: { 'Fn::GetAtt': ['SupportCaseAnalysisRole', 'Arn'] } },
    },
  };
}

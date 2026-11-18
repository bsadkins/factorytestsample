import { Template } from 'aws-cdk-lib/assertions';
import {
  addCaseInsightsStacks,
  type CaseInsightsAppStacks,
  MEMBER_ROLE_SUPPORT_ACTIONS,
  memberRoleTemplate,
  resourceNames,
} from '../../../lib';
import { normaliseAssetHashes } from '../../helpers/assets';
import { asArray, iamPolicyStatements, testApp } from '../../helpers/cdk';

interface MemberTemplate {
  Resources: Record<string, { Type: string; Properties: Record<string, unknown> }>;
}

interface StackSetProperties {
  PermissionModel: string;
  CallAs: string;
  AutoDeployment: Record<string, unknown>;
  Capabilities: string[];
  StackSetName: string;
  StackInstancesGroup: { DeploymentTargets: Record<string, unknown>; Regions: string[] }[];
  TemplateBody: string;
}

function stackSetProperties(stacks: CaseInsightsAppStacks): StackSetProperties {
  const stackSets = Template.fromStack(stacks.memberRole).findResources('AWS::CloudFormation::StackSet');
  const resources = Object.values(stackSets) as { Properties: StackSetProperties }[];
  expect(resources).toHaveLength(1);
  return (resources[0] as { Properties: StackSetProperties }).Properties;
}

describe.each(['dev', 'prod'] as const)('%s member-role StackSet', (env) => {
  let stacks: CaseInsightsAppStacks;
  let properties: StackSetProperties;
  let member: MemberTemplate;

  beforeAll(() => {
    stacks = addCaseInsightsStacks(testApp(), { env });
    properties = stackSetProperties(stacks);
    member = JSON.parse(properties.TemplateBody) as MemberTemplate;
  });

  test('is service-managed, auto-deploys to new accounts and targets only the configured root/OUs in one Region', () => {
    const { config } = stacks;
    expect(properties).toMatchObject({
      PermissionModel: 'SERVICE_MANAGED',
      CallAs: 'SELF',
      AutoDeployment: { Enabled: true, RetainStacksOnAccountRemoval: false },
      Capabilities: ['CAPABILITY_NAMED_IAM'],
      StackSetName: resourceNames(config).memberRoleStackSet,
    });
    expect(properties.StackInstancesGroup).toEqual([
      { DeploymentTargets: { OrganizationalUnitIds: [...config.memberRoleTargets] }, Regions: [config.region] },
    ]);
    // No account lists or account filters: the management account is never targeted, and no
    // member account needs a manual deployment.
    expect(Object.keys(properties.StackInstancesGroup[0]?.DeploymentTargets ?? {})).toEqual(['OrganizationalUnitIds']);
    expect(stacks.memberRole.account).toBe(config.managementAccountId);
    expect(stacks.memberRole.region).toBe(config.region);
  });

  test('deploys exactly one uniquely named read-only role with only the two Describe actions', () => {
    const resources = Object.values(member.Resources);
    expect(resources).toHaveLength(1);
    const role = resources[0];
    expect(role?.Type).toBe('AWS::IAM::Role');
    expect(role?.Properties.RoleName).toBe(`Support-Case-Analysis-Role-${stacks.config.uniqueId}`);
    expect(role?.Properties.ManagedPolicyArns).toBeUndefined();
    expect(role?.Properties.PermissionsBoundary).toBeUndefined();
    expect(role?.Properties.Policies).toEqual([
      {
        PolicyName: 'SupportCaseReadOnly',
        PolicyDocument: {
          Version: '2012-10-17',
          Statement: [
            { Sid: 'ReadSupportCases', Effect: 'Allow', Action: ['support:DescribeCases', 'support:DescribeCommunications'], Resource: '*' },
          ],
        },
      },
    ]);
    expect(MEMBER_ROLE_SUPPORT_ACTIONS).toEqual(['support:DescribeCases', 'support:DescribeCommunications']);
  });

  test('trusts only the exact retrieval and annotation roles, and only from the configured organization', () => {
    const { config } = stacks;
    const names = resourceNames(config);
    const trust = member.Resources.SupportCaseAnalysisRole?.Properties.AssumeRolePolicyDocument;
    expect(trust).toEqual({
      Version: '2012-10-17',
      Statement: [
        {
          Sid: 'TrustCaseInsightsCollectors',
          Effect: 'Allow',
          Principal: {
            AWS: [
              { 'Fn::Sub': `arn:\${AWS::Partition}:iam::${config.managementAccountId}:role/${names.caseRetrievalRole}` },
              { 'Fn::Sub': `arn:\${AWS::Partition}:iam::${config.managementAccountId}:role/${names.caseAnnotationRole}` },
            ],
          },
          Action: 'sts:AssumeRole',
          Condition: { StringEquals: { 'aws:PrincipalOrgID': config.orgId } },
        },
      ],
    });

    // The trusted names are the actual execution roles created by the core stack.
    const coreRoleNames = Object.values(Template.fromStack(stacks.core).findResources('AWS::IAM::Role')).map(
      (role) => (role as { Properties: { RoleName?: string } }).Properties.RoleName,
    );
    expect(coreRoleNames).toEqual(expect.arrayContaining([names.caseRetrievalRole, names.caseAnnotationRole]));
    expect(stacks.core.memberAccess.trust).toEqual({
      account: config.managementAccountId,
      roleNames: [names.caseRetrievalRole, names.caseAnnotationRole],
    });
  });

  test('is deployed after the core stack whose roles it trusts, without cross-stack exports', () => {
    expect(stacks.memberRole.dependencies).toContain(stacks.core);
    const coreOutputs = Template.fromStack(stacks.core).toJSON() as { Outputs?: Record<string, { Export?: unknown }> };
    expect(Object.values(coreOutputs.Outputs ?? {}).filter((output) => output.Export !== undefined)).toEqual([]);
  });

  test('core AssumeRole grants target only the configured member-role name inside the organization', () => {
    const { config } = stacks;
    const names = resourceNames(config);
    const template = Template.fromStack(stacks.core);
    const statements = iamPolicyStatements(template);
    const assume = statements.filter(({ statement }) => asArray(statement.Action).some((action) => action.startsWith('sts:')));
    expect(assume).toHaveLength(2);
    const roleIds = new Set(
      Object.entries(template.findResources('AWS::IAM::Role'))
        .filter(([, role]) =>
          [names.caseRetrievalRole, names.caseAnnotationRole].includes((role as { Properties: { RoleName?: string } }).Properties.RoleName ?? ''),
        )
        .map(([logicalId]) => logicalId),
    );
    for (const { statement, roles } of assume) {
      expect(statement).toEqual({
        Sid: 'AssumeMemberSupportRole',
        Effect: 'Allow',
        Action: 'sts:AssumeRole',
        Resource: `arn:aws:iam::*:role/${names.memberRole}`,
        Condition: { StringEquals: { 'aws:ResourceOrgID': config.orgId } },
      });
      expect(roles).toHaveLength(1);
      expect(roleIds.has((roles[0] as { Ref: string }).Ref)).toBe(true);
    }
  });

  test('no management-account role has Support permissions', () => {
    for (const { statement } of iamPolicyStatements(Template.fromStack(stacks.core))) {
      expect(asArray(statement.Action).filter((action) => action.toLowerCase().startsWith('support:'))).toEqual([]);
    }
    for (const role of Object.values(Template.fromStack(stacks.core).findResources('AWS::IAM::Role'))) {
      expect(JSON.stringify(role)).not.toMatch(/support:|AWSSupport/i);
    }
  });

  test('template snapshot', () => {
    expect(normaliseAssetHashes(Template.fromStack(stacks.memberRole).toJSON())).toMatchSnapshot('StackSet stack');
    expect(member).toMatchSnapshot('member-account template');
  });
});

describe('memberRoleTemplate validation', () => {
  const valid = {
    uniqueId: 'scitest',
    memberRoleName: 'Support-Case-Analysis-Role-scitest',
    orgId: 'o-exampleorg1',
    trust: { account: '111111111111', roleNames: ['CaseInsights-CaseRetrieval-scitest'] },
  };

  test('rejects an invalid trusted account, an empty trust list and invalid role names', () => {
    expect(() => memberRoleTemplate({ ...valid, trust: { ...valid.trust, account: '*' } })).toThrow(/12-digit/);
    expect(() => memberRoleTemplate({ ...valid, trust: { ...valid.trust, roleNames: [] } })).toThrow(/at least one/);
    expect(() => memberRoleTemplate({ ...valid, trust: { ...valid.trust, roleNames: ['role/*'] } })).toThrow(/Invalid IAM role name/);
    expect(() => memberRoleTemplate(valid)).not.toThrow();
  });
});

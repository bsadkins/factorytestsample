import * as fs from 'node:fs';
import * as path from 'node:path';
import { aws_s3 as s3 } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import {
  addCaseInsightsStacks,
  CaseInsightsCoreStack,
  CaseInsightsMemberRoleStack,
  CONFIG_DIR,
  ConfigValidationError,
} from '../lib';
import { normaliseAssetHashes } from './helpers/assets';
import { acknowledgedRules, synthError, tempOutdir, testApp } from './helpers/cdk';

describe('Support Case Insights app', () => {
  test('dev configuration adds the namespaced core and member-role stacks in the management account', () => {
    const app = testApp();
    const { config, core, memberRole } = addCaseInsightsStacks(app, { env: 'dev' });

    expect(core).toBeInstanceOf(CaseInsightsCoreStack);
    expect(core.stackName).toBe(`CaseInsightsCoreStack-${config.uniqueId}`);
    expect(core.account).toBe(config.managementAccountId);
    expect(core.region).toBe(config.region);
    expect(core.config).toBe(config);
    expect(memberRole).toBeInstanceOf(CaseInsightsMemberRoleStack);
    expect(memberRole.stackName).toBe(`CaseInsightsMemberRoleStack-${config.uniqueId}`);
    expect(memberRole.account).toBe(config.managementAccountId);
    expect(app.node.children.filter((child) => child instanceof CaseInsightsCoreStack)).toHaveLength(1);
    expect(app.node.children.filter((child) => child instanceof CaseInsightsMemberRoleStack)).toHaveLength(1);
  });

  test.each(['dev', 'prod'] as const)('%s configuration synthesises with validation plugins passing', (env) => {
    const app = testApp();
    addCaseInsightsStacks(app, { env });
    const assembly = app.synth();
    const report = JSON.parse(fs.readFileSync(path.join(assembly.directory, 'validation-report.json'), 'utf8')) as {
      pluginReports: { pluginName: string; conclusion: string }[];
    };
    expect(report.pluginReports.every((plugin) => plugin.conclusion === 'success')).toBe(true);
  });

  test.each(['dev', 'prod'] as const)('%s core stack template snapshot', (env) => {
    const { core } = addCaseInsightsStacks(testApp(), { env });
    expect(normaliseAssetHashes(Template.fromStack(core).toJSON())).toMatchSnapshot();
  });

  test('every cdk-nag acknowledgement is listed here with a specific reason', () => {
    const app = testApp();
    const { config, core: coreStack } = addCaseInsightsStacks(app, { env: 'dev' });
    const acknowledged = acknowledgedRules(app).map(({ path: constructPath, id, reason }) => ({
      path: constructPath,
      id: id.replace(/^.*::(?=AwsSolutions-)/, ''),
      reason,
    }));
    const core = `CaseInsightsCoreStack-${config.uniqueId}`;
    const memberRoleFinding = `AwsSolutions-IAM5[Resource::arn:aws:iam::*:role/Support-Case-Analysis-Role-${config.uniqueId}]`;
    const accountListsFinding = `AwsSolutions-IAM5[Resource::<${coreStack.getLogicalId(coreStack.storage.accountListBucket.node.defaultChild as never)}.Arn>/account-lists/*]`;
    const lookupVersionsFinding = `AwsSolutions-IAM5[Resource::<${coreStack.getLogicalId(coreStack.accountDiscovery.lookupFunction.function.node.defaultChild as never)}.Arn>:*]`;
    const rawObjectsFinding = `AwsSolutions-IAM5[Resource::<${coreStack.getLogicalId(coreStack.storage.rawBucket.node.defaultChild as never)}.Arn>/account_number=*]`;
    const idOf = (construct: { node: { defaultChild?: unknown } }): string => coreStack.getLogicalId(construct.node.defaultChild as never);
    const rawFinding = (pattern: string): string => `AwsSolutions-IAM5[Resource::<${idOf(coreStack.storage.rawBucket)}.Arn>/${pattern}]`;
    const processedFinding = `AwsSolutions-IAM5[Resource::<${idOf(coreStack.storage.processedBucket)}.Arn>/account_number=*]`;
    const executionsFinding = `AwsSolutions-IAM5[Resource::arn:aws:states:${config.region}:${config.managementAccountId}:execution:CaseAnalysis-${config.uniqueId}:*]`;
    const analysis = coreStack.caseAnalysis;
    const versionsFinding = (fn: { function: { node: { defaultChild?: unknown } } }): string => `AwsSolutions-IAM5[Resource::<${idOf(fn.function)}.Arn>:*]`;
    const analysisFunctionAcks = ['AnalysisStages', 'UpdateCaseMetadata', 'HandleFailure', 'WorkflowStarter', 'CaseCleanup'].flatMap((id) => [
      [`${core}/CaseAnalysis/${id}/TelemetryPolicy`, 'AwsSolutions-IAM5[Resource::*]'],
      [`${core}/CaseAnalysis/${id}/Function`, 'AwsSolutions-L1'],
    ]);
    const notificationsHandler = coreStack.node.children.find((child) => child.node.id.startsWith('BucketNotificationsHandler'));
    expect(acknowledged.map(({ path: constructPath, id }) => [constructPath, id]).sort()).toEqual(
      [
        [`${core}/MemberAccess/CaseAnnotationRoleMemberRoleAccess`, memberRoleFinding],
        [`${core}/MemberAccess/CaseRetrievalRoleMemberRoleAccess`, memberRoleFinding],
        [`${core}/Queues/WorkflowFailures`, 'AwsSolutions-SQS3'],
        [`${core}/Queues/AccountDiscoveryFailures`, 'AwsSolutions-SQS3'],
        [`${core}/AccountDiscovery/AccountLookup/TelemetryPolicy`, 'AwsSolutions-IAM5[Resource::*]'],
        [`${core}/AccountDiscovery/AccountLookup/Function`, 'AwsSolutions-L1'],
        [`${core}/AccountDiscovery/AccountReader/TelemetryPolicy`, 'AwsSolutions-IAM5[Resource::*]'],
        [`${core}/AccountDiscovery/AccountReader/Function`, 'AwsSolutions-L1'],
        [`${core}/AccountDiscovery/AccountLookupAccess`, 'AwsSolutions-IAM5[Resource::*]'],
        [`${core}/AccountDiscovery/AccountLookupAccess`, accountListsFinding],
        [`${core}/AccountDiscovery/AccountReaderAccess`, accountListsFinding],
        [`${core}/AccountDiscovery/AccountLookupScheduleRole`, lookupVersionsFinding],
        [`${core}/CaseCollection/CaseRetrieval/TelemetryPolicy`, 'AwsSolutions-IAM5[Resource::*]'],
        [`${core}/CaseCollection/CaseRetrieval/Function`, 'AwsSolutions-L1'],
        [`${core}/CaseCollection/CaseRetrievalAccess`, rawObjectsFinding],
        [`${core}/CaseCollection/CaseRetrievalAccess`, rawFinding('runs/*/accounts/*')],
        [`${core}/CaseCollection/CaseAnnotation/TelemetryPolicy`, 'AwsSolutions-IAM5[Resource::*]'],
        [`${core}/CaseCollection/CaseAnnotation/Function`, 'AwsSolutions-L1'],
        [`${core}/CaseCollection/CaseAnnotationAccess`, rawObjectsFinding],
        [`${core}/CaseCollection/CaseAnnotationAccess`, rawFinding('runs/*/cases/*')],
        [`${core}/CaseCollection/CaseAnnotationAccess`, rawFinding('account_number=*/case_number=*/terminal-failure.json')],
        ...analysisFunctionAcks,
        [`${core}/CaseAnalysis/AnalysisStagesAccess`, rawObjectsFinding],
        [`${core}/CaseAnalysis/AnalysisStagesAccess`, rawFinding('account_number=*/case_number=*/checkpoints/*')],
        [`${core}/CaseAnalysis/AnalysisStagesAccess`, processedFinding],
        [`${core}/CaseAnalysis/AnalysisStagesBedrock/Policy`, 'AwsSolutions-IAM5[Resource::*]'],
        [`${core}/CaseAnalysis/UpdateCaseMetadataAccess`, rawObjectsFinding],
        [`${core}/CaseAnalysis/UpdateCaseMetadataAccess`, processedFinding],
        [`${core}/CaseAnalysis/WorkflowStarterAccess`, rawFinding('account_number=*/case_number=*/workflow.json')],
        [`${core}/CaseAnalysis/WorkflowStarterAccess`, rawFinding('account_number=*/case_number=*/failure-deliveries/*')],
        [`${core}/CaseAnalysis/WorkflowStarterAccess`, rawFinding('runs/*/cases/*')],
        [`${core}/CaseAnalysis/HandleFailureAccess`, rawFinding('account_number=*/case_number=*/failure-deliveries/*')],
        [`${core}/CaseAnalysis/WorkflowStarterAccess`, processedFinding],
        [`${core}/CaseAnalysis/WorkflowStarterAccess`, executionsFinding],
        [`${core}/CaseAnalysis/CaseCleanupAccess`, rawObjectsFinding],
        [`${core}/CaseAnalysis/CaseCleanupAccess`, rawFinding('account_number=*/case_number=*/data.json')],
        [`${core}/CaseAnalysis/CaseCleanupAccess`, rawFinding('account_number=*/case_number=*/communications.json')],
        [`${core}/CaseAnalysis/CaseCleanupAccess`, rawFinding('account_number=*/case_number=*/workflow.json')],
        [`${core}/CaseAnalysis/CaseCleanupAccess`, rawFinding('cleanup-state/*')],
        [`${core}/CaseAnalysis/CaseCleanupAccess`, rawFinding('account_number=*/case_number=*/failure-deliveries/*')],
        [`${core}/CaseAnalysis/CaseCleanupAccess`, processedFinding],
        [`${core}/CaseAnalysis/CaseCleanupAccess`, executionsFinding],
        [`${core}/CaseAnalysis/StateMachine/Role`, 'AwsSolutions-IAM5[Resource::*]'],
        [`${core}/CaseAnalysis/StateMachine/Role`, versionsFinding(analysis.stagesFunction)],
        [`${core}/CaseAnalysis/StateMachine/Role`, versionsFinding(analysis.updateFunction)],
        [`${core}/CaseAnalysis/StateMachine/Role`, versionsFinding(analysis.failureFunction)],
        [`${core}/CaseAnalysis/CaseCleanupScheduleRole`, versionsFinding(analysis.cleanupFunction)],
        [`${core}/Observability/AlarmTopic`, 'AwsSolutions-SNS2'],
        [`${core}/Observability/RunCompletion/TelemetryPolicy`, 'AwsSolutions-IAM5[Resource::*]'],
        [`${core}/Observability/RunCompletion/Function`, 'AwsSolutions-L1'],
        [`${core}/Observability/RunCompletionAccess`, accountListsFinding],
        [`${core}/Observability/RunCompletionAccess`, rawFinding('runs/*')],
        [`${core}/Observability/RunCompletionAccess`, rawFinding('runs/*/completion.json')],
        [`${core}/Observability/RunCompletionAccess`, rawFinding('account_number=*/case_number=*/failure-deliveries/*')],
        [`${core}/Observability/RunCompletionAccess`, executionsFinding],
        [`${core}/Observability/RunCompletionScheduleRole`, versionsFinding(coreStack.observability.runCompletionFunction)],
        [
          `${notificationsHandler?.node.path ?? 'missing'}/Role`,
          'AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]',
        ],
      ].sort(),
    );
    for (const { reason } of acknowledged) {
      expect(reason.length).toBeGreaterThanOrEqual(40);
    }
  });

  test('cdk-nag findings fail synthesis', () => {
    const app = testApp();
    const { core } = addCaseInsightsStacks(app, { env: 'dev' });
    new s3.Bucket(core, 'InsecureBucket', { enforceSSL: false });
    const error = synthError(app);
    expect(error).toMatch(/AwsSolutions-S1'/);
    expect(error).toMatch(/AwsSolutions-S10'/);
  });

  test('invalid configuration stops before any stack is created', () => {
    const configDir = tempOutdir();
    const prod = JSON.parse(fs.readFileSync(path.join(CONFIG_DIR, 'prod.json'), 'utf8')) as Record<string, unknown>;
    delete prod.alarmEmail;
    fs.writeFileSync(path.join(configDir, 'prod.json'), JSON.stringify(prod));

    const app = testApp();
    expect(() => addCaseInsightsStacks(app, { env: 'prod', configDir })).toThrow(ConfigValidationError);
    expect(app.node.children).toHaveLength(0);
  });
});

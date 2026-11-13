import * as fs from 'node:fs';
import * as path from 'node:path';
import { Match, Template } from 'aws-cdk-lib/assertions';
import {
  ANALYSIS_FAILED_ERROR,
  ANALYSIS_STAGE_PACKAGES,
  addCaseInsightsStacks,
  CaseInsightsCoreStack,
  CLEANUP_RESETTABLE_FILES,
  CLEANUP_SCHEDULE_RETRY_ATTEMPTS,
  CLEANUP_STATE_PREFIX,
  REPO_ROOT,
  TERMINAL_MARKER_PATTERN,
  queueVisibilityTimeoutSeconds,
  resourceNames,
  STARTER_BATCH_SIZE,
} from '../../lib';
import { acknowledgedRules, asArray, iamPolicyStatements, nagApp, synthError, testApp, testConfig } from '../helpers/cdk';

function coreStack(overrides: Parameters<typeof testConfig>[0] = {}): CaseInsightsCoreStack {
  const config = testConfig(overrides);
  return new CaseInsightsCoreStack(testApp(), 'Core', { config, env: { account: config.managementAccountId, region: config.region } });
}

function coreTemplate(overrides: Parameters<typeof testConfig>[0] = {}): { core: CaseInsightsCoreStack; template: Template } {
  const core = coreStack(overrides);
  return { core, template: Template.fromStack(core) };
}

function logicalId(core: CaseInsightsCoreStack, construct: { node: { defaultChild?: unknown } }): string {
  return core.getLogicalId(construct.node.defaultChild as never);
}

const arnOf = (id: string): unknown => ({ 'Fn::GetAtt': [id, 'Arn'] });

interface RetrierJson {
  ErrorEquals: string[];
  MaxAttempts: number;
  IntervalSeconds: number;
  BackoffRate: number;
  MaxDelaySeconds?: number;
  JitterStrategy?: string;
}

interface StateJson {
  Type: string;
  Next?: string;
  End?: boolean;
  Resource?: string;
  Parameters?: unknown;
  ResultPath?: string;
  Retry?: RetrierJson[];
  Catch?: { ErrorEquals: string[]; Next: string; ResultPath?: string }[];
  Error?: string;
}

interface DefinitionJson {
  StartAt: string;
  TimeoutSeconds: number;
  States: Record<string, StateJson>;
}

/** The state machine definition with each function ARN replaced by `<logicalId>`. */
function definition(template: Template): DefinitionJson {
  const machines = Object.values(template.findResources('AWS::StepFunctions::StateMachine'));
  expect(machines).toHaveLength(1);
  const parts = (machines[0]?.Properties as { DefinitionString: { 'Fn::Join': [string, unknown[]] } }).DefinitionString['Fn::Join'][1];
  const text = parts.map((part) => (typeof part === 'string' ? part : `<${(part as { 'Fn::GetAtt': string[] })['Fn::GetAtt'][0] ?? ''}>`)).join('');
  return JSON.parse(text) as DefinitionJson;
}

function retrierFor(state: StateJson | undefined, error: string): RetrierJson | undefined {
  return state?.Retry?.find((retrier) => retrier.ErrorEquals.includes(error));
}

describe('CaseAnalysis', () => {
  test('a Standard workflow runs Summary, RCA, Lifecycle and UpdateCaseMetadata in order', () => {
    const { core, template } = coreTemplate({ operations: { workflow: { executionTimeoutMinutes: 90 } } });
    const names = resourceNames(core.config);
    template.hasResourceProperties('AWS::StepFunctions::StateMachine', {
      StateMachineName: names.caseAnalysisStateMachine,
      StateMachineType: 'STANDARD',
      TracingConfiguration: { Enabled: true },
      LoggingConfiguration: { IncludeExecutionData: false, Level: 'ALL' },
    });
    const def = definition(template);
    const stages = `<${logicalId(core, core.caseAnalysis.stagesFunction.function)}>`;
    const update = `<${logicalId(core, core.caseAnalysis.updateFunction.function)}>`;
    expect(def.StartAt).toBe('Summary');
    expect(def.TimeoutSeconds).toBe(90 * 60);
    expect(def.States.Summary).toMatchObject({ Type: 'Task', Next: 'RCA', Resource: stages, ResultPath: '$.summary' });
    expect(def.States.RCA).toMatchObject({ Next: 'Lifecycle', Resource: stages, ResultPath: '$.rca' });
    expect(def.States.Lifecycle).toMatchObject({ Next: 'UpdateCaseMetadata', Resource: stages, ResultPath: '$.lifecycle' });
    expect(def.States.UpdateCaseMetadata).toMatchObject({ End: true, Resource: update, ResultPath: '$.update' });
    for (const stage of ['summary', 'rca', 'lifecycle']) {
      const state = def.States[stage === 'rca' ? 'RCA' : stage.charAt(0).toUpperCase() + stage.slice(1)];
      expect(state?.Parameters).toEqual({ stage, 'case.$': '$.case' });
    }
    expect(def.States.UpdateCaseMetadata?.Parameters).toEqual({ 'case.$': '$.case' });
    expect(Object.keys(def.States).sort()).toEqual(['AnalysisFailed', 'HandleFailure', 'Lifecycle', 'RCA', 'Summary', 'UpdateCaseMetadata']);
  });

  test('every task has bounded retries and catches every error into HandleFailure, which then fails the execution', () => {
    const { core, template } = coreTemplate({ operations: { workflow: { taskMaxAttempts: 4 } } });
    const def = definition(template);
    for (const name of ['Summary', 'RCA', 'Lifecycle', 'UpdateCaseMetadata']) {
      const state = def.States[name];
      expect(state?.Catch).toEqual([{ ErrorEquals: ['States.ALL'], Next: 'HandleFailure', ResultPath: '$.error' }]);
      for (const retrier of state?.Retry ?? []) {
        expect(retrier.MaxAttempts).toBeGreaterThan(0);
        expect(retrier.MaxAttempts).toBeLessThanOrEqual(8);
        expect(retrier.BackoffRate).toBeGreaterThanOrEqual(2);
        expect(retrier.ErrorEquals).not.toContain('States.ALL');
      }
      expect(retrierFor(state, 'Lambda.TooManyRequestsException')).toMatchObject({ MaxAttempts: 8, BackoffRate: 2, MaxDelaySeconds: 300, JitterStrategy: 'FULL' });
      expect(retrierFor(state, 'ClientError')).toMatchObject({ MaxAttempts: 4, JitterStrategy: 'FULL' });
      expect(retrierFor(state, 'Sandbox.Timedout')).toBeDefined();
    }
    for (const name of ['Summary', 'RCA', 'Lifecycle']) {
      const bedrock = retrierFor(def.States[name], 'BedrockThrottlingError');
      expect(bedrock).toMatchObject({ ErrorEquals: ['BedrockThrottlingError', 'BedrockTransientError'], MaxAttempts: 4, IntervalSeconds: 15, BackoffRate: 2 });
    }
    expect(retrierFor(def.States.UpdateCaseMetadata, 'BedrockThrottlingError')).toBeUndefined();

    const handleFailure = def.States.HandleFailure;
    expect(handleFailure).toMatchObject({ Type: 'Task', Next: 'AnalysisFailed', ResultPath: '$.failureRecord' });
    expect(handleFailure?.Resource).toBe(`<${logicalId(core, core.caseAnalysis.failureFunction.function)}>`);
    // The handler needs the caught error and case reference, so it receives the whole state.
    expect(handleFailure?.Parameters).toBeUndefined();
    expect(handleFailure?.Catch).toBeUndefined();
    expect(retrierFor(handleFailure, 'FailureDeliveryError')).toMatchObject({ MaxAttempts: 6, BackoffRate: 2 });
    expect(def.States.AnalysisFailed).toMatchObject({ Type: 'Fail', Error: ANALYSIS_FAILED_ERROR });
  });

  test.each([1, 2, 5])('aggregate Bedrock concurrency equals the configured cap (%i) through one stage function', (cap) => {
    const { core, template } = coreTemplate({ bedrockMaxConcurrency: cap });
    const names = resourceNames(core.config);
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: names.caseAnalysisStagesFunction,
      Handler: 'analysis_stages.app.handler',
      ReservedConcurrentExecutions: cap,
      Timeout: core.config.operations.functions.analysisTimeoutSeconds,
      Layers: [{ Ref: logicalId(core, core.layers.commonLayer) }, { Ref: logicalId(core, core.layers.templatesLayer) }],
      Environment: { Variables: Match.objectLike({ BEDROCK_MODEL_ID: core.config.bedrockModelId, POWERTOOLS_SERVICE_NAME: 'case-analysis-stages' }) },
    });
    // No other function reserves concurrency, so the cap is not multiplied per stage.
    const reserved = Object.values(template.findResources('AWS::Lambda::Function')).filter(
      (fn) => (fn.Properties as { ReservedConcurrentExecutions?: number }).ReservedConcurrentExecutions !== undefined,
    );
    expect(reserved).toHaveLength(1);
    const bedrockRoles = iamPolicyStatements(template)
      .filter(({ statement }) => statement.Effect === 'Allow' && asArray(statement.Action).some((action) => action.startsWith('bedrock:')))
      .map(({ roles }) => JSON.stringify(roles));
    expect([...new Set(bedrockRoles)]).toEqual([JSON.stringify([{ Ref: logicalId(core, core.caseAnalysis.stagesFunction.role) }])]);
  });

  test('the stage function packages its dispatcher and the three stage handlers as top-level packages', () => {
    const app = testApp();
    const { core } = addCaseInsightsStacks(app, { env: 'dev' });
    const assembly = app.synth();
    const template = Template.fromStack(core);
    const fn = template.findResources('AWS::Lambda::Function', { Properties: { Handler: 'analysis_stages.app.handler' } });
    const key = (Object.values(fn)[0]?.Properties as { Code: { S3Key: string } }).Code.S3Key;
    const assetDir = path.join(assembly.directory, `asset.${key.replace(/\.zip$/, '')}`);
    for (const pkg of ANALYSIS_STAGE_PACKAGES) {
      expect(fs.existsSync(path.join(assetDir, pkg, 'app.py'))).toBe(true);
    }
    expect(fs.readdirSync(assetDir).sort()).toEqual([...ANALYSIS_STAGE_PACKAGES].sort());
  });

  test('the starter consumes the summary queue with partial batches; its concurrency is a start-rate control', () => {
    const { core, template } = coreTemplate({ operations: { workflow: { starterMaxConcurrency: 3 }, functions: { consumerTimeoutSeconds: 120 } } });
    const names = resourceNames(core.config);
    const starter = core.caseAnalysis.starterFunction;
    template.hasResourceProperties('AWS::Lambda::EventSourceMapping', {
      FunctionName: { Ref: logicalId(core, starter.function) },
      EventSourceArn: arnOf(logicalId(core, core.queues.summary.queue)),
      BatchSize: STARTER_BATCH_SIZE,
      FunctionResponseTypes: ['ReportBatchItemFailures'],
      ScalingConfig: { MaximumConcurrency: 3 },
    });
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: names.workflowStarterFunction,
      Timeout: 120,
      Environment: {
        Variables: Match.objectLike({
          STATE_MACHINE_ARN: `arn:aws:states:${core.config.region}:${core.config.managementAccountId}:stateMachine:${names.caseAnalysisStateMachine}`,
          CLAIM_SETTLE_SECONDS: '120',
          IDEMPOTENCY_TABLE_NAME: { Ref: logicalId(core, core.storage.idempotencyTable) },
          POWERTOOLS_SERVICE_NAME: 'workflow-starter',
        }),
      },
    });
    const visibility = queueVisibilityTimeoutSeconds(core.config);
    expect(visibility).toBeGreaterThanOrEqual(6 * 120);
    template.hasResourceProperties('AWS::SQS::Queue', {
      QueueName: names.summaryQueue,
      VisibilityTimeout: visibility,
      RedrivePolicy: { deadLetterTargetArn: arnOf(logicalId(core, core.queues.summary.deadLetterQueue)) },
    });
  });

  test('daily cleanup runs on the configured schedule through a role only that schedule can assume', () => {
    const { core, template } = coreTemplate({ schedules: { cleanup: 'cron(15 21 * * ? *)' }, operations: { cleanup: { graceHours: 36 } } });
    const names = resourceNames(core.config);
    const cleanupArn = arnOf(logicalId(core, core.caseAnalysis.cleanupFunction.function));
    template.hasResourceProperties('AWS::Scheduler::Schedule', {
      Name: names.caseCleanupSchedule,
      ScheduleExpression: 'cron(15 21 * * ? *)',
      ScheduleExpressionTimezone: 'Etc/UTC',
      State: 'ENABLED',
      Target: {
        Arn: cleanupArn,
        RetryPolicy: { MaximumRetryAttempts: CLEANUP_SCHEDULE_RETRY_ATTEMPTS, MaximumEventAgeInSeconds: 21600 },
      },
    });
    template.hasResourceProperties('AWS::IAM::Role', {
      AssumeRolePolicyDocument: {
        Statement: [
          Match.objectLike({
            Principal: { Service: 'scheduler.amazonaws.com' },
            Condition: {
              StringEquals: { 'aws:SourceAccount': core.config.managementAccountId },
              ArnLike: {
                'aws:SourceArn': `arn:aws:scheduler:${core.config.region}:${core.config.managementAccountId}:schedule/default/${names.caseCleanupSchedule}`,
              },
            },
          }),
        ],
      },
    });
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: names.caseCleanupFunction,
      Environment: { Variables: Match.objectLike({ CLEANUP_GRACE_HOURS: '36', POWERTOOLS_SERVICE_NAME: 'case-cleanup' }) },
    });
  });

  test('each workflow role holds only the access its function uses', () => {
    const { core, template } = coreTemplate();
    const names = resourceNames(core.config);
    const { region, managementAccountId } = core.config;
    const stateMachineArn = `arn:aws:states:${region}:${managementAccountId}:stateMachine:${names.caseAnalysisStateMachine}`;
    const executionsArn = `arn:aws:states:${region}:${managementAccountId}:execution:${names.caseAnalysisStateMachine}:*`;
    const raw = (pattern: string): unknown => ({ 'Fn::Join': ['', [arnOf(logicalId(core, core.storage.rawBucket)), `/${pattern}`]] });
    const processed = { 'Fn::Join': ['', [arnOf(logicalId(core, core.storage.processedBucket)), '/account_number=*']] };
    const listBuckets = {
      Sid: 'ListCaseBuckets',
      Effect: 'Allow',
      Action: 's3:ListBucket',
      Resource: [arnOf(logicalId(core, core.storage.processedBucket)), arnOf(logicalId(core, core.storage.rawBucket))],
    };
    const checkProcessed = { Sid: 'CheckProcessedRecords', Effect: 'Allow', Action: 's3:GetObject', Resource: processed };
    const statementsFor = (roleId: string): unknown[] =>
      iamPolicyStatements(template)
        .filter(({ roles }) => JSON.stringify(roles) === JSON.stringify([{ Ref: roleId }]))
        .map(({ statement }) => statement)
        .filter((s) => !['WriteOwnLogGroup', 'WriteXRaySegments'].includes(s.Sid ?? ''));
    const { starterFunction, updateFunction, failureFunction, cleanupFunction } = core.caseAnalysis;

    expect(statementsFor(logicalId(core, starterFunction.role))).toEqual([
      {
        Effect: 'Allow',
        Action: ['sqs:ChangeMessageVisibility', 'sqs:DeleteMessage', 'sqs:GetQueueAttributes', 'sqs:GetQueueUrl', 'sqs:ReceiveMessage'],
        Resource: arnOf(logicalId(core, core.queues.summary.queue)),
      },
      { Sid: 'ClaimCaseOwnership', Effect: 'Allow', Action: ['s3:GetObject', 's3:PutObject'], Resource: raw('account_number=*/case_number=*/workflow.json') },
      checkProcessed,
      listBuckets,
      { Sid: 'StartCaseAnalysis', Effect: 'Allow', Action: 'states:StartExecution', Resource: stateMachineArn },
      { Sid: 'DescribeCaseAnalysis', Effect: 'Allow', Action: 'states:DescribeExecution', Resource: executionsArn },
      {
        Sid: 'IdempotencyRecords',
        Effect: 'Allow',
        Action: ['dynamodb:DeleteItem', 'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem'],
        Resource: arnOf(logicalId(core, core.storage.idempotencyTable)),
      },
    ]);
    expect(statementsFor(logicalId(core, updateFunction.role))).toEqual([
      { Sid: 'ReadRawCasesAndCheckpoints', Effect: 'Allow', Action: ['s3:GetObject', 's3:GetObjectVersion'], Resource: raw('account_number=*') },
      checkProcessed,
      { Sid: 'CommitProcessedRecords', Effect: 'Allow', Action: 's3:PutObject', Resource: processed },
      listBuckets,
    ]);
    expect(statementsFor(logicalId(core, failureFunction.role))).toEqual([
      { Sid: 'SendFailureRecords', Effect: 'Allow', Action: 'sqs:SendMessage', Resource: arnOf(logicalId(core, core.queues.workflowFailures)) },
    ]);
    expect(statementsFor(logicalId(core, cleanupFunction.role))).toEqual([
      { Sid: 'ReadCaseOwnership', Effect: 'Allow', Action: 's3:GetObject', Resource: raw('account_number=*') },
      {
        Sid: 'ResetAbandonedCases',
        Effect: 'Allow',
        Action: 's3:DeleteObject',
        Resource: ['communications.json', 'data.json', 'workflow.json'].map((file) => raw(`account_number=*/case_number=*/${file}`)),
      },
      { Sid: 'KeepCleanupCursor', Effect: 'Allow', Action: ['s3:DeleteObject', 's3:GetObject', 's3:PutObject'], Resource: raw('cleanup-state/*') },
      checkProcessed,
      listBuckets,
      { Sid: 'ListActiveExecutions', Effect: 'Allow', Action: 'states:ListExecutions', Resource: stateMachineArn },
      { Sid: 'DescribeCaseAnalysis', Effect: 'Allow', Action: 'states:DescribeExecution', Resource: executionsArn },
    ]);
    // Coordination uses S3 and Step Functions only: the idempotency table is the only DynamoDB table.
    template.resourceCountIs('AWS::DynamoDB::Table', 1);
    const deletes = iamPolicyStatements(template).filter(({ statement }) => asArray(statement.Action).includes('s3:DeleteObject'));
    expect(deletes.map(({ statement }) => statement.Sid).sort()).toEqual(['KeepCleanupCursor', 'ResetAbandonedCases']);
  });

  test('the state machine role invokes only the workflow functions', () => {
    const { core, template } = coreTemplate();
    const roleId = logicalId(core, core.caseAnalysis.stateMachine.role);
    const invoked = iamPolicyStatements(template)
      .filter(({ roles }) => JSON.stringify(roles) === JSON.stringify([{ Ref: roleId }]))
      .filter(({ statement }) => asArray(statement.Action).includes('lambda:InvokeFunction'))
      .flatMap(({ statement }) => asArray(statement.Resource as unknown[]));
    const { stagesFunction, updateFunction, failureFunction } = core.caseAnalysis;
    const expected = [stagesFunction, updateFunction, failureFunction].flatMap((fn) => {
      const arn = arnOf(logicalId(core, fn.function));
      return [arn, { 'Fn::Join': ['', [arn, ':*']] }];
    });
    expect(invoked).toEqual(expect.arrayContaining(expected));
    expect(invoked).toHaveLength(expected.length);
  });

  test('IAM object patterns match the file names the Python handlers use', () => {
    const python = (relative: string): string => fs.readFileSync(path.join(REPO_ROOT, 'src', 'lambda', relative), 'utf8');
    const constant = (source: string, name: string): string => new RegExp(`^${name}: Final = "([^"]+)"`, 'm').exec(source)?.[1] ?? 'missing';
    const cleanup = python('case_cleanup/app.py');
    const workflow = python('common/workflow.py');
    expect(constant(cleanup, 'CURSOR_KEY').startsWith(CLEANUP_STATE_PREFIX)).toBe(true);
    expect([constant(cleanup, 'RAW_FILE'), constant(cleanup, 'COMMUNICATIONS_FILE'), constant(workflow, 'MARKER_FILE')]).toEqual([...CLEANUP_RESETTABLE_FILES]);
    expect(cleanup).toMatch(/^RESETTABLE_FILES: Final = \(RAW_FILE, COMMUNICATIONS_FILE, MARKER_FILE\)$/m);
    expect(TERMINAL_MARKER_PATTERN.endsWith(`/${constant(python('common/failures.py'), 'TERMINAL_MARKER_FILE')}`)).toBe(true);
  });

  test('passes cdk-nag with specific acknowledgements only', () => {
    const app = nagApp();
    const { core } = addCaseInsightsStacks(app, { env: 'dev' });
    expect(synthError(app)).toBeUndefined();
    const names = resourceNames(core.config);
    const rawId = logicalId(core, core.storage.rawBucket);
    const processedId = logicalId(core, core.storage.processedBucket);
    const rawFinding = (pattern: string): string => `AwsSolutions-IAM5[Resource::<${rawId}.Arn>/${pattern}]`;
    const processedFinding = `AwsSolutions-IAM5[Resource::<${processedId}.Arn>/account_number=*]`;
    const executionsFinding = `AwsSolutions-IAM5[Resource::arn:aws:states:${core.config.region}:${core.config.managementAccountId}:execution:${names.caseAnalysisStateMachine}:*]`;
    const versionsFinding = (fn: { function: { node: { defaultChild?: unknown } } }): string =>
      `AwsSolutions-IAM5[Resource::<${logicalId(core, fn.function)}.Arn>:*]`;
    const { stagesFunction, updateFunction, failureFunction, cleanupFunction } = core.caseAnalysis;
    const functionAcks = ['AnalysisStages', 'UpdateCaseMetadata', 'HandleFailure', 'WorkflowStarter', 'CaseCleanup'].flatMap((id) => [
      [`CaseAnalysis/${id}/TelemetryPolicy`, 'AwsSolutions-IAM5[Resource::*]'],
      [`CaseAnalysis/${id}/Function`, 'AwsSolutions-L1'],
    ]);
    const acknowledged = acknowledgedRules(core.caseAnalysis).map(({ path: constructPath, id }) => [
      constructPath.replace(`${core.node.id}/`, ''),
      id.replace(/^.*::(?=AwsSolutions-)/, ''),
    ]);
    expect(acknowledged.sort()).toEqual(
      [
        ...functionAcks,
        ['CaseAnalysis/AnalysisStagesAccess', rawFinding('account_number=*')],
        ['CaseAnalysis/AnalysisStagesAccess', rawFinding('account_number=*/case_number=*/checkpoints/*')],
        ['CaseAnalysis/AnalysisStagesAccess', processedFinding],
        ['CaseAnalysis/AnalysisStagesBedrock/Policy', 'AwsSolutions-IAM5[Resource::*]'],
        ['CaseAnalysis/UpdateCaseMetadataAccess', rawFinding('account_number=*')],
        ['CaseAnalysis/UpdateCaseMetadataAccess', processedFinding],
        ['CaseAnalysis/WorkflowStarterAccess', rawFinding('account_number=*/case_number=*/workflow.json')],
        ['CaseAnalysis/WorkflowStarterAccess', processedFinding],
        ['CaseAnalysis/WorkflowStarterAccess', executionsFinding],
        ['CaseAnalysis/CaseCleanupAccess', rawFinding('account_number=*')],
        ['CaseAnalysis/CaseCleanupAccess', rawFinding('account_number=*/case_number=*/data.json')],
        ['CaseAnalysis/CaseCleanupAccess', rawFinding('account_number=*/case_number=*/communications.json')],
        ['CaseAnalysis/CaseCleanupAccess', rawFinding('account_number=*/case_number=*/workflow.json')],
        ['CaseAnalysis/CaseCleanupAccess', rawFinding('cleanup-state/*')],
        ['CaseAnalysis/CaseCleanupAccess', processedFinding],
        ['CaseAnalysis/CaseCleanupAccess', executionsFinding],
        ['CaseAnalysis/StateMachine/Role', 'AwsSolutions-IAM5[Resource::*]'],
        ['CaseAnalysis/StateMachine/Role', versionsFinding(stagesFunction)],
        ['CaseAnalysis/StateMachine/Role', versionsFinding(updateFunction)],
        ['CaseAnalysis/StateMachine/Role', versionsFinding(failureFunction)],
        ['CaseAnalysis/CaseCleanupScheduleRole', versionsFinding(cleanupFunction)],
      ].sort(),
    );
  });
});

import * as fs from 'node:fs';
import * as path from 'node:path';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { Metric } from 'aws-cdk-lib/aws-cloudwatch';
import {
  addCaseInsightsStacks,
  CaseInsightsCoreStack,
  HEARTBEAT_METRIC,
  HEARTBEAT_WINDOW_HOURS,
  METRICS_NAMESPACE,
  REPO_ROOT,
  RUN_COMPLETION_SERVICE,
  RUN_LOOKBACK_HOURS,
  RUN_MARKERS_PREFIX,
  resourceNames,
} from '../../lib';
import { acknowledgedRules, iamPolicyStatements, nagApp, synthError, testApp, testConfig } from '../helpers/cdk';

function coreTemplate(overrides: Parameters<typeof testConfig>[0] = {}): { core: CaseInsightsCoreStack; template: Template } {
  const config = testConfig(overrides);
  const core = new CaseInsightsCoreStack(testApp(), 'Core', { config, env: { account: config.managementAccountId, region: config.region } });
  return { core, template: Template.fromStack(core) };
}

function logicalId(core: CaseInsightsCoreStack, construct: { node: { defaultChild?: unknown } }): string {
  return core.getLogicalId(construct.node.defaultChild as never);
}

const arnOf = (id: string): unknown => ({ 'Fn::GetAtt': [id, 'Arn'] });

interface AlarmJson {
  AlarmName: string;
  AlarmDescription: string;
  AlarmActions: unknown[];
  MetricName?: string;
  Namespace?: string;
  Dimensions?: { Name: string; Value: unknown }[];
  Period?: number;
  Statistic?: string;
  Threshold: number;
  ComparisonOperator: string;
  EvaluationPeriods: number;
  DatapointsToAlarm: number;
  TreatMissingData: string;
  Metrics?: {
    Id: string;
    Expression?: string;
    MetricStat?: { Metric: { MetricName: string; Namespace: string; Dimensions: { Name: string; Value: unknown }[] }; Period: number; Stat: string };
  }[];
}

function alarms(template: Template): AlarmJson[] {
  return Object.values(template.findResources('AWS::CloudWatch::Alarm')).map((alarm) => alarm.Properties as AlarmJson);
}

function alarmNamed(template: Template, name: string): AlarmJson {
  const alarm = alarms(template).find((candidate) => candidate.AlarmName === name);
  if (alarm === undefined) {
    throw new Error(`alarm ${name} not found`);
  }
  return alarm;
}

/** The dashboard body with CloudFormation tokens rendered as JSON. */
function dashboardBody(template: Template): string {
  const dashboards = Object.values(template.findResources('AWS::CloudWatch::Dashboard'));
  expect(dashboards).toHaveLength(1);
  const body = (dashboards[0]?.Properties as { DashboardBody: { 'Fn::Join': [string, unknown[]] } }).DashboardBody['Fn::Join'][1];
  return body.map((part) => (typeof part === 'string' ? part : JSON.stringify(part))).join('');
}

/** Service metrics (service, name) used by an alarm, from its metric or math expression inputs. */
function serviceMetricsOf(alarm: AlarmJson): string[] {
  const single = alarm.MetricName !== undefined ? [{ MetricName: alarm.MetricName, Namespace: alarm.Namespace, Dimensions: alarm.Dimensions ?? [] }] : [];
  const fromMath = (alarm.Metrics ?? []).flatMap((m) => (m.MetricStat ? [m.MetricStat.Metric] : []));
  return [...single, ...fromMath]
    .filter((metric) => metric.Namespace === METRICS_NAMESPACE)
    .map((metric) => `${String(metric.Dimensions.find((d) => d.Name === 'service')?.Value)}/${metric.MetricName}`)
    .sort();
}

const PIPELINE_FUNCTIONS = [
  'AccountLookup',
  'AccountReader',
  'CaseRetrieval',
  'CaseAnnotation',
  'WorkflowStarter',
  'AnalysisStages',
  'UpdateCaseMetadata',
  'HandleFailure',
  'CaseCleanup',
  'RunCompletion',
];
const MONITORED_QUEUES = ['ActiveAccountsDLQ', 'AnnotationDLQ', 'SummaryDLQ', 'WorkflowFailures', 'AccountDiscoveryFailures'];

describe('Observability', () => {
  test('Run Completion runs hourly through a role only its schedule can assume', () => {
    const { core, template } = coreTemplate();
    const names = resourceNames(core.config);
    const fn = core.observability.runCompletionFunction;
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: names.runCompletionFunction,
      Runtime: 'python3.12',
      Handler: 'app.handler',
      TracingConfig: { Mode: 'Active' },
      LoggingConfig: Match.objectLike({ LogFormat: 'JSON' }),
      Layers: [{ Ref: logicalId(core, core.layers.commonLayer) }],
      Environment: {
        Variables: Match.objectLike({
          ACCOUNT_LIST_BUCKET: { Ref: logicalId(core, core.storage.accountListBucket) },
          RAW_BUCKET: { Ref: logicalId(core, core.storage.rawBucket) },
          STATE_MACHINE_ARN: { Ref: logicalId(core, core.caseAnalysis.stateMachine) },
          FAILURE_QUEUE_URL: { Ref: logicalId(core, core.queues.workflowFailures) },
          MANAGEMENT_ACCOUNT_ID: core.config.managementAccountId,
          RUN_LOOKBACK_HOURS: String(RUN_LOOKBACK_HOURS),
          POWERTOOLS_SERVICE_NAME: RUN_COMPLETION_SERVICE,
          POWERTOOLS_LOGGER_LOG_EVENT: 'false',
        }),
      },
    });
    expect(RUN_LOOKBACK_HOURS).toBeGreaterThanOrEqual(24);
    template.hasResourceProperties('AWS::Scheduler::Schedule', {
      Name: names.runCompletionSchedule,
      ScheduleExpression: 'rate(1 hour)',
      State: 'ENABLED',
      Target: Match.objectLike({ Arn: arnOf(logicalId(core, fn.function)) }),
    });
    template.hasResourceProperties('AWS::IAM::Role', {
      AssumeRolePolicyDocument: {
        Statement: [
          Match.objectLike({
            Principal: { Service: 'scheduler.amazonaws.com' },
            Condition: {
              StringEquals: { 'aws:SourceAccount': core.config.managementAccountId },
              ArnLike: {
                'aws:SourceArn': `arn:aws:scheduler:${core.config.region}:${core.config.managementAccountId}:schedule/default/${names.runCompletionSchedule}`,
              },
            },
          }),
        ],
      },
    });
  });

  test('Run Completion holds only the run evidence, status and failure-record access it uses', () => {
    const { core, template } = coreTemplate();
    const names = resourceNames(core.config);
    const raw = (pattern: string): unknown => ({ 'Fn::Join': ['', [arnOf(logicalId(core, core.storage.rawBucket)), `/${pattern}`]] });
    const accountListBucket = arnOf(logicalId(core, core.storage.accountListBucket));
    const roleId = logicalId(core, core.observability.runCompletionFunction.role);
    const statements = iamPolicyStatements(template)
      .filter(({ roles }) => JSON.stringify(roles) === JSON.stringify([{ Ref: roleId }]))
      .map(({ statement }) => statement)
      .filter((s) => !['WriteOwnLogGroup', 'WriteXRaySegments'].includes(s.Sid ?? ''));
    expect(statements).toEqual([
      {
        Sid: 'ListAccountLists',
        Effect: 'Allow',
        Action: 's3:ListBucket',
        Resource: accountListBucket,
        Condition: { StringLike: { 's3:prefix': ['account-lists/*'] } },
      },
      { Sid: 'ReadAccountLists', Effect: 'Allow', Action: 's3:GetObject', Resource: { 'Fn::Join': ['', [accountListBucket, '/account-lists/*']] } },
      { Sid: 'ListRawBucket', Effect: 'Allow', Action: 's3:ListBucket', Resource: arnOf(logicalId(core, core.storage.rawBucket)) },
      { Sid: 'ReadRunMarkers', Effect: 'Allow', Action: 's3:GetObject', Resource: raw('runs/*') },
      { Sid: 'RecordRunCompletion', Effect: 'Allow', Action: 's3:PutObject', Resource: raw('runs/*/completion.json') },
      {
        Sid: 'TrackFailureDeliveries',
        Effect: 'Allow',
        Action: ['s3:GetObject', 's3:PutObject'],
        Resource: raw('account_number=*/case_number=*/failure-deliveries/*'),
      },
      {
        Sid: 'DescribeCaseAnalysis',
        Effect: 'Allow',
        Action: 'states:DescribeExecution',
        Resource: `arn:aws:states:${core.config.region}:${core.config.managementAccountId}:execution:${names.caseAnalysisStateMachine}:*`,
      },
      { Sid: 'SendFailureRecords', Effect: 'Allow', Action: 'sqs:SendMessage', Resource: arnOf(logicalId(core, core.queues.workflowFailures)) },
    ]);
    // Run Completion can never delete objects or start, stop or redrive executions.
    const actions = statements.flatMap((s) => [s.Action].flat());
    expect(actions.filter((action) => /Delete|StartExecution|StopExecution|RedriveExecution/.test(String(action)))).toEqual([]);
  });

  test('the alarm topic enforces TLS and subscribes the configured email address', () => {
    const { core, template } = coreTemplate({ alarmEmail: 'ops@example.com' });
    const names = resourceNames(core.config);
    template.hasResourceProperties('AWS::SNS::Topic', { TopicName: names.alarmTopic, KmsMasterKeyId: Match.absent() });
    template.hasResourceProperties('AWS::SNS::TopicPolicy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({ Effect: 'Deny', Action: 'sns:Publish', Condition: { Bool: { 'aws:SecureTransport': 'false' } } }),
        ]),
      },
    });
    template.resourcePropertiesCountIs('AWS::SNS::Subscription', { Protocol: 'email', Endpoint: 'ops@example.com' }, 1);
    template.resourceCountIs('AWS::KMS::Key', 0);
  });

  test('without alarmEmail the topic has no subscription; prod always subscribes its required address', () => {
    const { template } = coreTemplate();
    template.resourceCountIs('AWS::SNS::Subscription', 0);
    const { core } = addCaseInsightsStacks(testApp(), { env: 'prod' });
    expect(core.config.alarmEmail).toBeDefined();
    Template.fromStack(core).hasResourceProperties('AWS::SNS::Subscription', { Protocol: 'email', Endpoint: core.config.alarmEmail });
  });

  test('every alarm notifies the topic and the inventory covers every function, queue and requested signal', () => {
    const { core, template } = coreTemplate();
    const prefix = resourceNames(core.config).alarmPrefix;
    const topic = { Ref: logicalId(core, core.observability.alarmTopic) };
    const all = alarms(template);
    for (const alarm of all) {
      expect(alarm.AlarmActions).toEqual([topic]);
      expect(alarm.AlarmName.startsWith(prefix)).toBe(true);
    }
    expect(all.map((alarm) => alarm.AlarmName.slice(prefix.length)).sort()).toEqual(
      [
        ...PIPELINE_FUNCTIONS.map((fn) => `LambdaErrors-${fn}`),
        'WorkflowExecutionsFailed',
        'WorkflowExecutionsTimedOut',
        'WorkflowExecutionsAborted',
        ...MONITORED_QUEUES.map((queue) => `QueueMessages-${queue}`),
        'WorkflowFailuresRecorded',
        'FailureDeliveryErrors',
        'CleanupErrors',
        'RunCompletionErrors',
        'MissingDailyRunCompletion',
      ].sort(),
    );
    expect(core.observability.alarms.map(({ purpose }) => `${prefix}${purpose}`).sort()).toEqual(all.map((alarm) => alarm.AlarmName).sort());

    // One error alarm per pipeline function (CDK's own custom-resource functions excluded).
    const functions = Object.entries(template.findResources('AWS::Lambda::Function')).filter(
      ([, fn]) => (fn.Properties as { Environment?: { Variables?: Record<string, unknown> } }).Environment?.Variables?.POWERTOOLS_SERVICE_NAME !== undefined,
    );
    const errorAlarmTargets = all
      .filter((alarm) => alarm.MetricName === 'Errors' && alarm.Namespace === 'AWS/Lambda')
      .map((alarm) => alarm.Dimensions?.find((d) => d.Name === 'FunctionName')?.Value);
    expect(errorAlarmTargets).toEqual(expect.arrayContaining(functions.map(([id]) => ({ Ref: id }))));
    expect(errorAlarmTargets).toHaveLength(functions.length);

    // Every queue without a redrive policy (DLQs and terminal queues) alarms on any visible message.
    const unredriven = Object.entries(template.findResources('AWS::SQS::Queue'))
      .filter(([, queue]) => (queue.Properties as { RedrivePolicy?: unknown }).RedrivePolicy === undefined)
      .map(([id]) => ({ 'Fn::GetAtt': [id, 'QueueName'] }));
    const queueAlarms = all.filter((alarm) => alarm.MetricName === 'ApproximateNumberOfMessagesVisible');
    expect(queueAlarms.map((alarm) => alarm.Dimensions?.find((d) => d.Name === 'QueueName')?.Value)).toEqual(expect.arrayContaining(unredriven));
    expect(queueAlarms).toHaveLength(unredriven.length);
    expect(unredriven).toContainEqual({ 'Fn::GetAtt': [logicalId(core, core.queues.workflowFailures), 'QueueName'] });
    for (const alarm of queueAlarms) {
      expect(alarm).toMatchObject({ Statistic: 'Maximum', Threshold: 1, ComparisonOperator: 'GreaterThanOrEqualToThreshold' });
    }
  });

  test('workflow failures, timeouts and aborts alarm on the CaseAnalysis state machine', () => {
    const { core, template } = coreTemplate();
    const prefix = resourceNames(core.config).alarmPrefix;
    const machine = { Ref: logicalId(core, core.caseAnalysis.stateMachine) };
    for (const [purpose, metric] of [
      ['WorkflowExecutionsFailed', 'ExecutionsFailed'],
      ['WorkflowExecutionsTimedOut', 'ExecutionsTimedOut'],
      ['WorkflowExecutionsAborted', 'ExecutionsAborted'],
    ] as const) {
      expect(alarmNamed(template, `${prefix}${purpose}`)).toMatchObject({
        Namespace: 'AWS/States',
        MetricName: metric,
        Dimensions: [{ Name: 'StateMachineArn', Value: machine }],
        Statistic: 'Sum',
        Threshold: 1,
        ComparisonOperator: 'GreaterThanOrEqualToThreshold',
        TreatMissingData: 'notBreaching',
      });
    }
  });

  test('failure-record alarms include workflow, oversized-conversation and reconciled records and delivery errors', () => {
    const { core, template } = coreTemplate();
    const prefix = resourceNames(core.config).alarmPrefix;
    expect(serviceMetricsOf(alarmNamed(template, `${prefix}WorkflowFailuresRecorded`))).toEqual(
      [
        'handle-failure/WorkflowFailuresRecorded',
        'case-annotation/OversizedConversations',
        'case-cleanup/FailureDeliveriesSent',
        'run-completion/FailureDeliveriesSent',
      ].sort(),
    );
    expect(serviceMetricsOf(alarmNamed(template, `${prefix}FailureDeliveryErrors`))).toEqual(
      ['handle-failure/WorkflowFailureDeliveryErrors', 'case-cleanup/FailureDeliveryErrors', 'run-completion/FailureDeliveryErrors'].sort(),
    );
    expect(serviceMetricsOf(alarmNamed(template, `${prefix}CleanupErrors`))).toEqual(['case-cleanup/CleanupErrors']);
    expect(alarmNamed(template, `${prefix}RunCompletionErrors`)).toMatchObject({ EvaluationPeriods: 3, DatapointsToAlarm: 3, Period: 3600 });
    const math = alarmNamed(template, `${prefix}WorkflowFailuresRecorded`).Metrics?.find((m) => m.Expression !== undefined);
    expect(math?.Expression).toMatch(/^SUM\(\[FILL\(\w+, 0\)(, FILL\(\w+, 0\))+\]\)$/);
  });

  test('the missing-heartbeat alarm fires when no run completed within the window, including when no data arrives', () => {
    const { core, template } = coreTemplate();
    const prefix = resourceNames(core.config).alarmPrefix;
    expect(alarmNamed(template, `${prefix}MissingDailyRunCompletion`)).toMatchObject({
      Namespace: METRICS_NAMESPACE,
      MetricName: HEARTBEAT_METRIC,
      Dimensions: [{ Name: 'service', Value: RUN_COMPLETION_SERVICE }],
      Statistic: 'Sum',
      Period: 3600,
      EvaluationPeriods: HEARTBEAT_WINDOW_HOURS,
      DatapointsToAlarm: HEARTBEAT_WINDOW_HOURS,
      Threshold: 1,
      ComparisonOperator: 'LessThanThreshold',
      TreatMissingData: 'breaching',
    });
    // CloudWatch limits hourly alarms to a seven-day evaluation range.
    expect(HEARTBEAT_WINDOW_HOURS * 3600).toBeLessThanOrEqual(7 * 24 * 3600);
    expect(HEARTBEAT_WINDOW_HOURS).toBeGreaterThan(24);
    // Only Run Completion emits the heartbeat; discovery cannot satisfy it.
    const emitters = fs
      .readdirSync(path.join(REPO_ROOT, 'src', 'lambda'))
      .filter((dir) => fs.existsSync(path.join(REPO_ROOT, 'src', 'lambda', dir, 'app.py')))
      .filter((dir) => fs.readFileSync(path.join(REPO_ROOT, 'src', 'lambda', dir, 'app.py'), 'utf8').includes(`"${HEARTBEAT_METRIC}"`));
    expect(emitters).toEqual(['run_completion']);
  });

  test('the dashboard shows every requested metric, all monitored queues and every alarm', () => {
    const { core, template } = coreTemplate();
    const body = dashboardBody(template);
    template.hasResourceProperties('AWS::CloudWatch::Dashboard', { DashboardName: resourceNames(core.config).dashboard });
    for (const metric of [
      '"AccountsDiscovered"',
      '"AccountsExcludedInactive"',
      '"AccountsExcludedManagement"',
      '"AccountsSkipped"',
      '"CasesRetrieved"',
      '"CasesProcessed"',
      '"WorkflowFailuresRecorded"',
      '"OversizedConversations"',
      '"BedrockLatency"',
      '"BedrockInputTokens"',
      '"BedrockOutputTokens"',
      '"BedrockThrottles"',
      '"ExecutionsSucceeded"',
      '"ExecutionsFailed"',
      '"ExecutionsTimedOut"',
      '"ExecutionsAborted"',
      '"ApproximateNumberOfMessagesVisible"',
      '"CleanupErrors"',
      `"${HEARTBEAT_METRIC}"`,
    ]) {
      expect(body).toContain(metric);
    }
    expect(body).toContain('100 * succeeded / (succeeded');
    for (const queue of [
      core.queues.activeAccounts.deadLetterQueue,
      core.queues.annotation.deadLetterQueue,
      core.queues.summary.deadLetterQueue,
      core.queues.workflowFailures,
      core.queues.accountDiscoveryFailures,
    ]) {
      expect(body).toContain(JSON.stringify({ 'Fn::GetAtt': [logicalId(core, queue), 'QueueName'] }));
    }
    for (const { alarm } of core.observability.alarms) {
      expect(body).toContain(JSON.stringify(arnOf(logicalId(core, alarm))));
    }
  });

  test('alarms added later notify the topic and join the dashboard alarm status', () => {
    const config = testConfig();
    const core = new CaseInsightsCoreStack(testApp(), 'Core', { config, env: { account: config.managementAccountId, region: config.region } });
    const extra = core.observability.addAlarm('McpServerErrors', 'Synthetic later alarm.', new Metric({ namespace: 'Test', metricName: 'Errors' }));
    const template = Template.fromStack(core);
    expect(alarmNamed(template, `${resourceNames(config).alarmPrefix}McpServerErrors`).AlarmActions).toEqual([{ Ref: logicalId(core, core.observability.alarmTopic) }]);
    expect(dashboardBody(template)).toContain(JSON.stringify(arnOf(logicalId(core, extra))));
  });

  test('every function and the workflow keep payloads out of telemetry with the configured retention', () => {
    const { template } = coreTemplate({ logRetentionDays: 60 });
    const functions = Object.values(template.findResources('AWS::Lambda::Function'))
      .map((fn) => fn.Properties as { TracingConfig?: unknown; LoggingConfig?: unknown; Environment?: { Variables?: Record<string, unknown> } })
      .filter((fn) => fn.Environment?.Variables?.POWERTOOLS_SERVICE_NAME !== undefined);
    expect(functions).toHaveLength(PIPELINE_FUNCTIONS.length);
    for (const fn of functions) {
      expect(fn.TracingConfig).toEqual({ Mode: 'Active' });
      expect(fn.LoggingConfig).toMatchObject({ LogFormat: 'JSON' });
      expect(fn.Environment?.Variables).toMatchObject({
        POWERTOOLS_METRICS_NAMESPACE: METRICS_NAMESPACE,
        POWERTOOLS_LOGGER_LOG_EVENT: 'false',
        POWERTOOLS_TRACER_CAPTURE_RESPONSE: 'false',
        POWERTOOLS_TRACER_CAPTURE_ERROR: 'false',
      });
    }
    const logGroups = Object.values(template.findResources('AWS::Logs::LogGroup')).map((group) => group.Properties as { RetentionInDays?: number });
    expect(logGroups).toHaveLength(PIPELINE_FUNCTIONS.length + 1);
    for (const group of logGroups) {
      expect(group.RetentionInDays).toBe(60);
    }
    template.hasResourceProperties('AWS::StepFunctions::StateMachine', {
      TracingConfiguration: { Enabled: true },
      LoggingConfiguration: { IncludeExecutionData: false },
    });
  });

  test('alarm metric names and run-marker keys match the Python handlers', () => {
    const { template } = coreTemplate();
    const lambdaSource = fs
      .readdirSync(path.join(REPO_ROOT, 'src', 'lambda'), { recursive: true, encoding: 'utf8' })
      .filter((file) => file.endsWith('.py'))
      .map((file) => fs.readFileSync(path.join(REPO_ROOT, 'src', 'lambda', file), 'utf8'))
      .join('\n');
    const used = new Set(alarms(template).flatMap((alarm) => serviceMetricsOf(alarm)));
    expect(used.size).toBeGreaterThan(5);
    for (const entry of used) {
      const name = entry.split('/')[1] ?? '';
      expect(lambdaSource).toContain(`"${name}"`);
    }
    const runMarkers = fs.readFileSync(path.join(REPO_ROOT, 'src', 'lambda', 'common', 'run_markers.py'), 'utf8');
    expect(runMarkers).toMatch(new RegExp(`^RUNS_PREFIX: Final = "${RUN_MARKERS_PREFIX}"$`, 'm'));
    expect(runMarkers).toContain('accounts/{validate_account_id(account_id)}.json');
    expect(runMarkers).toContain('{run_prefix(run_id)}cases/');
    expect(runMarkers).toContain('{run_prefix(run_id)}completion.json');
  });

  test('every alarm has a runbook entry', () => {
    const { core } = coreTemplate();
    const runbook = fs.readFileSync(path.join(REPO_ROOT, 'docs', 'runbook.md'), 'utf8');
    const headings = new Set([...runbook.matchAll(/^### `?([A-Za-z0-9-]+)`?$/gm)].map((match) => match[1]));
    for (const { purpose, alarm } of core.observability.alarms) {
      expect(headings).toContain(purpose);
      const description = (alarm.node.defaultChild as { alarmDescription?: string }).alarmDescription ?? '';
      expect(description).toContain(`docs/runbook.md#${purpose.toLowerCase()}`);
    }
  });

  test('passes cdk-nag with specific acknowledgements only', () => {
    const app = nagApp();
    const { core } = addCaseInsightsStacks(app, { env: 'dev' });
    expect(synthError(app)).toBeUndefined();
    const names = resourceNames(core.config);
    const rawFinding = (pattern: string): string => `AwsSolutions-IAM5[Resource::<${logicalId(core, core.storage.rawBucket)}.Arn>/${pattern}]`;
    const acknowledged = acknowledgedRules(core.observability).map(({ path: constructPath, id }) => [
      constructPath.replace(`${core.node.id}/`, ''),
      id.replace(/^.*::(?=AwsSolutions-)/, ''),
    ]);
    expect(acknowledged.sort()).toEqual(
      [
        ['Observability/AlarmTopic', 'AwsSolutions-SNS2'],
        ['Observability/RunCompletion/TelemetryPolicy', 'AwsSolutions-IAM5[Resource::*]'],
        ['Observability/RunCompletion/Function', 'AwsSolutions-L1'],
        ['Observability/RunCompletionAccess', `AwsSolutions-IAM5[Resource::<${logicalId(core, core.storage.accountListBucket)}.Arn>/account-lists/*]`],
        ['Observability/RunCompletionAccess', rawFinding('runs/*')],
        ['Observability/RunCompletionAccess', rawFinding('runs/*/completion.json')],
        ['Observability/RunCompletionAccess', rawFinding('account_number=*/case_number=*/failure-deliveries/*')],
        [
          'Observability/RunCompletionAccess',
          `AwsSolutions-IAM5[Resource::arn:aws:states:${core.config.region}:${core.config.managementAccountId}:execution:${names.caseAnalysisStateMachine}:*]`,
        ],
        ['Observability/RunCompletionScheduleRole', `AwsSolutions-IAM5[Resource::<${logicalId(core, core.observability.runCompletionFunction.function)}.Arn>:*]`],
      ].sort(),
    );
  });
});

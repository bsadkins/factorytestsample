import { Match, Template } from 'aws-cdk-lib/assertions';
import {
  addCaseInsightsStacks,
  CASE_ANNOTATION_BATCH_SIZE,
  CASE_COLLECTION_MAX_CONCURRENCY,
  CASE_RETRIEVAL_BATCH_SIZE,
  CaseInsightsCoreStack,
  MEMBER_ROLE_SUPPORT_ACTIONS,
  queueVisibilityTimeoutSeconds,
  resourceNames,
  TERMINAL_MARKER_PATTERN,
} from '../../lib';
import { acknowledgedRules, asArray, iamPolicyStatements, nagApp, synthError, testApp, testConfig } from '../helpers/cdk';

function coreTemplate(overrides: Parameters<typeof testConfig>[0] = {}): { core: CaseInsightsCoreStack; template: Template } {
  const config = testConfig(overrides);
  const app = testApp();
  const core = new CaseInsightsCoreStack(app, 'Core', { config, env: { account: config.managementAccountId, region: config.region } });
  return { core, template: Template.fromStack(core) };
}

function logicalId(core: CaseInsightsCoreStack, construct: { node: { defaultChild?: unknown } }): string {
  return core.getLogicalId(construct.node.defaultChild as never);
}

const arnOf = (id: string): unknown => ({ 'Fn::GetAtt': [id, 'Arn'] });

describe('CaseCollection', () => {
  test('retrieval and annotation run as the member-trusted execution roles with focused configuration', () => {
    const { core, template } = coreTemplate({ lookbackMonths: 6 });
    const names = resourceNames(core.config);
    const { retrievalFunction, annotationFunction } = core.caseCollection;
    expect(retrievalFunction.role).toBe(core.memberAccess.retrievalRole);
    expect(annotationFunction.role).toBe(core.memberAccess.annotationRole);
    const shared = {
      RAW_BUCKET: { Ref: logicalId(core, core.storage.rawBucket) },
      PROCESSED_BUCKET: { Ref: logicalId(core, core.storage.processedBucket) },
      IDEMPOTENCY_TABLE_NAME: { Ref: logicalId(core, core.storage.idempotencyTable) },
      MEMBER_ROLE_NAME: names.memberRole,
      MEMBER_ROLE_PARTITION: 'aws',
    };
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: names.caseRetrievalFunction,
      Handler: 'app.handler',
      Runtime: 'python3.12',
      Timeout: core.config.operations.functions.consumerTimeoutSeconds,
      Role: arnOf(logicalId(core, core.memberAccess.retrievalRole)),
      Layers: [{ Ref: logicalId(core, core.layers.commonLayer) }],
      TracingConfig: { Mode: 'Active' },
      Environment: {
        Variables: Match.objectLike({
          ...shared,
          ANNOTATION_QUEUE_URL: { Ref: logicalId(core, core.queues.annotation.queue) },
          LOOKBACK_MONTHS: '6',
          POWERTOOLS_SERVICE_NAME: 'case-retrieval',
          POWERTOOLS_LOGGER_LOG_EVENT: 'false',
        }),
      },
    });
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: names.caseAnnotationFunction,
      Role: arnOf(logicalId(core, core.memberAccess.annotationRole)),
      Environment: {
        Variables: Match.objectLike({
          ...shared,
          SUMMARY_QUEUE_URL: { Ref: logicalId(core, core.queues.summary.queue) },
          FAILURE_QUEUE_URL: { Ref: logicalId(core, core.queues.workflowFailures) },
          POWERTOOLS_SERVICE_NAME: 'case-annotation',
        }),
      },
    });
  });

  test('both functions consume their stage queue with partial-batch failure reporting and bounded concurrency', () => {
    const { core, template } = coreTemplate();
    const { retrievalFunction, annotationFunction } = core.caseCollection;
    const collectionMappings = template.findResources('AWS::Lambda::EventSourceMapping', {
      Properties: { FunctionName: Match.objectLike({ Ref: Match.stringLikeRegexp('^CaseCollection') }) },
    });
    expect(Object.keys(collectionMappings)).toHaveLength(2);
    template.hasResourceProperties('AWS::Lambda::EventSourceMapping', {
      FunctionName: { Ref: logicalId(core, retrievalFunction.function) },
      EventSourceArn: arnOf(logicalId(core, core.queues.activeAccounts.queue)),
      BatchSize: CASE_RETRIEVAL_BATCH_SIZE,
      FunctionResponseTypes: ['ReportBatchItemFailures'],
      ScalingConfig: { MaximumConcurrency: CASE_COLLECTION_MAX_CONCURRENCY },
    });
    template.hasResourceProperties('AWS::Lambda::EventSourceMapping', {
      FunctionName: { Ref: logicalId(core, annotationFunction.function) },
      EventSourceArn: arnOf(logicalId(core, core.queues.annotation.queue)),
      BatchSize: CASE_ANNOTATION_BATCH_SIZE,
      FunctionResponseTypes: ['ReportBatchItemFailures'],
      ScalingConfig: { MaximumConcurrency: CASE_COLLECTION_MAX_CONCURRENCY },
    });
  });

  test('consumed queues keep a visibility timeout of at least six function timeouts and a DLQ', () => {
    const { core, template } = coreTemplate({ operations: { functions: { consumerTimeoutSeconds: 120 } } });
    const visibility = queueVisibilityTimeoutSeconds(core.config);
    expect(visibility).toBeGreaterThanOrEqual(6 * 120);
    const names = resourceNames(core.config);
    for (const [queueName, stage] of [
      [names.activeAccountsQueue, core.queues.activeAccounts],
      [names.annotationQueue, core.queues.annotation],
    ] as const) {
      template.hasResourceProperties('AWS::SQS::Queue', {
        QueueName: queueName,
        VisibilityTimeout: visibility,
        RedrivePolicy: { deadLetterTargetArn: arnOf(logicalId(core, stage.deadLetterQueue)) },
      });
    }
  });

  test('each role holds only the storage, queue and idempotency access its function uses', () => {
    const { core, template } = coreTemplate();
    const rawObjects = { 'Fn::Join': ['', [arnOf(logicalId(core, core.storage.rawBucket)), '/account_number=*']] };
    const tableArn = arnOf(logicalId(core, core.storage.idempotencyTable));
    const common = [
      { Sid: 'WriteRawCaseObjects', Effect: 'Allow', Action: 's3:PutObject', Resource: rawObjects },
      {
        Sid: 'ListProcessedCases',
        Effect: 'Allow',
        Action: 's3:ListBucket',
        Resource: arnOf(logicalId(core, core.storage.processedBucket)),
        Condition: { StringLike: { 's3:prefix': ['account_number=*'] } },
      },
      {
        Sid: 'IdempotencyRecords',
        Effect: 'Allow',
        Action: ['dynamodb:DeleteItem', 'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem'],
        Resource: tableArn,
      },
    ];
    const consume = (queue: string): unknown => ({
      Effect: 'Allow',
      Action: ['sqs:ChangeMessageVisibility', 'sqs:DeleteMessage', 'sqs:GetQueueAttributes', 'sqs:GetQueueUrl', 'sqs:ReceiveMessage'],
      Resource: arnOf(queue),
    });
    const statementsFor = (roleId: string): unknown[] =>
      iamPolicyStatements(template)
        .filter(({ roles }) => JSON.stringify(roles) === JSON.stringify([{ Ref: roleId }]))
        .map(({ statement }) => statement)
        .filter((s) => !['WriteOwnLogGroup', 'WriteXRaySegments', 'AssumeMemberSupportRole'].includes(s.Sid ?? ''));

    expect(statementsFor(logicalId(core, core.memberAccess.retrievalRole))).toEqual([
      consume(logicalId(core, core.queues.activeAccounts.queue)),
      ...common,
      {
        Sid: 'SendAnnotationReferences',
        Effect: 'Allow',
        Action: 'sqs:SendMessage',
        Resource: arnOf(logicalId(core, core.queues.annotation.queue)),
      },
    ]);
    expect(statementsFor(logicalId(core, core.memberAccess.annotationRole))).toEqual([
      consume(logicalId(core, core.queues.annotation.queue)),
      ...common,
      {
        Sid: 'SendSummaryReferences',
        Effect: 'Allow',
        Action: 'sqs:SendMessage',
        Resource: [arnOf(logicalId(core, core.queues.summary.queue)), arnOf(logicalId(core, core.queues.workflowFailures))],
      },
      {
        Sid: 'ReadTerminalMarkers',
        Effect: 'Allow',
        Action: 's3:GetObject',
        Resource: { 'Fn::Join': ['', [arnOf(logicalId(core, core.storage.rawBucket)), `/${TERMINAL_MARKER_PATTERN}`]] },
      },
      {
        Sid: 'FindTerminalMarkers',
        Effect: 'Allow',
        Action: 's3:ListBucket',
        Resource: arnOf(logicalId(core, core.storage.rawBucket)),
        Condition: { StringLike: { 's3:prefix': [TERMINAL_MARKER_PATTERN] } },
      },
    ]);
  });

  test('no management-account policy grants any AWS Support action; the member role is read-only', () => {
    const { template } = coreTemplate();
    const actions = iamPolicyStatements(template).flatMap(({ statement }) => asArray(statement.Action));
    expect(actions.filter((action) => action.toLowerCase().startsWith('support:'))).toEqual([]);
    expect([...MEMBER_ROLE_SUPPORT_ACTIONS]).toEqual(['support:DescribeCases', 'support:DescribeCommunications']);
  });

  test('passes cdk-nag with specific acknowledgements only', () => {
    const app = nagApp();
    const { core } = addCaseInsightsStacks(app, { env: 'dev' });
    expect(synthError(app)).toBeUndefined();
    const rawFinding = `AwsSolutions-IAM5[Resource::<${logicalId(core, core.storage.rawBucket)}.Arn>/account_number=*]`;
    const acknowledged = acknowledgedRules(core.caseCollection).map(({ path, id }) => [
      path.replace(`${core.node.id}/`, ''),
      id.replace(/^.*::(?=AwsSolutions-)/, ''),
    ]);
    expect(acknowledged.sort()).toEqual(
      [
        ['CaseCollection/CaseRetrieval/TelemetryPolicy', 'AwsSolutions-IAM5[Resource::*]'],
        ['CaseCollection/CaseRetrieval/Function', 'AwsSolutions-L1'],
        ['CaseCollection/CaseRetrievalAccess', rawFinding],
        ['CaseCollection/CaseAnnotation/TelemetryPolicy', 'AwsSolutions-IAM5[Resource::*]'],
        ['CaseCollection/CaseAnnotation/Function', 'AwsSolutions-L1'],
        ['CaseCollection/CaseAnnotationAccess', rawFinding],
        ['CaseCollection/CaseAnnotationAccess', `AwsSolutions-IAM5[Resource::<${logicalId(core, core.storage.rawBucket)}.Arn>/${TERMINAL_MARKER_PATTERN}]`],
      ].sort(),
    );
  });
});

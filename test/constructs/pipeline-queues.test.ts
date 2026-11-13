import { Duration, Stack } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import type { CfnQueue } from 'aws-cdk-lib/aws-sqs';
import {
  type CaseInsightsConfig,
  ConfigValidationError,
  deadLetterQueueName,
  parseConfig,
  PipelineQueues,
  resourceNames,
  SOURCE_QUEUE_RETENTION_DAYS,
  StageQueue,
} from '../../lib';
import { minimalConfig } from '../config/helpers';
import { acknowledgedRules, nagApp, synthError, testApp, testConfig, testStack } from '../helpers/cdk';

interface QueueResource {
  Properties: {
    QueueName: string;
    SqsManagedSseEnabled?: boolean;
    KmsMasterKeyId?: unknown;
    VisibilityTimeout?: number;
    MessageRetentionPeriod?: number;
    RedrivePolicy?: { deadLetterTargetArn: { 'Fn::GetAtt': [string, string] }; maxReceiveCount: number };
  };
}

function queuesTemplate(config: CaseInsightsConfig): { template: Template; queues: PipelineQueues; stack: Stack } {
  const app = testApp();
  const stack = testStack(app, config);
  const queues = new PipelineQueues(stack, 'Queues', { config });
  return { template: Template.fromStack(stack), queues, stack };
}

function queuesByName(template: Template): Map<string, { logicalId: string; resource: QueueResource }> {
  const queues = template.findResources('AWS::SQS::Queue') as Record<string, QueueResource>;
  return new Map(Object.entries(queues).map(([logicalId, resource]) => [resource.Properties.QueueName, { logicalId, resource }]));
}

describe('PipelineQueues', () => {
  const config = testConfig({ operations: { queues: { maxReceiveCount: 4, dlqRetentionDays: 10 }, functions: { consumerTimeoutSeconds: 120 } } });
  const names = resourceNames(config);
  const stageQueueNames = [names.activeAccountsQueue, names.annotationQueue, names.summaryQueue];

  test('every stage queue redrives to its own DLQ with configured receive count and 6x visibility', () => {
    const { template } = queuesTemplate(config);
    const byName = queuesByName(template);
    expect(byName.size).toBe(7);

    for (const name of stageQueueNames) {
      const source = byName.get(name);
      const dlq = byName.get(deadLetterQueueName(name));
      expect(source).toBeDefined();
      expect(dlq).toBeDefined();
      expect(source?.resource.Properties).toMatchObject({
        SqsManagedSseEnabled: true,
        VisibilityTimeout: 720,
        MessageRetentionPeriod: SOURCE_QUEUE_RETENTION_DAYS * 86400,
        RedrivePolicy: { deadLetterTargetArn: { 'Fn::GetAtt': [dlq?.logicalId, 'Arn'] }, maxReceiveCount: 4 },
      });
      expect(dlq?.resource.Properties).toMatchObject({ SqsManagedSseEnabled: true, MessageRetentionPeriod: 10 * 86400 });
      expect(dlq?.resource.Properties.RedrivePolicy).toBeUndefined();
    }

    const failures = byName.get(names.workflowFailureQueue);
    expect(failures?.resource.Properties).toMatchObject({ SqsManagedSseEnabled: true, MessageRetentionPeriod: 10 * 86400 });
    for (const { resource } of byName.values()) {
      expect(resource.Properties.KmsMasterKeyId).toBeUndefined();
    }
    template.resourceCountIs('AWS::KMS::Key', 0);
  });

  test('every queue denies requests without TLS', () => {
    const { template } = queuesTemplate(config);
    const byName = queuesByName(template);
    const policies = template.findResources('AWS::SQS::QueuePolicy') as Record<
      string,
      { Properties: { Queues: { Ref: string }[]; PolicyDocument: { Statement: Record<string, unknown>[] } } }
    >;
    const protectedQueues = new Set(
      Object.values(policies)
        .filter((policy) =>
          policy.Properties.PolicyDocument.Statement.some(
            (statement) =>
              statement.Effect === 'Deny' &&
              JSON.stringify(statement.Condition) === JSON.stringify({ Bool: { 'aws:SecureTransport': 'false' } }),
          ),
        )
        .flatMap((policy) => policy.Properties.Queues.map((queue) => queue.Ref)),
    );
    expect([...byName.values()].map(({ logicalId }) => protectedQueues.has(logicalId))).toEqual(Array(7).fill(true));
  });

  test('an explicit visibility timeout override is honoured', () => {
    const custom = testConfig({ operations: { queues: { visibilityTimeoutSeconds: 3600 } } });
    const byName = queuesByName(queuesTemplate(custom).template);
    for (const name of stageQueueNames) {
      expect(byName.get(name)?.resource.Properties.VisibilityTimeout).toBe(3600);
    }
  });

  test('stage queue handles are typed for consumers', () => {
    const { queues, stack } = queuesTemplate(config);
    for (const stage of [queues.activeAccounts, queues.annotation, queues.summary]) {
      expect(stage.queue.deadLetterQueue?.queue).toBe(stage.deadLetterQueue);
      expect(stack.resolve(stage.queue.queueName)).toEqual({ 'Fn::GetAtt': [stack.getLogicalId(stage.queue.node.defaultChild as CfnQueue), 'QueueName'] });
    }
  });

  test('only the terminal workflow-failure queue acknowledges SQS3, with a reason', () => {
    const app = nagApp();
    const stack = testStack(app, config);
    const queues = new PipelineQueues(stack, 'Queues', { config });
    expect(synthError(app)).toBeUndefined();
    expect(acknowledgedRules(queues).map(({ path, id }) => [path, id.replace(/^.*::(?=AwsSolutions-)/, '')])).toEqual([
      ['TestStack/Queues/WorkflowFailures', 'AwsSolutions-SQS3'],
    ]);
  });

  test('DLQ retention must exceed source retention', () => {
    const stack = new Stack(testApp(), 'Guard');
    expect(
      () =>
        new StageQueue(stack, 'Short', {
          queueName: 'short',
          visibilityTimeout: Duration.minutes(30),
          maxReceiveCount: 3,
          deadLetterRetention: Duration.days(SOURCE_QUEUE_RETENTION_DAYS),
        }),
    ).toThrow(/must exceed/);
    expect(() => parseConfig(minimalConfig({ operations: { queues: { dlqRetentionDays: 4 } } }))).toThrow(ConfigValidationError);
  });
});

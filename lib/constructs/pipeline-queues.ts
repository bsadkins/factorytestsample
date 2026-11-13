import { Duration } from 'aws-cdk-lib';
import { type IQueue, Queue, QueueEncryption } from 'aws-cdk-lib/aws-sqs';
import { Construct } from 'constructs';
import { type CaseInsightsConfig, queueVisibilityTimeoutSeconds, resourceNames } from '../config';
import { acknowledgeNagFinding } from '../nag';
import { StageQueue, type StageQueueResources } from './stage-queue';

/** Typed queue handles for the pipeline stages. Messages carry references, never case content. */
export interface PipelineQueueResources {
  /** `{accountId, accountName, runId}` per selected account; consumed by case retrieval. */
  readonly activeAccounts: StageQueueResources;
  /** Raw case references; consumed by case annotation. */
  readonly annotation: StageQueueResources;
  /** Annotated case references; consumed by the workflow starter. */
  readonly summary: StageQueueResources;
  /** Terminal queue for case-analysis attempts that exhausted their workflow retries. */
  readonly workflowFailures: IQueue;
  /** Terminal queue for discovery schedule deliveries and asynchronous invocations that exhausted retries. */
  readonly accountDiscoveryFailures: IQueue;
}

export interface PipelineQueuesProps {
  readonly config: CaseInsightsConfig;
}

/** The stage queues between account discovery, retrieval, annotation and the analysis workflow. */
export class PipelineQueues extends Construct implements PipelineQueueResources {
  public readonly activeAccounts: StageQueue;
  public readonly annotation: StageQueue;
  public readonly summary: StageQueue;
  public readonly workflowFailures: Queue;
  public readonly accountDiscoveryFailures: Queue;

  public constructor(scope: Construct, id: string, props: PipelineQueuesProps) {
    super(scope, id);
    const { config } = props;
    const names = resourceNames(config);
    const { maxReceiveCount, dlqRetentionDays } = config.operations.queues;
    const stageProps = {
      visibilityTimeout: Duration.seconds(queueVisibilityTimeoutSeconds(config)),
      maxReceiveCount,
      deadLetterRetention: Duration.days(dlqRetentionDays),
    };

    this.activeAccounts = new StageQueue(this, 'ActiveAccounts', { queueName: names.activeAccountsQueue, ...stageProps });
    this.annotation = new StageQueue(this, 'Annotation', { queueName: names.annotationQueue, ...stageProps });
    this.summary = new StageQueue(this, 'Summary', { queueName: names.summaryQueue, ...stageProps });

    this.workflowFailures = new Queue(this, 'WorkflowFailures', {
      queueName: names.workflowFailureQueue,
      encryption: QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: Duration.days(dlqRetentionDays),
    });
    acknowledgeNagFinding(
      this.workflowFailures,
      'AwsSolutions-SQS3',
      'Terminal dead-letter destination written by the workflow failure handler after task retries are exhausted; it has no consumer, so a further DLQ would never receive messages. Operators redrive it explicitly.',
    );

    this.accountDiscoveryFailures = new Queue(this, 'AccountDiscoveryFailures', {
      queueName: names.accountDiscoveryFailureQueue,
      encryption: QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: Duration.days(dlqRetentionDays),
    });
    acknowledgeNagFinding(
      this.accountDiscoveryFailures,
      'AwsSolutions-SQS3',
      'Terminal dead-letter destination for the discovery schedule and the asynchronous account lookup and reader invocations after their retries; it has no consumer, so a further DLQ would never receive messages.',
    );
  }
}

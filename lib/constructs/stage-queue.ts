import { Duration } from 'aws-cdk-lib';
import { type IQueue, Queue, QueueEncryption } from 'aws-cdk-lib/aws-sqs';
import { Construct } from 'constructs';
import { deadLetterQueueName } from '../config';

/**
 * Source work queues keep messages for this long. SQS keeps the original enqueue time when a
 * message moves to its DLQ, so DLQ retention must be longer (enforced by the config schema).
 */
export const SOURCE_QUEUE_RETENTION_DAYS = 4;

/** A source queue and its dead-letter queue, as consumed by an SQS-triggered function. */
export interface StageQueueResources {
  readonly queue: IQueue;
  readonly deadLetterQueue: IQueue;
}

export interface StageQueueProps {
  readonly queueName: string;
  /** At least six times the consuming function's timeout. */
  readonly visibilityTimeout: Duration;
  readonly maxReceiveCount: number;
  readonly deadLetterRetention: Duration;
}

/** SSE-SQS encrypted, SSL-only work queue with a dedicated, equally protected dead-letter queue. */
export class StageQueue extends Construct implements StageQueueResources {
  public readonly queue: Queue;
  public readonly deadLetterQueue: Queue;

  public constructor(scope: Construct, id: string, props: StageQueueProps) {
    super(scope, id);
    if (props.deadLetterRetention.toDays() <= SOURCE_QUEUE_RETENTION_DAYS) {
      throw new Error(
        `Dead-letter retention for ${props.queueName} must exceed the ${String(SOURCE_QUEUE_RETENTION_DAYS)}-day source retention`,
      );
    }

    this.deadLetterQueue = new Queue(this, 'DeadLetterQueue', {
      queueName: deadLetterQueueName(props.queueName),
      encryption: QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: props.deadLetterRetention,
    });

    this.queue = new Queue(this, 'Queue', {
      queueName: props.queueName,
      encryption: QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      visibilityTimeout: props.visibilityTimeout,
      retentionPeriod: Duration.days(SOURCE_QUEUE_RETENTION_DAYS),
      deadLetterQueue: { queue: this.deadLetterQueue, maxReceiveCount: props.maxReceiveCount },
    });
  }
}

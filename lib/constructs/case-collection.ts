import { Duration, Stack } from 'aws-cdk-lib';
import type { ITable } from 'aws-cdk-lib/aws-dynamodb';
import { type IRole, Policy, PolicyStatement } from 'aws-cdk-lib/aws-iam';
import type { ILayerVersion } from 'aws-cdk-lib/aws-lambda';
import { SqsEventSource } from 'aws-cdk-lib/aws-lambda-event-sources';
import type { IBucket } from 'aws-cdk-lib/aws-s3';
import type { IQueue } from 'aws-cdk-lib/aws-sqs';
import { Construct } from 'constructs';
import { type CaseInsightsConfig, resourceNames } from '../config';
import { acknowledgeNagFinding } from '../nag';
import { handlerDirectory, logicalIdOf } from './construct-utils';
import { PipelineFunction } from './pipeline-function';

/** Raw and processed case objects live under Hive-style `account_number=<id>/` partitions. */
export const CASE_PARTITION_PATTERN = 'account_number=*';
/** Terminal failure markers written by annotation; must match TERMINAL_MARKER_FILE in common/failures.py. */
export const TERMINAL_MARKER_PATTERN = 'account_number=*/case_number=*/terminal-failure.json';
/** One account per retrieval invocation, so a large account gets the whole function timeout. */
export const CASE_RETRIEVAL_BATCH_SIZE = 1;
export const CASE_ANNOTATION_BATCH_SIZE = 5;
/**
 * Upper bound on concurrent retrieval and annotation invocations each. Keeps the per-account
 * Support API request rate modest; SQS holds the remaining work.
 */
export const CASE_COLLECTION_MAX_CONCURRENCY = 5;

export interface CaseCollectionProps {
  readonly config: CaseInsightsConfig;
  /** Receives raw case metadata and complete conversations. */
  readonly rawBucket: IBucket;
  /** Authoritative processed records; listed to skip completed cases. */
  readonly processedBucket: IBucket;
  /** Powertools idempotency table. */
  readonly idempotencyTable: ITable;
  /** `{accountId, accountName, runId}` messages consumed by case retrieval. */
  readonly activeAccountsQueue: IQueue;
  /** Case references written by retrieval and consumed by annotation. */
  readonly annotationQueue: IQueue;
  /** Annotated case references consumed by the workflow starter. */
  readonly summaryQueue: IQueue;
  /** Terminal failure queue; receives conversations too large to summarise. */
  readonly failureQueue: IQueue;
  /** Execution roles trusted by the member role; see MemberAccessRoles. */
  readonly retrievalRole: IRole;
  readonly annotationRole: IRole;
  readonly commonLayer: ILayerVersion;
}

/**
 * Case collection: Case Retrieval reads resolved cases in each member account through the
 * scoped member role and stores their metadata; Case Annotation reads each case's complete
 * conversation into the raw bucket and hands a reference to the summary queue. Both are SQS
 * consumers with partial-batch failure reporting. Neither can write to AWS Support: the member
 * role grants only DescribeCases and DescribeCommunications.
 */
export class CaseCollection extends Construct {
  public readonly retrievalFunction: PipelineFunction;
  public readonly annotationFunction: PipelineFunction;

  public constructor(scope: Construct, id: string, props: CaseCollectionProps) {
    super(scope, id);
    const { config, rawBucket, processedBucket, idempotencyTable, commonLayer } = props;
    const names = resourceNames(config);
    const timeout = Duration.seconds(config.operations.functions.consumerTimeoutSeconds);
    const memberEnvironment = {
      RAW_BUCKET: rawBucket.bucketName,
      PROCESSED_BUCKET: processedBucket.bucketName,
      IDEMPOTENCY_TABLE_NAME: idempotencyTable.tableName,
      MEMBER_ROLE_NAME: names.memberRole,
      MEMBER_ROLE_PARTITION: Stack.of(this).partition,
    };
    const rawObjects = rawBucket.arnForObjects(CASE_PARTITION_PATTERN);
    const rawObjectsFinding = `AwsSolutions-IAM5[Resource::<${logicalIdOf(rawBucket)}.Arn>/${CASE_PARTITION_PATTERN}]`;
    const sharedStatements = (): PolicyStatement[] => [
      new PolicyStatement({
        sid: 'WriteRawCaseObjects',
        actions: ['s3:PutObject'],
        resources: [rawObjects],
      }),
      new PolicyStatement({
        sid: 'ListProcessedCases',
        actions: ['s3:ListBucket'],
        resources: [processedBucket.bucketArn],
        conditions: { StringLike: { 's3:prefix': [CASE_PARTITION_PATTERN] } },
      }),
      new PolicyStatement({
        sid: 'IdempotencyRecords',
        actions: ['dynamodb:DeleteItem', 'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem'],
        resources: [idempotencyTable.tableArn],
      }),
    ];

    this.retrievalFunction = new PipelineFunction(this, 'CaseRetrieval', {
      config,
      functionName: names.caseRetrievalFunction,
      description: `Collects resolved Support cases per member account for annotation (${config.uniqueId})`,
      handlerDir: handlerDirectory('case_retrieval'),
      serviceName: 'case-retrieval',
      layers: [commonLayer],
      timeout,
      role: props.retrievalRole,
      environment: {
        ...memberEnvironment,
        ANNOTATION_QUEUE_URL: props.annotationQueue.queueUrl,
        LOOKBACK_MONTHS: String(config.lookbackMonths),
      },
    });
    const retrievalAccess = new Policy(this, 'CaseRetrievalAccess', {
      roles: [props.retrievalRole],
      statements: [
        ...sharedStatements(),
        new PolicyStatement({
          sid: 'SendAnnotationReferences',
          actions: ['sqs:SendMessage'],
          resources: [props.annotationQueue.queueArn],
        }),
      ],
    });
    acknowledgeNagFinding(
      retrievalAccess,
      rawObjectsFinding,
      'Raw case objects are keyed by member account and case display ID discovered at run time; s3:PutObject is limited to the account_number= partitions of the dedicated raw bucket.',
    );
    this.retrievalFunction.function.addEventSource(
      new SqsEventSource(props.activeAccountsQueue, {
        batchSize: CASE_RETRIEVAL_BATCH_SIZE,
        maxConcurrency: CASE_COLLECTION_MAX_CONCURRENCY,
        reportBatchItemFailures: true,
      }),
    );

    this.annotationFunction = new PipelineFunction(this, 'CaseAnnotation', {
      config,
      functionName: names.caseAnnotationFunction,
      description: `Stores complete Support case conversations and queues them for summary (${config.uniqueId})`,
      handlerDir: handlerDirectory('case_annotation'),
      serviceName: 'case-annotation',
      layers: [commonLayer],
      timeout,
      role: props.annotationRole,
      environment: {
        ...memberEnvironment,
        SUMMARY_QUEUE_URL: props.summaryQueue.queueUrl,
        FAILURE_QUEUE_URL: props.failureQueue.queueUrl,
      },
    });
    const annotationAccess = new Policy(this, 'CaseAnnotationAccess', {
      roles: [props.annotationRole],
      statements: [
        ...sharedStatements(),
        new PolicyStatement({
          sid: 'SendSummaryReferences',
          actions: ['sqs:SendMessage'],
          resources: [props.summaryQueue.queueArn, props.failureQueue.queueArn],
        }),
        new PolicyStatement({
          sid: 'ReadTerminalMarkers',
          actions: ['s3:GetObject'],
          resources: [rawBucket.arnForObjects(TERMINAL_MARKER_PATTERN)],
        }),
        new PolicyStatement({
          sid: 'FindTerminalMarkers',
          actions: ['s3:ListBucket'],
          resources: [rawBucket.bucketArn],
          conditions: { StringLike: { 's3:prefix': [TERMINAL_MARKER_PATTERN] } },
        }),
      ],
    });
    acknowledgeNagFinding(
      annotationAccess,
      `AwsSolutions-IAM5[Resource::<${logicalIdOf(rawBucket)}.Arn>/${TERMINAL_MARKER_PATTERN}]`,
      'Annotation reads only the terminal-failure.json marker of a case partition to avoid re-reporting unchanged oversized conversations; keys are discovered at run time.',
    );
    acknowledgeNagFinding(
      annotationAccess,
      rawObjectsFinding,
      'Case conversations are keyed by member account and case display ID discovered at run time; s3:PutObject is limited to the account_number= partitions of the dedicated raw bucket.',
    );
    this.annotationFunction.function.addEventSource(
      new SqsEventSource(props.annotationQueue, {
        batchSize: CASE_ANNOTATION_BATCH_SIZE,
        maxConcurrency: CASE_COLLECTION_MAX_CONCURRENCY,
        reportBatchItemFailures: true,
      }),
    );
  }
}

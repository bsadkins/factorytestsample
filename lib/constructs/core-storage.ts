import { Duration, cx_api as cxapi } from 'aws-cdk-lib';
import { AttributeType, BillingMode, type ITable, Table, TableEncryption } from 'aws-cdk-lib/aws-dynamodb';
import {
  BlockPublicAccess,
  Bucket,
  BucketEncryption,
  type BucketProps,
  type IBucket,
  type LifecycleRule,
  ObjectOwnership,
  StorageClass,
  TransitionDefaultMinimumObjectSize,
} from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';
import type { CaseInsightsConfig } from '../config';
import { dataRemovalPolicy, isProduction } from './environment';

/** Attribute names of the Powertools for AWS Lambda (Python) DynamoDB persistence layer defaults. */
export const IDEMPOTENCY_TABLE_KEY = 'id';
export const IDEMPOTENCY_TABLE_TTL_ATTRIBUTE = 'expiration';

const INCOMPLETE_UPLOAD_DAYS = 7;

/** Typed storage handles shared with the pipeline constructs and optional stacks. */
export interface CoreStorageResources {
  /** Run-scoped account-list JSON objects written by account discovery. */
  readonly accountListBucket: IBucket;
  /** Raw case metadata, full conversations and stage checkpoints. */
  readonly rawBucket: IBucket;
  /** Authoritative completed case records read by Analytics and MCP. */
  readonly processedBucket: IBucket;
  /** Dedicated S3 server access log destination; it does not log to itself. */
  readonly accessLogsBucket: IBucket;
  /** Powertools idempotency records only; never a workflow ledger. */
  readonly idempotencyTable: ITable;
}

export interface CoreStorageProps {
  readonly config: CaseInsightsConfig;
}

/**
 * Buckets and the idempotency table for the core pipeline. Every bucket is private (BLOCK_ALL,
 * bucket-owner-enforced), SSL-only with TLS 1.2 minimum, versioned and SSE-S3 encrypted. Prod
 * retains data on stack deletion; dev deletes it, emptying buckets first.
 */
export class CoreStorage extends Construct implements CoreStorageResources {
  public readonly accountListBucket: Bucket;
  public readonly rawBucket: Bucket;
  public readonly processedBucket: Bucket;
  public readonly accessLogsBucket: Bucket;
  public readonly idempotencyTable: Table;

  public constructor(scope: Construct, id: string, props: CoreStorageProps) {
    super(scope, id);
    // Access-log delivery must be authorised by bucket policy (BUCKET_OWNER_ENFORCED disables
    // ACLs). Pinning the flag here keeps that true even in apps that do not load cdk.json.
    this.node.setContext(cxapi.S3_SERVER_ACCESS_LOGS_USE_BUCKET_POLICY, true);
    const { config } = props;
    const storage = config.operations.storage;
    const prod = isProduction(config);
    const removalPolicy = dataRemovalPolicy(config);
    const noncurrentVersionExpiration = Duration.days(storage.noncurrentVersionRetentionDays);

    const baseProps: BucketProps = {
      encryption: BucketEncryption.S3_MANAGED,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      objectOwnership: ObjectOwnership.BUCKET_OWNER_ENFORCED,
      enforceSSL: true,
      minimumTLSVersion: 1.2,
      versioned: true,
      removalPolicy,
      autoDeleteObjects: !prod,
    };

    // No server access logging on this bucket: it is the log destination, and logging to itself
    // would be recursive.
    this.accessLogsBucket = new Bucket(this, 'AccessLogsBucket', {
      ...baseProps,
      lifecycleRules: [
        {
          id: 'ExpireAccessLogs',
          expiration: Duration.days(storage.accessLogRetentionDays),
          noncurrentVersionExpiration,
          abortIncompleteMultipartUploadAfter: Duration.days(INCOMPLETE_UPLOAD_DAYS),
        },
      ],
    });

    const applicationBucket = (bucketId: string, logPrefix: string, lifecycleRules: LifecycleRule[]): Bucket =>
      new Bucket(this, bucketId, {
        ...baseProps,
        serverAccessLogsBucket: this.accessLogsBucket,
        serverAccessLogsPrefix: `${logPrefix}/`,
        lifecycleRules,
      });

    const rawExpiry: LifecycleRule = {
      expiration: Duration.days(config.rawRetentionDays),
      noncurrentVersionExpiration,
      abortIncompleteMultipartUploadAfter: Duration.days(INCOMPLETE_UPLOAD_DAYS),
    };

    this.accountListBucket = applicationBucket('AccountListBucket', 'account-list', [
      { id: 'ExpireAccountLists', ...rawExpiry },
    ]);
    this.rawBucket = applicationBucket('RawBucket', 'raw', [{ id: 'ExpireRawCaseData', ...rawExpiry }]);

    this.processedBucket = new Bucket(this, 'ProcessedBucket', {
      ...baseProps,
      serverAccessLogsBucket: this.accessLogsBucket,
      serverAccessLogsPrefix: 'processed/',
      // S3's default: objects under 128 KB are not transitioned, because Standard-IA bills them
      // as 128 KB. Stated explicitly so the cost behaviour of small case records is visible.
      transitionDefaultMinimumObjectSize: TransitionDefaultMinimumObjectSize.ALL_STORAGE_CLASSES_128_K,
      lifecycleRules: [
        {
          id: 'ProcessedToInfrequentAccess',
          transitions: [
            {
              storageClass: StorageClass.INFREQUENT_ACCESS,
              transitionAfter: Duration.days(storage.processedInfrequentAccessDays),
            },
          ],
          noncurrentVersionExpiration,
          abortIncompleteMultipartUploadAfter: Duration.days(INCOMPLETE_UPLOAD_DAYS),
        },
      ],
    });

    this.idempotencyTable = new Table(this, 'IdempotencyTable', {
      partitionKey: { name: IDEMPOTENCY_TABLE_KEY, type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: IDEMPOTENCY_TABLE_TTL_ATTRIBUTE,
      encryption: TableEncryption.DEFAULT,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      deletionProtection: prod,
      removalPolicy,
    });
  }
}

import { App, Stack } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import type { CfnBucket } from 'aws-cdk-lib/aws-s3';
import {
  type CaseInsightsConfig,
  CoreStorage,
  IDEMPOTENCY_TABLE_KEY,
  IDEMPOTENCY_TABLE_TTL_ATTRIBUTE,
} from '../../../lib';
import { nagApp, synthError, tempOutdir, testApp, testConfig, testStack } from '../../helpers/cdk';

interface BucketResource {
  DeletionPolicy?: string;
  UpdateReplacePolicy?: string;
  Properties: {
    BucketEncryption?: unknown;
    LoggingConfiguration?: { DestinationBucketName: { Ref: string }; LogFilePrefix: string };
    LifecycleConfiguration?: { Rules: Record<string, unknown>[] };
    VersioningConfiguration?: { Status: string };
    PublicAccessBlockConfiguration?: Record<string, boolean>;
    OwnershipControls?: unknown;
  };
}

function storageTemplate(config: CaseInsightsConfig): { template: Template; storage: CoreStorage } {
  const app = testApp();
  const stack = testStack(app, config);
  const storage = new CoreStorage(stack, 'Storage', { config });
  return { template: Template.fromStack(stack), storage };
}

function logicalId(storage: CoreStorage, bucket: 'accountListBucket' | 'rawBucket' | 'processedBucket' | 'accessLogsBucket'): string {
  return Stack.of(storage).getLogicalId(storage[bucket].node.defaultChild as CfnBucket);
}

describe('CoreStorage', () => {
  const devConfig = testConfig({ rawRetentionDays: 45, operations: { storage: { processedInfrequentAccessDays: 60, noncurrentVersionRetentionDays: 10, accessLogRetentionDays: 400 } } });
  const prodConfig = testConfig({ environment: 'prod', alarmEmail: 'alerts@example.com' });

  test('every bucket is private, SSE-S3, SSL/TLS1.2-only, versioned and bucket-owner enforced', () => {
    const { template } = storageTemplate(devConfig);
    const buckets = template.findResources('AWS::S3::Bucket') as Record<string, BucketResource>;
    expect(Object.keys(buckets)).toHaveLength(4);
    for (const bucket of Object.values(buckets)) {
      expect(bucket.Properties.BucketEncryption).toEqual({
        ServerSideEncryptionConfiguration: [{ ServerSideEncryptionByDefault: { SSEAlgorithm: 'AES256' } }],
      });
      expect(bucket.Properties.PublicAccessBlockConfiguration).toEqual({
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      });
      expect(bucket.Properties.VersioningConfiguration).toEqual({ Status: 'Enabled' });
      expect(bucket.Properties.OwnershipControls).toEqual({ Rules: [{ ObjectOwnership: 'BucketOwnerEnforced' }] });
    }

    const policies = template.findResources('AWS::S3::BucketPolicy') as Record<
      string,
      { Properties: { Bucket: { Ref: string }; PolicyDocument: { Statement: Record<string, unknown>[] } } }
    >;
    const policyByBucket = new Map(Object.values(policies).map((policy) => [policy.Properties.Bucket.Ref, policy]));
    for (const bucketId of Object.keys(buckets)) {
      const statements = policyByBucket.get(bucketId)?.Properties.PolicyDocument.Statement ?? [];
      expect(statements).toEqual(
        expect.arrayContaining([
          expect.objectContaining({ Effect: 'Deny', Action: 's3:*', Condition: { Bool: { 'aws:SecureTransport': 'false' } } }),
          expect.objectContaining({ Effect: 'Deny', Action: 's3:*', Condition: { NumericLessThan: { 's3:TlsVersion': 1.2 } } }),
        ]),
      );
    }
    template.resourceCountIs('AWS::KMS::Key', 0);
  });

  test('application buckets log to the dedicated access-log bucket, which does not log to itself', () => {
    const { template, storage } = storageTemplate(devConfig);
    const buckets = template.findResources('AWS::S3::Bucket') as Record<string, BucketResource>;
    const logsId = logicalId(storage, 'accessLogsBucket');
    expect(buckets[logsId]?.Properties.LoggingConfiguration).toBeUndefined();

    const expectedPrefixes = { accountListBucket: 'account-list/', rawBucket: 'raw/', processedBucket: 'processed/' } as const;
    for (const [bucket, prefix] of Object.entries(expectedPrefixes) as [keyof typeof expectedPrefixes, string][]) {
      expect(buckets[logicalId(storage, bucket)]?.Properties.LoggingConfiguration).toEqual({
        DestinationBucketName: { Ref: logsId },
        LogFilePrefix: prefix,
      });
    }

    // Log delivery is authorised by bucket policy for exactly the three source buckets.
    const logsPolicy = Object.values(
      template.findResources('AWS::S3::BucketPolicy', { Properties: { Bucket: { Ref: logsId } } }) as Record<
        string,
        { Properties: { PolicyDocument: { Statement: { Principal?: { Service?: string }; Condition?: unknown }[] } } }
      >,
    )[0];
    const delivery = logsPolicy?.Properties.PolicyDocument.Statement.filter(
      (statement) => statement.Principal?.Service === 'logging.s3.amazonaws.com',
    );
    expect(delivery).toHaveLength(3);
    for (const statement of delivery ?? []) {
      expect(statement.Condition).toEqual({
        ArnLike: { 'aws:SourceArn': { 'Fn::GetAtt': [expect.any(String), 'Arn'] } },
        StringEquals: { 'aws:SourceAccount': devConfig.managementAccountId },
      });
    }
  });

  test('lifecycle rules follow the configured retention and Standard-IA transition', () => {
    const { template, storage } = storageTemplate(devConfig);
    const buckets = template.findResources('AWS::S3::Bucket') as Record<string, BucketResource>;
    const rules = (bucket: Parameters<typeof logicalId>[1]) =>
      buckets[logicalId(storage, bucket)]?.Properties.LifecycleConfiguration?.Rules;

    const noncurrent = { NoncurrentDays: 10 };
    expect(rules('rawBucket')).toEqual([
      expect.objectContaining({ Id: 'ExpireRawCaseData', Status: 'Enabled', ExpirationInDays: 45, NoncurrentVersionExpiration: noncurrent }),
    ]);
    expect(rules('accountListBucket')).toEqual([
      expect.objectContaining({ Id: 'ExpireAccountLists', ExpirationInDays: 45, NoncurrentVersionExpiration: noncurrent }),
    ]);
    expect(rules('accessLogsBucket')).toEqual([
      expect.objectContaining({ Id: 'ExpireAccessLogs', ExpirationInDays: 400, NoncurrentVersionExpiration: noncurrent }),
    ]);
    const processed = rules('processedBucket');
    expect(processed).toEqual([
      expect.objectContaining({
        Id: 'ProcessedToInfrequentAccess',
        Transitions: [{ StorageClass: 'STANDARD_IA', TransitionInDays: 60 }],
        NoncurrentVersionExpiration: noncurrent,
      }),
    ]);
    // Completed records never expire automatically.
    expect(processed?.[0]).not.toHaveProperty('ExpirationInDays');
  });

  test('dev destroys and empties buckets; the idempotency table has no deletion protection', () => {
    const { template } = storageTemplate(devConfig);
    for (const bucket of Object.values(template.findResources('AWS::S3::Bucket'))) {
      expect(bucket).toMatchObject({ DeletionPolicy: 'Delete', UpdateReplacePolicy: 'Delete' });
    }
    template.resourceCountIs('Custom::S3AutoDeleteObjects', 4);
    template.hasResource('AWS::DynamoDB::Table', { DeletionPolicy: 'Delete', Properties: { DeletionProtectionEnabled: false } });
  });

  test('prod retains every bucket and the table, enables deletion protection and never auto-deletes', () => {
    const { template } = storageTemplate(prodConfig);
    for (const bucket of Object.values(template.findResources('AWS::S3::Bucket'))) {
      expect(bucket).toMatchObject({ DeletionPolicy: 'Retain', UpdateReplacePolicy: 'Retain' });
    }
    template.resourceCountIs('Custom::S3AutoDeleteObjects', 0);
    template.resourceCountIs('AWS::Lambda::Function', 0);
    template.hasResource('AWS::DynamoDB::Table', {
      DeletionPolicy: 'Retain',
      UpdateReplacePolicy: 'Retain',
      Properties: { DeletionProtectionEnabled: true },
    });
  });

  test('idempotency table matches the Powertools persistence layout with service-owned encryption', () => {
    const { template } = storageTemplate(devConfig);
    template.resourceCountIs('AWS::DynamoDB::Table', 1);
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      KeySchema: [{ AttributeName: IDEMPOTENCY_TABLE_KEY, KeyType: 'HASH' }],
      AttributeDefinitions: [{ AttributeName: IDEMPOTENCY_TABLE_KEY, AttributeType: 'S' }],
      BillingMode: 'PAY_PER_REQUEST',
      TimeToLiveSpecification: { AttributeName: IDEMPOTENCY_TABLE_TTL_ATTRIBUTE, Enabled: true },
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
      // SSEEnabled false selects the AWS owned key: encrypted at rest with no customer-managed key.
      SSESpecification: { SSEEnabled: false },
      GlobalSecondaryIndexes: Match.absent(),
    });
  });

  test('log delivery uses a bucket policy even when the app does not load cdk.json feature flags', () => {
    const app = new App({ outdir: tempOutdir() });
    const storage = new CoreStorage(testStack(app, devConfig), 'Storage', { config: devConfig });
    const template = Template.fromStack(Stack.of(storage));
    for (const bucket of Object.values(template.findResources('AWS::S3::Bucket')) as BucketResource[]) {
      expect(bucket.Properties).not.toHaveProperty('AccessControl');
      expect(bucket.Properties.OwnershipControls).toEqual({ Rules: [{ ObjectOwnership: 'BucketOwnerEnforced' }] });
    }
  });

  test.each([
    ['dev', devConfig],
    ['prod', prodConfig],
  ])('%s storage passes cdk-nag without acknowledgements', (_name, config) => {
    const app = nagApp();
    new CoreStorage(testStack(app, config), 'Storage', { config });
    expect(synthError(app)).toBeUndefined();
  });
});

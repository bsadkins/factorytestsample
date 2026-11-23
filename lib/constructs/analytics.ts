import { CfnOutput, Duration, Stack } from 'aws-cdk-lib';
import { CfnWorkGroup } from 'aws-cdk-lib/aws-athena';
import { CfnCrawler, CfnDatabase, CfnTable } from 'aws-cdk-lib/aws-glue';
import { Policy, PolicyStatement, Role, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import { BlockPublicAccess, Bucket, BucketEncryption, type IBucket, ObjectOwnership } from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';
import { type CaseInsightsConfig, resourceNames } from '../config';
import { acknowledgeCdkWarning, acknowledgeNagFinding, acknowledgeWildcardResourceFinding } from '../nag';
import { dataRemovalPolicy, isProduction } from './environment';

/**
 * Fields of a processed record (`common.processed_record.PROCESSED_FIELDS`), in stored order.
 * test/cdk/stacks/analytics-stack.test.ts fails if this list and the Python contract drift apart.
 */
export const PROCESSED_RECORD_FIELDS = [
  'caseId',
  'displayId',
  'subject',
  'serviceCode',
  'categoryCode',
  'severityCode',
  'submittedBy',
  'timeCreated',
  'Case_Retrieval_Date',
  'Case_Summary',
  'RCA_Category',
  'RCA_Reason',
  'RCA_Retrieval_Date',
  'Lifecycle_Category',
  'Lifecycle_Reason',
  'Lifecycle_Retrieval_Date',
  'Model_Id',
  'Prompt_Version',
] as const;

/** Hive partition keys of `account_number=<id>/case_number=<displayId>/data.json`, in path order. */
export const ANALYTICS_PARTITION_KEYS = ['account_number', 'case_number'] as const;

export const OPENX_JSON_SERDE = 'org.openx.data.jsonserde.JsonSerDe';
export const ATHENA_ENGINE_VERSION = 'Athena engine version 3';
/** Key prefix of query results inside the Athena results bucket. */
export const ATHENA_RESULTS_PREFIX = 'query-results/';
/** Prefix of the results bucket's server access logs in the Core access-log bucket. */
export const RESULTS_ACCESS_LOG_PREFIX = 'athena-results/';
/** Log group every Glue crawler writes to unless a security configuration renames it. */
export const GLUE_CRAWLER_LOG_GROUP = '/aws-glue/crawlers';

const PROCESSED_RECORDS_PATTERN = 'account_number=*';
const INCOMPLETE_UPLOAD_DAYS = 7;

/** Glue column name of a processed-record field; Glue and Athena store column names in lower case. */
export function analyticsColumnName(field: string): string {
  return field.toLowerCase();
}

/**
 * OpenX JSON SerDe properties. Lower-casing keys would lose the stored mixed-case names, so
 * matching is case-sensitive and every column maps explicitly to its exact JSON key.
 */
export function caseSummarySerdeParameters(): Record<string, string> {
  return {
    'case.insensitive': 'false',
    'ignore.malformed.json': 'false',
    'dots.in.keys': 'false',
    ...Object.fromEntries(PROCESSED_RECORD_FIELDS.map((field) => [`mapping.${analyticsColumnName(field)}`, field])),
  };
}

export interface AnalyticsProps {
  readonly config: CaseInsightsConfig;
  /** Core processed-record bucket: the table location, read by the crawler and queries. */
  readonly processedBucket: IBucket;
  /** Core server access-log bucket; receives the results bucket's logs under RESULTS_ACCESS_LOG_PREFIX. */
  readonly accessLogsBucket: IBucket;
}

/**
 * Glue database `case_insights_<id>` and OpenX JSON table `case_summary` over the processed
 * records, a daily crawler that adds new account/case partitions without changing the schema, and
 * the isolated Athena workgroup `CaseInsights-<id>` that enforces SSE-S3 results in its own
 * bucket and a per-query bytes-scanned cutoff. Glue and Athena have no stable L2 constructs, so
 * their L1 resources are used.
 */
export class Analytics extends Construct {
  public readonly database: CfnDatabase;
  public readonly table: CfnTable;
  public readonly crawler: CfnCrawler;
  public readonly crawlerRole: Role;
  public readonly crawlerAccess: Policy;
  public readonly workgroup: CfnWorkGroup;
  public readonly resultsBucket: Bucket;
  public readonly processedBucket: IBucket;
  public readonly databaseName: string;
  public readonly tableName: string;
  public readonly crawlerName: string;
  public readonly workgroupName: string;
  public readonly workgroupArn: string;
  /** `s3://<results bucket>/query-results/`, enforced for every query in the workgroup. */
  public readonly resultsLocation: string;

  public constructor(scope: Construct, id: string, props: AnalyticsProps) {
    super(scope, id);
    const { config, processedBucket, accessLogsBucket } = props;
    const names = resourceNames(config);
    const stack = Stack.of(this);
    this.processedBucket = processedBucket;
    this.databaseName = names.glueDatabase;
    this.tableName = names.glueTable;
    this.crawlerName = names.crawler;
    this.workgroupName = names.athenaWorkgroup;

    this.database = new CfnDatabase(this, 'Database', {
      catalogId: stack.account,
      databaseInput: {
        name: names.glueDatabase,
        description: `Support Case Insights processed case records (${config.uniqueId})`,
      },
    });

    this.table = new CfnTable(this, 'CaseSummaryTable', {
      catalogId: stack.account,
      databaseName: this.database.ref,
      tableInput: {
        name: names.glueTable,
        description: 'One enriched record per resolved Support case, partitioned by account and case.',
        tableType: 'EXTERNAL_TABLE',
        parameters: { classification: 'json', EXTERNAL: 'TRUE', typeOfData: 'file' },
        partitionKeys: ANALYTICS_PARTITION_KEYS.map((name) => ({ name, type: 'string' })),
        storageDescriptor: {
          location: `s3://${processedBucket.bucketName}/`,
          inputFormat: 'org.apache.hadoop.mapred.TextInputFormat',
          outputFormat: 'org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat',
          // Every field is a string (or null) in the stored record; dates stay ISO-8601 text.
          columns: PROCESSED_RECORD_FIELDS.map((field) => ({ name: analyticsColumnName(field), type: 'string' })),
          serdeInfo: { serializationLibrary: OPENX_JSON_SERDE, parameters: caseSummarySerdeParameters() },
        },
      },
    });
    this.table.addResourceDependency(this.database);

    // iam:PassRole is evaluated in the caller's account, so only principals here can attach this role to a crawler.
    this.crawlerRole = new Role(this, 'CrawlerRole', {
      assumedBy: new ServicePrincipal('glue.amazonaws.com'),
      description: `Glue crawler that adds partitions to ${names.glueDatabase}.${names.glueTable}`,
    });
    const glueArn = (resource: string, resourceName?: string): string =>
      stack.formatArn({ service: 'glue', resource, resourceName });
    const crawlerLogGroupArn = `arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:${GLUE_CRAWLER_LOG_GROUP}`;
    this.crawlerAccess = new Policy(this, 'CrawlerAccess', {
      roles: [this.crawlerRole],
      statements: [
        new PolicyStatement({
          sid: 'RefreshCaseSummaryPartitions',
          actions: [
            'glue:GetDatabase',
            'glue:GetTable',
            'glue:UpdateTable',
            'glue:GetPartition',
            'glue:GetPartitions',
            'glue:BatchGetPartition',
            'glue:CreatePartition',
            'glue:BatchCreatePartition',
            'glue:UpdatePartition',
            'glue:BatchUpdatePartition',
          ],
          resources: [glueArn('catalog'), glueArn('database', names.glueDatabase), glueArn('table', `${names.glueDatabase}/${names.glueTable}`)],
        }),
        new PolicyStatement({
          sid: 'ReadProcessedRecords',
          actions: ['s3:GetObject'],
          resources: [processedBucket.arnForObjects(PROCESSED_RECORDS_PATTERN)],
        }),
        new PolicyStatement({
          sid: 'ListProcessedRecords',
          actions: ['s3:ListBucket', 's3:GetBucketLocation'],
          resources: [processedBucket.bucketArn],
        }),
        new PolicyStatement({
          sid: 'WriteCrawlerLogs',
          actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
          resources: [crawlerLogGroupArn, `${crawlerLogGroupArn}:log-stream:*`],
        }),
      ],
    });
    acknowledgeWildcardResourceFinding(
      this.crawlerAccess,
      processedBucket.arnForObjects(PROCESSED_RECORDS_PATTERN),
      'The crawler reads processed records whose account and case partitions are created at run time; it has no write or delete access.',
    );
    acknowledgeWildcardResourceFinding(
      this.crawlerAccess,
      `${crawlerLogGroupArn}:log-stream:*`,
      'Glue names crawler log streams itself at run time; access is limited to streams of the fixed /aws-glue/crawlers log group.',
    );

    this.crawler = new CfnCrawler(this, 'PartitionCrawler', {
      name: names.crawler,
      description: `Adds new account/case partitions to ${names.glueDatabase}.${names.glueTable}`,
      role: this.crawlerRole.roleArn,
      schedule: { scheduleExpression: config.schedules.crawler },
      targets: { catalogTargets: [{ databaseName: this.database.ref, tables: [names.glueTable] }] },
      // Catalog-target crawlers must log deletions; logging updates keeps the declared schema authoritative.
      schemaChangePolicy: { updateBehavior: 'LOG', deleteBehavior: 'LOG' },
      configuration: JSON.stringify({ Version: 1.0, CrawlerOutput: { Partitions: { AddOrUpdateBehavior: 'InheritFromTable' } } }),
    });
    this.crawler.addResourceDependency(this.table);
    this.crawler.node.addDependency(this.crawlerAccess);
    acknowledgeNagFinding(
      this.crawler,
      'AwsSolutions-GL1',
      'Glue CloudWatch log encryption needs a customer-managed KMS key, which the approved AWS-managed encryption design excludes; crawler logs hold only object paths and partition names under CloudWatch default encryption.',
    );

    const prod = isProduction(config);
    // The results bucket cannot be referenced by a Core bucket policy without a Core -> Analytics
    // dependency cycle, so delivery is authorised on the Core bucket by prefix and account here and the
    // destination is passed to the results bucket as a reference (which adds no policy of its own).
    accessLogsBucket.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AllowAthenaResultsAccessLogs',
        principals: [new ServicePrincipal('logging.s3.amazonaws.com')],
        actions: ['s3:PutObject'],
        resources: [accessLogsBucket.arnForObjects(`${RESULTS_ACCESS_LOG_PREFIX}*`)],
        conditions: { StringEquals: { 'aws:SourceAccount': stack.account } },
      }),
    );
    this.resultsBucket = new Bucket(this, 'ResultsBucket', {
      encryption: BucketEncryption.S3_MANAGED,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      objectOwnership: ObjectOwnership.BUCKET_OWNER_ENFORCED,
      enforceSSL: true,
      minimumTLSVersion: 1.2,
      versioned: true,
      removalPolicy: dataRemovalPolicy(config),
      autoDeleteObjects: !prod,
      serverAccessLogsBucket: Bucket.fromBucketName(this, 'AccessLogsDestination', accessLogsBucket.bucketName),
      serverAccessLogsPrefix: RESULTS_ACCESS_LOG_PREFIX,
      lifecycleRules: [
        {
          id: 'ExpireQueryResults',
          expiration: Duration.days(config.operations.athena.resultsRetentionDays),
          noncurrentVersionExpiration: Duration.days(config.operations.storage.noncurrentVersionRetentionDays),
          abortIncompleteMultipartUploadAfter: Duration.days(INCOMPLETE_UPLOAD_DAYS),
        },
      ],
    });
    acknowledgeCdkWarning(
      this.resultsBucket,
      '@aws-cdk/aws-s3:accessLogsPolicyNotAdded',
      'Log delivery for the results bucket is granted on the Core access-log bucket policy (statement AllowAthenaResultsAccessLogs), scoped to the athena-results/ prefix and this account.',
    );

    this.resultsLocation = `s3://${this.resultsBucket.bucketName}/${ATHENA_RESULTS_PREFIX}`;
    this.workgroup = new CfnWorkGroup(this, 'Workgroup', {
      name: names.athenaWorkgroup,
      description: `Support Case Insights queries over ${names.glueDatabase} (${config.uniqueId})`,
      state: 'ENABLED',
      // Query history and saved queries are not case data; results live in the results bucket.
      recursiveDeleteOption: true,
      workGroupConfiguration: {
        enforceWorkGroupConfiguration: true,
        publishCloudWatchMetricsEnabled: true,
        requesterPaysEnabled: false,
        bytesScannedCutoffPerQuery: config.operations.athena.bytesScannedCutoffPerQuery,
        engineVersion: { selectedEngineVersion: ATHENA_ENGINE_VERSION },
        resultConfiguration: {
          outputLocation: this.resultsLocation,
          encryptionConfiguration: { encryptionOption: 'SSE_S3' },
          expectedBucketOwner: stack.account,
        },
      },
    });
    this.workgroupArn = stack.formatArn({ service: 'athena', resource: 'workgroup', resourceName: names.athenaWorkgroup });

    new CfnOutput(this, 'GlueDatabaseNameOutput', { key: 'GlueDatabaseName', value: this.database.ref, description: 'Glue database of the case_summary table' });
    new CfnOutput(this, 'GlueTableNameOutput', { key: 'GlueTableName', value: names.glueTable, description: 'Athena table of processed case records' });
    new CfnOutput(this, 'CrawlerNameOutput', { key: 'CrawlerName', value: names.crawler, description: 'Glue crawler that adds new account/case partitions' });
    new CfnOutput(this, 'CrawlerScheduleOutput', { key: 'CrawlerSchedule', value: config.schedules.crawler, description: 'Crawler schedule (UTC); new records are queryable after the next crawl' });
    new CfnOutput(this, 'ManualCrawlerStartOutput', {
      key: 'ManualCrawlerStartCommand',
      value: `aws glue start-crawler --name ${names.crawler} --region ${stack.region}`,
      description: 'Run the crawler now to make newly processed cases visible in Athena before the next scheduled crawl',
    });
    new CfnOutput(this, 'AthenaWorkgroupNameOutput', { key: 'AthenaWorkgroupName', value: names.athenaWorkgroup, description: 'Athena workgroup that enforces results encryption and the scan cutoff' });
    new CfnOutput(this, 'AthenaResultsBucketNameOutput', { key: 'AthenaResultsBucketName', value: this.resultsBucket.bucketName, description: 'SSE-S3 bucket holding Athena query results' });
  }
}

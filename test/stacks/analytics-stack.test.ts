import { execFileSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { type App, CfnElement, type Stack } from 'aws-cdk-lib';
import { Annotations, Match, Template } from 'aws-cdk-lib/assertions';
import {
  addCaseInsightsStacks,
  ANALYTICS_PARTITION_KEYS,
  ATHENA_ENGINE_VERSION,
  ATHENA_RESULTS_PREFIX,
  bundlingPython,
  type CaseInsightsAppStacks,
  CaseInsightsAnalyticsStack,
  ConfigValidationError,
  OPENX_JSON_SERDE,
  PROCESSED_RECORD_FIELDS,
  REPO_ROOT,
  RESULTS_ACCESS_LOG_PREFIX,
  resourceNames,
} from '../../lib';
import type { IConstruct } from 'constructs';
import { type JsonObject, readConfigJson } from '../config/helpers';
import { normaliseAssetHashes } from '../helpers/assets';
import { acknowledgedRules, iamPolicyStatements, synthError, tempOutdir, testApp, trustStatements } from '../helpers/cdk';

type Env = 'dev' | 'prod';

/** A config directory holding config/<env>.json with top-level overrides; validated by the real loader. */
function configDirWith(env: Env, overrides: JsonObject): string {
  const dir = tempOutdir();
  fs.writeFileSync(path.join(dir, `${env}.json`), JSON.stringify({ ...readConfigJson(`${env}.json`), ...overrides }));
  return dir;
}

function stacksFor(env: Env, overrides?: JsonObject, app: App = testApp()): CaseInsightsAppStacks {
  return addCaseInsightsStacks(app, { env, configDir: overrides === undefined ? undefined : configDirWith(env, overrides) });
}

function analyticsOf(stacks: CaseInsightsAppStacks): CaseInsightsAnalyticsStack {
  if (stacks.analytics === undefined) {
    throw new Error('Analytics stack missing');
  }
  return stacks.analytics;
}

interface GetStackOutput {
  'Fn::GetStackOutput': { StackName: string; OutputName: string };
}

/** Resolves a weak cross-stack reference to the value of the Core output it reads. */
function coreOutputValue(core: Stack, value: unknown): unknown {
  const ref = (value as Partial<GetStackOutput>)['Fn::GetStackOutput'];
  if (ref === undefined) {
    throw new Error(`not a cross-stack reference: ${JSON.stringify(value)}`);
  }
  expect(ref.StackName).toBe(core.stackName);
  const outputs = Template.fromStack(core).toJSON().Outputs as Record<string, { Value: unknown }>;
  return outputs[ref.OutputName]?.Value;
}

/** Ref to an L1 resource, or to the default resource of an L2 construct. */
function refTo(stack: Stack, construct: IConstruct): { Ref: string } {
  const element = CfnElement.isCfnElement(construct) ? construct : construct.node.defaultChild;
  if (!CfnElement.isCfnElement(element)) {
    throw new Error(`no CloudFormation resource for ${construct.node.path}`);
  }
  return { Ref: stack.getLogicalId(element) };
}

const MCP_OFF = { enableAnalytics: true, enableMcp: false } as const;
const ALL_OFF = { enableAnalytics: false, enableMcp: false } as const;

describe('Analytics feature flag', () => {
  test('default dev configuration adds CaseInsightsAnalyticsStack-<id> after Core', () => {
    const stacks = stacksFor('dev');
    const analytics = analyticsOf(stacks);
    expect(analytics).toBeInstanceOf(CaseInsightsAnalyticsStack);
    expect(analytics.stackName).toBe(`CaseInsightsAnalyticsStack-${stacks.config.uniqueId}`);
    expect(analytics.account).toBe(stacks.config.managementAccountId);
    expect(analytics.region).toBe(stacks.config.region);
    expect(analytics.dependencies).toContain(stacks.core);
    expect(stacks.core.dependencies).not.toContain(analytics);
  });

  test.each(['dev', 'prod'] as const)('%s Analytics-on/MCP-off synthesises with cdk-nag and no cycle', (env) => {
    const app = testApp();
    const stacks = stacksFor(env, MCP_OFF, app);
    expect(stacks.config.enableMcp).toBe(false);
    const analytics = analyticsOf(stacks);
    let assembly: ReturnType<App['synth']> | undefined;
    expect(() => {
      assembly = app.synth();
    }).not.toThrow();
    const names = resourceNames(stacks.config);
    expect(assembly?.stacks.map((stack) => stack.stackName).sort()).toEqual(
      [names.coreStack, names.memberRoleStack, names.analyticsStack].sort(),
    );
    Annotations.fromStack(analytics).hasNoError('*', Match.anyValue());
    // Core never reads Analytics outputs, so Core can be deployed and updated on its own.
    expect(JSON.stringify(Template.fromStack(stacks.core).toJSON())).not.toContain(names.analyticsStack);
  });

  test.each(['dev', 'prod'] as const)('%s with Analytics disabled creates no analytics resources anywhere', (env) => {
    const app = testApp();
    const stacks = stacksFor(env, ALL_OFF, app);
    expect(stacks.analytics).toBeUndefined();
    const assembly = app.synth();
    expect(assembly.stacks.map((stack) => stack.stackName)).not.toContain(resourceNames(stacks.config).analyticsStack);
    for (const stack of [stacks.core, stacks.memberRole]) {
      const template = JSON.stringify(Template.fromStack(stack).toJSON());
      expect(template).not.toMatch(/AWS::Glue::|AWS::Athena::/);
      expect(template).not.toContain(RESULTS_ACCESS_LOG_PREFIX);
    }
  });

  test('MCP without Analytics is rejected before any stack is created', () => {
    const app = testApp();
    const configDir = configDirWith('dev', { enableAnalytics: false, enableMcp: true });
    expect(() => addCaseInsightsStacks(app, { env: 'dev', configDir })).toThrow(ConfigValidationError);
    expect(app.node.children).toHaveLength(0);
  });

  test.each([
    ['bytes-scanned cutoff below the Athena minimum', { athena: { bytesScannedCutoffPerQuery: 1048576 } }],
    ['zero-day results retention', { athena: { resultsRetentionDays: 0 } }],
  ])('invalid Analytics limits are rejected: %s', (_name, operations) => {
    const app = testApp();
    const configDir = configDirWith('dev', { ...MCP_OFF, operations });
    expect(() => addCaseInsightsStacks(app, { env: 'dev', configDir })).toThrow(ConfigValidationError);
    expect(app.node.children).toHaveLength(0);
  });
});

describe.each(['dev', 'prod'] as const)('%s Analytics stack', (env) => {
  let stacks: CaseInsightsAppStacks;
  let analytics: CaseInsightsAnalyticsStack;
  let template: Template;

  beforeAll(() => {
    stacks = stacksFor(env, MCP_OFF);
    analytics = analyticsOf(stacks);
    template = Template.fromStack(analytics);
  });

  test('Glue database case_insights_<id> in the deployment account catalog', () => {
    template.resourceCountIs('AWS::Glue::Database', 1);
    template.hasResourceProperties('AWS::Glue::Database', {
      CatalogId: stacks.config.managementAccountId,
      DatabaseInput: { Name: `case_insights_${stacks.config.uniqueId}` },
    });
  });

  test('case_summary is an OpenX JSON external table over the processed bucket with mixed-case mappings', () => {
    const tables = Object.values(template.findResources('AWS::Glue::Table'));
    expect(tables).toHaveLength(1);
    const props = (tables[0] as { Properties: Record<string, unknown> }).Properties as {
      CatalogId: string;
      DatabaseName: unknown;
      TableInput: {
        Name: string;
        TableType: string;
        Parameters: Record<string, string>;
        PartitionKeys: { Name: string; Type: string }[];
        StorageDescriptor: {
          Location: { 'Fn::Join': [string, unknown[]] };
          InputFormat: string;
          OutputFormat: string;
          Columns: { Name: string; Type: string }[];
          SerdeInfo: { SerializationLibrary: string; Parameters: Record<string, string> };
        };
      };
    };
    expect(props.CatalogId).toBe(stacks.config.managementAccountId);
    expect(props.DatabaseName).toEqual(refTo(analytics, analytics.analytics.database));
    const input = props.TableInput;
    expect(input.Name).toBe('case_summary');
    expect(input.TableType).toBe('EXTERNAL_TABLE');
    expect(input.Parameters).toMatchObject({ classification: 'json', EXTERNAL: 'TRUE' });
    expect(input.PartitionKeys).toEqual([
      { Name: 'account_number', Type: 'string' },
      { Name: 'case_number', Type: 'string' },
    ]);

    const sd = input.StorageDescriptor;
    expect(sd.InputFormat).toBe('org.apache.hadoop.mapred.TextInputFormat');
    expect(sd.OutputFormat).toBe('org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat');
    const [separator, parts] = sd.Location['Fn::Join'];
    expect(separator).toBe('');
    expect(parts).toHaveLength(3);
    expect(parts[0]).toBe('s3://');
    expect(parts[2]).toBe('/');
    expect(coreOutputValue(stacks.core, parts[1])).toEqual(refTo(stacks.core, stacks.core.storage.processedBucket));

    expect(sd.Columns).toEqual(PROCESSED_RECORD_FIELDS.map((field) => ({ Name: field.toLowerCase(), Type: 'string' })));
    expect(sd.SerdeInfo.SerializationLibrary).toBe('org.openx.data.jsonserde.JsonSerDe');
    expect(sd.SerdeInfo.Parameters).toEqual({
      'case.insensitive': 'false',
      'ignore.malformed.json': 'false',
      'dots.in.keys': 'false',
      ...Object.fromEntries(PROCESSED_RECORD_FIELDS.map((field) => [`mapping.${field.toLowerCase()}`, field])),
    });
  });

  test('crawler refreshes partitions of the existing table daily at 09:00 UTC without changing its schema', () => {
    template.resourceCountIs('AWS::Glue::Crawler', 1);
    const crawler = Object.values(template.findResources('AWS::Glue::Crawler'))[0] as {
      DependsOn?: string[];
      Properties: Record<string, unknown> & { Configuration: string; Role: { 'Fn::GetAtt': [string, string] } };
    };
    expect(crawler.Properties).toMatchObject({
      Name: `case-insights-partition-crawler-${stacks.config.uniqueId}`,
      Schedule: { ScheduleExpression: 'cron(0 9 * * ? *)' },
      Targets: { CatalogTargets: [{ DatabaseName: refTo(analytics, analytics.analytics.database), Tables: ['case_summary'] }] },
      SchemaChangePolicy: { UpdateBehavior: 'LOG', DeleteBehavior: 'LOG' },
    });
    expect(JSON.parse(crawler.Properties.Configuration)).toEqual({
      Version: 1,
      CrawlerOutput: { Partitions: { AddOrUpdateBehavior: 'InheritFromTable' } },
    });
    const roleId = analytics.getLogicalId(analytics.analytics.crawlerRole.node.defaultChild as never);
    expect(crawler.Properties.Role).toEqual({ 'Fn::GetAtt': [roleId, 'Arn'] });
    // The crawler waits for its table and permissions.
    expect(crawler.DependsOn).toEqual(
      expect.arrayContaining([
        analytics.getLogicalId(analytics.analytics.table),
        analytics.getLogicalId(analytics.analytics.crawlerAccess.node.defaultChild as never),
      ]),
    );
  });

  test('crawler role is trusted only by Glue and can only read processed records and refresh this table', () => {
    const roleId = analytics.getLogicalId(analytics.analytics.crawlerRole.node.defaultChild as never);
    expect(trustStatements(template, roleId)).toEqual([
      { Action: 'sts:AssumeRole', Effect: 'Allow', Principal: { Service: 'glue.amazonaws.com' } },
    ]);
    template.hasResourceProperties('AWS::IAM::Role', { ManagedPolicyArns: Match.absent() });
    const { managementAccountId: account, region, uniqueId } = stacks.config;
    const db = `case_insights_${uniqueId}`;
    const statements = iamPolicyStatements(template).filter(({ roles }) => JSON.stringify(roles).includes(roleId));
    expect(statements.map(({ statement }) => statement.Sid).sort()).toEqual(
      ['ListProcessedRecords', 'ReadProcessedRecords', 'RefreshCaseSummaryPartitions', 'WriteCrawlerLogs'].sort(),
    );
    const bySid = new Map(statements.map(({ statement }) => [statement.Sid, statement]));
    const glue = bySid.get('RefreshCaseSummaryPartitions');
    expect(glue?.Effect).toBe('Allow');
    expect([glue?.Action].flat().sort()).toEqual(
      [
        'glue:BatchCreatePartition',
        'glue:BatchGetPartition',
        'glue:BatchUpdatePartition',
        'glue:CreatePartition',
        'glue:GetDatabase',
        'glue:GetPartition',
        'glue:GetPartitions',
        'glue:GetTable',
        'glue:UpdatePartition',
        'glue:UpdateTable',
      ].sort(),
    );
    expect([glue?.Resource].flat().sort()).toEqual(
      [
        `arn:aws:glue:${region}:${account}:catalog`,
        `arn:aws:glue:${region}:${account}:database/${db}`,
        `arn:aws:glue:${region}:${account}:table/${db}/case_summary`,
      ].sort(),
    );
    const processedArn = { 'Fn::GetAtt': [stacks.core.getLogicalId(stacks.core.storage.processedBucket.node.defaultChild as never), 'Arn'] };
    const read = bySid.get('ReadProcessedRecords');
    expect(read?.Action).toBe('s3:GetObject');
    const readResource = read?.Resource as { 'Fn::Join': [string, unknown[]] };
    expect(coreOutputValue(stacks.core, readResource['Fn::Join'][1][0])).toEqual(processedArn);
    expect(readResource['Fn::Join'][1][1]).toBe('/account_number=*');
    const list = bySid.get('ListProcessedRecords');
    expect([list?.Action].flat().sort()).toEqual(['s3:GetBucketLocation', 's3:ListBucket']);
    expect(coreOutputValue(stacks.core, list?.Resource)).toEqual(processedArn);
    const logs = bySid.get('WriteCrawlerLogs');
    expect([logs?.Action].flat().sort()).toEqual(['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents']);
    expect([logs?.Resource].flat().sort()).toEqual(
      [
        `arn:aws:logs:${region}:${account}:log-group:/aws-glue/crawlers`,
        `arn:aws:logs:${region}:${account}:log-group:/aws-glue/crawlers:log-stream:*`,
      ].sort(),
    );
    // Nothing in the Analytics stack can write or delete processed records.
    const actions = iamPolicyStatements(template).flatMap(({ statement }) => [statement.Action].flat());
    expect(actions.filter((action) => typeof action === 'string' && /^s3:(Put|Delete)/.test(action))).toEqual([]);
  });

  test('workgroup CaseInsights-<id> enforces its SSE-S3 results location, engine and bytes-scanned cutoff', () => {
    template.resourceCountIs('AWS::Athena::WorkGroup', 1);
    const workgroup = Object.values(template.findResources('AWS::Athena::WorkGroup'))[0] as {
      Properties: {
        Name: string;
        State: string;
        WorkGroupConfiguration: {
          EnforceWorkGroupConfiguration: boolean;
          PublishCloudWatchMetricsEnabled: boolean;
          RequesterPaysEnabled: boolean;
          BytesScannedCutoffPerQuery: number;
          EngineVersion: { SelectedEngineVersion: string };
          ResultConfiguration: {
            OutputLocation: { 'Fn::Join': [string, unknown[]] };
            EncryptionConfiguration: { EncryptionOption: string; KmsKey?: string };
            ExpectedBucketOwner: string;
          };
        };
      };
    };
    const props = workgroup.Properties;
    expect(props.Name).toBe(`CaseInsights-${stacks.config.uniqueId}`);
    expect(props.State).toBe('ENABLED');
    const wg = props.WorkGroupConfiguration;
    expect(wg.EnforceWorkGroupConfiguration).toBe(true);
    expect(wg.PublishCloudWatchMetricsEnabled).toBe(true);
    expect(wg.RequesterPaysEnabled).toBe(false);
    expect(wg.BytesScannedCutoffPerQuery).toBe(stacks.config.operations.athena.bytesScannedCutoffPerQuery);
    expect(wg.EngineVersion).toEqual({ SelectedEngineVersion: ATHENA_ENGINE_VERSION });
    expect(wg.ResultConfiguration.EncryptionConfiguration).toEqual({ EncryptionOption: 'SSE_S3' });
    expect(wg.ResultConfiguration.ExpectedBucketOwner).toBe(stacks.config.managementAccountId);
    expect(wg.ResultConfiguration.OutputLocation).toEqual({
      'Fn::Join': ['', ['s3://', refTo(analytics, analytics.analytics.resultsBucket), `/${ATHENA_RESULTS_PREFIX}`]],
    });
    template.resourceCountIs('AWS::KMS::Key', 0);
  });

  test('results bucket is private, SSL-only, SSE-S3, versioned, logged and expires results', () => {
    const buckets = template.findResources('AWS::S3::Bucket');
    expect(Object.keys(buckets)).toEqual([analytics.getLogicalId(analytics.analytics.resultsBucket.node.defaultChild as never)]);
    const bucket = Object.values(buckets)[0] as { DeletionPolicy: string; Properties: Record<string, unknown> };
    expect(bucket.DeletionPolicy).toBe(env === 'prod' ? 'Retain' : 'Delete');
    expect(bucket.Properties).toMatchObject({
      BucketEncryption: { ServerSideEncryptionConfiguration: [{ ServerSideEncryptionByDefault: { SSEAlgorithm: 'AES256' } }] },
      PublicAccessBlockConfiguration: { BlockPublicAcls: true, BlockPublicPolicy: true, IgnorePublicAcls: true, RestrictPublicBuckets: true },
      VersioningConfiguration: { Status: 'Enabled' },
      OwnershipControls: { Rules: [{ ObjectOwnership: 'BucketOwnerEnforced' }] },
      LifecycleConfiguration: {
        Rules: [
          expect.objectContaining({
            Id: 'ExpireQueryResults',
            Status: 'Enabled',
            ExpirationInDays: stacks.config.operations.athena.resultsRetentionDays,
            NoncurrentVersionExpiration: { NoncurrentDays: stacks.config.operations.storage.noncurrentVersionRetentionDays },
          }),
        ],
      },
    });
    const logging = bucket.Properties.LoggingConfiguration as { DestinationBucketName: unknown; LogFilePrefix: string };
    expect(logging.LogFilePrefix).toBe(RESULTS_ACCESS_LOG_PREFIX);
    expect(coreOutputValue(stacks.core, logging.DestinationBucketName)).toEqual(refTo(stacks.core, stacks.core.storage.accessLogsBucket));

    const policy = Object.values(template.findResources('AWS::S3::BucketPolicy'))[0] as {
      Properties: { PolicyDocument: { Statement: Record<string, unknown>[] } };
    };
    expect(policy.Properties.PolicyDocument.Statement).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ Effect: 'Deny', Action: 's3:*', Principal: { AWS: '*' }, Condition: { Bool: { 'aws:SecureTransport': 'false' } } }),
        expect.objectContaining({ Effect: 'Deny', Action: 's3:*', Condition: { NumericLessThan: { 's3:TlsVersion': 1.2 } } }),
      ]),
    );
    expect(policy.Properties.PolicyDocument.Statement.filter((statement) => statement.Effect === 'Allow' && JSON.stringify(statement.Principal).includes('"*"'))).toEqual([]);
    template.resourceCountIs('Custom::S3AutoDeleteObjects', env === 'prod' ? 0 : 1);
  });

  test('Core access-log bucket accepts results-bucket logs only under its own prefix from this account', () => {
    const coreTemplate = Template.fromStack(stacks.core);
    const logsRef = refTo(stacks.core, stacks.core.storage.accessLogsBucket);
    const policy = Object.values(coreTemplate.findResources('AWS::S3::BucketPolicy', { Properties: { Bucket: logsRef } }))[0] as {
      Properties: { PolicyDocument: { Statement: { Sid?: string; Principal?: { Service?: string }; Resource?: unknown; Condition?: unknown; Action?: unknown }[] } };
    };
    const delivery = policy.Properties.PolicyDocument.Statement.filter((statement) =>
      JSON.stringify(statement.Resource).includes(`/${RESULTS_ACCESS_LOG_PREFIX}*`),
    );
    expect(delivery).toEqual([
      {
        Sid: 'AllowAthenaResultsAccessLogs',
        Effect: 'Allow',
        Principal: { Service: 'logging.s3.amazonaws.com' },
        Action: 's3:PutObject',
        Resource: { 'Fn::Join': ['', [{ 'Fn::GetAtt': [logsRef.Ref, 'Arn'] }, `/${RESULTS_ACCESS_LOG_PREFIX}*`]] },
        Condition: { StringEquals: { 'aws:SourceAccount': stacks.config.managementAccountId } },
      },
    ]);
  });

  test('outputs name the Glue, crawler and workgroup resources and the manual refresh command', () => {
    const { uniqueId, region } = stacks.config;
    template.hasOutput('GlueDatabaseName', { Value: { Ref: analytics.getLogicalId(analytics.analytics.database) } });
    template.hasOutput('GlueTableName', { Value: 'case_summary' });
    template.hasOutput('CrawlerName', { Value: `case-insights-partition-crawler-${uniqueId}` });
    template.hasOutput('CrawlerSchedule', { Value: 'cron(0 9 * * ? *)' });
    template.hasOutput('ManualCrawlerStartCommand', {
      Value: `aws glue start-crawler --name case-insights-partition-crawler-${uniqueId} --region ${region}`,
    });
    template.hasOutput('AthenaWorkgroupName', { Value: `CaseInsights-${uniqueId}` });
    template.hasOutput('AthenaResultsBucketName', { Value: refTo(analytics, analytics.analytics.resultsBucket) });
  });

  test('every cdk-nag acknowledgement in the Analytics stack is listed with a specific reason', () => {
    const acknowledged = acknowledgedRules(analytics).map(({ path: constructPath, id, reason }) => ({
      path: constructPath.replace(`${analytics.stackName}/`, ''),
      id: id.replace(/^.*::(?=AwsSolutions-)/, ''),
      reason,
    }));
    // cdk-nag renders the weak cross-stack reference to the processed bucket ARN as its JSON intrinsic.
    const read = iamPolicyStatements(template).find(({ statement }) => statement.Sid === 'ReadProcessedRecords');
    const processedArnInFinding = JSON.stringify((read?.statement.Resource as { 'Fn::Join': [string, unknown[]] })['Fn::Join'][1][0]);
    expect(processedArnInFinding).toContain('Fn::GetStackOutput');
    expect(acknowledged.map(({ path: constructPath, id }) => [constructPath, id]).sort()).toEqual(
      [
        ['Analytics/CrawlerAccess', `AwsSolutions-IAM5[Resource::${processedArnInFinding}/account_number=*]`],
        ['Analytics/ResultsBucket', 'Annotation::@aws-cdk/aws-s3:accessLogsPolicyNotAdded'],
        ['Analytics/CrawlerAccess', `AwsSolutions-IAM5[Resource::arn:aws:logs:${stacks.config.region}:${stacks.config.managementAccountId}:log-group:/aws-glue/crawlers:log-stream:*]`],
        ['Analytics/PartitionCrawler', 'AwsSolutions-GL1'],
      ].sort(),
    );
    for (const { reason } of acknowledged) {
      expect(reason.length).toBeGreaterThanOrEqual(40);
    }
  });

  test('template snapshot', () => {
    expect(normaliseAssetHashes(template.toJSON())).toMatchSnapshot();
  });
});

describe('configured Analytics limits', () => {
  test('crawler schedule, bytes-scanned cutoff and results retention follow the configuration', () => {
    const stacks = stacksFor('dev', {
      ...MCP_OFF,
      schedules: { crawler: 'cron(30 7 * * ? *)' },
      operations: { athena: { bytesScannedCutoffPerQuery: 52428800, resultsRetentionDays: 7 } },
    });
    const template = Template.fromStack(analyticsOf(stacks));
    template.hasResourceProperties('AWS::Glue::Crawler', { Schedule: { ScheduleExpression: 'cron(30 7 * * ? *)' } });
    template.hasResourceProperties('AWS::Athena::WorkGroup', {
      WorkGroupConfiguration: Match.objectLike({ BytesScannedCutoffPerQuery: 52428800 }),
    });
    template.hasResourceProperties('AWS::S3::Bucket', {
      LifecycleConfiguration: { Rules: [Match.objectLike({ Id: 'ExpireQueryResults', ExpirationInDays: 7 })] },
    });
    template.hasOutput('CrawlerSchedule', { Value: 'cron(30 7 * * ? *)' });
  });

  test('a uniqueId with hyphens yields a valid Glue database name and matching outputs', () => {
    const app = testApp();
    const stacks = stacksFor('dev', { ...MCP_OFF, uniqueId: 'sci-dev2' }, app);
    const template = Template.fromStack(analyticsOf(stacks));
    template.hasResourceProperties('AWS::Glue::Database', { DatabaseInput: { Name: 'case_insights_sci_dev2' } });
    template.hasResourceProperties('AWS::Athena::WorkGroup', { Name: 'CaseInsights-sci-dev2' });
    expect(synthError(app)).toBeUndefined();
  });
});

describe('processed-record schema parity', () => {
  interface PythonSchema {
    fields: string[];
    key: string;
    parsed: [string, string];
  }

  // The runtime record contract lives in Python; the Glue table must describe exactly those records.
  const python = (): PythonSchema => {
    const script = [
      'import json, sys',
      `sys.path.insert(0, ${JSON.stringify(path.join(REPO_ROOT, 'src', 'lambda'))})`,
      'from common.processed import case_record_key, parse_case_record_key',
      'from common.processed_record import PROCESSED_FIELDS',
      "key = case_record_key('123456789012', '170000000012345')",
      "print(json.dumps({'fields': list(PROCESSED_FIELDS), 'key': key, 'parsed': list(parse_case_record_key(key))}))",
    ].join('\n');
    return JSON.parse(execFileSync(bundlingPython(), ['-I', '-c', script], { encoding: 'utf8' })) as PythonSchema;
  };

  test('table columns are exactly the processed-record fields, in order', () => {
    expect([...PROCESSED_RECORD_FIELDS]).toEqual(python().fields);
  });

  test('the S3 record key is the Hive layout of the partition keys', () => {
    const { key, parsed } = python();
    expect(key).toBe(`${ANALYTICS_PARTITION_KEYS[0]}=${parsed[0]}/${ANALYTICS_PARTITION_KEYS[1]}=${parsed[1]}/data.json`);
  });

  test('OpenX mappings read every key of a synthetic processed record and no column collides when lower-cased', () => {
    const stacks = stacksFor('dev', MCP_OFF);
    const table = Object.values(Template.fromStack(analyticsOf(stacks)).findResources('AWS::Glue::Table'))[0] as {
      Properties: { TableInput: { StorageDescriptor: { Columns: { Name: string }[]; SerdeInfo: { SerializationLibrary: string; Parameters: Record<string, string> } } } };
    };
    const sd = table.Properties.TableInput.StorageDescriptor;
    expect(sd.SerdeInfo.SerializationLibrary).toBe(OPENX_JSON_SERDE);
    const record: Record<string, string | null> = Object.fromEntries(python().fields.map((field, index) => [field, `value-${String(index)}`]));
    record.subject = null;
    // With case.insensitive=false OpenX looks up each column through mapping.<column> in the raw JSON keys.
    const row = sd.Columns.map(({ Name: name }) => {
      const jsonKey = sd.SerdeInfo.Parameters[`mapping.${name}`];
      if (jsonKey === undefined) {
        throw new Error(`no OpenX mapping for column ${name}`);
      }
      return [name, record[jsonKey]];
    });
    expect(row.map(([, value]) => value)).toEqual(Object.values(record));
    expect(new Set(sd.Columns.map(({ Name }) => Name)).size).toBe(sd.Columns.length);
  });
});

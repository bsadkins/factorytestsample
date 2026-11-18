import * as fs from 'node:fs';
import * as path from 'node:path';
import type { Stack } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { addCaseInsightsStacks, type CaseInsightsAppStacks, REPO_ROOT } from '../../lib';
import { asArray, iamPolicyStatements, testApp } from '../helpers/cdk';

/**
 * The shipped dev app (Analytics and MCP on) against the shared cross-surface fixtures that
 * test/integration/test_cross_surface_*.py feed through S3, the CSV exporter, the MCP handler and
 * the stdio bridge. These assertions tie the local evidence to the synthesised Glue table and the
 * MCP function's query scope; they prove template properties, not deployed Glue or Athena behaviour.
 */

interface Column {
  name: string;
  type: string;
}

interface TableContract {
  tableName: string;
  serializationLibrary: string;
  columns: Column[];
  partitionKeys: Column[];
  recordKeyTemplate: string;
  serdeParameters: Record<string, string>;
}

interface FixtureRecord {
  accountNumber: string;
  record: Record<string, string | null>;
}

interface GlueTableProperties {
  DatabaseName: unknown;
  TableInput: {
    Name: string;
    TableType: string;
    PartitionKeys: { Name: string; Type: string }[];
    StorageDescriptor: {
      Columns: { Name: string; Type: string }[];
      SerdeInfo: { SerializationLibrary: string; Parameters: Record<string, string> };
    };
  };
}

const FIXTURES = path.join(REPO_ROOT, 'test', 'fixtures', 'cross_surface');
const readFixture = (name: string): unknown => JSON.parse(fs.readFileSync(path.join(FIXTURES, name), 'utf8'));
const contract = readFixture('case_summary_table.json') as TableContract;
const fixtureRecords = readFixture('processed_records.json') as FixtureRecord[];

function onlyResource(template: Template, type: string): { id: string; properties: unknown } {
  const resources = Object.entries(template.findResources(type));
  expect(resources).toHaveLength(1);
  const [[id, resource]] = resources as [[string, { Properties: unknown }]];
  return { id, properties: resource.Properties };
}

describe('shared processed-record fixtures in the default dev app', () => {
  let stacks: CaseInsightsAppStacks;
  let analytics: Template;
  let mcp: Template;
  let table: GlueTableProperties;

  beforeAll(() => {
    stacks = addCaseInsightsStacks(testApp(), { env: 'dev' });
    expect(stacks.config.enableAnalytics).toBe(true);
    expect(stacks.config.enableMcp).toBe(true);
    analytics = Template.fromStack(stacks.analytics as Stack);
    mcp = Template.fromStack(stacks.mcp as Stack);
    table = onlyResource(analytics, 'AWS::Glue::Table').properties as GlueTableProperties;
  });

  test('the synthesised case_summary table is exactly the contract the Python integration tests read through', () => {
    const input = table.TableInput;
    expect(input.Name).toBe(contract.tableName);
    expect(input.TableType).toBe('EXTERNAL_TABLE');
    expect(input.PartitionKeys).toEqual(contract.partitionKeys.map(({ name, type }) => ({ Name: name, Type: type })));
    expect(input.StorageDescriptor.Columns).toEqual(contract.columns.map(({ name, type }) => ({ Name: name, Type: type })));
    expect(input.StorageDescriptor.SerdeInfo).toEqual({
      SerializationLibrary: contract.serializationLibrary,
      Parameters: contract.serdeParameters,
    });
  });

  test('every column of the synthesised table reads a stored key of every fixture record', () => {
    const parameters = table.TableInput.StorageDescriptor.SerdeInfo.Parameters;
    expect(parameters['case.insensitive']).toBe('false');
    expect(fixtureRecords).toHaveLength(16);
    for (const { accountNumber, record } of fixtureRecords) {
      const mapped = table.TableInput.StorageDescriptor.Columns.map(({ Name }) => parameters[`mapping.${Name}`]);
      // Every stored field is read by exactly one column, so no value is lost or read as null.
      expect(mapped).toEqual(Object.keys(record));
      const key = contract.recordKeyTemplate.replace('{account_number}', accountNumber).replace('{case_number}', String(record.displayId));
      expect(key).toBe(`account_number=${accountNumber}/case_number=${String(record.displayId)}/data.json`);
    }
  });

  test('the MCP function queries that table only through the synthesised Analytics database and workgroup', () => {
    const database = onlyResource(analytics, 'AWS::Glue::Database');
    const workgroup = onlyResource(analytics, 'AWS::Athena::WorkGroup').properties as {
      Name: string;
      WorkGroupConfiguration: {
        EnforceWorkGroupConfiguration: boolean;
        BytesScannedCutoffPerQuery: number;
        ResultConfiguration: { EncryptionConfiguration: { EncryptionOption: string } };
      };
    };
    const crawler = onlyResource(analytics, 'AWS::Glue::Crawler').properties as {
      Schedule: { ScheduleExpression: string };
      Targets: { CatalogTargets: { DatabaseName: unknown; Tables: string[] }[] };
    };
    const databaseName = (database.properties as { DatabaseInput: { Name: string } }).DatabaseInput.Name;

    expect(table.DatabaseName).toEqual({ Ref: database.id });
    expect(crawler.Targets.CatalogTargets).toEqual([{ DatabaseName: { Ref: database.id }, Tables: [contract.tableName] }]);
    expect(crawler.Schedule.ScheduleExpression).toBe('cron(0 9 * * ? *)');
    expect(workgroup.WorkGroupConfiguration.EnforceWorkGroupConfiguration).toBe(true);
    expect(workgroup.WorkGroupConfiguration.ResultConfiguration.EncryptionConfiguration).toEqual({ EncryptionOption: 'SSE_S3' });
    expect(workgroup.WorkGroupConfiguration.BytesScannedCutoffPerQuery).toBe(stacks.config.operations.athena.bytesScannedCutoffPerQuery);

    const fn = onlyResource(mcp, 'AWS::Lambda::Function').properties as { Environment: { Variables: Record<string, unknown> } };
    expect(fn.Environment.Variables.ATHENA_DATABASE).toBe(databaseName);
    expect(fn.Environment.Variables.ATHENA_WORKGROUP).toBe(workgroup.Name);

    const { managementAccountId: account, region } = stacks.config;
    const bySid = new Map(iamPolicyStatements(mcp).map(({ statement }) => [statement.Sid, statement]));
    expect(bySid.get('QueryCaseInsightsWorkgroup')?.Resource).toBe(`arn:aws:athena:${region}:${account}:workgroup/${workgroup.Name}`);
    expect(asArray(bySid.get('ReadCaseSummaryCatalog')?.Resource)).toContain(`arn:aws:glue:${region}:${account}:table/${databaseName}/${contract.tableName}`);
    // No other workgroup, database or catalog is reachable from the function's policy.
    const resources = iamPolicyStatements(mcp).flatMap(({ statement }) => asArray(statement.Resource).filter((r): r is string => typeof r === 'string'));
    for (const arn of resources.filter((r) => r.startsWith('arn:aws:athena:'))) {
      expect(arn).toBe(`arn:aws:athena:${region}:${account}:workgroup/${workgroup.Name}`);
    }
    for (const arn of resources.filter((r) => r.startsWith('arn:aws:glue:') && !r.endsWith(':catalog'))) {
      expect(arn).toMatch(new RegExp(`:(database|table)/${databaseName}(/|$)`));
    }
  });
});

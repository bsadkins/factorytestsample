import { RetentionDays } from 'aws-cdk-lib/aws-logs';
import {
  type CaseInsightsConfig,
  type OperationsConfig,
  type PipelineConfig,
  type ScheduleConfig,
  ENVIRONMENT_NAMES,
  SUPPORTED_BEDROCK_MODEL_IDS,
} from '../../lib';
import { readConfigJson } from './helpers';

interface SchemaNode {
  properties?: Record<string, SchemaNode>;
  enum?: unknown[];
  default?: unknown;
}

const schema = readConfigJson('schema.json') as SchemaNode;
const pipelineSchema = readConfigJson('pipeline.schema.json') as SchemaNode;

// `satisfies Record<keyof T, true>` makes the compiler reject missing or extra keys,
// so these objects are an exact inventory of each TypeScript interface.
const CONFIG_KEYS = {
  uniqueId: true,
  orgId: true,
  managementAccountId: true,
  region: true,
  environment: true,
  enableAnalytics: true,
  enableMcp: true,
  memberRoleTargets: true,
  bedrockModelId: true,
  bedrockMaxTokens: true,
  bedrockMaxConcurrency: true,
  lookbackMonths: true,
  rawRetentionDays: true,
  logRetentionDays: true,
  alarmEmail: true,
  mcpAllowedPrincipals: true,
  schedules: true,
  operations: true,
} satisfies Record<keyof CaseInsightsConfig, true>;

const SCHEDULE_KEYS = { accountLookup: true, cleanup: true, crawler: true } satisfies Record<keyof ScheduleConfig, true>;

const OPERATIONS_KEYS = {
  queues: { maxReceiveCount: true, visibilityTimeoutSeconds: true, dlqRetentionDays: true },
  functions: { consumerTimeoutSeconds: true, analysisTimeoutSeconds: true, memorySizeMb: true },
  workflow: { executionTimeoutMinutes: true, starterMaxConcurrency: true, taskMaxAttempts: true },
  cleanup: { graceHours: true },
  storage: { processedInfrequentAccessDays: true, noncurrentVersionRetentionDays: true, accessLogRetentionDays: true },
  athena: { bytesScannedCutoffPerQuery: true },
  mcp: { reservedConcurrency: true, throttleRateLimit: true, throttleBurstLimit: true, wafRateLimitPer5Minutes: true },
} satisfies { [G in keyof OperationsConfig]: Record<keyof OperationsConfig[G], true> };

const PIPELINE_KEYS = {
  pipelineName: true,
  account: true,
  region: true,
  connectionArn: true,
  repository: true,
  branch: true,
} satisfies Record<keyof PipelineConfig, true>;

/** Original brief configuration fields (library/brief-reference.md, "Original config fields"). */
const ORIGINAL_BRIEF_FIELDS = [
  'uniqueId',
  'orgId',
  'managementAccountId',
  'region',
  'environment',
  'enableAnalytics',
  'enableMcp',
  'memberRoleTargets',
  'bedrockModelId',
  'bedrockMaxTokens',
  'bedrockMaxConcurrency',
  'lookbackMonths',
  'rawRetentionDays',
  'logRetentionDays',
  'alarmEmail',
  'mcpAllowedPrincipals',
  'schedules',
];

function keysOf(node: SchemaNode | undefined): string[] {
  return Object.keys(node?.properties ?? {}).filter((key) => key !== '$schema').sort();
}

describe('schema and TypeScript parity', () => {
  test('every original brief field is in the schema and the typed config', () => {
    for (const field of ORIGINAL_BRIEF_FIELDS) {
      expect(keysOf(schema)).toContain(field);
      expect(Object.keys(CONFIG_KEYS)).toContain(field);
    }
    expect(keysOf(schema.properties?.schedules)).toEqual(['accountLookup', 'cleanup', 'crawler']);
  });

  test('top-level schema properties match CaseInsightsConfig exactly', () => {
    expect(keysOf(schema)).toEqual(Object.keys(CONFIG_KEYS).sort());
    expect(keysOf(schema.properties?.schedules)).toEqual(Object.keys(SCHEDULE_KEYS).sort());
  });

  test('operations groups match OperationsConfig exactly', () => {
    const operations = schema.properties?.operations;
    expect(keysOf(operations)).toEqual(Object.keys(OPERATIONS_KEYS).sort());
    for (const [group, keys] of Object.entries(OPERATIONS_KEYS)) {
      expect(keysOf(operations?.properties?.[group])).toEqual(Object.keys(keys).sort());
    }
  });

  test('pipeline schema matches PipelineConfig exactly', () => {
    expect(keysOf(pipelineSchema)).toEqual(Object.keys(PIPELINE_KEYS).sort());
  });

  test('model allowlist, environments and defaults agree with the TypeScript constants', () => {
    expect(schema.properties?.bedrockModelId?.enum).toEqual([...SUPPORTED_BEDROCK_MODEL_IDS]);
    expect(schema.properties?.bedrockModelId?.default).toBe(SUPPORTED_BEDROCK_MODEL_IDS[0]);
    expect(schema.properties?.environment?.enum).toEqual([...ENVIRONMENT_NAMES]);
    expect(schema.properties?.region?.default).toBe('us-east-1');
    expect(schema.properties?.lookbackMonths?.default).toBe(12);
    expect(schema.properties?.bedrockMaxTokens?.default).toBe(1000);
    expect(schema.properties?.logRetentionDays?.default).toBe(90);
  });

  test('every allowed log retention value is a CloudWatch Logs retention period', () => {
    const cloudWatchValues = new Set<number>(Object.values(RetentionDays).filter((value) => typeof value === 'number'));
    for (const value of schema.properties?.logRetentionDays?.enum ?? []) {
      expect(cloudWatchValues.has(value as number)).toBe(true);
    }
  });
});

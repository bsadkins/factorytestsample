import * as fs from 'node:fs';
import * as path from 'node:path';
import {
  CONFIG_DIR,
  ConfigValidationError,
  HAIKU_4_5_GLOBAL_PROFILE,
  SONNET_4_5_GLOBAL_PROFILE,
  loadConfigFile,
  loadEnvironmentConfig,
  MCP_RESERVED_CONCURRENCY,
  parseConfig,
  queueVisibilityTimeoutSeconds,
  resolveEnvironmentName,
  validateSharedApiLoggingConsistency,
} from '../../lib';
import { ownedTempDir } from '../helpers/temp-dirs';
import { minimalConfig, readConfigJson, type JsonObject } from './helpers';

function issuesFor(raw: unknown): readonly string[] {
  try {
    parseConfig(raw, 'test');
  } catch (error) {
    if (error instanceof ConfigValidationError) return error.issues;
    throw error;
  }
  throw new Error('expected configuration to be rejected');
}

function withOperations(operations: JsonObject, overrides: JsonObject = {}): JsonObject {
  return minimalConfig({ operations, ...overrides });
}

describe('committed configuration files', () => {
  test.each(['example.json', 'dev.json', 'prod.json'])('%s is valid', (file) => {
    expect(() => loadConfigFile(path.join(CONFIG_DIR, file))).not.toThrow();
  });

  test('dev enables both optional stacks', () => {
    const dev = loadEnvironmentConfig('dev');
    expect(dev.environment).toBe('dev');
    expect(dev.enableAnalytics).toBe(true);
    expect(dev.enableMcp).toBe(true);
    expect(dev.mcpAllowedPrincipals.length).toBeGreaterThan(0);
  });

  test('prod declares an alarm email and retains the original defaults', () => {
    const prod = loadEnvironmentConfig('prod');
    expect(prod.environment).toBe('prod');
    expect(prod.alarmEmail).toMatch(/@example\.com$/);
    expect(prod.region).toBe('us-east-1');
    expect(prod.lookbackMonths).toBe(12);
    expect(prod.bedrockMaxTokens).toBe(1000);
    expect(prod.logRetentionDays).toBe(90);
  });

  test('example lists every top-level and operations field explicitly', () => {
    const schema = readConfigJson('schema.json') as { properties: Record<string, { properties?: Record<string, unknown> }> };
    const example = readConfigJson('example.json');
    const expectedTopLevel = Object.keys(schema.properties).filter((key) => key !== '$schema');
    expect(Object.keys(example).filter((key) => key !== '$schema').sort()).toEqual(expectedTopLevel.sort());
    const operations = example.operations as Record<string, JsonObject>;
    for (const [group, groupSchema] of Object.entries(schema.properties.operations?.properties ?? {})) {
      const optional = new Set(['visibilityTimeoutSeconds']);
      const keys = Object.keys((groupSchema as { properties: JsonObject }).properties).filter((key) => !optional.has(key));
      expect(Object.keys(operations[group] ?? {}).sort()).toEqual(keys.sort());
    }
  });
});

describe('defaults', () => {
  test('a minimal configuration receives every documented default', () => {
    const config = parseConfig(minimalConfig());
    expect(config).toMatchObject({
      region: 'us-east-1',
      enableAnalytics: false,
      enableMcp: false,
      bedrockModelId: HAIKU_4_5_GLOBAL_PROFILE,
      bedrockMaxTokens: 1000,
      bedrockMaxConcurrency: 5,
      lookbackMonths: 12,
      rawRetentionDays: 90,
      logRetentionDays: 90,
      mcpAllowedPrincipals: [],
      apiGatewayLogging: { mode: 'managed', sharedId: 'shared' },
      schedules: {
        accountLookup: 'cron(0 0 * * ? *)',
        cleanup: 'cron(0 22 * * ? *)',
        crawler: 'cron(0 9 * * ? *)',
      },
      operations: {
        queues: { maxReceiveCount: 3, dlqRetentionDays: 14 },
        functions: { consumerTimeoutSeconds: 300, analysisTimeoutSeconds: 180, memorySizeMb: 512 },
        workflow: { executionTimeoutMinutes: 60, starterMaxConcurrency: 2, taskMaxAttempts: 3 },
        cleanup: { graceHours: 24 },
        storage: { processedInfrequentAccessDays: 90, noncurrentVersionRetentionDays: 30, accessLogRetentionDays: 365 },
        athena: { bytesScannedCutoffPerQuery: 10737418240, resultsRetentionDays: 30 },
        mcp: { reservedConcurrency: 20, throttleRateLimit: 10, throttleBurstLimit: 20, wafRateLimitPer5Minutes: 300 },
      },
    });
    expect(config.alarmEmail).toBeUndefined();
    expect(config.operations.queues.visibilityTimeoutSeconds).toBeUndefined();
  });

  test('partially specified groups keep defaults for omitted keys', () => {
    const config = parseConfig(withOperations({ queues: { maxReceiveCount: 5 } }));
    expect(config.operations.queues).toEqual({ maxReceiveCount: 5, dlqRetentionDays: 14 });
    expect(config.operations.functions.consumerTimeoutSeconds).toBe(300);
  });

  test('parsing never mutates the caller object', () => {
    const raw = minimalConfig();
    const snapshot = structuredClone(raw);
    parseConfig(raw);
    expect(raw).toEqual(snapshot);
  });

  test('visibility timeout defaults to six consumer timeouts', () => {
    expect(queueVisibilityTimeoutSeconds(parseConfig(minimalConfig()))).toBe(1800);
    const explicit = parseConfig(withOperations({ queues: { visibilityTimeoutSeconds: 3600 } }));
    expect(queueVisibilityTimeoutSeconds(explicit)).toBe(3600);
  });

  test('the Sonnet 4.5 global profile is the only accepted alternative model', () => {
    expect(parseConfig(minimalConfig({ bedrockModelId: SONNET_4_5_GLOBAL_PROFILE })).bedrockModelId).toBe(
      SONNET_4_5_GLOBAL_PROFILE,
    );
  });
});

describe('rejected configurations', () => {
  const cases: [string, JsonObject, RegExp][] = [
    ['missing uniqueId', minimalConfig({ uniqueId: undefined }), /required property 'uniqueId'/],
    ['upper-case uniqueId', minimalConfig({ uniqueId: 'SciDev' }), /\/uniqueId must match pattern/],
    ['uniqueId with trailing hyphen', minimalConfig({ uniqueId: 'sci-' }), /\/uniqueId must match pattern/],
    ['uniqueId too long', minimalConfig({ uniqueId: 'abcdefghijklm' }), /\/uniqueId must NOT have more than 12/],
    ['malformed orgId', minimalConfig({ orgId: 'org-123' }), /\/orgId must match pattern/],
    ['short account ID', minimalConfig({ managementAccountId: '12345' }), /\/managementAccountId must match pattern/],
    ['numeric account ID', minimalConfig({ managementAccountId: 111111111111 }), /\/managementAccountId must be string/],
    ['malformed region', minimalConfig({ region: 'US_EAST_1' }), /\/region must match pattern/],
    ['unknown environment', minimalConfig({ environment: 'staging' }), /\/environment must be one of "dev", "prod"/],
    ['no member role targets', minimalConfig({ memberRoleTargets: [] }), /\/memberRoleTargets must NOT have fewer than 1/],
    ['account ID as role target', minimalConfig({ memberRoleTargets: ['222222222222'] }), /\/memberRoleTargets\/0 must match/],
    ['duplicate role targets', minimalConfig({ memberRoleTargets: ['r-ab12', 'r-ab12'] }), /must NOT have duplicate items/],
    [
      'unsupported regional model',
      minimalConfig({ bedrockModelId: 'anthropic.claude-haiku-4-5-20251001-v1:0' }),
      /\/bedrockModelId must be one of/,
    ],
    [
      'unsupported US profile',
      minimalConfig({ bedrockModelId: 'us.anthropic.claude-sonnet-4-5-20250929-v1:0' }),
      /\/bedrockModelId must be one of/,
    ],
    ['tokens too low', minimalConfig({ bedrockMaxTokens: 100 }), /\/bedrockMaxTokens must be >= 256/],
    ['fractional tokens', minimalConfig({ bedrockMaxTokens: 1000.5 }), /\/bedrockMaxTokens must be integer/],
    ['zero concurrency', minimalConfig({ bedrockMaxConcurrency: 0 }), /\/bedrockMaxConcurrency must be >= 1/],
    ['lookback above Support retention', minimalConfig({ lookbackMonths: 13 }), /\/lookbackMonths must be <= 12/],
    ['zero lookback', minimalConfig({ lookbackMonths: 0 }), /\/lookbackMonths must be >= 1/],
    ['raw retention too short', minimalConfig({ rawRetentionDays: 1 }), /\/rawRetentionDays must be >= 7/],
    ['unsupported log retention', minimalConfig({ logRetentionDays: 45 }), /\/logRetentionDays must be one of/],
    ['invalid alarm email', minimalConfig({ alarmEmail: 'not-an-email' }), /\/alarmEmail must match format "email"/],
    ['unknown property', minimalConfig({ enableQuicksight: true }), /unsupported property "enableQuicksight"/],
    ['principal that is not an IAM ARN', minimalConfig({ mcpAllowedPrincipals: ['*'] }), /\/mcpAllowedPrincipals\/0 must match/],
    ['rate schedule', minimalConfig({ schedules: { accountLookup: 'rate(1 day)' } }), /\/schedules\/accountLookup must match/],
    ['five-field cron', minimalConfig({ schedules: { cleanup: 'cron(0 22 * * ?)' } }), /\/schedules\/cleanup must match/],
    ['cron with two "?"', minimalConfig({ schedules: { crawler: 'cron(0 9 ? * ? *)' } }), /\/schedules\/crawler must use "\?"/],
    ['cron without "?"', minimalConfig({ schedules: { crawler: 'cron(0 9 * * * *)' } }), /\/schedules\/crawler must use "\?"/],
    ['unknown schedule', minimalConfig({ schedules: { hourly: 'cron(0 * * * ? *)' } }), /unsupported property "hourly"/],
    ['receive count zero', withOperations({ queues: { maxReceiveCount: 0 } }), /maxReceiveCount must be >= 1/],
    ['consumer timeout above Lambda maximum', withOperations({ functions: { consumerTimeoutSeconds: 901 } }), /must be <= 900/],
    ['starter concurrency below SQS minimum', withOperations({ workflow: { starterMaxConcurrency: 1 } }), /must be >= 2/],
    ['Athena cutoff below service minimum', withOperations({ athena: { bytesScannedCutoffPerQuery: 1024 } }), /must be >= 10485760/],
    ['Athena cutoff above 1 TiB', withOperations({ athena: { bytesScannedCutoffPerQuery: 1099511627777 } }), /must be <= 1099511627776/],
    ['Athena results kept for zero days', withOperations({ athena: { resultsRetentionDays: 0 } }), /must be >= 1/],
    ['fractional Athena results retention', withOperations({ athena: { resultsRetentionDays: 1.5 } }), /must be integer/],
    ['IA transition before 30 days', withOperations({ storage: { processedInfrequentAccessDays: 7 } }), /must be >= 30/],
    ['unknown operations group', withOperations({ vpc: {} }), /unsupported property "vpc"/],
    ['MCP reserved concurrency other than 20', withOperations({ mcp: { reservedConcurrency: 7 } }), /\/operations\/mcp\/reservedConcurrency must be 20/],
    ['MCP reserved concurrency above 20', withOperations({ mcp: { reservedConcurrency: 100 } }), /reservedConcurrency must be 20/],
    ['MCP reserved concurrency as a string', withOperations({ mcp: { reservedConcurrency: '20' } }), /reservedConcurrency must be 20/],
    ['unknown API logging mode', minimalConfig({ apiGatewayLogging: { mode: 'off' } }), /\/apiGatewayLogging\/mode must be one of "managed", "existing"/],
    ['upper-case shared namespace', minimalConfig({ apiGatewayLogging: { sharedId: 'Shared' } }), /\/apiGatewayLogging\/sharedId must match pattern/],
    ['shared namespace too long', minimalConfig({ apiGatewayLogging: { sharedId: 'abcdefghijklm' } }), /\/apiGatewayLogging\/sharedId must NOT have more than 12/],
    ['unknown API logging property', minimalConfig({ apiGatewayLogging: { cloudWatchRole: false } }), /unsupported property "cloudWatchRole"/],
    [
      'existing mode without a role ARN',
      minimalConfig({ apiGatewayLogging: { mode: 'existing' } }),
      /rule violated: existingRoleArn is required when mode is existing and not allowed when mode is managed/,
    ],
    [
      'managed mode with a role ARN',
      minimalConfig({ apiGatewayLogging: { existingRoleArn: 'arn:aws:iam::111111111111:role/ApiGatewayLogs' } }),
      /rule violated: existingRoleArn is required when mode is existing/,
    ],
    [
      'existing role that is not an IAM role ARN',
      minimalConfig({ apiGatewayLogging: { mode: 'existing', existingRoleArn: 'arn:aws:iam::111111111111:user/ApiGatewayLogs' } }),
      /\/apiGatewayLogging\/existingRoleArn must match pattern/,
    ],
    [
      'existing role in another account',
      minimalConfig({ apiGatewayLogging: { mode: 'existing', existingRoleArn: 'arn:aws:iam::222222222222:role/ApiGatewayLogs' } }),
      /\/apiGatewayLogging\/existingRoleArn is in account 222222222222 but API Gateway needs a role in the management account 111111111111/,
    ],
    [
      'visibility below six consumer timeouts',
      withOperations({ queues: { visibilityTimeoutSeconds: 1000 }, functions: { consumerTimeoutSeconds: 300 } }),
      /visibilityTimeoutSeconds \(1000\) must be at least 6 x consumerTimeoutSeconds \(1800\)/,
    ],
    [
      'cleanup grace not longer than a workflow execution',
      withOperations({ cleanup: { graceHours: 2 }, workflow: { executionTimeoutMinutes: 120 } }),
      /graceHours \(2h\) must exceed workflow.executionTimeoutMinutes \(120min\)/,
    ],
    [
      'raw retention shorter than cleanup grace',
      withOperations({ cleanup: { graceHours: 240 } }, { rawRetentionDays: 7 }),
      /rawRetentionDays \(7d\) must exceed operations.cleanup.graceHours \(240h\)/,
    ],
  ];

  test.each(cases)('%s', (_name, raw, expected) => {
    expect(issuesFor(raw).join('\n')).toMatch(expected);
  });

  test('prod without an alarm email', () => {
    const issues = issuesFor(minimalConfig({ environment: 'prod' }));
    expect(issues).toContain("(root) must have required property 'alarmEmail'");
    expect(issues).toContain('rule violated: alarmEmail is required when environment is prod.');
  });

  test('MCP without Analytics', () => {
    const issues = issuesFor(
      minimalConfig({ enableMcp: true, enableAnalytics: false, mcpAllowedPrincipals: ['arn:aws:iam::111111111111:role/Analyst'] }),
    );
    expect(issues).toContain('/enableAnalytics must be true');
    expect(issues.join('\n')).toMatch(/rule violated: enableMcp requires enableAnalytics/);
  });

  test('MCP with Analytics omitted (defaults to disabled)', () => {
    const issues = issuesFor(minimalConfig({ enableMcp: true, mcpAllowedPrincipals: ['arn:aws:iam::111111111111:role/Analyst'] }));
    expect(issues).toContain("(root) must have required property 'enableAnalytics'");
  });

  test('MCP with an empty principal allowlist', () => {
    const issues = issuesFor(minimalConfig({ enableMcp: true, enableAnalytics: true, mcpAllowedPrincipals: [] }));
    expect(issues).toContain('/mcpAllowedPrincipals must NOT have fewer than 1 items');
  });

  test('MCP with the principal allowlist omitted', () => {
    const issues = issuesFor(minimalConfig({ enableMcp: true, enableAnalytics: true }));
    expect(issues).toContain("(root) must have required property 'mcpAllowedPrincipals'");
  });

  test('a non-object document', () => {
    expect(issuesFor(['not', 'an', 'object'])).toEqual(['(root) must be object']);
  });
});

describe('MCP reserved concurrency', () => {
  test('is fixed at 20 by default and when stated explicitly', () => {
    expect(MCP_RESERVED_CONCURRENCY).toBe(20);
    expect(parseConfig(minimalConfig()).operations.mcp.reservedConcurrency).toBe(20);
    expect(parseConfig(withOperations({ mcp: { reservedConcurrency: 20 } })).operations.mcp.reservedConcurrency).toBe(20);
  });

  test('the schema declares const 20 and default 20 while the other MCP limits stay configurable', () => {
    const schema = readConfigJson('schema.json') as {
      properties: { operations: { properties: { mcp: { properties: Record<string, Record<string, unknown>> } } } };
    };
    const mcp = schema.properties.operations.properties.mcp.properties;
    expect(mcp.reservedConcurrency).toMatchObject({ const: 20, default: 20 });
    expect(mcp.reservedConcurrency?.minimum).toBeUndefined();
    expect(mcp.reservedConcurrency?.maximum).toBeUndefined();
    const configured = parseConfig(withOperations({ mcp: { throttleRateLimit: 3, throttleBurstLimit: 6, wafRateLimitPer5Minutes: 150 } }));
    expect(configured.operations.mcp).toEqual({ reservedConcurrency: 20, throttleRateLimit: 3, throttleBurstLimit: 6, wafRateLimitPer5Minutes: 150 });
  });
});

describe('shared API Gateway logging settings', () => {
  const existing = { mode: 'existing', sharedId: 'shared', existingRoleArn: 'arn:aws:iam::111111111111:role/platform/ApiGatewayLogs' };

  test('existing mode accepts a role ARN with a path in the management account', () => {
    expect(parseConfig(minimalConfig({ apiGatewayLogging: existing })).apiGatewayLogging).toEqual(existing);
  });

  test('a configurable shared namespace keeps the managed default mode', () => {
    expect(parseConfig(minimalConfig({ apiGatewayLogging: { sharedId: 'platform-api' } })).apiGatewayLogging).toEqual({
      mode: 'managed',
      sharedId: 'platform-api',
    });
  });

  test('dev and prod in the same account and Region must agree', () => {
    const dev = parseConfig(minimalConfig());
    const prod = parseConfig(minimalConfig({ environment: 'prod', uniqueId: 'sciprod', alarmEmail: 'ops@example.com', apiGatewayLogging: existing }));
    expect(() => { validateSharedApiLoggingConsistency([dev, prod]); }).toThrow(
      /dev \(scitest\) and prod \(sciprod\) deploy to the same account and Region must use the same apiGatewayLogging settings/,
    );
    const prodElsewhere = parseConfig(
      minimalConfig({ environment: 'prod', uniqueId: 'sciprod', alarmEmail: 'ops@example.com', region: 'eu-west-1', apiGatewayLogging: existing }),
    );
    expect(() => { validateSharedApiLoggingConsistency([dev, prodElsewhere]); }).not.toThrow();
    expect(() => { validateSharedApiLoggingConsistency([dev, parseConfig(minimalConfig({ uniqueId: 'other' }))]); }).not.toThrow();
  });
});

describe('environment selection', () => {
  test.each(['dev', 'prod'])('accepts %s', (env) => {
    expect(resolveEnvironmentName(env)).toBe(env);
  });

  test.each([undefined, '', 'example', 'PROD', 1])('rejects %p', (value) => {
    expect(() => resolveEnvironmentName(value)).toThrow(ConfigValidationError);
  });

  test('rejects a file whose environment does not match the requested one', () => {
    const configDir = ownedTempDir('case-insights-config-');
    fs.writeFileSync(path.join(configDir, 'prod.json'), JSON.stringify(readConfigJson('dev.json')));
    expect(() => loadEnvironmentConfig('prod', { configDir })).toThrow(/\/environment is "dev" but the file was loaded for -c env=prod/);
  });

  test('reports unreadable and malformed files as configuration errors', () => {
    const configDir = ownedTempDir('case-insights-config-');
    expect(() => loadEnvironmentConfig('dev', { configDir })).toThrow(/cannot read file/);
    fs.writeFileSync(path.join(configDir, 'dev.json'), '{ "uniqueId": ');
    expect(() => loadEnvironmentConfig('dev', { configDir })).toThrow(/invalid JSON/);
  });
});

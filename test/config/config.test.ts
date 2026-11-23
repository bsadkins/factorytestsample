import * as fs from 'node:fs';
import * as path from 'node:path';
import {
  ConfigValidationError,
  HAIKU_4_5_GLOBAL_PROFILE,
  SONNET_4_5_GLOBAL_PROFILE,
  loadEnvironmentConfig,
  MCP_RESERVED_CONCURRENCY,
  parseConfig,
  queueVisibilityTimeoutSeconds,
  resolveEnvironmentName,
  validateSharedApiLoggingConsistency,
} from '../../lib';
import { ownedTempDir } from '../helpers/temp-dirs';
import { fixtureEnvironmentConfig, minimalConfig, readConfigJson, readFixtureJson, type JsonObject } from './helpers';

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

describe('synthetic fixtures and the committed example', () => {
  // The fixed CDK assertions and snapshots rely on these combinations, not on the operator files in config/.
  test('the dev fixture enables both optional stacks', () => {
    const dev = fixtureEnvironmentConfig('dev');
    expect(dev.environment).toBe('dev');
    expect(dev.enableAnalytics).toBe(true);
    expect(dev.enableMcp).toBe(true);
    expect(dev.mcpAllowedPrincipals.length).toBeGreaterThan(0);
  });

  test('the prod fixture declares an alarm email, enables Analytics only and keeps the original defaults', () => {
    const prod = fixtureEnvironmentConfig('prod');
    expect(prod.environment).toBe('prod');
    expect(prod.alarmEmail).toMatch(/@example\.com$/);
    expect(prod).toMatchObject({ enableAnalytics: true, enableMcp: false, region: 'us-east-1', lookbackMonths: 12, bedrockMaxTokens: 1000, logRetentionDays: 90 });
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
    ['cron minute 99', minimalConfig({ schedules: { accountLookup: 'cron(99 0 * * ? *)' } }), /\/schedules\/accountLookup minutes field "99" is invalid: expected 0-59, got "99"/],
    ['cron hour 24', minimalConfig({ schedules: { cleanup: 'cron(0 24 * * ? *)' } }), /\/schedules\/cleanup hours field "24" is invalid: expected 0-23, got "24"/],
    ['cron day-of-month 32', minimalConfig({ schedules: { crawler: 'cron(0 9 32 * ? *)' } }), /\/schedules\/crawler day-of-month field "32" is invalid: expected 1-31, got "32"/],
    ['cron day-of-month 0', minimalConfig({ schedules: { crawler: 'cron(0 9 0 * ? *)' } }), /day-of-month field "0" is invalid: expected 1-31, got "0"/],
    ['cron month 13', minimalConfig({ schedules: { crawler: 'cron(0 9 ? 13 MON *)' } }), /month field "13" is invalid: expected 1-12 or JAN-DEC, got "13"/],
    ['cron unknown month name', minimalConfig({ schedules: { crawler: 'cron(0 9 1 JUNE ? *)' } }), /month field "JUNE" is invalid: expected 1-12 or JAN-DEC, got "JUNE"/],
    ['cron day-of-week 8', minimalConfig({ schedules: { crawler: 'cron(0 9 ? * 8 *)' } }), /day-of-week field "8" is invalid: expected 1-7 or SUN-SAT, got "8"/],
    ['cron unknown weekday', minimalConfig({ schedules: { crawler: 'cron(0 9 ? * FUN *)' } }), /day-of-week field "FUN" is invalid: expected 1-7 or SUN-SAT, got "FUN"/],
    ['cron year before 1970', minimalConfig({ schedules: { crawler: 'cron(0 9 * * ? 1969)' } }), /year field "1969" is invalid: expected 1970-2199, got "1969"/],
    ['cron range past the maximum', minimalConfig({ schedules: { crawler: 'cron(0 9-25 * * ? *)' } }), /hours field "9-25" is invalid: expected 0-23, got "25"/],
    ['cron list element past the maximum', minimalConfig({ schedules: { crawler: 'cron(0,60 9 * * ? *)' } }), /minutes field "0,60" is invalid: expected 0-59, got "60"/],
    ['cron zero increment', minimalConfig({ schedules: { crawler: 'cron(0/0 9 * * ? *)' } }), /minutes field "0\/0" is invalid: expected an increment of 1-60, got "0"/],
    ['cron empty list element', minimalConfig({ schedules: { crawler: 'cron(0,,5 9 * * ? *)' } }), /minutes field "0,,5" is invalid: expected 0-59, got ""/],
    ['cron "?" in minutes', minimalConfig({ schedules: { crawler: 'cron(? 9 * * ? *)' } }), /minutes field "\?" is invalid: "\?" is allowed only in day-of-month or day-of-week/],
    ['cron "L" in hours', minimalConfig({ schedules: { crawler: 'cron(0 L * * ? *)' } }), /hours field "L" is invalid: expected 0-23, got "L"/],
    ['cron "#" in day-of-month', minimalConfig({ schedules: { crawler: 'cron(0 9 3#2 * ? *)' } }), /day-of-month field "3#2" is invalid/],
    ['cron sixth weekday of a month', minimalConfig({ schedules: { crawler: 'cron(0 9 ? * 2#6 *)' } }), /day-of-week field "2#6" is invalid: expected an occurrence of 1-5, got "6"/],
    ['cron weekday-nearest 32', minimalConfig({ schedules: { crawler: 'cron(0 9 32W * ? *)' } }), /day-of-month field "32W" is invalid: expected 1-31, got "32"/],
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

  test('every out-of-range schedule field is reported', () => {
    const issues = issuesFor(minimalConfig({ schedules: { accountLookup: 'cron(60 24 * 0 ? 2200)' } }));
    expect(issues).toEqual([
      '/schedules/accountLookup minutes field "60" is invalid: expected 0-59, got "60"',
      '/schedules/accountLookup hours field "24" is invalid: expected 0-23, got "24"',
      '/schedules/accountLookup month field "0" is invalid: expected 1-12 or JAN-DEC, got "0"',
      '/schedules/accountLookup year field "2200" is invalid: expected 1970-2199, got "2200"',
    ]);
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

describe('schedules', () => {
  test.each([
    'cron(0 0 * * ? *)',
    'cron(0 22 * * ? *)',
    'cron(0 9 * * ? *)',
    'cron(0/15 * * * ? *)',
    'cron(*/5 8-17 ? * MON-FRI *)',
    'cron(5,35 14 * JAN,JUL ? 2026-2030)',
    'cron(0 6 ? * 2-6 *)',
    'cron(10-50/20 0 1-15/7 * ? *)',
    'cron(30 2 L * ? *)',
    'cron(0 0 LW * ? *)',
    'cron(0 0 15W * ? *)',
    'cron(0 12 ? * 6#3 *)',
    'cron(0 12 ? * FRI#1 *)',
    'cron(0 12 ? * 6L *)',
    'cron(0 12 ? * L *)',
    'cron(59 23 31 DEC ? 2199)',
    'cron(0 0 1 1 ? 1970)',
  ])('accepts %s', (expression) => {
    const config = parseConfig(minimalConfig({ schedules: { accountLookup: expression, cleanup: expression, crawler: expression } }));
    expect(config.schedules).toEqual({ accountLookup: expression, cleanup: expression, crawler: expression });
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
    fs.writeFileSync(path.join(configDir, 'prod.json'), JSON.stringify(readFixtureJson('dev')));
    expect(() => loadEnvironmentConfig('prod', { configDir })).toThrow(/\/environment is "dev" but the file was loaded for -c env=prod/);
  });

  test('reports unreadable and malformed files as configuration errors', () => {
    const configDir = ownedTempDir('case-insights-config-');
    expect(() => loadEnvironmentConfig('dev', { configDir })).toThrow(/cannot read file/);
    fs.writeFileSync(path.join(configDir, 'dev.json'), '{ "uniqueId": ');
    expect(() => loadEnvironmentConfig('dev', { configDir })).toThrow(/invalid JSON/);
  });
});

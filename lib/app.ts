import { type App, Stage, Tags } from 'aws-cdk-lib';
import type { Construct } from 'constructs';
import {
  type ApiGatewayLoggingConfig,
  type CaseInsightsConfig,
  CONFIG_DIR,
  ConfigValidationError,
  type EnvironmentName,
  loadEnvironmentConfig,
  loadOtherEnvironmentConfigs,
  resourceNames,
  sameApiGatewayLoggingSettings,
  sharedApiLoggingNames,
  validateSharedApiLoggingConsistency,
} from './config';
import { addMcpObservability } from './constructs/mcp-observability';
import { enableNagChecks } from './nag';
import { CaseInsightsAnalyticsStack } from './stacks/analytics-stack';
import { CaseInsightsApiLoggingStack } from './stacks/api-logging-stack';
import { CaseInsightsCoreStack } from './stacks/core-stack';
import { CaseInsightsMcpStack } from './stacks/mcp-stack';
import { CaseInsightsMemberRoleStack } from './stacks/member-role-stack';

const PROJECT_TAG = 'SupportCaseInsights';

export interface CaseInsightsAppOptions {
  readonly env: EnvironmentName;
  /** Directory containing <env>.json; defaults to the repository config/ directory. */
  readonly configDir?: string;
}

/**
 * The account/Region API Gateway logging owner that environments with MCP share. Created once, at the
 * root of an app or pipeline, by `addSharedApiLogging`.
 */
export interface SharedApiLogging {
  readonly settings: ApiGatewayLoggingConfig;
  readonly account: string;
  readonly region: string;
  /** The owning stack in managed mode; absent in existing mode, where the setting is external. */
  readonly stack?: CaseInsightsApiLoggingStack;
}

export interface CaseInsightsEnvironmentStacks {
  readonly config: CaseInsightsConfig;
  readonly core: CaseInsightsCoreStack;
  readonly memberRole: CaseInsightsMemberRoleStack;
  /** Present only when enableAnalytics is true. */
  readonly analytics?: CaseInsightsAnalyticsStack;
  /** Present only when enableMcp is true (which requires enableAnalytics). */
  readonly mcp?: CaseInsightsMcpStack;
}

export interface CaseInsightsAppStacks extends CaseInsightsEnvironmentStacks {
  /** Shared API Gateway logging stack; present only when MCP is enabled in managed mode. */
  readonly apiLogging?: CaseInsightsApiLoggingStack;
}

export interface CaseInsightsEnvironmentOptions {
  /** Required when config.enableMcp is true; must carry the same settings as config.apiGatewayLogging. */
  readonly sharedApiLogging?: SharedApiLogging;
}

/** True when `configs` need the CaseInsightsApiLoggingStack: some environment enables MCP in managed mode. */
export function sharedApiLoggingOwnerRequired(configs: readonly CaseInsightsConfig[]): boolean {
  return configs.some((config) => config.enableMcp && config.apiGatewayLogging.mode === 'managed');
}

/**
 * Adds the shared API Gateway logging owner for environments that deploy to one account and Region.
 * Returns undefined when no configuration enables MCP. Rejects configurations whose shared settings,
 * account or Region differ. In managed mode exactly one CaseInsightsApiLoggingStack-<sharedId> is added to
 * `scope`; call this once per app or pipeline, never inside an environment stage.
 */
export function addSharedApiLogging(scope: Construct, configs: readonly CaseInsightsConfig[]): SharedApiLogging | undefined {
  const [first] = configs;
  if (first === undefined) {
    return undefined;
  }
  const targets = new Set(configs.map((config) => `${config.managementAccountId}/${config.region}`));
  if (targets.size > 1) {
    throw new ConfigValidationError('shared API Gateway logging', [
      `one shared API Gateway logging owner serves one account and Region; got ${[...targets].sort().join(', ')}`,
    ]);
  }
  validateSharedApiLoggingConsistency(configs);
  if (!configs.some((config) => config.enableMcp)) {
    return undefined;
  }
  const settings = first.apiGatewayLogging;
  const shared = { settings, account: first.managementAccountId, region: first.region };
  if (settings.mode !== 'managed') {
    return shared;
  }
  const stackName = sharedApiLoggingNames(settings.sharedId).stack;
  const stack = new CaseInsightsApiLoggingStack(scope, stackName, {
    stackName,
    description: `Support Case Insights shared API Gateway CloudWatch Logs role and account setting (${settings.sharedId})`,
    env: { account: shared.account, region: shared.region },
    settings,
  });
  Tags.of(stack).add('Project', PROJECT_TAG);
  Tags.of(stack).add('SharedId', settings.sharedId);
  return { ...shared, stack };
}

function checkSharedApiLogging(config: CaseInsightsConfig, shared: SharedApiLogging | undefined): SharedApiLogging | undefined {
  if (!config.enableMcp) {
    return undefined;
  }
  const source = `${config.environment} (${config.uniqueId}) apiGatewayLogging`;
  if (shared === undefined) {
    throw new ConfigValidationError(source, ['enableMcp needs the shared API Gateway logging owner from addSharedApiLogging']);
  }
  const issues: string[] = [];
  if (shared.account !== config.managementAccountId || shared.region !== config.region) {
    issues.push(
      `the shared API Gateway logging owner is for ${shared.account}/${shared.region}, not ${config.managementAccountId}/${config.region}`,
    );
  }
  if (!sameApiGatewayLoggingSettings(shared.settings, config.apiGatewayLogging)) {
    issues.push(
      `apiGatewayLogging ${JSON.stringify(config.apiGatewayLogging)} differs from the shared API Gateway logging settings ${JSON.stringify(shared.settings)}`,
    );
  }
  if (shared.settings.mode === 'managed' && shared.stack === undefined) {
    issues.push('managed apiGatewayLogging needs the shared CaseInsightsApiLoggingStack');
  }
  if (issues.length > 0) {
    throw new ConfigValidationError(source, issues);
  }
  return shared;
}

/**
 * Adds one environment's stacks to `scope` (the app, or a pipeline stage). Environment stacks never
 * contain an AWS::ApiGateway::Account resource or API Gateway logging role; with MCP enabled they reuse
 * `options.sharedApiLogging`. When the shared stack is in the same stage, MCP depends on it; across
 * stages CDK cannot order stacks, so a pipeline must deploy the shared stack before the stage.
 */
export function addCaseInsightsEnvironmentStacks(
  scope: Construct,
  config: CaseInsightsConfig,
  options: CaseInsightsEnvironmentOptions = {},
): CaseInsightsEnvironmentStacks {
  const sharedApiLogging = checkSharedApiLogging(config, options.sharedApiLogging);
  const names = resourceNames(config);
  const env = { account: config.managementAccountId, region: config.region };

  const core = new CaseInsightsCoreStack(scope, names.coreStack, {
    stackName: names.coreStack,
    description: `Support Case Insights core pipeline (${config.uniqueId})`,
    env,
    config,
  });

  const memberRole = new CaseInsightsMemberRoleStack(scope, names.memberRoleStack, {
    stackName: names.memberRoleStack,
    description: `Support Case Insights member-account role StackSet (${config.uniqueId})`,
    env,
    config,
    trustedRoles: core.memberAccess.trust,
  });
  // IAM rejects trust policies naming roles that do not exist yet, so the trusted execution
  // roles must be created before any StackSet instance.
  memberRole.addStackDependency(core, 'Member role trusts core execution roles');

  const analytics = config.enableAnalytics
    ? new CaseInsightsAnalyticsStack(scope, names.analyticsStack, {
        stackName: names.analyticsStack,
        description: `Support Case Insights Glue catalog, crawler and Athena workgroup (${config.uniqueId})`,
        env,
        config,
        processedBucket: core.storage.processedBucket,
        accessLogsBucket: core.storage.accessLogsBucket,
      })
    : undefined;
  analytics?.addStackDependency(core, 'Analytics reads the Core processed and access-log buckets');

  let mcp: CaseInsightsMcpStack | undefined;
  if (config.enableMcp) {
    if (analytics === undefined) {
      // Unreachable after schema validation; kept so the stack can never be built without its data source.
      throw new Error('enableMcp requires enableAnalytics');
    }
    mcp = new CaseInsightsMcpStack(scope, names.mcpStack, {
      stackName: names.mcpStack,
      description: `Support Case Insights MCP endpoint, WAF and read-only tool function (${config.uniqueId})`,
      env,
      config,
      commonLayer: core.layers.commonLayer,
      analytics: analytics.analytics,
    });
    mcp.addStackDependency(core, 'MCP uses the Core common layer and processed bucket');
    mcp.addStackDependency(analytics, 'MCP queries the Analytics workgroup, catalog and results bucket');
    const loggingStack = sharedApiLogging?.stack;
    if (loggingStack !== undefined && Stage.of(loggingStack) === Stage.of(mcp)) {
      mcp.addStackDependency(loggingStack, 'REST access logging needs the account/Region API Gateway CloudWatch role setting');
    }
    addMcpObservability(core.observability, config);
  }

  for (const stack of [core, memberRole, analytics, mcp]) {
    if (stack !== undefined) {
      Tags.of(stack).add('Project', PROJECT_TAG);
      Tags.of(stack).add('Environment', config.environment);
      Tags.of(stack).add('UniqueId', config.uniqueId);
    }
  }

  return { config, core, memberRole, analytics, mcp };
}

/**
 * Validates config/<env>.json (and its shared API Gateway logging settings against the other
 * environment's file, when present), registers cdk-nag and adds the Support Case Insights stacks,
 * including the shared API Gateway logging stack when MCP is enabled in managed mode, to `app`.
 * Throws ConfigValidationError before any stack is created when the configuration is invalid.
 */
export function addCaseInsightsStacks(app: App, options: CaseInsightsAppOptions): CaseInsightsAppStacks {
  const configDir = options.configDir ?? CONFIG_DIR;
  const config = loadEnvironmentConfig(options.env, { configDir });
  validateSharedApiLoggingConsistency([config, ...loadOtherEnvironmentConfigs(options.env, configDir)]);

  enableNagChecks(app);
  const sharedApiLogging = addSharedApiLogging(app, [config]);
  const stacks = addCaseInsightsEnvironmentStacks(app, config, { sharedApiLogging });
  return { ...stacks, apiLogging: sharedApiLogging?.stack };
}

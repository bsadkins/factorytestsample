import { type App, Tags } from 'aws-cdk-lib';
import { type CaseInsightsConfig, type EnvironmentName, loadEnvironmentConfig, resourceNames } from './config';
import { addMcpObservability } from './constructs/mcp-observability';
import { enableNagChecks } from './nag';
import { CaseInsightsAnalyticsStack } from './stacks/analytics-stack';
import { CaseInsightsCoreStack } from './stacks/core-stack';
import { CaseInsightsMcpStack } from './stacks/mcp-stack';
import { CaseInsightsMemberRoleStack } from './stacks/member-role-stack';

export interface CaseInsightsAppOptions {
  readonly env: EnvironmentName;
  /** Directory containing <env>.json; defaults to the repository config/ directory. */
  readonly configDir?: string;
}

export interface CaseInsightsAppStacks {
  readonly config: CaseInsightsConfig;
  readonly core: CaseInsightsCoreStack;
  readonly memberRole: CaseInsightsMemberRoleStack;
  /** Present only when enableAnalytics is true. */
  readonly analytics?: CaseInsightsAnalyticsStack;
  /** Present only when enableMcp is true (which requires enableAnalytics). */
  readonly mcp?: CaseInsightsMcpStack;
}

/**
 * Validates config/<env>.json, registers cdk-nag and adds the Support Case Insights stacks to `app`.
 * Throws ConfigValidationError before any stack is created when the configuration is invalid.
 */
export function addCaseInsightsStacks(app: App, options: CaseInsightsAppOptions): CaseInsightsAppStacks {
  const config = loadEnvironmentConfig(options.env, { configDir: options.configDir });
  const names = resourceNames(config);
  const env = { account: config.managementAccountId, region: config.region };

  enableNagChecks(app);
  Tags.of(app).add('Project', 'SupportCaseInsights');
  Tags.of(app).add('Environment', config.environment);
  Tags.of(app).add('UniqueId', config.uniqueId);

  const core = new CaseInsightsCoreStack(app, names.coreStack, {
    stackName: names.coreStack,
    description: `Support Case Insights core pipeline (${config.uniqueId})`,
    env,
    config,
  });

  const memberRole = new CaseInsightsMemberRoleStack(app, names.memberRoleStack, {
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
    ? new CaseInsightsAnalyticsStack(app, names.analyticsStack, {
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
    mcp = new CaseInsightsMcpStack(app, names.mcpStack, {
      stackName: names.mcpStack,
      description: `Support Case Insights MCP endpoint, WAF and read-only tool function (${config.uniqueId})`,
      env,
      config,
      commonLayer: core.layers.commonLayer,
      analytics: analytics.analytics,
    });
    mcp.addStackDependency(core, 'MCP uses the Core common layer and processed bucket');
    mcp.addStackDependency(analytics, 'MCP queries the Analytics workgroup, catalog and results bucket');
    addMcpObservability(core.observability, config);
  }

  return { config, core, memberRole, analytics, mcp };
}

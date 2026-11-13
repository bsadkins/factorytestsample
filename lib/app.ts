import { type App, Tags } from 'aws-cdk-lib';
import { type CaseInsightsConfig, type EnvironmentName, loadEnvironmentConfig, resourceNames } from './config';
import { enableNagChecks } from './nag';
import { CaseInsightsCoreStack } from './stacks/core-stack';

export interface CaseInsightsAppOptions {
  readonly env: EnvironmentName;
  /** Directory containing <env>.json; defaults to the repository config/ directory. */
  readonly configDir?: string;
}

export interface CaseInsightsAppStacks {
  readonly config: CaseInsightsConfig;
  readonly core: CaseInsightsCoreStack;
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

  return { config, core };
}

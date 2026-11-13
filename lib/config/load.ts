import * as path from 'node:path';
import { type CaseInsightsConfig, ENVIRONMENT_NAMES, type EnvironmentName, type ScheduleConfig } from './types';
import { CONFIG_DIR, CONFIG_SCHEMA_PATH, ConfigValidationError, readJsonFile, validateAgainstSchema } from './schema-validator';

/** SQS visibility must cover at least six consumer timeouts so in-flight work is not redelivered. */
export const VISIBILITY_TIMEOUT_MULTIPLIER = 6;

export function queueVisibilityTimeoutSeconds(config: CaseInsightsConfig): number {
  const { queues, functions } = config.operations;
  return queues.visibilityTimeoutSeconds ?? functions.consumerTimeoutSeconds * VISIBILITY_TIMEOUT_MULTIPLIER;
}

function cronIssues(name: string, expression: string): string[] {
  const fields = expression.slice('cron('.length, -1).split(' ');
  const [, , dayOfMonth, , dayOfWeek] = fields;
  if ((dayOfMonth === '?') === (dayOfWeek === '?')) {
    return [`/schedules/${name} must use "?" in exactly one of day-of-month or day-of-week`];
  }
  return [];
}

/** Cross-field rules that JSON Schema cannot express; documented in the schema $comment. */
function semanticIssues(config: CaseInsightsConfig): string[] {
  const issues: string[] = [];
  const { queues, functions, workflow, cleanup } = config.operations;

  for (const name of Object.keys(config.schedules) as (keyof ScheduleConfig)[]) {
    issues.push(...cronIssues(name, config.schedules[name]));
  }

  const minimumVisibility = functions.consumerTimeoutSeconds * VISIBILITY_TIMEOUT_MULTIPLIER;
  if (queues.visibilityTimeoutSeconds !== undefined && queues.visibilityTimeoutSeconds < minimumVisibility) {
    issues.push(
      `/operations/queues/visibilityTimeoutSeconds (${String(queues.visibilityTimeoutSeconds)}) must be at least ` +
        `${String(VISIBILITY_TIMEOUT_MULTIPLIER)} x consumerTimeoutSeconds (${String(minimumVisibility)})`,
    );
  }

  if (cleanup.graceHours * 60 <= workflow.executionTimeoutMinutes) {
    issues.push(
      `/operations/cleanup/graceHours (${String(cleanup.graceHours)}h) must exceed ` +
        `workflow.executionTimeoutMinutes (${String(workflow.executionTimeoutMinutes)}min) so cleanup never targets running work`,
    );
  }

  const { existingRoleArn } = config.apiGatewayLogging;
  const roleAccount = existingRoleArn?.split(':')[4];
  if (roleAccount !== undefined && roleAccount !== config.managementAccountId) {
    issues.push(
      `/apiGatewayLogging/existingRoleArn is in account ${roleAccount} but API Gateway needs a role in the management account ${config.managementAccountId}`,
    );
  }

  if (config.rawRetentionDays * 24 <= cleanup.graceHours) {
    issues.push(
      `/rawRetentionDays (${String(config.rawRetentionDays)}d) must exceed operations.cleanup.graceHours ` +
        `(${String(cleanup.graceHours)}h) so raw inputs do not expire before cleanup can reconcile them`,
    );
  }
  return issues;
}

/** Validates a parsed config object (schema, defaults, then cross-field rules). */
export function parseConfig(raw: unknown, source = 'configuration'): CaseInsightsConfig {
  const config = validateAgainstSchema(CONFIG_SCHEMA_PATH, raw, source) as CaseInsightsConfig;
  const issues = semanticIssues(config);
  if (issues.length > 0) {
    throw new ConfigValidationError(source, issues);
  }
  return config;
}

export function loadConfigFile(filePath: string): CaseInsightsConfig {
  return parseConfig(readJsonFile(filePath), filePath);
}

/** Resolves the required `-c env=<name>` context value. */
export function resolveEnvironmentName(value: unknown): EnvironmentName {
  if (typeof value === 'string' && (ENVIRONMENT_NAMES as readonly string[]).includes(value)) {
    return value as EnvironmentName;
  }
  const shown = value === undefined ? 'missing' : JSON.stringify(value);
  throw new ConfigValidationError('CDK context "env"', [
    `expected one of ${ENVIRONMENT_NAMES.join(', ')} (pass -c env=<name>); got ${shown}`,
  ]);
}

export interface LoadEnvironmentConfigOptions {
  /** Directory containing <env>.json; defaults to the repository config/ directory. */
  readonly configDir?: string;
}

/** Loads config/<env>.json and checks that its `environment` field matches the requested environment. */
export function loadEnvironmentConfig(env: EnvironmentName, options: LoadEnvironmentConfigOptions = {}): CaseInsightsConfig {
  const filePath = path.join(options.configDir ?? CONFIG_DIR, `${env}.json`);
  const config = loadConfigFile(filePath);
  if (config.environment !== env) {
    throw new ConfigValidationError(filePath, [
      `/environment is "${config.environment}" but the file was loaded for -c env=${env}`,
    ]);
  }
  return config;
}

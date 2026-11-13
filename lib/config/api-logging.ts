import { existsSync } from 'node:fs';
import * as path from 'node:path';
import { loadEnvironmentConfig } from './load';
import { CONFIG_DIR, ConfigValidationError } from './schema-validator';
import { type ApiGatewayLoggingConfig, type CaseInsightsConfig, ENVIRONMENT_NAMES, type EnvironmentName } from './types';

export function sameApiGatewayLoggingSettings(a: ApiGatewayLoggingConfig, b: ApiGatewayLoggingConfig): boolean {
  return a.mode === b.mode && a.sharedId === b.sharedId && a.existingRoleArn === b.existingRoleArn;
}

function label(config: CaseInsightsConfig): string {
  return `${config.environment} (${config.uniqueId})`;
}

/**
 * The API Gateway CloudWatch role is one setting per account and Region, so every environment deployed
 * there must name the same owner: otherwise two shared stacks (or a shared stack and an external owner)
 * would overwrite each other's AWS::ApiGateway::Account setting.
 */
export function validateSharedApiLoggingConsistency(configs: readonly CaseInsightsConfig[]): void {
  const issues: string[] = [];
  configs.forEach((config, index) => {
    for (const other of configs.slice(index + 1)) {
      const sameTarget = config.managementAccountId === other.managementAccountId && config.region === other.region;
      if (sameTarget && !sameApiGatewayLoggingSettings(config.apiGatewayLogging, other.apiGatewayLogging)) {
        issues.push(
          `${label(config)} and ${label(other)} deploy to the same account and Region must use the same apiGatewayLogging settings ` +
            `(${JSON.stringify(config.apiGatewayLogging)} versus ${JSON.stringify(other.apiGatewayLogging)})`,
        );
      }
    }
  });
  if (issues.length > 0) {
    throw new ConfigValidationError('shared API Gateway logging', issues);
  }
}

/** Loads every other config/<env>.json present in `configDir`, so standalone synthesis can check shared settings. */
export function loadOtherEnvironmentConfigs(env: EnvironmentName, configDir: string = CONFIG_DIR): CaseInsightsConfig[] {
  return ENVIRONMENT_NAMES.filter((name) => name !== env && existsSync(path.join(configDir, `${name}.json`))).map((name) =>
    loadEnvironmentConfig(name, { configDir }),
  );
}

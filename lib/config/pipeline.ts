import * as path from 'node:path';
import type { CaseInsightsConfig, PipelineConfig } from './types';
import { CONFIG_DIR, ConfigValidationError, PIPELINE_SCHEMA_PATH, readJsonFile, validateAgainstSchema } from './schema-validator';

export const PIPELINE_CONFIG_FILE = 'pipeline.json';

export function parsePipelineConfig(raw: unknown, source = 'pipeline configuration'): PipelineConfig {
  const config = validateAgainstSchema(PIPELINE_SCHEMA_PATH, raw, source) as PipelineConfig;
  const connectionAccount = config.connectionArn.split(':')[4];
  if (connectionAccount !== config.account) {
    throw new ConfigValidationError(source, [
      `/connectionArn is in account ${String(connectionAccount)} but the pipeline account is ${config.account}`,
    ]);
  }
  return config;
}

export function loadPipelineConfig(filePath = path.join(CONFIG_DIR, PIPELINE_CONFIG_FILE)): PipelineConfig {
  return parsePipelineConfig(readJsonFile(filePath), filePath);
}

/**
 * Checks that the dev and prod stage configurations can be deployed side by side by one pipeline:
 * distinct namespaces in the same organization and management account.
 */
export function validatePipelineStages(pipeline: PipelineConfig, dev: CaseInsightsConfig, prod: CaseInsightsConfig): void {
  const issues: string[] = [];
  if (dev.environment !== 'dev') issues.push(`dev stage configuration declares environment "${dev.environment}"`);
  if (prod.environment !== 'prod') issues.push(`prod stage configuration declares environment "${prod.environment}"`);
  if (dev.uniqueId === prod.uniqueId) {
    issues.push(`dev and prod must use different uniqueId values (both are "${dev.uniqueId}")`);
  }
  if (dev.orgId !== prod.orgId) issues.push(`dev orgId ${dev.orgId} differs from prod orgId ${prod.orgId}`);
  for (const stage of [dev, prod]) {
    if (stage.managementAccountId !== pipeline.account) {
      issues.push(
        `${stage.environment} managementAccountId ${stage.managementAccountId} differs from pipeline account ${pipeline.account}`,
      );
    }
  }
  if (issues.length > 0) {
    throw new ConfigValidationError('pipeline stages', issues);
  }
}

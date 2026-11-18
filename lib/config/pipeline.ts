import * as path from 'node:path';
import { validateSharedApiLoggingConsistency } from './api-logging';
import { loadEnvironmentConfig, resolveEnvironmentName } from './load';
import type { CaseInsightsConfig, EnvironmentName, PipelineConfig } from './types';
import { CONFIG_DIR, ConfigValidationError, PIPELINE_SCHEMA_PATH, readJsonFile, validateAgainstSchema } from './schema-validator';

export const PIPELINE_CONFIG_FILE = 'pipeline.json';

/** CDK context key that selects the optional pipeline app (`-c pipeline=true`). */
export const PIPELINE_CONTEXT_KEY = 'pipeline';

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
 * distinct namespaces in the same organization, management account and Region as the pipeline, sharing
 * one API Gateway logging owner.
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
    // A stage in another Region would need cross-Region replication buckets and support stacks,
    // which this pipeline deliberately does not create.
    if (stage.region !== pipeline.region) {
      issues.push(`${stage.environment} region ${stage.region} differs from pipeline region ${pipeline.region}`);
    }
  }
  if (issues.length > 0) {
    throw new ConfigValidationError('pipeline stages', issues);
  }
  validateSharedApiLoggingConsistency([dev, prod]);
}

export interface PipelineDeploymentConfig {
  readonly pipeline: PipelineConfig;
  readonly dev: CaseInsightsConfig;
  readonly prod: CaseInsightsConfig;
}

/** Loads and cross-validates config/pipeline.json, config/dev.json and config/prod.json. */
export function loadPipelineDeploymentConfig(configDir: string = CONFIG_DIR): PipelineDeploymentConfig {
  const pipeline = loadPipelineConfig(path.join(configDir, PIPELINE_CONFIG_FILE));
  const dev = loadEnvironmentConfig('dev', { configDir });
  const prod = loadEnvironmentConfig('prod', { configDir });
  validatePipelineStages(pipeline, dev, prod);
  return { pipeline, dev, prod };
}

export type AppMode = { readonly kind: 'standalone'; readonly env: EnvironmentName } | { readonly kind: 'pipeline' };

/**
 * Selects the app from CDK context. Standalone (`-c env=<env>`) is the default; the pipeline app is
 * opt-in with `-c pipeline=true` and takes no `env`, because it deploys both environments.
 */
export function resolveAppMode(pipelineContext: unknown, envContext: unknown): AppMode {
  if (pipelineContext === undefined) {
    return { kind: 'standalone', env: resolveEnvironmentName(envContext) };
  }
  if (pipelineContext !== true && pipelineContext !== 'true') {
    throw new ConfigValidationError(`CDK context "${PIPELINE_CONTEXT_KEY}"`, [
      `expected true (pass -c ${PIPELINE_CONTEXT_KEY}=true) or no value; got ${JSON.stringify(pipelineContext)}`,
    ]);
  }
  if (envContext !== undefined) {
    throw new ConfigValidationError('CDK context', [
      `-c ${PIPELINE_CONTEXT_KEY}=true deploys both environments through the pipeline; do not also pass -c env=${typeof envContext === 'string' ? envContext : JSON.stringify(envContext)}`,
    ]);
  }
  return { kind: 'pipeline' };
}

import { type App, Tags } from 'aws-cdk-lib';
import { addCaseInsightsStacks, type CaseInsightsAppStacks } from './app';
import { CONFIG_DIR, loadPipelineDeploymentConfig, PIPELINE_CONTEXT_KEY, resolveAppMode } from './config';
import { enableNagChecks } from './nag';
import { CaseInsightsPipelineStack, PIPELINE_STACK_NAME } from './stacks/pipeline-stack';

export interface CaseInsightsPipelineAppOptions {
  /** Directory containing pipeline.json, dev.json and prod.json; defaults to the repository config/ directory. */
  readonly configDir?: string;
}

/**
 * Validates config/pipeline.json against config/dev.json and config/prod.json, registers cdk-nag and adds
 * the single CaseInsightsPipelineStack to `app`. Throws ConfigValidationError before any stack is created
 * when any of the three files is missing or invalid.
 */
export function addCaseInsightsPipeline(app: App, options: CaseInsightsPipelineAppOptions = {}): CaseInsightsPipelineStack {
  const { pipeline, dev, prod } = loadPipelineDeploymentConfig(options.configDir ?? CONFIG_DIR);
  enableNagChecks(app);
  const stack = new CaseInsightsPipelineStack(app, PIPELINE_STACK_NAME, {
    stackName: PIPELINE_STACK_NAME,
    description: `Support Case Insights CDK pipeline: ${pipeline.repository}@${pipeline.branch} -> ${dev.uniqueId} -> approval -> ${prod.uniqueId}`,
    env: { account: pipeline.account, region: pipeline.region },
    pipeline,
    dev,
    prod,
  });
  Tags.of(stack).add('Project', 'SupportCaseInsights');
  return stack;
}

export type CaseInsightsApp =
  | { readonly kind: 'standalone'; readonly stacks: CaseInsightsAppStacks }
  | { readonly kind: 'pipeline'; readonly pipeline: CaseInsightsPipelineStack };

export interface CaseInsightsAppFromContextOptions {
  readonly configDir?: string;
}

/**
 * Entry point behaviour: `-c env=<env>` adds that environment's standalone stacks (unchanged by the
 * optional pipeline configuration); `-c pipeline=true` adds only the pipeline stack.
 */
export function addCaseInsightsApp(app: App, options: CaseInsightsAppFromContextOptions = {}): CaseInsightsApp {
  const mode = resolveAppMode(app.node.tryGetContext(PIPELINE_CONTEXT_KEY), app.node.tryGetContext('env'));
  if (mode.kind === 'pipeline') {
    return { kind: 'pipeline', pipeline: addCaseInsightsPipeline(app, { configDir: options.configDir }) };
  }
  return { kind: 'standalone', stacks: addCaseInsightsStacks(app, { env: mode.env, configDir: options.configDir }) };
}

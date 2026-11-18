import { CfnElement, Names, Stage, type StageProps } from 'aws-cdk-lib';
import { Construct, RootConstruct } from 'constructs';
import { addCaseInsightsEnvironmentStacks, addSharedApiLogging, type CaseInsightsEnvironmentStacks, type SharedApiLogging } from '../app';
import type { CaseInsightsConfig } from '../config';

export interface CaseInsightsSharedApiLoggingStageProps extends StageProps {
  /** Every environment the pipeline deploys to this account and Region. */
  readonly configs: readonly CaseInsightsConfig[];
}

/**
 * Pipeline stage holding only the shared CaseInsightsApiLoggingStack-<sharedId>: the single owner of the
 * account/Region API Gateway CloudWatch role setting. The pipeline deploys it before the environment
 * stages, which reuse it and never contain an AWS::ApiGateway::Account resource themselves.
 */
export class CaseInsightsSharedApiLoggingStage extends Stage {
  public readonly sharedApiLogging: SharedApiLogging;

  public constructor(scope: Construct, id: string, props: CaseInsightsSharedApiLoggingStageProps) {
    super(scope, id, props);
    const shared = addSharedApiLogging(this, props.configs);
    if (shared?.stack === undefined) {
      throw new Error('CaseInsightsSharedApiLoggingStage needs an environment with MCP in managed apiGatewayLogging mode');
    }
    this.sharedApiLogging = shared;
    alignLogicalIdsWithStandalone(this);
  }
}

export interface CaseInsightsEnvironmentStageProps extends StageProps {
  readonly config: CaseInsightsConfig;
  /** Required when config.enableMcp is true; created outside this stage. */
  readonly sharedApiLogging?: SharedApiLogging;
}

/** One environment's stacks (exactly those of standalone `cdk deploy --all -c env=<env>`, minus shared ones). */
export class CaseInsightsEnvironmentStage extends Stage {
  public readonly stacks: CaseInsightsEnvironmentStacks;

  public constructor(scope: Construct, id: string, props: CaseInsightsEnvironmentStageProps) {
    super(scope, id, props);
    this.stacks = addCaseInsightsEnvironmentStacks(this, props.config, { sharedApiLogging: props.sharedApiLogging });
    alignLogicalIdsWithStandalone(this);
  }
}

/**
 * Some L2 constructs (SqsEventSource, LambdaDestination) embed Names.nodeUniqueId of another construct in a
 * child id. That id includes every scope above the stack, so nesting a stack in a pipeline stage would change
 * those logical IDs and switching a deployment between standalone and pipeline mode would replace the
 * resources (Lambda rejects a second SQS event source mapping for the same queue and function). Rewriting the
 * embedded ids relative to the stage keeps every logical ID identical to standalone deploy-all.
 */
function alignLogicalIdsWithStandalone(stage: Stage): void {
  const depth = stage.node.scopes.length;
  const stagePrefix = Names.nodeUniqueId(stage.node).slice(0, -8);
  const standaloneIds = new Map<string, string>();
  for (const construct of stage.node.findAll()) {
    if (construct !== stage) {
      standaloneIds.set(Names.nodeUniqueId(construct.node), uniqueIdOf(construct.node.scopes.slice(depth).map((scope) => scope.node.id)));
    }
  }
  const rewrite = (component: string): string => {
    if (!component.includes(stagePrefix)) {
      return component;
    }
    let rewritten = component;
    for (const [nested, standalone] of standaloneIds) {
      if (rewritten.includes(nested)) {
        rewritten = rewritten.split(nested).join(standalone);
      }
    }
    return rewritten;
  };
  for (const construct of stage.node.findAll()) {
    if (!CfnElement.isCfnElement(construct)) {
      continue;
    }
    const scopes = construct.node.scopes;
    const components = scopes.slice(scopes.indexOf(construct.stack) + 1).map((scope) => scope.node.id);
    const rewritten = components.map(rewrite);
    if (rewritten.some((component, index) => component !== components[index])) {
      construct.overrideLogicalId(uniqueIdOf(rewritten));
    }
  }
}

/** The unique id CDK derives for a construct at this path below the app root (also Stack.allocateLogicalId). */
function uniqueIdOf(ids: readonly string[]): string {
  let scope: Construct = new RootConstruct();
  for (const id of ids) {
    scope = new Construct(scope, id);
  }
  return Names.nodeUniqueId(scope.node);
}

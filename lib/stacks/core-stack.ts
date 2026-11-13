import { Stack, type StackProps } from 'aws-cdk-lib';
import type { Construct } from 'constructs';
import type { CaseInsightsConfig } from '../config';
import { CoreStorage } from '../constructs/core-storage';
import { MemberAccessRoles } from '../constructs/member-access-roles';
import { PipelineQueues } from '../constructs/pipeline-queues';
import { SharedLayers } from '../constructs/shared-layers';

export interface CaseInsightsCoreStackProps extends StackProps {
  readonly config: CaseInsightsConfig;
}

/**
 * Core pipeline stack: account discovery, case collection, case analysis and observability.
 * It owns the shared foundation those constructs receive as typed props: storage, stage queues,
 * the execution roles trusted by member accounts and the shared Lambda layers.
 */
export class CaseInsightsCoreStack extends Stack {
  public readonly config: CaseInsightsConfig;
  public readonly layers: SharedLayers;
  public readonly storage: CoreStorage;
  public readonly queues: PipelineQueues;
  public readonly memberAccess: MemberAccessRoles;

  public constructor(scope: Construct, id: string, props: CaseInsightsCoreStackProps) {
    super(scope, id, props);
    this.config = props.config;
    this.storage = new CoreStorage(this, 'Storage', { config: props.config });
    this.queues = new PipelineQueues(this, 'Queues', { config: props.config });
    this.memberAccess = new MemberAccessRoles(this, 'MemberAccess', { config: props.config });
    this.layers = new SharedLayers(this, 'SharedLayers', { uniqueId: props.config.uniqueId });
  }
}

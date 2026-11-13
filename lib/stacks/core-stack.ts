import { Stack, type StackProps } from 'aws-cdk-lib';
import type { Construct } from 'constructs';
import type { CaseInsightsConfig } from '../config';
import { SharedLayers } from '../constructs/shared-layers';

export interface CaseInsightsCoreStackProps extends StackProps {
  readonly config: CaseInsightsConfig;
}

/**
 * Core pipeline stack: account discovery, case collection, case analysis and observability.
 * Resources are added by the AccountDiscovery, CaseCollection, CaseAnalysis and Observability
 * constructs; this class owns the typed configuration and shared Lambda layers they use.
 */
export class CaseInsightsCoreStack extends Stack {
  public readonly config: CaseInsightsConfig;
  public readonly layers: SharedLayers;

  public constructor(scope: Construct, id: string, props: CaseInsightsCoreStackProps) {
    super(scope, id, props);
    this.config = props.config;
    this.layers = new SharedLayers(this, 'SharedLayers', { uniqueId: props.config.uniqueId });
  }
}

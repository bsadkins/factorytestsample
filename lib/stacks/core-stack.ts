import { CfnResource, Stack, type StackProps, Validations } from 'aws-cdk-lib';
import type { Construct } from 'constructs';
import type { CaseInsightsConfig } from '../config';

export interface CaseInsightsCoreStackProps extends StackProps {
  readonly config: CaseInsightsConfig;
}

/**
 * Core pipeline stack: account discovery, case collection, case analysis and observability.
 * Resources are added by the AccountDiscovery, CaseCollection, CaseAnalysis and Observability
 * constructs; this class owns the typed configuration they share.
 */
export class CaseInsightsCoreStack extends Stack {
  public readonly config: CaseInsightsConfig;

  public constructor(scope: Construct, id: string, props: CaseInsightsCoreStackProps) {
    super(scope, id, props);
    this.config = props.config;

    // CDK's default CloudFormation rules reject a template without resources. The acknowledgement
    // only applies while no resources exist, so it can never hide the finding on a populated stack.
    if (!this.node.findAll().some((construct) => construct instanceof CfnResource)) {
      Validations.of(this).acknowledge({
        id: 'CloudFormation-Validate::F0001',
        reason: 'Scaffold stack: no core resources are defined yet, so the template is intentionally empty and is not deployable.',
      });
    }
  }
}

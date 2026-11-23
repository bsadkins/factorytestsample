import { Stack, type StackProps } from 'aws-cdk-lib';
import type { IBucket } from 'aws-cdk-lib/aws-s3';
import type { Construct } from 'constructs';
import type { CaseInsightsConfig } from '../config';
import { Analytics } from '../constructs/analytics';

export interface CaseInsightsAnalyticsStackProps extends StackProps {
  readonly config: CaseInsightsConfig;
  /** Core processed-record bucket queried through Glue and Athena. */
  readonly processedBucket: IBucket;
  /** Core server access-log bucket that also receives the Athena results bucket's logs. */
  readonly accessLogsBucket: IBucket;
}

/**
 * Optional Analytics stack (`enableAnalytics`): the Glue catalog, partition crawler and enforced
 * Athena workgroup over Core's processed records. It reads Core outputs; Core never references it.
 */
export class CaseInsightsAnalyticsStack extends Stack {
  public readonly analytics: Analytics;

  public constructor(scope: Construct, id: string, props: CaseInsightsAnalyticsStackProps) {
    super(scope, id, props);
    this.analytics = new Analytics(this, 'Analytics', {
      config: props.config,
      processedBucket: props.processedBucket,
      accessLogsBucket: props.accessLogsBucket,
    });
  }
}

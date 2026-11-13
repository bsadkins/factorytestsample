import { Stack, type StackProps } from 'aws-cdk-lib';
import type { ILayerVersion } from 'aws-cdk-lib/aws-lambda';
import type { Construct } from 'constructs';
import type { CaseInsightsConfig } from '../config';
import { type McpAnalyticsTarget, McpServer } from '../constructs/mcp-server';

export interface CaseInsightsMcpStackProps extends StackProps {
  readonly config: CaseInsightsConfig;
  /** Core shared layer providing `common` and the pinned runtime dependencies. */
  readonly commonLayer: ILayerVersion;
  /** The Analytics catalog, workgroup and buckets the tools query. */
  readonly analytics: McpAnalyticsTarget;
}

/**
 * Optional MCP stack (`enableMcp`, which requires `enableAnalytics`): the SigV4-authorised API Gateway
 * endpoint, WAF web ACL and MCP function. It reads Core and Analytics outputs; neither references it.
 * Its alarms and dashboard widgets live in Core and use literal resource names (see mcp-observability).
 */
export class CaseInsightsMcpStack extends Stack {
  public readonly mcpServer: McpServer;

  public constructor(scope: Construct, id: string, props: CaseInsightsMcpStackProps) {
    super(scope, id, props);
    this.mcpServer = new McpServer(this, 'McpServer', {
      config: props.config,
      commonLayer: props.commonLayer,
      analytics: props.analytics,
    });
  }
}

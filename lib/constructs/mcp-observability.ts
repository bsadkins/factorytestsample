import { Duration } from 'aws-cdk-lib';
import { GraphWidget, type IMetric, Metric, Stats } from 'aws-cdk-lib/aws-cloudwatch';
import { type CaseInsightsConfig, resourceNames } from '../config';
import { MCP_AUTH_FAILURES_METRIC, MCP_SERVICE_NAME, MCP_STAGE_NAME } from './mcp-server';
import { MCP_WAF_RULES } from './mcp-web-acl';
import { type Observability, serviceMetric } from './observability';
import { METRICS_NAMESPACE } from './pipeline-function';

/** Alarm purposes (alarm names without the deployment prefix); each has a `### <purpose>` runbook section. */
export const MCP_ALARM_PURPOSES = {
  lambdaErrors: 'LambdaErrors-McpServer',
  internalErrors: 'McpInternalErrors',
  serverErrors: 'McpApiServerErrors',
  authFailures: 'McpAuthFailures',
} as const;

/**
 * Refused SigV4 requests in five minutes before the auth-failure alarm fires. A client with an expired
 * session produces a few refusals before it refreshes; a sustained stream points to probing or a
 * misconfigured client.
 */
export const MCP_AUTH_FAILURE_ALARM_THRESHOLD = 10;

const PERIOD = Duration.minutes(5);

/**
 * Adds the MCP alarms and dashboard widgets to Core's observability. Every metric is addressed by the
 * literal names the MCP stack gives its function, API, stage and web ACL, so Core never references the
 * MCP stack and deploys without it.
 */
export function addMcpObservability(observability: Observability, config: CaseInsightsConfig): void {
  const names = resourceNames(config);
  const sum = (namespace: string, metricName: string, dimensionsMap: Record<string, string>, label?: string): Metric =>
    new Metric({ namespace, metricName, dimensionsMap, period: PERIOD, statistic: Stats.SUM, label });
  const lambdaErrors = sum('AWS/Lambda', 'Errors', { FunctionName: names.mcpServerFunction });
  const api = (metricName: string, label?: string): Metric => sum('AWS/ApiGateway', metricName, { ApiName: names.mcpApi, Stage: MCP_STAGE_NAME }, label);
  const authFailures = sum(METRICS_NAMESPACE, MCP_AUTH_FAILURES_METRIC, { ApiName: names.mcpApi });
  const mcp = (metricName: string): Metric => serviceMetric(MCP_SERVICE_NAME, metricName);

  observability.addAlarm(MCP_ALARM_PURPOSES.lambdaErrors, 'The McpServer function reported invocation errors.', lambdaErrors);
  observability.addAlarm(
    MCP_ALARM_PURPOSES.internalErrors,
    'The MCP server answered requests with a JSON-RPC internal error after an unexpected failure.',
    mcp('McpInternalErrors'),
  );
  observability.addAlarm(MCP_ALARM_PURPOSES.serverErrors, 'The MCP API returned 5xx responses on stage v1.', api('5XXError'));
  observability.addAlarm(
    MCP_ALARM_PURPOSES.authFailures,
    `The MCP API refused at least ${String(MCP_AUTH_FAILURE_ALARM_THRESHOLD)} requests in five minutes for failed SigV4 authentication or resource-policy authorisation.`,
    authFailures,
    MCP_AUTH_FAILURE_ALARM_THRESHOLD,
  );

  const graph = (title: string, metrics: IMetric[]): GraphWidget => new GraphWidget({ title, left: metrics, width: 8, height: 6 });
  const waf = (rule: string, label: string): Metric =>
    sum('AWS/WAFV2', 'BlockedRequests', { WebACL: names.mcpWebAcl, Region: config.region, Rule: rule }, label);
  observability.dashboard.addWidgets(
    graph('MCP requests and errors', [
      mcp('McpRequests'),
      mcp('McpToolCalls'),
      mcp('McpErrors'),
      mcp('McpProtocolErrors'),
      mcp('McpToolErrors'),
      mcp('McpInternalErrors'),
      lambdaErrors.with({ label: 'McpServer Lambda errors' }),
    ]),
    graph('MCP API responses (stage v1)', [api('Count', 'Requests'), api('4XXError', '4xx'), api('5XXError', '5xx'), authFailures.with({ label: 'Auth failures' })]),
    graph('MCP WAF blocked requests', [
      waf('ALL', 'All rules'),
      waf(MCP_WAF_RULES.rateLimit, 'Rate limit'),
      waf(MCP_WAF_RULES.bodySize, 'Body over 32 KiB'),
      waf(MCP_WAF_RULES.commonRuleSet, 'Common rule set'),
    ]),
  );
}

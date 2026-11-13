import type { CaseInsightsConfig } from './types';

/**
 * Resource names from the original design. Every name includes uniqueId so dev and prod
 * deployments can coexist in the same accounts.
 */
export interface ResourceNames {
  readonly coreStack: string;
  readonly memberRoleStack: string;
  readonly analyticsStack: string;
  readonly mcpStack: string;
  readonly memberRoleStackSet: string;
  readonly memberRole: string;
  /** Management-account execution roles trusted by the member role. */
  readonly caseRetrievalRole: string;
  readonly caseAnnotationRole: string;
  readonly activeAccountsQueue: string;
  readonly annotationQueue: string;
  readonly summaryQueue: string;
  readonly workflowFailureQueue: string;
  readonly accountLookupFunction: string;
  readonly accountReaderFunction: string;
  readonly accountLookupSchedule: string;
  readonly caseRetrievalFunction: string;
  readonly caseAnnotationFunction: string;
  /** Terminal queue for discovery schedule deliveries and invocations that exhaust retries. */
  readonly accountDiscoveryFailureQueue: string;
  readonly caseAnalysisStateMachine: string;
  readonly workflowStarterFunction: string;
  /** One function hosting the Summary, RCA and Lifecycle tasks; see CaseAnalysis. */
  readonly caseAnalysisStagesFunction: string;
  readonly updateCaseMetadataFunction: string;
  readonly handleFailureFunction: string;
  readonly caseCleanupFunction: string;
  readonly caseCleanupSchedule: string;
  readonly runCompletionFunction: string;
  readonly runCompletionSchedule: string;
  readonly alarmTopic: string;
  readonly dashboard: string;
  /** Prefix of every CloudWatch alarm name, followed by the alarm's purpose. */
  readonly alarmPrefix: string;
  readonly glueDatabase: string;
  readonly glueTable: string;
  readonly crawler: string;
  readonly athenaWorkgroup: string;
  readonly mcpServerFunction: string;
  /** REST API name; also the ApiName dimension of its CloudWatch metrics. */
  readonly mcpApi: string;
  readonly mcpWebAcl: string;
  readonly mcpAccessLogGroup: string;
}

export function resourceNames(config: Pick<CaseInsightsConfig, 'uniqueId'>): ResourceNames {
  const id = config.uniqueId;
  return {
    coreStack: `CaseInsightsCoreStack-${id}`,
    memberRoleStack: `CaseInsightsMemberRoleStack-${id}`,
    analyticsStack: `CaseInsightsAnalyticsStack-${id}`,
    mcpStack: `CaseInsightsMcpStack-${id}`,
    memberRoleStackSet: `CaseInsights-MemberRole-${id}`,
    memberRole: `Support-Case-Analysis-Role-${id}`,
    caseRetrievalRole: `CaseInsights-CaseRetrieval-${id}`,
    caseAnnotationRole: `CaseInsights-CaseAnnotation-${id}`,
    activeAccountsQueue: `CaseInsights-ActiveAccounts-${id}`,
    annotationQueue: `CaseInsights-CaseAnnotation-${id}`,
    summaryQueue: `CaseInsights-CaseSummary-${id}`,
    workflowFailureQueue: `CaseInsights-WorkflowFailures-${id}`,
    accountLookupFunction: `Lambda-AccountLookup-${id}`,
    accountReaderFunction: `Lambda-AccountReader-${id}`,
    accountLookupSchedule: `CaseInsights-AccountLookup-${id}`,
    caseRetrievalFunction: `Lambda-CaseRetrieval-${id}`,
    caseAnnotationFunction: `Lambda-CaseAnnotation-${id}`,
    accountDiscoveryFailureQueue: `CaseInsights-AccountDiscoveryFailures-${id}`,
    caseAnalysisStateMachine: `CaseAnalysis-${id}`,
    workflowStarterFunction: `Lambda-WorkflowStarter-${id}`,
    caseAnalysisStagesFunction: `Lambda-CaseAnalysisStages-${id}`,
    updateCaseMetadataFunction: `Lambda-UpdateCaseMetadata-${id}`,
    handleFailureFunction: `Lambda-HandleFailure-${id}`,
    caseCleanupFunction: `Lambda-CaseCleanup-${id}`,
    caseCleanupSchedule: `CaseInsights-CaseCleanup-${id}`,
    runCompletionFunction: `Lambda-RunCompletion-${id}`,
    runCompletionSchedule: `CaseInsights-RunCompletion-${id}`,
    alarmTopic: `CaseInsights-Alarms-${id}`,
    dashboard: `CaseInsights-${id}`,
    alarmPrefix: `CaseInsights-${id}-`,
    // Glue database names allow only lower-case letters, digits and underscores.
    glueDatabase: `case_insights_${id.replace(/-/g, '_')}`,
    glueTable: 'case_summary',
    crawler: `case-insights-partition-crawler-${id}`,
    athenaWorkgroup: `CaseInsights-${id}`,
    mcpServerFunction: `Lambda-McpServer-${id}`,
    mcpApi: `CaseInsights-Mcp-${id}`,
    mcpWebAcl: `CaseInsights-Mcp-${id}`,
    mcpAccessLogGroup: `/aws/apigateway/CaseInsights-Mcp-${id}/access`,
  };
}

/** Name of the dead-letter queue paired with a stage queue. */
export function deadLetterQueueName(queueName: string): string {
  return `${queueName}-DLQ`;
}

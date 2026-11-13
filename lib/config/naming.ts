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
  /** Terminal queue for discovery schedule deliveries and invocations that exhaust retries. */
  readonly accountDiscoveryFailureQueue: string;
  readonly caseAnalysisStateMachine: string;
  readonly glueDatabase: string;
  readonly glueTable: string;
  readonly crawler: string;
  readonly athenaWorkgroup: string;
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
    accountDiscoveryFailureQueue: `CaseInsights-AccountDiscoveryFailures-${id}`,
    caseAnalysisStateMachine: `CaseAnalysis-${id}`,
    // Glue database names allow only lower-case letters, digits and underscores.
    glueDatabase: `case_insights_${id.replace(/-/g, '_')}`,
    glueTable: 'case_summary',
    crawler: `case-insights-partition-crawler-${id}`,
    athenaWorkgroup: `CaseInsights-${id}`,
  };
}

/** Name of the dead-letter queue paired with a stage queue. */
export function deadLetterQueueName(queueName: string): string {
  return `${queueName}-DLQ`;
}

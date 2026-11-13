import type { CaseInsightsConfig } from './types';

/**
 * Resource names from the original design. Every name includes uniqueId so dev and prod
 * deployments can coexist in the same accounts.
 */
export interface ResourceNames {
  readonly coreStack: string;
  readonly analyticsStack: string;
  readonly mcpStack: string;
  readonly memberRoleStackSet: string;
  readonly memberRole: string;
  readonly accountLookupFunction: string;
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
    analyticsStack: `CaseInsightsAnalyticsStack-${id}`,
    mcpStack: `CaseInsightsMcpStack-${id}`,
    memberRoleStackSet: `CaseInsights-MemberRole-${id}`,
    memberRole: `Support-Case-Analysis-Role-${id}`,
    accountLookupFunction: `Lambda-AccountLookup-${id}`,
    caseAnalysisStateMachine: `CaseAnalysis-${id}`,
    // Glue database names allow only lower-case letters, digits and underscores.
    glueDatabase: `case_insights_${id.replace(/-/g, '_')}`,
    glueTable: 'case_summary',
    crawler: `case-insights-partition-crawler-${id}`,
    athenaWorkgroup: `CaseInsights-${id}`,
  };
}

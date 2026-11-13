import { deadLetterQueueName, resourceNames } from '../../lib';

describe('resource names', () => {
  test('match the original design and include uniqueId', () => {
    expect(resourceNames({ uniqueId: 'sci-dev' })).toEqual({
      coreStack: 'CaseInsightsCoreStack-sci-dev',
      memberRoleStack: 'CaseInsightsMemberRoleStack-sci-dev',
      analyticsStack: 'CaseInsightsAnalyticsStack-sci-dev',
      mcpStack: 'CaseInsightsMcpStack-sci-dev',
      memberRoleStackSet: 'CaseInsights-MemberRole-sci-dev',
      memberRole: 'Support-Case-Analysis-Role-sci-dev',
      caseRetrievalRole: 'CaseInsights-CaseRetrieval-sci-dev',
      caseAnnotationRole: 'CaseInsights-CaseAnnotation-sci-dev',
      activeAccountsQueue: 'CaseInsights-ActiveAccounts-sci-dev',
      annotationQueue: 'CaseInsights-CaseAnnotation-sci-dev',
      summaryQueue: 'CaseInsights-CaseSummary-sci-dev',
      workflowFailureQueue: 'CaseInsights-WorkflowFailures-sci-dev',
      accountLookupFunction: 'Lambda-AccountLookup-sci-dev',
      accountReaderFunction: 'Lambda-AccountReader-sci-dev',
      accountLookupSchedule: 'CaseInsights-AccountLookup-sci-dev',
      accountDiscoveryFailureQueue: 'CaseInsights-AccountDiscoveryFailures-sci-dev',
      caseAnalysisStateMachine: 'CaseAnalysis-sci-dev',
      glueDatabase: 'case_insights_sci_dev',
      glueTable: 'case_summary',
      crawler: 'case-insights-partition-crawler-sci-dev',
      athenaWorkgroup: 'CaseInsights-sci-dev',
    });
    expect(deadLetterQueueName('CaseInsights-CaseSummary-sci-dev')).toBe('CaseInsights-CaseSummary-sci-dev-DLQ');
  });

  test('stay within AWS name limits at the maximum uniqueId length', () => {
    const names = resourceNames({ uniqueId: 'a'.repeat(12) });
    expect(names.memberRole.length).toBeLessThanOrEqual(64);
    expect(names.caseRetrievalRole.length).toBeLessThanOrEqual(64);
    expect(names.caseAnnotationRole.length).toBeLessThanOrEqual(64);
    expect(names.accountLookupFunction.length).toBeLessThanOrEqual(64);
    expect(names.accountReaderFunction.length).toBeLessThanOrEqual(64);
    expect(names.accountLookupSchedule.length).toBeLessThanOrEqual(64);
    expect(names.accountDiscoveryFailureQueue.length).toBeLessThanOrEqual(80);
    expect(names.caseAnalysisStateMachine.length).toBeLessThanOrEqual(80);
    expect(names.coreStack.length).toBeLessThanOrEqual(128);
    expect(names.memberRoleStack.length).toBeLessThanOrEqual(128);
    expect(names.memberRoleStackSet.length).toBeLessThanOrEqual(128);
    expect(names.athenaWorkgroup.length).toBeLessThanOrEqual(128);
    for (const queue of [names.activeAccountsQueue, names.annotationQueue, names.summaryQueue, names.workflowFailureQueue]) {
      expect(deadLetterQueueName(queue).length).toBeLessThanOrEqual(80);
    }
    expect(names.glueDatabase).toMatch(/^[a-z0-9_]{1,255}$/);
  });
});

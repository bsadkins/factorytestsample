import { resourceNames } from '../../lib';

describe('resource names', () => {
  test('match the original design and include uniqueId', () => {
    expect(resourceNames({ uniqueId: 'sci-dev' })).toEqual({
      coreStack: 'CaseInsightsCoreStack-sci-dev',
      analyticsStack: 'CaseInsightsAnalyticsStack-sci-dev',
      mcpStack: 'CaseInsightsMcpStack-sci-dev',
      memberRoleStackSet: 'CaseInsights-MemberRole-sci-dev',
      memberRole: 'Support-Case-Analysis-Role-sci-dev',
      accountLookupFunction: 'Lambda-AccountLookup-sci-dev',
      caseAnalysisStateMachine: 'CaseAnalysis-sci-dev',
      glueDatabase: 'case_insights_sci_dev',
      glueTable: 'case_summary',
      crawler: 'case-insights-partition-crawler-sci-dev',
      athenaWorkgroup: 'CaseInsights-sci-dev',
    });
  });

  test('stay within AWS name limits at the maximum uniqueId length', () => {
    const names = resourceNames({ uniqueId: 'a'.repeat(12) });
    expect(names.memberRole.length).toBeLessThanOrEqual(64);
    expect(names.accountLookupFunction.length).toBeLessThanOrEqual(64);
    expect(names.caseAnalysisStateMachine.length).toBeLessThanOrEqual(80);
    expect(names.coreStack.length).toBeLessThanOrEqual(128);
    expect(names.memberRoleStackSet.length).toBeLessThanOrEqual(128);
    expect(names.athenaWorkgroup.length).toBeLessThanOrEqual(128);
    expect(names.glueDatabase).toMatch(/^[a-z0-9_]{1,255}$/);
  });
});

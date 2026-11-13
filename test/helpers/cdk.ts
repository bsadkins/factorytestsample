import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { App, Stack, Validations } from 'aws-cdk-lib';
import type { Template } from 'aws-cdk-lib/assertions';
import type { IConstruct } from 'constructs';
import { type CaseInsightsConfig, enableNagChecks, parseConfig, REPO_ROOT } from '../../lib';
import { type JsonObject, minimalConfig } from '../config/helpers';

export function tempOutdir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'case-insights-cdk-'));
}

/**
 * Feature flags from cdk.json. `new App()` does not read cdk.json (the CLI does), so tests pass the
 * same context to synthesise exactly what `npx cdk synth` produces.
 */
export function cdkJsonContext(): Record<string, unknown> {
  const cdkJson = JSON.parse(fs.readFileSync(path.join(REPO_ROOT, 'cdk.json'), 'utf8')) as { context: Record<string, unknown> };
  return cdkJson.context;
}

/** App configured like the CLI-run entry point (cdk.json feature flags), without cdk-nag. */
export function testApp(context: Record<string, unknown> = {}): App {
  return new App({ outdir: tempOutdir(), context: { ...cdkJsonContext(), ...context } });
}

/** A validated configuration built from the minimal dev config plus overrides. */
export function testConfig(overrides: JsonObject = {}): CaseInsightsConfig {
  return parseConfig(minimalConfig(overrides), 'test configuration');
}

/** App with cdk-nag registered exactly as the real entry point does. */
export function nagApp(): App {
  const app = testApp();
  enableNagChecks(app);
  return app;
}

export function testStack(app: App, config: CaseInsightsConfig, id = 'TestStack'): Stack {
  return new Stack(app, id, { env: { account: config.managementAccountId, region: config.region } });
}

/** Runs synthesis (including validation plugins) and returns the error message, or undefined on success. */
export function synthError(app: App): string | undefined {
  try {
    app.synth();
    return undefined;
  } catch (error) {
    return String(error);
  }
}

export interface PolicyStatementJson {
  Sid?: string;
  Effect: 'Allow' | 'Deny';
  Action?: string | string[];
  NotAction?: string | string[];
  Resource?: unknown;
  NotResource?: unknown;
  Condition?: Record<string, Record<string, unknown>>;
  Principal?: unknown;
}

export function asArray<T>(value: T | T[] | undefined): T[] {
  if (value === undefined) {
    return [];
  }
  return Array.isArray(value) ? value : [value];
}

/** Every statement of every AWS::IAM::Policy, keyed by the policy's logical ID. */
export function iamPolicyStatements(template: Template): { logicalId: string; roles: unknown[]; statement: PolicyStatementJson }[] {
  const policies = template.findResources('AWS::IAM::Policy') as Record<
    string,
    { Properties: { Roles?: unknown[]; PolicyDocument: { Statement: PolicyStatementJson[] } } }
  >;
  return Object.entries(policies).flatMap(([logicalId, policy]) =>
    policy.Properties.PolicyDocument.Statement.map((statement) => ({
      logicalId,
      roles: policy.Properties.Roles ?? [],
      statement,
    })),
  );
}

/** cdk-nag acknowledgements recorded under `scope`: finding ID and reason per construct path. */
export function acknowledgedRules(scope: IConstruct): { path: string; id: string; reason: string }[] {
  return scope.node.findAll().flatMap((construct) =>
    construct.node.metadata
      .filter((entry) => entry.type === Validations.ACKNOWLEDGED_RULES_METADATA_KEY)
      .flatMap((entry) =>
        Object.entries(entry.data as Record<string, string>).map(([id, reason]) => ({
          path: construct.node.path,
          id,
          reason,
        })),
      ),
  );
}

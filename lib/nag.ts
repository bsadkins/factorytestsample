import { type App, Stack, Validations } from 'aws-cdk-lib';
import type { CfnPolicy } from 'aws-cdk-lib/aws-iam';
import { AwsSolutionsChecks } from 'cdk-nag';
import { flattenCfnReference } from 'cdk-nag/lib/utils/flatten-cfn-reference';
import type { IConstruct } from 'constructs';

/**
 * Registers the cdk-nag AWS Solutions rule pack as a CDK validation plugin. Any unacknowledged
 * finding makes synthesis fail. Acknowledge individual findings with `acknowledgeNagFinding`.
 */
export function enableNagChecks(app: App): void {
  Validations.of(app).addPlugins(new AwsSolutionsChecks(app, { verbose: true }));
}

const MIN_REASON_LENGTH = 40;

function checkReason(id: string, reason: string): void {
  if (reason.trim().length < MIN_REASON_LENGTH) {
    throw new Error(`Acknowledgement of ${id} needs a specific reason (at least ${String(MIN_REASON_LENGTH)} characters)`);
  }
}

/**
 * Acknowledges one cdk-nag finding on `scope` (and its children) with a specific written reason.
 * Prefer finding-level IDs such as `AwsSolutions-IAM5[Resource::*]` on the narrowest construct
 * (for example a dedicated `iam.Policy`) so the acknowledgement cannot hide unrelated findings.
 */
export function acknowledgeNagFinding(scope: IConstruct, id: string, reason: string): void {
  if (!/^AwsSolutions-[A-Z]+[0-9]+(\[.+\])?$/.test(id)) {
    throw new Error(`Invalid cdk-nag rule or finding ID: ${id}`);
  }
  checkReason(id, reason);
  Validations.of(scope).acknowledge({ id, reason });
}

/**
 * Acknowledges the IAM5 finding for one wildcard `resource` that contains tokens. cdk-nag names the
 * finding after the resource as rendered in the synthesised template, and a cross-stack reference
 * takes its final form only when the app is prepared for synthesis, so the finding ID is computed
 * then, using cdk-nag's own rendering.
 */
export function acknowledgeWildcardResourceFinding(scope: IConstruct, resource: string, reason: string): void {
  checkReason(`AwsSolutions-IAM5[Resource::${resource}]`, reason);
  let acknowledged = false;
  scope.node.addValidation({
    validate: () => {
      if (!acknowledged) {
        acknowledged = true;
        const rendered = flattenCfnReference(Stack.of(scope).resolve(resource));
        acknowledgeNagFinding(scope, `AwsSolutions-IAM5[Resource::${rendered}]`, reason);
      }
      return [];
    },
  });
}

/** One expected wildcard in a policy that aws-cdk-lib generates, as cdk-nag names it (`Action::...` or `Resource::...`). */
export interface GeneratedWildcardRule {
  readonly finding: string | RegExp;
  readonly reason: string;
}

interface PolicyDocumentJson {
  readonly Statement?: readonly { Effect?: string; Action?: unknown; Resource?: unknown }[];
}

function asList(value: unknown): unknown[] {
  if (value === undefined) {
    return [];
  }
  return Array.isArray(value) ? value : [value];
}

/** The AwsSolutions-IAM5 finding names cdk-nag reports for a resolved policy document (same rules as cdk-nag). */
export function iamWildcardFindings(document: PolicyDocumentJson): string[] {
  const findings = new Set<string>();
  for (const statement of document.Statement ?? []) {
    if (statement.Effect !== 'Allow') {
      continue;
    }
    for (const action of asList(statement.Action)) {
      if (typeof action === 'string' && action.includes('*')) {
        findings.add(`Action::${action}`);
      }
    }
    for (const resource of asList(statement.Resource)) {
      const rendered = flattenCfnReference(resource);
      if (rendered.includes('*')) {
        findings.add(`Resource::${rendered}`);
      }
    }
  }
  return [...findings];
}

/**
 * Acknowledges the IAM5 findings of a policy that aws-cdk-lib generates (and this app does not write),
 * one exact finding ID at a time. At synthesis every wildcard cdk-nag would report is matched against
 * `rules`: a match is acknowledged with that rule's reason, any other wildcard still fails synthesis,
 * and a rule that matches nothing is a validation error, so the list cannot silently go stale.
 */
export function acknowledgeGeneratedPolicyWildcards(policy: CfnPolicy, rules: readonly GeneratedWildcardRule[]): void {
  for (const rule of rules) {
    checkReason(`AwsSolutions-IAM5[${String(rule.finding)}]`, rule.reason);
  }
  let evaluated = false;
  policy.node.addValidation({
    validate: () => {
      if (evaluated) {
        return [];
      }
      evaluated = true;
      const document = Stack.of(policy).resolve(policy.policyDocument) as PolicyDocumentJson;
      const used = new Set<GeneratedWildcardRule>();
      for (const finding of iamWildcardFindings(document)) {
        const rule = rules.find(({ finding: expected }) => (typeof expected === 'string' ? expected === finding : expected.test(finding)));
        if (rule !== undefined) {
          used.add(rule);
          acknowledgeNagFinding(policy, `AwsSolutions-IAM5[${finding}]`, rule.reason);
        }
      }
      return rules
        .filter((rule) => !used.has(rule))
        .map((rule) => `wildcard acknowledgement ${String(rule.finding)} matches no AwsSolutions-IAM5 finding of ${policy.node.path}`);
    },
  });
}

/** Acknowledges one aws-cdk-lib warning annotation (for example `@aws-cdk/aws-s3:...`) on `scope`. */
export function acknowledgeCdkWarning(scope: IConstruct, id: string, reason: string): void {
  if (!/^@aws-cdk\/[a-z0-9-]+:[A-Za-z0-9]+$/.test(id)) {
    throw new Error(`Invalid aws-cdk-lib warning ID: ${id}`);
  }
  checkReason(id, reason);
  Validations.of(scope).acknowledge({ id, reason });
}

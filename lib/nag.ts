import { type App, Validations } from 'aws-cdk-lib';
import { AwsSolutionsChecks } from 'cdk-nag';
import type { IConstruct } from 'constructs';

/**
 * Registers the cdk-nag AWS Solutions rule pack as a CDK validation plugin. Any unacknowledged
 * finding makes synthesis fail. Acknowledge individual findings with `acknowledgeNagFinding`.
 */
export function enableNagChecks(app: App): void {
  Validations.of(app).addPlugins(new AwsSolutionsChecks(app, { verbose: true }));
}

const MIN_REASON_LENGTH = 40;

/**
 * Acknowledges one cdk-nag finding on `scope` (and its children) with a specific written reason.
 * Prefer finding-level IDs such as `AwsSolutions-IAM5[Resource::*]` on the narrowest construct
 * (for example a dedicated `iam.Policy`) so the acknowledgement cannot hide unrelated findings.
 */
export function acknowledgeNagFinding(scope: IConstruct, id: string, reason: string): void {
  if (!/^AwsSolutions-[A-Z]+[0-9]+(\[.+\])?$/.test(id)) {
    throw new Error(`Invalid cdk-nag rule or finding ID: ${id}`);
  }
  if (reason.trim().length < MIN_REASON_LENGTH) {
    throw new Error(`cdk-nag acknowledgement of ${id} needs a specific reason (at least ${String(MIN_REASON_LENGTH)} characters)`);
  }
  Validations.of(scope).acknowledge({ id, reason });
}

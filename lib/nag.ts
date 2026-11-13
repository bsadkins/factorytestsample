import { type App, Stack, Validations } from 'aws-cdk-lib';
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

/** Acknowledges one aws-cdk-lib warning annotation (for example `@aws-cdk/aws-s3:...`) on `scope`. */
export function acknowledgeCdkWarning(scope: IConstruct, id: string, reason: string): void {
  if (!/^@aws-cdk\/[a-z0-9-]+:[A-Za-z0-9]+$/.test(id)) {
    throw new Error(`Invalid aws-cdk-lib warning ID: ${id}`);
  }
  checkReason(id, reason);
  Validations.of(scope).acknowledge({ id, reason });
}

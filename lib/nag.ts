import { type App, Validations } from 'aws-cdk-lib';
import { AwsSolutionsChecks } from 'cdk-nag';

/**
 * Registers the cdk-nag AWS Solutions rule pack as a CDK validation plugin. Any unacknowledged
 * finding makes synthesis fail. Acknowledge individual findings with
 * `Validations.of(construct).acknowledge({ id, reason })` and a specific written reason.
 */
export function enableNagChecks(app: App): void {
  Validations.of(app).addPlugins(new AwsSolutionsChecks(app, { verbose: true }));
}

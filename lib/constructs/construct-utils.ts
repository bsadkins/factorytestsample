import * as fs from 'node:fs';
import * as path from 'node:path';
import { type CfnElement, Stack } from 'aws-cdk-lib';
import { ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import type { IConstruct } from 'constructs';
import { REPO_ROOT } from '../packaging/python-assets';

/** Absolute path of a Lambda handler directory under src/lambda; fails synthesis if app.py is missing. */
export function handlerDirectory(name: string): string {
  const dir = path.join(REPO_ROOT, 'src', 'lambda', name);
  if (!fs.existsSync(path.join(dir, 'app.py'))) {
    throw new Error(`Lambda handler not found: ${dir}/app.py`);
  }
  return dir;
}

/** Schedule group of every schedule in this app; a schedule created without a group belongs to it. */
export const SCHEDULE_GROUP = 'default';

/**
 * EventBridge Scheduler as the principal of a schedule execution role, limited to schedules in this
 * account's schedule group. Scheduler sets aws:SourceArn to the schedule group ARN, never to the ARN
 * of an individual schedule, so a schedule ARN condition would deny every assumption; each schedule
 * still gets its own role that can invoke only its own target.
 */
export function schedulerPrincipal(scope: IConstruct): ServicePrincipal {
  const stack = Stack.of(scope);
  return new ServicePrincipal('scheduler.amazonaws.com', {
    conditions: {
      StringEquals: {
        'aws:SourceAccount': stack.account,
        'aws:SourceArn': stack.formatArn({ service: 'scheduler', resource: 'schedule-group', resourceName: SCHEDULE_GROUP }),
      },
    },
  });
}

/** Logical ID of a construct's CloudFormation resource, as cdk-nag renders it in finding IDs. */
export function logicalIdOf(construct: IConstruct): string {
  const child = construct.node.defaultChild;
  if (child === undefined) {
    throw new Error(`${construct.node.path} has no CloudFormation resource`);
  }
  const stack = Stack.of(construct);
  return String(stack.resolve(stack.getLogicalId(child as CfnElement)));
}

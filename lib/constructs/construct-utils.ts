import * as fs from 'node:fs';
import * as path from 'node:path';
import { type CfnElement, Stack } from 'aws-cdk-lib';
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

/** Logical ID of a construct's CloudFormation resource, as cdk-nag renders it in finding IDs. */
export function logicalIdOf(construct: IConstruct): string {
  const child = construct.node.defaultChild;
  if (child === undefined) {
    throw new Error(`${construct.node.path} has no CloudFormation resource`);
  }
  const stack = Stack.of(construct);
  return String(stack.resolve(stack.getLogicalId(child as CfnElement)));
}

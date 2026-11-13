import * as fs from 'node:fs';
import * as path from 'node:path';
import { CONFIG_DIR } from '../../lib';

export type JsonObject = Record<string, unknown>;

export function readConfigJson(fileName: string): JsonObject {
  return JSON.parse(fs.readFileSync(path.join(CONFIG_DIR, fileName), 'utf8')) as JsonObject;
}

/** Smallest valid dev configuration: only the required fields. */
export function minimalConfig(overrides: JsonObject = {}): JsonObject {
  return {
    uniqueId: 'scitest',
    orgId: 'o-exampleorg1',
    managementAccountId: '111111111111',
    environment: 'dev',
    memberRoleTargets: ['r-ab12'],
    ...overrides,
  };
}

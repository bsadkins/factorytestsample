import { readFileSync } from 'node:fs';
import * as path from 'node:path';
import Ajv2020, { type ErrorObject, type ValidateFunction } from 'ajv/dist/2020';
import addFormats from 'ajv-formats';

export const CONFIG_DIR = path.resolve(__dirname, '..', '..', 'config');
export const CONFIG_SCHEMA_PATH = path.join(CONFIG_DIR, 'schema.json');
export const PIPELINE_SCHEMA_PATH = path.join(CONFIG_DIR, 'pipeline.schema.json');

/** Raised for any configuration problem; synthesis must stop before producing an assembly. */
export class ConfigValidationError extends Error {
  public readonly source: string;
  public readonly issues: readonly string[];

  public constructor(source: string, issues: readonly string[]) {
    super(`Invalid configuration in ${source}:\n${issues.map((issue) => `  - ${issue}`).join('\n')}`);
    this.name = 'ConfigValidationError';
    this.source = source;
    this.issues = issues;
  }
}

export function readJsonFile(filePath: string): unknown {
  let text: string;
  try {
    text = readFileSync(filePath, 'utf8');
  } catch (error) {
    throw new ConfigValidationError(filePath, [`cannot read file: ${(error as Error).message}`]);
  }
  try {
    return JSON.parse(text) as unknown;
  } catch (error) {
    throw new ConfigValidationError(filePath, [`invalid JSON: ${(error as Error).message}`]);
  }
}

// strictRequired is relaxed because the if/then rules require properties declared at the top level.
const ajv = new Ajv2020({ allErrors: true, strict: true, strictRequired: false, useDefaults: true, verbose: true });
addFormats(ajv, ['email']);

const compiled = new Map<string, ValidateFunction>();

function validatorFor(schemaPath: string): ValidateFunction {
  let validate = compiled.get(schemaPath);
  if (!validate) {
    validate = ajv.compile(readJsonFile(schemaPath) as object);
    compiled.set(schemaPath, validate);
  }
  return validate;
}

function describe(error: ErrorObject): string | undefined {
  // An "if" failure only says "must match then schema"; the specific failing
  // keyword is reported separately, so print the rule description instead.
  if (error.keyword === 'if') {
    const rule = error.parentSchema as { description?: string } | undefined;
    return rule?.description ? `rule violated: ${rule.description}` : undefined;
  }
  const location = error.instancePath === '' ? '(root)' : error.instancePath;
  if (error.keyword === 'additionalProperties') {
    const extra = (error.params as { additionalProperty: string }).additionalProperty;
    return `${location} has unsupported property "${extra}"`;
  }
  if (error.keyword === 'const') {
    return `${location} must be ${JSON.stringify((error.params as { allowedValue: unknown }).allowedValue)}`;
  }
  if (error.keyword === 'enum') {
    const allowed = (error.params as { allowedValues: unknown[] }).allowedValues;
    return `${location} must be one of ${allowed.map((value) => JSON.stringify(value)).join(', ')}`;
  }
  return `${location} ${error.message ?? 'is invalid'}`;
}

/**
 * Validates `raw` against a JSON schema and returns a deep copy with schema defaults applied.
 * The input object is never mutated.
 */
export function validateAgainstSchema(schemaPath: string, raw: unknown, source: string): unknown {
  const data: unknown = structuredClone(raw);
  const validate = validatorFor(schemaPath);
  if (!validate(data)) {
    const issues = (validate.errors ?? []).map(describe).filter((issue): issue is string => issue !== undefined);
    throw new ConfigValidationError(source, [...new Set(issues)]);
  }
  return data;
}

import * as path from 'node:path';
import {
  CONFIG_DIR,
  ConfigValidationError,
  loadEnvironmentConfig,
  loadPipelineConfig,
  parseConfig,
  parsePipelineConfig,
  validatePipelineStages,
} from '../../lib';
import { readConfigJson, type JsonObject } from './helpers';

const example = (): JsonObject => readConfigJson('pipeline.example.json');

describe('optional pipeline configuration', () => {
  test('the committed example is valid and pins the main branch of the original repository', () => {
    const config = loadPipelineConfig(path.join(CONFIG_DIR, 'pipeline.example.json'));
    expect(config.branch).toBe('main');
    expect(config.repository).toBe('bsadkins/factorytestsample');
  });

  test('defaults pipeline name, repository and branch', () => {
    const config = parsePipelineConfig({
      account: '111111111111',
      region: 'us-east-1',
      connectionArn: 'arn:aws:codeconnections:us-east-1:111111111111:connection/00000000-0000-4000-8000-000000000000',
    });
    expect(config).toMatchObject({ pipelineName: 'CaseInsights-Pipeline', repository: 'bsadkins/factorytestsample', branch: 'main' });
  });

  test('accepts legacy CodeStar connection ARNs', () => {
    const arn = 'arn:aws:codestar-connections:eu-west-1:111111111111:connection/00000000-0000-4000-8000-000000000000';
    expect(parsePipelineConfig({ ...example(), connectionArn: arn }).connectionArn).toBe(arn);
  });

  test.each<[string, JsonObject, RegExp]>([
    ['missing connection ARN', { connectionArn: undefined }, /required property 'connectionArn'/],
    ['token instead of connection ARN', { connectionArn: 'ghp_exampletoken' }, /\/connectionArn must match pattern/],
    [
      'connection in another account',
      { connectionArn: 'arn:aws:codeconnections:us-east-1:222222222222:connection/00000000-0000-4000-8000-000000000000' },
      /connectionArn is in account 222222222222 but the pipeline account is 111111111111/,
    ],
    ['non-main branch', { branch: 'develop' }, /\/branch must be "main"/],
    ['manual approval toggle', { manualApproval: false }, /unsupported property "manualApproval"/],
    ['malformed account', { account: 'abc' }, /\/account must match pattern/],
  ])('rejects %s', (_name, overrides, expected) => {
    expect(() => parsePipelineConfig({ ...example(), ...overrides })).toThrow(expected);
  });

  describe('stage validation', () => {
    const pipeline = () => parsePipelineConfig(example());
    const dev = () => loadEnvironmentConfig('dev');
    const prod = () => loadEnvironmentConfig('prod');

    test('committed dev and prod configurations form a valid pipeline', () => {
      expect(() => { validatePipelineStages(pipeline(), dev(), prod()); }).not.toThrow();
    });

    test('rejects duplicate namespaces', () => {
      const clash = parseConfig({ ...readConfigJson('prod.json'), uniqueId: dev().uniqueId });
      expect(() => { validatePipelineStages(pipeline(), dev(), clash); }).toThrow(/different uniqueId values/);
    });

    test('rejects swapped environments', () => {
      expect(() => { validatePipelineStages(pipeline(), prod(), dev()); }).toThrow(ConfigValidationError);
    });

    test('rejects stages outside the pipeline management account or organization', () => {
      const other = parseConfig({ ...readConfigJson('prod.json'), managementAccountId: '222222222222', orgId: 'o-otherorg123' });
      let issues: readonly string[] = [];
      try {
        validatePipelineStages(pipeline(), dev(), other);
      } catch (error) {
        issues = (error as ConfigValidationError).issues;
      }
      expect(issues.join('\n')).toMatch(/orgId/);
      expect(issues.join('\n')).toMatch(/prod managementAccountId 222222222222 differs from pipeline account 111111111111/);
    });
  });
});

import {
  ConfigValidationError,
  parseConfig,
  parsePipelineConfig,
  validatePipelineStages,
} from '../../lib';
import { fixtureEnvironmentConfig, type JsonObject, readFixtureJson } from './helpers';

const fixture = (): JsonObject => readFixtureJson('pipeline');
const CONNECTION_ID = '00000000-0000-4000-8000-000000000000';

describe('optional pipeline configuration', () => {
  test('the pipeline fixture is valid and pins the main branch of the original repository', () => {
    const config = parsePipelineConfig(fixture());
    expect(config.branch).toBe('main');
    expect(config.repository).toBe('bsadkins/factorytestsample');
  });

  test('defaults pipeline name, repository and branch', () => {
    const config = parsePipelineConfig({
      account: '111111111111',
      region: 'us-east-1',
      connectionArn: `arn:aws:codeconnections:us-east-1:111111111111:connection/${CONNECTION_ID}`,
    });
    expect(config).toMatchObject({ pipelineName: 'CaseInsights-Pipeline', repository: 'bsadkins/factorytestsample', branch: 'main' });
  });

  test.each<[string, JsonObject]>([
    ['CodeConnections in us-east-1', { connectionArn: `arn:aws:codeconnections:us-east-1:111111111111:connection/${CONNECTION_ID}` }],
    ['legacy CodeStar Connections in us-east-1', { connectionArn: `arn:aws:codestar-connections:us-east-1:111111111111:connection/${CONNECTION_ID}` }],
    [
      'legacy CodeStar Connections in eu-west-1',
      { region: 'eu-west-1', connectionArn: `arn:aws:codestar-connections:eu-west-1:111111111111:connection/${CONNECTION_ID}` },
    ],
    [
      'CodeConnections in the China partition',
      { region: 'cn-north-1', connectionArn: `arn:aws-cn:codeconnections:cn-north-1:111111111111:connection/${CONNECTION_ID}` },
    ],
    [
      'CodeStar Connections in AWS GovCloud (US)',
      { region: 'us-gov-west-1', connectionArn: `arn:aws-us-gov:codestar-connections:us-gov-west-1:111111111111:connection/${CONNECTION_ID}` },
    ],
  ])('accepts a connection in the pipeline account, partition and Region: %s', (_name, overrides) => {
    const raw = { ...fixture(), ...overrides };
    expect(parsePipelineConfig(raw).connectionArn).toBe(raw.connectionArn);
  });

  test.each<[string, JsonObject, RegExp]>([
    ['missing connection ARN', { connectionArn: undefined }, /required property 'connectionArn'/],
    ['token instead of connection ARN', { connectionArn: 'ghp_exampletoken' }, /\/connectionArn must match pattern/],
    [
      'connection in another account',
      { connectionArn: `arn:aws:codeconnections:us-east-1:222222222222:connection/${CONNECTION_ID}` },
      /connectionArn is in account 222222222222 but the pipeline account is 111111111111/,
    ],
    [
      'connection in another Region',
      { connectionArn: `arn:aws:codeconnections:eu-west-1:111111111111:connection/${CONNECTION_ID}` },
      /\/connectionArn is in Region eu-west-1 but the pipeline Region is us-east-1/,
    ],
    [
      'legacy connection in another Region',
      { connectionArn: `arn:aws:codestar-connections:us-west-2:111111111111:connection/${CONNECTION_ID}` },
      /\/connectionArn is in Region us-west-2 but the pipeline Region is us-east-1/,
    ],
    [
      'partition that does not contain the pipeline Region',
      { connectionArn: `arn:aws-cn:codeconnections:us-east-1:111111111111:connection/${CONNECTION_ID}` },
      /\/connectionArn is in partition aws-cn but the pipeline Region us-east-1 is in partition aws/,
    ],
    [
      'commercial partition for a China Region',
      { region: 'cn-north-1', connectionArn: `arn:aws:codeconnections:cn-north-1:111111111111:connection/${CONNECTION_ID}` },
      /\/connectionArn is in partition aws but the pipeline Region cn-north-1 is in partition aws-cn/,
    ],
    ['non-main branch', { branch: 'develop' }, /\/branch must be "main"/],
    ['another owner of the repository name', { repository: 'example-org/factorytestsample' }, /\/repository must be "bsadkins\/factorytestsample"/],
    ['another repository of the same owner', { repository: 'bsadkins/another-repository' }, /\/repository must be "bsadkins\/factorytestsample"/],
    ['manual approval toggle', { manualApproval: false }, /unsupported property "manualApproval"/],
    ['malformed account', { account: 'abc' }, /\/account must match pattern/],
  ])('rejects %s', (_name, overrides, expected) => {
    expect(() => parsePipelineConfig({ ...fixture(), ...overrides })).toThrow(expected);
  });

  test('reports every locality problem of a connection ARN at once', () => {
    let issues: readonly string[] = [];
    try {
      parsePipelineConfig({ ...fixture(), connectionArn: `arn:aws-cn:codeconnections:cn-north-1:222222222222:connection/${CONNECTION_ID}` });
    } catch (error) {
      issues = (error as ConfigValidationError).issues;
    }
    expect(issues).toEqual([
      '/connectionArn is in account 222222222222 but the pipeline account is 111111111111',
      '/connectionArn is in Region cn-north-1 but the pipeline Region is us-east-1; CodePipeline source actions cannot use a connection in another Region',
      '/connectionArn is in partition aws-cn but the pipeline Region us-east-1 is in partition aws',
    ]);
  });

  describe('stage validation', () => {
    const pipeline = () => parsePipelineConfig(fixture());
    const dev = () => fixtureEnvironmentConfig('dev');
    const prod = () => fixtureEnvironmentConfig('prod');

    test('the dev and prod fixtures form a valid pipeline with the pipeline fixture', () => {
      expect(() => { validatePipelineStages(pipeline(), dev(), prod()); }).not.toThrow();
    });

    test('rejects duplicate namespaces', () => {
      const clash = parseConfig({ ...readFixtureJson('prod'), uniqueId: dev().uniqueId });
      expect(() => { validatePipelineStages(pipeline(), dev(), clash); }).toThrow(/different uniqueId values/);
    });

    test('rejects dev and prod stages with different shared API Gateway logging settings', () => {
      const conflict = parseConfig({ ...readFixtureJson('prod'), apiGatewayLogging: { mode: 'managed', sharedId: 'other' } });
      expect(() => { validatePipelineStages(pipeline(), dev(), conflict); }).toThrow(/same apiGatewayLogging settings/);
    });

    test('rejects swapped environments', () => {
      expect(() => { validatePipelineStages(pipeline(), prod(), dev()); }).toThrow(ConfigValidationError);
    });

    test('rejects stages outside the pipeline management account or organization', () => {
      const other = parseConfig({ ...readFixtureJson('prod'), managementAccountId: '222222222222', orgId: 'o-otherorg123' });
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

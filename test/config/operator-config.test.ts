import * as fs from 'node:fs';
import * as path from 'node:path';
import {
  addCaseInsightsApp,
  CONFIG_DIR,
  ENVIRONMENT_NAMES,
  loadConfigFile,
  loadEnvironmentConfig,
  loadPipelineConfig,
  PIPELINE_CONFIG_FILE,
} from '../../lib';
import { synthError, testApp } from '../helpers/cdk';

/**
 * The deployment configuration operators edit in config/. These checks hold for any valid operator
 * configuration; fixed values and templates are asserted against test/fixtures/config instead. The
 * standalone dev and prod synthesis with cdk-nag is `npm run synth`.
 */
describe('operator configuration in config/', () => {
  test.each(ENVIRONMENT_NAMES)('config/%s.json is valid for its environment', (env) => {
    expect(loadEnvironmentConfig(env).environment).toBe(env);
  });

  test('config/example.json is valid', () => {
    expect(() => loadConfigFile(path.join(CONFIG_DIR, 'example.json'))).not.toThrow();
  });

  test('config/pipeline.example.json is valid on its own', () => {
    expect(loadPipelineConfig(path.join(CONFIG_DIR, 'pipeline.example.json')).branch).toBe('main');
  });

  const pipelineFile = path.join(CONFIG_DIR, PIPELINE_CONFIG_FILE);
  (fs.existsSync(pipelineFile) ? test : test.skip)(
    'config/pipeline.json, when present, forms a valid pipeline with config/dev.json and config/prod.json and synthesises with cdk-nag',
    () => {
      const app = testApp({ pipeline: 'true' });
      expect(addCaseInsightsApp(app).kind).toBe('pipeline');
      expect(synthError(app)).toBeUndefined();
    },
  );
});

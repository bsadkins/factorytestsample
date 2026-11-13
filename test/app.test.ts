import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { App, aws_s3 as s3 } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { addCaseInsightsStacks, CaseInsightsCoreStack, CONFIG_DIR, ConfigValidationError } from '../lib';
import { normaliseAssetHashes } from './helpers/assets';

function tempOutdir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'case-insights-app-'));
}

describe('Support Case Insights app', () => {
  test('dev configuration adds the namespaced core stack in the management account', () => {
    const app = new App({ outdir: tempOutdir() });
    const { config, core } = addCaseInsightsStacks(app, { env: 'dev' });

    expect(core).toBeInstanceOf(CaseInsightsCoreStack);
    expect(core.stackName).toBe(`CaseInsightsCoreStack-${config.uniqueId}`);
    expect(core.account).toBe(config.managementAccountId);
    expect(core.region).toBe(config.region);
    expect(core.config).toBe(config);
    expect(app.node.children.filter((child) => child instanceof CaseInsightsCoreStack)).toHaveLength(1);
  });

  test.each(['dev', 'prod'] as const)('%s configuration synthesises with validation plugins passing', (env) => {
    const app = new App({ outdir: tempOutdir() });
    addCaseInsightsStacks(app, { env });
    const assembly = app.synth();
    const report = JSON.parse(fs.readFileSync(path.join(assembly.directory, 'validation-report.json'), 'utf8')) as {
      pluginReports: { pluginName: string; conclusion: string }[];
    };
    expect(report.pluginReports.every((plugin) => plugin.conclusion === 'success')).toBe(true);
  });

  test('core stack template snapshot', () => {
    const app = new App({ outdir: tempOutdir() });
    const { core } = addCaseInsightsStacks(app, { env: 'dev' });
    expect(normaliseAssetHashes(Template.fromStack(core).toJSON())).toMatchSnapshot();
  });

  test('cdk-nag findings fail synthesis', () => {
    const app = new App({ outdir: tempOutdir() });
    const { core } = addCaseInsightsStacks(app, { env: 'dev' });
    new s3.Bucket(core, 'InsecureBucket', { enforceSSL: false });
    let error: unknown;
    try {
      app.synth();
    } catch (caught) {
      error = caught;
    }
    expect(String(error)).toMatch(/AwsSolutions-S1'/);
    expect(String(error)).toMatch(/AwsSolutions-S10'/);
  });

  test('invalid configuration stops before any stack is created', () => {
    const configDir = fs.mkdtempSync(path.join(os.tmpdir(), 'case-insights-config-'));
    const prod = JSON.parse(fs.readFileSync(path.join(CONFIG_DIR, 'prod.json'), 'utf8')) as Record<string, unknown>;
    delete prod.alarmEmail;
    fs.writeFileSync(path.join(configDir, 'prod.json'), JSON.stringify(prod));

    const app = new App({ outdir: tempOutdir() });
    expect(() => addCaseInsightsStacks(app, { env: 'prod', configDir })).toThrow(ConfigValidationError);
    expect(app.node.children).toHaveLength(0);
  });
});

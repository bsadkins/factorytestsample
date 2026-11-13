import { execFileSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { App, Stack } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import {
  addCaseInsightsStacks,
  bundlingPython,
  defaultPythonAssetPaths,
  fingerprintInputs,
  LAMBDA_TEMPLATES_DIR,
  listAssetFiles,
  type PythonAssetPaths,
  REPO_ROOT,
  SharedLayers,
  TEMPLATES_DIR_ENV,
} from '../../lib';

const ENV = { account: '111111111111', region: 'us-east-1' };
const createdDirs: string[] = [];

function tempDir(prefix: string): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  createdDirs.push(dir);
  return dir;
}

afterAll(() => {
  for (const dir of createdDirs) {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

function copyOf(source: string): string {
  const target = path.join(tempDir('case-insights-copy-'), path.basename(source));
  fs.cpSync(source, target, { recursive: true });
  return target;
}

interface LayerSynth {
  readonly app: App;
  readonly stack: Stack;
  readonly layers: SharedLayers;
  readonly keys: { common: string; templates: string };
  readonly assetDirs: { common: string; templates: string };
}

function synthLayers(assetPaths: Partial<PythonAssetPaths> = {}, bundle = false): LayerSynth {
  const outdir = tempDir('case-insights-layers-');
  const app = new App({ outdir, context: bundle ? {} : { 'aws:cdk:bundling-stacks': [] } });
  const stack = new Stack(app, 'LayerTest', { env: ENV });
  const layers = new SharedLayers(stack, 'Layers', { uniqueId: 'test', assetPaths });
  const resources = Template.fromStack(stack).findResources('AWS::Lambda::LayerVersion') as Record<
    string,
    { Properties: { LayerName: string; Content: { S3Key: string } } }
  >;
  const byName = (name: string): { key: string; dir: string } => {
    const match = Object.values(resources).find((resource) => resource.Properties.LayerName === name);
    if (!match) {
      throw new Error(`layer ${name} missing`);
    }
    const key = match.Properties.Content.S3Key;
    // Bundled assets are staged in the cloud assembly as asset.<hash>.
    return { key, dir: path.join(outdir, `asset.${key.replace(/\.zip$/, '')}`) };
  };
  const common = byName('case-insights-common-test');
  const templates = byName('case-insights-templates-test');
  return {
    app,
    stack,
    layers,
    keys: { common: common.key, templates: templates.key },
    assetDirs: { common: common.dir, templates: templates.dir },
  };
}

describe('content fingerprints', () => {
  test('are independent of checkout location and ignore bytecode caches', () => {
    const { templatesDir, commonDir } = defaultPythonAssetPaths();
    const first = copyOf(templatesDir);
    const second = copyOf(templatesDir);
    expect(fingerprintInputs([{ label: 't', path: first }])).toBe(fingerprintInputs([{ label: 't', path: second }]));

    const common = copyOf(commonDir);
    const before = fingerprintInputs([{ label: 'c', path: common }]);
    fs.mkdirSync(path.join(common, '__pycache__'), { recursive: true });
    fs.writeFileSync(path.join(common, '__pycache__', 'x.cpython-312.pyc'), 'bytecode');
    fs.writeFileSync(path.join(common, 'stray.pyc'), 'bytecode');
    expect(fingerprintInputs([{ label: 'c', path: common }])).toBe(before);
    expect(listAssetFiles(common)).not.toContain('stray.pyc');
  });

  test('change with file content, file name and labels', () => {
    const templates = copyOf(defaultPythonAssetPaths().templatesDir);
    const baseline = fingerprintInputs([{ label: 't', path: templates }]);
    expect(fingerprintInputs([{ label: 'other', path: templates }])).not.toBe(baseline);
    fs.renameSync(path.join(templates, 'rca-template.txt'), path.join(templates, 'rca-template-renamed.txt'));
    expect(fingerprintInputs([{ label: 't', path: templates }])).not.toBe(baseline);
  });

  test('fail clearly when an input is missing', () => {
    expect(() => fingerprintInputs([{ label: 'missing', path: '/nonexistent/input' }])).toThrow(/missing not found/);
  });
});

describe('layer asset identity', () => {
  test('a template-only edit changes only the templates layer, and reverting restores it', () => {
    const templatesDir = copyOf(defaultPythonAssetPaths().templatesDir);
    const baseline = synthLayers({ templatesDir }).keys;
    expect(baseline).toEqual(synthLayers().keys);

    const file = path.join(templatesDir, 'summary-template.txt');
    const original = fs.readFileSync(file, 'utf8');
    fs.writeFileSync(file, `${original}\nAdditional customer guidance.\n`);
    const edited = synthLayers({ templatesDir }).keys;
    expect(edited.templates).not.toBe(baseline.templates);
    expect(edited.common).toBe(baseline.common);

    fs.writeFileSync(file, original);
    expect(synthLayers({ templatesDir }).keys).toEqual(baseline);
  });

  test('shared source and lock edits change only the shared layer', () => {
    const baseline = synthLayers().keys;
    const commonDir = copyOf(defaultPythonAssetPaths().commonDir);
    fs.appendFileSync(path.join(commonDir, 'taxonomy.py'), '\n# edited\n');
    const sourceEdit = synthLayers({ commonDir }).keys;
    expect(sourceEdit.common).not.toBe(baseline.common);
    expect(sourceEdit.templates).toBe(baseline.templates);

    const requirementsFile = path.join(tempDir('case-insights-lock-'), 'requirements.txt');
    fs.writeFileSync(requirementsFile, `${fs.readFileSync(defaultPythonAssetPaths().requirementsFile, 'utf8')}# edited\n`);
    const lockEdit = synthLayers({ requirementsFile }).keys;
    expect(lockEdit.common).not.toBe(baseline.common);
    expect(lockEdit.templates).toBe(baseline.templates);
  });

  test('layers target Python 3.12 on x86_64 and expose the templates directory', () => {
    const { stack, layers } = synthLayers();
    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::Lambda::LayerVersion', 2);
    template.allResourcesProperties('AWS::Lambda::LayerVersion', {
      CompatibleRuntimes: ['python3.12'],
      CompatibleArchitectures: ['x86_64'],
    });
    expect(layers.templateEnvironment).toEqual({ [TEMPLATES_DIR_ENV]: LAMBDA_TEMPLATES_DIR });
    expect(LAMBDA_TEMPLATES_DIR).toBe('/opt/templates');
  });
});

describe('local wheel-only bundling', () => {
  test('produces /opt/python/common and /opt/templates layouts that import and render', () => {
    const { assetDirs } = synthLayers({}, true);
    const pythonDir = path.join(assetDirs.common, 'python');
    expect(fs.existsSync(path.join(pythonDir, 'common', '__init__.py'))).toBe(true);
    expect(fs.existsSync(path.join(pythonDir, 'bin'))).toBe(false);
    expect(listAssetFiles(assetDirs.common).length).toBeGreaterThan(0);
    const all = execFileSync('find', [assetDirs.common, '-name', '__pycache__'], { encoding: 'utf8' });
    expect(all.trim()).toBe('');

    // Every pinned runtime package is installed from its wheel.
    const lock = fs.readFileSync(defaultPythonAssetPaths().requirementsFile, 'utf8');
    const pinned = [...lock.matchAll(/^([A-Za-z0-9_.-]+)==([^\s\\]+)/gm)].map((m): [string, string] => [m[1] ?? '', m[2] ?? '']);
    expect(pinned.length).toBeGreaterThan(5);
    const installed = fs.readdirSync(pythonDir).filter((name) => name.endsWith('.dist-info'));
    for (const [name, version] of pinned) {
      const normalised = name.toLowerCase().replace(/[-_.]+/g, '_');
      expect(installed.map((d) => d.toLowerCase())).toContain(`${normalised}-${version}.dist-info`);
    }

    const templateFiles = fs.readdirSync(path.join(assetDirs.templates, 'templates')).sort();
    expect(templateFiles).toEqual(listAssetFiles(defaultPythonAssetPaths().templatesDir).sort());

    // Mount both assets the way Lambda does (layer contents under one /opt root).
    const opt = tempDir('case-insights-opt-');
    fs.symlinkSync(pythonDir, path.join(opt, 'python'));
    fs.symlinkSync(path.join(assetDirs.templates, 'templates'), path.join(opt, 'templates'));
    const script = [
      'import json, sys',
      `sys.path.insert(0, ${JSON.stringify(path.join(opt, 'python'))})`,
      'import boto3, aws_lambda_powertools, common',
      'from common.taxonomy import RCA, LIFECYCLE',
      'from common.templates import render_summary_prompt, render_classification_prompt, prompt_version',
      'summary = render_summary_prompt("Synthetic conversation about an S3 403 error.")',
      'rca = render_classification_prompt(RCA, "Synthetic summary.")',
      'lifecycle = render_classification_prompt(LIFECYCLE, "Synthetic summary.")',
      'print(json.dumps({"common": common.__file__, "boto3": boto3.__file__,',
      '  "powertools": aws_lambda_powertools.__file__, "summary": summary, "rca": rca,',
      '  "lifecycle": lifecycle, "version": prompt_version()}))',
    ].join('\n');
    const output = execFileSync(bundlingPython(), ['-I', '-c', script], {
      encoding: 'utf8',
      env: { ...process.env, [TEMPLATES_DIR_ENV]: path.join(opt, 'templates') },
    });
    const result = JSON.parse(output) as Record<string, string>;
    const realOpt = fs.realpathSync(pythonDir);
    for (const module of ['common', 'boto3', 'powertools']) {
      expect(fs.realpathSync(String(result[module]))).toContain(realOpt);
    }
    expect(result.summary).toContain('Synthetic conversation about an S3 403 error.');
    expect(result.rca).toContain('- Throttling: ');
    expect(result.lifecycle).toContain('- Fault Injection and GameDay: ');
    expect(result.version).toMatch(/^pv-[0-9a-f]{16}$/);
  });

  test('a missing interpreter fails synthesis instead of falling back to Docker', () => {
    const previous = process.env.CASE_INSIGHTS_PYTHON;
    process.env.CASE_INSIGHTS_PYTHON = '/nonexistent/python3.12';
    try {
      expect(() => synthLayers({}, true)).toThrow(/wheel-only Lambda layer bundling not found/);
    } finally {
      if (previous === undefined) {
        delete process.env.CASE_INSIGHTS_PYTHON;
      } else {
        process.env.CASE_INSIGHTS_PYTHON = previous;
      }
    }
  });

  test('a failing bundler fails synthesis visibly', () => {
    const bundlerScript = path.join(tempDir('case-insights-bundler-'), 'fail.py');
    fs.writeFileSync(bundlerScript, 'import sys\nprint("no matching wheel", file=sys.stderr)\nsys.exit(3)\n');
    expect(() => synthLayers({ bundlerScript }, true)).toThrow(/no Docker fallback[\s\S]*no matching wheel/);
  });
});

describe('core stack', () => {
  test('exposes the shared layers', () => {
    const app = new App({ outdir: tempDir('case-insights-core-'), context: { 'aws:cdk:bundling-stacks': [] } });
    const { core, config } = addCaseInsightsStacks(app, { env: 'dev' });
    expect(core.layers).toBeInstanceOf(SharedLayers);
    const template = Template.fromStack(core);
    template.hasResourceProperties('AWS::Lambda::LayerVersion', { LayerName: `case-insights-common-${config.uniqueId}` });
    template.hasResourceProperties('AWS::Lambda::LayerVersion', { LayerName: `case-insights-templates-${config.uniqueId}` });
    expect(REPO_ROOT).toBe(path.resolve(__dirname, '..', '..'));
  });
});

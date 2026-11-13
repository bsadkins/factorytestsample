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
import { testApp } from '../helpers/cdk';

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

interface FunctionJson {
  Handler: string;
  Code: { S3Key: string };
  Layers?: { Ref: string }[];
  Environment?: { Variables?: Record<string, unknown> };
}

interface AttachedAssets {
  readonly template: Template;
  readonly assemblyDir: string;
  readonly functions: Record<string, FunctionJson>;
  readonly layerDir: (layerId: string) => string;
  readonly codeDir: (fn: FunctionJson) => string;
}

/** The dev app synthesised as the CLI does, with bundled assets staged in its cloud assembly. */
let attached: AttachedAssets | undefined;
function attachedAssets(): AttachedAssets {
  if (attached === undefined) {
    const app = testApp();
    const { core } = addCaseInsightsStacks(app, { env: 'dev' });
    const assemblyDir = app.synth().directory;
    const template = Template.fromStack(core);
    const functions = Object.fromEntries(
      Object.entries(template.findResources('AWS::Lambda::Function'))
        .map(([id, resource]): [string, FunctionJson] => [id, resource.Properties as FunctionJson])
        .filter(([, fn]) => fn.Environment?.Variables?.POWERTOOLS_SERVICE_NAME !== undefined),
    );
    const layers = template.findResources('AWS::Lambda::LayerVersion') as Record<string, { Properties: { Content: { S3Key: string } } }>;
    const stagedAsset = (key: string): string => path.join(assemblyDir, `asset.${key.replace(/\.zip$/, '')}`);
    attached = {
      template,
      assemblyDir,
      functions,
      layerDir: (layerId) => {
        const layer = layers[layerId];
        if (layer === undefined) {
          throw new Error(`layer ${layerId} not found`);
        }
        return stagedAsset(layer.Properties.Content.S3Key);
      },
      codeDir: (fn) => stagedAsset(fn.Code.S3Key),
    };
  }
  return attached;
}

function functionNamed(assets: AttachedAssets, serviceName: string): FunctionJson {
  const fn = Object.values(assets.functions).find((candidate) => candidate.Environment?.Variables?.POWERTOOLS_SERVICE_NAME === serviceName);
  if (fn === undefined) {
    throw new Error(`function ${serviceName} not found`);
  }
  return fn;
}

/** Mounts a function's attached layers under one directory, as Lambda extracts them to /opt. */
function mountLayers(assets: AttachedAssets, fn: FunctionJson): string {
  const opt = tempDir('case-insights-attached-opt-');
  for (const { Ref } of fn.Layers ?? []) {
    const layerDir = assets.layerDir(Ref);
    for (const entry of fs.readdirSync(layerDir)) {
      if (fs.existsSync(path.join(opt, entry))) {
        throw new Error(`attached layers both provide /opt/${entry}`);
      }
      fs.symlinkSync(path.join(layerDir, entry), path.join(opt, entry));
    }
  }
  return opt;
}

/** Python source files in a directory tree. */
function pythonSources(dir: string): string[] {
  return listAssetFiles(dir)
    .filter((file) => file.endsWith('.py'))
    .map((file) => fs.readFileSync(path.join(dir, file), 'utf8'));
}

/** Names of the `common` modules a source imports. */
function commonImports(source: string): string[] {
  const names = new Set<string>();
  for (const match of source.matchAll(/^\s*(?:from\s+common\.(\w+)\s+import|import\s+common\.(\w+)|from\s+common\s+import\s+\(?([\w\s,]+)\)?)/gm)) {
    for (const name of (match[1] ?? match[2] ?? match[3] ?? '').split(',')) {
      if (name.trim() !== '') {
        names.add(name.trim().split(/\s+/)[0] ?? '');
      }
    }
  }
  return [...names];
}

describe('attached function assets', () => {
  test('every function whose packaged code reads prompt templates has the templates layer and its directory', () => {
    const assets = attachedAssets();
    // The import graph comes from the shared package as it is staged in the common layer.
    const commonFn = functionNamed(assets, 'update-case-metadata');
    const commonPackage = (fn: FunctionJson): string | undefined =>
      (fn.Layers ?? []).map(({ Ref }) => path.join(assets.layerDir(Ref), 'python', 'common')).find((dir) => fs.existsSync(dir));
    const commonDir = commonPackage(commonFn);
    if (commonDir === undefined) {
      throw new Error('common layer not attached');
    }
    const graph = new Map(
      fs
        .readdirSync(commonDir)
        .filter((file) => file.endsWith('.py'))
        .map((file) => [file.replace(/\.py$/, ''), commonImports(fs.readFileSync(path.join(commonDir, file), 'utf8'))]),
    );
    const readsTemplates = new Set(['templates']);
    for (let grew = true; grew; ) {
      grew = false;
      for (const [module, imports] of graph) {
        if (!readsTemplates.has(module) && imports.some((name) => readsTemplates.has(name))) {
          readsTemplates.add(module);
          grew = true;
        }
      }
    }

    const templateUsers = Object.values(assets.functions)
      .filter((fn) => pythonSources(assets.codeDir(fn)).some((source) => commonImports(source).some((name) => readsTemplates.has(name))))
      .map((fn) => String(fn.Environment?.Variables?.POWERTOOLS_SERVICE_NAME));
    expect(templateUsers.sort()).toEqual(['case-analysis-stages', 'update-case-metadata']);

    const templateNames = listAssetFiles(defaultPythonAssetPaths().templatesDir).sort();
    for (const service of templateUsers) {
      const fn = functionNamed(assets, service);
      expect(fn.Environment?.Variables?.[TEMPLATES_DIR_ENV]).toBe(LAMBDA_TEMPLATES_DIR);
      const opt = mountLayers(assets, fn);
      expect(fs.readdirSync(path.join(opt, 'python', 'common')).length).toBeGreaterThan(0);
      expect(fs.readdirSync(path.join(opt, 'templates')).sort()).toEqual(templateNames);
    }
  });

  test('UpdateCaseMetadata commits a record with the prompt version from its own attached layers and environment', () => {
    const assets = attachedAssets();
    const fn = functionNamed(assets, 'update-case-metadata');
    expect(fn.Handler).toBe('app.handler');
    const opt = mountLayers(assets, fn);
    const codeDir = assets.codeDir(fn);

    // Only the function's configured environment, with /opt paths pointing at the mounted layers.
    // Unresolved values (bucket references) become synthetic bucket names.
    const environment: Record<string, string> = {};
    for (const [name, value] of Object.entries(fn.Environment?.Variables ?? {})) {
      if (typeof value !== 'string') {
        environment[name] = `smoke-${name.toLowerCase().replace(/_/g, '-')}`;
      } else {
        environment[name] = value === '/opt' || value.startsWith('/opt/') ? path.join(opt, value.slice('/opt'.length)) : value;
      }
    }
    // Without the variable, common.templates reads Lambda's /opt/templates.
    environment[TEMPLATES_DIR_ENV] ??= path.join(opt, LAMBDA_TEMPLATES_DIR.slice('/opt'.length));

    const summaries = JSON.parse(fs.readFileSync(path.join(REPO_ROOT, 'test', 'fixtures', 'prompts', 'summaries.json'), 'utf8')) as {
      cases: { id: string; text: string }[];
    };
    const input = {
      summary: summaries.cases.find((c) => c.id === 'valid-five-sentences')?.text,
      raw: {
        schemaVersion: 1,
        accountId: '111122223333',
        runId: '20261113T000000Z',
        caseId: 'case-111122223333-2026-abcdef01',
        displayId: '170000000001',
        subject: 'Synthetic: load balancer returns 503',
        status: 'resolved',
        serviceCode: 'elastic-load-balancing',
        categoryCode: 'performance',
        severityCode: 'high',
        submittedBy: 'synthetic-requester@example.com',
        timeCreated: '2026-06-01T10:00:00.000Z',
        language: 'en',
        Case_Retrieval_Date: '2026-11-13T00:05:12Z',
      },
    };
    const script = [
      'import json, os, sys',
      `sys.path[:0] = [${JSON.stringify(codeDir)}, ${JSON.stringify(path.join(opt, 'python'))}]`,
      `data = json.loads(${JSON.stringify(JSON.stringify(input))})`,
      'from moto import mock_aws',
      'with mock_aws():',
      '    import boto3, common',
      '    import app',
      '    from common.analysis import STAGE_LIFECYCLE, STAGE_RCA, STAGE_SUMMARY, text_digest',
      '    from common.case_refs import SummaryRef',
      '    from common.checkpoints import Checkpoint',
      '    raw_bucket, processed_bucket = os.environ["RAW_BUCKET"], os.environ["PROCESSED_BUCKET"]',
      '    s3 = boto3.client("s3")',
      '    for bucket in (raw_bucket, processed_bucket):',
      '        s3.create_bucket(Bucket=bucket)',
      '    raw = data["raw"]',
      '    ref = SummaryRef(account_id=raw["accountId"], case_id=raw["caseId"], display_id=raw["displayId"],',
      '        run_id=raw["runId"], communications_version_id="synthetic-version", communication_count=3)',
      '    s3.put_object(Bucket=raw_bucket, Key=ref.raw_key, Body=json.dumps(raw).encode())',
      '    version = app.prompt_version()',
      '    digest = text_digest(data["summary"])',
      '    outputs = {',
      '        STAGE_SUMMARY: ("synthetic-conversation-digest", {"Case_Summary": data["summary"]}),',
      '        STAGE_RCA: (digest, {"RCA_Category": "Customer Release", "RCA_Reason": "The release changed the port.", "fallback": False}),',
      '        STAGE_LIFECYCLE: (digest, {"Lifecycle_Category": "CICD", "Lifecycle_Reason": "A pipeline check would have caught it.", "fallback": False}),',
      '    }',
      '    for stage, (input_digest, output) in outputs.items():',
      '        checkpoint = Checkpoint(stage=stage, account_id=ref.account_id, case_id=ref.case_id, display_id=ref.display_id,',
      '            model_id=os.environ["BEDROCK_MODEL_ID"], prompt_version=version, input_digest=input_digest,',
      '            completed_at="2026-11-13T01:02:03Z", output=output)',
      '        s3.put_object(Bucket=raw_bucket, Key=checkpoint.key, Body=json.dumps(checkpoint.to_document()).encode())',
      '    class Context:',
      '        function_name = "smoke"; memory_limit_in_mb = 512; aws_request_id = "smoke-1"',
      '        invoked_function_arn = "arn:aws:lambda:us-east-1:111122223333:function:smoke"',
      '    result = app.handler({"case": ref.to_message()}, Context())',
      '    record = json.loads(s3.get_object(Bucket=processed_bucket, Key=ref.raw_key)["Body"].read())',
      '    print("SMOKE-RESULT " + json.dumps({"result": result, "promptVersion": record["Prompt_Version"],',
      '        "common": common.__file__, "app": app.__file__}))',
    ].join('\n');
    const output = execFileSync(bundlingPython(), ['-I', '-c', script], {
      encoding: 'utf8',
      // A fresh environment: nothing from the test process (such as a source-tree templates directory) leaks in.
      env: {
        PATH: process.env.PATH ?? '',
        AWS_ACCESS_KEY_ID: 'testing',
        AWS_SECRET_ACCESS_KEY: 'testing',
        AWS_DEFAULT_REGION: 'us-east-1',
        POWERTOOLS_TRACE_DISABLED: 'true',
        ...environment,
      },
    });
    const line = output.split('\n').find((candidate) => candidate.startsWith('SMOKE-RESULT '));
    if (line === undefined) {
      throw new Error(`no smoke result in output:\n${output}`);
    }
    const result = JSON.parse(line.slice('SMOKE-RESULT '.length)) as { result: { status: string }; promptVersion: string; common: string; app: string };
    expect(result.result.status).toBe('committed');
    expect(fs.realpathSync(result.common)).toContain(fs.realpathSync(path.join(opt, 'python')));
    expect(fs.realpathSync(result.app)).toBe(fs.realpathSync(path.join(codeDir, 'app.py')));

    // Independent expectation: the version of the repository templates, computed separately.
    const expected = execFileSync(
      bundlingPython(),
      [
        '-I',
        '-c',
        `import sys; sys.path.insert(0, ${JSON.stringify(path.join(REPO_ROOT, 'src', 'lambda'))}); from common.templates import prompt_version; print(prompt_version(directory=${JSON.stringify(defaultPythonAssetPaths().templatesDir)}))`,
      ],
      { encoding: 'utf8', env: { PATH: process.env.PATH ?? '' } },
    ).trim();
    expect(expected).toMatch(/^pv-[0-9a-f]{16}$/);
    expect(result.promptVersion).toBe(expected);
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

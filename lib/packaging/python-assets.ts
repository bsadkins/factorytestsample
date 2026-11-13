import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { AssetHashType, BundlingOutput, DockerImage, type ILocalBundling } from 'aws-cdk-lib';
import { Code } from 'aws-cdk-lib/aws-lambda';

/** Repository root (lib/packaging/../..). */
export const REPO_ROOT = path.resolve(__dirname, '..', '..');

/** Directory where the templates layer is mounted in the Lambda execution environment. */
export const LAMBDA_TEMPLATES_DIR = '/opt/templates';

/** Environment variable read by common.templates; defaults to /opt/templates when unset. */
export const TEMPLATES_DIR_ENV = 'CASE_INSIGHTS_TEMPLATES_DIR';

/** pip target-platform arguments; must match TARGET_PLATFORM_ARGS in the bundler script. */
export const LAMBDA_WHEEL_PLATFORM = [
  '--platform',
  'manylinux_2_28_x86_64',
  '--implementation',
  'cp',
  '--python-version',
  '3.12',
  '--abi',
  'cp312',
] as const;

/** Source inputs for the shared and templates layers. Override only to build from another tree. */
export interface PythonAssetPaths {
  /** Shared package copied to /opt/python/common. */
  readonly commonDir: string;
  /** Fully hashed runtime lock installed into /opt/python. */
  readonly requirementsFile: string;
  /** Prompt templates copied to /opt/templates. */
  readonly templatesDir: string;
  /** Wheel-only layer bundler script. */
  readonly bundlerScript: string;
  /** Repository-local pip download cache. */
  readonly pipCacheDir: string;
}

export function defaultPythonAssetPaths(): PythonAssetPaths {
  return {
    commonDir: path.join(REPO_ROOT, 'src', 'lambda', 'common'),
    requirementsFile: path.join(REPO_ROOT, 'requirements.txt'),
    templatesDir: path.join(REPO_ROOT, 'src', 'templates'),
    bundlerScript: path.join(REPO_ROOT, 'scripts', 'build', 'bundle_python_layer.py'),
    pipCacheDir: path.join(REPO_ROOT, '.cache', 'pip'),
  };
}

const IGNORED_NAMES = new Set(['__pycache__', '.DS_Store', '.mypy_cache', '.pytest_cache', '.ruff_cache']);
const IGNORED_SUFFIXES = ['.pyc', '.pyo'];

function isIgnored(name: string): boolean {
  return IGNORED_NAMES.has(name) || IGNORED_SUFFIXES.some((suffix) => name.endsWith(suffix));
}

/** Files under `root` (or `root` itself), as sorted POSIX paths relative to `root`, skipping caches. */
export function listAssetFiles(root: string): string[] {
  const stat = fs.statSync(root);
  if (stat.isFile()) {
    return [path.basename(root)];
  }
  const files: string[] = [];
  const walk = (dir: string, prefix: string): void => {
    const entries = fs.readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name, 'en'));
    for (const entry of entries) {
      if (isIgnored(entry.name)) {
        continue;
      }
      const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.isDirectory()) {
        walk(path.join(dir, entry.name), relative);
      } else if (entry.isFile()) {
        files.push(relative);
      } else {
        throw new Error(`Unsupported asset entry (not a regular file or directory): ${path.join(dir, entry.name)}`);
      }
    }
  };
  walk(root, '');
  return files;
}

export interface FingerprintInput {
  /** Stable label so the hash does not depend on where the checkout lives. */
  readonly label: string;
  readonly path: string;
}

/**
 * SHA-256 over the labelled relative path and bytes of every input file, plus `extra` strings.
 * Independent of absolute paths, timestamps and cache directories.
 */
export function fingerprintInputs(inputs: readonly FingerprintInput[], extra: readonly string[] = []): string {
  const hash = createHash('sha256');
  for (const value of extra) {
    hash.update(`extra\0${value}\0`);
  }
  for (const input of inputs) {
    if (!fs.existsSync(input.path)) {
      throw new Error(`Asset input ${input.label} not found: ${input.path}`);
    }
    const isFile = fs.statSync(input.path).isFile();
    for (const relative of listAssetFiles(input.path)) {
      const absolute = isFile ? input.path : path.join(input.path, ...relative.split('/'));
      hash.update(`file\0${input.label}/${relative}\0`);
      hash.update(fs.readFileSync(absolute));
      hash.update('\0');
    }
  }
  return hash.digest('hex');
}

/**
 * Interpreter used to run the wheel-only bundler: CASE_INSIGHTS_PYTHON, else the repository .venv.
 */
export function bundlingPython(): string {
  const configured = process.env.CASE_INSIGHTS_PYTHON;
  const candidate = configured && configured.length > 0 ? configured : path.join(REPO_ROOT, '.venv', 'bin', 'python');
  if (!fs.existsSync(candidate)) {
    throw new Error(
      `Python interpreter for wheel-only Lambda layer bundling not found at ${candidate}. ` +
        'Create the repository virtual environment (see README "Set up") or set CASE_INSIGHTS_PYTHON.',
    );
  }
  return candidate;
}

// Bundling must never fall back to Docker. tryBundle either succeeds or throws, so CDK never
// reaches this image; it exists only because the BundlingOptions type requires one.
const NO_DOCKER_IMAGE = DockerImage.fromRegistry('docker-bundling-disabled.invalid/no-image');

function localOnlyBundling(bundle: (outputDir: string) => void): ILocalBundling {
  return {
    tryBundle(outputDir: string): boolean {
      bundle(outputDir);
      return true;
    },
  };
}

function runBundler(paths: PythonAssetPaths, outputDir: string): void {
  try {
    execFileSync(
      bundlingPython(),
      [
        paths.bundlerScript,
        '--output',
        outputDir,
        '--requirements',
        paths.requirementsFile,
        '--source',
        paths.commonDir,
        '--cache-dir',
        paths.pipCacheDir,
      ],
      { stdio: ['ignore', 'pipe', 'pipe'], encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 },
    );
  } catch (error) {
    const details = error as { stderr?: string; stdout?: string; message: string };
    const output = `${details.stdout ?? ''}${details.stderr ?? ''}`.trim().split('\n').slice(-20).join('\n');
    throw new Error(`Shared Lambda layer bundling failed (wheel-only, no Docker fallback):\n${output || details.message}`, {
      cause: error,
    });
  }
}

/** Asset hash of the shared layer: common source, runtime lock, bundler and target platform. */
export function sharedLayerAssetHash(paths: PythonAssetPaths = defaultPythonAssetPaths()): string {
  return fingerprintInputs(
    [
      { label: 'common', path: paths.commonDir },
      { label: 'requirements', path: paths.requirementsFile },
      { label: 'bundler', path: paths.bundlerScript },
    ],
    ['shared-layer', ...LAMBDA_WHEEL_PLATFORM],
  );
}

/** Asset hash of the templates layer: template file names and contents only. */
export function templatesLayerAssetHash(paths: PythonAssetPaths = defaultPythonAssetPaths()): string {
  return fingerprintInputs([{ label: 'templates', path: paths.templatesDir }], ['templates-layer']);
}

/**
 * Shared layer code: pinned dependencies plus the `common` package under python/, built locally
 * from hash-verified Linux x86_64 cp312 wheels.
 */
export function sharedLayerCode(paths: PythonAssetPaths = defaultPythonAssetPaths()): Code {
  return Code.fromAsset(paths.commonDir, {
    assetHash: sharedLayerAssetHash(paths),
    assetHashType: AssetHashType.CUSTOM,
    bundling: {
      image: NO_DOCKER_IMAGE,
      outputType: BundlingOutput.NOT_ARCHIVED,
      local: localOnlyBundling((outputDir) => {
        runBundler(paths, outputDir);
      }),
    },
  });
}

/** Templates layer code: every template file copied to templates/ so it mounts at /opt/templates. */
export function templatesLayerCode(paths: PythonAssetPaths = defaultPythonAssetPaths()): Code {
  return Code.fromAsset(paths.templatesDir, {
    assetHash: templatesLayerAssetHash(paths),
    assetHashType: AssetHashType.CUSTOM,
    bundling: {
      image: NO_DOCKER_IMAGE,
      outputType: BundlingOutput.NOT_ARCHIVED,
      local: localOnlyBundling((outputDir) => {
        const destination = path.join(outputDir, 'templates');
        fs.mkdirSync(destination, { recursive: true });
        for (const relative of listAssetFiles(paths.templatesDir)) {
          const target = path.join(destination, ...relative.split('/'));
          fs.mkdirSync(path.dirname(target), { recursive: true });
          fs.copyFileSync(path.join(paths.templatesDir, ...relative.split('/')), target);
        }
      }),
    },
  });
}

/** Handler code for one function directory under src/lambda, excluding bytecode caches. */
export function lambdaHandlerCode(handlerDir: string): Code {
  return Code.fromAsset(handlerDir, { exclude: ['__pycache__', '*.pyc', '*.pyo'] });
}

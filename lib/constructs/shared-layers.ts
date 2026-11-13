import { RemovalPolicy } from 'aws-cdk-lib';
import { Architecture, LayerVersion, Runtime } from 'aws-cdk-lib/aws-lambda';
import { Construct } from 'constructs';
import {
  defaultPythonAssetPaths,
  LAMBDA_TEMPLATES_DIR,
  type PythonAssetPaths,
  sharedLayerCode,
  TEMPLATES_DIR_ENV,
  templatesLayerCode,
} from '../packaging/python-assets';

export interface SharedLayersProps {
  readonly uniqueId: string;
  /** Source locations; defaults to this repository's src/, requirements.txt and bundler. */
  readonly assetPaths?: Partial<PythonAssetPaths>;
}

/**
 * The two layers every Python function uses:
 * - `commonLayer`: pinned runtime dependencies and the shared `common` package at /opt/python;
 * - `templatesLayer`: prompt templates at /opt/templates, versioned by template content.
 */
export class SharedLayers extends Construct {
  public readonly commonLayer: LayerVersion;
  public readonly templatesLayer: LayerVersion;
  /** Environment for functions that render prompts from the templates layer. */
  public readonly templateEnvironment: Readonly<Record<string, string>>;

  public constructor(scope: Construct, id: string, props: SharedLayersProps) {
    super(scope, id);
    const paths: PythonAssetPaths = { ...defaultPythonAssetPaths(), ...props.assetPaths };

    this.commonLayer = new LayerVersion(this, 'CommonLayer', {
      layerVersionName: `case-insights-common-${props.uniqueId}`,
      description: 'Support Case Insights shared Python package (/opt/python/common) and pinned dependencies',
      code: sharedLayerCode(paths),
      compatibleRuntimes: [Runtime.PYTHON_3_12],
      compatibleArchitectures: [Architecture.X86_64],
      license: 'MIT-0',
      removalPolicy: RemovalPolicy.DESTROY,
    });

    this.templatesLayer = new LayerVersion(this, 'TemplatesLayer', {
      layerVersionName: `case-insights-templates-${props.uniqueId}`,
      description: 'Support Case Insights prompt templates (/opt/templates)',
      code: templatesLayerCode(paths),
      compatibleRuntimes: [Runtime.PYTHON_3_12],
      compatibleArchitectures: [Architecture.X86_64],
      license: 'MIT-0',
      removalPolicy: RemovalPolicy.DESTROY,
    });

    this.templateEnvironment = { [TEMPLATES_DIR_ENV]: LAMBDA_TEMPLATES_DIR };
  }
}

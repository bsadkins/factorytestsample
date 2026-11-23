import * as fs from 'node:fs';
import * as path from 'node:path';
import { Duration, RemovalPolicy, Stack, type StackProps } from 'aws-cdk-lib';
import { BuildSpec, Cache, CfnProject, ComputeType, LinuxBuildImage, LocalCacheMode } from 'aws-cdk-lib/aws-codebuild';
import { CfnPolicy, PolicyStatement, Role } from 'aws-cdk-lib/aws-iam';
import { ExecutionMode, PipelineType } from 'aws-cdk-lib/aws-codepipeline';
import { BlockPublicAccess, Bucket, BucketEncryption, ObjectOwnership } from 'aws-cdk-lib/aws-s3';
import { CodePipeline, CodePipelineSource, ManualApprovalStep, ShellStep } from 'aws-cdk-lib/pipelines';
import type { Construct, IConstruct } from 'constructs';
import { addSharedApiLogging, type SharedApiLogging, sharedApiLoggingOwnerRequired } from '../app';
import { type CaseInsightsConfig, PIPELINE_CONTEXT_KEY, type PipelineConfig, validatePipelineStages } from '../config';
import { logicalIdOf } from '../constructs/construct-utils';
import { acknowledgeGeneratedPolicyWildcards, acknowledgeNagFinding, type GeneratedWildcardRule } from '../nag';
import { REPO_ROOT } from '../packaging/python-assets';
import { CaseInsightsEnvironmentStage, CaseInsightsSharedApiLoggingStage } from '../stages/case-insights-stages';

/** Ubuntu 24.04 CodeBuild image; offers the Node.js 24 and Python 3.12 runtimes pinned by .nvmrc and .python-version. */
export const PIPELINE_BUILD_IMAGE = LinuxBuildImage.STANDARD_8_0;
export const PIPELINE_RUNTIME_VERSIONS = { nodejs: '24', python: '3.12' } as const;
/** cdk-assets release installed by the asset publishing step (CDK Pipelines would otherwise install "latest"). */
export const CDK_ASSETS_CLI_VERSION = '4.8.0';
/** Cloud assembly directory of the pipeline app inside the synth step; `npm run synth` uses cdk.out/dev and cdk.out/prod. */
export const PIPELINE_SYNTH_OUTPUT = 'cdk.out/pipeline';
/** Repository-local pip cache used by layer bundling (lib/packaging/python-assets.ts); cached between synth builds. */
export const PIPELINE_WHEEL_CACHE_DIR = '.cache/pip';
export const PIPELINE_STACK_NAME = 'CaseInsightsPipelineStack';

const ARTIFACT_RETENTION_DAYS = 90;
const ACCESS_LOG_RETENTION_DAYS = 365;
const NONCURRENT_VERSION_DAYS = 30;
const INCOMPLETE_UPLOAD_DAYS = 7;

/** The exact aws-cdk CLI version pinned in package.json, so self-mutation runs the CLI the repository was verified with. */
export function pinnedCdkCliVersion(packageJsonPath: string = path.join(REPO_ROOT, 'package.json')): string {
  const manifest = JSON.parse(fs.readFileSync(packageJsonPath, 'utf8')) as { devDependencies?: Record<string, string> };
  const version = manifest.devDependencies?.['aws-cdk'];
  if (version === undefined || !/^\d+\.\d+\.\d+$/.test(version)) {
    throw new Error(`package.json must pin devDependencies["aws-cdk"] to an exact version; got ${String(version)}`);
  }
  return version;
}

/** Commands of the synth step, in order: verified locked dependencies, the full local gate, then the pipeline assembly. */
export const PIPELINE_INSTALL_COMMANDS = [
  'python3 -c "import sys; assert sys.version_info[:2] == (3, 12), sys.version"',
  'python3 -m venv .venv',
  '.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt',
  'npm ci',
] as const;
export const PIPELINE_SYNTH_COMMANDS = [
  'npm run verify',
  `npx cdk synth -c ${PIPELINE_CONTEXT_KEY}=true --no-lookups --quiet --output ${PIPELINE_SYNTH_OUTPUT}`,
] as const;

export interface CaseInsightsPipelineStackProps extends StackProps {
  readonly pipeline: PipelineConfig;
  readonly dev: CaseInsightsConfig;
  readonly prod: CaseInsightsConfig;
}

/**
 * Optional CDK Pipelines deployment in the management account: source (main of the configured GitHub
 * repository through an existing, human-authorised CodeConnections connection) -> verify and synth ->
 * self-mutation and asset publishing -> shared API Gateway logging (when needed) -> dev -> manual
 * approval -> prod. The stages contain only environment stacks, never another pipeline. Self-mutation
 * means anyone who can write to the source branch can change this pipeline, including its approval
 * step; the approval is a release gate, not a control against malicious source writers.
 */
export class CaseInsightsPipelineStack extends Stack {
  public readonly pipeline: CodePipeline;
  public readonly artifactBucket: Bucket;
  public readonly accessLogsBucket: Bucket;
  public readonly sharedApiLoggingStage?: CaseInsightsSharedApiLoggingStage;
  public readonly devStage: CaseInsightsEnvironmentStage;
  public readonly prodStage: CaseInsightsEnvironmentStage;
  public readonly approval: ManualApprovalStep;

  public constructor(scope: Construct, id: string, props: CaseInsightsPipelineStackProps) {
    super(scope, id, props);
    const { pipeline: settings, dev, prod } = props;
    validatePipelineStages(settings, dev, prod);
    const stageEnv = { account: settings.account, region: settings.region };

    const bucketBase = {
      encryption: BucketEncryption.S3_MANAGED,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      objectOwnership: ObjectOwnership.BUCKET_OWNER_ENFORCED,
      enforceSSL: true,
      minimumTLSVersion: 1.2,
      versioned: true,
      removalPolicy: RemovalPolicy.RETAIN,
    } as const;
    // The log destination does not log to itself.
    this.accessLogsBucket = new Bucket(this, 'ArtifactAccessLogsBucket', {
      ...bucketBase,
      lifecycleRules: [
        {
          id: 'ExpireAccessLogs',
          expiration: Duration.days(ACCESS_LOG_RETENTION_DAYS),
          noncurrentVersionExpiration: Duration.days(NONCURRENT_VERSION_DAYS),
          abortIncompleteMultipartUploadAfter: Duration.days(INCOMPLETE_UPLOAD_DAYS),
        },
      ],
    });
    this.artifactBucket = new Bucket(this, 'ArtifactBucket', {
      ...bucketBase,
      serverAccessLogsBucket: this.accessLogsBucket,
      serverAccessLogsPrefix: 'pipeline-artifacts/',
      lifecycleRules: [
        {
          id: 'ExpirePipelineArtifacts',
          expiration: Duration.days(ARTIFACT_RETENTION_DAYS),
          noncurrentVersionExpiration: Duration.days(NONCURRENT_VERSION_DAYS),
          abortIncompleteMultipartUploadAfter: Duration.days(INCOMPLETE_UPLOAD_DAYS),
        },
      ],
    });

    const source = CodePipelineSource.connection(settings.repository, settings.branch, {
      connectionArn: settings.connectionArn,
      triggerOnPush: true,
    });

    this.pipeline = new CodePipeline(this, 'Pipeline', {
      pipelineName: settings.pipelineName,
      pipelineType: PipelineType.V2,
      // Releases run one at a time in commit order; a newer commit never overtakes one awaiting approval.
      executionMode: ExecutionMode.QUEUED,
      artifactBucket: this.artifactBucket,
      selfMutation: true,
      dockerEnabledForSynth: false,
      dockerEnabledForSelfMutation: false,
      publishAssetsInParallel: false,
      useChangeSets: true,
      cliVersion: pinnedCdkCliVersion(),
      cdkAssetsCliVersion: CDK_ASSETS_CLI_VERSION,
      synth: new ShellStep('Synth', {
        input: source,
        env: { PIP_CACHE_DIR: PIPELINE_WHEEL_CACHE_DIR, PIP_DISABLE_PIP_VERSION_CHECK: '1' },
        installCommands: [...PIPELINE_INSTALL_COMMANDS],
        commands: [...PIPELINE_SYNTH_COMMANDS],
        primaryOutputDirectory: PIPELINE_SYNTH_OUTPUT,
      }),
      codeBuildDefaults: {
        buildEnvironment: { buildImage: PIPELINE_BUILD_IMAGE, computeType: ComputeType.SMALL, privileged: false },
        partialBuildSpec: BuildSpec.fromObject({ phases: { install: { 'runtime-versions': PIPELINE_RUNTIME_VERSIONS } } }),
        timeout: Duration.minutes(30),
      },
      synthCodeBuildDefaults: {
        buildEnvironment: { computeType: ComputeType.MEDIUM },
        // Best-effort cache only: every wheel is still verified against requirements.txt hashes, and an
        // empty cache falls back to the package index.
        cache: Cache.local(LocalCacheMode.CUSTOM),
        partialBuildSpec: BuildSpec.fromObject({ cache: { paths: [`${PIPELINE_WHEEL_CACHE_DIR}/**/*`] } }),
        timeout: Duration.minutes(60),
      },
    });

    const configs = [dev, prod];
    let sharedApiLogging: SharedApiLogging | undefined;
    if (sharedApiLoggingOwnerRequired(configs)) {
      this.sharedApiLoggingStage = new CaseInsightsSharedApiLoggingStage(this, 'SharedApiLogging', { env: stageEnv, configs });
      sharedApiLogging = this.sharedApiLoggingStage.sharedApiLogging;
      this.pipeline.addStage(this.sharedApiLoggingStage);
    } else {
      // Existing mode or MCP disabled everywhere: no shared stack is created (in existing mode the
      // account/Region setting is external), so this scope receives nothing.
      sharedApiLogging = addSharedApiLogging(this, configs);
    }

    this.devStage = new CaseInsightsEnvironmentStage(this, 'Dev', { env: stageEnv, config: dev, sharedApiLogging });
    this.pipeline.addStage(this.devStage);

    this.approval = new ManualApprovalStep('PromoteToProd', {
      comment: `Review the ${dev.uniqueId} deployment and the change sets before promoting this revision to ${prod.uniqueId}.`,
    });
    this.prodStage = new CaseInsightsEnvironmentStage(this, 'Prod', { env: stageEnv, config: prod, sharedApiLogging });
    this.pipeline.addStage(this.prodStage, { pre: [this.approval] });

    this.pipeline.buildPipeline();
    this.grantSourceConnection(settings.connectionArn);
    this.acknowledgeGeneratedFindings();
  }

  /**
   * The CodeStarSourceConnection action needs both codestar-connections:UseConnection and
   * codeconnections:UseConnection on the connection, whichever service prefix its ARN uses; CDK
   * Pipelines grants only the legacy action to the role CodePipeline assumes for the Source action.
   */
  private grantSourceConnection(connectionArn: string): void {
    const roles = this.pipeline.pipeline.node
      .findAll()
      .filter((construct): construct is Role => construct instanceof Role && construct.node.path.includes('/Pipeline/Source/'));
    const [role] = roles;
    if (role === undefined || roles.length !== 1) {
      throw new Error(`expected exactly one CDK Pipelines Source action role, found ${String(roles.length)}`);
    }
    role.addToPrincipalPolicy(
      new PolicyStatement({
        actions: ['codestar-connections:UseConnection', 'codeconnections:UseConnection'],
        resources: [connectionArn],
      }),
    );
  }

  /** cdk-nag findings on resources that CDK Pipelines generates, each acknowledged individually with its reason. */
  private acknowledgeGeneratedFindings(): void {
    for (const project of this.pipeline.node.findAll().filter((construct): construct is CfnProject => construct instanceof CfnProject)) {
      acknowledgeNagFinding(
        project,
        'AwsSolutions-CB4',
        'Approved AWS-managed encryption design (no customer-managed KMS keys): without an encryption key CodeBuild protects build output with the AWS managed aws/s3 key, and pipeline artifacts are stored in the SSE-S3 artifact bucket.',
      );
    }

    const policies = this.pipeline.node.findAll().filter((construct): construct is CfnPolicy => construct instanceof CfnPolicy);
    const policyAt = (fragment: string): CfnPolicy => {
      const matches = policies.filter((policy) => policy.node.path.includes(fragment));
      const [policy] = matches;
      if (policy === undefined || matches.length !== 1) {
        throw new Error(`expected exactly one CDK Pipelines policy at ${fragment}, found ${String(matches.length)}`);
      }
      return policy;
    };

    const artifacts = `<${logicalIdOf(this.artifactBucket)}.Arn>/*`;
    const artifactReason =
      "CDK Pipelines grant on this pipeline's own artifact bucket only: executions write and read artifact objects whose keys CodePipeline generates per run, so object-level wildcards on this one bucket are required.";
    const artifactRead = ['s3:GetBucket*', 's3:GetObject*', 's3:List*'];
    const artifactWrite = [...artifactRead, 's3:Abort*', 's3:DeleteObject*'];
    const artifactRules = (actions: readonly string[]): GeneratedWildcardRule[] => [
      ...actions.map((action) => ({ finding: `Action::${action}`, reason: artifactReason })),
      { finding: `Resource::${artifacts}`, reason: artifactReason },
    ];
    const logGroups = `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/codebuild/`;
    const reportGroups = `arn:aws:codebuild:${this.region}:${this.account}:report-group/`;
    const projectTelemetry = (project: IConstruct): GeneratedWildcardRule[] => {
      const id = logicalIdOf(project);
      const reason =
        'CodeBuild writes build logs to log streams it names per build, and test reports to report groups prefixed with this project name; both are limited to this one project.';
      return [
        { finding: `Resource::${logGroups}<${id}>:*`, reason },
        { finding: `Resource::${reportGroups}<${id}>-*`, reason },
      ];
    };

    acknowledgeGeneratedPolicyWildcards(policyAt('/Pipeline/Pipeline/Role/DefaultPolicy/'), artifactRules(artifactWrite));
    acknowledgeGeneratedPolicyWildcards(policyAt('/Pipeline/Pipeline/Source/'), artifactRules(artifactWrite));
    acknowledgeGeneratedPolicyWildcards(policyAt('/Build/Synth/CdkBuildProject/Role/DefaultPolicy/'), [
      ...artifactRules(artifactWrite),
      ...projectTelemetry(this.pipeline.synthProject),
    ]);
    acknowledgeGeneratedPolicyWildcards(policyAt('/UpdatePipeline/SelfMutation/Role/DefaultPolicy/'), [
      ...artifactRules(artifactRead),
      ...projectTelemetry(this.pipeline.selfMutationProject),
      {
        finding: `Resource::arn:*:iam::${this.account}:role/*`,
        reason:
          'Self-mutation assumes only the CDK bootstrap deploy and publishing roles of this account: the statement is conditioned on the iam:ResourceTag/aws-cdk:bootstrap-role tag, whose role names CDK does not know at synthesis.',
      },
      {
        finding: 'Resource::*',
        reason:
          'The CDK CLI in the self-mutation step calls cloudformation:DescribeStacks and s3:ListBucket to locate the bootstrap stack and asset bucket; these discovery calls are not resource-scoped by CDK Pipelines.',
      },
    ]);
    const assetsReason =
      'The single shared file-asset publishing project is created by CDK Pipelines and writes logs and reports under CodeBuild-generated names; it can only read pipeline artifacts and assume the CDK file-publishing role.';
    acknowledgeGeneratedPolicyWildcards(policyAt('/Assets/FileRole/DefaultPolicy/'), [
      ...artifactRules(artifactRead),
      { finding: `Resource::${logGroups}*`, reason: assetsReason },
      { finding: `Resource::${reportGroups}*`, reason: assetsReason },
      {
        finding: 'Resource::*',
        reason:
          'CDK Pipelines lets the asset publishing role start and poll its own CodeBuild builds (codebuild:StartBuild, BatchGetBuilds, StopBuild); the role is assumable only by CodeBuild and this pipeline.',
      },
    ]);
  }
}

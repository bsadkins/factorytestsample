import type { Duration } from 'aws-cdk-lib';
import { type IRole, Policy, PolicyStatement, Role, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import { Architecture, type CfnFunction, Function as LambdaFunction, type ILayerVersion, LoggingFormat, Runtime } from 'aws-cdk-lib/aws-lambda';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import type { CaseInsightsConfig } from '../config';
import { acknowledgeNagFinding } from '../nag';
import { lambdaHandlerCode } from '../packaging/python-assets';
import { dataRemovalPolicy } from './environment';

/** CloudWatch metric namespace used by every function's Powertools Metrics. */
export const METRICS_NAMESPACE = 'SupportCaseInsights';

/**
 * Powertools settings applied to every function. Event and response capture stay off so case
 * content never reaches logs or X-Ray trace metadata. Callers cannot override these.
 */
export const SAFE_POWERTOOLS_ENVIRONMENT: Readonly<Record<string, string>> = {
  POWERTOOLS_METRICS_NAMESPACE: METRICS_NAMESPACE,
  POWERTOOLS_LOG_LEVEL: 'INFO',
  POWERTOOLS_LOGGER_LOG_EVENT: 'false',
  POWERTOOLS_LOGGER_SAMPLE_RATE: '0',
  POWERTOOLS_TRACER_CAPTURE_RESPONSE: 'false',
  POWERTOOLS_TRACER_CAPTURE_ERROR: 'false',
};

const MAX_FUNCTION_NAME_LENGTH = 64;
const SERVICE_NAME_PATTERN = /^[a-z][a-z0-9-]{1,62}$/;

export interface PipelineFunctionProps {
  readonly config: CaseInsightsConfig;
  /** Physical name; must include uniqueId. */
  readonly functionName: string;
  readonly description: string;
  /** Absolute path of the handler directory under src/lambda. */
  readonly handlerDir: string;
  /** Defaults to `app.handler`. */
  readonly handler?: string;
  /** Powertools service name (bounded, low-cardinality metric dimension). */
  readonly serviceName: string;
  readonly layers: readonly ILayerVersion[];
  readonly timeout: Duration;
  /** Defaults to operations.functions.memorySizeMb. */
  readonly memorySize?: number;
  /** A focused role created elsewhere (for example a role trusted by member accounts); otherwise one is created. */
  readonly role?: IRole;
  readonly environment?: Readonly<Record<string, string>>;
  readonly reservedConcurrentExecutions?: number;
}

/**
 * Standard Python 3.12 x86_64 pipeline function: its own execution role, its own log group with
 * configured retention, JSON logging, active X-Ray tracing and safe Powertools defaults.
 * The role receives only log writes for this function's log group and X-Ray segment writes;
 * callers grant everything else explicitly.
 */
export class PipelineFunction extends Construct {
  public readonly function: LambdaFunction;
  public readonly role: IRole;
  public readonly logGroup: LogGroup;

  public constructor(scope: Construct, id: string, props: PipelineFunctionProps) {
    super(scope, id);
    const { config } = props;
    validateProps(props);

    this.role =
      props.role ??
      new Role(this, 'Role', {
        description: `Execution role for ${props.functionName}`,
        assumedBy: new ServicePrincipal('lambda.amazonaws.com'),
      });

    this.logGroup = new LogGroup(this, 'LogGroup', {
      logGroupName: `/aws/lambda/${props.functionName}`,
      retention: logRetention(config.logRetentionDays),
      removalPolicy: dataRemovalPolicy(config),
    });

    const telemetry = new Policy(this, 'TelemetryPolicy', {
      roles: [this.role],
      statements: [
        new PolicyStatement({
          sid: 'WriteOwnLogGroup',
          actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
          resources: [this.logGroup.logGroupArn],
        }),
        new PolicyStatement({
          sid: 'WriteXRaySegments',
          actions: ['xray:PutTraceSegments', 'xray:PutTelemetryRecords'],
          resources: ['*'],
        }),
      ],
    });
    acknowledgeNagFinding(
      telemetry,
      'AwsSolutions-IAM5[Resource::*]',
      'xray:PutTraceSegments and xray:PutTelemetryRecords do not support resource-level permissions; this policy holds only those two actions plus log writes scoped to the function log group.',
    );

    this.function = new LambdaFunction(this, 'Function', {
      functionName: props.functionName,
      description: props.description,
      runtime: Runtime.PYTHON_3_12,
      architecture: Architecture.X86_64,
      handler: props.handler ?? 'app.handler',
      code: lambdaHandlerCode(props.handlerDir),
      layers: [...props.layers],
      role: this.role,
      timeout: props.timeout,
      memorySize: props.memorySize ?? config.operations.functions.memorySizeMb,
      logGroup: this.logGroup,
      loggingFormat: LoggingFormat.JSON,
      reservedConcurrentExecutions: props.reservedConcurrentExecutions,
      environment: {
        ...props.environment,
        ...SAFE_POWERTOOLS_ENVIRONMENT,
        POWERTOOLS_SERVICE_NAME: props.serviceName,
        CASE_INSIGHTS_UNIQUE_ID: config.uniqueId,
      },
    });
    // Tracing is enabled here rather than through the `tracing` prop so the X-Ray wildcard sits
    // in the dedicated telemetry policy above instead of the role's shared default policy.
    (this.function.node.defaultChild as CfnFunction).tracingConfig = { mode: 'Active' };
    this.function.node.addDependency(telemetry);

    acknowledgeNagFinding(
      this.function,
      'AwsSolutions-L1',
      'Python 3.12 is the approved runtime: the shared layer is built from hash-locked CPython 3.12 (cp312) Linux x86_64 wheels, and changing runtime requires re-locking those wheels.',
    );
  }
}

/** CloudWatch Logs retention for a day count the config schema already restricts to valid values. */
export function logRetention(days: number): RetentionDays {
  const match = (Object.values(RetentionDays) as unknown[]).find((value): value is RetentionDays => value === days);
  if (match === undefined) {
    throw new Error(`Unsupported CloudWatch Logs retention: ${String(days)} days`);
  }
  return match;
}

function validateProps(props: PipelineFunctionProps): void {
  const { functionName, config } = props;
  if (functionName.length > MAX_FUNCTION_NAME_LENGTH || !/^[A-Za-z0-9_-]+$/.test(functionName)) {
    throw new Error(`Invalid Lambda function name: ${functionName}`);
  }
  if (!functionName.includes(config.uniqueId)) {
    throw new Error(`Function name ${functionName} must include uniqueId ${config.uniqueId}`);
  }
  if (!SERVICE_NAME_PATTERN.test(props.serviceName)) {
    throw new Error(`Invalid Powertools service name: ${props.serviceName}`);
  }
  const reserved = ['POWERTOOLS_SERVICE_NAME', 'CASE_INSIGHTS_UNIQUE_ID', ...Object.keys(SAFE_POWERTOOLS_ENVIRONMENT)];
  const overridden = Object.keys(props.environment ?? {}).filter((key) => reserved.includes(key));
  if (overridden.length > 0) {
    throw new Error(`Environment variables managed by PipelineFunction cannot be overridden: ${overridden.join(', ')}`);
  }
}

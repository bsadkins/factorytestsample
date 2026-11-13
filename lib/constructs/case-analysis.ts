import * as path from 'node:path';
import { ArnFormat, Duration, Stack, TimeZone, Token } from 'aws-cdk-lib';
import type { ITable } from 'aws-cdk-lib/aws-dynamodb';
import { Policy, PolicyStatement, Role } from 'aws-cdk-lib/aws-iam';
import type { ILayerVersion } from 'aws-cdk-lib/aws-lambda';
import { SqsEventSource } from 'aws-cdk-lib/aws-lambda-event-sources';
import { LogGroup } from 'aws-cdk-lib/aws-logs';
import type { IBucket } from 'aws-cdk-lib/aws-s3';
import { Schedule, ScheduleExpression } from 'aws-cdk-lib/aws-scheduler';
import { LambdaInvoke as ScheduledLambdaInvoke } from 'aws-cdk-lib/aws-scheduler-targets';
import type { IQueue } from 'aws-cdk-lib/aws-sqs';
import {
  DefinitionBody,
  Errors,
  Fail,
  JitterType,
  JsonPath,
  LogLevel,
  type RetryProps,
  StateMachine,
  StateMachineType,
  TaskInput,
} from 'aws-cdk-lib/aws-stepfunctions';
import { LambdaInvoke } from 'aws-cdk-lib/aws-stepfunctions-tasks';
import { Construct, type IConstruct } from 'constructs';
import { type CaseInsightsConfig, resourceNames } from '../config';
import { acknowledgeNagFinding } from '../nag';
import { lambdaPackagesCode, REPO_ROOT } from '../packaging/python-assets';
import { BedrockInvokePolicy } from './bedrock-invoke-policy';
import { CASE_PARTITION_PATTERN, RUN_CASE_MARKER_PATTERN } from './case-collection';
import { handlerDirectory, logicalIdOf, schedulerPrincipal } from './construct-utils';
import { dataRemovalPolicy } from './environment';
import { logRetention, PipelineFunction } from './pipeline-function';

/** Summary references per starter invocation; each is reconciled independently. */
export const STARTER_BATCH_SIZE = 10;
/** Handler directories packaged into the shared Bedrock stage function, entry point first. */
export const ANALYSIS_STAGE_PACKAGES = ['analysis_stages', 'case_summary', 'case_rca', 'case_lifecycle'] as const;
export const ANALYSIS_STAGES = ['summary', 'rca', 'lifecycle'] as const;
/** Raw-bucket key prefix of cleanup's resume cursor; must match CURSOR_KEY in case_cleanup/app.py. */
export const CLEANUP_STATE_PREFIX = 'cleanup-state/';
/** Files cleanup may delete from an abandoned case; must match RESETTABLE_FILES in case_cleanup/app.py. */
export const CLEANUP_RESETTABLE_FILES = ['data.json', 'communications.json', 'workflow.json'] as const;
/** Failure-delivery markers; must match DELIVERY_DIR in common/failure_delivery.py. Never deletable. */
export const FAILURE_DELIVERY_PATTERN = 'account_number=*/case_number=*/failure-deliveries/*';
/** Scheduler retries delivering the daily cleanup invocation; a missed run is repeated the next day. */
export const CLEANUP_SCHEDULE_RETRY_ATTEMPTS = 3;
export const CLEANUP_MAX_EVENT_AGE = Duration.hours(6);

/**
 * Lambda rejects invocations beyond the stage function's reserved concurrency with
 * TooManyRequestsException; the workflow waits and retries, which is how the Bedrock cap applies
 * backpressure. About 15 minutes of waiting in total before the attempt fails and is recorded.
 */
export const LAMBDA_THROTTLE_RETRY: RetryProps = {
  errors: ['Lambda.TooManyRequestsException'],
  interval: Duration.seconds(5),
  backoffRate: 2,
  maxAttempts: 8,
  maxDelay: Duration.minutes(5),
  jitterStrategy: JitterType.FULL,
};
/** Named stage errors left after the stage's own bounded in-function Bedrock retries. */
export const BEDROCK_RETRYABLE_ERRORS = ['BedrockThrottlingError', 'BedrockTransientError'] as const;
/** S3/network errors and function or task timeouts; a timed-out stage may rarely repeat an inference. */
export const TRANSIENT_TASK_ERRORS = [
  'ClientError',
  'EndpointConnectionError',
  'ConnectTimeoutError',
  'ReadTimeoutError',
  'Sandbox.Timedout',
  Errors.TIMEOUT,
] as const;
/** Failure-record delivery is retried longer than tasks: losing the record would hide the failure. */
export const FAILURE_HANDLER_RETRY: RetryProps = {
  errors: ['FailureDeliveryError', 'Lambda.TooManyRequestsException', Errors.TIMEOUT],
  interval: Duration.seconds(5),
  backoffRate: 2,
  maxAttempts: 6,
  maxDelay: Duration.minutes(2),
  jitterStrategy: JitterType.FULL,
};
export const ANALYSIS_FAILED_ERROR = 'CaseAnalysisFailed';

export interface CaseAnalysisProps {
  readonly config: CaseInsightsConfig;
  readonly rawBucket: IBucket;
  readonly processedBucket: IBucket;
  /** Powertools idempotency table used by the workflow starter. */
  readonly idempotencyTable: ITable;
  /** Annotated case references consumed by the workflow starter. */
  readonly summaryQueue: IQueue;
  /** Terminal queue receiving one failure record per exhausted case-analysis attempt. */
  readonly failureQueue: IQueue;
  readonly commonLayer: ILayerVersion;
  readonly templatesLayer: ILayerVersion;
  /** Environment pointing common.templates at the templates layer. */
  readonly templateEnvironment: Readonly<Record<string, string>>;
}

/**
 * Case analysis: the Workflow Starter consumes summary references and starts one Standard
 * `CaseAnalysis-<uniqueId>` execution per case attempt (Summary -> RCA -> Lifecycle ->
 * UpdateCaseMetadata). Every task has bounded retries and catches into Handle Failure, which
 * records a safe failure message on the workflow failure queue before the execution fails. Case
 * Cleanup runs daily and resets abandoned, unfinished cases so a later run retries them.
 *
 * Bedrock concurrency: the three Bedrock tasks run in one function whose reserved concurrency is
 * exactly `bedrockMaxConcurrency`. One invocation runs one stage and makes its Bedrock calls
 * sequentially, so concurrent Bedrock requests never exceed the configured value, including caps
 * of 1 or 2. Starter concurrency only limits how fast executions start; running executions wait
 * for stage capacity through Lambda throttling and bounded workflow retries.
 */
export class CaseAnalysis extends Construct {
  public readonly starterFunction: PipelineFunction;
  public readonly stagesFunction: PipelineFunction;
  public readonly updateFunction: PipelineFunction;
  public readonly failureFunction: PipelineFunction;
  public readonly cleanupFunction: PipelineFunction;
  public readonly stateMachine: StateMachine;
  public readonly cleanupSchedule: Schedule;

  public constructor(scope: Construct, id: string, props: CaseAnalysisProps) {
    super(scope, id);
    const { config, rawBucket, processedBucket, idempotencyTable, commonLayer } = props;
    const names = resourceNames(config);
    const { functions, workflow, cleanup } = config.operations;
    const stack = Stack.of(this);
    const analysisTimeout = Duration.seconds(functions.analysisTimeoutSeconds);
    const consumerTimeout = Duration.seconds(functions.consumerTimeoutSeconds);
    const buckets = { RAW_BUCKET: rawBucket.bucketName, PROCESSED_BUCKET: processedBucket.bucketName };
    const stateMachineArn = stack.formatArn({
      service: 'states',
      resource: 'stateMachine',
      resourceName: names.caseAnalysisStateMachine,
      arnFormat: ArnFormat.COLON_RESOURCE_NAME,
    });
    const executionsArn = stack.formatArn({
      service: 'states',
      resource: 'execution',
      resourceName: `${names.caseAnalysisStateMachine}:*`,
      arnFormat: ArnFormat.COLON_RESOURCE_NAME,
    });
    const raw = new BucketGrants(rawBucket);
    const processed = new BucketGrants(processedBucket);

    this.stagesFunction = new PipelineFunction(this, 'AnalysisStages', {
      config,
      functionName: names.caseAnalysisStagesFunction,
      description: `Summary, RCA and Lifecycle Bedrock tasks of the CaseAnalysis workflow (${config.uniqueId})`,
      handlerDir: handlerDirectory(ANALYSIS_STAGE_PACKAGES[0]),
      code: lambdaPackagesCode(path.join(REPO_ROOT, 'src', 'lambda'), ANALYSIS_STAGE_PACKAGES),
      handler: `${ANALYSIS_STAGE_PACKAGES[0]}.app.handler`,
      serviceName: 'case-analysis-stages',
      layers: [commonLayer, props.templatesLayer],
      timeout: analysisTimeout,
      reservedConcurrentExecutions: config.bedrockMaxConcurrency,
      environment: {
        ...buckets,
        ...props.templateEnvironment,
        BEDROCK_MODEL_ID: config.bedrockModelId,
        BEDROCK_MAX_TOKENS: String(config.bedrockMaxTokens),
      },
    });
    const stagesAccess = new Policy(this, 'AnalysisStagesAccess', {
      roles: [this.stagesFunction.role],
      statements: [
        raw.read('ReadRawCases', CASE_PARTITION_PATTERN, ['s3:GetObject', 's3:GetObjectVersion']),
        raw.write('WriteStageCheckpoints', 'account_number=*/case_number=*/checkpoints/*'),
        processed.read('CheckProcessedRecords', CASE_PARTITION_PATTERN),
        listBuckets([rawBucket, processedBucket]),
      ],
    });
    raw.acknowledge(stagesAccess, CASE_PARTITION_PATTERN, 'Stages read the raw metadata and conversation of the case named in the workflow input; keys are account and case IDs discovered at run time.');
    raw.acknowledge(stagesAccess, 'account_number=*/case_number=*/checkpoints/*', 'Stage checkpoints are written only under each case partition checkpoints/ prefix; account and case IDs are discovered at run time.');
    processed.acknowledge(stagesAccess, CASE_PARTITION_PATTERN, 'Stages only read (HEAD) processed records to stop before inference once a case is committed; keys are account and case IDs discovered at run time.');
    new BedrockInvokePolicy(this, 'AnalysisStagesBedrock', { role: this.stagesFunction.role, modelIds: [config.bedrockModelId] });

    this.updateFunction = new PipelineFunction(this, 'UpdateCaseMetadata', {
      config,
      functionName: names.updateCaseMetadataFunction,
      description: `Commits the validated processed record for one case (${config.uniqueId})`,
      handlerDir: handlerDirectory('update_case_metadata'),
      serviceName: 'update-case-metadata',
      // Every commit computes Prompt_Version from the template files in the templates layer.
      layers: [commonLayer, props.templatesLayer],
      timeout: analysisTimeout,
      environment: { ...buckets, ...props.templateEnvironment, BEDROCK_MODEL_ID: config.bedrockModelId },
    });
    const updateAccess = new Policy(this, 'UpdateCaseMetadataAccess', {
      roles: [this.updateFunction.role],
      statements: [
        raw.read('ReadRawCasesAndCheckpoints', CASE_PARTITION_PATTERN, ['s3:GetObject', 's3:GetObjectVersion']),
        processed.read('CheckProcessedRecords', CASE_PARTITION_PATTERN),
        processed.write('CommitProcessedRecords', CASE_PARTITION_PATTERN),
        listBuckets([rawBucket, processedBucket]),
      ],
    });
    raw.acknowledge(updateAccess, CASE_PARTITION_PATTERN, 'The update task reads raw metadata and stage checkpoints of the case named in the workflow input; keys are account and case IDs discovered at run time.');
    processed.acknowledge(updateAccess, CASE_PARTITION_PATTERN, 'Processed records are keyed by account and case ID discovered at run time; the task reads and conditionally creates only account_number= partition records.');

    this.failureFunction = new PipelineFunction(this, 'HandleFailure', {
      config,
      functionName: names.handleFailureFunction,
      description: `Records exhausted case-analysis attempts on the workflow failure queue (${config.uniqueId})`,
      handlerDir: handlerDirectory('handle_failure'),
      serviceName: 'handle-failure',
      layers: [commonLayer],
      timeout: analysisTimeout,
      environment: { FAILURE_QUEUE_URL: props.failureQueue.queueUrl, RAW_BUCKET: rawBucket.bucketName },
    });
    const sendFailureRecords = (): PolicyStatement =>
      new PolicyStatement({ sid: 'SendFailureRecords', actions: ['sqs:SendMessage'], resources: [props.failureQueue.queueArn] });
    const trackFailureDeliveries = (): PolicyStatement => raw.read('TrackFailureDeliveries', FAILURE_DELIVERY_PATTERN, ['s3:GetObject', 's3:PutObject']);
    const failureAccess = new Policy(this, 'HandleFailureAccess', {
      roles: [this.failureFunction.role],
      statements: [sendFailureRecords(), trackFailureDeliveries(), listBuckets([rawBucket])],
    });
    raw.acknowledge(failureAccess, FAILURE_DELIVERY_PATTERN, 'The failure handler reads and confirms the failure-delivery marker of the failed execution only, under the case partition failure-deliveries/ prefix; keys are discovered at run time.');

    this.stateMachine = this.buildStateMachine(config, names.caseAnalysisStateMachine);

    this.starterFunction = new PipelineFunction(this, 'WorkflowStarter', {
      config,
      functionName: names.workflowStarterFunction,
      description: `Starts one CaseAnalysis execution per case attempt (${config.uniqueId})`,
      handlerDir: handlerDirectory('workflow_starter'),
      serviceName: 'workflow-starter',
      layers: [commonLayer],
      timeout: consumerTimeout,
      environment: {
        ...buckets,
        STATE_MACHINE_ARN: stateMachineArn,
        // A starter cannot outlive its timeout, so an older claim without an execution is abandoned.
        CLAIM_SETTLE_SECONDS: String(functions.consumerTimeoutSeconds),
        IDEMPOTENCY_TABLE_NAME: idempotencyTable.tableName,
      },
    });
    const starterAccess = new Policy(this, 'WorkflowStarterAccess', {
      roles: [this.starterFunction.role],
      statements: [
        raw.read('ClaimCaseOwnership', 'account_number=*/case_number=*/workflow.json', ['s3:GetObject', 's3:PutObject']),
        trackFailureDeliveries(),
        raw.write('RecordRunCases', RUN_CASE_MARKER_PATTERN),
        processed.read('CheckProcessedRecords', CASE_PARTITION_PATTERN),
        listBuckets([rawBucket, processedBucket]),
        new PolicyStatement({ sid: 'StartCaseAnalysis', actions: ['states:StartExecution'], resources: [stateMachineArn] }),
        new PolicyStatement({ sid: 'DescribeCaseAnalysis', actions: ['states:DescribeExecution'], resources: [executionsArn] }),
        new PolicyStatement({
          sid: 'IdempotencyRecords',
          actions: ['dynamodb:DeleteItem', 'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem'],
          resources: [idempotencyTable.tableArn],
        }),
      ],
    });
    raw.acknowledge(starterAccess, 'account_number=*/case_number=*/workflow.json', 'Ownership markers are one workflow.json per case partition, keyed by account and case IDs discovered at run time; only that file is read or written.');
    raw.acknowledge(starterAccess, FAILURE_DELIVERY_PATTERN, 'Before replacing the ownership marker of a closed failed execution the starter preserves a pending failure-delivery marker under the case failure-deliveries/ prefix; keys are discovered at run time.');
    raw.acknowledge(starterAccess, RUN_CASE_MARKER_PATTERN, 'The starter records which execution owns each case for the run under runs/<runId>/cases/; run, account and case IDs are discovered at run time.');
    processed.acknowledge(starterAccess, CASE_PARTITION_PATTERN, 'The starter only reads (HEAD) processed records so completed cases are never started again; keys are account and case IDs discovered at run time.');
    acknowledgeExecutions(starterAccess, names.caseAnalysisStateMachine, 'Execution names are derived per case attempt at run time; DescribeExecution is limited to executions of this deployment CaseAnalysis state machine.');
    this.starterFunction.function.addEventSource(
      new SqsEventSource(props.summaryQueue, {
        batchSize: STARTER_BATCH_SIZE,
        maxConcurrency: workflow.starterMaxConcurrency,
        reportBatchItemFailures: true,
      }),
    );

    this.cleanupFunction = new PipelineFunction(this, 'CaseCleanup', {
      config,
      functionName: names.caseCleanupFunction,
      description: `Daily reset of abandoned, unfinished cases for a later run (${config.uniqueId})`,
      handlerDir: handlerDirectory('case_cleanup'),
      serviceName: 'case-cleanup',
      layers: [commonLayer],
      timeout: consumerTimeout,
      environment: {
        ...buckets,
        STATE_MACHINE_ARN: stateMachineArn,
        CLEANUP_GRACE_HOURS: String(cleanup.graceHours),
        FAILURE_QUEUE_URL: props.failureQueue.queueUrl,
      },
    });
    const resettable = CLEANUP_RESETTABLE_FILES.map((file) => `account_number=*/case_number=*/${file}`);
    const cleanupAccess = new Policy(this, 'CaseCleanupAccess', {
      roles: [this.cleanupFunction.role],
      statements: [
        raw.read('ReadCaseOwnership', CASE_PARTITION_PATTERN),
        new PolicyStatement({
          sid: 'ResetAbandonedCases',
          actions: ['s3:DeleteObject'],
          resources: resettable.map((pattern) => rawBucket.arnForObjects(pattern)),
        }),
        raw.read('KeepCleanupCursor', `${CLEANUP_STATE_PREFIX}*`, ['s3:DeleteObject', 's3:GetObject', 's3:PutObject']),
        trackFailureDeliveries(),
        sendFailureRecords(),
        processed.read('CheckProcessedRecords', CASE_PARTITION_PATTERN),
        listBuckets([rawBucket, processedBucket]),
        new PolicyStatement({ sid: 'ListActiveExecutions', actions: ['states:ListExecutions'], resources: [stateMachineArn] }),
        new PolicyStatement({ sid: 'DescribeCaseAnalysis', actions: ['states:DescribeExecution'], resources: [executionsArn] }),
      ],
    });
    raw.acknowledge(cleanupAccess, CASE_PARTITION_PATTERN, 'Cleanup reads ownership markers of case partitions discovered by listing the raw bucket; it never reads processed output from the raw bucket.');
    for (const pattern of resettable) {
      raw.acknowledge(cleanupAccess, pattern, 'Cleanup may delete only data.json, communications.json and workflow.json of abandoned cases (conditional on the checked ETag); checkpoints and terminal markers are never deletable.');
    }
    raw.acknowledge(cleanupAccess, FAILURE_DELIVERY_PATTERN, 'Cleanup preserves and delivers failure-delivery markers of closed failed executions under the case failure-deliveries/ prefix; it can write but never delete them.');
    raw.acknowledge(cleanupAccess, `${CLEANUP_STATE_PREFIX}*`, 'The cleanup resume cursor lives under the cleanup-state/ prefix of the raw bucket; its name is fixed but the policy keeps the prefix for future cursor versions.');
    processed.acknowledge(cleanupAccess, CASE_PARTITION_PATTERN, 'Cleanup only lists and reads (HEAD) processed records so committed cases are never reset; keys are account and case IDs discovered at run time.');
    acknowledgeExecutions(cleanupAccess, names.caseAnalysisStateMachine, 'Cleanup describes the execution named by a case ownership marker; DescribeExecution is limited to executions of this deployment CaseAnalysis state machine.');

    this.cleanupSchedule = this.buildCleanupSchedule(config, names.caseCleanupSchedule);
  }

  private buildStateMachine(config: CaseInsightsConfig, stateMachineName: string): StateMachine {
    const { workflow } = config.operations;
    const bedrockRetry: RetryProps = {
      errors: [...BEDROCK_RETRYABLE_ERRORS],
      interval: Duration.seconds(15),
      backoffRate: 2,
      maxAttempts: workflow.taskMaxAttempts,
      maxDelay: Duration.minutes(2),
      jitterStrategy: JitterType.FULL,
    };
    const transientRetry: RetryProps = {
      errors: [...TRANSIENT_TASK_ERRORS],
      interval: Duration.seconds(5),
      backoffRate: 2,
      maxAttempts: workflow.taskMaxAttempts,
      maxDelay: Duration.minutes(1),
      jitterStrategy: JitterType.FULL,
    };

    const handleFailure = new LambdaInvoke(this, 'HandleFailureTask', {
      stateName: 'HandleFailure',
      lambdaFunction: this.failureFunction.function,
      // No payload: the handler receives the whole state, including the caught $.error.
      payloadResponseOnly: true,
      resultPath: '$.failureRecord',
    });
    handleFailure.addRetry(FAILURE_HANDLER_RETRY);
    handleFailure.next(
      new Fail(this, 'AnalysisFailed', {
        stateName: 'AnalysisFailed',
        error: ANALYSIS_FAILED_ERROR,
        cause: 'Task retries were exhausted; the failure was recorded on the workflow failure queue.',
      }),
    );

    const task = (stateName: string, resultPath: string, fn: PipelineFunction, payload: TaskInput, retries: RetryProps[]): LambdaInvoke => {
      const invoke = new LambdaInvoke(this, `${stateName}Task`, {
        stateName,
        lambdaFunction: fn.function,
        payload,
        payloadResponseOnly: true,
        resultPath,
      });
      for (const retry of retries) {
        invoke.addRetry(retry);
      }
      invoke.addCatch(handleFailure, { errors: [Errors.ALL], resultPath: '$.error' });
      return invoke;
    };
    const stageTask = (stateName: string, stage: (typeof ANALYSIS_STAGES)[number]): LambdaInvoke =>
      task(stateName, `$.${stage}`, this.stagesFunction, TaskInput.fromObject({ stage, case: JsonPath.objectAt('$.case') }), [
        LAMBDA_THROTTLE_RETRY,
        bedrockRetry,
        transientRetry,
      ]);
    const summary = stageTask('Summary', 'summary');
    const rca = stageTask('RCA', 'rca');
    const lifecycle = stageTask('Lifecycle', 'lifecycle');
    const update = task('UpdateCaseMetadata', '$.update', this.updateFunction, TaskInput.fromObject({ case: JsonPath.objectAt('$.case') }), [
      LAMBDA_THROTTLE_RETRY,
      transientRetry,
    ]);

    const logGroup = new LogGroup(this, 'StateMachineLogs', {
      logGroupName: `/aws/vendedlogs/states/${stateMachineName}`,
      retention: logRetention(config.logRetentionDays),
      removalPolicy: dataRemovalPolicy(config),
    });
    const stateMachine = new StateMachine(this, 'StateMachine', {
      stateMachineName,
      stateMachineType: StateMachineType.STANDARD,
      comment: 'One case attempt: Summary -> RCA -> Lifecycle -> UpdateCaseMetadata; state carries references only.',
      definitionBody: DefinitionBody.fromChainable(summary.next(rca).next(lifecycle).next(update)),
      timeout: Duration.minutes(workflow.executionTimeoutMinutes),
      tracingEnabled: true,
      // Execution data stays out of the logs: state holds only references, but task errors can
      // carry exception text.
      logs: { destination: logGroup, level: LogLevel.ALL, includeExecutionData: false },
    });
    acknowledgeNagFinding(
      stateMachine.role,
      'AwsSolutions-IAM5[Resource::*]',
      'CloudWatch Logs delivery actions and X-Ray segment writes used by Step Functions logging and tracing do not support resource-level permissions; the role otherwise only invokes the workflow functions.',
    );
    for (const fn of [this.stagesFunction, this.updateFunction, this.failureFunction]) {
      acknowledgeNagFinding(
        stateMachine.role,
        `AwsSolutions-IAM5[Resource::<${logicalIdOf(fn.function)}.Arn>:*]`,
        'The CDK Lambda task grant covers this workflow function and its versions or aliases (:*); the state machine invokes only its own task functions.',
      );
    }
    return stateMachine;
  }

  private buildCleanupSchedule(config: CaseInsightsConfig, scheduleName: string): Schedule {
    const role = new Role(this, 'CaseCleanupScheduleRole', {
      description: `EventBridge Scheduler role that invokes ${this.cleanupFunction.function.node.id}`,
      assumedBy: schedulerPrincipal(this),
    });
    acknowledgeNagFinding(
      role,
      `AwsSolutions-IAM5[Resource::<${logicalIdOf(this.cleanupFunction.function)}.Arn>:*]`,
      'The CDK Lambda grant covers the case cleanup function and its versions or aliases (:*); the role holds only lambda:InvokeFunction on that one function.',
    );
    return new Schedule(this, 'CaseCleanupSchedule', {
      scheduleName,
      description: `Daily Support Case Insights cleanup of abandoned unfinished cases (${config.uniqueId})`,
      schedule: ScheduleExpression.expression(config.schedules.cleanup, TimeZone.ETC_UTC),
      target: new ScheduledLambdaInvoke(this.cleanupFunction.function, {
        retryAttempts: CLEANUP_SCHEDULE_RETRY_ATTEMPTS,
        maxEventAge: CLEANUP_MAX_EVENT_AGE,
        role,
      }),
    });
  }
}

/** Object-level statements and matching cdk-nag acknowledgements for one bucket. */
class BucketGrants {
  public constructor(private readonly bucket: IBucket) {}

  public read(sid: string, pattern: string, actions: string[] = ['s3:GetObject']): PolicyStatement {
    return new PolicyStatement({ sid, actions, resources: [this.bucket.arnForObjects(pattern)] });
  }

  public write(sid: string, pattern: string): PolicyStatement {
    return new PolicyStatement({ sid, actions: ['s3:PutObject'], resources: [this.bucket.arnForObjects(pattern)] });
  }

  public acknowledge(policy: IConstruct, pattern: string, reason: string): void {
    acknowledgeNagFinding(policy, `AwsSolutions-IAM5[Resource::<${logicalIdOf(this.bucket)}.Arn>/${pattern}]`, reason);
  }
}

/**
 * Bucket-level listing. It is not prefix-conditioned because S3 returns 404 instead of 403 for a
 * missing object only when s3:ListBucket is allowed for the request, and object reads carry no
 * s3:prefix; the handlers rely on 404 to tell "absent" from "denied".
 */
function listBuckets(buckets: readonly IBucket[]): PolicyStatement {
  return new PolicyStatement({ sid: 'ListCaseBuckets', actions: ['s3:ListBucket'], resources: buckets.map((b) => b.bucketArn) });
}

/** cdk-nag renders unresolved pseudo parameters in finding IDs as `<AWS::Name>`. */
function nagArnPart(value: string, pseudo: string): string {
  return Token.isUnresolved(value) ? `<AWS::${pseudo}>` : value;
}

/** Acknowledges DescribeExecution on every execution of one CaseAnalysis state machine. */
export function acknowledgeExecutions(policy: IConstruct, stateMachineName: string, reason: string): void {
  const stack = Stack.of(policy);
  const resource = [
    'arn',
    nagArnPart(stack.partition, 'Partition'),
    'states',
    nagArnPart(stack.region, 'Region'),
    nagArnPart(stack.account, 'AccountId'),
    'execution',
    stateMachineName,
    '*',
  ].join(':');
  acknowledgeNagFinding(policy, `AwsSolutions-IAM5[Resource::${resource}]`, reason);
}

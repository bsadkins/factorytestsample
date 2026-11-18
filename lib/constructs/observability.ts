import { ArnFormat, Duration, Stack } from 'aws-cdk-lib';
import {
  Alarm,
  AlarmStatusWidget,
  ComparisonOperator,
  Dashboard,
  GraphWidget,
  type IMetric,
  MathExpression,
  Metric,
  SingleValueWidget,
  Stats,
  TextWidget,
  TreatMissingData,
} from 'aws-cdk-lib/aws-cloudwatch';
import { SnsAction } from 'aws-cdk-lib/aws-cloudwatch-actions';
import { Policy, PolicyStatement, Role, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import type { ILayerVersion } from 'aws-cdk-lib/aws-lambda';
import type { IBucket } from 'aws-cdk-lib/aws-s3';
import { Schedule, ScheduleExpression } from 'aws-cdk-lib/aws-scheduler';
import { LambdaInvoke } from 'aws-cdk-lib/aws-scheduler-targets';
import { Topic } from 'aws-cdk-lib/aws-sns';
import { EmailSubscription } from 'aws-cdk-lib/aws-sns-subscriptions';
import type { IQueue } from 'aws-cdk-lib/aws-sqs';
import type { IStateMachine } from 'aws-cdk-lib/aws-stepfunctions';
import { Construct } from 'constructs';
import { type CaseInsightsConfig, resourceNames } from '../config';
import { acknowledgeNagFinding } from '../nag';
import { ACCOUNT_LIST_PREFIX } from './account-discovery';
import { acknowledgeExecutions, FAILURE_DELIVERY_PATTERN } from './case-analysis';
import { RUN_COMPLETION_PATTERN, RUN_MARKERS_PREFIX } from './case-collection';
import { handlerDirectory, logicalIdOf, schedulerPrincipal } from './construct-utils';
import { METRICS_NAMESPACE, PipelineFunction } from './pipeline-function';

/** Run Completion checks open runs this often; each check is idempotent. */
export const RUN_COMPLETION_INTERVAL = Duration.hours(1);
/** Account lists older than this are no longer checked; an unfinished run then never completes. */
export const RUN_LOOKBACK_HOURS = 72;
/**
 * The missing-heartbeat alarm fires when no run completed in this many hours: one daily run plus
 * six hours for retrieval, analysis and the hourly completion check.
 */
export const HEARTBEAT_WINDOW_HOURS = 30;
export const RUN_COMPLETION_SCHEDULE_RETRY_ATTEMPTS = 2;
/** Powertools service name of Run Completion; the heartbeat metric's dimension. */
export const RUN_COMPLETION_SERVICE = 'run-completion';
export const HEARTBEAT_METRIC = 'DailyRunCompleted';

const DEFAULT_PERIOD = Duration.minutes(5);

/** A queue whose visible messages need operator attention (DLQs and terminal queues). */
export interface MonitoredQueue {
  /** Short label used in the alarm name and dashboard, for example `SummaryDLQ`. */
  readonly label: string;
  readonly queue: IQueue;
}

export interface ObservabilityProps {
  readonly config: CaseInsightsConfig;
  /** Run-scoped account lists: one per run. */
  readonly accountListBucket: IBucket;
  /** Holds run markers and failure-delivery markers. */
  readonly rawBucket: IBucket;
  readonly commonLayer: ILayerVersion;
  /** The CaseAnalysis workflow. */
  readonly stateMachine: IStateMachine;
  /** Terminal queue that receives reconciled failure records of closed failed executions. */
  readonly failureQueue: IQueue;
  /** Every pipeline function; each gets an error alarm. Run Completion is added automatically. */
  readonly functions: readonly PipelineFunction[];
  readonly deadLetterQueues: readonly MonitoredQueue[];
}

/** One alarm and the runbook heading (alarm name without the deployment prefix) that covers it. */
export interface MonitoredAlarm {
  readonly purpose: string;
  readonly alarm: Alarm;
}

interface AlarmSpec {
  readonly purpose: string;
  readonly description: string;
  readonly metric: IMetric;
  /** Alarm when the value is >= threshold, unless `below` is set. */
  readonly threshold?: number;
  readonly below?: boolean;
  readonly evaluationPeriods?: number;
  readonly treatMissingData?: TreatMissingData;
}

/**
 * Observability: Run Completion, the daily-completion heartbeat, the alarm topic, alarms and the
 * operations dashboard.
 *
 * Run Completion runs hourly and emits `DailyRunCompleted` only when every account and case of a
 * run reached a terminal outcome (see src/lambda/run_completion/app.py); it also delivers missing
 * failure records of closed failed workflow executions. The heartbeat alarm watches that metric,
 * so discovery alone never satisfies it.
 *
 * Telemetry carries counts with the bounded `service` dimension only, never account or case IDs.
 * The alarm topic is not encrypted at rest: CloudWatch alarms can only publish to an encrypted topic
 * through a customer managed KMS key, which this solution does not use. Alarm notifications hold
 * alarm names and metric states, never case content. The topic policy lets CloudWatch publish only
 * for this account's alarms named with the deployment alarm prefix, and denies publishing without TLS.
 */
export class Observability extends Construct {
  public readonly runCompletionFunction: PipelineFunction;
  public readonly runCompletionSchedule: Schedule;
  public readonly alarmTopic: Topic;
  public readonly dashboard: Dashboard;
  private readonly monitored: MonitoredAlarm[] = [];
  /**
   * Rendered by the dashboard's alarm status widget at synthesis, so alarms added later through
   * addAlarm appear there too.
   */
  private readonly statusAlarms: Alarm[] = [];
  private readonly alarmAction: SnsAction;
  private readonly alarmPrefix: string;

  public constructor(scope: Construct, id: string, props: ObservabilityProps) {
    super(scope, id);
    const { config } = props;
    const names = resourceNames(config);
    this.alarmPrefix = names.alarmPrefix;

    this.alarmTopic = new Topic(this, 'AlarmTopic', {
      topicName: names.alarmTopic,
      displayName: `Support Case Insights alarms (${config.uniqueId})`,
      enforceSSL: true,
    });
    acknowledgeNagFinding(
      this.alarmTopic,
      'AwsSolutions-SNS2',
      "Approved exception: CloudWatch alarms cannot publish to a topic encrypted with the AWS managed key alias/aws/sns, and the design uses no customer managed KMS key, so this topic has no at-rest encryption. It enforces TLS and only this deployment's alarms may publish. Notifications carry no case content but do carry operational metadata: the account ID, Region, alarm names, descriptions and states, metric values and resource names.",
    );
    if (config.alarmEmail !== undefined) {
      // The recipient must confirm the subscription before notifications are delivered.
      this.alarmTopic.addSubscription(new EmailSubscription(config.alarmEmail));
    }
    // An alarm action names the topic but does not authorise CloudWatch to publish to it.
    const stack = Stack.of(this);
    this.alarmTopic.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AllowCloudWatchAlarmsToPublish',
        principals: [new ServicePrincipal('cloudwatch.amazonaws.com')],
        actions: ['sns:Publish'],
        resources: [this.alarmTopic.topicArn],
        conditions: {
          ArnLike: {
            'aws:SourceArn': stack.formatArn({
              service: 'cloudwatch',
              resource: 'alarm',
              resourceName: `${this.alarmPrefix}*`,
              arnFormat: ArnFormat.COLON_RESOURCE_NAME,
            }),
          },
          StringEquals: { 'aws:SourceAccount': stack.account },
        },
      }),
    );
    this.alarmAction = new SnsAction(this.alarmTopic);

    this.runCompletionFunction = this.buildRunCompletion(props);
    this.runCompletionSchedule = this.buildRunCompletionSchedule(config);

    for (const fn of [...props.functions, this.runCompletionFunction]) {
      this.alarm({
        purpose: `LambdaErrors-${fn.node.id}`,
        description: `The ${fn.node.id} function reported invocation errors.`,
        metric: fn.function.metricErrors({ period: DEFAULT_PERIOD, statistic: Stats.SUM }),
      });
    }
    const workflowMetric = (metricFn: 'metricFailed' | 'metricTimedOut' | 'metricAborted'): IMetric =>
      props.stateMachine[metricFn]({ period: DEFAULT_PERIOD, statistic: Stats.SUM });
    this.alarm({
      purpose: 'WorkflowExecutionsFailed',
      description: 'CaseAnalysis executions failed after task retries; each should have a failure record on the workflow failure queue.',
      metric: workflowMetric('metricFailed'),
    });
    this.alarm({
      purpose: 'WorkflowExecutionsTimedOut',
      description: 'CaseAnalysis executions hit the workflow timeout, which bypasses task error handling; reconciliation records their failures.',
      metric: workflowMetric('metricTimedOut'),
    });
    this.alarm({
      purpose: 'WorkflowExecutionsAborted',
      description: 'CaseAnalysis executions were stopped; reconciliation records their failures.',
      metric: workflowMetric('metricAborted'),
    });
    for (const { label, queue } of props.deadLetterQueues) {
      this.alarm({
        purpose: `QueueMessages-${label}`,
        description: `Messages are waiting in ${label}; they need investigation and an explicit redrive.`,
        metric: queue.metricApproximateNumberOfMessagesVisible({ period: DEFAULT_PERIOD, statistic: Stats.MAXIMUM }),
      });
    }
    this.alarm({
      purpose: 'WorkflowFailuresRecorded',
      description: 'Failure records were written to the workflow failure queue: exhausted or closed CaseAnalysis attempts, or conversations too large to summarise.',
      metric: sumOf('Failure records', {
        handled: serviceMetric('handle-failure', 'WorkflowFailuresRecorded'),
        oversized: serviceMetric('case-annotation', 'OversizedConversations'),
        cleanup: serviceMetric('case-cleanup', 'FailureDeliveriesSent'),
        completion: serviceMetric(RUN_COMPLETION_SERVICE, 'FailureDeliveriesSent'),
      }),
    });
    this.alarm({
      purpose: 'FailureDeliveryErrors',
      description: 'A failure record could not be written to the workflow failure queue; a pending marker or workflow retry keeps the evidence.',
      metric: sumOf(
        'Failure delivery errors',
        {
          handler: serviceMetric('handle-failure', 'WorkflowFailureDeliveryErrors', Duration.hours(1)),
          cleanup: serviceMetric('case-cleanup', 'FailureDeliveryErrors', Duration.hours(1)),
          completion: serviceMetric(RUN_COMPLETION_SERVICE, 'FailureDeliveryErrors', Duration.hours(1)),
        },
        Duration.hours(1),
      ),
    });
    this.alarm({
      purpose: 'CleanupErrors',
      description: 'Case Cleanup could not evaluate or reset some cases; they are retried on the next daily run.',
      metric: serviceMetric('case-cleanup', 'CleanupErrors', Duration.hours(1)),
    });
    this.alarm({
      purpose: 'RunCompletionErrors',
      description: 'Run Completion could not reconcile some run markers or executions for three consecutive hours.',
      metric: serviceMetric(RUN_COMPLETION_SERVICE, 'RunCompletionErrors', Duration.hours(1)),
      evaluationPeriods: 3,
    });
    this.alarm({
      purpose: 'MissingDailyRunCompletion',
      description: `No daily run completed in the last ${String(HEARTBEAT_WINDOW_HOURS)} hours: discovery, retrieval, analysis or completion reconciliation is stuck or failing.`,
      metric: serviceMetric(RUN_COMPLETION_SERVICE, HEARTBEAT_METRIC, Duration.hours(1)),
      threshold: 1,
      below: true,
      evaluationPeriods: HEARTBEAT_WINDOW_HOURS,
      treatMissingData: TreatMissingData.BREACHING,
    });

    this.dashboard = this.buildDashboard(names.dashboard, props);
  }

  /** Every alarm with its purpose, in creation order. */
  public get alarms(): readonly MonitoredAlarm[] {
    return [...this.monitored];
  }

  /** Adds an alarm that notifies the alarm topic and appears in the dashboard's alarm status. */
  public addAlarm(purpose: string, description: string, metric: IMetric, threshold = 1): Alarm {
    return this.alarm({ purpose, description, metric, threshold });
  }

  private alarm(spec: AlarmSpec): Alarm {
    const evaluationPeriods = spec.evaluationPeriods ?? 1;
    const alarm = new Alarm(this, `${spec.purpose.replace(/[^A-Za-z0-9]/g, '')}Alarm`, {
      alarmName: `${this.alarmPrefix}${spec.purpose}`,
      alarmDescription: `${spec.description} Runbook: docs/runbook.md#${spec.purpose.toLowerCase()}`,
      metric: spec.metric,
      threshold: spec.threshold ?? 1,
      comparisonOperator: spec.below ? ComparisonOperator.LESS_THAN_THRESHOLD : ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      evaluationPeriods,
      datapointsToAlarm: evaluationPeriods,
      treatMissingData: spec.treatMissingData ?? TreatMissingData.NOT_BREACHING,
    });
    alarm.addAlarmAction(this.alarmAction);
    this.monitored.push({ purpose: spec.purpose, alarm });
    this.statusAlarms.push(alarm);
    return alarm;
  }

  private buildRunCompletion(props: ObservabilityProps): PipelineFunction {
    const { config, accountListBucket, rawBucket, failureQueue } = props;
    const names = resourceNames(config);
    const fn = new PipelineFunction(this, 'RunCompletion', {
      config,
      functionName: names.runCompletionFunction,
      description: `Emits the daily heartbeat once a run's accounts and cases are terminal (${config.uniqueId})`,
      handlerDir: handlerDirectory('run_completion'),
      serviceName: RUN_COMPLETION_SERVICE,
      layers: [props.commonLayer],
      timeout: Duration.seconds(config.operations.functions.consumerTimeoutSeconds),
      environment: {
        ACCOUNT_LIST_BUCKET: accountListBucket.bucketName,
        RAW_BUCKET: rawBucket.bucketName,
        STATE_MACHINE_ARN: props.stateMachine.stateMachineArn,
        FAILURE_QUEUE_URL: failureQueue.queueUrl,
        MANAGEMENT_ACCOUNT_ID: config.managementAccountId,
        RUN_LOOKBACK_HOURS: String(RUN_LOOKBACK_HOURS),
      },
    });
    const stack = Stack.of(this);
    const runMarkers = `${RUN_MARKERS_PREFIX}*`;
    const accountLists = `${ACCOUNT_LIST_PREFIX}*`;
    const access = new Policy(this, 'RunCompletionAccess', {
      roles: [fn.role],
      statements: [
        new PolicyStatement({
          sid: 'ListAccountLists',
          actions: ['s3:ListBucket'],
          resources: [accountListBucket.bucketArn],
          conditions: { StringLike: { 's3:prefix': [accountLists] } },
        }),
        new PolicyStatement({ sid: 'ReadAccountLists', actions: ['s3:GetObject'], resources: [accountListBucket.arnForObjects(accountLists)] }),
        // Not prefix-conditioned: a missing failure-delivery marker must read as 404, not 403.
        new PolicyStatement({ sid: 'ListRawBucket', actions: ['s3:ListBucket'], resources: [rawBucket.bucketArn] }),
        new PolicyStatement({ sid: 'ReadRunMarkers', actions: ['s3:GetObject'], resources: [rawBucket.arnForObjects(runMarkers)] }),
        new PolicyStatement({ sid: 'RecordRunCompletion', actions: ['s3:PutObject'], resources: [rawBucket.arnForObjects(RUN_COMPLETION_PATTERN)] }),
        new PolicyStatement({
          sid: 'TrackFailureDeliveries',
          actions: ['s3:GetObject', 's3:PutObject'],
          resources: [rawBucket.arnForObjects(FAILURE_DELIVERY_PATTERN)],
        }),
        new PolicyStatement({
          sid: 'DescribeCaseAnalysis',
          actions: ['states:DescribeExecution'],
          resources: [
            stack.formatArn({
              service: 'states',
              resource: 'execution',
              resourceName: `${names.caseAnalysisStateMachine}:*`,
              arnFormat: ArnFormat.COLON_RESOURCE_NAME,
            }),
          ],
        }),
        new PolicyStatement({ sid: 'SendFailureRecords', actions: ['sqs:SendMessage'], resources: [failureQueue.queueArn] }),
      ],
    });
    const rawId = logicalIdOf(rawBucket);
    acknowledgeNagFinding(
      access,
      `AwsSolutions-IAM5[Resource::<${logicalIdOf(accountListBucket)}.Arn>/${accountLists}]`,
      'Run Completion reads account lists named by run ID at run time; reads are limited to the account-lists/ prefix of the dedicated account-list bucket.',
    );
    acknowledgeNagFinding(
      access,
      `AwsSolutions-IAM5[Resource::<${rawId}.Arn>/${runMarkers}]`,
      'Run Completion reads run markers under the runs/ prefix of the raw bucket; run, account and case IDs in their keys are discovered at run time.',
    );
    acknowledgeNagFinding(
      access,
      `AwsSolutions-IAM5[Resource::<${rawId}.Arn>/${RUN_COMPLETION_PATTERN}]`,
      'Run Completion writes only the completion.json marker of each run (with IfNoneMatch); run IDs are discovered at run time.',
    );
    acknowledgeNagFinding(
      access,
      `AwsSolutions-IAM5[Resource::<${rawId}.Arn>/${FAILURE_DELIVERY_PATTERN}]`,
      'Run Completion preserves and delivers failure-delivery markers of closed failed executions under each case failure-deliveries/ prefix; it cannot delete them.',
    );
    acknowledgeExecutions(
      access,
      names.caseAnalysisStateMachine,
      'Run Completion describes the executions named by run case markers; DescribeExecution is limited to executions of this deployment CaseAnalysis state machine.',
    );
    return fn;
  }

  private buildRunCompletionSchedule(config: CaseInsightsConfig): Schedule {
    const names = resourceNames(config);
    const fn = this.runCompletionFunction.function;
    const role = new Role(this, 'RunCompletionScheduleRole', {
      description: `EventBridge Scheduler role that invokes ${names.runCompletionFunction}`,
      assumedBy: schedulerPrincipal(this),
    });
    acknowledgeNagFinding(
      role,
      `AwsSolutions-IAM5[Resource::<${logicalIdOf(fn)}.Arn>:*]`,
      'The CDK Lambda grant covers the run completion function and its versions or aliases (:*); the role holds only lambda:InvokeFunction on that one function.',
    );
    return new Schedule(this, 'RunCompletionSchedule', {
      scheduleName: names.runCompletionSchedule,
      description: `Hourly Support Case Insights run completion check (${config.uniqueId})`,
      schedule: ScheduleExpression.rate(RUN_COMPLETION_INTERVAL),
      target: new LambdaInvoke(fn, {
        retryAttempts: RUN_COMPLETION_SCHEDULE_RETRY_ATTEMPTS,
        // A missed check is repeated by the next hourly invocation.
        maxEventAge: Duration.minutes(30),
        role,
      }),
    });
  }

  private buildDashboard(dashboardName: string, props: ObservabilityProps): Dashboard {
    const { stateMachine } = props;
    const graph = (title: string, metrics: IMetric[], width = 12): GraphWidget => new GraphWidget({ title, left: metrics, width, height: 6 });
    const sfn = (metricFn: 'metricSucceeded' | 'metricFailed' | 'metricTimedOut' | 'metricAborted' | 'metricStarted', label: string): IMetric =>
      stateMachine[metricFn]({ period: DEFAULT_PERIOD, statistic: Stats.SUM, label });
    const successRate = new MathExpression({
      expression: '100 * succeeded / (succeeded + FILL(failed, 0) + FILL(timedOut, 0) + FILL(aborted, 0))',
      usingMetrics: {
        succeeded: sfn('metricSucceeded', 'Succeeded'),
        failed: sfn('metricFailed', 'Failed'),
        timedOut: sfn('metricTimedOut', 'Timed out'),
        aborted: sfn('metricAborted', 'Aborted'),
      },
      label: 'Workflow success rate (%)',
      period: DEFAULT_PERIOD,
    });
    const allFunctions = [...props.functions, this.runCompletionFunction];
    const dashboard = new Dashboard(this, 'Dashboard', { dashboardName, defaultInterval: Duration.days(7) });
    dashboard.addWidgets(
      new TextWidget({
        markdown: `# Support Case Insights (${props.config.uniqueId})\nCounts only; no account or case content is published. Alarm runbook: docs/runbook.md.`,
        width: 24,
        height: 2,
      }),
    );
    dashboard.addWidgets(
      new SingleValueWidget({
        title: 'Daily runs completed (heartbeat)',
        metrics: [serviceMetric(RUN_COMPLETION_SERVICE, HEARTBEAT_METRIC, Duration.days(1), 'Runs completed')],
        width: 8,
        height: 4,
      }),
      new SingleValueWidget({
        title: 'Workflow success rate',
        metrics: [successRate],
        width: 8,
        height: 4,
      }),
      new AlarmStatusWidget({ title: 'Alarms', alarms: this.statusAlarms, width: 8, height: 4 }),
    );
    dashboard.addWidgets(
      graph('Accounts discovered and skipped', [
        serviceMetric('account-lookup', 'AccountsDiscovered'),
        serviceMetric('account-lookup', 'AccountsExcludedInactive'),
        serviceMetric('account-lookup', 'AccountsExcludedManagement'),
        serviceMetric('case-retrieval', 'AccountsProcessed'),
        serviceMetric('case-retrieval', 'AccountsSkipped'),
      ]),
      graph('Cases retrieved, processed and failed', [
        serviceMetric('case-retrieval', 'CasesRetrieved'),
        serviceMetric('case-annotation', 'CasesAnnotated'),
        serviceMetric('update-case-metadata', 'CasesProcessed'),
        serviceMetric('handle-failure', 'WorkflowFailuresRecorded'),
        serviceMetric('case-annotation', 'OversizedConversations'),
        serviceMetric('case-retrieval', 'CaseRetrievalFailures'),
        serviceMetric('case-annotation', 'CaseAnnotationFailures'),
      ]),
    );
    dashboard.addWidgets(
      new GraphWidget({
        title: 'Bedrock latency (ms)',
        left: [
          serviceMetric('case-analysis-stages', 'BedrockLatency', DEFAULT_PERIOD, 'Average', Stats.AVERAGE),
          serviceMetric('case-analysis-stages', 'BedrockLatency', DEFAULT_PERIOD, 'p90', Stats.p(90)),
        ],
        width: 8,
        height: 6,
      }),
      new GraphWidget({
        title: 'Bedrock tokens',
        left: [serviceMetric('case-analysis-stages', 'BedrockInputTokens'), serviceMetric('case-analysis-stages', 'BedrockOutputTokens')],
        width: 8,
        height: 6,
      }),
      new GraphWidget({
        title: 'Bedrock throttles and failures',
        left: [serviceMetric('case-analysis-stages', 'BedrockThrottles'), serviceMetric('case-analysis-stages', 'BedrockInvocationFailures')],
        width: 8,
        height: 6,
      }),
    );
    dashboard.addWidgets(
      graph('CaseAnalysis executions', [
        sfn('metricStarted', 'Started'),
        sfn('metricSucceeded', 'Succeeded'),
        sfn('metricFailed', 'Failed'),
        sfn('metricTimedOut', 'Timed out'),
        sfn('metricAborted', 'Aborted'),
      ]),
      graph(
        'Dead-letter and terminal queue depth',
        props.deadLetterQueues.map(({ label, queue }) =>
          queue.metricApproximateNumberOfMessagesVisible({ period: DEFAULT_PERIOD, statistic: Stats.MAXIMUM, label }),
        ),
      ),
    );
    dashboard.addWidgets(
      graph('Run completion', [
        serviceMetric(RUN_COMPLETION_SERVICE, HEARTBEAT_METRIC, Duration.hours(1)),
        serviceMetric(RUN_COMPLETION_SERVICE, 'RunsIncomplete', Duration.hours(1), undefined, Stats.MAXIMUM),
        serviceMetric(RUN_COMPLETION_SERVICE, 'RunCasesPending', Duration.hours(1), undefined, Stats.MAXIMUM),
        serviceMetric(RUN_COMPLETION_SERVICE, 'RunCompletionErrors', Duration.hours(1)),
      ]),
      graph('Cleanup and failure-record reconciliation', [
        serviceMetric('case-cleanup', 'CleanupCasesReset', Duration.hours(1)),
        serviceMetric('case-cleanup', 'CleanupErrors', Duration.hours(1)),
        serviceMetric('case-cleanup', 'FailureDeliveriesSent', Duration.hours(1), 'Cleanup deliveries'),
        serviceMetric(RUN_COMPLETION_SERVICE, 'FailureDeliveriesSent', Duration.hours(1), 'Run completion deliveries'),
        serviceMetric('workflow-starter', 'FailureDeliveriesPreserved', Duration.hours(1), 'Starter preserved'),
        serviceMetric('handle-failure', 'WorkflowFailureDeliveryErrors', Duration.hours(1)),
      ]),
    );
    dashboard.addWidgets(
      graph(
        'Lambda errors',
        allFunctions.map((fn) => fn.function.metricErrors({ period: DEFAULT_PERIOD, statistic: Stats.SUM, label: fn.node.id })),
        24,
      ),
    );
    return dashboard;
  }

}

/** A Powertools EMF metric of one function, by its `service` dimension. */
export function serviceMetric(
  service: string,
  metricName: string,
  period: Duration = DEFAULT_PERIOD,
  label?: string,
  statistic: string = Stats.SUM,
): Metric {
  return new Metric({ namespace: METRICS_NAMESPACE, metricName, dimensionsMap: { service }, period, statistic, label });
}

function sumOf(label: string, metrics: Record<string, IMetric>, period: Duration = DEFAULT_PERIOD): MathExpression {
  return new MathExpression({
    expression: `SUM([${Object.keys(metrics)
      .map((id) => `FILL(${id}, 0)`)
      .join(', ')}])`,
    usingMetrics: metrics,
    label,
    period,
  });
}

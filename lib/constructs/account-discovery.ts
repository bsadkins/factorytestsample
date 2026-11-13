import { Duration, Stack, TimeZone } from 'aws-cdk-lib';
import type { ITable } from 'aws-cdk-lib/aws-dynamodb';
import { Policy, PolicyStatement, Role, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import type { ILayerVersion } from 'aws-cdk-lib/aws-lambda';
import { SqsDestination } from 'aws-cdk-lib/aws-lambda-destinations';
import { EventType, type IBucket } from 'aws-cdk-lib/aws-s3';
import { LambdaDestination } from 'aws-cdk-lib/aws-s3-notifications';
import { ContextAttribute, Schedule, ScheduleExpression, ScheduleTargetInput } from 'aws-cdk-lib/aws-scheduler';
import { LambdaInvoke } from 'aws-cdk-lib/aws-scheduler-targets';
import type { IQueue } from 'aws-cdk-lib/aws-sqs';
import { Construct, type IConstruct } from 'constructs';
import { type CaseInsightsConfig, resourceNames } from '../config';
import { acknowledgeNagFinding } from '../nag';
import { handlerDirectory, logicalIdOf } from './construct-utils';
import { PipelineFunction } from './pipeline-function';

/** Key prefix of run-scoped account lists; must equal ACCOUNT_LIST_PREFIX in common/account_list.py. */
export const ACCOUNT_LIST_PREFIX = 'account-lists/';
export const ACCOUNT_LIST_SUFFIX = '.json';

/** Lambda's asynchronous retries before an event goes to the discovery failure queue. */
export const DISCOVERY_ASYNC_RETRY_ATTEMPTS = 2;
/** Scheduler retries delivering the daily invocation before using the failure queue. */
export const DISCOVERY_SCHEDULE_RETRY_ATTEMPTS = 3;
/** Events older than this are not retried; the next daily schedule starts a fresh run. */
export const DISCOVERY_MAX_EVENT_AGE = Duration.hours(6);

export interface AccountDiscoveryProps {
  readonly config: CaseInsightsConfig;
  readonly accountListBucket: IBucket;
  /** Powertools idempotency table used by the account reader. */
  readonly idempotencyTable: ITable;
  /** Receives one `{accountId, accountName, runId}` message per selected account. */
  readonly activeAccountsQueue: IQueue;
  /** Terminal queue for schedule deliveries and asynchronous invocations that exhaust retries. */
  readonly failureQueue: IQueue;
  readonly commonLayer: ILayerVersion;
}

/**
 * Scheduled account discovery: an EventBridge Scheduler schedule (default 00:00 UTC) invokes
 * Account Lookup, which writes `account-lists/<runId>.json`; the S3 object notification invokes
 * Account Reader, which enqueues each account on the active-accounts queue.
 */
export class AccountDiscovery extends Construct {
  public readonly lookupFunction: PipelineFunction;
  public readonly readerFunction: PipelineFunction;
  public readonly schedule: Schedule;

  public constructor(scope: Construct, id: string, props: AccountDiscoveryProps) {
    super(scope, id);
    const { config, accountListBucket, idempotencyTable, activeAccountsQueue, failureQueue, commonLayer } = props;
    const names = resourceNames(config);
    const timeout = Duration.seconds(config.operations.functions.consumerTimeoutSeconds);
    const accountListObjects = accountListBucket.arnForObjects(`${ACCOUNT_LIST_PREFIX}*`);
    const asyncInvokeOptions = {
      retryAttempts: DISCOVERY_ASYNC_RETRY_ATTEMPTS,
      maxEventAge: DISCOVERY_MAX_EVENT_AGE,
      onFailure: new SqsDestination(failureQueue),
    };

    this.lookupFunction = new PipelineFunction(this, 'AccountLookup', {
      config,
      functionName: names.accountLookupFunction,
      description: `Lists active member accounts and writes the run account list (${config.uniqueId})`,
      handlerDir: handlerDirectory('account_lookup'),
      serviceName: 'account-lookup',
      layers: [commonLayer],
      timeout,
      environment: {
        ACCOUNT_LIST_BUCKET: accountListBucket.bucketName,
        MANAGEMENT_ACCOUNT_ID: config.managementAccountId,
      },
    });
    this.lookupFunction.function.configureAsyncInvoke(asyncInvokeOptions);

    const lookupAccess = new Policy(this, 'AccountLookupAccess', {
      roles: [this.lookupFunction.role],
      statements: [
        new PolicyStatement({
          sid: 'ListOrganizationAccounts',
          actions: ['organizations:ListAccounts'],
          resources: ['*'],
        }),
        new PolicyStatement({
          sid: 'WriteAccountLists',
          actions: ['s3:PutObject'],
          resources: [accountListObjects],
        }),
      ],
    });
    acknowledgeNagFinding(
      lookupAccess,
      'AwsSolutions-IAM5[Resource::*]',
      'organizations:ListAccounts does not support resource-level permissions; this statement grants only that read-only action in the management account.',
    );
    acknowledgeNagFinding(
      lookupAccess,
      `AwsSolutions-IAM5[Resource::<${logicalIdOf(accountListBucket)}.Arn>/${ACCOUNT_LIST_PREFIX}*]`,
      'Account lists are named by run ID at run time; s3:PutObject is limited to the account-lists/ prefix of the dedicated account-list bucket.',
    );

    this.readerFunction = new PipelineFunction(this, 'AccountReader', {
      config,
      functionName: names.accountReaderFunction,
      description: `Enqueues one active-account reference per account in a new account list (${config.uniqueId})`,
      handlerDir: handlerDirectory('account_reader'),
      serviceName: 'account-reader',
      layers: [commonLayer],
      timeout,
      environment: {
        ACCOUNT_LIST_BUCKET: accountListBucket.bucketName,
        ACTIVE_ACCOUNTS_QUEUE_URL: activeAccountsQueue.queueUrl,
        IDEMPOTENCY_TABLE_NAME: idempotencyTable.tableName,
        MANAGEMENT_ACCOUNT_ID: config.managementAccountId,
      },
    });
    this.readerFunction.function.configureAsyncInvoke(asyncInvokeOptions);

    const readerAccess = new Policy(this, 'AccountReaderAccess', {
      roles: [this.readerFunction.role],
      statements: [
        new PolicyStatement({
          sid: 'ReadAccountLists',
          actions: ['s3:GetObject', 's3:GetObjectVersion'],
          resources: [accountListObjects],
        }),
        new PolicyStatement({
          sid: 'SendAccountReferences',
          actions: ['sqs:SendMessage'],
          resources: [activeAccountsQueue.queueArn],
        }),
        new PolicyStatement({
          sid: 'IdempotencyRecords',
          actions: ['dynamodb:DeleteItem', 'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem'],
          resources: [idempotencyTable.tableArn],
        }),
      ],
    });
    acknowledgeNagFinding(
      readerAccess,
      `AwsSolutions-IAM5[Resource::<${logicalIdOf(accountListBucket)}.Arn>/${ACCOUNT_LIST_PREFIX}*]`,
      'Account lists are named by run ID at run time; object reads are limited to the account-lists/ prefix of the dedicated account-list bucket.',
    );

    accountListBucket.addEventNotification(EventType.OBJECT_CREATED, new LambdaDestination(this.readerFunction.function), {
      prefix: ACCOUNT_LIST_PREFIX,
      suffix: ACCOUNT_LIST_SUFFIX,
    });
    acknowledgeNagFinding(
      bucketNotificationsHandlerRole(accountListBucket),
      'AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]',
      'CDK-managed custom resource that applies the bucket notification configuration at deployment; the AWS managed basic execution policy only allows it to write its own CloudWatch Logs.',
    );

    const stack = Stack.of(this);
    const scheduleRole = new Role(this, 'AccountLookupScheduleRole', {
      description: `EventBridge Scheduler role that invokes ${names.accountLookupFunction}`,
      assumedBy: new ServicePrincipal('scheduler.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': stack.account },
          ArnLike: {
            'aws:SourceArn': stack.formatArn({
              service: 'scheduler',
              resource: 'schedule',
              resourceName: `default/${names.accountLookupSchedule}`,
            }),
          },
        },
      }),
    });
    acknowledgeNagFinding(
      scheduleRole,
      `AwsSolutions-IAM5[Resource::<${logicalIdOf(this.lookupFunction.function)}.Arn>:*]`,
      'The CDK Lambda grant covers the account lookup function and its versions or aliases (:*); the role holds only lambda:InvokeFunction on that one function plus sending failed deliveries to the discovery failure queue.',
    );

    this.schedule = new Schedule(this, 'AccountLookupSchedule', {
      scheduleName: names.accountLookupSchedule,
      description: `Daily Support Case Insights account discovery (${config.uniqueId})`,
      schedule: ScheduleExpression.expression(config.schedules.accountLookup, TimeZone.ETC_UTC),
      target: new LambdaInvoke(this.lookupFunction.function, {
        // The schedule time gives each scheduled run a deterministic run ID, so a repeated
        // delivery for the same schedule cannot start a second run.
        input: ScheduleTargetInput.fromObject({ scheduledTime: ContextAttribute.scheduledTime }),
        retryAttempts: DISCOVERY_SCHEDULE_RETRY_ATTEMPTS,
        maxEventAge: DISCOVERY_MAX_EVENT_AGE,
        deadLetterQueue: failureQueue,
        role: scheduleRole,
      }),
    });
  }
}

/** Role of the stack-level singleton function CDK uses to configure S3 bucket notifications. */
function bucketNotificationsHandlerRole(bucket: IBucket): IConstruct {
  const handler = Stack.of(bucket).node.children.find((child) => child.node.id.startsWith('BucketNotificationsHandler'));
  const role = handler?.node.tryFindChild('Role');
  if (role === undefined) {
    throw new Error('CDK bucket notifications handler role not found');
  }
  return role;
}

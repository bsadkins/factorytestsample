import * as fs from 'node:fs';
import * as path from 'node:path';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { ACCOUNT_LIST_PREFIX, addCaseInsightsStacks, CaseInsightsCoreStack, REPO_ROOT, resourceNames } from '../../lib';
import { acknowledgedRules, iamPolicyStatements, nagApp, schedulerTrustStatement, synthError, testApp, testConfig, trustStatements } from '../helpers/cdk';

function coreTemplate(overrides: Parameters<typeof testConfig>[0] = {}): { core: CaseInsightsCoreStack; template: Template } {
  const config = testConfig(overrides);
  const app = testApp();
  const core = new CaseInsightsCoreStack(app, 'Core', { config, env: { account: config.managementAccountId, region: config.region } });
  return { core, template: Template.fromStack(core) };
}

function logicalId(core: CaseInsightsCoreStack, construct: { node: { defaultChild?: unknown } }): string {
  return core.getLogicalId(construct.node.defaultChild as never);
}

describe('AccountDiscovery', () => {
  test('a daily 00:00 UTC schedule invokes account lookup with the schedule time', () => {
    const { core, template } = coreTemplate();
    const names = resourceNames(core.config);
    const discovery = core.accountDiscovery;
    expect(Object.keys(template.findResources('AWS::Scheduler::Schedule', { Properties: { Name: names.accountLookupSchedule } }))).toHaveLength(1);
    template.hasResourceProperties('AWS::Scheduler::Schedule', {
      Name: names.accountLookupSchedule,
      ScheduleExpression: 'cron(0 0 * * ? *)',
      ScheduleExpressionTimezone: 'Etc/UTC',
      State: 'ENABLED',
      FlexibleTimeWindow: { Mode: 'OFF' },
      Target: {
        Arn: { 'Fn::GetAtt': [logicalId(core, discovery.lookupFunction.function), 'Arn'] },
        Input: JSON.stringify({ scheduledTime: '<aws.scheduler.scheduled-time>' }),
        RetryPolicy: { MaximumRetryAttempts: 3, MaximumEventAgeInSeconds: 21600 },
        DeadLetterConfig: { Arn: { 'Fn::GetAtt': [logicalId(core, core.queues.accountDiscoveryFailures), 'Arn'] } },
      },
    });
  });

  test('the schedule expression is configurable', () => {
    const { template } = coreTemplate({ schedules: { accountLookup: 'cron(30 2 * * ? *)' } });
    template.hasResourceProperties('AWS::Scheduler::Schedule', {
      ScheduleExpression: 'cron(30 2 * * ? *)',
      ScheduleExpressionTimezone: 'Etc/UTC',
    });
  });

  test('the schedule role trusts only Scheduler in the default schedule group of this account and only invokes account lookup', () => {
    const { core, template } = coreTemplate();
    const names = resourceNames(core.config);
    const discovery = core.accountDiscovery;
    const lookupArn = { 'Fn::GetAtt': [logicalId(core, discovery.lookupFunction.function), 'Arn'] };
    const scheduleRoleId = logicalId(core, core.node.findChild('AccountDiscovery').node.findChild('AccountLookupScheduleRole'));
    // Scheduler sends the schedule group ARN as aws:SourceArn, so the role trusts that group.
    expect(trustStatements(template, scheduleRoleId)).toEqual([schedulerTrustStatement(core.config)]);
    template.hasResourceProperties('AWS::Scheduler::Schedule', {
      Name: names.accountLookupSchedule,
      GroupName: Match.absent(),
      Target: Match.objectLike({ RoleArn: { 'Fn::GetAtt': [scheduleRoleId, 'Arn'] } }),
    });
    const statements = iamPolicyStatements(template)
      .filter(({ roles }) => JSON.stringify(roles) === JSON.stringify([{ Ref: scheduleRoleId }]))
      .map(({ statement }) => statement);
    expect(statements).toEqual([
      { Action: 'lambda:InvokeFunction', Effect: 'Allow', Resource: [lookupArn, { 'Fn::Join': ['', [lookupArn, ':*']] }] },
      { Action: 'sqs:SendMessage', Effect: 'Allow', Resource: { 'Fn::GetAtt': [logicalId(core, core.queues.accountDiscoveryFailures), 'Arn'] } },
    ]);
  });

  test('functions are named, configured and retry asynchronously into the discovery failure queue', () => {
    const { core, template } = coreTemplate();
    const names = resourceNames(core.config);
    const bucketRef = { Ref: logicalId(core, core.storage.accountListBucket) };
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: names.accountLookupFunction,
      Handler: 'app.handler',
      Timeout: core.config.operations.functions.consumerTimeoutSeconds,
      Environment: {
        Variables: Match.objectLike({
          ACCOUNT_LIST_BUCKET: bucketRef,
          MANAGEMENT_ACCOUNT_ID: core.config.managementAccountId,
          POWERTOOLS_SERVICE_NAME: 'account-lookup',
        }),
      },
      Layers: [{ Ref: logicalId(core, core.layers.commonLayer) }],
    });
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: names.accountReaderFunction,
      Environment: {
        Variables: Match.objectLike({
          ACCOUNT_LIST_BUCKET: bucketRef,
          ACTIVE_ACCOUNTS_QUEUE_URL: { Ref: logicalId(core, core.queues.activeAccounts.queue) },
          IDEMPOTENCY_TABLE_NAME: { Ref: logicalId(core, core.storage.idempotencyTable) },
          MANAGEMENT_ACCOUNT_ID: core.config.managementAccountId,
          POWERTOOLS_SERVICE_NAME: 'account-reader',
        }),
      },
    });
    const failureQueueArn = { 'Fn::GetAtt': [logicalId(core, core.queues.accountDiscoveryFailures), 'Arn'] };
    for (const fn of [core.accountDiscovery.lookupFunction, core.accountDiscovery.readerFunction]) {
      template.hasResourceProperties('AWS::Lambda::EventInvokeConfig', {
        FunctionName: { Ref: logicalId(core, fn.function) },
        MaximumRetryAttempts: 2,
        MaximumEventAgeInSeconds: 21600,
        DestinationConfig: { OnFailure: { Destination: failureQueueArn } },
      });
    }
  });

  test('only account-list objects created in the account-list bucket notify the reader', () => {
    const { core, template } = coreTemplate();
    const readerArn = { 'Fn::GetAtt': [logicalId(core, core.accountDiscovery.readerFunction.function), 'Arn'] };
    const bucketId = logicalId(core, core.storage.accountListBucket);
    template.resourceCountIs('Custom::S3BucketNotifications', 1);
    template.hasResourceProperties('Custom::S3BucketNotifications', {
      BucketName: { Ref: bucketId },
      NotificationConfiguration: {
        LambdaFunctionConfigurations: [
          {
            Events: ['s3:ObjectCreated:*'],
            Filter: {
              Key: {
                FilterRules: [
                  { Name: 'suffix', Value: '.json' },
                  { Name: 'prefix', Value: 'account-lists/' },
                ],
              },
            },
            LambdaFunctionArn: readerArn,
          },
        ],
      },
    });
    template.hasResourceProperties('AWS::Lambda::Permission', {
      Action: 'lambda:InvokeFunction',
      FunctionName: readerArn,
      Principal: 's3.amazonaws.com',
      SourceAccount: core.config.managementAccountId,
      SourceArn: { 'Fn::GetAtt': [bucketId, 'Arn'] },
    });
  });

  test('each function role holds only its own focused permissions', () => {
    const { core, template } = coreTemplate();
    const { lookupFunction, readerFunction } = core.accountDiscovery;
    const listObjects = { 'Fn::Join': ['', [{ 'Fn::GetAtt': [logicalId(core, core.storage.accountListBucket), 'Arn'] }, '/account-lists/*']] };
    const failureQueueArn = { 'Fn::GetAtt': [logicalId(core, core.queues.accountDiscoveryFailures), 'Arn'] };
    const destinationGrant = {
      Action: ['sqs:GetQueueAttributes', 'sqs:GetQueueUrl', 'sqs:SendMessage'],
      Effect: 'Allow',
      Resource: failureQueueArn,
    };
    const statementsFor = (roleId: string): unknown[] =>
      iamPolicyStatements(template)
        .filter(({ roles }) => JSON.stringify(roles) === JSON.stringify([{ Ref: roleId }]))
        .map(({ statement }) => statement)
        .filter((statement) => statement.Sid !== 'WriteOwnLogGroup' && statement.Sid !== 'WriteXRaySegments');

    expect(statementsFor(logicalId(core, lookupFunction.role))).toEqual([
      destinationGrant,
      { Sid: 'ListOrganizationAccounts', Effect: 'Allow', Action: 'organizations:ListAccounts', Resource: '*' },
      { Sid: 'WriteAccountLists', Effect: 'Allow', Action: 's3:PutObject', Resource: listObjects },
    ]);
    expect(statementsFor(logicalId(core, readerFunction.role))).toEqual([
      destinationGrant,
      { Sid: 'ReadAccountLists', Effect: 'Allow', Action: ['s3:GetObject', 's3:GetObjectVersion'], Resource: listObjects },
      {
        Sid: 'SendAccountReferences',
        Effect: 'Allow',
        Action: 'sqs:SendMessage',
        Resource: { 'Fn::GetAtt': [logicalId(core, core.queues.activeAccounts.queue), 'Arn'] },
      },
      {
        Sid: 'IdempotencyRecords',
        Effect: 'Allow',
        Action: ['dynamodb:DeleteItem', 'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem'],
        Resource: { 'Fn::GetAtt': [logicalId(core, core.storage.idempotencyTable), 'Arn'] },
      },
    ]);
  });

  test('passes cdk-nag with specific acknowledgements only', () => {
    const app = nagApp();
    const { core } = addCaseInsightsStacks(app, { env: 'dev' });
    expect(synthError(app)).toBeUndefined();
    const discovery = acknowledgedRules(core.accountDiscovery).map(({ path: constructPath, id }) => [
      constructPath.replace(`${core.node.id}/`, ''),
      id.replace(/^.*::(?=AwsSolutions-)/, ''),
    ]);
    const bucketId = core.getLogicalId(core.storage.accountListBucket.node.defaultChild as never);
    const lookupId = core.getLogicalId(core.accountDiscovery.lookupFunction.function.node.defaultChild as never);
    expect(discovery).toEqual(
      expect.arrayContaining([
        ['AccountDiscovery/AccountLookupAccess', 'AwsSolutions-IAM5[Resource::*]'],
        ['AccountDiscovery/AccountLookupAccess', `AwsSolutions-IAM5[Resource::<${bucketId}.Arn>/account-lists/*]`],
        ['AccountDiscovery/AccountReaderAccess', `AwsSolutions-IAM5[Resource::<${bucketId}.Arn>/account-lists/*]`],
        ['AccountDiscovery/AccountLookupScheduleRole', `AwsSolutions-IAM5[Resource::<${lookupId}.Arn>:*]`],
      ]),
    );
  });

  test('the account-list prefix matches the Python handlers', () => {
    const source = fs.readFileSync(path.join(REPO_ROOT, 'src', 'lambda', 'common', 'account_list.py'), 'utf8');
    expect(source).toContain(`ACCOUNT_LIST_PREFIX: Final = "${ACCOUNT_LIST_PREFIX}"`);
  });
});

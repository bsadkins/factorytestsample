# Permissions

This guide describes every IAM permission involved in Support Case Insights: what an administrator must set up once, what the person or pipeline that deploys needs, every role the stacks create with its trust policy, statements and the reason for each, the member account role, the resource policies, the permissions operators and analysts need, and how cdk-nag findings are acknowledged. The policies are taken from the synthesised templates in `cdk.out/dev` (Haiku model) and the CDK code; nothing has been deployed, so the deployer policy in particular is untested. The data protection design is in [security](security.md), and the resources the roles act on are in [resources](resources.md).

## Principals at a glance

| Principal | Where | Purpose |
| --- | --- | --- |
| Administrator | Management account | One-time setup: CDK bootstrap, StackSets trusted access, Bedrock model access, API Gateway logging ownership |
| Deployer | Management account | Runs `npx cdk diff`, `deploy` and `destroy` through the CDK bootstrap roles |
| CDK bootstrap roles | Management account | Publish assets and run CloudFormation on the deployer's behalf |
| Lambda, Step Functions, Scheduler, Glue and API Gateway service roles | Management account | Run the application; one role per function or service |
| `Support-Case-Analysis-Role-<uniqueId>` | Every targeted member account | Read-only access to AWS Support cases, assumed by two collector roles |
| Operators | Management account | Monitor, respond to alarms and run the recovery tools |
| Analysts | Management account | Query with Athena, export CSV files or call the MCP endpoint |

## One-time setup by an administrator

These steps need broad permissions in the management account and are not performed by the application.

1. **CDK bootstrap.** Run once per account and Region:

   ```bash
   npx cdk bootstrap aws://<account-id>/<region>
   ```

   This creates the `CDKToolkit` stack with the default qualifier `hnb659fds` and the roles `cdk-hnb659fds-deploy-role-<account-id>-<region>`, `cdk-hnb659fds-file-publishing-role-<account-id>-<region>`, `cdk-hnb659fds-lookup-role-<account-id>-<region>`, `cdk-hnb659fds-image-publishing-role-<account-id>-<region>` and `cdk-hnb659fds-cfn-exec-role-<account-id>-<region>`. The application does not use the image publishing role, because it has no container assets. By default the CloudFormation execution role has `AdministratorAccess`; see [CloudFormation execution role](#cloudformation-execution-role).

2. **StackSets trusted access.** The member role StackSet is service-managed, so CloudFormation StackSets must have trusted access to AWS Organizations. Activate it from the management account (or in the CloudFormation console under StackSets) and check the result:

   ```bash
   aws organizations enable-aws-service-access --service-principal member.org.stacksets.cloudformation.amazonaws.com
   aws cloudformation activate-organizations-access --region <region>
   aws cloudformation describe-organizations-access --region <region>
   ```

   The StackSet is called as `SELF`, so the stacks must be deployed from the management account itself, not from a delegated administrator account.

3. **Amazon Bedrock model access.** Anthropic models can require a one-time first-time-use (FTU) form and an AWS Marketplace subscription with a valid payment method on the account. IAM cannot complete or waive either. The function roles hold `aws-marketplace:Subscribe` for the configured model's product only, so that the first invocation can complete the automatic subscription; the FTU form must still be submitted, for example in the Amazon Bedrock console.

4. **API Gateway logging ownership** (only with MCP). Decide between managed and existing mode as described in [Shared API Gateway logging prerequisite](mcp-server-deployment.md#shared-api-gateway-logging-prerequisite).

## Deployer

The CDK CLI deploys by assuming the bootstrap roles, so the deployer's own policy can be small. The example below is a starting point that has **not been tested** against a real deployment; extend it if a command reports an access denied error. Replace `<account-id>` and `<region>`.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AssumeCdkBootstrapRoles",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": [
        "arn:aws:iam::<account-id>:role/cdk-hnb659fds-deploy-role-<account-id>-<region>",
        "arn:aws:iam::<account-id>:role/cdk-hnb659fds-file-publishing-role-<account-id>-<region>",
        "arn:aws:iam::<account-id>:role/cdk-hnb659fds-lookup-role-<account-id>-<region>"
      ]
    },
    {
      "Sid": "PreDeploymentAndTeardownChecks",
      "Effect": "Allow",
      "Action": [
        "organizations:DescribeOrganization",
        "organizations:ListRoots",
        "organizations:ListOrganizationalUnitsForParent",
        "cloudformation:DescribeStacks",
        "cloudformation:DescribeStackResources",
        "cloudformation:DescribeStackSet",
        "cloudformation:ListStackInstances",
        "cloudformation:DescribeOrganizationsAccess"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ReadApiGatewayAccountSetting",
      "Effect": "Allow",
      "Action": "apigateway:GET",
      "Resource": "arn:aws:apigateway:<region>::/account"
    }
  ]
}
```

- The deploy role runs CloudFormation change sets and reads the bootstrap version; the file publishing role uploads the Lambda code and layer assets to the bootstrap assets bucket; the lookup role is used by some CDK commands, such as `cdk diff`, to read deployed templates. The app performs no context lookups.
- The second and third statements cover the read-only checks in the documentation: confirming the organisation, root and OU IDs for `orgId` and `memberRoleTargets`, reading stack outputs, confirming StackSet instances and reading the API Gateway account setting. They are not needed by the CDK itself.
- Teardown of retained prod resources (emptying buckets, deleting the table and log groups) is done with the deployer's own credentials and needs further permissions; see [Teardown](resources.md#teardown).
- No KMS permissions are needed.

### CloudFormation execution role

CloudFormation creates and deletes every resource with `cdk-hnb659fds-cfn-exec-role-<account-id>-<region>`, which has `AdministratorAccess` unless bootstrap was run with `--cloudformation-execution-policies`. A narrower execution policy must still allow full lifecycle management of every resource type in [resources](resources.md): Amazon S3 buckets, bucket policies and notifications; Amazon SQS queues and queue policies; AWS Lambda functions, layers, permissions, event source mappings and event invoke configurations; IAM roles and policies, including `iam:PassRole` for the service roles; Amazon DynamoDB; AWS Step Functions; Amazon EventBridge Scheduler; Amazon SNS topics and subscriptions; Amazon CloudWatch alarms and dashboards; Amazon CloudWatch Logs log groups, metric filters and Step Functions log delivery; AWS CloudFormation service-managed StackSets; AWS Glue; Amazon Athena; Amazon API Gateway, including the account setting; and AWS WAF. A narrowed policy has not been written or tested for this application. If you narrow it, review the policy with the change sets that `npx cdk diff --all -c env=<env>` shows and with AWS CloudTrail, and keep it in step with future changes.

## Runtime roles

Each role is created by the stacks with a generated name, except the two collector roles, which have fixed names because the member role trusts them by ARN. In the ARNs below, `<raw>`, `<processed>` and `<account-list>` stand for the generated bucket names.

### Common to every function role

Every function role is trusted by `lambda.amazonaws.com` (without conditions) and has a telemetry policy with:

- `logs:CreateLogStream` and `logs:PutLogEvents` on the function's own log group only. There is no `logs:CreateLogGroup`, because each log group is created by the stack with its retention;
- `xray:PutTraceSegments` and `xray:PutTelemetryRecords` on `*`, because these actions do not support resource-level permissions.

### Account discovery

**Account Lookup role.** Lists the organisation's accounts and starts a run.

| Actions | Resource | Why |
| --- | --- | --- |
| `organizations:ListAccounts` | `*` | Discover member accounts. The action has no resource-level permissions and is read-only |
| `s3:PutObject` | `arn:aws:s3:::<account-list>/account-lists/*` | Write the run's account list, named by run ID |
| `sqs:SendMessage`, `sqs:GetQueueAttributes`, `sqs:GetQueueUrl` | `CaseInsights-AccountDiscoveryFailures-<uniqueId>` | On-failure destination of the asynchronous invocation |

**Account Reader role.** Turns an account list into one message per active account.

| Actions | Resource | Why |
| --- | --- | --- |
| `s3:GetObject`, `s3:GetObjectVersion` | `arn:aws:s3:::<account-list>/account-lists/*` | Read the account list that triggered it |
| `sqs:SendMessage` | `CaseInsights-ActiveAccounts-<uniqueId>` | Queue each account |
| `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `DeleteItem` | Idempotency table | Powertools idempotency |
| `sqs:SendMessage`, `sqs:GetQueueAttributes`, `sqs:GetQueueUrl` | `CaseInsights-AccountDiscoveryFailures-<uniqueId>` | On-failure destination |

### Case collection

**`CaseInsights-CaseRetrieval-<uniqueId>`.** Assumes the member role and lists resolved cases.

| Actions | Resource | Why |
| --- | --- | --- |
| `sqs:ReceiveMessage`, `DeleteMessage`, `ChangeMessageVisibility`, `GetQueueAttributes`, `GetQueueUrl` | `CaseInsights-ActiveAccounts-<uniqueId>` | Consume account messages |
| `sts:AssumeRole` | `arn:aws:iam::*:role/Support-Case-Analysis-Role-<uniqueId>`, condition `aws:ResourceOrgID` equals `orgId` | Member account IDs are discovered at run time, so the account is a wildcard; the role name is fixed and the condition limits targets to the organisation |
| `s3:PutObject` | `arn:aws:s3:::<raw>/account_number=*` | Store case metadata (`data.json`) |
| `s3:GetObject`, `s3:PutObject` | `arn:aws:s3:::<raw>/runs/*/accounts/*` | Write, and re-read on retry, the account's run marker |
| `s3:ListBucket` | Raw bucket, prefix `runs/*/accounts/*` | Tell an absent marker from a denied read |
| `s3:ListBucket` | Processed bucket, prefix `account_number=*` | Skip cases that are already processed |
| `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `DeleteItem` | Idempotency table | Powertools idempotency |
| `sqs:SendMessage` | `CaseInsights-CaseAnnotation-<uniqueId>` | Queue each case for annotation |

**`CaseInsights-CaseAnnotation-<uniqueId>`.** Assumes the member role and downloads conversations.

| Actions | Resource | Why |
| --- | --- | --- |
| `sqs:ReceiveMessage`, `DeleteMessage`, `ChangeMessageVisibility`, `GetQueueAttributes`, `GetQueueUrl` | `CaseInsights-CaseAnnotation-<uniqueId>` | Consume case messages |
| `sts:AssumeRole` | `arn:aws:iam::*:role/Support-Case-Analysis-Role-<uniqueId>`, condition `aws:ResourceOrgID` equals `orgId` | As for retrieval |
| `s3:PutObject` | `arn:aws:s3:::<raw>/account_number=*` | Store the conversation (`communications.json`) and the terminal-failure marker |
| `s3:GetObject` | `arn:aws:s3:::<raw>/account_number=*/case_number=*/terminal-failure.json` | Avoid re-reporting an unchanged oversized conversation |
| `s3:ListBucket` | Raw bucket, prefix `account_number=*/case_number=*/terminal-failure.json` | Check for that marker |
| `s3:PutObject` | `arn:aws:s3:::<raw>/runs/*/cases/*` | Record cases whose work ends at annotation |
| `s3:ListBucket` | Processed bucket, prefix `account_number=*` | Skip processed cases |
| `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `DeleteItem` | Idempotency table | Powertools idempotency |
| `sqs:SendMessage` | `CaseInsights-CaseSummary-<uniqueId>`, `CaseInsights-WorkflowFailures-<uniqueId>` | Hand the case to the workflow starter, or record a terminal failure |

### Case analysis

**Workflow Starter role.** Claims a case and starts its execution.

| Actions | Resource | Why |
| --- | --- | --- |
| `sqs:ReceiveMessage`, `DeleteMessage`, `ChangeMessageVisibility`, `GetQueueAttributes`, `GetQueueUrl` | `CaseInsights-CaseSummary-<uniqueId>` | Consume summary messages |
| `s3:GetObject`, `s3:PutObject` | `arn:aws:s3:::<raw>/account_number=*/case_number=*/workflow.json` | Read and write the case ownership marker |
| `s3:GetObject`, `s3:PutObject` | `arn:aws:s3:::<raw>/account_number=*/case_number=*/failure-deliveries/*` | Preserve a pending failure delivery before replacing a failed execution's claim |
| `s3:PutObject` | `arn:aws:s3:::<raw>/runs/*/cases/*` | Record which execution owns the case in the run |
| `s3:GetObject` | `arn:aws:s3:::<processed>/account_number=*` | Check (HEAD) for a processed record so completed cases never start again |
| `s3:ListBucket` | Raw and processed buckets | Distinguish missing objects from denied reads |
| `states:StartExecution` | `CaseAnalysis-<uniqueId>` | Start the workflow |
| `states:DescribeExecution` | `arn:aws:states:<region>:<account-id>:execution:CaseAnalysis-<uniqueId>:*` | Check the execution named by an existing claim |
| `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `DeleteItem` | Idempotency table | Powertools idempotency |

**Case Analysis Stages role.** Runs the Summary, RCA and Lifecycle Bedrock stages.

| Actions | Resource | Why |
| --- | --- | --- |
| `s3:GetObject`, `s3:GetObjectVersion` | `arn:aws:s3:::<raw>/account_number=*` | Read the case metadata and conversation |
| `s3:PutObject` | `arn:aws:s3:::<raw>/account_number=*/case_number=*/checkpoints/*` | Store stage checkpoints |
| `s3:GetObject` | `arn:aws:s3:::<processed>/account_number=*` | Stop before inference once a case is committed |
| `s3:ListBucket` | Raw and processed buckets | Distinguish missing objects from denied reads |
| Bedrock policy | See [Bedrock policy](#bedrock-policy) | Invoke only the configured model |

**Update Case Metadata role.** Writes the processed record.

| Actions | Resource | Why |
| --- | --- | --- |
| `s3:GetObject`, `s3:GetObjectVersion` | `arn:aws:s3:::<raw>/account_number=*` | Read the metadata and stage checkpoints |
| `s3:GetObject`, `s3:PutObject` | `arn:aws:s3:::<processed>/account_number=*` | Conditionally create the processed record |
| `s3:ListBucket` | Raw and processed buckets | Distinguish missing objects from denied reads |

**Handle Failure role.** Records a failed execution.

| Actions | Resource | Why |
| --- | --- | --- |
| `sqs:SendMessage` | `CaseInsights-WorkflowFailures-<uniqueId>` | Send the failure record |
| `s3:GetObject`, `s3:PutObject` | `arn:aws:s3:::<raw>/account_number=*/case_number=*/failure-deliveries/*` | Read and confirm the failure-delivery marker |
| `s3:ListBucket` | Raw bucket | Distinguish missing objects from denied reads |

**State machine role.** Trusted by `states.amazonaws.com` with `aws:SourceAccount` equal to the account and `aws:SourceArn` like `arn:aws:states:<region>:<account-id>:stateMachine:*`.

| Actions | Resource | Why |
| --- | --- | --- |
| `lambda:InvokeFunction` | The Case Analysis Stages, Update Case Metadata and Handle Failure functions and their versions (`:*`) | Run the workflow tasks |
| `logs:CreateLogDelivery`, `GetLogDelivery`, `UpdateLogDelivery`, `DeleteLogDelivery`, `ListLogDeliveries`, `PutResourcePolicy`, `DescribeResourcePolicies`, `DescribeLogGroups`; `xray:PutTraceSegments`, `PutTelemetryRecords`, `GetSamplingRules`, `GetSamplingTargets` | `*` | Step Functions logging and tracing; these actions do not support resource-level permissions |

### Reconciliation

**Case Cleanup role.** Makes abandoned cases eligible for the next run.

| Actions | Resource | Why |
| --- | --- | --- |
| `s3:GetObject` | `arn:aws:s3:::<raw>/account_number=*` | Read ownership markers of case partitions found by listing |
| `s3:DeleteObject` | Only `.../data.json`, `.../communications.json` and `.../workflow.json` under `arn:aws:s3:::<raw>/account_number=*/case_number=*/` | Reset abandoned cases, conditional on the checked ETag. Checkpoints and terminal markers cannot be deleted |
| `s3:PutObject` | `arn:aws:s3:::<raw>/account_number=*/case_number=*/workflow.json` | Write the reset claim before deleting |
| `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` | `arn:aws:s3:::<raw>/cleanup-state/*` | Resume cursor |
| `s3:GetObject`, `s3:PutObject` | `arn:aws:s3:::<raw>/account_number=*/case_number=*/failure-deliveries/*` | Preserve and deliver failure records; it cannot delete them |
| `sqs:SendMessage` | `CaseInsights-WorkflowFailures-<uniqueId>` | Deliver failure records |
| `s3:GetObject` | `arn:aws:s3:::<processed>/account_number=*` | Never reset a committed case |
| `s3:ListBucket` | Raw and processed buckets | List case partitions |
| `states:ListExecutions` | `CaseAnalysis-<uniqueId>` | Find running executions |
| `states:DescribeExecution` | Executions of `CaseAnalysis-<uniqueId>` | Check the execution named by a marker |

**Run Completion role.** Decides when a run is complete.

| Actions | Resource | Why |
| --- | --- | --- |
| `s3:ListBucket` | Account-list bucket, prefix `account-lists/*` | Find recent runs |
| `s3:GetObject` | `arn:aws:s3:::<account-list>/account-lists/*` | Read account lists |
| `s3:ListBucket` | Raw bucket | List run markers |
| `s3:GetObject` | `arn:aws:s3:::<raw>/runs/*` | Read run markers |
| `s3:PutObject` | `arn:aws:s3:::<raw>/runs/*/completion.json` | Write the completion marker once (`IfNoneMatch`) |
| `s3:GetObject`, `s3:PutObject` | `arn:aws:s3:::<raw>/account_number=*/case_number=*/failure-deliveries/*` | Preserve and deliver failure records |
| `states:DescribeExecution` | Executions of `CaseAnalysis-<uniqueId>` | Check executions named by case markers |
| `sqs:SendMessage` | `CaseInsights-WorkflowFailures-<uniqueId>` | Deliver failure records |

### Schedule roles

Three roles are trusted by `scheduler.amazonaws.com` with `aws:SourceAccount` equal to the account and `aws:SourceArn` equal to `arn:aws:scheduler:<region>:<account-id>:schedule-group/default`.

| Role | Actions |
| --- | --- |
| Account Lookup schedule role | `lambda:InvokeFunction` on `Lambda-AccountLookup-<uniqueId>` (and `:*`); `sqs:SendMessage` on `CaseInsights-AccountDiscoveryFailures-<uniqueId>`, the schedule's dead-letter queue |
| Case Cleanup schedule role | `lambda:InvokeFunction` on `Lambda-CaseCleanup-<uniqueId>` (and `:*`) |
| Run Completion schedule role | `lambda:InvokeFunction` on `Lambda-RunCompletion-<uniqueId>` (and `:*`) |

### Bedrock policy

The Case Analysis Stages role and the MCP function role each have the same Bedrock policy for the configured `bedrockModelId`. For the default Haiku 4.5 model, with `<profile>` = `global.anthropic.claude-haiku-4-5-20251001-v1:0` and `<model>` = `anthropic.claude-haiku-4-5-20251001-v1:0`:

| Effect | Actions | Resource | Condition |
| --- | --- | --- | --- |
| Allow | `bedrock:InvokeModel` | `arn:aws:bedrock:<region>:<account-id>:inference-profile/<profile>` | `aws:RequestedRegion` equals `region` |
| Allow | `bedrock:InvokeModel` | `arn:aws:bedrock:<region>::foundation-model/<model>` | `aws:RequestedRegion` equals `region` and `bedrock:InferenceProfileArn` equals the profile ARN |
| Allow | `bedrock:InvokeModel` | `arn:aws:bedrock:::foundation-model/<model>` | `aws:RequestedRegion` equals `unspecified` and `bedrock:InferenceProfileArn` equals the profile ARN |
| Deny (`DenyUnapprovedModels`) | `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream` | Every resource except the three above | None |
| Allow | `aws-marketplace:Subscribe` | `*` | `aws-marketplace:ProductId` is the model's product: `prod-xdkflymybwmvi` (Haiku 4.5) or `prod-mxcfnwvpd6kb4` (Sonnet 4.5) |
| Allow | `aws-marketplace:ViewSubscriptions` | `*` | None (the action supports no condition keys and only lists subscriptions) |

The three allows are the policy AWS documents for global cross-Region inference profiles: the foundation model can be invoked only through the approved profile, which may route a request to any AWS commercial Region. The application never calls the foundation model directly.

### Analytics crawler role

Trusted by `glue.amazonaws.com`.

| Actions | Resource | Why |
| --- | --- | --- |
| `glue:GetDatabase`, `GetTable`, `UpdateTable`, `GetPartition`, `GetPartitions`, `BatchGetPartition`, `CreatePartition`, `BatchCreatePartition`, `UpdatePartition`, `BatchUpdatePartition` | The Data Catalog, `case_insights_<uniqueId>` and `case_summary` | Add partitions for new cases |
| `s3:GetObject` | `arn:aws:s3:::<processed>/account_number=*` | Read processed records; no write or delete |
| `s3:ListBucket`, `s3:GetBucketLocation` | Processed bucket | List partitions |
| `logs:CreateLogGroup`, `CreateLogStream`, `PutLogEvents` | `/aws-glue/crawlers` and its streams | Crawler logs |

### MCP function role

| Actions | Resource | Why |
| --- | --- | --- |
| `athena:StartQueryExecution`, `StopQueryExecution`, `GetQueryExecution`, `GetQueryResults` | Workgroup `CaseInsights-<uniqueId>` | Run the read-only tool queries |
| `glue:GetDatabase`, `GetTable`, `GetPartition`, `GetPartitions`, `BatchGetPartition` | The Data Catalog, `case_insights_<uniqueId>` and `case_summary` | Read the table definition |
| `glue:GetTables` | The Data Catalog, the database and `table/case_insights_<uniqueId>/*` | `SHOW TABLES` on the database, which contains only `case_summary` |
| `s3:GetObject`; `s3:ListBucket`, `s3:GetBucketLocation` | Processed bucket (`account_number=*` for objects) | Athena reads records with the function's credentials; no write or delete |
| `s3:GetObject`, `PutObject`, `AbortMultipartUpload`, `ListMultipartUploadParts` | `arn:aws:s3:::<results bucket>/query-results/*` | Athena writes and reads query results |
| `s3:ListBucket`, `GetBucketLocation`, `ListBucketMultipartUploads` | Results bucket | As above |
| Bedrock policy | See [Bedrock policy](#bedrock-policy) | The `analyze_with_bedrock` tool |

### Shared API Gateway logging role

Trusted by `apigateway.amazonaws.com`, inline policy `ApiGatewayAccessLogs`: `logs:CreateLogStream`, `PutLogEvents`, `DescribeLogStreams`, `GetLogEvents` and `FilterLogEvents` on `arn:aws:logs:<region>:<account-id>:log-group:/aws/apigateway/CaseInsights-Mcp-*`, and `logs:DescribeLogGroups` on `arn:aws:logs:<region>:<account-id>:log-group:*`. See [Managed mode](mcp-server-deployment.md#managed-mode). In existing mode the application creates no role, and the existing role must allow at least these actions.

### CDK helper roles

| Role | Trust | Permissions |
| --- | --- | --- |
| `Custom::S3AutoDeleteObjects` provider role (dev only) | `lambda.amazonaws.com` | AWS managed `AWSLambdaBasicExecutionRole`; the dev bucket policies grant it `s3:DeleteObject*`, `s3:GetBucket*`, `s3:List*` and `s3:PutBucketPolicy` on their own bucket so it can empty it on deletion |
| `BucketNotificationsHandler` role | `lambda.amazonaws.com` | AWS managed `AWSLambdaBasicExecutionRole` and `s3:PutBucketNotification` on the account-list bucket |

## Member account role

The StackSet deploys `Support-Case-Analysis-Role-<uniqueId>` to every account under `memberRoleTargets`. It is the only resource the application creates outside the management account.

Trust policy (statement `TrustCaseInsightsCollectors`):

```json
{
  "Effect": "Allow",
  "Principal": {
    "AWS": [
      "arn:aws:iam::<management-account-id>:role/CaseInsights-CaseRetrieval-<uniqueId>",
      "arn:aws:iam::<management-account-id>:role/CaseInsights-CaseAnnotation-<uniqueId>"
    ]
  },
  "Action": "sts:AssumeRole",
  "Condition": { "StringEquals": { "aws:PrincipalOrgID": "<orgId>" } }
}
```

Inline policy `SupportCaseReadOnly` (statement `ReadSupportCases`): `support:DescribeCases` and `support:DescribeCommunications` on `*`. The AWS Support API does not support resource-level permissions. The role cannot create, update or resolve cases, and cannot add communications or attachments.

The role has a maximum session duration of one hour and carries the `Project` and `UniqueId` tags. It is managed by the StackSet; do not edit it in member accounts, because the next StackSet operation would overwrite the change. Calling the AWS Support API requires a Business, Enterprise On-Ramp or Enterprise support plan in the member account; accounts without one are skipped (`skippedSubscriptionRequired`).

The two sides limit each other: the collector roles may assume only roles with this exact name in accounts of the configured organisation (`aws:ResourceOrgID`), and the member role trusts only those two roles in an account of the same organisation (`aws:PrincipalOrgID`).

## Resource policies

| Resource | Policy |
| --- | --- |
| Every S3 bucket | Denies all actions without TLS (`aws:SecureTransport` false) and with TLS earlier than 1.2 (`s3:TlsVersion`) |
| Access-log bucket | Allows `logging.s3.amazonaws.com` to `s3:PutObject` under `account-list/`, `raw/` and `processed/` for the matching source bucket and `aws:SourceAccount`, and under `athena-results/` (statement `AllowAthenaResultsAccessLogs`) when Analytics is deployed |
| Every SQS queue | Denies all actions without TLS |
| Alarm topic | `AllowPublishThroughSSLOnly` denies `sns:Publish` without TLS; `AllowCloudWatchAlarmsToPublish` allows `cloudwatch.amazonaws.com` to publish only for `arn:aws:cloudwatch:<region>:<account-id>:alarm:CaseInsights-<uniqueId>-*` with `aws:SourceAccount` |
| `Lambda-AccountReader-<uniqueId>` | Allows invocation by Amazon S3 for the account-list bucket notification |
| `Lambda-McpServer-<uniqueId>` | Allows invocation by `apigateway.amazonaws.com` only from `arn:aws:execute-api:<region>:<account-id>:<api-id>/v1/POST/mcp` |
| MCP REST API | `AllowConfiguredPrincipalsInOrganization` allows `execute-api:Invoke` on `/v1/POST/mcp` to `mcpAllowedPrincipals` while `aws:PrincipalOrgID` equals `orgId`; `DenyOutsideOrganization`, `DenyUnlistedPrincipals` (by `aws:PrincipalArn`) and `DenyOtherRoutes` deny everything else |

## Operator and analyst permissions

The stacks create no human roles. Grant these in the management account to the people who need them.

| Task | Permissions | Reference |
| --- | --- | --- |
| Deploy, diff and destroy | The [deployer](#deployer) policy | [installation](installation.md) |
| Receive alarms | Confirm the SNS email subscription; no IAM permission needed | [monitoring](monitoring.md) |
| Monitor | Read access to CloudWatch alarms, dashboards, metrics and Logs Insights for the application log groups, and Step Functions read actions (`states:ListExecutions`, `DescribeExecution`, `GetExecutionHistory`) for `CaseAnalysis-<uniqueId>` | [monitoring](monitoring.md), [logging](logging.md) |
| Redrive a dead-letter queue | The SQS permissions listed in the runbook | [Redrive a stage dead-letter queue](runbook.md#redrive-a-stage-dead-letter-queue) |
| Reprocess committed cases | The S3, SQS, CloudFormation and Step Functions permissions listed in the runbook, including `s3:ListBucket` on the raw bucket and `s3:GetObjectVersion` on the raw conversations. This writes a short-lived reset claim to `workflow.json`, deletes processed records and checkpoints and causes new Bedrock invocations. Restoring a previous processed version needs separate permissions, also listed there | [Reprocess committed cases](runbook.md#reprocess-committed-cases) |
| Query with Athena | The analyst policy | [Permissions for analysts](athena.md#permissions-for-analysts) |
| Export CSV files | `s3:ListBucket` on the processed bucket and `s3:GetObject` on `account_number=*` | [Required permissions](csv_export.md#required-permissions) |
| Call the MCP endpoint | See below | [mcp-server-deployment](mcp-server-deployment.md) |
| Approve a prod release (pipeline only) | `codepipeline:PutApprovalResult` on `arn:aws:codepipeline:<region>:<account-id>:<pipelineName>/Prod/PromoteToProd` | [Trust boundary](pipeline.md#trust-boundary) |

Read access to the raw bucket, the processed bucket or the Athena results bucket is access to case content, including subjects, conversations and submitter identities. Grant it only to people who may see AWS Support cases from every member account.

### MCP callers

A caller needs all three of the following:

1. Its IAM role or user ARN in `mcpAllowedPrincipals`. For a role session, `aws:PrincipalArn` is the role ARN including any path, so list the role ARN (for an IAM Identity Center role, the ARN with its `/aws-reserved/sso.amazonaws.com/` path), not the session ARN.
2. Membership of the organisation `orgId`.
3. An identity policy that allows the call:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Sid": "InvokeCaseInsightsMcp",
         "Effect": "Allow",
         "Action": "execute-api:Invoke",
         "Resource": "arn:aws:execute-api:<region>:<account-id>:<api-id>/v1/POST/mcp"
       }
     ]
   }
   ```

The caller needs no Athena, Glue, S3 or Bedrock permissions: the MCP function uses its own role, and the tools are read-only.

## Encryption keys

Every resource uses AWS managed or service-managed encryption, and the application creates no customer managed KMS keys. No KMS permissions are needed by the deployer, the runtime roles, operators or analysts. The alarm topic is the one resource that is not encrypted at rest; see [Encryption at rest](resources.md#encryption-at-rest) and [security](security.md).

## Pipeline roles

With `-c pipeline=true`, CDK Pipelines generates the pipeline role (artifact bucket access), the CodeBuild synth role, the self-mutation role (which may assume only roles tagged `aws-cdk:bootstrap-role` in the account) and the file asset publishing role (which may assume only the bootstrap file publishing role). The stage deployments run through the bootstrap deploy and CloudFormation execution roles, as for a workstation deployment. Their wildcard findings are acknowledged one by one in `lib/stacks/pipeline-stack.ts`. Anyone who can push to `main` can change what these roles do; see [Trust boundary](pipeline.md#trust-boundary).

## cdk-nag checks

`lib/nag.ts` registers the cdk-nag `AwsSolutionsChecks` rule pack as a CDK validation plugin on every app. An unacknowledged finding fails synthesis, so `npm run verify` cannot pass with one. Findings are acknowledged individually on the narrowest construct with `acknowledgeNagFinding`, which rejects reasons shorter than 40 characters. Wildcards in policies that aws-cdk-lib generates are acknowledged by exact finding ID with `acknowledgeGeneratedPolicyWildcards`, which also fails synthesis when an acknowledgement no longer matches any finding, so the list cannot go stale.

The dev app (Core, MemberRole, Analytics, MCP and the shared logging stack) has 81 acknowledgements:

| Finding | Count | Reason in short |
| --- | --- | --- |
| `AwsSolutions-IAM5` (wildcards) | 61 | X-Ray and `organizations:ListAccounts` support no resource-level permissions; member account IDs, run IDs, account IDs and case IDs in S3 keys and execution names are discovered at run time, so keys are scoped to fixed prefixes and file names; CDK Lambda grants include versions (`:*`); Step Functions log delivery actions; Marketplace actions; the API Gateway logging role's log group prefix and `logs:DescribeLogGroups`; `glue:GetTables` for `SHOW TABLES` |
| `AwsSolutions-L1` (latest runtime) | 11 | Python 3.12 is the approved runtime, because the layer is built from hash-locked cp312 wheels |
| `AwsSolutions-SQS3` (no dead-letter queue) | 2 | `WorkflowFailures` and `AccountDiscoveryFailures` are terminal queues with no consumer |
| `AwsSolutions-SNS2` (topic not encrypted) | 1 | CloudWatch alarms cannot publish to a topic encrypted with `alias/aws/sns`, and the design excludes customer managed KMS keys; the topic enforces TLS, and notifications carry no case content but do carry operational metadata (account ID, Region, alarm names and states, metric values, resource names) |
| `AwsSolutions-GL1` (crawler log encryption) | 1 | Needs a customer managed KMS key; crawler logs hold only object paths and partition names |
| `AwsSolutions-IAM4` (AWS managed policy) | 1 | The CDK bucket notification helper uses `AWSLambdaBasicExecutionRole` |
| `AwsSolutions-APIG2` (request validation) | 1 | The MCP function validates the JSON-RPC envelope, body size and every tool argument |
| `AwsSolutions-APIG6` (execution logging) | 1 | Execution logs can contain request and response data; payload-free access logs and X-Ray are used instead |
| `AwsSolutions-COG4` (Cognito authoriser) | 1 | Authentication is SigV4 with `AWS_IAM` authorisation and the resource policy |
| `@aws-cdk/aws-s3:accessLogsPolicyNotAdded` (CDK warning) | 1 | Log delivery for the results bucket is granted in the Core access-log bucket policy |

The full text of each reason is in the code next to the resource it covers. The construct tests in `test/cdk/constructs/` assert the exact acknowledgements each construct makes, and `test/cdk/nag.test.ts` tests the acknowledgement helpers.

## Live verification (deferred)

None of the following has been run against a real account:

- [ ] The deployer policy above is sufficient for `npx cdk diff`, `deploy` and `destroy` with a freshly bootstrapped account.
- [ ] The collector roles can assume `Support-Case-Analysis-Role-<uniqueId>` in member accounts, and assumption fails from outside the organisation.
- [ ] Bedrock invocation succeeds through the global inference profile and the deny blocks every other model.
- [ ] The MCP resource policy admits listed principals in the organisation and refuses everyone else.

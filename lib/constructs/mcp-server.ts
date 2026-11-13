import * as path from 'node:path';
import { CfnOutput, Duration, Stack } from 'aws-cdk-lib';
import {
  AccessLogFormat,
  AuthorizationType,
  EndpointType,
  LambdaIntegration,
  LogGroupLogDestination,
  type Method,
  MethodLoggingLevel,
  RestApi,
} from 'aws-cdk-lib/aws-apigateway';
import { AnyPrincipal, ArnPrincipal, Effect, Policy, PolicyDocument, PolicyStatement } from 'aws-cdk-lib/aws-iam';
import type { ILayerVersion } from 'aws-cdk-lib/aws-lambda';
import { FilterPattern, LogGroup } from 'aws-cdk-lib/aws-logs';
import type { IBucket } from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';
import { type CaseInsightsConfig, MCP_RESERVED_CONCURRENCY, resourceNames } from '../config';
import { acknowledgeNagFinding, acknowledgeWildcardResourceFinding } from '../nag';
import { lambdaPackagesCode, REPO_ROOT } from '../packaging/python-assets';
import { ATHENA_RESULTS_PREFIX } from './analytics';
import { BedrockInvokePolicy } from './bedrock-invoke-policy';
import { handlerDirectory } from './construct-utils';
import { dataRemovalPolicy } from './environment';
import { McpWebAcl } from './mcp-web-acl';
import { logRetention, METRICS_NAMESPACE, PipelineFunction } from './pipeline-function';

export const MCP_STAGE_NAME = 'v1';
export const MCP_RESOURCE_PATH = 'mcp';
/** Powertools service name of the MCP function (the `service` dimension of its metrics). */
export const MCP_SERVICE_NAME = 'mcp-server';
export const MCP_HANDLER_PACKAGES = ['mcp_server', 'mcp_tools'] as const;
/**
 * The handler bounds each call to 25 seconds plus a one-second margin, and API Gateway REST
 * integrations stop waiting after 29 seconds, so the function never outlives its caller.
 */
export const MCP_FUNCTION_TIMEOUT = Duration.seconds(28);
/** The only route callers may invoke, in API Gateway resource-policy form. */
export const MCP_INVOKE_RESOURCE = `execute-api:/${MCP_STAGE_NAME}/POST/${MCP_RESOURCE_PATH}`;
const ALL_ROUTES = 'execute-api:/*';
const INVOKE = 'execute-api:Invoke';

/** API Gateway response types of requests refused by SigV4/IAM authorisation or the resource policy. */
export const MCP_AUTH_FAILURE_RESPONSE_TYPES = [
  'ACCESS_DENIED',
  'EXPIRED_TOKEN',
  'INVALID_SIGNATURE',
  'MISSING_AUTHENTICATION_TOKEN',
  'UNAUTHORIZED',
] as const;
/** Metric derived from the access log; dimension `ApiName`. */
export const MCP_AUTH_FAILURES_METRIC = 'McpAuthFailures';

const PROCESSED_RECORDS_PATTERN = 'account_number=*';

/**
 * Access log record of one request. Only request metadata: no bodies, headers, query strings or
 * caller ARNs (assumed-role session names can identify people), and no `$context.error.message`,
 * which repeats the canonical request, including the session token, for signature errors.
 * `apiName` is a literal so the auth-failure metric filter can publish an `ApiName` dimension.
 */
export function mcpAccessLogFormat(apiName: string): string {
  return JSON.stringify({
    requestId: '$context.requestId',
    extendedRequestId: '$context.extendedRequestId',
    requestTime: '$context.requestTime',
    apiName,
    stage: '$context.stage',
    httpMethod: '$context.httpMethod',
    resourcePath: '$context.resourcePath',
    protocol: '$context.protocol',
    status: '$context.status',
    responseLength: '$context.responseLength',
    responseLatency: '$context.responseLatency',
    errorType: '$context.error.responseType',
    integrationStatus: '$context.integration.status',
    integrationLatency: '$context.integration.latency',
    wafResponseCode: '$context.wafResponseCode',
    sourceIp: '$context.identity.sourceIp',
    callerAccountId: '$context.identity.accountId',
    xrayTraceId: '$context.xrayTraceId',
  });
}

/** What the MCP function queries: the Analytics catalog, workgroup and buckets, never request-supplied. */
export interface McpAnalyticsTarget {
  readonly databaseName: string;
  readonly tableName: string;
  readonly workgroupName: string;
  readonly workgroupArn: string;
  /** Holds the workgroup's enforced results location under ATHENA_RESULTS_PREFIX. */
  readonly resultsBucket: IBucket;
  /** Core processed-record bucket behind the Glue table. */
  readonly processedBucket: IBucket;
}

export interface McpServerProps {
  readonly config: CaseInsightsConfig;
  /** Core shared layer providing `common` and the pinned runtime dependencies. */
  readonly commonLayer: ILayerVersion;
  readonly analytics: McpAnalyticsTarget;
}

/**
 * The MCP endpoint: an API Gateway REST API with stage `v1` and the single JSON-RPC route
 * `POST /mcp`, authorised with SigV4 (AWS_IAM) and a resource policy that admits only the configured
 * principals, and only while they belong to the configured AWS Organization. The stage is throttled,
 * traced with X-Ray, access-logged without payloads and protected by AWS WAF. CORS is not enabled.
 *
 * The function runs the eight read-only tools against the Analytics workgroup and database (fixed in
 * its environment) and the configured Bedrock model, with reserved concurrency and its own role.
 */
export class McpServer extends Construct {
  public readonly handler: PipelineFunction;
  public readonly access: Policy;
  public readonly bedrock: BedrockInvokePolicy;
  public readonly api: RestApi;
  public readonly method: Method;
  public readonly accessLogGroup: LogGroup;
  public readonly webAcl: McpWebAcl;
  /** `https://<api-id>.execute-api.<region>.amazonaws.com/v1/mcp` */
  public readonly endpointUrl: string;

  public constructor(scope: Construct, id: string, props: McpServerProps) {
    super(scope, id);
    const { config, analytics } = props;
    const names = resourceNames(config);

    this.handler = new PipelineFunction(this, 'McpFunction', {
      config,
      functionName: names.mcpServerFunction,
      description: `Support Case Insights MCP JSON-RPC server with eight read-only tools (${config.uniqueId})`,
      handlerDir: handlerDirectory(MCP_HANDLER_PACKAGES[0]),
      code: lambdaPackagesCode(path.join(REPO_ROOT, 'src', 'lambda'), MCP_HANDLER_PACKAGES),
      handler: 'mcp_server.app.handler',
      serviceName: MCP_SERVICE_NAME,
      layers: [props.commonLayer],
      timeout: MCP_FUNCTION_TIMEOUT,
      reservedConcurrentExecutions: MCP_RESERVED_CONCURRENCY,
      environment: {
        ATHENA_WORKGROUP: analytics.workgroupName,
        ATHENA_DATABASE: analytics.databaseName,
        BEDROCK_MODEL_ID: config.bedrockModelId,
        BEDROCK_MAX_TOKENS: String(config.bedrockMaxTokens),
      },
    });
    this.access = this.grantQueryAccess(analytics);
    this.bedrock = new BedrockInvokePolicy(this, 'Bedrock', { role: this.handler.role, modelIds: [config.bedrockModelId] });

    this.accessLogGroup = new LogGroup(this, 'AccessLogs', {
      logGroupName: names.mcpAccessLogGroup,
      retention: logRetention(config.logRetentionDays),
      removalPolicy: dataRemovalPolicy(config),
    });
    this.accessLogGroup.addMetricFilter('AuthFailures', {
      filterPattern: FilterPattern.any(...MCP_AUTH_FAILURE_RESPONSE_TYPES.map((type) => FilterPattern.stringValue('$.errorType', '=', type))),
      metricNamespace: METRICS_NAMESPACE,
      metricName: MCP_AUTH_FAILURES_METRIC,
      metricValue: '1',
      dimensions: { ApiName: '$.apiName' },
    });

    const resourcePolicy = mcpResourcePolicy(config);
    this.api = new RestApi(this, 'Api', {
      restApiName: names.mcpApi,
      description: `Support Case Insights MCP endpoint (${config.uniqueId})`,
      endpointConfiguration: { types: [EndpointType.REGIONAL] },
      // Access logging needs the account/Region API Gateway CloudWatch role setting, which belongs to the
      // shared CaseInsightsApiLoggingStack-<sharedId> or an existing configuration (apiGatewayLogging).
      // A per-API role here would create a competing AWS::ApiGateway::Account in every environment.
      cloudWatchRole: false,
      policy: resourcePolicy,
      deployOptions: {
        stageName: MCP_STAGE_NAME,
        description: `MCP stage ${MCP_STAGE_NAME}`,
        tracingEnabled: true,
        throttlingRateLimit: config.operations.mcp.throttleRateLimit,
        throttlingBurstLimit: config.operations.mcp.throttleBurstLimit,
        loggingLevel: MethodLoggingLevel.OFF,
        dataTraceEnabled: false,
        metricsEnabled: false,
        accessLogDestination: new LogGroupLogDestination(this.accessLogGroup),
        accessLogFormat: AccessLogFormat.custom(mcpAccessLogFormat(names.mcpApi)),
      },
    });
    // API Gateway applies a changed resource policy only after a new deployment.
    this.api.latestDeployment?.addToLogicalId({ resourcePolicy: resourcePolicy.toJSON() as unknown });

    this.method = this.api.root.addResource(MCP_RESOURCE_PATH).addMethod(
      'POST',
      new LambdaIntegration(this.handler.function, { proxy: true, allowTestInvoke: false }),
      { authorizationType: AuthorizationType.IAM },
    );
    this.endpointUrl = this.api.urlForPath(`/${MCP_RESOURCE_PATH}`);

    this.webAcl = new McpWebAcl(this, 'WebAcl', { config, stageArn: this.api.deploymentStage.stageArn });

    acknowledgeNagFinding(
      this.api,
      'AwsSolutions-APIG2',
      'The MCP function validates the JSON-RPC envelope, content type, 32 KiB body limit and every tool argument schema; API Gateway draft-04 models cannot express these rules and would replace JSON-RPC error replies.',
    );
    acknowledgeNagFinding(
      this.api.deploymentStage,
      'AwsSolutions-APIG6',
      'Execution logs can contain request and response data and would add per-method log groups; each request is recorded in the payload-free JSON access log, which uses the shared account/Region CloudWatch role, and traced with X-Ray instead.',
    );
    acknowledgeNagFinding(
      this.method,
      'AwsSolutions-COG4',
      'The approved authentication is SigV4 with AWS_IAM authorisation plus a resource policy limited to configured principals in the Organization; no Cognito user pool is used.',
    );

    new CfnOutput(this, 'EndpointUrlOutput', { key: 'McpEndpointUrl', value: this.endpointUrl, description: 'MCP_SERVER_URL for scripts/mcp_aws_client.py (SigV4, execute-api)' });
    new CfnOutput(this, 'RestApiIdOutput', { key: 'McpRestApiId', value: this.api.restApiId, description: 'MCP REST API ID' });
    new CfnOutput(this, 'FunctionNameOutput', { key: 'McpFunctionName', value: this.handler.function.functionName, description: 'MCP Lambda function' });
    new CfnOutput(this, 'AccessLogGroupOutput', { key: 'McpAccessLogGroupName', value: this.accessLogGroup.logGroupName, description: 'MCP API access log group' });
    new CfnOutput(this, 'WebAclArnOutput', { key: 'McpWebAclArn', value: this.webAcl.webAcl.attrArn, description: 'AWS WAF web ACL protecting the MCP stage' });
  }

  private grantQueryAccess(analytics: McpAnalyticsTarget): Policy {
    const stack = Stack.of(this);
    const glueArn = (resource: string, resourceName?: string): string => stack.formatArn({ service: 'glue', resource, resourceName });
    const catalog = glueArn('catalog');
    const database = glueArn('database', analytics.databaseName);
    const allTables = glueArn('table', `${analytics.databaseName}/*`);
    const processedObjects = analytics.processedBucket.arnForObjects(PROCESSED_RECORDS_PATTERN);
    const resultObjects = analytics.resultsBucket.arnForObjects(`${ATHENA_RESULTS_PREFIX}*`);
    const access = new Policy(this, 'McpServerAccess', {
      roles: [this.handler.role],
      statements: [
        new PolicyStatement({
          sid: 'QueryCaseInsightsWorkgroup',
          actions: ['athena:StartQueryExecution', 'athena:GetQueryExecution', 'athena:GetQueryResults', 'athena:StopQueryExecution'],
          resources: [analytics.workgroupArn],
        }),
        new PolicyStatement({
          sid: 'ReadCaseSummaryCatalog',
          actions: ['glue:GetDatabase', 'glue:GetTable', 'glue:GetPartition', 'glue:GetPartitions', 'glue:BatchGetPartition'],
          resources: [catalog, database, glueArn('table', `${analytics.databaseName}/${analytics.tableName}`)],
        }),
        new PolicyStatement({ sid: 'ListCaseSummaryTables', actions: ['glue:GetTables'], resources: [catalog, database, allTables] }),
        new PolicyStatement({ sid: 'ReadProcessedRecords', actions: ['s3:GetObject'], resources: [processedObjects] }),
        new PolicyStatement({
          sid: 'ListProcessedRecords',
          actions: ['s3:ListBucket', 's3:GetBucketLocation'],
          resources: [analytics.processedBucket.bucketArn],
        }),
        new PolicyStatement({
          sid: 'WriteQueryResults',
          actions: ['s3:GetObject', 's3:PutObject', 's3:AbortMultipartUpload', 's3:ListMultipartUploadParts'],
          resources: [resultObjects],
        }),
        new PolicyStatement({
          sid: 'ListQueryResults',
          actions: ['s3:ListBucket', 's3:GetBucketLocation', 's3:ListBucketMultipartUploads'],
          resources: [analytics.resultsBucket.bucketArn],
        }),
      ],
    });
    acknowledgeWildcardResourceFinding(
      access,
      processedObjects,
      'Athena reads processed records with the caller credentials; account and case partitions are created at run time, and the role cannot write or delete them.',
    );
    acknowledgeWildcardResourceFinding(
      access,
      resultObjects,
      'Athena writes and reads query results under the enforced query-results/ prefix of the dedicated results bucket with the caller credentials; result object names are generated per query.',
    );
    acknowledgeWildcardResourceFinding(
      access,
      allTables,
      `SHOW TABLES of the configured database needs glue:GetTables over its tables; the statement holds only that read action and the database contains only ${analytics.tableName}.`,
    );
    return access;
  }
}

/**
 * Resource policy of the MCP API. Only the configured principals may invoke `POST /v1/mcp`, and only
 * from accounts in the Organization. The explicit denies also bind same-account principals whose own
 * IAM policies would otherwise grant execute-api:Invoke: anyone outside the Organization, any principal
 * not in the allowlist, and any other stage, method or path.
 */
export function mcpResourcePolicy(config: Pick<CaseInsightsConfig, 'orgId' | 'mcpAllowedPrincipals'>): PolicyDocument {
  const principals = [...config.mcpAllowedPrincipals];
  if (principals.length === 0) {
    throw new Error('The MCP API needs at least one allowed principal');
  }
  return new PolicyDocument({
    statements: [
      new PolicyStatement({
        sid: 'AllowConfiguredPrincipalsInOrganization',
        principals: principals.map((arn) => new ArnPrincipal(arn)),
        actions: [INVOKE],
        resources: [MCP_INVOKE_RESOURCE],
        conditions: { StringEquals: { 'aws:PrincipalOrgID': config.orgId } },
      }),
      new PolicyStatement({
        sid: 'DenyOutsideOrganization',
        effect: Effect.DENY,
        principals: [new AnyPrincipal()],
        actions: [INVOKE],
        resources: [ALL_ROUTES],
        conditions: { StringNotEquals: { 'aws:PrincipalOrgID': config.orgId } },
      }),
      new PolicyStatement({
        sid: 'DenyUnlistedPrincipals',
        effect: Effect.DENY,
        principals: [new AnyPrincipal()],
        actions: [INVOKE],
        resources: [ALL_ROUTES],
        conditions: { ArnNotEquals: { 'aws:PrincipalArn': principals } },
      }),
      new PolicyStatement({
        sid: 'DenyOtherRoutes',
        effect: Effect.DENY,
        principals: [new AnyPrincipal()],
        actions: [INVOKE],
        notResources: [MCP_INVOKE_RESOURCE],
      }),
    ],
  });
}

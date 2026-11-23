import { ArnFormat, RemovalPolicy, Stack } from 'aws-cdk-lib';
import { CfnAccount } from 'aws-cdk-lib/aws-apigateway';
import { PolicyDocument, PolicyStatement, Role, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { MCP_ACCESS_LOG_GROUP_PREFIX } from '../config';
import { acknowledgeWildcardResourceFinding } from '../nag';

export interface ApiGatewayAccountLoggingProps {
  /** Shared namespace (apiGatewayLogging.sharedId), used in descriptions. */
  readonly sharedId: string;
}

/**
 * The account- and Region-wide prerequisite of REST API access logging: an IAM role that API Gateway
 * assumes to write CloudWatch Logs, and the single AWS::ApiGateway::Account resource that sets it as the
 * CloudWatch role ARN. Both are retained when their stack is deleted, because every API in the account
 * and Region (including other environments) depends on the setting.
 *
 * The role's permissions are inline so a retained role keeps them after its stack is gone, and its name
 * is generated so a later deployment can create a new role while a retained one still exists.
 */
export class ApiGatewayAccountLogging extends Construct {
  public readonly role: Role;
  public readonly account: CfnAccount;

  public constructor(scope: Construct, id: string, props: ApiGatewayAccountLoggingProps) {
    super(scope, id);
    const stack = Stack.of(this);
    const logGroupArn = (name: string): string =>
      stack.formatArn({ service: 'logs', resource: 'log-group', resourceName: name, arnFormat: ArnFormat.COLON_RESOURCE_NAME });
    const solutionAccessLogs = logGroupArn(`${MCP_ACCESS_LOG_GROUP_PREFIX}*`);
    const allLogGroups = logGroupArn('*');

    this.role = new Role(this, 'CloudWatchRole', {
      description: `API Gateway CloudWatch Logs role for Support Case Insights REST API access logs (shared ${props.sharedId})`,
      assumedBy: new ServicePrincipal('apigateway.amazonaws.com'),
      inlinePolicies: {
        ApiGatewayAccessLogs: new PolicyDocument({
          statements: [
            new PolicyStatement({
              sid: 'WriteSolutionApiAccessLogs',
              actions: ['logs:CreateLogStream', 'logs:DescribeLogStreams', 'logs:FilterLogEvents', 'logs:GetLogEvents', 'logs:PutLogEvents'],
              resources: [solutionAccessLogs],
            }),
            new PolicyStatement({ sid: 'DescribeLogGroups', actions: ['logs:DescribeLogGroups'], resources: [allLogGroups] }),
          ],
        }),
      },
    });
    this.role.applyRemovalPolicy(RemovalPolicy.RETAIN);

    this.account = new CfnAccount(this, 'Account', { cloudWatchRoleArn: this.role.roleArn });
    this.account.applyRemovalPolicy(RemovalPolicy.RETAIN);

    acknowledgeWildcardResourceFinding(
      this.role,
      solutionAccessLogs,
      'API Gateway writes REST access logs only to the Support Case Insights MCP access log groups of every environment namespace in this account and Region; their streams are created by API Gateway at run time.',
    );
    acknowledgeWildcardResourceFinding(
      this.role,
      allLogGroups,
      'logs:DescribeLogGroups is a list operation that cannot be limited to named log groups; API Gateway uses it to find access log destinations, and it is limited to this account and Region and grants no read or write access.',
    );
  }
}

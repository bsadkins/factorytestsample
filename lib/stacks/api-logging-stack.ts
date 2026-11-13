import { CfnOutput, Stack, type StackProps } from 'aws-cdk-lib';
import type { Construct } from 'constructs';
import type { ApiGatewayLoggingConfig } from '../config';
import { ApiGatewayAccountLogging } from '../constructs/api-gateway-account-logging';

export interface CaseInsightsApiLoggingStackProps extends StackProps {
  /** Shared settings in managed mode; the stack depends only on these, its account and its Region. */
  readonly settings: ApiGatewayLoggingConfig;
}

/**
 * Shared, environment-independent owner of the API Gateway CloudWatch role setting for one account and
 * Region (`CaseInsightsApiLoggingStack-<sharedId>`). Every environment app that enables MCP synthesises
 * the identical template, so dev and prod deployments reuse one stack instead of competing for the
 * setting. Its role and setting are retained if the stack is deleted with an environment.
 */
export class CaseInsightsApiLoggingStack extends Stack {
  public readonly settings: ApiGatewayLoggingConfig;
  public readonly logging: ApiGatewayAccountLogging;

  public constructor(scope: Construct, id: string, props: CaseInsightsApiLoggingStackProps) {
    super(scope, id, props);
    if (props.settings.mode !== 'managed') {
      throw new Error('CaseInsightsApiLoggingStack is only created in managed apiGatewayLogging mode');
    }
    this.settings = props.settings;
    this.logging = new ApiGatewayAccountLogging(this, 'ApiGatewayLogging', { sharedId: props.settings.sharedId });

    new CfnOutput(this, 'RoleArnOutput', {
      key: 'ApiGatewayCloudWatchRoleArn',
      value: this.logging.role.roleArn,
      description: 'Role set as the API Gateway CloudWatch role ARN for this account and Region (compare with aws apigateway get-account)',
    });
    new CfnOutput(this, 'RoleNameOutput', {
      key: 'ApiGatewayCloudWatchRoleName',
      value: this.logging.role.roleName,
      description: 'Shared API Gateway CloudWatch Logs role (retained when the stack is deleted)',
    });
  }
}

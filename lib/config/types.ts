/**
 * Typed view of config/<env>.json after schema defaults are applied.
 *
 * config/schema.json is the published contract and the source of defaults; these
 * interfaces mirror it so stacks and constructs receive typed props rather than raw
 * CDK context. test/config/schema-parity.test.ts fails if the two drift apart.
 */

export const ENVIRONMENT_NAMES = ['dev', 'prod'] as const;
export type EnvironmentName = (typeof ENVIRONMENT_NAMES)[number];

export const HAIKU_4_5_GLOBAL_PROFILE = 'global.anthropic.claude-haiku-4-5-20251001-v1:0';
export const SONNET_4_5_GLOBAL_PROFILE = 'global.anthropic.claude-sonnet-4-5-20250929-v1:0';

/** The only Bedrock inference profiles the solution accepts; the first is the default. */
export const SUPPORTED_BEDROCK_MODEL_IDS = [HAIKU_4_5_GLOBAL_PROFILE, SONNET_4_5_GLOBAL_PROFILE] as const;
export type BedrockModelId = (typeof SUPPORTED_BEDROCK_MODEL_IDS)[number];

/** UTC cron expressions in EventBridge Scheduler / AWS Glue syntax. */
export interface ScheduleConfig {
  readonly accountLookup: string;
  readonly cleanup: string;
  readonly crawler: string;
}

export interface QueueOperationsConfig {
  readonly maxReceiveCount: number;
  /** Explicit override; when absent the visibility timeout is 6 x consumerTimeoutSeconds. */
  readonly visibilityTimeoutSeconds?: number;
  readonly dlqRetentionDays: number;
}

export interface FunctionOperationsConfig {
  readonly consumerTimeoutSeconds: number;
  readonly analysisTimeoutSeconds: number;
  readonly memorySizeMb: number;
}

export interface WorkflowOperationsConfig {
  readonly executionTimeoutMinutes: number;
  readonly starterMaxConcurrency: number;
  readonly taskMaxAttempts: number;
}

export interface CleanupOperationsConfig {
  readonly graceHours: number;
}

export interface StorageOperationsConfig {
  readonly processedInfrequentAccessDays: number;
  readonly noncurrentVersionRetentionDays: number;
  readonly accessLogRetentionDays: number;
}

export interface AthenaOperationsConfig {
  readonly bytesScannedCutoffPerQuery: number;
  readonly resultsRetentionDays: number;
}

/** Reserved concurrency of the MCP function: fixed by design, not an operator setting. */
export const MCP_RESERVED_CONCURRENCY = 20;

export interface McpOperationsConfig {
  readonly reservedConcurrency: typeof MCP_RESERVED_CONCURRENCY;
  readonly throttleRateLimit: number;
  readonly throttleBurstLimit: number;
  readonly wafRateLimitPer5Minutes: number;
}

export interface OperationsConfig {
  readonly queues: QueueOperationsConfig;
  readonly functions: FunctionOperationsConfig;
  readonly workflow: WorkflowOperationsConfig;
  readonly cleanup: CleanupOperationsConfig;
  readonly storage: StorageOperationsConfig;
  readonly athena: AthenaOperationsConfig;
  readonly mcp: McpOperationsConfig;
}

/**
 * Who owns the account/Region API Gateway CloudWatch Logs role setting that REST API access logging needs.
 * - `managed`: the shared CaseInsightsApiLoggingStack-<sharedId> creates the role and the one
 *   AWS::ApiGateway::Account association for the account and Region.
 * - `existing`: the account/Region setting is already configured with `existingRoleArn`; nothing is created.
 */
export const API_GATEWAY_LOGGING_MODES = ['managed', 'existing'] as const;
export type ApiGatewayLoggingMode = (typeof API_GATEWAY_LOGGING_MODES)[number];

export interface ApiGatewayLoggingConfig {
  readonly mode: ApiGatewayLoggingMode;
  /** Namespace of the shared logging stack; the same for every environment in an account and Region. */
  readonly sharedId: string;
  /** Present exactly when mode is `existing`. */
  readonly existingRoleArn?: string;
}

export interface CaseInsightsConfig {
  readonly uniqueId: string;
  readonly orgId: string;
  readonly managementAccountId: string;
  readonly region: string;
  readonly environment: EnvironmentName;
  readonly enableAnalytics: boolean;
  readonly enableMcp: boolean;
  /** Organization root (r-...) or OU (ou-...) IDs targeted by the member-role StackSet. */
  readonly memberRoleTargets: readonly string[];
  readonly bedrockModelId: BedrockModelId;
  readonly bedrockMaxTokens: number;
  readonly bedrockMaxConcurrency: number;
  readonly lookbackMonths: number;
  readonly rawRetentionDays: number;
  readonly logRetentionDays: number;
  readonly alarmEmail?: string;
  readonly mcpAllowedPrincipals: readonly string[];
  readonly apiGatewayLogging: ApiGatewayLoggingConfig;
  readonly schedules: ScheduleConfig;
  readonly operations: OperationsConfig;
}

export interface PipelineConfig {
  readonly pipelineName: string;
  readonly account: string;
  readonly region: string;
  readonly connectionArn: string;
  readonly repository: string;
  readonly branch: 'main';
}

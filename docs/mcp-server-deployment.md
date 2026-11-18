# MCP server deployment and client access

The optional MCP stack (`CaseInsightsMcpStack-<uniqueId>`) exposes one JSON-RPC endpoint, `POST /v1/mcp`, on an Amazon API Gateway REST API. It requires AWS Signature Version 4 (SigV4) authentication. MCP clients such as Kiro and Claude Desktop speak MCP over stdio, so they connect through the local bridge `scripts/mcp_aws_client.py`, which signs every request with your AWS credentials.

Live client-to-AWS connectivity, including Kiro discovering the eight tools against a deployed endpoint, has **not** been tested yet: it is part of the deferred deployment checklist. Local tests run the bridge as a real subprocess against a loopback HTTPS fixture that verifies the signatures (see [Local verification](#local-verification)).

## Prerequisites

- The deployment has `enableAnalytics` and `enableMcp` set to `true`, and your IAM principal (user or role ARN) is listed in `mcpAllowedPrincipals`.
- Your principal is allowed `execute-api:Invoke` on `arn:aws:execute-api:<region>:<account-id>:<api-id>/v1/POST/mcp`. The API's resource policy also limits callers to your AWS Organization.
- A local checkout with the repository's Python environment, which provides `boto3`:

  ```bash
  npm ci
  python3.12 -m venv .venv
  .venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
  ```

  The bridge needs only Python 3.12 and `boto3`; any environment with those works.
- AWS credentials available through the standard credential provider chain: environment variables, a named profile in the shared credentials or config file, IAM Identity Center (`aws sso login --profile <name>`), an assumed role, or a container or instance role. Temporary credentials and their session token are supported and refreshed automatically. **Never put access keys or session tokens in an MCP client configuration file.**

## Infrastructure and security controls

The MCP stack depends on the Core and Analytics stacks and reads their outputs; neither reads MCP outputs, so they deploy and update without it. In managed API Gateway logging mode it also deploys after the shared `CaseInsightsApiLoggingStack-<sharedId>` (see [below](#shared-api-gateway-logging-prerequisite)). It contains:

- **REST API** `CaseInsights-Mcp-<uniqueId>` (regional) with stage `v1` and one method, `POST /mcp`, with `AWS_IAM` authorisation and a Lambda proxy integration. There is no unauthenticated method, API key, authoriser or CORS configuration.
- **Resource policy**: allows `execute-api:Invoke` on `/v1/POST/mcp` only to the ARNs in `mcpAllowedPrincipals` and only while `aws:PrincipalOrgID` equals `orgId`. Explicit denies refuse callers outside the Organization, principals not in the list (by `aws:PrincipalArn`, so these denies also bind same-account principals whose own IAM policies allow `execute-api:Invoke`), and every other stage, method or path. A change to the principals produces a new deployment so the policy takes effect.
- **Stage settings**: throttling at `operations.mcp.throttleRateLimit` requests per second with burst `operations.mcp.throttleBurstLimit`, X-Ray tracing, and JSON access logs in `/aws/apigateway/CaseInsights-Mcp-<uniqueId>/access` (retention `logRetentionDays`). Access logs record request metadata only: no bodies, headers, query strings, caller ARNs or API Gateway error messages, which can repeat a signed request. Execution logging is off, because execution logs can contain request and response data. Access logs need the account- and Region-wide API Gateway CloudWatch role setting; see [Shared API Gateway logging prerequisite](#shared-api-gateway-logging-prerequisite).
- **AWS WAF** web ACL `CaseInsights-Mcp-<uniqueId>` on the stage: a per-IP rate rule (`operations.mcp.wafRateLimitPer5Minutes` per five minutes, `429`), an explicit block of bodies over 32,768 bytes (`413`), and the AWS managed Core rule set. The web ACL inspects the first 32 KiB of each body (the API Gateway default is 16 KiB) and treats larger bodies as matching the size rule. The managed rule set's 8 KB body-size rule only counts, so the application limit stays 32 KiB; the MCP function enforces the same limit on the decoded body. Sampled requests are off, because they would keep signed request headers.
- **Function** `Lambda-McpServer-<uniqueId>` (Python 3.12, reserved concurrency fixed at 20 (`operations.mcp.reservedConcurrency` accepts only 20), 28-second timeout within API Gateway's 29-second limit) with the Core common layer. Its environment fixes the Analytics workgroup and database and the Bedrock model; requests cannot choose them. Its role can query only the Analytics workgroup, read the case summary table, read processed records, read and write query results under `query-results/`, and invoke only the configured model.
- **Monitoring** in Core: alarms [LambdaErrors-McpServer](runbook.md#lambdaerrors-mcpserver), [McpInternalErrors](runbook.md#mcpinternalerrors), [McpApiServerErrors](runbook.md#mcpapiservererrors) and [McpAuthFailures](runbook.md#mcpauthfailures), and dashboard rows for MCP requests and errors, API responses and WAF blocks. `McpAuthFailures` is derived from access log entries refused by SigV4 or the resource policy.

## Shared API Gateway logging prerequisite

REST API access logs can only be delivered after the account's API Gateway settings name a CloudWatch Logs role (`cloudWatchRoleArn`) in that Region, even with execution logging off. There is one such setting per account and Region, shared by every REST API there. It therefore does not belong to an environment's MCP stack; the MCP API keeps `cloudWatchRole: false`, and the owner is chosen with `apiGatewayLogging` in `config/<env>.json`:

| Field | Default | Meaning |
| --- | --- | --- |
| `mode` | `managed` | `managed`: the shared stack `CaseInsightsApiLoggingStack-<sharedId>` creates the role and the account setting. `existing`: the setting is already configured by someone else; nothing is created or changed. |
| `sharedId` | `shared` | Namespace of the shared stack, independent of `uniqueId` so that dev and prod share one owner. The shipped examples use `scishared`. Same rules as `uniqueId`. |
| `existingRoleArn` | none | Required in `existing` mode and rejected in `managed` mode: the IAM role in `managementAccountId` that is already set as the CloudWatch role ARN in `region`. |

Every environment deployed to the same account and Region must use identical `apiGatewayLogging` settings. When the app synthesises one environment it also reads the other `config/<env>.json`, if present, and stops with an error when the two disagree; the optional pipeline's stage validation applies the same rule. The check compares the files whether or not MCP is enabled, so keep the block identical in every environment file. The setting has an effect only when `enableMcp` is true: with MCP disabled no shared stack, role or account setting is added.

### Managed mode

With MCP enabled, ordinary `npx cdk deploy --all -c env=<env>` also deploys `CaseInsightsApiLoggingStack-<sharedId>`, and the MCP stack depends on it so the setting exists before the `v1` stage is created. The shared stack contains:

- one IAM role trusted only by `apigateway.amazonaws.com`, with a generated name (outputs `ApiGatewayCloudWatchRoleName` and `ApiGatewayCloudWatchRoleArn`) and one inline policy, `ApiGatewayAccessLogs`:
  - `logs:CreateLogStream`, `logs:PutLogEvents`, `logs:DescribeLogStreams`, `logs:GetLogEvents` and `logs:FilterLogEvents` on `arn:aws:logs:<region>:<account-id>:log-group:/aws/apigateway/CaseInsights-Mcp-*`, which covers the MCP access log group of every environment namespace;
  - `logs:DescribeLogGroups` on `arn:aws:logs:<region>:<account-id>:log-group:*`, because this list operation cannot be limited to named log groups. It grants no read or write access to log data.

  There is no `logs:CreateLogGroup`: each MCP stack creates its own access log group with its retention, and execution logging stays off. The AWS managed policy `AmazonAPIGatewayPushToCloudWatchLogs` grants the same actions on every log group; this role is narrower.
- one `AWS::ApiGateway::Account` resource that sets that role as the CloudWatch role ARN for the account and Region.

Both resources have `DeletionPolicy: Retain`. Environment stacks never contain an `AWS::ApiGateway::Account` resource or an API Gateway logging role. The shared stack's template depends only on `sharedId`, the account and the Region (it carries `Project` and `SharedId` tags, not environment tags), so dev and prod synthesise the identical stack and whichever deploys second finds nothing to change. The CDK API for pipelines follows the same model: `addSharedApiLogging(app, [dev, prod])` creates the shared stack once at the root, and each stage receives it through `addCaseInsightsEnvironmentStacks(stage, config, { sharedApiLogging })`. CDK cannot order stacks across stages, so the optional pipeline deploys the shared stack in its own `SharedApiLogging` stage before its dev stage (see [docs/pipeline.md](pipeline.md#stacks-stages-and-names)).

**Check ownership before the first deployment.** The CDK app never queries your account. Managed mode sets the account-wide value, replacing any role ARN that is already configured, and its role can write only to this solution's log groups. Before the first deployment in an account and Region, run:

```bash
aws apigateway get-account --region <region> --query cloudwatchRoleArn --output text
```

- `None` (nothing configured): managed mode is appropriate.
- A role that `CaseInsightsApiLoggingStack-<sharedId>` created (compare with its `ApiGatewayCloudWatchRoleArn` output): managed mode is already in place.
- Any other role, for example one owned by another application, a platform team or a landing zone: do not take it over. Use existing mode with that ARN, and ask its owner to allow writes to `/aws/apigateway/CaseInsights-Mcp-*`.

Then review `npx cdk diff --all -c env=<env>` before deploying. An added or changed `AWS::ApiGateway::Account` in `CaseInsightsApiLoggingStack-<sharedId>` is an account- and Region-wide change that affects every REST API there, including APIs that are not part of this solution. If other REST APIs in the Region also need the setting for their logs, use a broader role that they share, in existing mode, rather than the solution-scoped managed role.

### Existing-role mode

```json
"apiGatewayLogging": {
  "mode": "existing",
  "sharedId": "scishared",
  "existingRoleArn": "arn:aws:iam::111111111111:role/ApiGatewayCloudWatchLogs"
}
```

Nothing is created or changed: no shared stack, role or `AWS::ApiGateway::Account`. The MCP stack records the ARN in its `ApiGatewayCloudWatchRoleArn` output so you can compare it with `aws apigateway get-account`. You are responsible for the setting: the role must already be set as the CloudWatch role ARN in that account and Region, must be in the management account (the loader checks the account ID), must trust `apigateway.amazonaws.com` and must allow at least the actions above on `/aws/apigateway/CaseInsights-Mcp-*` (`AmazonAPIGatewayPushToCloudWatchLogs` is sufficient). If the setting is missing, API Gateway rejects the stage's access log configuration and the MCP stack deployment fails; configure the setting, or switch to managed mode if nobody else owns it.

### Regional STS

API Gateway assumes the logging role through AWS STS in the API's Region, so STS must be active there. It is active by default in Regions that are enabled by default; for opt-in Regions, check **IAM → Account settings → Security Token Service (STS)**. If setting the role ARN fails with an STS error, activate STS for the Region and deploy again.

### Teardown and final clean-up

The shared role and account setting outlive environment teardown, so destroying dev cannot break prod's access logs:

- `npx cdk destroy --all -c env=dev` also deletes `CaseInsightsApiLoggingStack-<sharedId>` when it is part of that app, but the role (with its inline policy) and the account setting are retained. Prod's API keeps logging.
- A later deployment with MCP enabled recreates the shared stack. It creates a new role (the generated name avoids a clash with the retained one) and points the setting at it. The previously retained role is then unused and can be deleted once `aws apigateway get-account` shows the new ARN.

Delete the shared resources deliberately, by hand, only after every MCP environment in that account and Region has been destroyed:

1. Confirm what the setting points at: `aws apigateway get-account --region <region> --query cloudwatchRoleArn --output text`.
2. Confirm that no other REST API in the Region still needs it: list them with `aws apigateway get-rest-apis --region <region>` and check each stage's access logging and execution logging settings with `aws apigateway get-stages --rest-api-id <api-id> --region <region>`. If another API uses it, hand the setting to that API's owner with `aws apigateway update-account --region <region> --patch-operations op=replace,path=/cloudwatchRoleArn,value=<their-role-arn>`, or leave it in place.
3. If the shared stack still exists, delete it; its resources are retained: `aws cloudformation delete-stack --stack-name CaseInsightsApiLoggingStack-<sharedId> --region <region>`.
4. Once nothing depends on the role, delete it: `aws iam delete-role-policy --role-name <role-name> --policy-name ApiGatewayAccessLogs`, then `aws iam delete-role --role-name <role-name>`. Deleting the role while the setting still names it breaks access logging for every API in the Region that relies on it.

In existing mode there is nothing of this solution's to clean up; the setting and role belong to their owner.

None of this has been exercised against a real account. The synthesised templates are checked locally (`test/cdk/stacks/api-logging-stack.test.ts`): one `AWS::ApiGateway::Account` and one API Gateway-trusted role in managed mode, none in existing mode or with MCP disabled, the role's trust and permissions, retention, the dependency of the MCP stack, identical dev and prod shared stacks and rejection of inconsistent settings. Delivery of access log records depends on the live account and is part of the deferred checklist.

## Tools

The server offers exactly eight tools, listed by `tools/list` in this order. All are annotated `readOnlyHint: true`, `destructiveHint: false` and `openWorldHint: false`; `analyze_with_bedrock` and `analyze_case_summaries` are also `idempotentHint: false` because a model can answer the same question differently. Nothing a tool does writes to the processed data, the Glue Data Catalog or AWS Support: Athena tools run queries in the fixed workgroup `CaseInsights-<uniqueId>` against the database `case_insights_<uniqueId>`, and Bedrock tools invoke only the configured `bedrockModelId`. Every input schema is closed (`additionalProperties: false`), so an unknown or misspelt argument is rejected before any AWS call.

### Common filters

Most tools accept the same optional filters. They are passed to Athena as `?` execution parameters, never inserted into SQL text.

| Argument | Format |
| --- | --- |
| `accountId` | 12 digits |
| `startDate`, `endDate` | `YYYY-MM-DD`, UTC calendar days on the case creation time (`timeCreated`); `endDate` is inclusive. Between 2000-01-01 and 2099-12-31, `endDate` not before `startDate`, at most 36,600 days apart |
| `serviceCode` | AWS Support service code such as `amazon-dynamodb`: lower-case letters, digits and hyphens, 1 to 128 characters |
| `severityCode` | `low`, `normal`, `high`, `urgent` or `critical` |
| `rcaCategory` | One RCA label or `Unclassified` (see [prompts.md](prompts.md)) |
| `lifecycleCategory` | One lifecycle label or `Unclassified` |

### Tool reference

| Tool | Calls | Arguments (in addition to the filters shown) | Returns |
| --- | --- | --- | --- |
| `query_athena` | Athena | Either `sql` (1 to 8,000 characters, one read-only statement) or `queryExecutionId` (a UUID returned earlier), never both; `nextToken` (requires `queryExecutionId`); `maxRows` 1 to 1,000, default 100 | `status` `SUCCEEDED` with `columns`, `rows`, `rowCount`, `truncated`, `truncationReason`, `nextToken`, `dataScannedBytes` and `engineExecutionTimeMillis`; or `status` `PENDING` with the `queryExecutionId` to call again |
| `analyze_with_bedrock` | Bedrock | `question` (required, 1 to 2,000 characters); `data` (up to 60,000 characters, for example rows from another tool) | `analysis`, `modelId`, `usage` (`inputTokens`, `outputTokens`), `stopReason`, `outputTruncated` |
| `get_case_summary` | Athena | All filters except `rcaCategory` | The RCA distribution: every category and `Unclassified` with `cases` and `percentOfCases`, plus `totalCases` and the applied `filters`. It does not look up individual cases |
| `analyze_case_summaries` | Athena, then Bedrock | `startDate` and `endDate` (required), the other filters, `maxCases` 1 to 200 (default 100), optional `question` (1 to 2,000 characters) | `status` `ANALYSED` with the analysis, or `NO_CASES`; `matchingCases`, `casesAnalysed` and `selectionTruncated`. The most recent matching cases are sent with case number, account, creation time, service, severity, subject, categories and summary, but not the requester identity |
| `get_temporal_patterns` | Athena | All filters; `dimensions`: one to three distinct values of `dayOfWeek`, `weekOfMonth`, `monthly` (default all three) | Case counts by ISO day of week (1 is Monday), by week of month (days 1-7, 8-14, 15-21, 22-28, 29-31) and by month (`yyyy-MM`, with months without cases filled with zero), all in UTC |
| `get_rca_analysis` | Athena | All filters except `rcaCategory`; `groupBy` `service` (default), `severity`, `account`, `month` or `lifecycleCategory`; `limit` 1 to 50 groups (default 10) | RCA counts per group, with `otherGroups`, `otherGroupCases` and `truncated` for the groups beyond the limit |
| `get_lifecycle_analysis` | Athena | All filters except `lifecycleCategory`; `groupBy` `service` (default), `severity`, `account`, `month` or `rcaCategory`; `limit` 1 to 50 (default 10) | Lifecycle counts per group, with the same overflow fields |
| `get_service_trends` | Athena | All filters; `granularity` `month` (default), `week` (periods start on Monday) or `day`; `topServices` 1 to 50 (default 10) | Case counts per service, period and RCA category for the services with most cases, with `totalServices` and `truncated` |

Typical questions: "Which services drive most of our cases?" (`get_service_trends`, or `get_rca_analysis` grouped by service), "What were the top root causes this quarter?" (`get_case_summary` with `startDate` and `endDate`), "Do more cases start on Mondays?" (`get_temporal_patterns` with `dayOfWeek`) and "Summarise last month's cases" (`analyze_case_summaries`).

The Bedrock tools send the question and data with a system prompt that marks the content inside `<data>` or `<cases>` tags as data, not instructions. Treat their output as untrusted model text, and remember that each call incurs model charges.

### Read-only SQL rules

`query_athena` checks the SQL before it is sent to Athena, and the fixed workgroup and the function's IAM role (Analytics workgroup, case summary table and processed records only) still limit what a statement could reach. The statement must be:

- exactly one statement, with at most one trailing semicolon, no comments (`--` or `/* */`), no `?` placeholders, no backticks, no non-ASCII characters outside string literals, no `U&` escapes and no empty quoted identifiers;
- `SELECT` or `WITH ... SELECT` (optionally in parentheses), or one of `SHOW TABLES [FROM|IN <database>]`, `SHOW COLUMNS FROM|IN <table> [FROM|IN <database>]`, `SHOW PARTITIONS <table>` and `DESCRIBE [EXTENDED|FORMATTED] <table> [<column>]` for the configured database and table;
- free of data-changing and session keywords in queries (for example `INSERT`, `CREATE`, `DROP`, `UNLOAD`, `MSCK`, `SET`, `PREPARE`, `EXECUTE` and `CALL`), recursive CTEs and qualified function calls;
- reading only `case_summary`, `<database>.case_summary`, `awsdatacatalog.<database>.case_summary` or a CTE defined in scope, in every `FROM` and `JOIN`, including subqueries, CTE bodies and comma joins (`UNNEST` and `LATERAL` are allowed in parentheses);
- at most 4,000 tokens and 32 levels of nesting.

A rejected statement returns `INVALID_ARGUMENTS` with `errors[{path: "sql", message, reason}]`. `reason` is a stable code such as `stacked_statement`, `comment`, `forbidden_keyword`, `table_not_allowed`, `statement_not_allowed`, `recursive_cte`, `qualified_function` or `too_complex`; the message never repeats the SQL.

The workgroup enforces its own settings (`EnforceWorkGroupConfiguration`): results go to its results bucket with SSE-S3, and a query that scans more than `operations.athena.bytesScannedCutoffPerQuery` (default 10 GiB) is cancelled. See [Workgroup and query results](athena.md#workgroup-and-query-results).

### Limits and time budget

- **Request budget.** Each `tools/call` has 25 seconds, less if the Lambda function has less time left (minus one second), inside the function's 28-second timeout and API Gateway's 29-second limit. SDK retries are off; the tools make their own bounded attempts, and each Athena or Bedrock call gets a connect and read timeout chosen to end before the deadline (`src/lambda/mcp_tools/limits.py`). If a response did not arrive in time, API Gateway returns `504`.
- **Time budget limitation.** botocore applies `read_timeout` to each socket read, not to the whole response. The 25-second budget therefore relies on the Athena APIs and the non-streaming Bedrock `InvokeModel` API sending their response at once, as they do; a response trickling in over many small reads could outlast the budget, in which case the function's 28-second timeout and API Gateway's 29-second limit still end the request.
- **`query_athena` paging.** A query still running when its budget ends returns `PENDING`; call again with `queryExecutionId`. Results are cut at `maxRows`, a page limit or the time budget (`truncationReason` `rowLimit`, `pageLimit` or `timeLimit`, each with a `nextToken`), or at the 256 KiB response limit (`responseSizeLimit`, no token: lower `maxRows` or select fewer columns). A resumed query must belong to the same workgroup, database and catalogue and pass the SQL rules again, otherwise `QUERY_NOT_FOUND`. Non-finite doubles are returned as the strings `"NaN"`, `"Infinity"` and `"-Infinity"`.
- **Fixed-query tools.** Results are limited to 5,000 rows and 1 MiB. A query still running at the budget is stopped and returns `QUERY_TIMEOUT`; results that cannot be read in time return `TIME_BUDGET_EXCEEDED`. Both are retryable; narrowing the date range usually helps.
- **`analyze_case_summaries`.** Athena may use up to 10 seconds, and the tool keeps at least 5.5 seconds for Bedrock. The rendered prompt is limited to 200,000 characters (`ANALYSIS_INPUT_TOO_LARGE`; lower `maxCases`).
- **Concurrency.** The function's reserved concurrency is 20, and the stage throttling and WAF rate rule described above apply.

### Tool errors

Argument errors, rejected SQL and controlled Athena or Bedrock failures are returned as a tool result with `isError: true` and `structuredContent.error` containing `code`, `message`, `retryable` and, where useful, `details`, so the client's model can correct the call. An unknown tool name is a JSON-RPC protocol error (`-32602`, `data.code` `UNKNOWN_TOOL`); anything unexpected is HTTP `500` with JSON-RPC error `-32603` and no detail. Messages never contain SQL, argument values, case content, prompts or model replies.

| Code | Meaning | Retryable |
| --- | --- | --- |
| `INVALID_ARGUMENTS` | Arguments failed the schema, or the SQL was rejected | No |
| `QUERY_FAILED` | Athena reported the query as failed; `details.athenaReason` (up to 500 characters) explains why and is returned to the caller but never logged | No |
| `QUERY_CANCELLED` | The query was cancelled, for example by the workgroup's bytes-scanned cutoff; `details.athenaReason` explains why | No |
| `QUERY_TIMEOUT` | A fixed query did not finish within the budget and was stopped | Yes |
| `QUERY_NOT_FOUND` | The `queryExecutionId` is unknown or belongs to another workgroup, database or catalogue | No |
| `INVALID_NEXT_TOKEN` | The `nextToken` does not belong to the query | No |
| `ATHENA_THROTTLED`, `BEDROCK_THROTTLED` | The service throttled the request | Yes |
| `ATHENA_ERROR` | Another Athena error, including a call timeout | Timeouts and transient errors only |
| `BEDROCK_ERROR` | Another Bedrock error, for example access denied | No |
| `BEDROCK_UNAVAILABLE` | Bedrock could not be reached or timed out | Yes |
| `BEDROCK_RESPONSE_INVALID` | The model's reply could not be read | No |
| `TIME_BUDGET_EXCEEDED` | Too little of the request budget was left to finish | Yes |
| `ANALYSIS_INPUT_TOO_LARGE` | The selected cases exceed the prompt limit | No |

### Verification status

The tools are tested locally against an SQLite-backed stand-in for Athena, botocore stubbers for Bedrock and a fake clock (`test/support/mcp_kit.py`, `test/lambda/mcp/`), and through the bridge and the cross-surface fixtures (`test/integration/test_cross_surface_mcp.py`). SQLite is not Trino, so the SQL dialect, Athena paging and real latency are not proven by these tests. Running each tool against a deployed dev environment is part of the deferred live checks.
## Finding the endpoint URL

```bash
aws cloudformation describe-stacks \
  --stack-name CaseInsightsMcpStack-<uniqueId> \
  --query 'Stacks[0].Outputs'
```

The URL has the form `https://<api-id>.execute-api.<region>.amazonaws.com/v1/mcp`.

## The SigV4 bridge

```bash
export MCP_SERVER_URL=https://<api-id>.execute-api.us-east-1.amazonaws.com/v1/mcp
export AWS_REGION=us-east-1
.venv/bin/python scripts/mcp_aws_client.py --help
```

The bridge reads one JSON-RPC message per line from stdin, POSTs it to `MCP_SERVER_URL` signed for the `execute-api` service in `AWS_REGION`, and writes the JSON-RPC reply to stdout as one line. Notifications receive no reply.

| Setting | Option | Default |
| --- | --- | --- |
| `MCP_SERVER_URL` | `--url` | Required. `https://` only; `http://` is accepted for loopback hosts so the bridge can be tested locally. |
| `AWS_REGION` | `--region` | Region in an `execute-api` host name, then the profile's Region. A value that contradicts the host name is rejected. |
| `AWS_PROFILE` | `--profile` | Default credential provider chain. |
| `AWS_CA_BUNDLE` | `--ca-bundle` | botocore's bundled certificate authorities. TLS verification cannot be disabled. |
| | `--timeout` | 30 seconds for connecting and for reading each response (1 to 120). |
| | `--max-concurrency` | 4 requests forwarded at once (1 to 16), so a long Athena query does not block `ping`. |
| | `--verbose` | Off. Adds one stderr line per message with its method, HTTP status and duration. |

Stdout carries MCP protocol messages only. Diagnostics go to stderr and never include credentials, signatures, request or response bodies, SQL, tool arguments or case content. When a request cannot be completed, the bridge answers it with a JSON-RPC error that keeps the request's `id`:

| Code | Meaning | `error.data` |
| --- | --- | --- |
| `-32000` | The endpoint returned an HTTP error that is not a JSON-RPC reply, for example an IAM `403` or a `429` throttle | `httpStatus`, `retryable`, `errorType` (API Gateway's `x-amzn-ErrorType`, when present) |
| `-32001` | The endpoint could not be reached, TLS verification failed or the response timed out | `errorType`, `retryable` |
| `-32002` | The endpoint's reply was not a valid JSON-RPC response | `httpStatus` |
| `-32003` | Credentials could not be obtained or refreshed | `errorType` |
| `-32600` | The message exceeds the endpoint's 32,768-byte limit, is a batch, or has a `method` that is not a string; it is not sent | |
| `-32700` | The line is not valid JSON; it is not sent | |
| `-32603` | Unexpected failure inside the bridge | |

When the bridge cannot tell which request an error belongs to (invalid JSON, a batch, a line over 1 MiB, or a message without a usable `id`), the error has no `id` member, as the MCP 2025-11-25 schema requires; it never sends `"id": null`. A usable `id` is an integer, including `0`, or a string that is valid Unicode text; booleans, arrays, objects, fractional numbers and strings containing an escaped unpaired surrogate such as `"\ud800"` are never echoed, by local errors or by endpoint errors the bridge relays. A reply whose `id` does not match the request's exactly is reported as `-32002`. Every request receives exactly one reply; an unexpected failure inside the bridge is answered with `-32603`.

JSON-RPC errors produced by the MCP function itself (for example `-32602` for an unknown tool) are passed through unchanged. The body of an API Gateway error is never copied, because signature errors can echo the canonical request, including the session token.

Exit status: `0` when stdin closes, `2` for invalid options or configuration, `3` when no credentials are found, the profile does not exist, the AWS configuration files cannot be parsed or the credential provider fails. A startup failure writes one stderr line that names only the error class, never the provider's own output.

## Kiro

1. Copy the example into place. `.kiro/settings/mcp.json` is ignored by Git.

   ```bash
   cp .kiro/settings/mcp.json.example .kiro/settings/mcp.json
   ```

2. Set `MCP_SERVER_URL` to the endpoint URL and `AWS_REGION` to its Region.
3. To use a named profile, add `"AWS_PROFILE": "<profile>"` to `env`. Otherwise the bridge uses the credentials in Kiro's environment.
4. Save the file. Kiro starts the server; open the MCP panel and confirm that eight tools are listed.

The example runs `.venv/bin/python scripts/mcp_aws_client.py`, with paths relative to the workspace root. If Kiro does not start the server from the workspace root, or you put the entry in your user-level `~/.kiro/settings/mcp.json`, replace both with absolute paths. On Windows use `.venv\Scripts\python.exe`.

`autoApprove` is empty, so Kiro asks before each tool call. `analyze_with_bedrock` and `analyze_case_summaries` invoke Amazon Bedrock and incur model charges; the other tools run Athena queries.

## Claude Desktop

Edit `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`; Claude Desktop's **Settings → Developer → Edit Config** opens it). Claude Desktop does not start servers in your checkout, so use absolute paths:

```json
{
  "mcpServers": {
    "support-case-insights": {
      "command": "/ABSOLUTE/PATH/TO/factorytestsample/.venv/bin/python",
      "args": ["/ABSOLUTE/PATH/TO/factorytestsample/scripts/mcp_aws_client.py"],
      "env": {
        "MCP_SERVER_URL": "https://<api-id>.execute-api.us-east-1.amazonaws.com/v1/mcp",
        "AWS_REGION": "us-east-1"
      }
    }
  }
}
```

Add `"AWS_PROFILE": "<profile>"` to `env` if your credentials are in a named profile. Restart Claude Desktop and check that the server's tools appear. Its MCP logs show the bridge's stderr diagnostics.

## Calling the endpoint with awscurl

[awscurl](https://github.com/okigan/awscurl) is a convenient way to check access without an MCP client. It is not a project dependency.

```bash
awscurl --service execute-api --region "$AWS_REGION" -X POST \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  "$MCP_SERVER_URL"
```

An unsigned request, for example with plain `curl`, must receive HTTP `403`.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| Bridge exits with status `2` | `MCP_SERVER_URL` is missing, not `https://`, or contradicts `AWS_REGION`. The stderr line names the problem. |
| Bridge exits with status `3` | No credentials found, the profile does not exist, the AWS config file is malformed, or a `credential_process` or other provider failed. Run `aws sts get-caller-identity` with the same profile, or `aws sso login`, to see the provider's full error. |
| `-32000`, `httpStatus` `403`, `errorType` `InvalidSignatureException` | Wrong `AWS_REGION`, or the local clock is more than five minutes out. |
| `-32000`, `httpStatus` `403`, `errorType` `AccessDeniedException` | Your principal is not in `mcpAllowedPrincipals`, lacks `execute-api:Invoke`, or is outside the Organization. |
| `-32000`, `httpStatus` `403`, `errorType` `ExpiredTokenException` or `UnrecognizedClientException` | Temporary credentials expired or are invalid. Refresh them. |
| `-32000`, `httpStatus` `403`, `errorType` `MissingAuthenticationTokenException` or `404` | The URL path is wrong; it must end in `/v1/mcp`. |
| `-32000`, `httpStatus` `429` | API Gateway or WAF rate limits. Retry later. |
| `-32001` | Network, proxy (`HTTPS_PROXY`) or TLS problem, or a response took longer than `--timeout`. Long Athena queries return `PENDING` from `query_athena`; call it again with the `queryExecutionId`. |
| No tools in the client | Check the client's MCP log for the bridge's stderr lines, then run the bridge by hand with `--verbose` and send an `initialize` message. |

## Local verification

`test/scripts/test_mcp_aws_client.py` runs the bridge as a subprocess and drives `initialize`, `notifications/initialized`, `tools/list`, `tools/call` and `ping` through it to a loopback HTTPS server that independently verifies each SigV4 signature (`execute-api` service, configured Region, signed session token) before passing the request to the real MCP Lambda handler with synthetic data. The tests also cover credential sources, HTTP and connection failures, oversized and invalid messages, stdout and stderr hygiene, and the Kiro and Claude Desktop examples above:

```bash
.venv/bin/pytest test/scripts/test_mcp_aws_client.py
```

These tests prove the bridge's behaviour, not API Gateway IAM authorisation, the resource policy or WAF, which are checked only as synthesised CloudFormation (`test/cdk/stacks/mcp-stack.test.ts`) until a live deployment. The deferred live checks are: `aws apigateway get-account` shows the shared role (managed mode) or the configured existing role, and the access log group receives records; a signed `tools/list` from an allowed principal succeeds; unsigned requests, unlisted principals and principals outside the Organization receive `403`; a 32,769-byte body receives `413` from WAF while a 32,768-byte body reaches the function; requests above the per-IP rate receive `429`; and the MCP alarms and dashboard rows show data.

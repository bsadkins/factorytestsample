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
| | `--timeout` | 30 seconds for connecting and for reading each response (1–120). |
| | `--max-concurrency` | 4 requests forwarded at once (1–16), so a long Athena query does not block `ping`. |
| | `--verbose` | Off. Adds one stderr line per message with its method, HTTP status and duration. |

Stdout carries MCP protocol messages only. Diagnostics go to stderr and never include credentials, signatures, request or response bodies, SQL, tool arguments or case content. When a request cannot be completed, the bridge answers it with a JSON-RPC error that keeps the request's `id`:

| Code | Meaning | `error.data` |
| --- | --- | --- |
| `-32000` | The endpoint returned an HTTP error that is not a JSON-RPC reply, for example an IAM `403` or a `429` throttle | `httpStatus`, `retryable`, `errorType` (API Gateway's `x-amzn-ErrorType`, when present) |
| `-32001` | The endpoint could not be reached, TLS verification failed or the response timed out | `errorType`, `retryable` |
| `-32002` | The endpoint's reply was not a valid JSON-RPC response | `httpStatus` |
| `-32003` | Credentials could not be obtained or refreshed | `errorType` |
| `-32600` | The message exceeds the endpoint's 32,768-byte limit, or is a batch; it is not sent | |
| `-32700` | The line is not valid JSON; it is not sent | |

JSON-RPC errors produced by the MCP function itself (for example `-32602` for an unknown tool) are passed through unchanged. The body of an API Gateway error is never copied, because signature errors can echo the canonical request, including the session token.

Exit status: `0` when stdin closes, `2` for invalid options or configuration, `3` when no credentials are found.

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
| Bridge exits with status `3` | No credentials found or the profile does not exist. Run `aws sts get-caller-identity` with the same profile, or `aws sso login`. |
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

These tests prove the bridge's behaviour, not API Gateway IAM authorisation, the resource policy or WAF, which are checked only as synthesised CloudFormation until a live deployment.

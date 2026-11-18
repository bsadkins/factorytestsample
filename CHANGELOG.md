# Changelog

All notable changes to this project are documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version is the `version` field of `package.json`.

## [Unreleased]

Nothing yet.

## [0.1.0] - 2026-11-18

First version of Support Case Insights. Nothing in this release has been deployed to AWS; the live checks are listed in the [README](README.md#deferred-live-verification).

### Added

- Core stack `CaseInsightsCoreStack-<uniqueId>`: scheduled account discovery through AWS Organizations, case retrieval and annotation of resolved cases through AWS Support, three-stage analysis (summary, root cause analysis and operational lifecycle classification) with Amazon Bedrock global cross-Region inference profiles (Claude Haiku 4.5 by default, Claude Sonnet 4.5 as the only alternative) in a Standard Step Functions workflow, S3 raw and processed buckets, Powertools idempotency in DynamoDB, failure records, daily cleanup and an hourly daily-completion heartbeat.
- Member role stack with the service-managed StackSet `CaseInsights-MemberRole-<uniqueId>`, which deploys the read-only `Support-Case-Analysis-Role-<uniqueId>` to every member account in the configured root or organisational units.
- Optional Analytics stack: Glue database and `case_summary` table, partition crawler, and an Athena workgroup with an enforced results location and scan limit.
- Optional MCP stack: API Gateway REST endpoint `POST /v1/mcp` with IAM (SigV4) authorisation, a resource policy limited to listed principals in the organisation, AWS WAF, and a Lambda function with eight read-only tools; the local SigV4 stdio bridge `scripts/mcp_aws_client.py` with Kiro and Claude Desktop examples.
- Shared stack `CaseInsightsApiLoggingStack-<sharedId>` that owns the account and Region API Gateway CloudWatch role setting in managed mode, with an existing-role mode.
- Optional approval-gated CDK pipeline (`-c pipeline=true`) that deploys dev, then prod after a manual approval, from `main` through AWS CodeConnections.
- CloudWatch dashboard, 23 alarms (27 with MCP) and the alarm topic `CaseInsights-Alarms-<uniqueId>` with optional email subscription.
- CSV export (`scripts/generate_csv_insights.py`), dead-letter queue redrive (`scripts/redrive-dlq.sh`) and opt-in reprocessing (`scripts/reprocess-cases.py`).
- Schema-validated configuration for dev and prod, cdk-nag on every synthesis, local Docker-free Python asset bundling from hash-locked wheels, and the `npm run verify` and `npm run security` commands.
- Documentation set: architecture with diagram, installation, configuration, permissions, security, resources, usage, Athena, CSV export, MCP server, prompts, monitoring, logging, runbook, troubleshooting, development, cost and pipeline guides.

### Security

- All data stores use AWS managed or AWS owned encryption (SSE-S3, SSE-SQS, DynamoDB default encryption, CloudWatch Logs default encryption, Athena `SSE_S3`); no customer managed KMS keys are used. The alarm topic is not encrypted at rest, by explicit approval, because CloudWatch alarms can publish to an encrypted topic only with a customer managed KMS key; it enforces TLS and carries no case content. See [Security](docs/security.md#alarm-sns-topic-exception).

[Unreleased]: https://github.com/bsadkins/factorytestsample/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/bsadkins/factorytestsample/releases/tag/v0.1.0

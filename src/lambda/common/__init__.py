"""Shared helpers for Support Case Insights Lambda functions.

The package is published in the shared Lambda layer at `/opt/python/common`. Modules:

- `taxonomy`: the single source of RCA and lifecycle categories and definitions.
- `templates`: prompt template loading and rendering from `/opt/templates`.
- `json_extract`, `classification`, `summary`: defensive model-output parsing and validation.
- `bedrock`: Anthropic messages requests to the approved global inference profiles.
- `retry`: bounded exponential backoff with jitter.
- `sts`: member-role assumption.
- `s3_json`, `processed`: S3 JSON objects and processed-record keys, including
  `get_existing_cases_batch`.
- `sqs`: batched sends and SQS event-record parsing.
- `safe_logging`: log fields that never carry payloads or error messages.
- `analysis`, `checkpoints`, `classifier_stage`, `processed_record`: the CaseAnalysis workflow
  stage contract, durable stage checkpoints, the shared classifier stage and the 18-field
  processed record.
"""

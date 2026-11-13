"""Run one Lambda handler with X-Ray tracing enabled and report what its traces contain.

Usage: `python -m handler_trace_probe <handler>` in a fresh interpreter (tracing is configured
when Powertools Tracer is created at import) with PYTHONPATH covering src/lambda and
test/support. Every AWS SDK call the handler makes is answered, below the X-Ray botocore
instrumentation, with a service error whose message carries an invented sentinel, so the
automatic SDK subsegments record a real `ClientError`. Trace documents go to an in-memory
emitter and sockets are blocked, so nothing leaves the process. Prints one JSON report as the
last line on stdout.
"""

from __future__ import annotations

import importlib
import json
import socket
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, Final

from aws_xray_sdk.core import xray_recorder  # type: ignore[import-untyped]
from aws_xray_sdk.core.context import Context  # type: ignore[import-untyped]
from aws_xray_sdk.core.models.entity import Entity  # type: ignore[import-untyped]
from botocore.awsrequest import AWSResponse, HTTPHeaders
from botocore.client import BaseClient

SENTINEL: Final = "SENTINEL-HANDLER-TRACE-5e1f"
ERROR_CODE: Final = "SyntheticServiceError"
ERROR_MESSAGE: Final = f"Request content echoed by the service: {SENTINEL}"
SCRUBBER_FLAG: Final = "_exception_messages_withheld"

ACCOUNT: Final = "111122223333"
MANAGEMENT: Final = "999999999999"
DISPLAY: Final = "170000000001"
CASE_ID: Final = "case-111122223333-2026-abcdef01"
RUN: Final = "20261113T000000Z"
QUEUE: Final = "https://sqs.us-east-1.amazonaws.com/123456789012"
CASE_KEY: Final = f"account_number={ACCOUNT}/case_number={DISPLAY}"

# The Powertools settings every deployed function receives (SAFE_POWERTOOLS_ENVIRONMENT in
# lib/constructs/pipeline-function.ts); with them Powertools adds no error or response metadata.
DEPLOYED_POWERTOOLS_SETTINGS: Final = {
    "POWERTOOLS_LOG_LEVEL": "INFO",
    "POWERTOOLS_LOGGER_LOG_EVENT": "false",
    "POWERTOOLS_LOGGER_SAMPLE_RATE": "0",
    "POWERTOOLS_TRACER_CAPTURE_RESPONSE": "false",
    "POWERTOOLS_TRACER_CAPTURE_ERROR": "false",
}

ENVIRONMENT: Final = {
    "AWS_ACCESS_KEY_ID": "synthetic-access-key",
    "AWS_SECRET_ACCESS_KEY": "synthetic-secret-key",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_REGION": "us-east-1",
    "AWS_EC2_METADATA_DISABLED": "true",
    "AWS_XRAY_SDK_ENABLED": "true",
    "LAMBDA_TASK_ROOT": "/var/task",
    "POWERTOOLS_TRACE_DISABLED": "false",
    "POWERTOOLS_SERVICE_NAME": "handler-trace-probe",
    "POWERTOOLS_METRICS_NAMESPACE": "SyntheticHandlerTraceProbe",
    **DEPLOYED_POWERTOOLS_SETTINGS,
    "MANAGEMENT_ACCOUNT_ID": MANAGEMENT,
    "ACCOUNT_LIST_BUCKET": "synthetic-account-lists",
    "RAW_BUCKET": "synthetic-raw",
    "PROCESSED_BUCKET": "synthetic-processed",
    "ACTIVE_ACCOUNTS_QUEUE_URL": f"{QUEUE}/active-accounts",
    "ANNOTATION_QUEUE_URL": f"{QUEUE}/annotation",
    "SUMMARY_QUEUE_URL": f"{QUEUE}/summary",
    "FAILURE_QUEUE_URL": f"{QUEUE}/workflow-failures",
    "MEMBER_ROLE_NAME": "Synthetic-Case-Analysis-Role",
    "LOOKBACK_MONTHS": "12",
    "CLEANUP_GRACE_HOURS": "24",
    "RUN_LOOKBACK_HOURS": "48",
    "CLAIM_SETTLE_SECONDS": "0",
    "STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:CaseAnalysis-probe",
    "IDEMPOTENCY_TABLE_NAME": "synthetic-idempotency",
    "BEDROCK_MODEL_ID": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "BEDROCK_MAX_TOKENS": "1000",
    "ATHENA_WORKGROUP": "CaseInsights-synthetic",
    "ATHENA_DATABASE": "case_insights_synthetic",
}
UNSET: Final = (
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_ENDPOINT_URL",
    "AWS_SAM_LOCAL",
    "AWS_CHALICE_CLI_MODE",
    "_X_AMZN_TRACE_ID",
)

ANNOTATION_MESSAGE: Final = {
    "accountId": ACCOUNT,
    "caseId": CASE_ID,
    "displayId": DISPLAY,
    "runId": RUN,
    "rawKey": f"{CASE_KEY}/data.json",
}
SUMMARY_MESSAGE: Final = {
    **ANNOTATION_MESSAGE,
    "communicationsKey": f"{CASE_KEY}/communications.json",
    "communicationsVersionId": "synthetic-version",
    "communicationCount": 2,
}


def _sqs_event(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "Records": [
            {
                "messageId": "m0",
                "receiptHandle": "r0",
                "body": json.dumps(body),
                "attributes": {"ApproximateReceiveCount": "1"},
                "messageAttributes": {},
                "md5OfBody": "",
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:us-east-1:123456789012:queue",
                "awsRegion": "us-east-1",
            }
        ]
    }


def _mcp_event() -> dict[str, Any]:
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "query_athena",
            "arguments": {"sql": "SELECT account_id FROM case_summary"},
        },
    }
    return {
        "httpMethod": "POST",
        "headers": {"Content-Type": "application/json", "MCP-Protocol-Version": "2025-11-25"},
        "body": json.dumps(message),
        "isBase64Encoded": False,
    }


WORKFLOW_EVENT: Final = {"case": SUMMARY_MESSAGE}

# Every Lambda handler module that creates a Powertools Tracer, with an event that reaches its
# first AWS SDK call.
HANDLERS: Final[dict[str, Callable[[], dict[str, Any]]]] = {
    "account_lookup": lambda: {"scheduledTime": "2026-11-13T00:00:00+00:00"},
    "account_reader": lambda: {
        "Records": [
            {
                "eventSource": "aws:s3",
                "eventName": "ObjectCreated:Put",
                "s3": {
                    "bucket": {"name": ENVIRONMENT["ACCOUNT_LIST_BUCKET"]},
                    "object": {"key": f"account-lists/{RUN}.json", "versionId": "v1"},
                },
            }
        ]
    },
    "case_retrieval": lambda: _sqs_event(
        {"accountId": ACCOUNT, "accountName": "Synthetic member", "runId": RUN}
    ),
    "case_annotation": lambda: _sqs_event(ANNOTATION_MESSAGE),
    "workflow_starter": lambda: _sqs_event(SUMMARY_MESSAGE),
    "case_summary": lambda: dict(WORKFLOW_EVENT),
    "case_rca": lambda: dict(WORKFLOW_EVENT),
    "case_lifecycle": lambda: dict(WORKFLOW_EVENT),
    "update_case_metadata": lambda: dict(WORKFLOW_EVENT),
    "handle_failure": lambda: {
        **WORKFLOW_EVENT,
        "error": {"Error": "States.TaskFailed", "Cause": "{}"},
    },
    "run_completion": dict,
    "case_cleanup": dict,
    "mcp_server": _mcp_event,
}


def probe_environment(base: dict[str, str]) -> dict[str, str]:
    """`base` with the probe's synthetic configuration applied."""
    environment = {key: value for key, value in base.items() if key not in UNSET}
    environment.update(ENVIRONMENT)
    return environment


def _block_network() -> list[str]:
    attempts: list[str] = []

    def blocked(*_: object, **__: object) -> None:
        attempts.append("blocked")
        raise OSError("network access is disabled in the trace probe")

    socket.create_connection = blocked  # type: ignore[assignment]
    socket.getaddrinfo = blocked  # type: ignore[assignment]
    for method in ("connect", "connect_ex", "send", "sendall", "sendto"):
        setattr(socket.socket, method, blocked)
    return attempts


def _answer_with_service_errors() -> list[str]:
    """Replace the HTTP exchange of every SDK client with a sentinel-bearing service error.

    `_make_request` runs inside the `_make_api_call` wrapper that X-Ray installs, so the error
    is recorded on the automatic SDK subsegment exactly as a real service error would be.
    """
    operations: list[str] = []

    def make_request(
        self: BaseClient,
        operation_model: Any,  # noqa: ANN401
        request_dict: object,
        request_context: object,
    ) -> tuple[AWSResponse, dict[str, Any]]:
        del self, request_dict, request_context
        operations.append(operation_model.name)
        parsed = {
            "Error": {"Code": ERROR_CODE, "Message": ERROR_MESSAGE},
            "ResponseMetadata": {"HTTPStatusCode": 400, "RequestId": "synthetic-request"},
        }
        return AWSResponse("https://synthetic.invalid", 400, HTTPHeaders(), None), parsed

    setattr(BaseClient, "_make_request", make_request)  # noqa: B010 - private botocore hook
    return operations


class InMemoryEmitter:
    """Collects serialised trace documents instead of sending them to the X-Ray daemon."""

    def __init__(self) -> None:
        self.documents: list[dict[str, Any]] = []

    def send_entity(self, entity: Any) -> bool:  # noqa: ANN401
        self.documents.append(json.loads(entity.serialize()))
        return True

    def set_daemon_address(self, address: str | None) -> None:
        """Unused: nothing is sent to a daemon."""


def _context(name: str) -> Any:  # noqa: ANN401
    return SimpleNamespace(
        function_name=f"{name}-trace-probe",
        memory_limit_in_mb=512,
        invoked_function_arn=f"arn:aws:lambda:us-east-1:123456789012:function:{name}-probe",
        aws_request_id="synthetic-request",
        function_version="$LATEST",
        log_group_name=f"/aws/lambda/{name}-trace-probe",
        log_stream_name="stream",
        get_remaining_time_in_millis=lambda: 120_000,
    )


def _scrubber_installed() -> bool:
    return bool(getattr(Entity.add_exception, SCRUBBER_FLAG, False))


def run(name: str) -> dict[str, Any]:
    """Import and invoke handler `name` once; return the trace report."""
    attempts = _block_network()
    operations = _answer_with_service_errors()
    installed_before = _scrubber_installed()
    app = importlib.import_module(f"{name}.app")
    installed_after = _scrubber_installed()

    emitter = InMemoryEmitter()
    xray_recorder.configure(
        context=Context(),
        sampling=False,
        emitter=emitter,
        context_missing="RUNTIME_ERROR",
        service="handler-trace-probe",
    )
    xray_recorder.begin_segment(f"{name}-trace-probe", sampling=True)
    outcome: dict[str, Any]
    try:
        response = app.handler(HANDLERS[name](), _context(name))
    except Exception as exc:  # the report records how the handler ended; the test judges it
        outcome = {"raised": type(exc).__name__}
    else:
        outcome = {"returned": response}
    xray_recorder.end_segment()
    return {
        "handler": name,
        "tracerDisabled": app.tracer.disabled,
        "scrubberInstalledBeforeImport": installed_before,
        "scrubberInstalledAfterImport": installed_after,
        "sdkOperations": operations,
        "outcome": outcome,
        "documents": emitter.documents,
        "networkAttempts": len(attempts),
    }


def main() -> None:
    (name,) = sys.argv[1:]
    if name not in HANDLERS:
        raise SystemExit(f"unknown handler: {name}")
    sys.stdout.write(json.dumps(run(name), default=str) + "\n")


if __name__ == "__main__":
    main()

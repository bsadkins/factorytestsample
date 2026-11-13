"""Run the MCP Lambda handler with X-Ray tracing enabled and report what the traces contain.

Run in a fresh interpreter (tracing is configured when Powertools Tracer is created at import)
with PYTHONPATH covering src/lambda and test/support. The handler's real botocore clients are
auto-instrumented by the X-Ray SDK and answered by Stubber with service errors whose messages
carry invented sentinels; every trace document goes to an in-memory emitter and sockets are
blocked, so nothing leaves the process. Prints one JSON report on stdout.
"""

from __future__ import annotations

import json
import socket
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, Final

import boto3
from aws_xray_sdk.core import xray_recorder  # type: ignore[import-untyped]
from aws_xray_sdk.core.context import Context  # type: ignore[import-untyped]
from botocore.stub import Stubber
from mcp_server import app
from mcp_tools.context import CachedClients, McpClients, McpSettings

SQL_SENTINEL: Final = "SENTINEL-TRACE-SQL-5ca1ab1e"
PROBE_SQL: Final = "SELECT 'SENTINEL-TRACE-SQL-5ca1ab1e' AS label FROM case_summary"
ATHENA_SENTINEL: Final = "SENTINEL-TRACE-ATHENA-0ddba11"
QUESTION_SENTINEL: Final = "SENTINEL-TRACE-QUESTION-feed"
DATA_SENTINEL: Final = "SENTINEL-TRACE-CASE-TEXT-beef"
BEDROCK_SENTINEL: Final = "SENTINEL-TRACE-BEDROCK-cafe"
SENTINELS: Final = (
    SQL_SENTINEL,
    ATHENA_SENTINEL,
    QUESTION_SENTINEL,
    DATA_SENTINEL,
    BEDROCK_SENTINEL,
)

ENVIRONMENT: Final = {
    "AWS_ACCESS_KEY_ID": "synthetic-access-key",
    "AWS_SECRET_ACCESS_KEY": "synthetic-secret-key",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_EC2_METADATA_DISABLED": "true",
    "AWS_XRAY_SDK_ENABLED": "true",
    "LAMBDA_TASK_ROOT": "/var/task",
    "POWERTOOLS_TRACE_DISABLED": "false",
    "POWERTOOLS_SERVICE_NAME": "mcp-trace-probe",
    "POWERTOOLS_METRICS_NAMESPACE": "SyntheticMcpTraceProbe",
    "POWERTOOLS_LOGGER_LOG_EVENT": "false",
    "ATHENA_WORKGROUP": "CaseInsights-synthetic",
    "ATHENA_DATABASE": "case_insights_synthetic",
    "BEDROCK_MODEL_ID": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "BEDROCK_MAX_TOKENS": "1000",
}
UNSET: Final = (
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_SESSION_TOKEN",
    "AWS_ENDPOINT_URL",
    "AWS_ENDPOINT_URL_ATHENA",
    "AWS_ENDPOINT_URL_BEDROCK_RUNTIME",
    "AWS_SAM_LOCAL",
    "AWS_CHALICE_CLI_MODE",
    "_X_AMZN_TRACE_ID",
)


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


class InMemoryEmitter:
    """Collects serialised trace documents instead of sending them to the X-Ray daemon."""

    def __init__(self) -> None:
        self.documents: list[dict[str, Any]] = []

    def send_entity(self, entity: Any) -> bool:  # noqa: ANN401
        self.documents.append(json.loads(entity.serialize()))
        return True

    def set_daemon_address(self, address: str | None) -> None:
        """Unused: nothing is sent to a daemon."""


def _event(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "httpMethod": "POST",
        "headers": {"Content-Type": "application/json", "MCP-Protocol-Version": "2025-11-25"},
        "body": json.dumps(message),
        "isBase64Encoded": False,
    }


def _context() -> Any:  # noqa: ANN401
    return SimpleNamespace(
        function_name="mcp-trace-probe",
        memory_limit_in_mb=512,
        invoked_function_arn="arn:aws:lambda:us-east-1:123456789012:function:mcp-trace-probe",
        aws_request_id="synthetic-request",
        function_version="$LATEST",
        get_remaining_time_in_millis=lambda: 28_000,
    )


def main() -> None:
    attempts = _block_network()
    emitter = InMemoryEmitter()
    xray_recorder.configure(
        context=Context(),
        sampling=False,
        emitter=emitter,
        context_missing="RUNTIME_ERROR",
        service="mcp-trace-probe",
    )
    athena = boto3.client("athena", region_name="us-east-1")
    bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")
    clients = McpClients(
        athena=CachedClients(lambda _timeout: athena),
        bedrock=CachedClients(lambda _timeout: bedrock),
    )
    settings = McpSettings.from_env()
    app.runtime = lambda: (settings, clients)  # type: ignore[assignment]

    scenarios: list[tuple[str, dict[str, Any], Callable[[], None]]] = [
        (
            "athena",
            {"name": "query_athena", "arguments": {"sql": PROBE_SQL}},
            lambda: athena_stubber.add_client_error(
                "start_query_execution",
                service_error_code="InvalidRequestException",
                service_message=f"line 1:8: column '{SQL_SENTINEL}' {ATHENA_SENTINEL}",
                http_status_code=400,
            ),
        ),
        (
            "bedrock",
            {
                "name": "analyze_with_bedrock",
                "arguments": {"question": QUESTION_SENTINEL, "data": DATA_SENTINEL},
            },
            lambda: bedrock_stubber.add_client_error(
                "invoke_model",
                service_error_code="ValidationException",
                service_message=f"Malformed input: {DATA_SENTINEL} {BEDROCK_SENTINEL}",
                http_status_code=400,
            ),
        ),
    ]
    report: dict[str, Any] = {"tracerDisabled": app.tracer.disabled, "scenarios": {}}
    with Stubber(athena) as athena_stubber, Stubber(bedrock) as bedrock_stubber:
        for request_id, (name, params, arrange) in enumerate(scenarios, start=1):
            arrange()
            emitter.documents.clear()
            xray_recorder.begin_segment(f"mcp-trace-probe-{name}", sampling=True)
            message = {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}
            response = app.handler(_event(message), _context())
            xray_recorder.end_segment()
            report["scenarios"][name] = {
                "response": response,
                "documents": list(emitter.documents),
            }
        athena_stubber.assert_no_pending_responses()
        bedrock_stubber.assert_no_pending_responses()
    report["networkAttempts"] = len(attempts)
    sys.stdout.write(json.dumps(report) + "\n")


if __name__ == "__main__":
    main()

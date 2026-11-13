"""MCP server Lambda function: stateless JSON-RPC over the API Gateway REST endpoint `/v1/mcp`.

API Gateway authenticates callers with SigV4/IAM before this function runs. The function then
enforces the transport rules and 32 KiB body limit (`mcp_server.transport`), the MCP lifecycle
and method dispatch (`mcp_server.server`) and the eight read-only tools with their argument
schemas (`mcp_tools.registry`). Settings and AWS clients are created once per container and only
when a tool actually runs, so `initialize` and `tools/list` never need them.

Logs, traces and metrics carry bounded labels only: the method (known names or `unknown`), the
tool name when it is registered, the outcome, status and error code. Request bodies, IDs,
arguments, SQL, case content and model replies are never logged.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from common.safe_logging import error_fields
from mcp_tools.context import McpClients, McpSettings, ToolContext
from mcp_tools.registry import build_registry

from mcp_server.server import McpServer, Outcome, internal_error
from mcp_server.transport import Rejected, http_response, read_request

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()
tracer = Tracer()
metrics = Metrics()

REGISTRY: Final = build_registry()


@cache
def runtime() -> tuple[McpSettings, McpClients]:
    """Validated settings and AWS clients, built on the first tool call in this container."""
    return McpSettings.from_env(), McpClients.create()


def tool_context(remaining_millis: int | None) -> ToolContext:
    """Per-call tool context bounded by the request budget and the function's remaining time."""
    settings, clients = runtime()
    return ToolContext.start(settings, clients, remaining_millis=remaining_millis)


def _dispatch(event: object, context: LambdaContext) -> Outcome:
    try:
        request = read_request(event)
    except Rejected as rejected:
        return rejected.outcome
    message = request.message
    server = McpServer(REGISTRY, lambda: tool_context(context.get_remaining_time_in_millis()))
    try:
        return server.handle(message, request.protocol_header)
    except Exception as exc:
        outcome = internal_error(message.request_id, "unknown", None)
        outcome.failure = exc
        return outcome


def _record(outcome: Outcome) -> None:
    metrics.add_metric(name="McpRequests", unit=MetricUnit.Count, value=1)
    if outcome.tool is not None and outcome.kind in {"ok", "tool_error", "internal_error"}:
        metrics.add_metric(name="McpToolCalls", unit=MetricUnit.Count, value=1)
    if outcome.tool_duration_ms is not None:
        metrics.add_metric(
            name="McpToolDuration",
            unit=MetricUnit.Milliseconds,
            value=outcome.tool_duration_ms,
        )
    error_metric = {
        "protocol_error": "McpProtocolErrors",
        "tool_error": "McpToolErrors",
        "internal_error": "McpInternalErrors",
    }.get(outcome.kind)
    if error_metric is not None:
        metrics.add_metric(name=error_metric, unit=MetricUnit.Count, value=1)
        metrics.add_metric(name="McpErrors", unit=MetricUnit.Count, value=1)

    tracer.put_annotation("mcp_method", outcome.method)
    tracer.put_annotation("mcp_outcome", outcome.kind)
    fields: dict[str, Any] = {
        "mcp_method": outcome.method,
        "mcp_outcome": outcome.kind,
        "status_code": outcome.status,
    }
    if outcome.tool is not None:
        tracer.put_annotation("mcp_tool", outcome.tool)
        fields["mcp_tool"] = outcome.tool
    if outcome.error_code is not None:
        fields["mcp_error_code"] = outcome.error_code
    if outcome.tool_duration_ms is not None:
        fields["tool_duration_ms"] = round(outcome.tool_duration_ms, 1)
    if outcome.failure is not None:
        fields.update(error_fields(outcome.failure))
        logger.error("MCP request failed unexpectedly", extra=fields)
    elif outcome.kind in {"ok", "accepted"}:
        logger.info("MCP request handled", extra=fields)
    else:
        logger.warning("MCP request rejected", extra=fields)


@logger.inject_lambda_context(log_event=False, clear_state=True)
@tracer.capture_lambda_handler(capture_response=False, capture_error=False)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """API Gateway REST proxy handler; always returns a proxy response."""
    outcome = _dispatch(event, context)
    _record(outcome)
    return http_response(outcome)

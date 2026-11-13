"""MCP lifecycle and method dispatch over the eight read-only tools.

The server is stateless: every HTTP request carries one JSON-RPC message, no session ID is
issued, and `initialize` simply negotiates the protocol version and advertises the `tools`
capability. Supported methods are `initialize`, `ping`, `tools/list` and `tools/call`; accepted
notifications are acknowledged without a response. Every other method is METHOD_NOT_FOUND.

`tools/call` maps outcomes as the MCP specification describes: an unknown tool name or malformed
`tools/call` params are protocol errors (INVALID_PARAMS); argument validation failures, rejected
SQL and controlled Athena/Bedrock failures are tool results with `isError: true`, so a model can
correct itself; anything unexpected is an opaque INTERNAL_ERROR.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from mcp_tools.context import ToolContext
from mcp_tools.errors import ToolError, UnknownToolError
from mcp_tools.registry import ToolRegistry

from mcp_server.jsonrpc import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    Message,
    RequestId,
    error_response,
    result_response,
)

# Pinned MCP revisions. Both use the same JSON-RPC rules (no batching) and tool semantics, so a
# client requesting either gets it echoed; any other requested version is answered with the
# latest, and the client decides whether to continue.
LATEST_PROTOCOL_VERSION: Final = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS: Final = (LATEST_PROTOCOL_VERSION, "2025-06-18")

SERVER_NAME: Final = "support-case-insights"
SERVER_VERSION: Final = "0.1.0"
INSTRUCTIONS: Final = (
    "Read-only analysis of processed AWS Support cases. Use the analytical tools for RCA, "
    "lifecycle, temporal and service trends; query_athena accepts a single read-only SELECT, "
    "SHOW, DESCRIBE or WITH statement over the case_summary table."
)

ACCEPTED_NOTIFICATIONS: Final = frozenset(
    {
        "notifications/initialized",
        "notifications/cancelled",
        "notifications/progress",
        "notifications/roots/list_changed",
    }
)
KNOWN_METHODS: Final = frozenset({"initialize", "ping", "tools/list", "tools/call"})
_META_ONLY: Final = frozenset({"_meta"})
_TOOLS_CALL_PARAMS: Final = frozenset({"name", "arguments", "_meta"})

OutcomeKind = Literal["ok", "accepted", "protocol_error", "tool_error", "internal_error"]


@dataclass(slots=True)
class Outcome:
    """The reply to one message plus bounded, value-free telemetry labels.

    `method` is a known method name, `notification`, `response` or `unknown`; `tool` is set only
    for registered tool names. `error_code` is a JSON-RPC code or a tool error code. `failure`
    is the unexpected exception behind an internal error, for type-only logging.
    """

    status: int
    response: dict[str, Any] | None
    kind: OutcomeKind
    method: str = "unknown"
    tool: str | None = None
    error_code: str | None = None
    tool_duration_ms: float | None = None
    failure: BaseException | None = None


def protocol_error(  # noqa: PLR0913
    status: int,
    request_id: RequestId | None,
    code: int,
    message: str,
    *,
    method: str = "unknown",
    data: Mapping[str, Any] | None = None,
) -> Outcome:
    return Outcome(
        status=status,
        response=error_response(request_id, code, message, data),
        kind="protocol_error",
        method=method,
        error_code=str(code),
    )


class ParamsError(Exception):
    """Request params do not match the method's parameter shape. Carries a fixed message."""


def _object_params(message: Message, allowed: frozenset[str] | None) -> dict[str, Any]:
    if not message.has_params:
        return {}
    params = message.params
    if not isinstance(params, dict):
        raise ParamsError("params must be an object")
    if allowed is not None and set(params) - allowed:
        raise ParamsError("params has unsupported members")
    if "_meta" in params and not isinstance(params["_meta"], dict):
        raise ParamsError("_meta must be an object")
    return params


def tool_result(payload: Mapping[str, Any], *, is_error: bool) -> dict[str, Any]:
    """A CallToolResult carrying `payload` as structured content and serialised JSON text.

    Serialisation is strict JSON: a NaN or Infinity in `payload` raises instead of producing a
    body that standards-compliant clients cannot parse.
    """
    structured = dict(payload)
    text = json.dumps(structured, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
        "isError": is_error,
    }


class McpServer:
    """Dispatches classified messages. `context_factory` builds the per-call tool context and is
    only invoked for `tools/call` with a registered tool name and well-formed params."""

    def __init__(
        self,
        registry: ToolRegistry,
        context_factory: Callable[[], ToolContext],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.registry = registry
        self.context_factory = context_factory
        self.clock = clock

    def handle(self, message: Message, protocol_header: str | None) -> Outcome:
        if message.kind == "response":
            return Outcome(status=202, response=None, kind="accepted", method="response")
        if message.kind == "notification":
            return self._notification(message)
        return self._request(message, protocol_header)

    def _request(self, message: Message, protocol_header: str | None) -> Outcome:
        method = str(message.method)
        label = method if method in KNOWN_METHODS else "unknown"
        if (
            method != "initialize"
            and protocol_header is not None
            and protocol_header not in SUPPORTED_PROTOCOL_VERSIONS
        ):
            return protocol_error(
                400,
                message.request_id,
                INVALID_REQUEST,
                "Unsupported MCP-Protocol-Version header",
                method=label,
                data={"supported": list(SUPPORTED_PROTOCOL_VERSIONS)},
            )
        request_id = message.request_id
        assert request_id is not None  # noqa: S101 - classify() guarantees requests have an ID
        methods: dict[str, Callable[[Message, RequestId], Outcome]] = {
            "initialize": self._initialize,
            "ping": self._ping,
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
        }
        run = methods.get(method)
        if run is None:
            return protocol_error(200, request_id, METHOD_NOT_FOUND, "Method not found")
        try:
            return run(message, request_id)
        except ParamsError as exc:
            return protocol_error(200, request_id, INVALID_PARAMS, str(exc), method=label)

    def _ping(self, message: Message, request_id: RequestId) -> Outcome:
        _object_params(message, _META_ONLY)
        return Outcome(200, result_response(request_id, {}), "ok", "ping")

    def _notification(self, message: Message) -> Outcome:
        method = str(message.method)
        if method not in ACCEPTED_NOTIFICATIONS:
            return protocol_error(400, None, METHOD_NOT_FOUND, "Notification not supported")
        if message.has_params and not isinstance(message.params, dict):
            return protocol_error(
                400, None, INVALID_PARAMS, "params must be an object", method="notification"
            )
        return Outcome(status=202, response=None, kind="accepted", method="notification")

    def _initialize(self, message: Message, request_id: RequestId) -> Outcome:
        if not message.has_params:
            raise ParamsError("initialize requires params")
        # Unknown initialize members are ignored so newer clients can still negotiate.
        params = _object_params(message, None)
        requested = params.get("protocolVersion")
        if not isinstance(requested, str):
            raise ParamsError("protocolVersion must be a string")
        if not isinstance(params.get("capabilities"), dict):
            raise ParamsError("capabilities must be an object")
        client = params.get("clientInfo")
        if not (
            isinstance(client, dict)
            and isinstance(client.get("name"), str)
            and isinstance(client.get("version"), str)
        ):
            raise ParamsError("clientInfo must have string name and version")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": SERVER_NAME,
                "title": "Support Case Insights",
                "version": SERVER_VERSION,
            },
            "instructions": INSTRUCTIONS,
        }
        return Outcome(200, result_response(request_id, result), "ok", "initialize")

    def _tools_list(self, message: Message, request_id: RequestId) -> Outcome:
        params = _object_params(message, frozenset({"cursor", "_meta"}))
        if "cursor" in params:
            # Every tool is returned in one page, so no cursor is ever issued.
            raise ParamsError("Invalid cursor")
        result = {"tools": self.registry.list_tools()}
        return Outcome(200, result_response(request_id, result), "ok", "tools/list")

    def _tools_call(self, message: Message, request_id: RequestId) -> Outcome:
        params = _object_params(message, _TOOLS_CALL_PARAMS)
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise ParamsError("name must be a non-empty string")
        # CallToolRequest arguments are optional but not nullable: only omission means {}.
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ParamsError("arguments must be an object")
        if name not in self.registry.names():
            return protocol_error(
                200,
                request_id,
                INVALID_PARAMS,
                "Unknown tool",
                method="tools/call",
                data={"code": UnknownToolError.code},
            )
        started = self.clock()
        try:
            outcome = self._run_tool(name, arguments, request_id)
        except Exception as exc:
            outcome = internal_error(request_id, "tools/call", name)
            outcome.failure = exc
        outcome.tool_duration_ms = (self.clock() - started) * 1000
        return outcome

    def _run_tool(self, name: str, arguments: object, request_id: RequestId) -> Outcome:
        try:
            payload = self.registry.call(name, arguments, self.context_factory())
            result = tool_result(payload, is_error=False)
            kind: OutcomeKind = "ok"
            error_code = None
        except ToolError as exc:
            result = tool_result({"error": exc.to_payload()}, is_error=True)
            kind = "tool_error"
            error_code = exc.code
        return Outcome(
            status=200,
            response=result_response(request_id, result),
            kind=kind,
            method="tools/call",
            tool=name,
            error_code=error_code,
        )


def internal_error(request_id: RequestId | None, method: str, tool: str | None) -> Outcome:
    outcome = protocol_error(500, request_id, INTERNAL_ERROR, "Internal error", method=method)
    outcome.kind = "internal_error"
    outcome.tool = tool
    return outcome

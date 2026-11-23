"""MCP JSON-RPC handler: the actual Lambda entry point driven with API Gateway REST proxy events
through a realistic client exchange (initialize, notifications/initialized, tools/list,
tools/call), every tool dispatched with synthetic data, and controlled protocol, transport,
body-size and telemetry failures. All data is invented; no real AWS calls are made."""

from __future__ import annotations

import base64
import io
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from mcp_kit import McpKit, dataset, make_kit
from mcp_server import app
from mcp_server.jsonrpc import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    Message,
)
from mcp_server.server import LATEST_PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS, McpServer
from mcp_server.transport import MAX_BODY_BYTES
from mcp_tools.context import ToolContext
from mcp_tools.limits import McpLimits
from mcp_tools.registry import build_registry

EXPECTED_TOOLS = [
    "query_athena",
    "analyze_with_bedrock",
    "get_case_summary",
    "analyze_case_summaries",
    "get_temporal_patterns",
    "get_rca_analysis",
    "get_lifecycle_analysis",
    "get_service_trends",
]
SENTINEL = "SENTINEL-MCP-c0ffee"
JSON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

VALID_CALLS: dict[str, dict[str, Any]] = {
    "query_athena": {"sql": "SELECT count(*) AS n FROM case_summary"},
    "analyze_with_bedrock": {"question": "Which themes recur?", "data": "Invented notes."},
    "get_case_summary": {"serviceCode": "amazon-s3"},
    "analyze_case_summaries": {"startDate": "2026-01-01", "endDate": "2026-01-31"},
    "get_temporal_patterns": {},
    "get_rca_analysis": {"groupBy": "severity"},
    "get_lifecycle_analysis": {"accountId": "111122223333"},
    "get_service_trends": {"granularity": "week"},
}

# Per tool: a missing required argument (where the schema has one), a wrong type and an
# unsupported argument. Each must be rejected before any Athena or Bedrock call.
INVALID_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("query_athena", {}),
    ("query_athena", {"sql": 42}),
    ("query_athena", {"sql": "SELECT 1", "workGroup": "primary"}),
    ("analyze_with_bedrock", {}),
    ("analyze_with_bedrock", {"question": ["q"]}),
    ("analyze_with_bedrock", {"question": "q", "modelId": "anthropic.claude-3-opus"}),
    ("get_case_summary", {"accountId": 111122223333}),
    ("get_case_summary", {"caseNumber": "1001"}),
    ("analyze_case_summaries", {"startDate": "2026-01-01"}),
    ("analyze_case_summaries", {"startDate": 20260101, "endDate": "2026-01-31"}),
    ("analyze_case_summaries", {"startDate": "2026-01-01", "endDate": "2026-01-31", "x": 1}),
    ("get_temporal_patterns", {"dimensions": "monthly"}),
    ("get_temporal_patterns", {"interval": "hourly"}),
    ("get_rca_analysis", {"limit": "10"}),
    ("get_rca_analysis", {"sql": "SELECT 1"}),
    ("get_lifecycle_analysis", {"accountId": ["111122223333"]}),
    ("get_lifecycle_analysis", {"lifecycleCategory": "Monitoring"}),
    ("get_service_trends", {"topServices": 2.5}),
    ("get_service_trends", {"database": "other"}),
]


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    body: str

    def json(self) -> Any:  # noqa: ANN401
        return json.loads(self.body)


@dataclass
class Harness:
    kit: McpKit
    context: Callable[..., LambdaContext]
    log_stream: io.StringIO
    remaining: list[int | None]

    def post(  # noqa: PLR0913
        self,
        body: object,
        *,
        headers: dict[str, str] | None = None,
        http_method: str = "POST",
        encode: bool = True,
        b64: bool = False,
        raw_event: dict[str, Any] | None = None,
    ) -> Reply:
        if raw_event is not None:
            event = raw_event
        else:
            text = json.dumps(body) if encode else body
            if b64:
                data = text if isinstance(text, bytes) else str(text).encode()
                text = base64.b64encode(data).decode()
            event = {
                "resource": "/mcp",
                "path": "/mcp",
                "httpMethod": http_method,
                "headers": dict(JSON_HEADERS if headers is None else headers),
                "multiValueHeaders": None,
                "queryStringParameters": None,
                "requestContext": {"stage": "v1", "requestId": "req-1"},
                "body": text,
                "isBase64Encoded": b64,
            }
        result = app.handler(event, self.context())
        assert set(result) == {"statusCode", "headers", "body", "isBase64Encoded"}
        assert result["isBase64Encoded"] is False
        # The Python Lambda runtime serialises the proxy response with ensure_ascii=False and
        # sends it as UTF-8, so every response must survive that encoding.
        json.dumps(result, ensure_ascii=False).encode("utf-8")
        return Reply(result["statusCode"], result["headers"], result["body"])

    def rpc(self, method: str, params: object = None, request_id: object = 1, **kw: Any) -> Reply:  # noqa: ANN401
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        return self.post(message, **kw)

    def call(self, tool: str, arguments: object, request_id: object = 7) -> Reply:
        return self.rpc("tools/call", {"name": tool, "arguments": arguments}, request_id)

    def logs(self) -> str:
        return self.log_stream.getvalue()


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, make_context: Callable[..., LambdaContext]) -> Harness:
    stream = io.StringIO()
    service_logger = logging.getLogger("handler-test")
    for log_handler in service_logger.handlers:
        if isinstance(log_handler, logging.StreamHandler):
            monkeypatch.setattr(log_handler, "stream", stream)
    kit = make_kit(dataset())
    remaining: list[int | None] = []

    def tool_context(remaining_millis: int | None) -> ToolContext:
        remaining.append(remaining_millis)
        return kit.context(remaining_millis)

    monkeypatch.setattr(app, "tool_context", tool_context)
    return Harness(kit=kit, context=make_context, log_stream=stream, remaining=remaining)


def initialize_params(version: object = LATEST_PROTOCOL_VERSION) -> dict[str, Any]:
    return {
        "protocolVersion": version,
        "capabilities": {"roots": {"listChanged": True}},
        "clientInfo": {"name": "synthetic-client", "version": "1.0.0"},
    }


def assert_error_response_schema(document: dict[str, Any]) -> None:
    """MCP 2025-11-25 schema.ts: `JSONRPCErrorResponse { jsonrpc: "2.0"; id?: RequestId;
    error: Error }` with `RequestId = string | number`. The id is optional but never null
    (basic/index: error responses carry the request's ID "except in error cases where the ID
    could not be read due a malformed request")."""
    assert set(document) <= {"jsonrpc", "id", "error"}
    assert document["jsonrpc"] == "2.0"
    if "id" in document:
        request_id = document["id"]
        assert isinstance(request_id, (str, int))
        assert not isinstance(request_id, bool)
    error = document["error"]
    assert isinstance(error["code"], int)
    assert not isinstance(error["code"], bool)
    assert isinstance(error["message"], str)


def assert_error(reply: Reply, status: int, code: int, request_id: object) -> dict[str, Any]:
    """`request_id=None` means the request ID could not be read, so the error has no `id`."""
    assert reply.status == status, reply.body
    assert reply.headers["Content-Type"] == "application/json"
    document = reply.json()
    assert_error_response_schema(document)
    if request_id is None:
        assert "id" not in document
    else:
        assert document["id"] == request_id
    assert "result" not in document
    assert document["error"]["code"] == code
    assert isinstance(document["error"]["message"], str)
    return dict(document["error"])


def tool_result(reply: Reply, request_id: object) -> dict[str, Any]:
    assert reply.status == 200, reply.body
    document = reply.json()
    assert document["jsonrpc"] == "2.0"
    assert document["id"] == request_id
    assert "error" not in document
    result = document["result"]
    assert result["content"][0]["type"] == "text"
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    return dict(result)


# Lifecycle -------------------------------------------------------------------------------------


def test_realistic_client_exchange_discovers_and_calls_tools(harness: Harness) -> None:
    init = harness.rpc("initialize", initialize_params(), request_id=1)
    assert init.status == 200
    assert init.json() == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": init.json()["result"]["serverInfo"],
            "instructions": init.json()["result"]["instructions"],
        },
    }
    assert init.json()["result"]["serverInfo"]["name"] == "support-case-insights"
    assert "Mcp-Session-Id" not in init.headers

    session = {**JSON_HEADERS, "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION}
    initialized = harness.post(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=session
    )
    assert initialized.status == 202
    assert initialized.body == ""

    listed = harness.rpc("tools/list", {}, request_id="list-1", headers=session)
    assert listed.status == 200
    document = listed.json()
    assert document["id"] == "list-1"
    tools = document["result"]["tools"]
    assert [tool["name"] for tool in tools] == EXPECTED_TOOLS
    assert "nextCursor" not in document["result"]
    for tool in tools:
        assert len(tool["description"]) >= 80
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["annotations"]["readOnlyHint"] is True

    called = harness.rpc(
        "tools/call",
        {"name": "get_case_summary", "arguments": {"serviceCode": "amazon-s3"}},
        request_id=3,
        headers=session,
    )
    result = tool_result(called, 3)
    assert result["isError"] is False
    assert result["structuredContent"]["totalCases"] == 4
    assert harness.kit.athena.operations().count("StartQueryExecution") == 1
    # The Lambda function's remaining time bounds the tool call.
    assert harness.remaining == [120_000]


@pytest.mark.parametrize("request_id", [0, -5, 2**53, "", "abc-123", "\u00e9\u20ac"])
def test_request_ids_are_preserved(harness: Harness, request_id: object) -> None:
    reply = harness.rpc("ping", request_id=request_id)
    assert reply.json() == {"jsonrpc": "2.0", "id": request_id, "result": {}}


@pytest.mark.parametrize("version", SUPPORTED_PROTOCOL_VERSIONS)
def test_supported_protocol_version_is_echoed(harness: Harness, version: str) -> None:
    reply = harness.rpc("initialize", initialize_params(version))
    assert reply.json()["result"]["protocolVersion"] == version


@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "1.0.0", "2099-01-01"])
def test_unsupported_protocol_version_negotiates_latest(harness: Harness, version: str) -> None:
    reply = harness.rpc("initialize", initialize_params(version))
    assert reply.status == 200
    assert reply.json()["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION


@pytest.mark.parametrize(
    "params",
    [
        None,
        [],
        {},
        {"capabilities": {}, "clientInfo": {"name": "c", "version": "1"}},
        initialize_params(20251125),
        {**initialize_params(), "capabilities": []},
        {**initialize_params(), "clientInfo": {"name": "c"}},
        {**initialize_params(), "clientInfo": "c"},
    ],
)
def test_invalid_initialize_params_are_rejected(harness: Harness, params: object) -> None:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": 9, "method": "initialize"}
    if params is not None:
        message["params"] = params
    assert_error(harness.post(message), 200, INVALID_PARAMS, 9)


@pytest.mark.parametrize("header", ["2024-11-05", "2025-03-26", "garbage", ""])
def test_unsupported_protocol_header_is_rejected_before_dispatch(
    harness: Harness, header: str
) -> None:
    headers = {**JSON_HEADERS, "MCP-Protocol-Version": header}
    reply = harness.rpc(
        "tools/call", {"name": "get_case_summary", "arguments": {}}, 4, headers=headers
    )
    error = assert_error(reply, 400, INVALID_REQUEST, 4)
    assert header not in error["message"] or not header
    assert harness.kit.external_calls() == 0


def test_missing_protocol_header_is_accepted(harness: Harness) -> None:
    reply = harness.rpc("tools/list", request_id=2)
    assert reply.status == 200
    assert len(reply.json()["result"]["tools"]) == 8


def test_ping_and_tools_list_reject_unknown_params(harness: Harness) -> None:
    assert harness.rpc("ping", {"_meta": {"progressToken": 1}}).json()["result"] == {}
    assert_error(harness.rpc("ping", {"extra": 1}, 5), 200, INVALID_PARAMS, 5)
    assert_error(harness.rpc("tools/list", {"cursor": "abc"}, 6), 200, INVALID_PARAMS, 6)
    assert_error(harness.rpc("tools/list", {"limit": 2}, 6), 200, INVALID_PARAMS, 6)
    assert_error(harness.rpc("tools/list", [], 6), 200, INVALID_PARAMS, 6)


@pytest.mark.parametrize(
    "method", ["resources/list", "prompts/list", "tools/delete", "completion/complete", "Ping"]
)
def test_unknown_methods_are_method_not_found(harness: Harness, method: str) -> None:
    error = assert_error(harness.rpc(method, {}, "m-1"), 200, METHOD_NOT_FOUND, "m-1")
    assert method not in json.dumps(error)


# Notifications and client responses -----------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3}},
        {"jsonrpc": "2.0", "method": "notifications/roots/list_changed"},
        {"jsonrpc": "2.0", "id": 5, "result": {}},
        {"jsonrpc": "2.0", "id": "x", "error": {"code": -32601, "message": "nope"}},
        # 2025-11-25 JSONRPCErrorResponse: a client error response may omit the id.
        {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}},
    ],
)
def test_notifications_and_responses_get_no_jsonrpc_response(
    harness: Harness, message: dict[str, Any]
) -> None:
    reply = harness.post(message)
    assert reply.status == 202
    assert reply.body == ""
    assert harness.kit.external_calls() == 0


@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "get_case_summary"}},
        {"jsonrpc": "2.0", "method": "tools/list"},
        {"jsonrpc": "2.0", "method": "notifications/unknown"},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": []},
    ],
)
def test_unacceptable_notifications_are_rejected_without_work(
    harness: Harness, message: dict[str, Any]
) -> None:
    reply = harness.post(message)
    assert reply.status == 400
    # 2025-11-25 transports: a rejected notification's body "MAY comprise a JSON-RPC error
    # response that has no id".
    assert_error_response_schema(reply.json())
    assert "id" not in reply.json()
    assert reply.json()["error"]["code"] in {INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS}
    assert harness.kit.external_calls() == 0


# Envelope and parse errors --------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "{",
        "",
        "   ",
        "not json",
        '{"jsonrpc": "2.0", "id": 1, "method": "ping"',
        "[1,",
        '{"a": NaN}',
    ],
)
def test_malformed_json_is_a_parse_error(harness: Harness, body: str) -> None:
    reply = harness.post(body, encode=False)
    error = assert_error(reply, 400, PARSE_ERROR, None)
    assert "data" not in error or SENTINEL not in json.dumps(error)


def test_unreadable_id_error_matches_the_pinned_schema_exactly(harness: Harness) -> None:
    """MCP 2025-11-25 schema.ts defines `JSONRPCErrorResponse.id?: RequestId` where
    `RequestId = string | number`: an error whose request ID cannot be read omits `id`;
    `id: null` is not valid under the pinned revision."""
    headers = {**JSON_HEADERS, "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION}
    reply = harness.post("{", encode=False, headers=headers)
    assert reply.status == 400
    assert reply.json() == {
        "jsonrpc": "2.0",
        "error": {"code": PARSE_ERROR, "message": "Parse error"},
    }


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({**JSON_HEADERS, "Origin": "https://attacker.example"}, 403),
        ({**JSON_HEADERS, "Content-Type": "text/plain"}, 415),
        ({**JSON_HEADERS, "Accept": "text/html"}, 406),
    ],
)
def test_transport_rejections_carry_no_id(
    harness: Harness, headers: dict[str, str], status: int
) -> None:
    """2025-11-25 transports: the 403 for an invalid Origin "MAY comprise a JSON-RPC error
    response that has no id"; the other pre-dispatch rejections follow the same shape."""
    reply = harness.rpc("ping", request_id=21, headers=headers)
    assert_error(reply, status, INVALID_REQUEST, None)


def test_parse_errors_do_not_echo_the_body(harness: Harness) -> None:
    reply = harness.post('{"jsonrpc": "2.0", "id": 1, "method": "' + SENTINEL, encode=False)
    assert_error(reply, 400, PARSE_ERROR, None)
    assert SENTINEL not in reply.body
    assert SENTINEL not in harness.logs()


@pytest.mark.parametrize(
    ("message", "echoed_id"),
    [
        ([{"jsonrpc": "2.0", "id": 1, "method": "ping"}], None),
        ([], None),
        ("ping", None),
        (42, None),
        (None, None),
        ({"id": 1, "method": "ping"}, 1),
        ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, 1),
        ({"jsonrpc": 2.0, "id": 1, "method": "ping"}, 1),
        ({"jsonrpc": "2.0", "id": None, "method": "ping"}, None),
        ({"jsonrpc": "2.0", "id": 1.5, "method": "ping"}, None),
        ({"jsonrpc": "2.0", "id": True, "method": "ping"}, None),
        ({"jsonrpc": "2.0", "id": {"a": 1}, "method": "ping"}, None),
        ({"jsonrpc": "2.0", "id": [1], "method": "ping"}, None),
        ({"jsonrpc": "2.0", "id": 1, "method": 5}, 1),
        ({"jsonrpc": "2.0", "id": 1, "method": ""}, 1),
        ({"jsonrpc": "2.0", "id": 1, "method": "ping", "extra": True}, 1),
        ({"jsonrpc": "2.0", "id": 1}, 1),
        ({"jsonrpc": "2.0", "id": 1, "result": {}, "error": {"code": 1, "message": "m"}}, 1),
        ({"jsonrpc": "2.0", "id": 1, "method": "ping", "result": {}}, 1),
        ({"jsonrpc": "2.0", "result": {}}, None),
        ({"jsonrpc": "2.0", "id": 1, "error": "bad"}, 1),
        # 2025-11-25 schema.ts: request, result and error ids are RequestId, never null.
        ({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "m"}}, None),
        ({"jsonrpc": "2.0", "id": None, "result": {}}, None),
        ({"jsonrpc": "2.0", "error": "bad"}, None),
        ({"jsonrpc": "2.0", "error": {"code": -32600, "message": "m"}, "data": 1}, None),
    ],
)
def test_invalid_envelopes_are_invalid_requests(
    harness: Harness, message: object, echoed_id: object
) -> None:
    assert_error(harness.post(message), 400, INVALID_REQUEST, echoed_id)
    assert harness.kit.external_calls() == 0


def test_request_without_id_is_rejected_explicitly() -> None:
    # The guard must not rely on assert, which python -O strips.
    def no_context() -> ToolContext:
        pytest.fail("no tool context may be built for an id-less request")

    server = McpServer(build_registry(), no_context)
    message = Message(kind="request", method="ping", request_id=None, params=None, has_params=False)
    outcome = server.handle(message, None)
    assert outcome.status == 400
    assert outcome.kind == "protocol_error"
    assert outcome.response is not None
    assert outcome.response["error"]["code"] == INVALID_REQUEST
    assert "id" not in outcome.response


def test_params_must_be_an_object(harness: Harness) -> None:
    reply = harness.rpc("tools/call", ["get_case_summary", {}], request_id=11)
    assert_error(reply, 200, INVALID_PARAMS, 11)
    assert harness.kit.external_calls() == 0


def test_duplicate_keys_are_rejected(harness: Harness) -> None:
    body = '{"jsonrpc": "2.0", "id": 1, "method": "ping", "method": "tools/list"}'
    assert_error(harness.post(body, encode=False), 400, PARSE_ERROR, None)


def test_deeply_nested_json_is_rejected_safely(harness: Harness) -> None:
    body = "[" * 16_000 + "]" * 16_000
    assert len(body) <= MAX_BODY_BYTES
    assert_error(harness.post(body, encode=False), 400, PARSE_ERROR, None)


# tools/call -----------------------------------------------------------------------------------


@pytest.mark.parametrize("tool", EXPECTED_TOOLS)
def test_every_advertised_tool_is_callable_through_dispatch(harness: Harness, tool: str) -> None:
    if tool in {"analyze_with_bedrock", "analyze_case_summaries"}:
        harness.kit.bedrock.reply("Invented analysis.")
    result = tool_result(
        harness.call(tool, VALID_CALLS[tool], request_id=f"call-{tool}"), f"call-{tool}"
    )
    assert result["isError"] is False
    assert result["structuredContent"]
    harness.kit.bedrock.assert_consumed()
    assert harness.kit.external_calls() >= 1


@pytest.mark.parametrize(
    ("tool", "arguments"), INVALID_CALLS, ids=[f"{t}-{i}" for i, (t, _) in enumerate(INVALID_CALLS)]
)
def test_invalid_tool_arguments_fail_before_external_calls(
    harness: Harness, tool: str, arguments: dict[str, Any]
) -> None:
    result = tool_result(harness.call(tool, arguments, request_id=21), 21)
    assert result["isError"] is True
    error = result["structuredContent"]["error"]
    assert error["code"] == "INVALID_ARGUMENTS"
    assert error["errors"]
    assert harness.kit.external_calls() == 0


def test_rejected_sql_is_a_tool_error_without_athena_calls(harness: Harness) -> None:
    result = tool_result(harness.call("query_athena", {"sql": "DROP TABLE case_summary"}, 8), 8)
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "INVALID_ARGUMENTS"
    assert harness.kit.external_calls() == 0


def test_out_of_scope_table_behind_nested_cte_is_rejected_before_athena(harness: Harness) -> None:
    harness.kit.athena.db.execute("CREATE TABLE secrets (x TEXT)")
    harness.kit.athena.db.execute("INSERT INTO secrets VALUES ('SENTINEL-OUTSIDE-SCOPE')")
    sql = (
        "SELECT * FROM secrets WHERE EXISTS (WITH secrets AS (SELECT 1 AS x) SELECT x FROM secrets)"
    )
    reply = harness.call("query_athena", {"sql": sql}, request_id=31)
    result = tool_result(reply, 31)
    assert result["isError"] is True
    error = result["structuredContent"]["error"]
    assert error["code"] == "INVALID_ARGUMENTS"
    assert [issue["reason"] for issue in error["errors"]] == ["table_not_allowed"]
    assert "SENTINEL-OUTSIDE-SCOPE" not in reply.body
    assert harness.kit.external_calls() == 0


def _no_constants(token: str) -> object:
    raise ValueError(f"non-standard JSON constant {token}")


def strict_json(text: str) -> Any:  # noqa: ANN401
    return json.loads(text, parse_constant=_no_constants)


def test_non_finite_athena_doubles_produce_strict_json(harness: Harness) -> None:
    harness.kit.athena.next_result = (
        [("n", "double"), ("p", "double"), ("m", "double")],
        [["NaN", "Infinity", "-Infinity"]],
    )
    reply = harness.call(
        "query_athena", {"sql": "SELECT CAST('NaN' AS DOUBLE) AS n FROM case_summary"}, 32
    )
    assert reply.status == 200
    document = strict_json(reply.body)
    structured = document["result"]["structuredContent"]
    assert document["result"]["isError"] is False
    assert structured["rows"] == [{"n": "NaN", "p": "Infinity", "m": "-Infinity"}]
    assert strict_json(document["result"]["content"][0]["text"]) == structured


@pytest.mark.parametrize(
    "payload",
    [
        {"content": [{"type": "text", "text": 123}]},
        {"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": "bad"}},
        {"content": [{"type": "image", "source": {}}]},
        {"usage": {"input_tokens": 1, "output_tokens": 1}},
    ],
)
def test_malformed_bedrock_reply_is_a_controlled_tool_error(
    harness: Harness, payload: dict[str, Any]
) -> None:
    harness.kit.bedrock.raw(json.dumps(payload).encode())
    reply = harness.call("analyze_with_bedrock", {"question": "q"}, request_id=33)
    result = tool_result(reply, 33)
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "BEDROCK_RESPONSE_INVALID"
    for leaked in ("TypeError", "ValueError", "Traceback", "int()"):
        assert leaked not in reply.body
        assert leaked not in harness.logs()


def test_explicit_null_arguments_are_rejected_before_any_external_work(harness: Harness) -> None:
    body = (
        '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        '"params":{"name":"get_case_summary","arguments":null}}'
    )
    error = assert_error(harness.post(body, encode=False), 200, INVALID_PARAMS, 1)
    assert error["message"] == "arguments must be an object"
    assert harness.kit.external_calls() == 0
    assert harness.remaining == []


def test_slow_athena_status_calls_stay_within_the_request_budget(harness: Harness) -> None:
    harness.kit.athena.polls_before_success = None
    harness.kit.athena.latency["GetQueryExecution"] = 7.0
    harness.kit.athena.honour_timeouts = False
    start = harness.kit.clock.now
    reply = harness.call("query_athena", {"sql": "SELECT count(*) AS n FROM case_summary"}, 34)
    result = tool_result(reply, 34)
    assert result["isError"] is False
    assert result["structuredContent"]["status"] == "PENDING"
    assert harness.kit.clock.now - start <= McpLimits().request_budget_seconds


def test_hanging_bedrock_call_through_the_handler_stays_within_the_request_budget(
    harness: Harness,
) -> None:
    harness.kit.bedrock.latency = 120.0
    harness.kit.bedrock.reply("unused")
    harness.kit.bedrock.reply("unused")
    start = harness.kit.clock.now
    reply = harness.call("analyze_with_bedrock", {"question": "q"}, request_id=35)
    result = tool_result(reply, 35)
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["retryable"] is True
    assert harness.kit.clock.now - start <= McpLimits().request_budget_seconds


def test_missing_arguments_default_to_empty_object(harness: Harness) -> None:
    reply = harness.rpc("tools/call", {"name": "get_case_summary"}, request_id=12)
    assert tool_result(reply, 12)["structuredContent"]["totalCases"] == 12


def test_unknown_tool_is_a_protocol_error_without_external_calls(harness: Harness) -> None:
    reply = harness.call(SENTINEL, {}, request_id="u-1")
    error = assert_error(reply, 200, INVALID_PARAMS, "u-1")
    assert error["data"] == {"code": "UNKNOWN_TOOL"}
    assert SENTINEL not in reply.body
    assert SENTINEL not in harness.logs()
    assert harness.kit.external_calls() == 0
    assert harness.remaining == []


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"name": 5},
        {"name": ""},
        {"name": "get_case_summary", "arguments": None},
        {"name": "get_case_summary", "arguments": []},
        {"name": "get_case_summary", "arguments": "x"},
        {"name": "get_case_summary", "arguments": {}, "extra": 1},
        {"name": "get_case_summary", "arguments": {}, "_meta": []},
    ],
)
def test_invalid_tools_call_params_are_rejected(harness: Harness, params: dict[str, Any]) -> None:
    assert_error(harness.rpc("tools/call", params, request_id=13), 200, INVALID_PARAMS, 13)
    assert harness.kit.external_calls() == 0
    assert harness.remaining == []


def test_tool_failure_is_a_safe_tool_error(harness: Harness) -> None:
    harness.kit.bedrock.error("ThrottlingException", 429, times=5)
    reply = harness.call("analyze_with_bedrock", {"question": SENTINEL}, request_id=14)
    result = tool_result(reply, 14)
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"].startswith("BEDROCK_")
    assert SENTINEL not in reply.body
    assert "SENTINEL-ERR" not in reply.body
    assert SENTINEL not in harness.logs()


def test_unexpected_tool_exception_is_an_opaque_internal_error(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(name: str, arguments: object, ctx: ToolContext) -> dict[str, Any]:
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(app.REGISTRY, "call", explode)
    reply = harness.call("get_case_summary", {}, request_id="boom")
    error = assert_error(reply, 500, INTERNAL_ERROR, "boom")
    assert error["message"] == "Internal error"
    assert SENTINEL not in reply.body
    assert SENTINEL not in harness.logs()
    assert "RuntimeError" in harness.logs()


def test_missing_configuration_fails_tool_calls_but_not_discovery(
    monkeypatch: pytest.MonkeyPatch, make_context: Callable[..., LambdaContext]
) -> None:
    for name in ("ATHENA_WORKGROUP", "ATHENA_DATABASE", "BEDROCK_MODEL_ID", "BEDROCK_MAX_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    app.runtime.cache_clear()
    harness = Harness(make_kit(), make_context, io.StringIO(), [])
    assert harness.rpc("initialize", initialize_params()).status == 200
    assert len(harness.rpc("tools/list").json()["result"]["tools"]) == 8
    reply = harness.call("get_case_summary", {}, request_id=15)
    assert_error(reply, 500, INTERNAL_ERROR, 15)
    app.runtime.cache_clear()


def test_default_tool_context_uses_function_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATHENA_WORKGROUP", "CaseInsights-mcp-test")
    monkeypatch.setenv("ATHENA_DATABASE", "case_insights_mcp_test")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "global.anthropic.claude-haiku-4-5-20251001-v1:0")
    monkeypatch.setenv("BEDROCK_MAX_TOKENS", "1000")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    app.runtime.cache_clear()
    try:
        ctx = app.tool_context(10_000)
        assert ctx.settings.workgroup == "CaseInsights-mcp-test"
        assert ctx.remaining() <= 9.0
    finally:
        app.runtime.cache_clear()


# HTTP transport -------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT", "OPTIONS"])
def test_only_post_is_allowed(harness: Harness, method: str) -> None:
    reply = harness.rpc("ping", http_method=method)
    assert reply.status == 405
    assert reply.headers["Allow"] == "POST"


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({**JSON_HEADERS, "Origin": "https://attacker.example"}, 403),
        ({**JSON_HEADERS, "origin": "null"}, 403),
        ({"Accept": "application/json"}, 415),
        ({**JSON_HEADERS, "Content-Type": "text/plain"}, 415),
        ({**JSON_HEADERS, "Content-Type": "application/json; charset=latin-1"}, 415),
        ({**JSON_HEADERS, "Accept": "text/html"}, 406),
        ({**JSON_HEADERS, "Accept": "text/event-stream"}, 406),
    ],
)
def test_transport_headers_are_enforced(
    harness: Harness, headers: dict[str, str], status: int
) -> None:
    reply = harness.rpc(
        "tools/call", {"name": "get_case_summary", "arguments": {}}, 16, headers=headers
    )
    assert reply.status == status
    assert reply.json()["error"]["code"] == INVALID_REQUEST
    assert harness.kit.external_calls() == 0


@pytest.mark.parametrize(
    "headers",
    [
        {"content-type": "application/json", "accept": "application/json, text/event-stream"},
        {"Content-Type": "Application/JSON; charset=UTF-8"},
        {"Content-Type": "application/json", "Accept": "*/*"},
        {"Content-Type": "application/json", "Accept": "application/*;q=0.8"},
    ],
)
def test_compatible_headers_are_accepted(harness: Harness, headers: dict[str, str]) -> None:
    assert harness.rpc("ping", headers=headers).status == 200


def test_multi_value_headers_are_honoured(harness: Harness) -> None:
    event = {
        "httpMethod": "POST",
        "headers": None,
        "multiValueHeaders": {
            "Content-Type": ["application/json"],
            "Origin": ["https://a.example", "https://b.example"],
        },
        "body": json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        "isBase64Encoded": False,
    }
    assert harness.post(None, raw_event=event).status in {400, 403}
    event["multiValueHeaders"] = {"Content-Type": ["application/json"]}
    assert harness.post(None, raw_event=event).status == 200


@pytest.mark.parametrize("event", [{}, {"httpMethod": "POST"}, {"httpMethod": "POST", "body": 5}])
def test_incomplete_events_are_rejected(harness: Harness, event: dict[str, Any]) -> None:
    event = {"headers": {"Content-Type": "application/json"}, **event}
    assert harness.post(None, raw_event=event).status in {400, 405}


# Body cap -------------------------------------------------------------------------------------


def padded_ping(size: int, filler: str = "\u20ac") -> str:
    """A valid ping whose UTF-8 encoding is exactly `size` bytes, padded with `filler`."""
    prefix = '{"jsonrpc":"2.0","id":"cap","method":"ping","params":{"_meta":{"pad":"'
    suffix = '"}}}'
    budget = size - len((prefix + suffix).encode())
    width = len(filler.encode())
    text = prefix + filler * (budget // width) + "a" * (budget % width) + suffix
    assert len(text.encode()) == size
    return text


@pytest.mark.parametrize("filler", ["a", "\u00e9", "\u20ac", "\U0001f600"])
def test_body_at_cap_is_accepted(harness: Harness, filler: str) -> None:
    reply = harness.post(padded_ping(MAX_BODY_BYTES, filler), encode=False)
    assert reply.status == 200
    assert reply.json() == {"jsonrpc": "2.0", "id": "cap", "result": {}}


@pytest.mark.parametrize("filler", ["a", "\u00e9", "\u20ac", "\U0001f600"])
def test_body_over_cap_is_rejected_by_bytes(harness: Harness, filler: str) -> None:
    body = padded_ping(MAX_BODY_BYTES + 1, filler)
    if filler != "a":
        assert len(body) < MAX_BODY_BYTES
    reply = harness.post(body, encode=False)
    assert_error(reply, 413, INVALID_REQUEST, None)


def test_oversized_tool_call_is_rejected_before_dispatch(harness: Harness) -> None:
    sql = "SELECT 1 -- " + "\u20ac" * 11_000
    reply = harness.call("query_athena", {"sql": sql}, request_id=17)
    assert_error(reply, 413, INVALID_REQUEST, None)
    assert harness.kit.external_calls() == 0
    assert harness.remaining == []


def test_base64_body_cap_applies_to_decoded_bytes(harness: Harness) -> None:
    at_cap = padded_ping(MAX_BODY_BYTES, "\u20ac").encode()
    reply = harness.post(at_cap, encode=False, b64=True)
    assert reply.status == 200
    assert reply.json()["result"] == {}
    over = padded_ping(MAX_BODY_BYTES + 1, "\u20ac").encode()
    assert_error(harness.post(over, encode=False, b64=True), 413, INVALID_REQUEST, None)
    huge = b"a" * (10 * MAX_BODY_BYTES)
    assert_error(harness.post(huge, encode=False, b64=True), 413, INVALID_REQUEST, None)


@pytest.mark.parametrize("payload", ["%%%not-base64%%%", "YWJj!"])
def test_invalid_base64_is_rejected(harness: Harness, payload: str) -> None:
    event = {
        "httpMethod": "POST",
        "headers": dict(JSON_HEADERS),
        "body": payload,
        "isBase64Encoded": True,
    }
    assert_error(harness.post(None, raw_event=event), 400, PARSE_ERROR, None)


def test_invalid_utf8_is_a_parse_error(harness: Harness) -> None:
    assert_error(harness.post(b'{"a": "\xff"}', encode=False, b64=True), 400, PARSE_ERROR, None)


def test_lone_surrogate_in_body_is_a_parse_error(harness: Harness) -> None:
    body = '{"jsonrpc":"2.0","id":1,"method":"ping","params":{"_meta":{"x":"\ud800"}}}'
    assert_error(harness.post(body, encode=False), 400, PARSE_ERROR, None)


# Raw strings: the wire bytes are ASCII and carry literal \uXXXX escapes, which JSON decodes to
# unpaired surrogate code points that cannot be encoded as UTF-8.
ESCAPED_LONE_SURROGATE_BODIES = [
    r'{"jsonrpc":"2.0","id":"\ud800","method":"ping"}',
    r'{"jsonrpc":"2.0","id":"\udfff","method":"ping"}',
    r'{"jsonrpc":"2.0","id":"call-\ud83d","method":"ping"}',
    r'{"jsonrpc":"2.0","id":"\ude00\ud83d","method":"ping"}',
    r'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"query_athena",'
    r'"arguments":{"sql":"SELECT \ud800 AS n FROM case_summary"}}}',
    r'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"_meta":{"\udc00":1}}}',
    r'{"jsonrpc":"2.0","method":"notifications/initialized","params":{"x":["\ud800"]}}',
    r'{"jsonrpc":"2.0","id":"\ud800","error":{"code":-32600,"message":"m"}}',
    r'"\ud800"',
]


@pytest.mark.parametrize("body", ESCAPED_LONE_SURROGATE_BODIES)
@pytest.mark.parametrize("b64", [False, True])
def test_escaped_lone_surrogates_are_invalid_requests(
    harness: Harness, body: str, b64: bool
) -> None:
    """MCP 2025-11-25 transports: "JSON-RPC messages MUST be UTF-8 encoded"; text holding an
    unpaired surrogate has no UTF-8 encoding, so it is refused without echoing the id."""
    assert body.isascii()
    assert "\\u" in body
    reply = harness.post(body, encode=False, b64=b64)
    assert_error(reply, 400, INVALID_REQUEST, None)
    reply.body.encode("utf-8")
    assert harness.kit.external_calls() == 0


def test_escaped_surrogate_pair_id_is_preserved(harness: Harness) -> None:
    reply = harness.post(r'{"jsonrpc":"2.0","id":"\ud83d\ude00","method":"ping"}', encode=False)
    assert reply.json() == {"jsonrpc": "2.0", "id": "\U0001f600", "result": {}}
    reply.body.encode("utf-8")


def test_unpaired_surrogate_in_model_output_still_yields_a_utf8_response(
    harness: Harness,
) -> None:
    # json.dumps escapes the surrogate, so the stubbed InvokeModel body is valid ASCII JSON that
    # decodes to an unpaired surrogate, as an escaped one in a real model reply would.
    harness.kit.bedrock.reply("Invented \ud800 analysis.")
    reply = harness.call("analyze_with_bedrock", {"question": "q"}, request_id=36)
    reply.body.encode("utf-8")
    result = tool_result(reply, 36)
    assert result["isError"] is False
    structured = json.dumps(result["structuredContent"], ensure_ascii=False)
    assert "Invented \ud800 analysis." in structured


# Telemetry ------------------------------------------------------------------------------------


def test_telemetry_never_contains_arguments_bodies_or_ids(
    harness: Harness,
    capsys: pytest.CaptureFixture[str],
    metrics_in: Callable[[str], dict[str, float]],
) -> None:
    harness.rpc("initialize", initialize_params(), request_id=SENTINEL + "-id")
    harness.call("get_case_summary", {"serviceCode": "amazon-s3"}, request_id=SENTINEL + "-id2")
    harness.call("query_athena", {"sql": f"SELECT '{SENTINEL}' AS x"}, request_id=1)
    harness.call("query_athena", {"sql": f"DROP TABLE {SENTINEL}"}, request_id=2)
    harness.call("get_case_summary", {"serviceCode": SENTINEL}, request_id=3)
    harness.call(SENTINEL, {}, request_id=4)
    harness.rpc(SENTINEL, {}, request_id=5)
    harness.post('{"oops": "' + SENTINEL, encode=False)
    harness.post(padded_ping(MAX_BODY_BYTES + 1).replace("cap", SENTINEL[:3]), encode=False)
    output = capsys.readouterr().out
    logs = harness.logs()
    assert SENTINEL not in logs
    assert SENTINEL not in output
    assert "SENTINEL-SUMMARY" not in logs + output
    assert "requester-" not in logs + output
    assert '"mcp_method":"tools/call"' in logs.replace(" ", "")
    assert '"mcp_tool":"get_case_summary"' in logs.replace(" ", "")
    totals = metrics_in(output)
    assert totals["McpRequests"] == 9
    assert totals["McpToolCalls"] == 4
    assert totals["McpToolErrors"] == 2
    assert totals["McpProtocolErrors"] == 4
    assert totals["McpErrors"] == 6
    assert totals.get("McpInternalErrors", 0) == 0


def test_internal_errors_are_counted(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    metrics_in: Callable[[str], dict[str, float]],
) -> None:
    def explode(name: str, arguments: object, ctx: ToolContext) -> dict[str, Any]:
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(app.REGISTRY, "call", explode)
    harness.call("get_case_summary", {}, request_id=1)
    totals = metrics_in(capsys.readouterr().out)
    assert totals["McpInternalErrors"] == 1
    assert totals["McpErrors"] == 1

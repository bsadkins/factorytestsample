"""Cross-surface regression, part 3: the real `scripts/mcp_aws_client.py` stdio bridge signs every
request for execute-api with synthetic credentials and round-trips all eight tools against a
loopback HTTPS endpoint backed by the real MCP handler and the shared fixture records. A loopback
fixture proves the bridge, not API Gateway IAM, resource policy or WAF."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest
from cross_surface import (
    ACCOUNT_A,
    ACCOUNT_B,
    ANALYSIS_TEXT,
    EXPECTED_TOOLS,
    Endpoint,
    assert_no_case_content,
    nonzero,
)
from mcp_kit import make_kit
from mcp_tools.context import ToolContext
from sigv4_loopback import (
    REGION,
    SERVICE,
    Bridge,
    assert_clean,
    bridge_env,
    handler_backend,
    initialize,
    make_tls_files,
    rpc,
    start_fixture,
    stop_fixture,
)

# --- SigV4 stdio bridge ----------------------------------------------------------------------

TRANSCRIPT: Final[list[tuple[str, dict[str, Any]]]] = [
    (
        "query_athena",
        {
            "sql": "SELECT rca_category, count(*) AS cases FROM case_summary "
            "GROUP BY 1 ORDER BY 2 DESC, 1"
        },
    ),
    ("analyze_with_bedrock", {"question": "Which services drive most cases?", "data": "ec2,4"}),
    ("get_case_summary", {"accountId": ACCOUNT_A}),
    ("analyze_case_summaries", {"startDate": "2026-02-01", "endDate": "2026-02-28", "maxCases": 3}),
    ("get_temporal_patterns", {}),
    ("get_rca_analysis", {"groupBy": "service", "limit": 2}),
    ("get_lifecycle_analysis", {"accountId": ACCOUNT_B, "groupBy": "rcaCategory"}),
    ("get_service_trends", {"granularity": "month", "topServices": 2}),
]


def comparable(result: dict[str, Any]) -> dict[str, Any]:
    """A tool result without the per-execution Athena query ID."""
    return {key: value for key, value in result.items() if key != "queryExecutionId"}


def test_stdio_bridge_signs_and_round_trips_all_eight_tools(
    rows: list[dict[str, str | None]],
    endpoint: Endpoint,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for _ in range(2):
        endpoint.kit.bedrock.reply(ANALYSIS_TEXT)
    direct = {tool: comparable(endpoint.ok(tool, arguments)) for tool, arguments in TRANSCRIPT}
    assert [
        (r, c) for r, c in nonzero(direct["get_case_summary"]["distribution"], "rcaCategory")
    ] == [
        ("Throttling", 3),
        ("ICE Error", 2),
        ("Limit reached", 1),
    ]
    assert direct["query_athena"]["rows"][0] == {"rca_category": "Throttling", "cases": 6}

    def tool_context(remaining_millis: int | None) -> ToolContext:
        # Each request runs on its own server thread and the SQLite stand-in is thread-bound.
        kit = make_kit(rows)
        kit.bedrock.reply(ANALYSIS_TEXT)
        return kit.context(remaining_millis)

    monkeypatch.setattr(endpoint.app, "tool_context", tool_context)
    loopback = start_fixture(make_tls_files(tmp_path))
    try:
        loopback.use(handler_backend(endpoint.app))
        bridge = Bridge([], bridge_env(tmp_path, loopback))
        try:
            init = bridge.request(initialize(1))
            bridge.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            listed = bridge.request(rpc("tools/list", 2, {}))
            via_bridge = {}
            for index, (tool, arguments) in enumerate(TRANSCRIPT, start=10):
                reply = bridge.request(
                    rpc("tools/call", index, {"name": tool, "arguments": arguments})
                )
                assert reply["id"] == index
                assert reply["result"]["isError"] is False, reply
                via_bridge[tool] = comparable(reply["result"]["structuredContent"])
        finally:
            code, rest, stderr = bridge.finish()
    finally:
        stop_fixture(loopback)

    assert init["result"]["protocolVersion"] == "2025-11-25"
    assert [tool["name"] for tool in listed["result"]["tools"]] == EXPECTED_TOOLS
    assert via_bridge == direct
    assert code == 0
    assert rest == []
    assert len(bridge.stdout_lines) == 2 + len(TRANSCRIPT)  # nothing for the notification
    assert len(loopback.received) == 3 + len(TRANSCRIPT)
    for received in loopback.received:
        assert received.signature.valid, received.signature.problem
        assert received.signature.service == SERVICE
        assert received.signature.region == REGION
        assert received.path == "/v1/mcp"
    assert_clean(stderr, b"".join(bridge.stdout_lines))
    assert_no_case_content(stderr)
    assert_no_case_content(endpoint.logs())

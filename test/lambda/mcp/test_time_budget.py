"""Request time budget: every tool call, including Athena polling, result paging, cancellation and
Bedrock invocation, ends within the request budget even when AWS calls are slow or hang.

Slow calls advance the kit's fake clock. Unless a test says otherwise the stand-ins behave like
botocore: a call slower than the read timeout of the client it was made through fails with
ReadTimeoutError after the connect plus read timeout. Each SDK call must therefore be given a
timeout that ends before the deadline, and none may start without enough time left.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp_kit import McpKit, dataset, make_kit, processed_row
from mcp_tools.context import McpClients
from mcp_tools.errors import ToolError
from mcp_tools.limits import McpLimits
from mcp_tools.registry import build_registry

LIMITS = McpLimits()
BUDGET = LIMITS.request_budget_seconds
COUNT_SQL = "SELECT rca_category, count(*) AS cases FROM case_summary GROUP BY rca_category"
CASE_RANGE = {"startDate": "2026-01-01", "endDate": "2026-01-31"}


def run(kit: McpKit, tool: str, arguments: dict[str, Any], **context: Any) -> dict[str, Any]:  # noqa: ANN401
    return build_registry().call(tool, arguments, kit.context(**context))


def fail(kit: McpKit, tool: str, arguments: dict[str, Any], **context: Any) -> ToolError:  # noqa: ANN401
    with pytest.raises(ToolError) as raised:
        run(kit, tool, arguments, **context)
    return raised.value


def assert_calls_fit(kit: McpKit, start: float, budget: float = BUDGET) -> None:
    """Every SDK call had a read timeout whose worst case ended within the budget."""
    deadline = start + budget
    for operation, started, timeout in kit.athena.timed_calls:
        assert timeout is not None, operation
        assert started + kit.athena.connect_timeout + timeout <= deadline, operation
    for started, timeout in kit.bedrock.timed_calls:
        assert timeout is not None
        assert started + kit.bedrock.connect_timeout + timeout <= deadline


def many_rows(count: int) -> list[dict[str, str | None]]:
    return [
        processed_row(case=f"{5000 + i}", created=f"2026-01-{1 + i % 28:02d}T10:00:00Z")
        for i in range(count)
    ]


def test_production_clients_have_no_sdk_retries_and_the_requested_read_timeout() -> None:
    clients = McpClients.create(region_name="us-east-1")
    athena = clients.athena.with_timeout(7.0)
    bedrock = clients.bedrock.with_timeout(19.0)
    for client, connect, read in (
        (athena, LIMITS.athena_connect_timeout_seconds, 7.0),
        (bedrock, LIMITS.bedrock_connect_timeout_seconds, 19.0),
    ):
        config: Any = client.meta.config
        assert config.retries["total_max_attempts"] == 1
        assert config.connect_timeout == connect
        assert config.read_timeout == read
    assert clients.athena.with_timeout(7.0) is athena
    assert clients.athena.with_timeout(6.0) is not athena


# query_athena ----------------------------------------------------------------------------------


def test_slow_status_calls_return_pending_within_budget_even_ignoring_timeouts() -> None:
    """Scrutiny reproduction: every GetQueryExecution takes seven seconds."""
    kit = make_kit(dataset())
    kit.athena.polls_before_success = None
    kit.athena.latency["GetQueryExecution"] = 7.0
    kit.athena.honour_timeouts = False
    start = kit.clock.now
    result = run(kit, "query_athena", {"sql": COUNT_SQL})
    assert result["status"] == "PENDING"
    assert result["queryExecutionId"]
    assert kit.clock.now - start <= BUDGET


def test_hanging_status_calls_return_pending_within_budget() -> None:
    kit = make_kit(dataset())
    kit.athena.latency["GetQueryExecution"] = 120.0
    start = kit.clock.now
    result = run(kit, "query_athena", {"sql": COUNT_SQL})
    assert result["status"] == "PENDING"
    assert kit.clock.now - start <= BUDGET - LIMITS.response_reserve_seconds
    assert_calls_fit(kit, start)
    # Resuming must first confirm the ID belongs to this server, so a hanging status call is a
    # retryable error rather than an unverified PENDING.
    resume_start = kit.clock.now
    error = fail(kit, "query_athena", {"queryExecutionId": result["queryExecutionId"]})
    assert error.code == "ATHENA_ERROR"
    assert error.retryable is True
    assert kit.clock.now - resume_start <= BUDGET - LIMITS.response_reserve_seconds


def test_slow_first_start_call_is_bounded_and_controlled() -> None:
    kit = make_kit(dataset())
    kit.athena.latency["StartQueryExecution"] = 120.0
    start = kit.clock.now
    error = fail(kit, "query_athena", {"sql": COUNT_SQL})
    assert error.code == "ATHENA_ERROR"
    assert error.retryable is True
    assert kit.athena.operations() == ["StartQueryExecution"]
    assert kit.clock.now - start <= BUDGET
    assert_calls_fit(kit, start)


def test_slow_result_pages_stop_at_the_budget_with_a_resumable_token() -> None:
    kit = make_kit(many_rows(40), limits=McpLimits(results_page_size=2))
    kit.athena.latency["GetQueryResults"] = 3.0
    start = kit.clock.now
    first = run(
        kit,
        "query_athena",
        {"sql": "SELECT case_number FROM case_summary ORDER BY case_number", "maxRows": 40},
    )
    assert kit.clock.now - start <= BUDGET - LIMITS.response_reserve_seconds
    assert_calls_fit(kit, start)
    assert first["status"] == "SUCCEEDED"
    assert first["truncated"] is True
    assert first["truncationReason"] == "timeLimit"
    assert 0 < first["rowCount"] < 40
    kit.athena.latency.clear()
    second = run(
        kit,
        "query_athena",
        {"queryExecutionId": first["queryExecutionId"], "nextToken": first["nextToken"],
         "maxRows": 40},
    )  # fmt: skip
    numbers = [row["case_number"] for row in first["rows"] + second["rows"]]
    assert numbers == [f"{5000 + i}" for i in range(40)]
    assert second["truncated"] is False


def test_hanging_first_result_page_is_a_controlled_error_within_budget() -> None:
    kit = make_kit(dataset())
    kit.athena.latency["GetQueryResults"] = 120.0
    start = kit.clock.now
    error = fail(kit, "query_athena", {"sql": COUNT_SQL})
    assert error.code in {"ATHENA_ERROR", "TIME_BUDGET_EXCEEDED"}
    assert error.retryable is True
    assert kit.clock.now - start <= BUDGET
    assert_calls_fit(kit, start)


def test_lambda_remaining_time_bounds_slow_calls() -> None:
    kit = make_kit(dataset())
    kit.athena.polls_before_success = None
    kit.athena.latency["GetQueryExecution"] = 30.0
    start = kit.clock.now
    result = run(kit, "query_athena", {"sql": COUNT_SQL}, remaining_millis=12_000)
    assert result["status"] == "PENDING"
    budget = 12.0 - LIMITS.lambda_deadline_margin_seconds
    assert kit.clock.now - start <= budget
    assert_calls_fit(kit, start, budget)


def test_too_little_time_to_start_a_query_starts_nothing() -> None:
    kit = make_kit(dataset())
    error = fail(kit, "query_athena", {"sql": COUNT_SQL}, remaining_millis=3_000)
    assert error.code == "TIME_BUDGET_EXCEEDED"
    assert error.retryable is True
    assert kit.athena.calls == []


# Fixed analytical queries ----------------------------------------------------------------------


def test_fixed_query_with_slow_polls_is_cancelled_within_budget() -> None:
    kit = make_kit(dataset())
    kit.athena.polls_before_success = None
    kit.athena.latency["GetQueryExecution"] = 7.0
    start = kit.clock.now
    error = fail(kit, "get_rca_analysis", {})
    assert error.code == "QUERY_TIMEOUT"
    assert kit.athena.operations()[-1] == "StopQueryExecution"
    assert kit.clock.now - start <= BUDGET - LIMITS.response_reserve_seconds
    assert_calls_fit(kit, start)


def test_fixed_query_with_slow_result_pages_fails_rather_than_returning_partial_counts() -> None:
    kit = make_kit(many_rows(40), limits=McpLimits(results_page_size=2))
    kit.athena.latency["GetQueryResults"] = 3.0
    start = kit.clock.now
    error = fail(kit, "get_service_trends", {"granularity": "day"})
    assert error.code == "TIME_BUDGET_EXCEEDED"
    assert error.retryable is True
    assert kit.clock.now - start <= BUDGET
    assert_calls_fit(kit, start)


# Bedrock ---------------------------------------------------------------------------------------


def test_hanging_bedrock_invocation_ends_within_budget() -> None:
    kit = make_kit()
    kit.bedrock.latency = 120.0
    kit.bedrock.reply("unused")
    kit.bedrock.reply("unused")
    start = kit.clock.now
    error = fail(kit, "analyze_with_bedrock", {"question": "q"})
    assert error.code in {"BEDROCK_UNAVAILABLE", "TIME_BUDGET_EXCEEDED"}
    assert error.retryable is True
    assert kit.clock.now - start <= BUDGET - LIMITS.response_reserve_seconds
    assert_calls_fit(kit, start)


def test_slow_athena_then_hanging_bedrock_stays_within_the_combined_budget() -> None:
    kit = make_kit(dataset())
    kit.athena.polls_before_success = 2
    kit.athena.latency["GetQueryExecution"] = 1.0
    kit.bedrock.latency = 120.0
    kit.bedrock.reply("unused")
    kit.bedrock.reply("unused")
    start = kit.clock.now
    error = fail(kit, "analyze_case_summaries", CASE_RANGE)
    assert error.code in {"BEDROCK_UNAVAILABLE", "TIME_BUDGET_EXCEEDED"}
    assert kit.bedrock.timed_calls, "Bedrock should have been invoked"
    assert kit.clock.now - start <= BUDGET - LIMITS.response_reserve_seconds
    assert_calls_fit(kit, start)


def test_slow_athena_then_slow_bedrock_succeeds_within_budget() -> None:
    kit = make_kit(dataset())
    kit.athena.polls_before_success = 2
    kit.athena.latency["GetQueryExecution"] = 1.0
    kit.bedrock.latency = 8.0
    kit.bedrock.reply("Invented themes.")
    start = kit.clock.now
    result = run(kit, "analyze_case_summaries", CASE_RANGE)
    assert result["status"] == "ANALYSED"
    assert result["analysis"] == "Invented themes."
    assert kit.clock.now - start <= BUDGET
    assert_calls_fit(kit, start)
    kit.bedrock.assert_consumed()


def test_slow_case_selection_leaves_no_time_for_bedrock() -> None:
    kit = make_kit(dataset())
    kit.athena.polls_before_success = None
    kit.athena.latency["GetQueryExecution"] = 4.0
    start = kit.clock.now
    error = fail(kit, "analyze_case_summaries", CASE_RANGE)
    assert error.code == "QUERY_TIMEOUT"
    assert kit.bedrock.requests == []
    assert kit.clock.now - start <= LIMITS.analysis_query_budget_seconds
    assert_calls_fit(kit, start, LIMITS.analysis_query_budget_seconds)

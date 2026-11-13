"""query_athena and the shared Athena execution boundary: fixed workgroup/database, bounded
polling with a pending/resume outcome, bounded rows/pages/response size and controlled failures.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp_kit import (
    DATABASE,
    WORKGROUP,
    McpKit,
    client_error,
    dataset,
    make_kit,
    processed_row,
)
from mcp_tools.errors import ToolError, ToolInputError
from mcp_tools.limits import McpLimits
from mcp_tools.registry import build_registry

COUNT_SQL = "SELECT rca_category, count(*) AS cases FROM case_summary GROUP BY rca_category"


def call(kit: McpKit, arguments: dict[str, Any]) -> dict[str, Any]:
    return build_registry().call("query_athena", arguments, kit.context())


def test_successful_query_returns_typed_rows_and_metadata() -> None:
    kit = make_kit(dataset())
    result = call(kit, {"sql": COUNT_SQL + " ORDER BY cases DESC, rca_category"})
    assert result["status"] == "SUCCEEDED"
    assert result["columns"] == [
        {"name": "rca_category", "type": "varchar"},
        {"name": "cases", "type": "bigint"},
    ]
    assert result["rows"][0] == {"rca_category": "Throttling", "cases": 5}
    assert result["rowCount"] == len(result["rows"]) == 6
    assert result["truncated"] is False
    assert "nextToken" not in result
    assert result["dataScannedBytes"] == 2048
    assert result["queryExecutionId"]


def test_requests_use_only_server_side_boundary() -> None:
    kit = make_kit(dataset())
    call(kit, {"sql": "SELECT 1 AS one FROM case_summary LIMIT 1"})
    (start,) = kit.athena.started()
    assert start == {
        "QueryString": "SELECT 1 AS one FROM case_summary LIMIT 1",
        "QueryExecutionContext": {"Catalog": "AwsDataCatalog", "Database": DATABASE},
        "WorkGroup": WORKGROUP,
    }


def test_trailing_semicolon_is_removed_before_submission() -> None:
    kit = make_kit(dataset())
    call(kit, {"sql": "SELECT count(*) AS n FROM case_summary;"})
    assert kit.athena.started()[0]["QueryString"] == "SELECT count(*) AS n FROM case_summary"


def test_show_and_describe_results_have_no_header_row() -> None:
    kit = make_kit(dataset())
    kit.athena.canned["DESCRIBE"] = [["caseid\tstring"], ["displayid\tstring"]]
    result = call(kit, {"sql": "DESCRIBE case_summary"})
    assert result["rows"] == [{"result": "caseid\tstring"}, {"result": "displayid\tstring"}]


def test_null_values_are_returned_as_null() -> None:
    kit = make_kit(dataset())
    result = call(kit, {"sql": "SELECT servicecode FROM case_summary WHERE case_number = '2003'"})
    assert result["rows"] == [{"servicecode": None}]


def test_non_finite_doubles_are_returned_as_json_safe_strings() -> None:
    kit = make_kit(dataset())
    kit.athena.next_result = (
        [("n", "double"), ("p", "double"), ("m", "double"), ("r", "real"), ("f", "float"),
         ("ok", "double"), ("i", "bigint")],
        [["NaN", "Infinity", "-Infinity", "NaN", "Infinity", "1.5", "7"]],
    )  # fmt: skip
    result = call(kit, {"sql": "SELECT 1 FROM case_summary"})
    assert result["rows"] == [
        {"n": "NaN", "p": "Infinity", "m": "-Infinity", "r": "NaN", "f": "Infinity", "ok": 1.5,
         "i": 7},
    ]  # fmt: skip
    json.dumps(result, allow_nan=False)


def test_empty_result() -> None:
    kit = make_kit(dataset())
    result = call(kit, {"sql": "SELECT * FROM case_summary WHERE account_number = '000000000000'"})
    assert result["status"] == "SUCCEEDED"
    assert result["rows"] == []
    assert result["rowCount"] == 0
    assert result["truncated"] is False


@pytest.mark.parametrize(("max_rows", "truncated"), [(11, True), (12, False), (13, False)])
def test_max_rows_boundaries(max_rows: int, truncated: bool) -> None:
    kit = make_kit(dataset())
    result = call(kit, {"sql": "SELECT case_number FROM case_summary", "maxRows": max_rows})
    assert result["rowCount"] == min(max_rows, 12)
    assert result["truncated"] is truncated
    assert ("nextToken" in result) is truncated


def test_multi_page_results_and_resume_with_next_token() -> None:
    limits = McpLimits(results_page_size=5)
    kit = make_kit(dataset(), limits=limits)
    first = call(
        kit, {"sql": "SELECT case_number FROM case_summary ORDER BY case_number", "maxRows": 7}
    )
    assert [r["case_number"] for r in first["rows"]] == [
        "1001",
        "1002",
        "1003",
        "1004",
        "1005",
        "1006",
        "2001",
    ]
    pages = [p for op, p in kit.athena.calls if op == "GetQueryResults"]
    assert [p.get("MaxResults") for p in pages] == [5, 3]  # header row + 7 rows
    assert first["truncated"] is True
    second = call(
        kit,
        {
            "queryExecutionId": first["queryExecutionId"],
            "nextToken": first["nextToken"],
            "maxRows": 10,
        },
    )
    assert [r["case_number"] for r in second["rows"]] == ["2002", "2003", "2004", "2005", "2006"]
    assert second["truncated"] is False
    assert len(kit.athena.started()) == 1


def test_response_size_cap_is_explicit() -> None:
    rows = [processed_row(case=f"{3000 + i}", created="2026-04-01T00:00:00Z") for i in range(40)]
    kit = make_kit(rows, limits=McpLimits(max_result_bytes=2_000))
    result = call(kit, {"sql": "SELECT case_summary FROM case_summary", "maxRows": 40})
    assert result["truncated"] is True
    assert result["truncationReason"] == "responseSizeLimit"
    assert 0 < result["rowCount"] < 40
    assert "nextToken" not in result


def test_query_still_running_returns_bounded_pending_outcome() -> None:
    kit = make_kit(dataset())
    kit.athena.polls_before_success = None
    result = call(kit, {"sql": COUNT_SQL})
    assert result["status"] == "PENDING"
    assert result["queryExecutionId"]
    assert "rows" not in result
    elapsed = kit.clock.now - 1000.0
    assert elapsed <= McpLimits().request_budget_seconds
    polls = kit.athena.operations().count("GetQueryExecution")
    assert polls <= McpLimits().max_polls
    assert all(0 < s <= McpLimits().poll_max_seconds for s in kit.clock.sleeps)
    assert "StopQueryExecution" not in kit.athena.operations()


def test_pending_query_can_be_resumed() -> None:
    kit = make_kit(dataset())
    kit.athena.polls_before_success = None
    pending = call(kit, {"sql": COUNT_SQL})
    execution = kit.athena.executions[pending["queryExecutionId"]]
    execution.polls_until_done = 2
    resumed = call(kit, {"queryExecutionId": pending["queryExecutionId"]})
    assert resumed["status"] == "SUCCEEDED"
    assert resumed["rowCount"] == 6
    assert len(kit.athena.started()) == 1


def test_lambda_remaining_time_shortens_the_budget() -> None:
    kit = make_kit(dataset())
    kit.athena.polls_before_success = None
    ctx = kit.context(remaining_millis=8_000)
    result = build_registry().call("query_athena", {"sql": COUNT_SQL}, ctx)
    assert result["status"] == "PENDING"
    assert kit.clock.now - 1000.0 <= 8.0


def test_resume_rejects_queries_from_other_workgroups_or_databases() -> None:
    kit = make_kit(dataset())
    foreign = kit.athena.start_query_execution(
        QueryString="SELECT * FROM case_summary",
        QueryExecutionContext={"Database": DATABASE},
        WorkGroup="primary",
    )["QueryExecutionId"]
    other_db = kit.athena.start_query_execution(
        QueryString="SELECT * FROM case_summary",
        QueryExecutionContext={"Database": "other_db"},
        WorkGroup=WORKGROUP,
    )["QueryExecutionId"]
    for query_id in (foreign, other_db):
        with pytest.raises(ToolError) as raised:
            call(kit, {"queryExecutionId": query_id})
        assert raised.value.code == "QUERY_NOT_FOUND"
    assert "GetQueryResults" not in kit.athena.operations()


def test_resume_revalidates_the_stored_query_text() -> None:
    kit = make_kit(dataset())
    unsafe = kit.athena.start_query_execution(
        QueryString="SELECT * FROM case_summary, other_db.secret",
        QueryExecutionContext={"Database": DATABASE},
        WorkGroup=WORKGROUP,
    )["QueryExecutionId"]
    with pytest.raises(ToolError) as raised:
        call(kit, {"queryExecutionId": unsafe})
    assert raised.value.code == "QUERY_NOT_FOUND"
    assert "GetQueryResults" not in kit.athena.operations()


def test_unknown_query_execution_id() -> None:
    kit = make_kit(dataset())
    with pytest.raises(ToolError) as raised:
        call(kit, {"queryExecutionId": "0f8e2a8c-4b6b-4a52-9e0f-1c2d3e4f5a6b"})
    assert raised.value.code == "QUERY_NOT_FOUND"


@pytest.mark.parametrize(
    ("state", "reason", "code"),
    [
        ("FAILED", "Bytes scanned limit was exceeded", "QUERY_FAILED"),
        ("FAILED", "COLUMN_NOT_FOUND: line 1:8: Column 'x' cannot be resolved", "QUERY_FAILED"),
        ("CANCELLED", "Query cancelled", "QUERY_CANCELLED"),
    ],
)
def test_failed_and_cancelled_queries_are_controlled_errors(
    state: str, reason: str, code: str
) -> None:
    kit = make_kit(dataset())
    kit.athena.fail_next = (state, reason)
    with pytest.raises(ToolError) as raised:
        call(kit, {"sql": COUNT_SQL})
    assert raised.value.code == code
    assert raised.value.retryable is False
    assert raised.value.to_payload()["details"]["athenaReason"] == reason
    assert "GetQueryResults" not in kit.athena.operations()


def test_start_throttling_is_retried_within_bounds() -> None:
    kit = make_kit(dataset())
    kit.athena.start_errors = [client_error("TooManyRequestsException", "StartQueryExecution")]
    assert call(kit, {"sql": COUNT_SQL})["status"] == "SUCCEEDED"
    assert len(kit.athena.started()) == 2


def test_persistent_start_throttling_is_retryable_error() -> None:
    kit = make_kit(dataset())
    kit.athena.start_errors = [
        client_error("TooManyRequestsException", "StartQueryExecution") for _ in range(10)
    ]
    with pytest.raises(ToolError) as raised:
        call(kit, {"sql": COUNT_SQL})
    assert raised.value.code == "ATHENA_THROTTLED"
    assert raised.value.retryable is True
    assert len(kit.athena.started()) == McpLimits().athena_start_attempts


def test_access_denied_is_controlled() -> None:
    kit = make_kit(dataset())
    kit.athena.start_errors = [client_error("AccessDeniedException", "StartQueryExecution")]
    with pytest.raises(ToolError) as raised:
        call(kit, {"sql": COUNT_SQL})
    assert raised.value.code == "ATHENA_ERROR"
    assert raised.value.to_payload()["details"] == {"awsErrorCode": "AccessDeniedException"}


def test_results_error_is_controlled() -> None:
    kit = make_kit(dataset())
    kit.athena.results_errors = [client_error("InternalServerException", "GetQueryResults", 500)]
    with pytest.raises(ToolError) as raised:
        call(kit, {"sql": COUNT_SQL})
    assert raised.value.code == "ATHENA_ERROR"
    assert raised.value.retryable is True


def test_sql_length_boundary() -> None:
    limit = McpLimits().max_sql_chars
    kit = make_kit(dataset())
    base = "SELECT count(*) AS n FROM case_summary WHERE subject <> '"
    at_limit = base + "x" * (limit - len(base) - 1) + "'"
    assert len(at_limit) == limit
    assert call(kit, {"sql": at_limit})["status"] == "SUCCEEDED"
    with pytest.raises(ToolInputError):
        call(kit, {"sql": at_limit + " "})


def test_logs_never_contain_sql_or_rows(caplog: pytest.LogCaptureFixture) -> None:
    kit = make_kit(dataset())
    with caplog.at_level("DEBUG"):
        call(kit, {"sql": "SELECT case_summary FROM case_summary WHERE subject <> 'SENTINEL-Q'"})
    text = caplog.text
    assert "SENTINEL" not in text
    assert "case_summary FROM" not in text

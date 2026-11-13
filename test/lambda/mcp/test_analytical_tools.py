"""Analytical tools over synthetic processed records. The SQLite-backed Athena stand-in really
executes the generated SQL, so these tests check selection and aggregation results, not just
query text: RCA distribution, temporal patterns, RCA/lifecycle grouping and service trends."""

from __future__ import annotations

import re
from typing import Any

import pytest
from common.taxonomy import LIFECYCLE, RCA, UNCLASSIFIED
from mcp_kit import ACCOUNT_A, ACCOUNT_B, McpKit, counts, dataset, make_kit
from mcp_tools.errors import ToolError
from mcp_tools.limits import McpLimits
from mcp_tools.registry import build_registry
from mcp_tools.sql_guard import SqlPolicy, validate_read_only_sql

ALL_RCA = {"Throttling": 5, "ICE Error": 2, "Customer Question": 2, "Limit reached": 1,
           UNCLASSIFIED: 1, "Development Issue": 1}  # fmt: skip


def call(kit: McpKit, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return build_registry().call(tool, arguments, kit.context())


def only_query(kit: McpKit) -> dict[str, Any]:
    (start,) = kit.athena.started()
    return start


# get_case_summary: filtered RCA distribution ----------------------------------------------


def test_case_summary_unfiltered_distribution() -> None:
    kit = make_kit(dataset())
    result = call(kit, "get_case_summary", {})
    assert result["totalCases"] == 12
    assert counts(result["distribution"], "rcaCategory") == ALL_RCA
    assert result["distribution"][0] == {
        "rcaCategory": "Throttling",
        "cases": 5,
        "percentOfCases": 41.7,
    }
    listed = [row["rcaCategory"] for row in result["distribution"]]
    assert set(listed) == {*RCA.labels, UNCLASSIFIED}
    assert len(listed) == len(RCA.labels) + 1
    assert result["filters"] == {}


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"serviceCode": "amazon-s3"}, {"Throttling": 3, "Customer Question": 1}),
        ({"accountId": ACCOUNT_A},
         {"Throttling": 3, "ICE Error": 1, "Customer Question": 1, "Limit reached": 1}),
        ({"startDate": "2026-01-01", "endDate": "2026-01-31"},
         {"Throttling": 2, "ICE Error": 1, "Customer Question": 2, UNCLASSIFIED: 1}),
        ({"startDate": "2026-02-01"},
         {"Limit reached": 1, "Throttling": 2, "Development Issue": 1, "ICE Error": 1}),
        ({"endDate": "2025-12-31"}, {"Throttling": 1}),
        ({"severityCode": "normal"}, {"Customer Question": 2, "Limit reached": 1, "Throttling": 1}),
        ({"lifecycleCategory": "Monitoring"}, {"Throttling": 3, "Limit reached": 1}),
        ({"lifecycleCategory": UNCLASSIFIED}, {UNCLASSIFIED: 1}),
        ({"accountId": ACCOUNT_B, "serviceCode": "amazon-ec2", "severityCode": "low"},
         {"Throttling": 1}),
    ],
)  # fmt: skip
def test_case_summary_filters_select_records(
    filters: dict[str, Any], expected: dict[str, int]
) -> None:
    kit = make_kit(dataset())
    result = call(kit, "get_case_summary", filters)
    assert counts(result["distribution"], "rcaCategory") == expected
    assert result["totalCases"] == sum(expected.values())
    assert result["filters"] == filters


def test_case_summary_parameterises_every_filter_value() -> None:
    kit = make_kit(dataset())
    call(
        kit,
        "get_case_summary",
        {
            "accountId": ACCOUNT_A,
            "startDate": "2026-01-01",
            "endDate": "2026-01-31",
            "serviceCode": "amazon-s3",
            "severityCode": "high",
            "lifecycleCategory": "Backup/DR",
        },
    )
    start = only_query(kit)
    query = start["QueryString"]
    for value in (ACCOUNT_A, "2026-01", "amazon-s3", "high", "Backup/DR"):
        assert value not in query
    assert start["ExecutionParameters"] == [
        f"'{ACCOUNT_A}'",
        "'2026-01-01T00:00:00Z'",
        "'2026-02-01T00:00:00Z'",
        "'amazon-s3'",
        "'high'",
        "'Backup/DR'",
    ]
    validated = validate_read_only_sql(
        query, SqlPolicy(database="case_insights_mcp_test"), allow_parameters=True
    )
    assert validated.parameter_count == len(start["ExecutionParameters"])


def test_case_summary_empty_selection() -> None:
    kit = make_kit(dataset())
    result = call(kit, "get_case_summary", {"accountId": "999999999999"})
    assert result["totalCases"] == 0
    assert all(row["cases"] == 0 and row["percentOfCases"] == 0 for row in result["distribution"])


def test_case_summary_athena_failure_is_controlled() -> None:
    kit = make_kit(dataset())
    kit.athena.fail_next = ("FAILED", "HIVE_BAD_DATA")
    with pytest.raises(ToolError) as raised:
        call(kit, "get_case_summary", {})
    assert raised.value.code == "QUERY_FAILED"


def test_reversed_date_range_rejected_before_athena() -> None:
    kit = make_kit(dataset())
    with pytest.raises(ToolError) as raised:
        call(kit, "get_case_summary", {"startDate": "2026-02-01", "endDate": "2026-01-01"})
    assert raised.value.code == "INVALID_ARGUMENTS"
    assert kit.athena.calls == []


# get_temporal_patterns -----------------------------------------------------------------------


def test_temporal_patterns_all_dimensions() -> None:
    kit = make_kit(dataset())
    result = call(kit, "get_temporal_patterns", {})
    assert result["totalCases"] == 12
    assert [(d["isoDay"], d["day"], d["cases"]) for d in result["dayOfWeek"]] == [
        (1, "Monday", 5), (2, "Tuesday", 2), (3, "Wednesday", 1), (4, "Thursday", 1),
        (5, "Friday", 0), (6, "Saturday", 1), (7, "Sunday", 2),
    ]  # fmt: skip
    assert [(w["week"], w["days"], w["cases"]) for w in result["weekOfMonth"]] == [
        (1, "1-7", 6), (2, "8-14", 2), (3, "15-21", 1), (4, "22-28", 0), (5, "29-31", 3),
    ]  # fmt: skip
    assert [(m["month"], m["cases"]) for m in result["monthly"]] == [
        ("2025-12", 1), ("2026-01", 6), ("2026-02", 3), ("2026-03", 2),
    ]  # fmt: skip
    assert result["timeZone"] == "UTC"
    query = only_query(kit)["QueryString"]
    assert query.lstrip().upper().startswith("WITH ")
    assert not re.search(r"\b(CREATE|VIEW)\b", query, flags=re.IGNORECASE)


def test_temporal_patterns_selected_dimension_and_filters() -> None:
    kit = make_kit(dataset())
    result = call(
        kit,
        "get_temporal_patterns",
        {"dimensions": ["monthly"], "rcaCategory": "Throttling", "accountId": ACCOUNT_A},
    )
    assert set(result) >= {"monthly", "totalCases"}
    assert "dayOfWeek" not in result
    assert "weekOfMonth" not in result
    assert [(m["month"], m["cases"]) for m in result["monthly"]] == [
        ("2026-01", 2), ("2026-02", 1),
    ]  # fmt: skip
    assert result["totalCases"] == 3


def test_temporal_monthly_fills_gaps_between_observed_months() -> None:
    kit = make_kit(dataset())
    result = call(
        kit, "get_temporal_patterns", {"dimensions": ["monthly"], "serviceCode": "amazon-ec2"}
    )
    assert [(m["month"], m["cases"]) for m in result["monthly"]] == [
        ("2026-01", 3), ("2026-02", 0), ("2026-03", 1),
    ]  # fmt: skip


def test_temporal_patterns_empty() -> None:
    kit = make_kit(dataset())
    result = call(kit, "get_temporal_patterns", {"startDate": "2030-01-01"})
    assert result["totalCases"] == 0
    assert all(d["cases"] == 0 for d in result["dayOfWeek"])
    assert all(w["cases"] == 0 for w in result["weekOfMonth"])
    assert result["monthly"] == []


# get_rca_analysis / get_lifecycle_analysis ---------------------------------------------------


def test_rca_analysis_grouped_by_service() -> None:
    kit = make_kit(dataset())
    result = call(kit, "get_rca_analysis", {})
    assert result["groupBy"] == "service"
    assert result["totalCases"] == 12
    by_category = {c["category"]: c for c in result["categories"]}
    assert {k: v["cases"] for k, v in by_category.items() if v["cases"]} == ALL_RCA
    assert counts(by_category["Throttling"]["groups"], "value") == {
        "amazon-s3": 3, "aws-lambda": 1, "amazon-ec2": 1,
    }  # fmt: skip
    assert by_category["Throttling"]["groups"][0] == {"value": "amazon-s3", "cases": 3}
    assert counts(by_category["Development Issue"]["groups"], "value") == {"unknown": 1}
    assert by_category["Throttling"]["percentOfCases"] == 41.7
    assert set(by_category) == {*RCA.labels, UNCLASSIFIED}


@pytest.mark.parametrize(
    ("group_by", "category", "expected"),
    [
        ("severity", "Throttling", {"low": 2, "high": 1, "critical": 1, "normal": 1}),
        ("account", "Throttling", {ACCOUNT_A: 3, ACCOUNT_B: 2}),
        ("month", "Throttling", {"2025-12": 1, "2026-01": 2, "2026-02": 1, "2026-03": 1}),
        ("lifecycleCategory", "Throttling", {"Monitoring": 3, "Alarming": 1, "Load Testing": 1}),
    ],
)
def test_rca_analysis_group_by_allowlist(
    group_by: str, category: str, expected: dict[str, int]
) -> None:
    kit = make_kit(dataset())
    result = call(kit, "get_rca_analysis", {"groupBy": group_by})
    groups = next(c["groups"] for c in result["categories"] if c["category"] == category)
    assert counts(groups, "value") == expected


def test_rca_analysis_limit_reports_other_groups() -> None:
    kit = make_kit(dataset())
    result = call(kit, "get_rca_analysis", {"groupBy": "severity", "limit": 2})
    throttling = next(c for c in result["categories"] if c["category"] == "Throttling")
    assert throttling["groups"] == [{"value": "low", "cases": 2}, {"value": "critical", "cases": 1}]
    assert throttling["otherGroupCases"] == 2
    assert throttling["otherGroups"] == 2


def test_lifecycle_analysis_with_rca_filter() -> None:
    kit = make_kit(dataset())
    result = call(
        kit, "get_lifecycle_analysis", {"rcaCategory": "Throttling", "groupBy": "severity"}
    )
    by_category = {c["category"]: c for c in result["categories"]}
    assert {k: v["cases"] for k, v in by_category.items() if v["cases"]} == {
        "Monitoring": 3, "Alarming": 1, "Load Testing": 1,
    }  # fmt: skip
    assert counts(by_category["Monitoring"]["groups"], "value") == {"low": 2, "normal": 1}
    assert set(by_category) == {*LIFECYCLE.labels, UNCLASSIFIED}
    assert result["totalCases"] == 5


def test_lifecycle_analysis_unfiltered_includes_unclassified() -> None:
    kit = make_kit(dataset())
    result = call(kit, "get_lifecycle_analysis", {"groupBy": "rcaCategory"})
    by_category = {c["category"]: c for c in result["categories"]}
    assert by_category["Monitoring"]["cases"] == 4
    assert by_category[UNCLASSIFIED]["cases"] == 1
    assert counts(by_category["Monitoring"]["groups"], "value") == {
        "Throttling": 3,
        "Limit reached": 1,
    }


@pytest.mark.parametrize("tool", ["get_rca_analysis", "get_lifecycle_analysis"])
def test_category_analysis_empty(tool: str) -> None:
    kit = make_kit(dataset())
    result = call(kit, tool, {"serviceCode": "amazon-route-53"})
    assert result["totalCases"] == 0
    assert all(c["cases"] == 0 and c["groups"] == [] for c in result["categories"])


# get_service_trends --------------------------------------------------------------------------


def test_service_trends_monthly_top_services() -> None:
    kit = make_kit(dataset())
    result = call(kit, "get_service_trends", {"topServices": 2})
    assert result["granularity"] == "month"
    assert result["totalServices"] == 4
    assert [s["serviceCode"] for s in result["services"]] == ["amazon-ec2", "amazon-s3"]
    ec2, s3 = result["services"]
    assert ec2["totalCases"] == 4
    assert [(p["period"], p["cases"], counts(p["rca"], "category")) for p in ec2["periods"]] == [
        ("2026-01", 3, {"ICE Error": 1, "Customer Question": 1, UNCLASSIFIED: 1}),
        ("2026-03", 1, {"Throttling": 1}),
    ]
    assert [(p["period"], counts(p["rca"], "category")) for p in s3["periods"]] == [
        ("2025-12", {"Throttling": 1}),
        ("2026-01", {"Throttling": 2, "Customer Question": 1}),
    ]
    assert result["truncated"] is False


@pytest.mark.parametrize(
    ("granularity", "expected"),
    [
        ("week", [("2025-12-29", {"Throttling": 1}),
                  ("2026-01-05", {"Throttling": 2, "Customer Question": 1})]),
        ("day", [("2025-12-31", {"Throttling": 1}),
                 ("2026-01-05", {"Throttling": 1, "Customer Question": 1}),
                 ("2026-01-06", {"Throttling": 1})]),
    ],
)  # fmt: skip
def test_service_trends_granularity(
    granularity: str, expected: list[tuple[str, dict[str, int]]]
) -> None:
    kit = make_kit(dataset())
    result = call(
        kit, "get_service_trends", {"granularity": granularity, "serviceCode": "amazon-s3"}
    )
    (s3,) = result["services"]
    assert [(p["period"], counts(p["rca"], "category")) for p in s3["periods"]] == expected


def test_service_trends_filters_and_unknown_service() -> None:
    kit = make_kit(dataset())
    result = call(kit, "get_service_trends", {"accountId": ACCOUNT_B, "startDate": "2026-02-01"})
    assert [(s["serviceCode"], s["totalCases"]) for s in result["services"]] == [
        ("amazon-ec2", 1), ("aws-lambda", 1), ("unknown", 1),
    ]  # fmt: skip


def test_service_trends_empty() -> None:
    kit = make_kit(dataset())
    result = call(kit, "get_service_trends", {"rcaCategory": "AWS Release"})
    assert result["services"] == []
    assert result["totalServices"] == 0


def test_fixed_query_row_cap_is_explicit() -> None:
    kit = make_kit(dataset(), limits=McpLimits(fixed_query_max_rows=3))
    result = call(kit, "get_service_trends", {"granularity": "day"})
    assert result["truncated"] is True


def test_fixed_query_timeout_cancels_the_query() -> None:
    kit = make_kit(dataset())
    kit.athena.polls_before_success = None
    with pytest.raises(ToolError) as raised:
        call(kit, "get_rca_analysis", {})
    assert raised.value.code == "QUERY_TIMEOUT"
    assert raised.value.retryable is True
    assert kit.athena.operations()[-1] == "StopQueryExecution"
    assert kit.clock.now - 1000.0 <= McpLimits().request_budget_seconds

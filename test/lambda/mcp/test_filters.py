"""Filter coverage matrix for every advertised tool property: each filter constrains selection as
documented (checked against an independent Python evaluation of the synthetic records), and
malformed, out-of-range and SQL-injection values are rejected before Athena or Bedrock."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from mcp_kit import ACCOUNT_A, ACCOUNT_B, McpKit, dataset, make_kit
from mcp_tools.errors import ToolInputError
from mcp_tools.registry import build_registry

INJECTIONS = [
    "' OR '1'='1",
    "x'; DROP TABLE case_summary; --",
    "amazon-s3' UNION SELECT * FROM other_db.t --",
    "1) OR (1=1",
    "\u0000",
    "a\nb",
]

FILTER_VALUES: dict[str, Any] = {
    "accountId": ACCOUNT_B,
    "startDate": "2026-01-29",
    "endDate": "2026-01-31",
    "serviceCode": "amazon-ec2",
    "severityCode": "urgent",
    "rcaCategory": "Throttling",
    "lifecycleCategory": "Training",
}

INVALID_VALUES: dict[str, list[Any]] = {
    "accountId": ["11112222333", "1111222233334", "abcdefghijkl", 111122223333, None],
    "startDate": ["2026-13-01", "2026-02-29", "2026-1-1", "20260101", "2026-01-01T00:00:00Z", 1],
    "endDate": ["2026-00-10", "2026-04-31", "", "yesterday"],
    "serviceCode": ["", "Amazon-S3", "amazon s3", "-amazon", "a" * 129, ["amazon-s3"]],
    "severityCode": ["sev1", "URGENT", ""],
    "rcaCategory": ["throttling", "Throttling ", "Other", ""],
    "lifecycleCategory": ["monitoring", "Unknown", ""],
    "groupBy": ["subject", "rca_category; DROP", "", 1],
    "granularity": ["hour", "year", "MONTH"],
    "dimensions": ["monthly", ["hourly"], [], ["monthly", "monthly"], [1]],
    "limit": [0, 51, -1, "5", 2.5],
    "topServices": [0, 51, "10"],
    "maxCases": [0, 201, "100"],
    "maxRows": [0, 1001, "10"],
    "question": ["", "x" * 2001, 7],
    "data": ["x" * 60_001, {"k": "v"}],
    "sql": ["", "x" * 8001, ["SELECT 1"]],
    "queryExecutionId": ["abc", "0f8e2a8c-4b6b-4a52-9e0f-1c2d3e4f5a6"],
    "nextToken": ["", "has space", "x" * 2049],
}

BASE_ARGUMENTS: dict[str, dict[str, Any]] = {
    "analyze_case_summaries": {"startDate": "2025-01-01", "endDate": "2026-12-31"},
    "analyze_with_bedrock": {"question": "q"},
    "query_athena": {"sql": "SELECT 1 FROM case_summary"},
}


def schemas() -> dict[str, dict[str, Any]]:
    return {t["name"]: t["inputSchema"] for t in build_registry().list_tools()}


def property_cases() -> list[tuple[str, str]]:
    return [(tool, prop) for tool, schema in schemas().items() for prop in schema["properties"]]


def expected_count(filters: dict[str, Any]) -> int:
    """Independent evaluation of the documented filter semantics over the synthetic records."""
    total = 0
    for row in dataset():
        created = date.fromisoformat(str(row["timecreated"])[:10])
        if "accountId" in filters and row["account_number"] != filters["accountId"]:
            continue
        if "startDate" in filters and created < date.fromisoformat(filters["startDate"]):
            continue
        if "endDate" in filters and created > date.fromisoformat(filters["endDate"]):
            continue
        if "serviceCode" in filters and row["servicecode"] != filters["serviceCode"]:
            continue
        if "severityCode" in filters and row["severitycode"] != filters["severityCode"]:
            continue
        if "rcaCategory" in filters and row["rca_category"] != filters["rcaCategory"]:
            continue
        if (
            "lifecycleCategory" in filters
            and row["lifecycle_category"] != filters["lifecycleCategory"]
        ):
            continue
        total += 1
    return total


def selected_total(kit: McpKit, tool: str, arguments: dict[str, Any]) -> int:
    if tool == "analyze_case_summaries":
        kit.bedrock.reply("Invented.")
    result = build_registry().call(tool, arguments, kit.context())
    if tool == "get_service_trends":
        return sum(int(s["totalCases"]) for s in result["services"])
    if tool == "analyze_case_summaries":
        return int(result["matchingCases"])
    return int(result["totalCases"])


FILTERING_TOOLS = [
    "get_case_summary",
    "analyze_case_summaries",
    "get_temporal_patterns",
    "get_rca_analysis",
    "get_lifecycle_analysis",
    "get_service_trends",
]


def filter_cases() -> list[tuple[str, str]]:
    all_schemas = schemas()
    return [
        (tool, prop)
        for tool in FILTERING_TOOLS
        for prop in all_schemas[tool]["properties"]
        if prop in FILTER_VALUES
    ]


@pytest.mark.parametrize(("tool", "prop"), filter_cases())
def test_each_advertised_filter_constrains_selection(tool: str, prop: str) -> None:
    kit = make_kit(dataset())
    arguments = {**BASE_ARGUMENTS.get(tool, {}), prop: FILTER_VALUES[prop]}
    if tool == "get_service_trends":
        arguments["topServices"] = 50
    expected = expected_count(arguments)
    assert expected < len(dataset())
    assert selected_total(kit, tool, arguments) == expected


@pytest.mark.parametrize("tool", FILTERING_TOOLS)
def test_combined_filters(tool: str) -> None:
    all_schemas = schemas()
    kit = make_kit(dataset())
    arguments = {
        **BASE_ARGUMENTS.get(tool, {}),
        **{
            k: v
            for k, v in {
                "accountId": ACCOUNT_A,
                "startDate": "2026-01-01",
                "endDate": "2026-02-28",
                "rcaCategory": "Throttling",
            }.items()
            if k in all_schemas[tool]["properties"]
        },
    }
    if tool == "get_service_trends":
        arguments["topServices"] = 50
    assert selected_total(kit, tool, arguments) == expected_count(arguments)


@pytest.mark.parametrize("tool", ["get_case_summary", "get_temporal_patterns", "get_rca_analysis"])
def test_single_day_range_includes_whole_utc_day(tool: str) -> None:
    kit = make_kit(dataset())
    arguments = {"startDate": "2026-01-31", "endDate": "2026-01-31"}
    assert selected_total(kit, tool, arguments) == 1  # 23:59:59 on the 31st


@pytest.mark.parametrize(("tool", "prop"), property_cases())
def test_every_property_rejects_invalid_and_injection_values(tool: str, prop: str) -> None:
    values = list(INVALID_VALUES[prop])
    schema = schemas()[tool]["properties"][prop]
    if schema.get("type") == "string" and prop not in {"question", "data", "sql"}:
        values += INJECTIONS
    for value in values:
        kit = make_kit(dataset())
        arguments = {**BASE_ARGUMENTS.get(tool, {}), prop: value}
        if prop == "nextToken":
            arguments = {"queryExecutionId": "0f8e2a8c-4b6b-4a52-9e0f-1c2d3e4f5a6b", prop: value}
        if prop == "queryExecutionId":
            arguments = {prop: value}
        with pytest.raises(ToolInputError):
            build_registry().call(tool, arguments, kit.context())
        assert kit.external_calls() == 0, (tool, prop, value)


def test_every_property_has_invalid_value_coverage() -> None:
    advertised = {prop for _, prop in property_cases()}
    assert advertised == set(INVALID_VALUES)
    assert {prop for prop in advertised if prop in FILTER_VALUES} == set(FILTER_VALUES)


@pytest.mark.parametrize("tool", FILTERING_TOOLS)
def test_reversed_dates_rejected(tool: str) -> None:
    kit = make_kit(dataset())
    arguments = {**BASE_ARGUMENTS.get(tool, {}), "startDate": "2026-03-02", "endDate": "2026-03-01"}
    with pytest.raises(ToolInputError) as raised:
        build_registry().call(tool, arguments, kit.context())
    assert raised.value.errors[0]["path"] == "endDate"
    assert kit.external_calls() == 0


def test_date_range_span_is_bounded() -> None:
    kit = make_kit(dataset())
    start = date(2000, 1, 1)
    end = start + timedelta(days=366 * 101)
    with pytest.raises(ToolInputError):
        build_registry().call(
            "get_case_summary",
            {"startDate": start.isoformat(), "endDate": end.isoformat()},
            kit.context(),
        )
    assert kit.external_calls() == 0

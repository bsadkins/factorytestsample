"""Tool registry: exactly eight discoverable tools whose advertised JSON Schemas are what the
engine enforces, before any Athena or Bedrock call."""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator
from mcp_kit import dataset, make_kit
from mcp_tools.errors import ToolInputError, UnknownToolError
from mcp_tools.registry import TOOL_NAMES, build_registry
from mcp_tools.schema import SUPPORTED_KEYWORDS, check_schema, validate_arguments

EXPECTED_TOOLS = (
    "query_athena",
    "analyze_with_bedrock",
    "get_case_summary",
    "analyze_case_summaries",
    "get_temporal_patterns",
    "get_rca_analysis",
    "get_lifecycle_analysis",
    "get_service_trends",
)

# One valid call per tool; Bedrock tools need a queued reply.
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

# Invalid arguments per tool: missing required, wrong type, unsupported property, bounds.
INVALID_CALLS: list[tuple[str, Any]] = [
    ("query_athena", {}),
    ("query_athena", {"sql": 42}),
    ("query_athena", {"sql": "SELECT 1", "workGroup": "primary"}),
    ("query_athena", {"sql": "SELECT 1", "database": "other"}),
    ("query_athena", {"sql": "SELECT 1", "outputLocation": "s3://elsewhere/"}),
    (
        "query_athena",
        {"sql": "SELECT 1", "queryExecutionId": "0f8e2a8c-4b6b-4a52-9e0f-1c2d3e4f5a6b"},
    ),
    ("query_athena", {"sql": "SELECT 1", "nextToken": "abc"}),
    ("query_athena", {"queryExecutionId": "not-a-uuid"}),
    ("query_athena", {"sql": "SELECT 1", "maxRows": 0}),
    ("query_athena", {"sql": "SELECT 1", "maxRows": 1001}),
    ("query_athena", {"sql": "SELECT 1", "maxRows": True}),
    ("query_athena", {"sql": "SELECT 1", "maxRows": 1.5}),
    ("query_athena", {"sql": ""}),
    ("analyze_with_bedrock", {}),
    ("analyze_with_bedrock", {"question": ""}),
    ("analyze_with_bedrock", {"question": "x" * 2001}),
    ("analyze_with_bedrock", {"question": "q", "data": "x" * 60_001}),
    ("analyze_with_bedrock", {"question": "q", "modelId": "anthropic.claude-3-opus"}),
    ("analyze_with_bedrock", {"question": "q", "maxTokens": 100_000}),
    ("analyze_with_bedrock", {"question": ["q"]}),
    ("get_case_summary", {"accountId": "12345"}),
    ("get_case_summary", {"accountId": 111122223333}),
    ("get_case_summary", {"rcaCategory": "Throttling"}),
    ("get_case_summary", {"startDate": "2026-02-30"}),
    ("get_case_summary", {"limit": 5}),
    ("analyze_case_summaries", {"startDate": "2026-01-01"}),
    ("analyze_case_summaries", {"endDate": "2026-01-31"}),
    ("analyze_case_summaries", {"startDate": "2026-01-01", "endDate": "2026-01-31", "maxCases": 0}),
    (
        "analyze_case_summaries",
        {"startDate": "2026-01-01", "endDate": "2026-01-31", "maxCases": 201},
    ),
    ("analyze_case_summaries", {"startDate": "01/01/2026", "endDate": "2026-01-31"}),
    ("get_temporal_patterns", {"dimensions": []}),
    ("get_temporal_patterns", {"dimensions": ["hourly"]}),
    ("get_temporal_patterns", {"dimensions": ["monthly", "monthly"]}),
    ("get_temporal_patterns", {"severityCode": "sev1"}),
    ("get_rca_analysis", {"groupBy": "subject"}),
    ("get_rca_analysis", {"rcaCategory": "Throttling"}),
    ("get_rca_analysis", {"limit": 51}),
    ("get_lifecycle_analysis", {"lifecycleCategory": "Monitoring"}),
    ("get_lifecycle_analysis", {"rcaCategory": "Not A Category"}),
    ("get_service_trends", {"granularity": "hour"}),
    ("get_service_trends", {"topServices": 0}),
    ("get_service_trends", {"serviceCode": "Amazon S3"}),
]


def test_exactly_eight_tools_once_each() -> None:
    listed = build_registry().list_tools()
    names = [tool["name"] for tool in listed]
    assert names == list(EXPECTED_TOOLS)
    assert tuple(TOOL_NAMES) == EXPECTED_TOOLS


@pytest.mark.parametrize("tool", build_registry().list_tools(), ids=lambda t: t["name"])
def test_tool_descriptions_and_schemas_are_meaningful(tool: dict[str, Any]) -> None:
    assert set(tool) == {"name", "title", "description", "inputSchema", "annotations"}
    assert len(tool["description"]) >= 80
    schema = tool["inputSchema"]
    Draft202012Validator.check_schema(schema)
    check_schema(schema)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    for name, prop in schema["properties"].items():
        assert prop.get("description"), f"{tool['name']}.{name} lacks a description"
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["annotations"]["destructiveHint"] is False


def test_get_case_summary_is_a_filtered_rca_distribution_not_a_lookup() -> None:
    tool = next(t for t in build_registry().list_tools() if t["name"] == "get_case_summary")
    assert "RCA" in tool["description"]
    assert "distribution" in tool["description"].lower()
    properties = set(tool["inputSchema"]["properties"])
    assert "caseNumber" not in properties
    assert "caseId" not in properties
    assert tool["inputSchema"].get("required", []) == []


def test_unknown_tool_is_rejected_without_external_calls() -> None:
    kit = make_kit(dataset())
    with pytest.raises(UnknownToolError) as raised:
        build_registry().call("drop_everything", {}, kit.context())
    assert raised.value.code == "UNKNOWN_TOOL"
    assert kit.external_calls() == 0


@pytest.mark.parametrize("arguments", [None, {}])
def test_missing_arguments_default_to_empty_object(arguments: dict[str, Any] | None) -> None:
    kit = make_kit(dataset())
    result = build_registry().call("get_case_summary", arguments, kit.context())
    assert result["totalCases"] == 12


@pytest.mark.parametrize("arguments", [[], "x", 3])
def test_non_object_arguments_are_rejected(arguments: Any) -> None:  # noqa: ANN401
    kit = make_kit(dataset())
    with pytest.raises(ToolInputError):
        build_registry().call("get_case_summary", arguments, kit.context())
    assert kit.external_calls() == 0


@pytest.mark.parametrize(
    ("tool", "arguments"), INVALID_CALLS, ids=[f"{t}-{i}" for i, (t, _) in enumerate(INVALID_CALLS)]
)
def test_invalid_arguments_rejected_before_external_calls(tool: str, arguments: Any) -> None:  # noqa: ANN401
    kit = make_kit(dataset())
    registry = build_registry()
    schema = next(t["inputSchema"] for t in registry.list_tools() if t["name"] == tool)
    with pytest.raises(ToolInputError) as raised:
        registry.call(tool, arguments, kit.context())
    assert raised.value.code == "INVALID_ARGUMENTS"
    assert raised.value.errors
    assert kit.external_calls() == 0
    # The advertised schema reaches the same decision as the engine's validator.
    assert not Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    ).is_valid(arguments)


@pytest.mark.parametrize("tool", EXPECTED_TOOLS)
def test_every_tool_is_callable_with_valid_arguments(tool: str) -> None:
    kit = make_kit(dataset())
    if tool in {"analyze_with_bedrock", "analyze_case_summaries"}:
        kit.bedrock.reply("Invented analysis.")
    registry = build_registry()
    schema = next(t["inputSchema"] for t in registry.list_tools() if t["name"] == tool)
    Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(
        VALID_CALLS[tool]
    )
    result = registry.call(tool, VALID_CALLS[tool], kit.context())
    assert isinstance(result, dict)
    assert result
    kit.bedrock.assert_consumed()


def test_validator_agrees_with_jsonschema_on_corpus() -> None:
    registry = build_registry()
    corpus: list[tuple[str, Any]] = [*INVALID_CALLS, *VALID_CALLS.items()]
    corpus += [
        ("get_case_summary", {"startDate": "2026-01-01", "endDate": "2026-12-31"}),
        ("query_athena", {"sql": "SELECT 1", "maxRows": 10.0}),
        ("query_athena", {"queryExecutionId": "0f8e2a8c-4b6b-4a52-9e0f-1c2d3e4f5a6b"}),
        (
            "query_athena",
            {"queryExecutionId": "0f8e2a8c-4b6b-4a52-9e0f-1c2d3e4f5a6b", "nextToken": "AbC="},
        ),
        ("get_case_summary", {"accountId": "111122223333\n"}),
        ("get_case_summary", {"serviceCode": "amazon-s3' OR '1'='1"}),
    ]
    for tool, arguments in corpus:
        schema = next(t["inputSchema"] for t in registry.list_tools() if t["name"] == tool)
        ours = not validate_arguments(schema, arguments)
        theirs = Draft202012Validator(
            schema, format_checker=Draft202012Validator.FORMAT_CHECKER
        ).is_valid(arguments)
        assert ours == theirs, (tool, arguments)


def test_patterns_use_ecmascript_end_anchor_semantics() -> None:
    # Python's `$` also matches before a trailing newline; JSON Schema (ECMA-262) does not.
    schema = next(
        t["inputSchema"] for t in build_registry().list_tools() if t["name"] == "get_case_summary"
    )
    assert validate_arguments(schema, {"serviceCode": "amazon-s3\n"})
    assert not validate_arguments(schema, {"serviceCode": "amazon-s3"})


def test_schema_checker_rejects_keywords_the_validator_does_not_enforce() -> None:
    assert "pattern" in SUPPORTED_KEYWORDS
    with pytest.raises(ValueError, match="unsupported"):
        check_schema({"type": "object", "properties": {"x": {"type": "string", "if": {}}}})


def test_validation_errors_name_paths_without_echoing_values() -> None:
    kit = make_kit()
    secret = "SENTINEL-ARG-55aa"
    with pytest.raises(ToolInputError) as raised:
        build_registry().call("get_case_summary", {"serviceCode": secret}, kit.context())
    payload = raised.value.to_payload()
    assert payload["errors"][0]["path"] == "serviceCode"
    assert secret not in str(payload)

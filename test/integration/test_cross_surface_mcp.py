"""Cross-surface regression, part 2: the same S3 records, read through the Glue table contract into
the SQLite Athena stand-in, answer all eight MCP tools through the real Lambda handler. Results
are checked against hand-written fixture counts; unsafe SQL, out-of-scope filters, the body cap and
time/error paths are rejected before or instead of any Athena or Bedrock call. This is local
evidence only: it proves no deployed IAM, WAF or Athena behaviour."""

from __future__ import annotations

import json
from typing import Any, Final

import pytest
from common.processed_record import PROCESSED_FIELDS
from common.taxonomy import LIFECYCLE, RCA, UNCLASSIFIED
from cross_surface import (
    ACCOUNT_A,
    ACCOUNT_B,
    ACCOUNT_C,
    ANALYSIS_TEXT,
    DYNAMODB,
    EC2,
    EXPECTED_TOOLS,
    LAMBDA,
    MAX_BODY,
    RECORDS,
    S3,
    Endpoint,
    assert_no_case_content,
    nonzero,
)
from mcp_kit import HAIKU, McpKit
from mcp_tools.context import CASE_SUMMARY_TABLE

# --- MCP over the handler --------------------------------------------------------------------


def test_session_initialises_and_discovers_exactly_the_eight_tools(endpoint: Endpoint) -> None:
    init = endpoint.rpc(
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "cross-surface", "version": "1.0.0"},
        },
    )
    status, _, body = endpoint.post(
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    )
    listed = endpoint.rpc("tools/list", {}, request_id="list")

    assert init["result"]["protocolVersion"] == "2025-11-25"
    assert (status, body) == (202, "")
    tools = listed["result"]["tools"]
    assert [tool["name"] for tool in tools] == EXPECTED_TOOLS
    assert all(tool["annotations"]["readOnlyHint"] is True for tool in tools)
    assert endpoint.kit.external_calls() == 0


ALL_RCA: Final = [
    ("Throttling", 6),
    ("Customer Question", 2),
    ("Development Issue", 2),
    ("ICE Error", 2),
    ("Limit reached", 1),
    ("Feature Request", 1),
    ("Service Event", 1),
    (UNCLASSIFIED, 1),
    ("Customer Release", 0),
    ("Lack Monitoring", 0),
    ("Customer Dependency", 0),
    ("Exceeding Capability", 0),
    ("Security Issue", 0),
    ("Transient Issues", 0),
    ("AWS Release", 0),
    ("AWS Single Component Failure", 0),
    ("Upgrade Management", 0),
]


def test_get_case_summary_is_the_full_rca_distribution(endpoint: Endpoint) -> None:
    summary = endpoint.ok("get_case_summary", {})

    assert summary["totalCases"] == 16
    assert summary["filters"] == {}
    assert [(row["rcaCategory"], row["cases"]) for row in summary["distribution"]] == ALL_RCA
    percents = {row["rcaCategory"]: row["percentOfCases"] for row in summary["distribution"]}
    assert percents["Throttling"] == 37.5
    assert percents["ICE Error"] == 12.5
    assert percents["Upgrade Management"] == 0.0
    assert {label for label, _ in ALL_RCA} == {*RCA.labels, UNCLASSIFIED}


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        pytest.param(
            {"accountId": ACCOUNT_A},
            [("Throttling", 3), ("ICE Error", 2), ("Limit reached", 1)],
            id="account",
        ),
        pytest.param(
            {"startDate": "2026-01-29", "endDate": "2026-02-01"},
            [("Throttling", 2), ("Limit reached", 1)],
            id="inclusive-end-date-at-midnight",
        ),
        pytest.param(
            {"serviceCode": LAMBDA, "severityCode": "low"},
            [("Limit reached", 1), ("Development Issue", 1), ("Throttling", 1)],
            id="service-and-severity",
        ),
        pytest.param(
            {"lifecycleCategory": "Monitoring"},
            [("Throttling", 3), ("Limit reached", 1)],
            id="lifecycle",
        ),
        pytest.param({"accountId": "999999999999"}, [], id="no-matches"),
    ],
)
def test_get_case_summary_filters(
    endpoint: Endpoint, arguments: dict[str, Any], expected: list[tuple[str, int]]
) -> None:
    summary = endpoint.ok("get_case_summary", arguments)

    assert nonzero(summary["distribution"], "rcaCategory") == expected
    assert summary["totalCases"] == sum(cases for _, cases in expected)
    assert len(summary["distribution"]) == len(RCA.labels) + 1
    assert summary["filters"] == arguments


def category_groups(result: dict[str, Any]) -> dict[str, list[tuple[str, int]]]:
    return {
        category["category"]: [(group["value"], group["cases"]) for group in category["groups"]]
        for category in result["categories"]
        if category["cases"]
    }


def test_get_rca_analysis_by_service_with_group_limit(endpoint: Endpoint) -> None:
    result = endpoint.ok("get_rca_analysis", {"groupBy": "service", "limit": 2})

    assert result["totalCases"] == 16
    assert result["truncated"] is False
    assert category_groups(result) == {
        "Throttling": [(DYNAMODB, 2), (LAMBDA, 2)],
        "Customer Question": [(EC2, 1), (S3, 1)],
        "Development Issue": [(LAMBDA, 2)],
        "ICE Error": [(EC2, 2)],
        "Limit reached": [(LAMBDA, 1)],
        "Feature Request": [(DYNAMODB, 1)],
        "Service Event": [(S3, 1)],
        UNCLASSIFIED: [("unknown", 1)],
    }
    throttling = result["categories"][0]
    assert throttling["category"] == "Throttling"
    assert (throttling["otherGroups"], throttling["otherGroupCases"]) == (2, 2)
    assert len(result["categories"]) == len(RCA.labels) + 1


def test_get_rca_analysis_by_month_within_a_date_range(endpoint: Endpoint) -> None:
    result = endpoint.ok(
        "get_rca_analysis",
        {"groupBy": "month", "startDate": "2026-01-01", "endDate": "2026-03-31"},
    )

    assert result["totalCases"] == 13
    assert category_groups(result) == {
        "Throttling": [("2026-01", 2), ("2026-02", 2)],
        "Customer Question": [("2026-01", 1), ("2026-02", 1)],
        "Development Issue": [("2026-03", 2)],
        "ICE Error": [("2026-01", 2)],
        "Limit reached": [("2026-01", 1)],
        "Service Event": [("2026-03", 1)],
        UNCLASSIFIED: [("2026-02", 1)],
    }


def test_get_lifecycle_analysis_for_one_account_by_rca(endpoint: Endpoint) -> None:
    result = endpoint.ok(
        "get_lifecycle_analysis", {"accountId": ACCOUNT_B, "groupBy": "rcaCategory"}
    )

    assert result["totalCases"] == 5
    assert category_groups(result) == {
        "Training": [("Customer Question", 2)],
        "Alarming": [("Throttling", 1)],
        "Load Testing": [("Throttling", 1)],
        UNCLASSIFIED: [(UNCLASSIFIED, 1)],
    }
    order = [category["category"] for category in result["categories"]]
    assert order[:4] == ["Training", "Alarming", "Load Testing", UNCLASSIFIED]
    assert set(order) == {*LIFECYCLE.labels, UNCLASSIFIED}


def test_get_lifecycle_analysis_of_throttling_cases_by_severity(endpoint: Endpoint) -> None:
    result = endpoint.ok(
        "get_lifecycle_analysis", {"rcaCategory": "Throttling", "groupBy": "severity"}
    )

    assert result["totalCases"] == 6
    assert result["filters"] == {"rcaCategory": "Throttling"}
    assert category_groups(result) == {
        "Monitoring": [("normal", 2), ("low", 1)],
        "Load Testing": [("high", 1), ("normal", 1)],
        "Alarming": [("critical", 1)],
    }


def test_get_temporal_patterns_finds_the_monday_cluster(endpoint: Endpoint) -> None:
    result = endpoint.ok("get_temporal_patterns", {})

    assert result["totalCases"] == 16
    assert result["timeZone"] == "UTC"
    assert [(d["day"], d["cases"]) for d in result["dayOfWeek"]] == [
        ("Monday", 9),
        ("Tuesday", 0),
        ("Wednesday", 2),
        ("Thursday", 1),
        ("Friday", 1),
        ("Saturday", 2),
        ("Sunday", 1),
    ]
    assert result["dayOfWeek"][0]["isoDay"] == 1
    assert result["dayOfWeek"][0]["percentOfCases"] == 56.2
    assert [(w["days"], w["cases"]) for w in result["weekOfMonth"]] == [
        ("1-7", 6),
        ("8-14", 3),
        ("15-21", 1),
        ("22-28", 2),
        ("29-31", 4),
    ]
    assert result["monthly"] == [
        {"month": "2025-12", "cases": 1},
        {"month": "2026-01", "cases": 6},
        {"month": "2026-02", "cases": 4},
        {"month": "2026-03", "cases": 3},
        {"month": "2026-04", "cases": 0},
        {"month": "2026-05", "cases": 2},
    ]
    # Computed inline with a CTE over the table, never through a view.
    (query,) = [params["QueryString"] for params in endpoint.kit.athena.started()]
    assert query.startswith("WITH cases AS (SELECT from_iso8601_timestamp(timecreated) AS created ")
    assert f"FROM {CASE_SUMMARY_TABLE} " in query


def test_get_temporal_patterns_with_filters_and_selected_dimensions(endpoint: Endpoint) -> None:
    ec2 = endpoint.ok(
        "get_temporal_patterns", {"serviceCode": EC2, "dimensions": ["monthly", "dayOfWeek"]}
    )
    edge = endpoint.ok(
        "get_temporal_patterns", {"startDate": "2026-01-29", "endDate": "2026-02-01"}
    )

    assert ec2["totalCases"] == 4
    assert "weekOfMonth" not in ec2
    assert [(d["day"], d["cases"]) for d in ec2["dayOfWeek"] if d["cases"]] == [("Monday", 4)]
    assert ec2["dayOfWeek"][0]["percentOfCases"] == 100.0
    assert [(m["month"], m["cases"]) for m in ec2["monthly"]] == [
        ("2026-01", 2),
        ("2026-02", 1),
        ("2026-03", 0),
        ("2026-04", 0),
        ("2026-05", 1),
    ]
    assert edge["totalCases"] == 3
    assert [w["cases"] for w in edge["weekOfMonth"]] == [1, 0, 0, 0, 2]
    assert [(d["day"], d["cases"]) for d in edge["dayOfWeek"] if d["cases"]] == [
        ("Thursday", 1),
        ("Saturday", 1),
        ("Sunday", 1),
    ]


def service_cells(
    result: dict[str, Any],
) -> list[tuple[str, int, list[tuple[str, list[tuple[str, int]]]]]]:
    return [
        (
            service["serviceCode"],
            service["totalCases"],
            [
                (period["period"], [(rca["category"], rca["cases"]) for rca in period["rca"]])
                for period in service["periods"]
            ],
        )
        for service in result["services"]
    ]


def test_get_service_trends_is_service_by_month_by_rca(endpoint: Endpoint) -> None:
    result = endpoint.ok("get_service_trends", {"granularity": "month", "topServices": 2})

    assert result["totalServices"] == 5  # four services and the case without one
    assert service_cells(result) == [
        (
            LAMBDA,
            5,
            [
                ("2025-12", [("Throttling", 1)]),
                ("2026-01", [("Limit reached", 1)]),
                ("2026-02", [("Throttling", 1)]),
                ("2026-03", [("Development Issue", 2)]),
            ],
        ),
        (
            EC2,
            4,
            [
                ("2026-01", [("ICE Error", 2)]),
                ("2026-02", [("Customer Question", 1)]),
                ("2026-05", [("Throttling", 1)]),
            ],
        ),
    ]


def test_get_service_trends_weeks_start_on_monday(endpoint: Endpoint) -> None:
    result = endpoint.ok(
        "get_service_trends",
        {"granularity": "week", "startDate": "2026-01-01", "endDate": "2026-01-31"},
    )

    assert result["totalServices"] == 4
    assert service_cells(result) == [
        (EC2, 2, [("2026-01-05", [("ICE Error", 2)])]),
        (S3, 2, [("2026-01-12", [("Throttling", 1)]), ("2026-01-19", [("Customer Question", 1)])]),
        (DYNAMODB, 1, [("2026-01-26", [("Throttling", 1)])]),
        (LAMBDA, 1, [("2026-01-26", [("Limit reached", 1)])]),
    ]


def test_query_athena_runs_an_inline_cte_in_the_fixed_workgroup(endpoint: Endpoint) -> None:
    sql = (
        "WITH monday AS (SELECT servicecode FROM case_summary "
        "WHERE day_of_week(from_iso8601_timestamp(timecreated)) = 1) "
        "SELECT COALESCE(servicecode, 'unknown') AS service, count(*) AS cases "
        "FROM monday GROUP BY 1 ORDER BY cases DESC, service"
    )
    result = endpoint.ok("query_athena", {"sql": sql})

    assert result["status"] == "SUCCEEDED"
    assert result["rows"] == [
        {"service": EC2, "cases": 4},
        {"service": S3, "cases": 3},
        {"service": DYNAMODB, "cases": 1},
        {"service": LAMBDA, "cases": 1},
    ]
    assert result["truncated"] is False
    (started,) = endpoint.kit.athena.started()
    settings = endpoint.kit.settings()
    assert started["WorkGroup"] == settings.workgroup
    assert started["QueryExecutionContext"]["Database"] == settings.database


def test_query_athena_returns_stored_mixed_case_fields_unchanged(endpoint: Endpoint) -> None:
    columns = ", ".join(field.lower() for field in PROCESSED_FIELDS)
    sql = f"SELECT {columns}, account_number FROM case_summary WHERE case_number = '7200000004'"  # noqa: S608
    result = endpoint.ok("query_athena", {"sql": sql})

    (row,) = result["rows"]
    record = RECORDS["7200000004"]
    assert [row[field.lower()] for field in PROCESSED_FIELDS] == list(record.values())
    assert row["account_number"] == ACCOUNT_B


def test_query_athena_pages_through_every_case(endpoint: Endpoint) -> None:
    sql = (
        "SELECT account_number, case_number FROM case_summary ORDER BY account_number, case_number"
    )
    first = endpoint.ok("query_athena", {"sql": sql, "maxRows": 5})
    pages = [first]
    while "nextToken" in pages[-1]:
        pages.append(
            endpoint.ok(
                "query_athena",
                {
                    "queryExecutionId": first["queryExecutionId"],
                    "nextToken": pages[-1]["nextToken"],
                    "maxRows": 5,
                },
            )
        )

    assert first["truncated"] is True
    assert [page["rowCount"] for page in pages] == [5, 5, 5, 1]
    cases = [row["case_number"] for page in pages for row in page["rows"]]
    assert cases == sorted(RECORDS)
    assert len(endpoint.kit.athena.started()) == 1  # resumed pages reuse the one execution


def test_query_athena_long_query_returns_pending_then_resumes(endpoint: Endpoint) -> None:
    athena = endpoint.kit.athena
    athena.polls_before_success = None  # still running when the request budget runs out
    pending = endpoint.ok("query_athena", {"sql": "SELECT count(*) AS cases FROM case_summary"})

    assert pending["status"] == "PENDING"
    assert sum(endpoint.kit.clock.sleeps) <= 25.0
    assert "StopQueryExecution" not in athena.operations()
    athena.executions[pending["queryExecutionId"]].polls_until_done = 0
    resumed = endpoint.ok("query_athena", {"queryExecutionId": pending["queryExecutionId"]})

    assert resumed["status"] == "SUCCEEDED"
    assert resumed["rows"] == [{"cases": 16}]
    assert len(athena.started()) == 1


def bedrock_prompt(kit: McpKit) -> str:
    (request,) = kit.bedrock.requests
    assert request["modelId"] == HAIKU
    body = request["body"]
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert body["max_tokens"] == 1000
    (message,) = body["messages"]
    content = message["content"]
    text = content if isinstance(content, str) else "".join(part["text"] for part in content)
    assert isinstance(text, str)
    return text


def test_analyze_case_summaries_sends_the_most_recent_matching_cases(endpoint: Endpoint) -> None:
    endpoint.kit.bedrock.reply(ANALYSIS_TEXT)
    result = endpoint.ok(
        "analyze_case_summaries",
        {"startDate": "2026-02-01", "endDate": "2026-02-28", "maxCases": 3},
    )

    assert result["status"] == "ANALYSED"
    assert result["matchingCases"] == 4
    assert result["casesAnalysed"] == 3
    assert result["selectionTruncated"] is True
    assert result["analysis"] == ANALYSIS_TEXT
    assert result["modelId"] == HAIKU
    prompt = bedrock_prompt(endpoint.kit)
    assert "these 3 of 4 AWS Support cases" in prompt
    lines = prompt.split("<cases>\n", 1)[1].split("\n</cases>", 1)[0].splitlines()
    sent = [json.loads(line) for line in lines]
    assert [case["caseNumber"] for case in sent] == ["7200000005", "7200000004", "7200000003"]
    assert sent[0]["summary"] == RECORDS["7200000005"]["Case_Summary"]
    assert sent[1]["serviceCode"] is None
    assert "requester-" not in prompt  # requester identities are never sent to the model
    endpoint.kit.bedrock.assert_consumed()


def test_analyze_case_summaries_with_no_matching_cases_skips_bedrock(endpoint: Endpoint) -> None:
    result = endpoint.ok(
        "analyze_case_summaries", {"startDate": "2026-04-01", "endDate": "2026-04-30"}
    )

    assert result["status"] == "NO_CASES"
    assert result["matchingCases"] == 0
    assert endpoint.kit.bedrock.requests == []


def test_analyze_with_bedrock_over_another_tools_output(endpoint: Endpoint) -> None:
    distribution = endpoint.ok("get_case_summary", {"accountId": ACCOUNT_C})
    endpoint.kit.bedrock.reply(ANALYSIS_TEXT)
    data = json.dumps(distribution)
    result = endpoint.ok(
        "analyze_with_bedrock", {"question": "Which root cause dominates?", "data": data}
    )

    assert result["analysis"] == ANALYSIS_TEXT
    assert result["outputTruncated"] is False
    prompt = bedrock_prompt(endpoint.kit)
    assert prompt.startswith("Which root cause dominates?")
    assert f"<data>\n{data}\n</data>" in prompt
    assert distribution["totalCases"] == 5


# --- negative paths --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        pytest.param(
            "DELETE FROM case_summary WHERE account_number = '1'",
            "statement_not_allowed",
            id="delete",
        ),
        pytest.param(
            "SELECT * FROM case_summary; DROP TABLE case_summary", "stacked_statement", id="stacked"
        ),
        pytest.param("SELECT * FROM case_summary -- note", "comment", id="line-comment"),
        pytest.param("SELECT /*+ hint */ * FROM case_summary", "comment", id="block-comment"),
        pytest.param(
            "WITH x AS (SELECT * FROM case_summary) INSERT INTO case_summary SELECT * FROM x",
            "forbidden_keyword",
            id="cte-insert",
        ),
        pytest.param(
            "CREATE TABLE stolen AS SELECT * FROM case_summary", "statement_not_allowed", id="ctas"
        ),
        pytest.param(
            "UNLOAD (SELECT * FROM case_summary) TO 's3://example-bucket/x/' WITH (format='JSON')",
            "statement_not_allowed",
            id="unload",
        ),
        pytest.param(
            "SELECT * FROM other_db.case_summary", "table_not_allowed", id="other-database"
        ),
        pytest.param(
            'SELECT * FROM "hive"."case_insights_mcp_test"."case_summary"',
            "table_not_allowed",
            id="other-catalog",
        ),
        pytest.param(
            "SELECT * FROM information_schema.tables", "table_not_allowed", id="information-schema"
        ),
        pytest.param(
            "SELECT * FROM TABLE(system.query(query => 'SELECT 1'))",
            "forbidden_keyword",
            id="table-function",
        ),
        pytest.param(
            "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT * FROM r",
            "recursive_cte",
            id="recursive-cte",
        ),
        pytest.param(
            "SELECT * FROM secrets WHERE EXISTS "
            "(WITH secrets AS (SELECT 1 AS x) SELECT x FROM secrets)",
            "table_not_allowed",
            id="nested-cte-scope",
        ),
        pytest.param(
            "SELECT * FROM case_summary WHERE servicecode = ?",
            "parameter_placeholder",
            id="placeholder",
        ),
    ],
)
def test_unsafe_sql_is_rejected_before_athena(endpoint: Endpoint, sql: str, reason: str) -> None:
    error = endpoint.failed("query_athena", {"sql": sql})

    assert error["code"] == "INVALID_ARGUMENTS"
    assert [issue.get("reason") for issue in error["errors"]] == [reason]
    assert endpoint.kit.athena.calls == []


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        pytest.param(
            "get_case_summary", {"serviceCode": "amazon-s3' OR '1'='1"}, id="quote-injection"
        ),
        pytest.param("get_case_summary", {"accountId": "21098765432"}, id="short-account"),
        pytest.param("get_case_summary", {"lifecycleCategory": "monitoring"}, id="label-case"),
        pytest.param(
            "get_case_summary",
            {"startDate": "2026-02-01", "endDate": "2026-01-31"},
            id="reversed-dates",
        ),
        pytest.param("get_case_summary", {"startDate": "2026-02-30"}, id="impossible-date"),
        pytest.param("get_rca_analysis", {"rcaCategory": "Throttling"}, id="filter-not-offered"),
        pytest.param("get_temporal_patterns", {"severityCode": "sev1"}, id="severity-enum"),
        pytest.param("get_temporal_patterns", {"dimensions": ["hourly"]}, id="dimension-enum"),
        pytest.param("get_service_trends", {"topServices": 51}, id="top-services-bound"),
        pytest.param(
            "analyze_case_summaries",
            {"startDate": "1999-12-31", "endDate": "2000-01-02"},
            id="date-floor",
        ),
        pytest.param(
            "analyze_case_summaries",
            {"startDate": "2026-01-01", "endDate": "2026-01-31", "maxCases": 201},
            id="max-cases-bound",
        ),
        pytest.param(
            "query_athena",
            {"sql": "SELECT 1", "queryExecutionId": "00000000-0000-4000-8000-000000000000"},
            id="sql-and-resume",
        ),
        pytest.param("query_athena", {"nextToken": "abc"}, id="token-without-query"),
        pytest.param(
            "query_athena", {"sql": "SELECT 1", "workGroup": "primary"}, id="workgroup-override"
        ),
        pytest.param(
            "analyze_with_bedrock", {"question": "q", "modelId": HAIKU}, id="model-override"
        ),
    ],
)
def test_out_of_scope_arguments_are_rejected_before_any_call(
    endpoint: Endpoint, tool: str, arguments: dict[str, Any]
) -> None:
    error = endpoint.failed(tool, arguments)

    assert error["code"] == "INVALID_ARGUMENTS"
    assert endpoint.kit.external_calls() == 0
    for value in arguments.values():
        assert str(value) not in json.dumps(error) or str(value) in {"1", "abc"}


def padded(message: dict[str, Any], size: int) -> bytes:
    data = json.dumps(message).encode("utf-8")
    return data + b" " * (size - len(data))


def test_body_cap_is_32_kib_of_decoded_bytes(endpoint: Endpoint) -> None:
    call = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "get_case_summary", "arguments": {"accountId": ACCOUNT_B}},
    }
    at_cap = endpoint.post(padded(call, MAX_BODY))
    over_cap = endpoint.post(padded(call, MAX_BODY + 1))

    assert at_cap[0] == 200
    assert json.loads(at_cap[2])["result"]["structuredContent"]["totalCases"] == 5
    assert over_cap[0] == 413
    assert len(endpoint.kit.athena.started()) == 1


def test_fixed_query_time_budget_and_failures_are_tool_errors(endpoint: Endpoint) -> None:
    athena = endpoint.kit.athena
    athena.polls_before_success = None
    timeout = endpoint.failed("get_rca_analysis", {})
    assert timeout["code"] == "QUERY_TIMEOUT"
    assert timeout["retryable"] is True
    assert athena.operations()[-1] == "StopQueryExecution"
    assert sum(endpoint.kit.clock.sleeps) <= 25.0

    athena.polls_before_success = 0
    athena.fail_next = ("FAILED", "SYNTAX_ERROR: synthetic failure")
    assert endpoint.failed("get_service_trends", {})["code"] == "QUERY_FAILED"

    endpoint.kit.bedrock.error("ThrottlingException", 429, times=2)
    throttled = endpoint.failed("analyze_with_bedrock", {"question": "Summarise."})
    assert throttled["code"] == "BEDROCK_THROTTLED"
    assert throttled["retryable"] is True

    endpoint.remaining_millis = 4_000
    assert (
        endpoint.failed(
            "analyze_case_summaries", {"startDate": "2026-01-01", "endDate": "2026-01-31"}
        )["code"]
        == "TIME_BUDGET_EXCEEDED"
    )
    assert_no_case_content(endpoint.logs())


def test_unknown_tool_is_a_protocol_error(endpoint: Endpoint) -> None:
    document = endpoint.rpc("tools/call", {"name": "get_case_by_id", "arguments": {}}, request_id=9)

    assert document["error"]["code"] == -32602
    assert document["error"]["data"]["code"] == "UNKNOWN_TOOL"
    assert endpoint.kit.external_calls() == 0

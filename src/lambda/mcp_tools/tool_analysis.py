"""Bedrock-backed tools: `analyze_with_bedrock` (free-form question over supplied data) and
`analyze_case_summaries` (theme analysis of the case summaries created in a date range).

Case selection for `analyze_case_summaries` is a fixed parameterised Athena query with its own
time budget that always leaves enough of the call for the Bedrock invocation. Case records are
passed to the model as JSON lines inside `<cases>` tags, which the system prompt marks as data.
"""

from __future__ import annotations

import json
from typing import Any, Final

from mcp_tools.bedrock_analysis import SYSTEM_PROMPT, analyse
from mcp_tools.context import CASE_SUMMARY_TABLE, ToolContext
from mcp_tools.definitions import ToolDefinition, integer_property, object_schema
from mcp_tools.errors import ToolError
from mcp_tools.filters import TIMESTAMP, build_selection, filter_properties
from mcp_tools.limits import McpLimits
from mcp_tools.queries import run_fixed_query

CASE_FILTERS: Final = (
    "startDate",
    "endDate",
    "accountId",
    "serviceCode",
    "severityCode",
    "rcaCategory",
    "lifecycleCategory",
)
DEFAULT_FOCUS: Final = (
    "Identify the main recurring themes and root causes, notable services or accounts, and "
    "practical operational improvements that would prevent similar cases."
)
# Columns sent to the model, as (JSON key, column). Requester identities are not included.
_CASE_FIELDS: Final = (
    ("caseNumber", "case_number"),
    ("accountId", "account_number"),
    ("createdAt", "timecreated"),
    ("serviceCode", "servicecode"),
    ("severityCode", "severitycode"),
    ("subject", "subject"),
    ("rcaCategory", "rca_category"),
    ("lifecycleCategory", "lifecycle_category"),
    ("summary", "case_summary"),
)


def _question_property(limits: McpLimits, description: str) -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": limits.max_question_chars,
        "description": description,
    }


def analyze_with_bedrock(arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    prompt = arguments["question"]
    if "data" in arguments:
        prompt = (
            f"{prompt}\n\nUse the following data supplied with the question.\n"
            f"<data>\n{arguments['data']}\n</data>"
        )
    return analyse(ctx, prompt)


def _selection_sql(where: str, max_cases: int) -> str:
    columns = ", ".join(column for _, column in _CASE_FIELDS)
    return (
        f"SELECT {columns}, count(*) OVER () AS matching_cases "  # noqa: S608  # nosec B608
        f"FROM {CASE_SUMMARY_TABLE} {where} "
        f"ORDER BY {TIMESTAMP} DESC, account_number, case_number "
        f"LIMIT {int(max_cases)}"
    )


def _prompt(
    cases: list[str], matching: int, arguments: dict[str, Any], filters: dict[str, Any]
) -> str:
    other = {k: v for k, v in filters.items() if k not in {"startDate", "endDate"}}
    lines = [
        f"Analyse these {len(cases)} of {matching} AWS Support cases created between "
        f"{arguments['startDate']} and {arguments['endDate']} (UTC dates, inclusive)"
        + (f", selected with filters {json.dumps(other, sort_keys=True)}" if other else "")
        + ". When not all matching cases are included, they are the most recently created.",
        "",
        f"Focus: {arguments.get('question') or DEFAULT_FOCUS}",
        "",
        "<cases>",
        *cases,
        "</cases>",
    ]
    return "\n".join(lines)


def _fit_cases(rows: list[dict[str, Any]], budget: int) -> list[str]:
    cases: list[str] = []
    used = 0
    for row in rows:
        line = json.dumps(
            {key: row.get(column) for key, column in _CASE_FIELDS}, ensure_ascii=False
        )
        if used + len(line) + 1 > budget:
            break
        cases.append(line)
        used += len(line) + 1
    return cases


def _time_budget_exceeded() -> ToolError:
    return ToolError(
        "Not enough time remains in this request to analyse the cases; retry, or narrow the "
        "date range",
        code="TIME_BUDGET_EXCEEDED",
        retryable=True,
    )


def analyze_case_summaries(arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    limits = ctx.limits
    selection = build_selection(arguments, CASE_FILTERS, limits)
    if ctx.remaining() < limits.min_bedrock_seconds:
        raise _time_budget_exceeded()
    budget = min(limits.analysis_query_budget_seconds, ctx.remaining() - limits.min_bedrock_seconds)
    page = run_fixed_query(
        ctx,
        _selection_sql(selection.where, int(arguments["maxCases"])),
        selection.parameters,
        budget_seconds=budget,
    )
    matching = int(page.rows[0]["matching_cases"]) if page.rows else 0
    result: dict[str, Any] = {
        "dateRange": {"startDate": arguments["startDate"], "endDate": arguments["endDate"]},
        "filters": selection.filters,
        "matchingCases": matching,
    }
    if not page.rows:
        return {
            **result,
            "status": "NO_CASES",
            "casesAnalysed": 0,
            "selectionTruncated": False,
            "analysis": None,
        }
    overhead = len(SYSTEM_PROMPT) + len(_prompt([], matching, arguments, selection.filters)) + 64
    cases = _fit_cases(page.rows, limits.max_prompt_chars - overhead)
    if not cases:
        raise ToolError(
            "The selected case records are too large to analyse; narrow the selection",
            code="ANALYSIS_INPUT_TOO_LARGE",
        )
    if ctx.remaining() < limits.min_bedrock_seconds:
        raise _time_budget_exceeded()
    analysis = analyse(ctx, _prompt(cases, matching, arguments, selection.filters))
    return {
        **result,
        "status": "ANALYSED",
        "casesAnalysed": len(cases),
        "selectionTruncated": len(cases) < matching,
        **analysis,
    }


def definitions(limits: McpLimits) -> tuple[ToolDefinition, ToolDefinition]:
    with_bedrock = ToolDefinition(
        name="analyze_with_bedrock",
        title="Ask Bedrock to analyse supplied data",
        description=(
            "Ask the server's configured Amazon Bedrock model (an approved Anthropic Claude "
            "global inference profile) a question, optionally about data you supply such as "
            "query_athena results. The data is treated as data, not instructions. Output length "
            "is bounded by the server's max tokens; outputTruncated reports when it was cut."
        ),
        input_schema=object_schema(
            {
                "question": _question_property(
                    limits, "The question or analysis request for the model."
                ),
                "data": {
                    "type": "string",
                    "maxLength": limits.max_analysis_data_chars,
                    "description": "Optional data to analyse, for example CSV or JSON rows "
                    "returned by another tool.",
                },
            },
            required=["question"],
        ),
        handler=analyze_with_bedrock,
        idempotent=False,
    )
    case_summaries = ToolDefinition(
        name="analyze_case_summaries",
        title="Analyse case summaries in a date range",
        description=(
            "Select the processed support cases created in a UTC date range (optionally "
            "filtered by account, service, severity, RCA or lifecycle category) and ask the "
            "configured Bedrock model to analyse their summaries for recurring themes, root "
            "causes and improvements. At most maxCases of the most recent matching cases are "
            "sent; the response reports how many matched and how many were analysed."
        ),
        input_schema=object_schema(
            {
                **filter_properties(*CASE_FILTERS),
                "maxCases": integer_property(
                    1,
                    limits.max_cases,
                    limits.default_max_cases,
                    "Maximum number of matching cases (most recent first) to send for analysis.",
                ),
                "question": _question_property(
                    limits, "Optional focus for the analysis, for example a specific concern."
                ),
            },
            required=["startDate", "endDate"],
        ),
        handler=analyze_case_summaries,
        idempotent=False,
    )
    return with_bedrock, case_summaries

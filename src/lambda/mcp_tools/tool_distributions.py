"""RCA and lifecycle distributions: `get_case_summary`, `get_rca_analysis` and
`get_lifecycle_analysis`.

Each runs one fixed, parameterised aggregate query and lists every taxonomy category (plus
`Unclassified`) with its case count, including categories with no matching cases. Group values
that are missing in the data are reported as `unknown`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Any, Final

from common.taxonomy import LIFECYCLE, RCA, UNCLASSIFIED, Taxonomy

from mcp_tools.context import CASE_SUMMARY_TABLE, ToolContext
from mcp_tools.definitions import ToolDefinition, integer_property, object_schema, percent
from mcp_tools.filters import FILTER_SCHEMAS, TIMESTAMP, build_selection, filter_properties
from mcp_tools.limits import McpLimits
from mcp_tools.queries import run_fixed_query

UNKNOWN: Final = "unknown"
ALL_FILTERS: Final = tuple(FILTER_SCHEMAS)
CASE_SUMMARY_FILTERS: Final = tuple(name for name in ALL_FILTERS if name != "rcaCategory")
RCA_FILTERS: Final = CASE_SUMMARY_FILTERS
LIFECYCLE_FILTERS: Final = tuple(name for name in ALL_FILTERS if name != "lifecycleCategory")

# Allowlisted grouping dimensions and their fixed SQL expressions.
GROUP_EXPRESSIONS: Final = {
    "service": "servicecode",
    "severity": "severitycode",
    "account": "account_number",
    "month": f"format_datetime({TIMESTAMP}, 'yyyy-MM')",
    "rcaCategory": "rca_category",
    "lifecycleCategory": "lifecycle_category",
}


def _ordered_categories(taxonomy: Taxonomy, counts: dict[str, int]) -> list[str]:
    """Every category, most cases first; ties keep the taxonomy order with Unclassified and
    any values outside the taxonomy last."""
    known = [*taxonomy.labels, UNCLASSIFIED]
    extra = sorted(set(counts) - set(known))
    order = {label: index for index, label in enumerate([*known, *extra])}
    return sorted(order, key=lambda label: (-counts.get(label, 0), order[label]))


def get_case_summary(arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    selection = build_selection(arguments, CASE_SUMMARY_FILTERS, ctx.limits)
    page = run_fixed_query(
        ctx,
        f"SELECT COALESCE(rca_category, '{UNKNOWN}') AS category, count(*) AS cases "  # noqa: S608  # nosec B608
        f"FROM {CASE_SUMMARY_TABLE} {selection.where} GROUP BY 1",
        selection.parameters,
    )
    counts = {str(row["category"]): int(row["cases"]) for row in page.rows}
    total = sum(counts.values())
    distribution = [
        {
            "rcaCategory": label,
            "cases": counts.get(label, 0),
            "percentOfCases": percent(counts.get(label, 0), total),
        }
        for label in _ordered_categories(RCA, counts)
    ]
    return {"totalCases": total, "distribution": distribution, "filters": selection.filters}


def _category_analysis(
    arguments: dict[str, Any],
    ctx: ToolContext,
    *,
    taxonomy: Taxonomy,
    column: str,
    filters: Sequence[str],
) -> dict[str, Any]:
    selection = build_selection(arguments, filters, ctx.limits)
    group_by = arguments["groupBy"]
    page = run_fixed_query(
        ctx,
        f"SELECT COALESCE({column}, '{UNKNOWN}') AS category, "  # noqa: S608  # nosec B608
        f"COALESCE({GROUP_EXPRESSIONS[group_by]}, '{UNKNOWN}') AS group_value, "
        f"count(*) AS cases FROM {CASE_SUMMARY_TABLE} {selection.where} GROUP BY 1, 2",
        selection.parameters,
    )
    groups: dict[str, dict[str, int]] = defaultdict(dict)
    for row in page.rows:
        groups[str(row["category"])][str(row["group_value"])] = int(row["cases"])
    counts = {category: sum(values.values()) for category, values in groups.items()}
    total = sum(counts.values())
    limit = int(arguments["limit"])
    categories = []
    for label in _ordered_categories(taxonomy, counts):
        ranked = sorted(groups.get(label, {}).items(), key=lambda item: (-item[1], item[0]))
        shown, rest = ranked[:limit], ranked[limit:]
        categories.append(
            {
                "category": label,
                "cases": counts.get(label, 0),
                "percentOfCases": percent(counts.get(label, 0), total),
                "groups": [{"value": value, "cases": cases} for value, cases in shown],
                "otherGroups": len(rest),
                "otherGroupCases": sum(cases for _, cases in rest),
            }
        )
    return {
        "groupBy": group_by,
        "totalCases": total,
        "categories": categories,
        "filters": selection.filters,
        "truncated": page.truncated,
    }


def get_rca_analysis(arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return _category_analysis(
        arguments, ctx, taxonomy=RCA, column="rca_category", filters=RCA_FILTERS
    )


def get_lifecycle_analysis(arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return _category_analysis(
        arguments, ctx, taxonomy=LIFECYCLE, column="lifecycle_category", filters=LIFECYCLE_FILTERS
    )


def _group_properties(limits: McpLimits, choices: Sequence[str]) -> dict[str, dict[str, Any]]:
    return {
        "groupBy": {
            "type": "string",
            "enum": list(choices),
            "default": choices[0],
            "description": "Dimension to break each category down by: "
            + ", ".join(choices)
            + " (month is the UTC creation month, YYYY-MM).",
        },
        "limit": integer_property(
            1,
            limits.max_group_limit,
            limits.default_group_limit,
            "Maximum number of groups listed per category; the rest are summarised in "
            "otherGroups and otherGroupCases.",
        ),
    }


def definitions(limits: McpLimits) -> tuple[ToolDefinition, ToolDefinition, ToolDefinition]:
    case_summary = ToolDefinition(
        name="get_case_summary",
        title="RCA distribution of support cases",
        description=(
            "Return the RCA (root cause analysis) category distribution of the processed "
            "support cases: the number and percentage of cases in every RCA category, including "
            "Unclassified and categories with no cases. All filters are optional (account, UTC "
            "creation date range, service, severity, lifecycle category). This is an aggregate "
            "distribution, not a lookup of individual cases."
        ),
        input_schema=object_schema(filter_properties(*CASE_SUMMARY_FILTERS)),
        handler=get_case_summary,
    )
    rca = ToolDefinition(
        name="get_rca_analysis",
        title="RCA categories broken down by a dimension",
        description=(
            "Count cases per RCA category and break each category down by service, severity, "
            "account, creation month or lifecycle category, with optional filters. Every RCA "
            "category is listed; the top groups per category are returned and the remainder "
            "summarised."
        ),
        input_schema=object_schema(
            {
                **filter_properties(*RCA_FILTERS),
                **_group_properties(
                    limits, ("service", "severity", "account", "month", "lifecycleCategory")
                ),
            }
        ),
        handler=get_rca_analysis,
    )
    lifecycle = ToolDefinition(
        name="get_lifecycle_analysis",
        title="Lifecycle categories broken down by a dimension",
        description=(
            "Count cases per operational lifecycle category (the practice that would have "
            "prevented or shortened the case) and break each category down by service, "
            "severity, account, creation month or RCA category, with optional filters. Every "
            "lifecycle category is listed; the top groups per category are returned."
        ),
        input_schema=object_schema(
            {
                **filter_properties(*LIFECYCLE_FILTERS),
                **_group_properties(
                    limits, ("service", "severity", "account", "month", "rcaCategory")
                ),
            }
        ),
        handler=get_lifecycle_analysis,
    )
    return case_summary, rca, lifecycle

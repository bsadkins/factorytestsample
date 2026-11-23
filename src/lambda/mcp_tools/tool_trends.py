"""Time-based analyses: `get_temporal_patterns` and `get_service_trends`.

Temporal definitions match docs/athena.md and are computed in UTC from `timecreated` through an
inline CTE (no views): ISO day of week (1 = Monday), week of month `(day_of_month - 1) / 7 + 1`
(days 1-7, 8-14, 15-21, 22-28 and 29-31) and calendar month `yyyy-MM`.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Final

from mcp_tools.context import CASE_SUMMARY_TABLE, ToolContext
from mcp_tools.definitions import ToolDefinition, integer_property, object_schema, percent
from mcp_tools.filters import FILTER_SCHEMAS, TIMESTAMP, build_selection, filter_properties
from mcp_tools.limits import McpLimits
from mcp_tools.queries import run_fixed_query

ALL_FILTERS: Final = tuple(FILTER_SCHEMAS)
DIMENSIONS: Final = ("dayOfWeek", "weekOfMonth", "monthly")
DAY_NAMES: Final = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
WEEK_DAYS: Final = ("1-7", "8-14", "15-21", "22-28", "29-31")

# One UNION ALL branch per dimension over the `cases` CTE: (bucket bigint, period varchar).
_BRANCHES: Final = {
    "dayOfWeek": (
        "SELECT 'dayOfWeek' AS dimension, day_of_week(created) AS bucket, "
        "CAST(NULL AS varchar) AS period, count(*) AS cases FROM cases GROUP BY 2"
    ),
    "weekOfMonth": (
        "SELECT 'weekOfMonth' AS dimension, (day_of_month(created) - 1) / 7 + 1 AS bucket, "
        "CAST(NULL AS varchar) AS period, count(*) AS cases FROM cases GROUP BY 2"
    ),
    "monthly": (
        "SELECT 'monthly' AS dimension, CAST(NULL AS bigint) AS bucket, "
        "format_datetime(created, 'yyyy-MM') AS period, count(*) AS cases FROM cases GROUP BY 3"
    ),
}
_TOTAL_BRANCH: Final = (
    "SELECT 'total' AS dimension, CAST(NULL AS bigint) AS bucket, CAST(NULL AS varchar) AS period, "
    "count(*) AS cases FROM cases"
)
PERIOD_EXPRESSIONS: Final = {
    "month": f"format_datetime({TIMESTAMP}, 'yyyy-MM')",
    "week": f"format_datetime(date_trunc('week', {TIMESTAMP}), 'yyyy-MM-dd')",
    "day": f"format_datetime({TIMESTAMP}, 'yyyy-MM-dd')",
}


def _month_range(first: str, last: str) -> list[str]:
    year, month = int(first[:4]), int(first[5:7])
    months = []
    while f"{year:04d}-{month:02d}" <= last:
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)  # noqa: PLR2004
    return months


def get_temporal_patterns(arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    selection = build_selection(arguments, ALL_FILTERS, ctx.limits)
    dimensions = [name for name in DIMENSIONS if name in arguments["dimensions"]]
    branches = [_BRANCHES[name] for name in dimensions]
    sql = (
        f"WITH cases AS (SELECT {TIMESTAMP} AS created "  # noqa: S608  # nosec B608
        f"FROM {CASE_SUMMARY_TABLE} {selection.where}) "
        + " UNION ALL ".join([*branches, _TOTAL_BRANCH])
    )
    page = run_fixed_query(ctx, sql, selection.parameters)
    buckets: dict[str, dict[Any, int]] = defaultdict(dict)
    for row in page.rows:
        key = row["period"] if row["dimension"] == "monthly" else row["bucket"]
        buckets[str(row["dimension"])][key] = int(row["cases"])
    total = buckets["total"].get(None, 0)
    result: dict[str, Any] = {
        "totalCases": total,
        "timeZone": "UTC",
        "filters": selection.filters,
    }
    if "dayOfWeek" in dimensions:
        days = buckets["dayOfWeek"]
        result["dayOfWeek"] = [
            {
                "isoDay": iso,
                "day": name,
                "cases": days.get(iso, 0),
                "percentOfCases": percent(days.get(iso, 0), total),
            }
            for iso, name in enumerate(DAY_NAMES, start=1)
        ]
    if "weekOfMonth" in dimensions:
        weeks = buckets["weekOfMonth"]
        result["weekOfMonth"] = [
            {
                "week": week,
                "days": span,
                "cases": weeks.get(week, 0),
                "percentOfCases": percent(weeks.get(week, 0), total),
            }
            for week, span in enumerate(WEEK_DAYS, start=1)
        ]
    if "monthly" in dimensions:
        months = {key: value for key, value in buckets["monthly"].items() if key is not None}
        observed = sorted(months)
        result["monthly"] = (
            [
                {"month": month, "cases": months.get(month, 0)}
                for month in _month_range(observed[0], observed[-1])
            ]
            if observed
            else []
        )
    return result


def get_service_trends(arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    selection = build_selection(arguments, ALL_FILTERS, ctx.limits)
    granularity = arguments["granularity"]
    page = run_fixed_query(
        ctx,
        "SELECT COALESCE(servicecode, 'unknown') AS service, "  # noqa: S608  # nosec B608
        f"{PERIOD_EXPRESSIONS[granularity]} AS period, "
        "COALESCE(rca_category, 'unknown') AS category, count(*) AS cases "
        f"FROM {CASE_SUMMARY_TABLE} {selection.where} GROUP BY 1, 2, 3",
        selection.parameters,
    )
    cells: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(dict))
    for row in page.rows:
        period = "unknown" if row["period"] is None else str(row["period"])
        cells[str(row["service"])][period][str(row["category"])] = int(row["cases"])
    totals = {
        service: sum(sum(rca.values()) for rca in periods.values())
        for service, periods in cells.items()
    }
    ranked = sorted(totals, key=lambda service: (-totals[service], service))
    services = []
    for service in ranked[: int(arguments["topServices"])]:
        periods = []
        for period in sorted(cells[service]):
            rca = cells[service][period]
            periods.append(
                {
                    "period": period,
                    "cases": sum(rca.values()),
                    "rca": [
                        {"category": category, "cases": cases}
                        for category, cases in sorted(rca.items(), key=lambda i: (-i[1], i[0]))
                    ],
                }
            )
        services.append({"serviceCode": service, "totalCases": totals[service], "periods": periods})
    return {
        "granularity": granularity,
        "timeZone": "UTC",
        "totalServices": len(totals),
        "services": services,
        "filters": selection.filters,
        "truncated": page.truncated,
    }


def definitions(limits: McpLimits) -> tuple[ToolDefinition, ToolDefinition]:
    temporal = ToolDefinition(
        name="get_temporal_patterns",
        title="When support cases are opened",
        description=(
            "Count cases by the UTC day of week they were created (ISO, Monday first), by week "
            "of month (days 1-7, 8-14, 15-21, 22-28, 29-31) and by calendar month, with optional "
            "filters. Days and weeks with no cases are listed with zero, and months between the "
            "first and last observed month are filled with zero."
        ),
        input_schema=object_schema(
            {
                **filter_properties(*ALL_FILTERS),
                "dimensions": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(DIMENSIONS)},
                    "minItems": 1,
                    "maxItems": len(DIMENSIONS),
                    "uniqueItems": True,
                    "default": list(DIMENSIONS),
                    "description": "Which patterns to return: dayOfWeek, weekOfMonth and/or "
                    "monthly (default: all three).",
                },
            }
        ),
        handler=get_temporal_patterns,
    )
    trends = ToolDefinition(
        name="get_service_trends",
        title="Service case trends by RCA category",
        description=(
            "Show how case volumes change over time for each AWS service: cases per service per "
            "UTC month, ISO week (starting Monday) or day, each broken down by RCA category, "
            "with optional filters. Services are ranked by total cases and the top services are "
            "returned; totalServices reports how many services matched."
        ),
        input_schema=object_schema(
            {
                **filter_properties(*ALL_FILTERS),
                "granularity": {
                    "type": "string",
                    "enum": list(PERIOD_EXPRESSIONS),
                    "default": "month",
                    "description": "Period size: month (YYYY-MM), week (YYYY-MM-DD of the "
                    "Monday) or day (YYYY-MM-DD).",
                },
                "topServices": integer_property(
                    1,
                    limits.max_top_services,
                    limits.default_top_services,
                    "Maximum number of services (most cases first) to return.",
                ),
            }
        ),
        handler=get_service_trends,
    )
    return temporal, trends

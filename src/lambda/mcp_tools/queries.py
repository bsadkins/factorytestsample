"""Execution of the engine's own fixed, parameterised queries.

Before submission each generated statement passes the same read-only SQL guard as client SQL
(with `?` placeholders allowed) and its placeholder count must equal the number of bound values.
A fixed query that is still running at its budget is stopped and reported as QUERY_TIMEOUT; one
whose results cannot all be read within the budget fails with TIME_BUDGET_EXCEEDED rather than
returning partial aggregates.
"""

from __future__ import annotations

from collections.abc import Sequence

from mcp_tools.athena import ResultPage, read_results, start_query, stop_query, wait_for_query
from mcp_tools.context import ToolContext
from mcp_tools.errors import TimeBudgetExceededError, ToolError
from mcp_tools.sql_guard import SqlPolicy, validate_read_only_sql


def sql_policy(ctx: ToolContext) -> SqlPolicy:
    """The read-only boundary for this server's database and limits."""
    return SqlPolicy(
        database=ctx.settings.database,
        max_chars=ctx.limits.max_sql_chars,
        max_tokens=ctx.limits.max_sql_tokens,
        max_depth=ctx.limits.max_sql_depth,
    )


def run_fixed_query(
    ctx: ToolContext,
    sql: str,
    parameters: Sequence[str],
    *,
    max_rows: int | None = None,
    budget_seconds: float | None = None,
) -> ResultPage:
    """Run a generated query and return its rows (truncation is reported, never hidden).

    Every Athena call, including cancellation, ends within `budget_seconds` (default: the rest
    of the call's time budget).
    """
    validated = validate_read_only_sql(sql, sql_policy(ctx), allow_parameters=True)
    if validated.parameter_count != len(parameters):
        raise ValueError("fixed query placeholder count does not match its parameters")
    if budget_seconds is not None:
        ctx = ctx.bounded(budget_seconds)
    query_id = start_query(ctx, validated.text, parameters)
    status = wait_for_query(ctx, query_id)
    if status.state == "PENDING":
        stop_query(ctx, query_id)
        raise ToolError(
            "The Athena query did not finish within the time budget and was cancelled; "
            "narrow the filters (for example the date range) and retry",
            code="QUERY_TIMEOUT",
            retryable=True,
            details={"queryExecutionId": query_id},
        )
    page = read_results(
        ctx,
        status,
        max_rows=max_rows or ctx.limits.fixed_query_max_rows,
        max_bytes=ctx.limits.fixed_query_max_bytes,
    )
    if page.truncation_reason == "timeLimit":
        raise TimeBudgetExceededError(
            "The Athena query finished but its results could not be read within the time "
            "budget; narrow the filters (for example the date range) and retry",
            details={"queryExecutionId": query_id},
        )
    return page

"""`query_athena`: run one read-only statement, or resume a query this server started.

Client SQL passes the read-only guard and runs in the server's fixed workgroup and database. A
query still running at the call's time budget is returned as PENDING with its execution ID; a
later call with that ID (and optionally a `nextToken`) resumes it. Resuming re-checks that the
stored query belongs to this workgroup and database and that its text passes the same guard, so
the ID cannot be used to read other queries' results.
"""

from __future__ import annotations

from typing import Any, Final

from mcp_tools.athena import (
    QueryStatus,
    describe_query,
    query_not_found,
    read_results,
    start_query,
    wait_for_query,
)
from mcp_tools.context import DATA_CATALOG, ToolContext
from mcp_tools.definitions import ToolDefinition, integer_property, object_schema
from mcp_tools.errors import SqlRejectedError
from mcp_tools.limits import McpLimits
from mcp_tools.queries import sql_policy
from mcp_tools.sql_guard import validate_read_only_sql

_UUID_PATTERN: Final = (
    "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _schema(limits: McpLimits) -> dict[str, Any]:
    return object_schema(
        {
            "sql": {
                "type": "string",
                "minLength": 1,
                "maxLength": limits.max_sql_chars,
                "description": "One read-only statement (SELECT, WITH ... SELECT, SHOW or "
                "DESCRIBE) over the case_summary table. Comments, multiple statements, writes "
                "and other databases or catalogs are rejected.",
            },
            "queryExecutionId": {
                "type": "string",
                "pattern": _UUID_PATTERN,
                "minLength": 36,
                "maxLength": 36,
                "description": "Resume a query previously started by this tool (for example "
                "one that returned status PENDING) instead of running new SQL.",
            },
            "nextToken": {
                "type": "string",
                "pattern": "^[A-Za-z0-9+/=_-]+$",
                "minLength": 1,
                "maxLength": 2048,
                "description": "Continue reading results from where a truncated response "
                "stopped; requires queryExecutionId.",
            },
            "maxRows": integer_property(
                1,
                limits.max_rows,
                limits.default_max_rows,
                "Maximum number of result rows to return in this response.",
            ),
        },
        oneOf=[{"required": ["sql"]}, {"required": ["queryExecutionId"]}],
        dependentRequired={"nextToken": ["queryExecutionId"]},
    )


def _check_resumable(ctx: ToolContext, query_id: str) -> dict[str, Any]:
    """The stored query's description, once it is known to be one this tool could have run."""
    execution = describe_query(ctx, query_id)
    context = execution.get("QueryExecutionContext", {})
    if (
        execution.get("WorkGroup") != ctx.settings.workgroup
        or context.get("Database") != ctx.settings.database
        or context.get("Catalog", DATA_CATALOG) != DATA_CATALOG
    ):
        raise query_not_found()
    try:
        validate_read_only_sql(str(execution.get("Query", "")), sql_policy(ctx))
    except SqlRejectedError:
        raise query_not_found() from None
    return execution


def _succeeded(
    ctx: ToolContext, status: QueryStatus, max_rows: int, next_token: str | None
) -> dict[str, Any]:
    page = read_results(
        ctx, status, max_rows=max_rows, max_bytes=ctx.limits.max_result_bytes, next_token=next_token
    )
    result: dict[str, Any] = {
        "status": "SUCCEEDED",
        "queryExecutionId": status.query_execution_id,
        "columns": page.columns,
        "rows": page.rows,
        "rowCount": len(page.rows),
        "truncated": page.truncated,
    }
    if page.truncated:
        result["truncationReason"] = page.truncation_reason
    if page.next_token is not None:
        result["nextToken"] = page.next_token
    if status.data_scanned_bytes is not None:
        result["dataScannedBytes"] = status.data_scanned_bytes
    if status.execution_time_ms is not None:
        result["engineExecutionTimeMillis"] = status.execution_time_ms
    return result


def query_athena(arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    max_rows = int(arguments["maxRows"])
    first: dict[str, Any] | None = None
    if "sql" in arguments:
        validated = validate_read_only_sql(arguments["sql"], sql_policy(ctx))
        query_id = start_query(ctx, validated.text)
    else:
        query_id = arguments["queryExecutionId"]
        first = _check_resumable(ctx, query_id)
    status = wait_for_query(ctx, query_id, first=first)
    if status.state == "PENDING":
        return {
            "status": "PENDING",
            "queryExecutionId": query_id,
            "message": "The query is still running. Call query_athena again with this "
            "queryExecutionId to fetch the results.",
        }
    return _succeeded(ctx, status, max_rows, arguments.get("nextToken"))


def definition(limits: McpLimits) -> ToolDefinition:
    return ToolDefinition(
        name="query_athena",
        title="Query case data with Athena SQL",
        description=(
            "Run a single read-only SQL statement (SELECT, WITH ... SELECT, SHOW or DESCRIBE) "
            "against the processed support case table `case_summary` in the server's fixed "
            "Athena workgroup and database. Returns typed rows with explicit truncation and a "
            "nextToken for more rows. Long-running queries return status PENDING with a "
            "queryExecutionId that can be passed back to resume."
        ),
        input_schema=_schema(limits),
        handler=query_athena,
    )

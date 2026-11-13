"""Bounded Athena execution in the configured workgroup and database.

Every query is started with the server-side workgroup and `AwsDataCatalog`/database context and
no result configuration (the workgroup enforces its SSE-S3 results location and bytes-scanned
cutoff). Polling backs off between `GetQueryExecution` calls and stops at the call's budget.
Results are read page by page up to an explicit row cap and response-size cap. SQL text, result
rows and Athena failure reasons are never logged.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal

from aws_lambda_powertools import Logger
from botocore.exceptions import BotoCoreError, ClientError
from common.retry import RetryPolicy, is_retryable_error, is_throttling_error, retry_with_backoff
from common.safe_logging import aws_error_code, error_fields

from mcp_tools.context import DATA_CATALOG, ToolContext
from mcp_tools.errors import ToolError

if TYPE_CHECKING:
    from types_boto3_athena.type_defs import GetQueryResultsOutputTypeDef

logger = Logger(child=True)

QueryState = Literal["SUCCEEDED", "PENDING"]
_INTEGER_TYPES: Final = frozenset({"tinyint", "smallint", "integer", "int", "bigint"})
_FLOAT_TYPES: Final = frozenset({"double", "float", "real"})
MAX_REASON_CHARS: Final = 500


@dataclass(frozen=True, slots=True)
class QueryStatus:
    """Outcome of waiting for a query: finished successfully, or still running at the budget."""

    query_execution_id: str
    state: QueryState
    statement_type: str | None = None
    data_scanned_bytes: int | None = None
    execution_time_ms: int | None = None


@dataclass(slots=True)
class ResultPage:
    """Rows read from a finished query, with the explicit truncation outcome."""

    columns: list[dict[str, str]]
    rows: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    truncation_reason: str | None = None
    next_token: str | None = None


def _athena_failure(exc: Exception, operation: str) -> ToolError:
    logger.warning("Athena request failed", extra={"operation": operation, **error_fields(exc)})
    if isinstance(exc, ClientError) and is_throttling_error(exc):
        return ToolError(
            "Athena is throttling requests; retry shortly",
            code="ATHENA_THROTTLED",
            retryable=True,
            details={"awsErrorCode": aws_error_code(exc)},
        )
    details = {"awsErrorCode": aws_error_code(exc)} if aws_error_code(exc) else {}
    return ToolError(
        f"Athena {operation} failed",
        code="ATHENA_ERROR",
        retryable=is_retryable_error(exc),
        details=details,
    )


def start_query(ctx: ToolContext, sql: str, parameters: Sequence[str] = ()) -> str:
    """Start `sql` in the configured workgroup/database; return the query execution ID.

    `parameters` are SQL literals bound in order to `?` placeholders (fixed tool queries only).
    Throttling is retried a bounded number of times within the call's deadline.
    """
    request: dict[str, Any] = {
        "QueryString": sql,
        "QueryExecutionContext": {"Catalog": DATA_CATALOG, "Database": ctx.settings.database},
        "WorkGroup": ctx.settings.workgroup,
    }
    if parameters:
        request["ExecutionParameters"] = list(parameters)
    policy = RetryPolicy(
        max_attempts=ctx.limits.athena_start_attempts,
        base_delay_seconds=0.25,
        max_delay_seconds=1.0,
    )
    try:
        response = retry_with_backoff(
            lambda: ctx.clients.athena.start_query_execution(**request),
            policy=policy,
            is_retryable=is_throttling_error,
            sleep=ctx.sleep,
            clock=ctx.clock,
            deadline=ctx.deadline,
            operation_name="athena:StartQueryExecution",
        )
    except (ClientError, BotoCoreError) as exc:
        raise _athena_failure(exc, "StartQueryExecution") from None
    query_id = response["QueryExecutionId"]
    logger.info("Athena query started", extra={"query_execution_id": query_id})
    return query_id


def describe_query(ctx: ToolContext, query_id: str) -> dict[str, Any]:
    """Return the `QueryExecution` description, or raise QUERY_NOT_FOUND / ATHENA_ERROR."""
    try:
        response = ctx.clients.athena.get_query_execution(QueryExecutionId=query_id)
    except ClientError as exc:
        if aws_error_code(exc) == "InvalidRequestException":
            raise query_not_found() from None
        raise _athena_failure(exc, "GetQueryExecution") from None
    except BotoCoreError as exc:
        raise _athena_failure(exc, "GetQueryExecution") from None
    return dict(response["QueryExecution"])


def query_not_found() -> ToolError:
    return ToolError(
        "No query with that ID exists in this server's workgroup and database",
        code="QUERY_NOT_FOUND",
    )


def _terminal_failure(execution: dict[str, Any]) -> ToolError:
    status = execution.get("Status", {})
    state = status.get("State")
    reason = str(status.get("StateChangeReason") or "")[:MAX_REASON_CHARS]
    logger.info(
        "Athena query did not succeed",
        extra={"query_execution_id": execution.get("QueryExecutionId"), "state": state},
    )
    details = {"queryExecutionId": execution.get("QueryExecutionId")}
    if reason:
        details["athenaReason"] = reason
    if state == "CANCELLED":
        return ToolError("The Athena query was cancelled", code="QUERY_CANCELLED", details=details)
    return ToolError("The Athena query failed", code="QUERY_FAILED", details=details)


def wait_for_query(ctx: ToolContext, query_id: str, budget_seconds: float) -> QueryStatus:
    """Poll until the query finishes or `budget_seconds` (capped at the call deadline) pass.

    Returns SUCCEEDED or PENDING; FAILED and CANCELLED raise. The number of polls is bounded by
    `max_polls` and every poll after the first follows a back-off sleep, so this never spins.
    """
    stop_at = min(ctx.deadline, ctx.clock() + max(budget_seconds, 0.0))
    delay = ctx.limits.poll_initial_seconds
    for poll in range(ctx.limits.max_polls):
        execution = describe_query(ctx, query_id)
        state = execution.get("Status", {}).get("State")
        if state == "SUCCEEDED":
            statistics = execution.get("Statistics", {})
            return QueryStatus(
                query_execution_id=query_id,
                state="SUCCEEDED",
                statement_type=execution.get("StatementType"),
                data_scanned_bytes=statistics.get("DataScannedInBytes"),
                execution_time_ms=statistics.get("EngineExecutionTimeInMillis"),
            )
        if state in {"FAILED", "CANCELLED"}:
            raise _terminal_failure(execution)
        remaining = stop_at - ctx.clock()
        if remaining <= 0 or poll == ctx.limits.max_polls - 1:
            break
        ctx.sleep(min(delay, remaining))
        delay = min(delay * 2, ctx.limits.poll_max_seconds)
    logger.info(
        "Athena query still running at the time budget", extra={"query_execution_id": query_id}
    )
    return QueryStatus(query_execution_id=query_id, state="PENDING")


def stop_query(ctx: ToolContext, query_id: str) -> None:
    """Best-effort cancellation of a query that ran past a fixed tool's budget."""
    try:
        ctx.clients.athena.stop_query_execution(QueryExecutionId=query_id)
    except (ClientError, BotoCoreError) as exc:
        logger.warning(
            "Could not stop Athena query",
            extra={"query_execution_id": query_id, **error_fields(exc)},
        )


def _convert(value: str | None, column_type: str) -> Any:  # noqa: ANN401
    if value is None:
        return None
    kind = column_type.lower()
    try:
        if kind in _INTEGER_TYPES:
            return int(value)
        if kind in _FLOAT_TYPES:
            return float(value)
    except ValueError:
        return value
    if kind == "boolean":
        return value.lower() == "true"
    return value


def _page(
    ctx: ToolContext, query_id: str, max_results: int, token: str | None
) -> GetQueryResultsOutputTypeDef:
    request: dict[str, Any] = {"QueryExecutionId": query_id, "MaxResults": max_results}
    if token is not None:
        request["NextToken"] = token
    try:
        return ctx.clients.athena.get_query_results(**request)
    except ClientError as exc:
        if aws_error_code(exc) == "InvalidRequestException" and token is not None:
            raise ToolError(
                "The nextToken is not valid for this query", code="INVALID_NEXT_TOKEN"
            ) from None
        raise _athena_failure(exc, "GetQueryResults") from None
    except BotoCoreError as exc:
        raise _athena_failure(exc, "GetQueryResults") from None


def read_results(
    ctx: ToolContext,
    status: QueryStatus,
    *,
    max_rows: int,
    max_bytes: int,
    next_token: str | None = None,
) -> ResultPage:
    """Read up to `max_rows` rows (and at most `max_bytes` of JSON) from a finished query.

    For DML the first row of the first page is Athena's header row and is skipped. When more rows
    exist the result is marked truncated; a row-cap truncation carries Athena's `next_token` for
    resumption, a size-cap truncation does not (the page boundary no longer matches).
    """
    skip_header = status.statement_type == "DML" and next_token is None
    token = next_token
    result = ResultPage(columns=[])
    size = 0
    # Athena may return short or empty pages; this bounds the loop regardless.
    max_pages = max_rows // ctx.limits.results_page_size + 3
    for _ in range(max_pages):
        wanted = max_rows - len(result.rows) + (1 if skip_header else 0)
        response = _page(
            ctx, status.query_execution_id, min(ctx.limits.results_page_size, wanted), token
        )
        result_set = response["ResultSet"]
        columns = [
            {
                "name": str(info.get("Name") or info.get("Label")),
                "type": str(info.get("Type", "varchar")),
            }
            for info in result_set.get("ResultSetMetadata", {}).get("ColumnInfo", [])
        ]
        if not result.columns:
            result.columns = columns
        rows = result_set.get("Rows", [])
        if skip_header and rows:
            rows = rows[1:]
            skip_header = False
        for raw in rows:
            data = raw.get("Data", [])
            row = {
                column["name"]: _convert(
                    data[i].get("VarCharValue") if i < len(data) else None, column["type"]
                )
                for i, column in enumerate(result.columns)
            }
            row_size = len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
            if size + row_size > max_bytes:
                result.truncated = True
                result.truncation_reason = "responseSizeLimit"
                return result
            size += row_size
            result.rows.append(row)
        token = response.get("NextToken")
        if token is None:
            return result
        if len(result.rows) >= max_rows or ctx.remaining() <= 0:
            break
    result.truncated = True
    result.truncation_reason = "rowLimit" if len(result.rows) >= max_rows else "pageLimit"
    result.next_token = token
    return result

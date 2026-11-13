"""Test kit for the MCP tool engine: a SQLite-backed Athena stand-in that really executes the
generated SQL over synthetic processed records, a stubbed Bedrock runtime client, a fake clock
and tool-context helpers.

The Athena stand-in validates every request against the botocore Athena service model, records
each call and evaluates queries with SQLite plus Python implementations of the Trino functions
the tools use (`from_iso8601_timestamp`, `day_of_week`, `day_of_month`, `format_datetime`,
`date_trunc`). Results come back in the GetQueryResults shape (strings, header row first for
DML), so the engine's result parsing is exercised too. All data is invented.
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import boto3
import botocore.session
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber
from botocore.validate import validate_parameters
from mcp_tools.context import McpClients, McpSettings, ToolContext
from mcp_tools.limits import McpLimits

HAIKU: Final = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
SONNET: Final = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
WORKGROUP: Final = "CaseInsights-mcp-test"
DATABASE: Final = "case_insights_mcp_test"
ACCOUNT_A: Final = "111122223333"
ACCOUNT_B: Final = "444455556666"
SENTINEL_SUMMARY: Final = "SENTINEL-SUMMARY-7f3a"

COLUMNS: Final = (
    "caseid",
    "displayid",
    "subject",
    "servicecode",
    "categorycode",
    "severitycode",
    "submittedby",
    "timecreated",
    "case_retrieval_date",
    "case_summary",
    "rca_category",
    "rca_reason",
    "rca_retrieval_date",
    "lifecycle_category",
    "lifecycle_reason",
    "lifecycle_retrieval_date",
    "model_id",
    "prompt_version",
    "account_number",
    "case_number",
)

_ATHENA_MODEL = botocore.session.get_session().get_service_model("athena")


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _from_iso8601_timestamp(value: str | None) -> str | None:
    return None if value is None else _parse_ts(value).isoformat(timespec="microseconds")


def _day_of_week(value: str | None) -> int | None:
    return None if value is None else _parse_ts(value).isoweekday()


def _day_of_month(value: str | None) -> int | None:
    return None if value is None else _parse_ts(value).day


_JODA = {"yyyy-MM": "%Y-%m", "yyyy-MM-dd": "%Y-%m-%d", "EEEE": "%A"}


def _format_datetime(value: str | None, pattern: str) -> str | None:
    if value is None:
        return None
    return _parse_ts(value).strftime(_JODA[pattern])


def _date_trunc(unit: str, value: str | None) -> str | None:
    if value is None:
        return None
    ts = _parse_ts(value)
    day = ts.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "day":
        result = day
    elif unit == "week":
        result = day - timedelta(days=day.weekday())
    elif unit == "month":
        result = day.replace(day=1)
    else:
        raise ValueError(unit)
    return result.isoformat(timespec="microseconds")


def _literal(parameter: str) -> object:
    """Decode an Athena execution parameter, which must be a SQL literal."""
    if re.fullmatch(r"-?[0-9]+", parameter):
        return int(parameter)
    match = re.fullmatch(r"'((?:[^']|'')*)'", parameter, flags=re.DOTALL)
    if match is None:
        raise AssertionError(f"execution parameter is not a SQL literal: {parameter!r}")
    return match.group(1).replace("''", "'")


def client_error(code: str, operation: str, status: int = 400) -> ClientError:
    """A botocore ClientError whose message carries a sentinel that must never be surfaced."""
    return ClientError(
        {
            "Error": {"Code": code, "Message": "synthetic SENTINEL-ERR"},
            "ResponseMetadata": {"HTTPStatusCode": status},  # type: ignore[typeddict-item]
        },
        operation,
    )


@dataclass
class _Execution:
    id: str
    query: str
    workgroup: str
    database: str
    catalog: str
    statement_type: str
    state: str
    reason: str | None
    columns: list[tuple[str, str]] = field(default_factory=list)
    rows: list[list[str | None]] = field(default_factory=list)
    polls_until_done: int = 0


def processed_row(  # noqa: PLR0913
    *,
    account: str = ACCOUNT_A,
    case: str,
    created: str,
    service: str | None = "amazon-elastic-compute-cloud-linux",
    severity: str | None = "normal",
    rca: str = "Customer Question",
    lifecycle: str = "Monitoring",
    summary: str | None = None,
) -> dict[str, str | None]:
    """A synthetic processed record as Athena would see it (lower-case columns + partitions)."""
    return {
        "caseid": f"case-{account}-{case}",
        "displayid": case,
        "subject": f"Invented subject {case}",
        "servicecode": service,
        "categorycode": "other",
        "severitycode": severity,
        "submittedby": f"requester-{case}@example.com",
        "timecreated": created,
        "case_retrieval_date": "2026-11-13T00:05:12Z",
        "case_summary": summary or f"Invented summary for case {case}. {SENTINEL_SUMMARY}",
        "rca_category": rca,
        "rca_reason": "Invented reason.",
        "rca_retrieval_date": "2026-11-13T00:06:00Z",
        "lifecycle_category": lifecycle,
        "lifecycle_reason": "Invented reason.",
        "lifecycle_retrieval_date": "2026-11-13T00:07:00Z",
        "model_id": HAIKU,
        "prompt_version": "pv-0123456789abcdef",
        "account_number": account,
        "case_number": case,
    }


class SqliteAthena:
    """Athena client stand-in that executes queries over SQLite.

    `polls_before_success` makes each new query report RUNNING for that many
    GetQueryExecution calls (`None` = never completes). `fail_next` makes the next query end in
    the given terminal state and reason. Calls are recorded as `(operation, params)`.
    """

    def __init__(self, rows: Iterable[dict[str, str | None]] = ()) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.create_function("from_iso8601_timestamp", 1, _from_iso8601_timestamp)
        self.db.create_function("day_of_week", 1, _day_of_week)
        self.db.create_function("day_of_month", 1, _day_of_month)
        self.db.create_function("format_datetime", 2, _format_datetime)
        self.db.create_function("date_trunc", 2, _date_trunc)
        self.db.execute(f"CREATE TABLE case_summary ({', '.join(c + ' TEXT' for c in COLUMNS)})")
        self.insert(rows)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.executions: dict[str, _Execution] = {}
        self.polls_before_success: int | None = 0
        self.fail_next: tuple[str, str] | None = None
        self.start_errors: list[ClientError] = []
        self.results_errors: list[ClientError] = []
        self.canned: dict[str, list[list[str | None]]] = {}

    def insert(self, rows: Iterable[dict[str, str | None]]) -> None:
        placeholders = ", ".join("?" for _ in COLUMNS)
        self.db.executemany(
            f"INSERT INTO case_summary VALUES ({placeholders})",  # noqa: S608
            [tuple(row[c] for c in COLUMNS) for row in rows],
        )

    def _record(self, operation: str, params: dict[str, Any]) -> None:
        shape = _ATHENA_MODEL.operation_model(operation).input_shape
        assert shape is not None
        validate_parameters(params, shape)
        self.calls.append((operation, params))

    def operations(self) -> list[str]:
        return [operation for operation, _ in self.calls]

    def started(self) -> list[dict[str, Any]]:
        return [params for operation, params in self.calls if operation == "StartQueryExecution"]

    def start_query_execution(self, **params: Any) -> dict[str, Any]:  # noqa: ANN401
        self._record("StartQueryExecution", params)
        if self.start_errors:
            raise self.start_errors.pop(0)
        query = params["QueryString"]
        context = params.get("QueryExecutionContext", {})
        first = query.lstrip("( \n").split(None, 1)[0].upper()
        execution = _Execution(
            id=str(uuid.uuid4()),
            query=query,
            workgroup=params.get("WorkGroup", "primary"),
            database=context.get("Database", "default"),
            catalog=context.get("Catalog", "AwsDataCatalog"),
            statement_type="DML" if first in {"SELECT", "WITH"} else "UTILITY",
            state="RUNNING",
            reason=None,
            polls_until_done=-1 if self.polls_before_success is None else self.polls_before_success,
        )
        if self.fail_next is not None:
            execution.state, execution.reason = self.fail_next
            self.fail_next = None
        else:
            self._evaluate(execution, [_literal(p) for p in params.get("ExecutionParameters", [])])
        self.executions[execution.id] = execution
        return {"QueryExecutionId": execution.id}

    def _evaluate(self, execution: _Execution, parameters: Sequence[object]) -> None:
        if execution.statement_type != "DML":
            execution.columns = [("result", "varchar")]
            key = execution.query.split(None, 1)[0].upper()
            execution.rows = self.canned.get(key, [["case_summary"]])
            return
        try:
            cursor = self.db.execute(execution.query, tuple(parameters))
        except sqlite3.Error as exc:
            execution.state, execution.reason = "FAILED", f"SYNTAX_ERROR: {exc}"
            return
        names = [d[0] for d in cursor.description]
        values = cursor.fetchall()
        types: list[str] = []
        for index in range(len(names)):
            sample = next((row[index] for row in values if row[index] is not None), None)
            types.append(
                "bigint"
                if isinstance(sample, int)
                else "double"
                if isinstance(sample, float)
                else "varchar"
            )
        execution.columns = list(zip(names, types, strict=True))
        execution.rows = [
            [None if value is None else str(value) for value in row] for row in values
        ]

    def get_query_execution(self, **params: Any) -> dict[str, Any]:  # noqa: ANN401
        self._record("GetQueryExecution", params)
        execution = self.executions.get(params["QueryExecutionId"])
        if execution is None:
            raise client_error("InvalidRequestException", "GetQueryExecution")
        if execution.state == "RUNNING":
            if execution.polls_until_done == 0:
                execution.state = "SUCCEEDED"
            elif execution.polls_until_done > 0:
                execution.polls_until_done -= 1
        status: dict[str, Any] = {"State": execution.state}
        if execution.reason is not None:
            status["StateChangeReason"] = execution.reason
        return {
            "QueryExecution": {
                "QueryExecutionId": execution.id,
                "Query": execution.query,
                "StatementType": execution.statement_type,
                "WorkGroup": execution.workgroup,
                "QueryExecutionContext": {
                    "Database": execution.database,
                    "Catalog": execution.catalog,
                },
                "Status": status,
                "Statistics": {"DataScannedInBytes": 2048, "EngineExecutionTimeInMillis": 321},
            }
        }

    def get_query_results(self, **params: Any) -> dict[str, Any]:  # noqa: ANN401
        self._record("GetQueryResults", params)
        if self.results_errors:
            raise self.results_errors.pop(0)
        execution = self.executions[params["QueryExecutionId"]]
        if execution.state != "SUCCEEDED":
            raise client_error("InvalidRequestException", "GetQueryResults")
        header = [[name for name, _ in execution.columns]]
        all_rows = (header if execution.statement_type == "DML" else []) + execution.rows
        offset = int(params.get("NextToken", "0"))
        limit = params.get("MaxResults", 1000)
        page = all_rows[offset : offset + limit]
        response: dict[str, Any] = {
            "ResultSet": {
                "Rows": [
                    {"Data": [{} if v is None else {"VarCharValue": v} for v in row]}
                    for row in page
                ],
                "ResultSetMetadata": {
                    "ColumnInfo": [
                        {"Name": name, "Label": name, "Type": kind}
                        for name, kind in execution.columns
                    ]
                },
            },
            "UpdateCount": 0,
        }
        if offset + limit < len(all_rows):
            response["NextToken"] = str(offset + limit)
        return response

    def stop_query_execution(self, **params: Any) -> dict[str, Any]:  # noqa: ANN401
        self._record("StopQueryExecution", params)
        execution = self.executions[params["QueryExecutionId"]]
        execution.state = "CANCELLED"
        return {}


class BedrockStub:
    """A real bedrock-runtime client behind Stubber; request bodies are recorded."""

    def __init__(self) -> None:
        self.client = boto3.client(
            "bedrock-runtime",
            region_name="us-east-1",
            aws_access_key_id="stub",
            aws_secret_access_key="stub",
        )
        self.stubber = Stubber(self.client)
        self.requests: list[dict[str, Any]] = []
        self.client.meta.events.register(
            "provide-client-params.bedrock-runtime.InvokeModel", self._record
        )
        self.stubber.activate()

    def _record(self, params: dict[str, Any], **_: Any) -> None:  # noqa: ANN401
        body = params["body"]
        self.requests.append(
            {
                "modelId": params["modelId"],
                "contentType": params.get("contentType"),
                "accept": params.get("accept"),
                "body": json.loads(body if isinstance(body, (bytes, str)) else body.read()),
            }
        )

    def reply(self, text: str, *, stop: str = "end_turn") -> None:
        self.raw(
            json.dumps(
                {
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": stop,
                    "usage": {"input_tokens": 1200, "output_tokens": 180},
                }
            ).encode()
        )

    def raw(self, payload: bytes) -> None:
        self.stubber.add_response(
            "invoke_model",
            {
                "body": StreamingBody(io.BytesIO(payload), len(payload)),
                "contentType": "application/json",
            },
        )

    def error(self, code: str, status: int = 400, times: int = 1) -> None:
        for _ in range(times):
            self.stubber.add_client_error(
                "invoke_model", service_error_code=code, http_status_code=status
            )

    def assert_consumed(self) -> None:
        self.stubber.assert_no_pending_responses()


@dataclass
class FakeClock:
    """Monotonic clock whose sleep advances time; every sleep is recorded."""

    now: float = 1000.0
    sleeps: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds > 0, "polling must never busy-loop"
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class McpKit:
    athena: SqliteAthena
    bedrock: BedrockStub
    clock: FakeClock
    model_id: str = HAIKU
    max_tokens: int = 1000
    limits: McpLimits = field(default_factory=McpLimits)

    def settings(self) -> McpSettings:
        return McpSettings(
            workgroup=WORKGROUP,
            database=DATABASE,
            model_id=self.model_id,
            max_tokens=self.max_tokens,
        )

    def context(self, remaining_millis: int | None = None) -> ToolContext:
        return ToolContext.start(
            self.settings(),
            McpClients(athena=self.athena, bedrock=self.bedrock.client),  # type: ignore[arg-type]
            limits=self.limits,
            clock=self.clock,
            sleep=self.clock.sleep,
            remaining_millis=remaining_millis,
        )

    def external_calls(self) -> int:
        return len(self.athena.calls) + len(self.bedrock.requests)


def make_kit(rows: Iterable[dict[str, str | None]] = (), **overrides: Any) -> McpKit:  # noqa: ANN401
    return McpKit(athena=SqliteAthena(rows), bedrock=BedrockStub(), clock=FakeClock(), **overrides)


def dataset() -> list[dict[str, str | None]]:
    """Twelve invented cases across two accounts, three services, dates and categories."""
    # fmt: off
    spec: list[tuple[str, str, str, str | None, str | None, str, str]] = [
        # account, case, created, service, severity, rca, lifecycle
        (ACCOUNT_A, "1001", "2026-01-05T09:00:00.000Z", "amazon-s3", "low", "Throttling", "Monitoring"),  # noqa: E501
        (ACCOUNT_A, "1002", "2026-01-06T10:00:00.000Z", "amazon-s3", "high", "Throttling", "Alarming"),  # noqa: E501
        (ACCOUNT_A, "1003", "2026-01-12T11:00:00.000Z", "amazon-ec2", "urgent", "ICE Error", "Architecture"),  # noqa: E501
        (ACCOUNT_A, "1004", "2026-01-31T23:59:59.000Z", "amazon-ec2", "normal", "Customer Question", "Training"),  # noqa: E501
        (ACCOUNT_A, "1005", "2026-02-01T00:00:00.000Z", "aws-lambda", "normal", "Limit reached", "Monitoring"),  # noqa: E501
        (ACCOUNT_A, "1006", "2026-02-15T08:30:00.000Z", "aws-lambda", "critical", "Throttling", "Load Testing"),  # noqa: E501
        (ACCOUNT_B, "2001", "2026-01-05T12:00:00.000Z", "amazon-s3", "normal", "Customer Question", "Training"),  # noqa: E501
        (ACCOUNT_B, "2002", "2026-01-29T12:00:00.000Z", "amazon-ec2", "high", "Unclassified", "Unclassified"),  # noqa: E501
        (ACCOUNT_B, "2003", "2026-02-02T12:00:00.000Z", None, None, "Development Issue", "Code Review"),  # noqa: E501
        (ACCOUNT_B, "2004", "2026-03-03T12:00:00.000Z", "amazon-ec2", "low", "Throttling", "Monitoring"),  # noqa: E501
        (ACCOUNT_B, "2005", "2026-03-09T12:00:00.000Z", "aws-lambda", "urgent", "ICE Error", "Resilience"),  # noqa: E501
        (ACCOUNT_B, "2006", "2025-12-31T23:59:59.000Z", "amazon-s3", "normal", "Throttling", "Monitoring"),  # noqa: E501
    ]
    # fmt: on
    return [
        processed_row(
            account=account,
            case=case,
            created=created,
            service=service,
            severity=severity,
            rca=rca,
            lifecycle=lifecycle,
        )
        for account, case, created, service, severity, rca, lifecycle in spec
    ]


def counts(rows: Sequence[dict[str, Any]], key: str, value_key: str = "cases") -> dict[Any, int]:
    """Map `key` -> `value_key` for a list of result dicts, skipping zero counts."""
    return {row[key]: row[value_key] for row in rows if row[value_key]}


Clock = Callable[[], float]

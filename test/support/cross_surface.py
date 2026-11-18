"""Shared synthetic processed records and drivers for the cross-surface integration tests.

`test/fixtures/cross_surface/processed_records.json` holds sixteen complete processed records in
the stored (mixed-case) field shape with their account numbers; `case_summary_table.json` is the
Glue table contract that test/cdk/cross-surface.test.ts compares with the synthesised Analytics
stack.
The helpers here read the records the way Athena would through that contract, drive the CSV
exporter's real `main()` and the MCP Lambda handler with REST proxy events. All data is invented.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Final

import pytest
from common.processed import case_record_key
from common.processed_record import PROCESSED_FIELDS
from mcp_kit import McpKit
from sigv4_loopback import FixtureLambdaContext

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
FIXTURES: Final = REPO_ROOT / "test" / "fixtures" / "cross_surface"
CSV_SCRIPT: Final = REPO_ROOT / "scripts" / "generate_csv_insights.py"
BUCKET: Final = "case-insights-processed-cross-surface"

ACCOUNT_A: Final = "210987654321"
ACCOUNT_B: Final = "345678901234"
ACCOUNT_C: Final = "567890123456"
EC2: Final = "amazon-elastic-compute-cloud-linux"
S3: Final = "amazon-simple-storage-service"
LAMBDA: Final = "aws-lambda"
DYNAMODB: Final = "amazon-dynamodb"
ANALYSIS_TEXT: Final = "Synthetic analysis: throttling is the most common root cause."
MAX_BODY: Final = 32768

# The fixture table, for reading the expectations below (accounts A, B and C are
# ACCOUNT_A/B/C; times are UTC; '-' is null):
#   case        acct  created              day  service   severity  RCA / lifecycle
#   7100000006  A     2025-12-31 23:59:59  Wed  lambda    low       Throttling / Monitoring
#   7100000001  A     2026-01-05 08:15:00  Mon  ec2       high      ICE Error / Architecture
#   7100000002  A     2026-01-05 23:59:59  Mon  ec2       urgent    ICE Error / Resilience
#   7100000003  A     2026-01-12 09:00:00  Mon  s3        normal    Throttling / Monitoring
#   7100000004  A     2026-01-31 12:00:00  Sat  lambda    low       Limit reached / Monitoring
#   7100000005  A     2026-02-01 00:00:00  Sun  lambda    normal    Throttling / Load Testing
#   7200000001  B     2026-01-19 10:30:00  Mon  s3        normal    Customer Question / Training
#   7200000002  B     2026-01-29 14:00:00  Thu  dynamodb  critical  Throttling / Alarming
#   7200000003  B     2026-02-09 07:45:00  Mon  dynamodb  high      Throttling / Load Testing
#   7200000004  B     2026-02-14 16:20:00  Sat  -         -         Unclassified / Unclassified
#   7200000005  B     2026-02-23 11:11:00  Mon  ec2       normal    Customer Question / Training
#   7300000001  C     2026-03-02 09:00:00  Mon  lambda    urgent    Development Issue / Code Review
#   7300000002  C     2026-03-04 18:00:00  Wed  lambda    low       Development Issue / CICD
#   7300000003  C     2026-03-30 06:00:00  Mon  s3        high      Service Event / Resilience
#   7300000004  C     2026-05-04 13:00:00  Mon  ec2       normal    Throttling / Monitoring
#   7300000005  C     2026-05-22 20:00:00  Fri  dynamodb  normal    Feature Request / Architecture

CSV_ORDER: Final = [
    "7100000006",
    "7100000001",
    "7100000002",
    "7100000003",
    "7100000004",
    "7100000005",
    "7200000001",
    "7200000002",
    "7200000003",
    "7200000004",
    "7200000005",
    "7300000001",
    "7300000002",
    "7300000003",
    "7300000004",
    "7300000005",
]


def load_json(name: str) -> Any:  # noqa: ANN401
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


FIXTURE: Final[list[dict[str, Any]]] = load_json("processed_records.json")
CONTRACT: Final[dict[str, Any]] = load_json("case_summary_table.json")
RECORDS: Final = {item["record"]["displayId"]: item["record"] for item in FIXTURE}
ACCOUNT_OF: Final = {item["record"]["displayId"]: item["accountNumber"] for item in FIXTURE}


def record_key_pattern() -> re.Pattern[str]:
    template = re.escape(CONTRACT["recordKeyTemplate"])
    for partition in CONTRACT["partitionKeys"]:
        name = partition["name"]
        template = template.replace(re.escape("{" + name + "}"), f"(?P<{name}>[^/]+)")
    return re.compile(template)


def openx_row(key: str, data: bytes) -> dict[str, str | None]:
    """What Athena reads for one object through the table contract: partition values from the
    Hive key and, because matching is case-sensitive, each column from its mapped JSON key."""
    match = record_key_pattern().fullmatch(key)
    assert match is not None, key
    document = json.loads(data)
    serde = CONTRACT["serdeParameters"]
    row: dict[str, str | None] = {}
    for column in CONTRACT["columns"]:
        value = document.get(serde[f"mapping.{column['name']}"])
        assert value is None or isinstance(value, str)
        row[column["name"]] = value
    row.update(match.groupdict())
    return row


def athena_rows(client: S3Client, bucket: str) -> list[dict[str, str | None]]:
    rows = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for item in page.get("Contents", []):
            body = client.get_object(Bucket=bucket, Key=item["Key"])["Body"].read()
            rows.append(openx_row(item["Key"], body))
    return rows


def put_fixture_records(client: S3Client, bucket: str) -> None:
    """Store every fixture record at its production key."""
    for item in FIXTURE:
        record = item["record"]
        client.put_object(
            Bucket=bucket,
            Key=case_record_key(item["accountNumber"], record["displayId"]),
            Body=json.dumps(record, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )


def load_csv_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_csv_insights", CSV_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


@dataclass
class CsvExport:
    script: ModuleType
    directory: Path
    calls: list[tuple[str, dict[str, Any]]]
    runs: int = 0

    def run(self, *args: str) -> tuple[int, list[dict[str, str]]]:
        self.runs += 1
        out = self.directory / f"export-{self.runs}.csv"
        code = self.script.main(["--bucket", BUCKET, "--output", str(out), *args])
        if not out.exists():
            return code, []
        raw = out.read_bytes()
        assert b"\r\n" in raw
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""))
        assert reader.fieldnames == ["account_number", *PROCESSED_FIELDS]
        return code, list(reader)


class EndpointContext(FixtureLambdaContext):
    def __init__(self, remaining_millis: int) -> None:
        super().__init__()
        self._remaining = remaining_millis

    def get_remaining_time_in_millis(self) -> int:  # type: ignore[override]
        return self._remaining


@dataclass
class Endpoint:
    """The real MCP Lambda handler driven with API Gateway REST proxy events."""

    app: ModuleType
    kit: McpKit
    log_stream: io.StringIO
    remaining_millis: int = 28_000

    def post(self, body: str | bytes) -> tuple[int, dict[str, str], str]:
        text = body.decode("utf-8") if isinstance(body, bytes) else body
        event = {
            "resource": "/mcp",
            "path": "/mcp",
            "httpMethod": "POST",
            "headers": {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            "multiValueHeaders": None,
            "queryStringParameters": None,
            "requestContext": {"stage": "v1", "requestId": "cross-surface"},
            "body": text,
            "isBase64Encoded": False,
        }
        result = self.app.handler(event, EndpointContext(self.remaining_millis))
        return result["statusCode"], result["headers"], result["body"]

    def rpc(self, method: str, params: object = None, request_id: object = 1) -> dict[str, Any]:
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        status, _, body = self.post(json.dumps(message))
        assert status == 200, body
        document: dict[str, Any] = json.loads(body)
        assert document["id"] == request_id
        return document

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """The `tools/call` result (content, structuredContent, isError)."""
        document = self.rpc("tools/call", {"name": tool, "arguments": arguments}, request_id=7)
        assert "error" not in document, document
        result: dict[str, Any] = document["result"]
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
        return result

    def ok(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.call(tool, arguments)
        assert result["isError"] is False, result["structuredContent"]
        structured: dict[str, Any] = result["structuredContent"]
        return structured

    def failed(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.call(tool, arguments)
        assert result["isError"] is True, result["structuredContent"]
        error: dict[str, Any] = result["structuredContent"]["error"]
        return error

    def logs(self) -> str:
        return self.log_stream.getvalue()


def capture_service_logs(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    stream = io.StringIO()
    service_logger = logging.getLogger("handler-test")
    for log_handler in service_logger.handlers:
        if isinstance(log_handler, logging.StreamHandler):
            monkeypatch.setattr(log_handler, "stream", stream)
    return stream


def assert_no_case_content(text: str) -> None:
    for record in RECORDS.values():
        assert record["Case_Summary"] not in text
        assert record["subject"] not in text
        if record["submittedBy"] is not None:
            assert record["submittedBy"] not in text


def nonzero(rows: list[dict[str, Any]], key: str) -> list[tuple[str, int]]:
    return [(row[key], row["cases"]) for row in rows if row["cases"]]


EXPECTED_TOOLS: Final = [
    "query_athena",
    "analyze_with_bedrock",
    "get_case_summary",
    "analyze_case_summaries",
    "get_temporal_patterns",
    "get_rca_analysis",
    "get_lifecycle_analysis",
    "get_service_trends",
]

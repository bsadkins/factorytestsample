"""Fixtures for the cross-surface integration tests: synthetic processed records in mocked S3,
the CSV exporter and the MCP Lambda handler. Handler modules create their Powertools
Logger/Metrics/Tracer at import time, so Powertools settings are applied before collection."""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from botocore.model import OperationModel
from cross_surface import (
    BUCKET,
    CsvExport,
    Endpoint,
    athena_rows,
    capture_service_logs,
    load_csv_script,
    put_fixture_records,
)
from mcp_kit import make_kit
from mcp_tools.context import ToolContext
from moto import mock_aws
from sigv4_loopback import POWERTOOLS_ENV

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

for _name, _value in POWERTOOLS_ENV.items():
    os.environ.setdefault(_name, _value)


@pytest.fixture(autouse=True)
def powertools_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in POWERTOOLS_ENV.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def s3_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[S3Client]:
    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        put_fixture_records(client, BUCKET)
        yield client


@pytest.fixture
def rows(s3_client: S3Client) -> list[dict[str, str | None]]:
    return athena_rows(s3_client, BUCKET)


@pytest.fixture(scope="module")
def csv_script() -> ModuleType:
    return load_csv_script()


@pytest.fixture
def exporter(
    csv_script: ModuleType,
    s3_client: S3Client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> CsvExport:
    calls: list[tuple[str, dict[str, Any]]] = []
    original = csv_script.create_s3_client

    def factory(region: str | None, profile: str | None) -> S3Client:
        client: S3Client = original(region, profile)

        def record(params: dict[str, Any], model: OperationModel, **_: object) -> None:
            calls.append((model.name, dict(params)))

        client.meta.events.register("provide-client-params.s3.*", record)
        return client

    monkeypatch.setattr(csv_script, "create_s3_client", factory)
    return CsvExport(csv_script, tmp_path, calls)


@pytest.fixture
def endpoint(rows: list[dict[str, str | None]], monkeypatch: pytest.MonkeyPatch) -> Endpoint:
    app = importlib.import_module("mcp_server.app")
    stream = capture_service_logs(monkeypatch)
    kit = make_kit(rows)

    def tool_context(remaining_millis: int | None) -> ToolContext:
        return kit.context(remaining_millis)

    monkeypatch.setattr(app, "tool_context", tool_context)
    return Endpoint(app, kit, stream)

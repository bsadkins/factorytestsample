"""Tracing safety: the actual MCP Lambda handler with X-Ray enabled, real auto-instrumented
botocore clients answered by Stubber, and an in-memory emitter. Service errors that echo SQL,
questions or case text must not reach trace documents, while the downstream subsegments and
their fault signal remain."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

import mcp_trace_probe
import pytest
from aws_xray_sdk.core.models.entity import Entity  # type: ignore[import-untyped]
from aws_xray_sdk.core.models.segment import Segment  # type: ignore[import-untyped]
from aws_xray_sdk.core.models.subsegment import Subsegment  # type: ignore[import-untyped]
from common.tracing import withhold_exception_messages
from mcp_server import app  # noqa: F401 - importing the handler installs trace scrubbing

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPECTED_SUBSEGMENTS = {
    "athena": ("athena", "StartQueryExecution", "InvalidRequestException"),
    "bedrock": ("bedrock-runtime", "InvokeModel", "ValidationException"),
}


@cache
def probe_report() -> dict[str, Any]:
    environment = mcp_trace_probe.probe_environment(dict(os.environ))
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src" / "lambda"), str(REPO_ROOT / "test" / "support")]
    )
    completed = subprocess.run(
        [sys.executable, "-B", "-m", "mcp_trace_probe"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    lines = completed.stdout.strip().splitlines()
    report: dict[str, Any] = json.loads(lines[-1])
    return report


def walk(document: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield document
    for child in document.get("subsegments", []):
        yield from walk(child)


def test_probe_runs_the_handler_with_tracing_enabled_and_no_network() -> None:
    assert mcp_trace_probe.SQL_SENTINEL in mcp_trace_probe.PROBE_SQL
    report = probe_report()
    assert report["tracerDisabled"] is False
    assert report["networkAttempts"] == 0


@pytest.mark.parametrize("scenario", sorted(EXPECTED_SUBSEGMENTS))
def test_sdk_error_subsegments_keep_signal_without_sensitive_text(scenario: str) -> None:
    result = probe_report()["scenarios"][scenario]
    name, operation, error_type = EXPECTED_SUBSEGMENTS[scenario]
    documents = result["documents"]
    subsegments = [
        entity for document in documents for entity in walk(document) if entity["name"] == name
    ]
    assert len(subsegments) == 1, json.dumps(documents)[:2000]
    subsegment = subsegments[0]
    assert subsegment["namespace"] == "aws"
    assert subsegment["aws"]["operation"] == operation
    assert subsegment["http"]["response"]["status"] == 400
    assert subsegment.get("error") is True or subsegment.get("fault") is True
    exceptions = subsegment["cause"]["exceptions"]
    assert [exception["type"] for exception in exceptions] == [error_type]
    assert all("message" not in exception for exception in exceptions)

    handler_annotations = [
        entity.get("annotations", {})
        for document in documents
        for entity in walk(document)
        if entity["name"] == "## handler"
    ]
    assert handler_annotations == [
        {
            "ColdStart": handler_annotations[0].get("ColdStart"),
            "Service": "mcp-trace-probe",
            "mcp_method": "tools/call",
            "mcp_outcome": "tool_error",
            "mcp_tool": result_tool(scenario),
        }
    ]


def result_tool(scenario: str) -> str:
    return {"athena": "query_athena", "bedrock": "analyze_with_bedrock"}[scenario]


@pytest.mark.parametrize("scenario", sorted(EXPECTED_SUBSEGMENTS))
def test_no_sentinel_reaches_any_trace_document(scenario: str) -> None:
    result = probe_report()["scenarios"][scenario]
    serialised = json.dumps(result["documents"])
    assert result["documents"]
    for sentinel in mcp_trace_probe.SENTINELS:
        assert sentinel not in serialised


@pytest.mark.parametrize(
    ("scenario", "code"), [("athena", "ATHENA_ERROR"), ("bedrock", "BEDROCK_ERROR")]
)
def test_caller_still_gets_a_controlled_scrubbed_tool_error(scenario: str, code: str) -> None:
    response = probe_report()["scenarios"][scenario]["response"]
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["result"]["isError"] is True
    assert payload["result"]["structuredContent"]["error"]["code"] == code
    assert mcp_trace_probe.ATHENA_SENTINEL not in response["body"]
    assert mcp_trace_probe.BEDROCK_SENTINEL not in response["body"]


def test_installing_the_scrubber_again_keeps_a_single_wrapper() -> None:
    installed = Entity.add_exception
    withhold_exception_messages()
    assert Entity.add_exception is installed


def test_any_recorded_exception_keeps_type_and_drops_message() -> None:
    segment = Segment("synthetic-segment", sampled=True)
    subsegment = Subsegment("synthetic-subsegment", "local", segment)
    subsegment.add_exception(ValueError("SENTINEL-LOCAL-c0de"), [("app.py", 7, "handler", "x")])
    subsegment.add_exception(KeyError("SENTINEL-LOCAL-d00d"), None, remote=True)
    document = json.loads(subsegment.serialize())
    exceptions = document["cause"]["exceptions"]
    assert [exception["type"] for exception in exceptions] == ["ValueError", "KeyError"]
    assert all("message" not in exception for exception in exceptions)
    assert exceptions[0]["stack"] == [{"path": "app.py", "line": 7, "label": "handler"}]
    assert document["fault"] is True
    assert "SENTINEL-LOCAL" not in subsegment.serialize()

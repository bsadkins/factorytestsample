"""Tracing safety for every Lambda handler that creates a Powertools Tracer.

Each handler runs in a fresh interpreter with X-Ray enabled and the deployed Powertools settings.
Every AWS SDK call it makes is answered with a service error whose message carries a sentinel,
standing in for AWS errors that echo request content. The automatic SDK subsegments must keep the
operation, exception type, stack and fault flags, but no exception message or sentinel text may
reach any trace document.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path
from typing import Any

import handler_trace_probe
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_ROOT = REPO_ROOT / "src" / "lambda"
HANDLERS = sorted(handler_trace_probe.HANDLERS)
# Fresh interpreters are cheap to start but heavy to import; a few at a time keeps memory bounded.
PARALLEL_PROBES = 4


def _run_probe(name: str) -> dict[str, Any]:
    environment = handler_trace_probe.probe_environment(dict(os.environ))
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(LAMBDA_ROOT), str(REPO_ROOT / "test" / "support")]
    )
    # `name` comes from the probe's fixed handler table.
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-B", "-m", "handler_trace_probe", name],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    *handler_output, last_line = completed.stdout.strip().splitlines()
    report: dict[str, Any] = json.loads(last_line)
    report["stdout"] = "\n".join(handler_output)
    report["stderr"] = completed.stderr
    return report


@cache
def probe_reports() -> dict[str, dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=PARALLEL_PROBES) as pool:
        return dict(zip(HANDLERS, pool.map(_run_probe, HANDLERS), strict=True))


def walk(document: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield document
    for child in document.get("subsegments", []):
        yield from walk(child)


def entities(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [entity for document in report["documents"] for entity in walk(document)]


def recorded_exceptions(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        exception
        for entity in entities(report)
        if isinstance(entity.get("cause"), dict)
        for exception in entity["cause"].get("exceptions", [])
    ]


def _constructs_tracer(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Tracer"
        for node in ast.walk(tree)
    )


def test_probe_covers_every_module_that_creates_a_tracer() -> None:
    creators = {
        path.relative_to(LAMBDA_ROOT).parts[0]
        for path in LAMBDA_ROOT.rglob("*.py")
        if _constructs_tracer(path)
    }
    assert creators == set(HANDLERS)
    assert all((LAMBDA_ROOT / name / "app.py").is_file() for name in HANDLERS)


def test_probe_uses_the_deployed_powertools_settings() -> None:
    construct = (REPO_ROOT / "lib" / "constructs" / "pipeline-function.ts").read_text("utf-8")
    for name, value in handler_trace_probe.DEPLOYED_POWERTOOLS_SETTINGS.items():
        assert f"{name}: '{value}'," in construct


@pytest.mark.parametrize("name", HANDLERS)
def test_handler_import_installs_the_scrubber_with_tracing_enabled(name: str) -> None:
    report = probe_reports()[name]
    assert report["tracerDisabled"] is False
    assert report["scrubberInstalledBeforeImport"] is False
    assert report["scrubberInstalledAfterImport"] is True
    assert report["networkAttempts"] == 0


@pytest.mark.parametrize("name", HANDLERS)
def test_sdk_error_subsegments_keep_signal_without_messages(name: str) -> None:
    report = probe_reports()[name]
    assert report["sdkOperations"], f"{name} made no AWS SDK call"
    sdk_subsegments = [entity for entity in entities(report) if entity.get("namespace") == "aws"]
    assert sorted(entity["aws"]["operation"] for entity in sdk_subsegments) == sorted(
        report["sdkOperations"]
    )
    for subsegment in sdk_subsegments:
        assert subsegment["http"]["response"]["status"] == 400
        assert subsegment.get("error") is True
        assert subsegment.get("fault") is True
        exceptions = subsegment["cause"]["exceptions"]
        assert [exception["type"] for exception in exceptions] == ["ClientError"]
        assert all(exception["remote"] is True for exception in exceptions)
        assert all(exception["stack"] for exception in exceptions)
        assert all("message" not in exception for exception in exceptions)


@pytest.mark.parametrize("name", HANDLERS)
def test_no_exception_message_or_sentinel_reaches_any_trace_document(name: str) -> None:
    report = probe_reports()[name]
    exceptions = recorded_exceptions(report)
    assert exceptions
    assert all(exception["type"] for exception in exceptions)
    assert all("message" not in exception for exception in exceptions)
    serialised = json.dumps(report["documents"])
    assert handler_trace_probe.SENTINEL not in serialised
    assert handler_trace_probe.ERROR_CODE not in serialised


@pytest.mark.parametrize("name", HANDLERS)
def test_handler_logs_and_metrics_do_not_carry_the_service_error_text(name: str) -> None:
    report = probe_reports()[name]
    assert handler_trace_probe.SENTINEL not in report["stdout"]
    assert handler_trace_probe.SENTINEL not in report["stderr"]

"""Fixtures for Lambda handler tests: mocked AWS, Lambda contexts and EMF metric capture."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from moto import mock_aws

# Handler modules create Powertools Logger/Metrics/Tracer at import time, so these must be set
# before test modules are collected.
POWERTOOLS_TEST_ENV = {
    "POWERTOOLS_METRICS_NAMESPACE": "SupportCaseInsights",
    "POWERTOOLS_SERVICE_NAME": "handler-test",
    "POWERTOOLS_TRACE_DISABLED": "true",
    "POWERTOOLS_LOGGER_LOG_EVENT": "false",
}
for _name, _value in POWERTOOLS_TEST_ENV.items():
    os.environ.setdefault(_name, _value)

FUNCTION_NAME = "test-function"
FUNCTION_ARN = f"arn:aws:lambda:us-east-1:123456789012:function:{FUNCTION_NAME}"


class FakeLambdaContext(LambdaContext):
    """Lambda runtime context with a fixed remaining time, as Powertools idempotency requires."""

    def __init__(self, request_id: str = "request-1", remaining_millis: int = 120_000) -> None:
        self._aws_request_id = request_id
        self._function_name = FUNCTION_NAME
        self._function_version = "$LATEST"
        self._invoked_function_arn = FUNCTION_ARN
        self._memory_limit_in_mb = 512
        self._log_group_name = f"/aws/lambda/{FUNCTION_NAME}"
        self._log_stream_name = "stream"
        self._remaining_millis = remaining_millis

    def get_remaining_time_in_millis(self) -> int:  # type: ignore[override]
        return self._remaining_millis


def emitted_metrics(output: str) -> dict[str, float]:
    """Sum the values of every EMF metric printed to stdout by Powertools Metrics."""
    totals: dict[str, float] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith("{") or '"_aws"' not in line:
            continue
        document: dict[str, Any] = json.loads(line)
        for directive in document["_aws"]["CloudWatchMetrics"]:
            for metric in directive["Metrics"]:
                name = metric["Name"]
                value = document[name]
                values = value if isinstance(value, list) else [value]
                totals[name] = totals.get(name, 0.0) + float(sum(values))
    return totals


@pytest.fixture(autouse=True)
def powertools_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in POWERTOOLS_TEST_ENV.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def mocked_aws(aws_env: None) -> Iterator[None]:
    with mock_aws():
        yield


@pytest.fixture
def make_context() -> Callable[[str], LambdaContext]:
    return FakeLambdaContext


@pytest.fixture
def lambda_context() -> LambdaContext:
    return FakeLambdaContext()


@pytest.fixture
def metrics_in() -> Callable[[str], dict[str, float]]:
    return emitted_metrics

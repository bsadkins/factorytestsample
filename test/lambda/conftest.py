"""Fixtures for Lambda handler tests: mocked AWS, Lambda contexts, EMF metric capture and
stubbed member-account STS/Support access."""

from __future__ import annotations

import io
import json
import logging
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError
from botocore.stub import Stubber
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


class FakeSts:
    """STS stand-in: per-account credentials; listed accounts are denied like a missing role."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.denied: set[str] = set()

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        self.calls.append(kwargs)
        account = str(kwargs["RoleArn"]).split(":")[4]
        if account in self.denied:
            raise ClientError(
                {
                    "Error": {"Code": "AccessDenied", "Message": "not authorized"},
                    "ResponseMetadata": {"HTTPStatusCode": 403},  # type: ignore[typeddict-item]
                },
                "AssumeRole",
            )
        return {
            "Credentials": {
                "AccessKeyId": f"ASIA{account}",
                "SecretAccessKey": "fake-secret",
                "SessionToken": "fake-token",
                "Expiration": datetime(2030, 1, 1, tzinfo=UTC),
            }
        }


class SupportStubs:
    """Per-account stubbed Support clients. Stubber rejects any call that was not queued."""

    def __init__(self) -> None:
        self.stubbers: dict[str, Stubber] = {}
        self.clients: dict[str, Any] = {}
        self.partitions: list[str] = []

    def stub(self, account_id: str) -> Stubber:
        if account_id not in self.stubbers:
            client = boto3.client(
                "support",
                region_name="us-east-1",
                aws_access_key_id="stub",
                aws_secret_access_key="stub",
            )
            stubber = Stubber(client)
            stubber.activate()
            self.clients[account_id] = client
            self.stubbers[account_id] = stubber
        return self.stubbers[account_id]

    def factory(self, session: boto3.Session, partition: str) -> Any:  # noqa: ANN401
        self.partitions.append(partition)
        credentials = session.get_credentials()
        assert credentials is not None
        account = credentials.access_key.removeprefix("ASIA")
        self.stub(account)
        return self.clients[account]

    def assert_all_consumed(self) -> None:
        for stubber in self.stubbers.values():
            stubber.assert_no_pending_responses()


def support_error(code: str, operation: str = "DescribeCases", status: int = 400) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "synthetic"},
            "ResponseMetadata": {"HTTPStatusCode": status},  # type: ignore[typeddict-item]
        },
        operation,
    )


def sqs_event(*bodies: object) -> dict[str, Any]:
    """An SQS Lambda event whose record message IDs are `m0`, `m1`, ..."""
    records = []
    for index, body in enumerate(bodies):
        records.append(
            {
                "messageId": f"m{index}",
                "receiptHandle": f"r{index}",
                "body": body if isinstance(body, str) else json.dumps(body),
                "attributes": {"ApproximateReceiveCount": "1"},
                "messageAttributes": {},
                "md5OfBody": "",
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:us-east-1:123456789012:queue",
                "awsRegion": "us-east-1",
            }
        )
    return {"Records": records}


def drain(sqs: Any, queue_url: str) -> list[dict[str, Any]]:  # noqa: ANN401
    """Receive and delete every message on a queue, returning the parsed bodies."""
    received: list[dict[str, Any]] = []
    while True:
        batch = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10).get("Messages", [])
        if not batch:
            return received
        for message in batch:
            received.append(json.loads(message["Body"]))
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])


def create_idempotency_table(name: str) -> None:
    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@dataclass
class CaseCollectionKit:
    """Stand-ins and helpers for the member-account handlers (case retrieval and annotation)."""

    sts: FakeSts
    support: SupportStubs
    log_stream: io.StringIO
    event: Callable[..., dict[str, Any]] = sqs_event
    drain: Callable[[Any, str], list[dict[str, Any]]] = drain
    support_error: Callable[..., ClientError] = support_error
    create_table: Callable[[str], None] = create_idempotency_table
    metrics: Callable[[str], dict[str, float]] = emitted_metrics

    def context(self, request_id: str = "request-1") -> LambdaContext:
        return FakeLambdaContext(request_id)

    def logs(self) -> str:
        """Everything Powertools Logger wrote; Logger binds its stream at import time, so
        `capsys` only sees EMF metrics."""
        return self.log_stream.getvalue()


@pytest.fixture
def case_collection_kit(monkeypatch: pytest.MonkeyPatch) -> Iterator[CaseCollectionKit]:
    stream = io.StringIO()
    service_logger = logging.getLogger(POWERTOOLS_TEST_ENV["POWERTOOLS_SERVICE_NAME"])
    for log_handler in service_logger.handlers:
        if isinstance(log_handler, logging.StreamHandler):
            monkeypatch.setattr(log_handler, "stream", stream)
    kit = CaseCollectionKit(sts=FakeSts(), support=SupportStubs(), log_stream=stream)
    yield kit
    kit.support.assert_all_consumed()

"""Account Reader handler: S3 account-list events to active-account queue references."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote_plus

import boto3
import pytest
from account_reader import app
from aws_lambda_powertools.utilities.typing import LambdaContext
from common.account_list import (
    ACCOUNT_LIST_PREFIX,
    AccountRef,
    DiscoveryCounts,
    account_list_key,
    build_account_list_document,
)
from common.idempotency import persistence_layer
from common.sqs import SqsSendError, send_messages

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client
    from types_boto3_sqs.client import SQSClient

BUCKET = "account-lists-test"
TABLE = "idempotency-test"
MANAGEMENT = "999999999999"
RUN = "20261113T000000Z"
ACCOUNTS = [
    AccountRef("111111111111", "SENTINEL-NAME-A"),
    AccountRef("222222222222", "Prod workloads"),
    AccountRef("333333333333", "Sandbox"),
]

MetricsIn = Callable[[str], dict[str, float]]


@dataclass
class Env:
    s3: S3Client
    sqs: SQSClient
    queue_url: str

    def put_list(self, run_id: str = RUN, accounts: list[AccountRef] = ACCOUNTS) -> str:
        document = build_account_list_document(
            run_id,
            datetime(2026, 11, 13, tzinfo=UTC),
            accounts,
            DiscoveryCounts(len(accounts) + 1, len(accounts), 0, 1),
        )
        return self.put_raw(account_list_key(run_id), json.dumps(document).encode())

    def put_raw(self, key: str, body: bytes) -> str:
        return self.s3.put_object(Bucket=BUCKET, Key=key, Body=body)["VersionId"]

    def messages(self) -> list[dict[str, Any]]:
        received: list[dict[str, Any]] = []
        while True:
            batch = self.sqs.receive_message(QueueUrl=self.queue_url, MaxNumberOfMessages=10).get(
                "Messages", []
            )
            if not batch:
                return received
            received.extend(json.loads(message["Body"]) for message in batch)
            for message in batch:
                self.sqs.delete_message(
                    QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"]
                )


def s3_record(
    key: str,
    version_id: str | None,
    *,
    bucket: str = BUCKET,
    event_name: str = "ObjectCreated:Put",
) -> dict[str, Any]:
    obj: dict[str, Any] = {"key": quote_plus(key), "size": 100, "sequencer": "0A1B2C"}
    if version_id is not None:
        obj["versionId"] = version_id
    return {
        "eventVersion": "2.1",
        "eventSource": "aws:s3",
        "awsRegion": "us-east-1",
        "eventTime": "2026-11-13T00:00:07.000Z",
        "eventName": event_name,
        "s3": {"s3SchemaVersion": "1.0", "bucket": {"name": bucket}, "object": obj},
    }


def s3_event(*records: object) -> dict[str, Any]:
    return {"Records": list(records)}


def expected_messages(run_id: str = RUN) -> list[dict[str, str]]:
    return [
        {"accountId": ref.account_id, "accountName": ref.account_name, "runId": run_id}
        for ref in ACCOUNTS
    ]


@pytest.fixture
def env(mocked_aws: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=BUCKET)
    s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
    sqs = boto3.client("sqs", region_name="us-east-1")
    queue_url = sqs.create_queue(QueueName="active-accounts")["QueueUrl"]
    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    monkeypatch.setenv("ACCOUNT_LIST_BUCKET", BUCKET)
    monkeypatch.setenv("ACTIVE_ACCOUNTS_QUEUE_URL", queue_url)
    monkeypatch.setenv("IDEMPOTENCY_TABLE_NAME", TABLE)
    monkeypatch.setenv("MANAGEMENT_ACCOUNT_ID", MANAGEMENT)
    cached_clients = app.clients
    cached_clients.cache_clear()
    persistence_layer.cache_clear()
    yield Env(s3=s3, sqs=sqs, queue_url=queue_url)
    cached_clients.cache_clear()
    persistence_layer.cache_clear()


def test_valid_list_enqueues_one_reference_per_account(
    env: Env,
    lambda_context: LambdaContext,
    capsys: pytest.CaptureFixture[str],
    metrics_in: MetricsIn,
) -> None:
    version = env.put_list()

    result = app.handler(s3_event(s3_record(account_list_key(RUN), version)), lambda_context)

    assert sorted(env.messages(), key=lambda m: m["accountId"]) == expected_messages()
    assert result == {
        "accountListsProcessed": 1,
        "accountsEnqueued": 3,
        "duplicateEvents": 0,
        "ignoredEvents": 0,
    }
    output = capsys.readouterr().out
    assert metrics_in(output) == {"AccountListsProcessed": 1, "AccountsEnqueued": 3}
    assert "SENTINEL-NAME" not in output


def test_duplicate_event_delivery_does_not_enqueue_again(
    env: Env,
    lambda_context: LambdaContext,
    make_context: Callable[[str], LambdaContext],
    capsys: pytest.CaptureFixture[str],
    metrics_in: MetricsIn,
) -> None:
    event = s3_event(s3_record(account_list_key(RUN), env.put_list()))
    app.handler(event, lambda_context)
    capsys.readouterr()

    duplicate = app.handler(event, make_context("request-2"))

    assert len(env.messages()) == 3
    assert duplicate["duplicateEvents"] == 1
    assert duplicate["accountsEnqueued"] == 0
    assert metrics_in(capsys.readouterr().out) == {"DuplicateAccountListEvents": 1}


def test_concurrent_duplicate_while_first_is_in_progress_is_skipped(
    env: Env,
    lambda_context: LambdaContext,
    make_context: Callable[[str], LambdaContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = s3_event(s3_record(account_list_key(RUN), env.put_list()))
    nested: list[dict[str, Any]] = []

    def send_with_concurrent_duplicate(
        sqs: SQSClient, queue_url: str, bodies: Sequence[Mapping[str, Any]]
    ) -> int:
        if not nested:
            nested.append(app.handler(event, make_context("request-2")))
        return send_messages(sqs, queue_url, bodies)

    monkeypatch.setattr(app, "send_messages", send_with_concurrent_duplicate)
    first = app.handler(event, lambda_context)

    assert nested == [
        {
            "accountListsProcessed": 0,
            "accountsEnqueued": 0,
            "duplicateEvents": 1,
            "ignoredEvents": 0,
        }
    ]
    assert first["accountsEnqueued"] == 3
    assert len(env.messages()) == 3


def test_failed_attempt_is_retried_rather_than_cached(
    env: Env, lambda_context: LambdaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = s3_event(s3_record(account_list_key(RUN), env.put_list()))

    def failing_send(_sqs: SQSClient, _queue_url: str, _bodies: Sequence[Mapping[str, Any]]) -> int:
        raise SqsSendError(3, ("InternalError",))

    with monkeypatch.context() as patch:
        patch.setattr(app, "send_messages", failing_send)
        with pytest.raises(app.AccountListProcessingError):
            app.handler(event, lambda_context)
    assert env.messages() == []

    retried = app.handler(event, lambda_context)

    assert retried["accountsEnqueued"] == 3
    assert sorted(env.messages(), key=lambda m: m["accountId"]) == expected_messages()


def test_empty_run_list_is_processed_without_messages(
    env: Env,
    lambda_context: LambdaContext,
    capsys: pytest.CaptureFixture[str],
    metrics_in: MetricsIn,
) -> None:
    version = env.put_list(accounts=[])

    result = app.handler(s3_event(s3_record(account_list_key(RUN), version)), lambda_context)

    assert result["accountListsProcessed"] == 1
    assert result["accountsEnqueued"] == 0
    assert env.messages() == []
    assert metrics_in(capsys.readouterr().out) == {
        "AccountListsProcessed": 1,
        "AccountsEnqueued": 0,
    }


@pytest.mark.parametrize(
    "body",
    [
        b"{not json",
        b"[]",
        json.dumps({"schemaVersion": 1, "runId": RUN}).encode(),
        json.dumps(
            build_account_list_document(
                "20261112T000000Z",
                datetime(2026, 11, 12, tzinfo=UTC),
                ACCOUNTS,
                DiscoveryCounts(3, 3, 0, 0),
            )
        ).encode(),
    ],
    ids=["invalid-json", "not-object", "missing-fields", "run-id-mismatch"],
)
def test_malformed_list_fails_observably_and_enqueues_nothing(
    env: Env,
    lambda_context: LambdaContext,
    capsys: pytest.CaptureFixture[str],
    metrics_in: MetricsIn,
    body: bytes,
) -> None:
    key = account_list_key(RUN)
    event = s3_event(s3_record(key, env.put_raw(key, body)))

    with pytest.raises(app.AccountListProcessingError):
        app.handler(event, lambda_context)
    # A failed list is not cached as processed: a retry fails the same way.
    with pytest.raises(app.AccountListProcessingError):
        app.handler(event, lambda_context)

    assert env.messages() == []
    metrics = metrics_in(capsys.readouterr().out)
    assert metrics["InvalidAccountLists"] == 2
    assert "AccountListsProcessed" not in metrics


def test_list_containing_the_management_account_is_rejected(
    env: Env, lambda_context: LambdaContext
) -> None:
    version = env.put_list(accounts=[*ACCOUNTS, AccountRef(MANAGEMENT, "Management")])
    with pytest.raises(app.AccountListProcessingError):
        app.handler(s3_event(s3_record(account_list_key(RUN), version)), lambda_context)
    assert env.messages() == []


def test_oversized_list_is_rejected(
    env: Env, lambda_context: LambdaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app, "MAX_ACCOUNT_LIST_BYTES", 64)
    version = env.put_list()
    with pytest.raises(app.AccountListProcessingError):
        app.handler(s3_event(s3_record(account_list_key(RUN), version)), lambda_context)
    assert env.messages() == []


def test_unrelated_object_events_are_ignored(
    env: Env,
    lambda_context: LambdaContext,
    capsys: pytest.CaptureFixture[str],
    metrics_in: MetricsIn,
) -> None:
    other_version = env.put_raw(f"notes/{RUN}.json", b"{}")
    env.put_list()
    event = s3_event(
        s3_record(f"notes/{RUN}.json", other_version),
        s3_record(f"{ACCOUNT_LIST_PREFIX}{RUN}.json.tmp", None),
        s3_record(account_list_key(RUN), None, bucket="some-other-bucket"),
        s3_record(account_list_key(RUN), None, event_name="ObjectRemoved:Delete"),
        {"eventSource": "aws:sqs", "body": "{}"},
        {"eventSource": "aws:s3", "eventName": "ObjectCreated:Put", "s3": {"bucket": BUCKET}},
        "not-a-record",
    )

    result = app.handler(event, lambda_context)

    assert result == {
        "accountListsProcessed": 0,
        "accountsEnqueued": 0,
        "duplicateEvents": 0,
        "ignoredEvents": 7,
    }
    assert env.messages() == []
    assert metrics_in(capsys.readouterr().out) == {"UnrelatedObjectEventsIgnored": 7}


def test_s3_test_event_is_ignored(env: Env, lambda_context: LambdaContext) -> None:
    test_event = {"Service": "Amazon S3", "Event": "s3:TestEvent", "Bucket": BUCKET}
    assert app.handler(test_event, lambda_context)["ignoredEvents"] == 1
    assert env.messages() == []


def test_valid_records_are_processed_when_another_record_fails(
    env: Env, lambda_context: LambdaContext
) -> None:
    good_run, bad_run = RUN, "20261114T000000Z"
    good_version = env.put_list(good_run)
    bad_key = account_list_key(bad_run)
    bad_version = env.put_raw(bad_key, b"{")
    event = s3_event(
        s3_record(bad_key, bad_version), s3_record(account_list_key(good_run), good_version)
    )

    with pytest.raises(app.AccountListProcessingError, match="1 account list"):
        app.handler(event, lambda_context)
    assert len(env.messages()) == 3

    # The retried event does not re-send the list that already succeeded.
    with pytest.raises(app.AccountListProcessingError):
        app.handler(event, lambda_context)
    assert env.messages() == []


def test_unversioned_object_events_are_supported(env: Env, lambda_context: LambdaContext) -> None:
    env.put_list()
    event = s3_event(s3_record(account_list_key(RUN), None))
    assert app.handler(event, lambda_context)["accountsEnqueued"] == 3
    assert app.handler(event, lambda_context)["duplicateEvents"] == 1
    assert len(env.messages()) == 3

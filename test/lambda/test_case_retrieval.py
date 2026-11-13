"""Case Retrieval handler: member-role DescribeCases paging, filtering and annotation hand-off."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import boto3
import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from case_retrieval import app
from common.idempotency import persistence_layer
from common.sqs import SqsSendError, send_messages

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client
    from types_boto3_sqs.client import SQSClient

RAW = "raw-test"
PROCESSED = "processed-test"
TABLE = "idempotency-test"
ROLE_NAME = "Support-Case-Analysis-Role-test"
RUN = "20261113T000000Z"
NEXT_RUN = "20261114T000000Z"
AFTER = "2025-11-13T00:00:00Z"
BEFORE = "2026-11-13T00:00:00Z"
A, B, C, D = "111111111111", "222222222222", "333333333333", "444444444444"
SENTINEL_SUBJECT = "SENTINEL-SUBJECT-91c2"
SENTINEL_PERSON = "sentinel-person@example.com"
NOW = datetime(2026, 11, 13, 0, 5, 12, tzinfo=UTC)


class Kit(Protocol):
    sts: Any
    support: Any
    event: Callable[..., dict[str, Any]]
    drain: Callable[[Any, str], list[dict[str, Any]]]
    create_table: Callable[[str], None]
    metrics: Callable[[str], dict[str, float]]

    def context(self, request_id: str = ...) -> LambdaContext: ...

    def logs(self) -> str: ...


def case(
    account: str, display: str, status: str = "resolved", created: str = "2026-06-01T10:00:00.000Z"
) -> dict[str, Any]:
    return {
        "caseId": f"case-{account}-2026-{display}",
        "displayId": display,
        "subject": f"{SENTINEL_SUBJECT} {display}",
        "status": status,
        "serviceCode": "amazon-simple-storage-service",
        "categoryCode": "general-guidance",
        "severityCode": "low",
        "submittedBy": SENTINEL_PERSON,
        "timeCreated": created,
        "language": "en",
    }


def params(after: str = AFTER, before: str = BEFORE, token: str | None = None) -> dict[str, Any]:
    expected: dict[str, Any] = {
        "includeResolvedCases": True,
        "includeCommunications": False,
        "afterTime": after,
        "beforeTime": before,
        "maxResults": 100,
    }
    if token is not None:
        expected["nextToken"] = token
    return expected


def message(account: str, run_id: str = RUN) -> dict[str, str]:
    return {"accountId": account, "accountName": f"SENTINEL-ACCOUNT-{account}", "runId": run_id}


def raw_key(account: str, display: str) -> str:
    return f"account_number={account}/case_number={display}/data.json"


@dataclass
class Env:
    s3: S3Client
    sqs: SQSClient
    queue_url: str
    kit: Kit
    capsys: pytest.CaptureFixture[str]
    stdout: str = field(default="")

    @property
    def sts(self) -> Any:  # noqa: ANN401
        return self.kit.sts

    @property
    def support(self) -> Any:  # noqa: ANN401
        return self.kit.support

    def handle(self, *bodies: object, request_id: str = "request-1") -> Any:  # noqa: ANN401
        return app.handler(self.kit.event(*bodies), self.kit.context(request_id))

    def metrics(self) -> dict[str, float]:
        self.stdout = self.capsys.readouterr().out
        return self.kit.metrics(self.stdout)

    def annotation_messages(self) -> list[dict[str, Any]]:
        return self.kit.drain(self.sqs, self.queue_url)

    def raw_keys(self, prefix: str = "account_number=") -> list[str]:
        listing = self.s3.list_objects_v2(Bucket=RAW, Prefix=prefix)
        return sorted(o["Key"] for o in listing.get("Contents", []))

    def account_marker(self, account: str, run_id: str = RUN) -> dict[str, Any]:
        return self.raw(f"runs/{run_id}/accounts/{account}.json")

    def raw(self, key: str) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(
            self.s3.get_object(Bucket=RAW, Key=key)["Body"].read()
        )
        return document

    def mark_processed(self, account: str, display: str) -> None:
        self.s3.put_object(Bucket=PROCESSED, Key=raw_key(account, display), Body=b"{}")


@pytest.fixture
def env(
    mocked_aws: None,
    monkeypatch: pytest.MonkeyPatch,
    case_collection_kit: Kit,
    capsys: pytest.CaptureFixture[str],
) -> Iterator[Env]:
    kit = case_collection_kit
    s3 = boto3.client("s3", region_name="us-east-1")
    for name in (RAW, PROCESSED):
        s3.create_bucket(Bucket=name)
        s3.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})
    sqs = boto3.client("sqs", region_name="us-east-1")
    queue_url = sqs.create_queue(QueueName="annotation")["QueueUrl"]
    kit.create_table(TABLE)
    for key, value in {
        "RAW_BUCKET": RAW,
        "PROCESSED_BUCKET": PROCESSED,
        "ANNOTATION_QUEUE_URL": queue_url,
        "IDEMPOTENCY_TABLE_NAME": TABLE,
        "MEMBER_ROLE_NAME": ROLE_NAME,
        "MEMBER_ROLE_PARTITION": "aws",
        "LOOKBACK_MONTHS": "12",
    }.items():
        monkeypatch.setenv(key, value)
    clients = app.Clients(s3=s3, sqs=sqs, sts=kit.sts)
    monkeypatch.setattr(app, "clients", lambda: clients)
    monkeypatch.setattr(app, "support_client", kit.support.factory)
    monkeypatch.setattr(app, "now", lambda: NOW)
    persistence_layer.cache_clear()
    yield Env(s3, sqs, queue_url, kit, capsys)
    persistence_layer.cache_clear()


def test_pages_all_cases_and_queues_only_new_resolved_cases_in_the_window(env: Env) -> None:
    page1 = [
        case(A, "1001"),
        case(A, "1002", status="work-in-progress"),
        case(A, "1003", created=AFTER),  # inclusive lower bound
        case(A, "1004", created="2025-11-12T23:59:59.999Z"),  # just before the window
        case(A, "1005", status="pending-customer-action"),
    ]
    page2 = [
        case(A, "1006", created="2026-11-12T23:59:59.000Z"),  # last second inside
        case(A, "1007", created=BEFORE),  # exclusive upper bound
        case(A, "1008"),  # already processed
        case(A, "1009", status="reopened"),
    ]
    page3 = [
        case(A, "1010", status="opened"),
        case(A, "1011", created="2026-01-01T08:00:00+00:00"),
    ]
    stub = env.support.stub(A)
    stub.add_response("describe_cases", {"cases": page1, "nextToken": "t1"}, params())
    stub.add_response("describe_cases", {"cases": page2, "nextToken": "t2"}, params(token="t1"))
    stub.add_response("describe_cases", {"cases": page3}, params(token="t2"))
    env.mark_processed(A, "1008")

    response = env.handle(message(A))

    assert response == {"batchItemFailures": []}
    assert env.sts.calls == [
        {
            "RoleArn": f"arn:aws:iam::{A}:role/{ROLE_NAME}",
            "RoleSessionName": f"case-retrieval-{RUN}",
            "DurationSeconds": 900,
        }
    ]
    assert env.support.partitions == ["aws"]
    new = ["1001", "1003", "1006", "1011"]
    assert env.raw_keys() == sorted(raw_key(A, d) for d in new)
    assert sorted(env.annotation_messages(), key=lambda m: m["displayId"]) == [
        {
            "accountId": A,
            "caseId": f"case-{A}-2026-{d}",
            "displayId": d,
            "runId": RUN,
            "rawKey": raw_key(A, d),
        }
        for d in new
    ]
    assert env.account_marker(A) == {
        "schemaVersion": 1,
        "runId": RUN,
        "accountId": A,
        "outcome": "retrieved",
        "cases": new,
        "skipReason": None,
    }
    assert env.raw(raw_key(A, "1001")) == {
        "schemaVersion": 1,
        "accountId": A,
        "runId": RUN,
        **case(A, "1001"),
        "Case_Retrieval_Date": "2026-11-13T00:05:12Z",
    }
    assert env.metrics() == {
        "AccountsProcessed": 1,
        "CasesListed": 11,
        "CasesExcludedUnresolved": 4,
        "CasesExcludedOutsideWindow": 2,
        "CasesInvalid": 0,
        "CasesAlreadyProcessed": 1,
        "CasesRetrieved": 4,
    }
    logs = env.kit.logs()
    assert '"message":"Account cases retrieved"' in logs
    for sentinel in (SENTINEL_SUBJECT, SENTINEL_PERSON, "SENTINEL-ACCOUNT"):
        assert sentinel not in env.stdout + logs


@pytest.mark.parametrize(
    "window",
    [
        ("20240229T000000Z", 12, "2023-02-28T00:00:00Z", "2024-02-29T00:00:00Z"),
        ("20250331T000000Z", 1, "2025-02-28T00:00:00Z", "2025-03-31T00:00:00Z"),
        ("20240331T000000Z", 1, "2024-02-29T00:00:00Z", "2024-03-31T00:00:00Z"),
        ("20260115T000000Z", 2, "2025-11-15T00:00:00Z", "2026-01-15T00:00:00Z"),
        ("20261113T093000Z-0a1b2c3d", 12, "2025-11-13T09:30:00Z", "2026-11-13T09:30:00Z"),
    ],
    ids=["leap-day-12m", "month-end-to-feb", "month-end-to-leap-feb", "year-crossing", "manual"],
)
def test_requests_the_configured_trailing_calendar_month_window(
    env: Env, monkeypatch: pytest.MonkeyPatch, window: tuple[str, int, str, str]
) -> None:
    run_id, lookback, after, before = window
    monkeypatch.setenv("LOOKBACK_MONTHS", str(lookback))
    first_day = case(A, "2001", created=after)
    env.support.stub(A).add_response(
        "describe_cases", {"cases": [first_day]}, params(after=after, before=before)
    )

    assert env.handle(message(A, run_id)) == {"batchItemFailures": []}

    assert [m["displayId"] for m in env.annotation_messages()] == ["2001"]


def test_inaccessible_and_unentitled_accounts_are_counted_skips_and_reevaluated_next_run(
    env: Env,
) -> None:
    env.sts.denied.add(B)  # member role missing or not trusting this function
    env.support.stub(A).add_response("describe_cases", {"cases": [case(A, "3001")]}, params())
    env.support.stub(C).add_client_error(
        "describe_cases", service_error_code="SubscriptionRequiredException", http_status_code=400
    )
    env.support.stub(D).add_client_error(
        "describe_cases", service_error_code="AccessDeniedException", http_status_code=403
    )

    response = env.handle(message(B), message(A), message(C), message(D))

    assert response == {"batchItemFailures": []}
    assert [m["accountId"] for m in env.annotation_messages()] == [A]
    assert env.metrics() == {
        "AccountsSkipped": 3,
        "AccountsSkippedAccessDenied": 2,
        "AccountsSkippedSubscriptionRequired": 1,
        "AccountsProcessed": 1,
        "CasesListed": 1,
        "CasesExcludedUnresolved": 0,
        "CasesExcludedOutsideWindow": 0,
        "CasesInvalid": 0,
        "CasesAlreadyProcessed": 0,
        "CasesRetrieved": 1,
    }

    # Skipped accounts are settled for the run, so run completion does not wait for them.
    for account, reason in ((B, "AccessDenied"), (C, "SubscriptionRequired"), (D, "AccessDenied")):
        marker = env.account_marker(account)
        assert (marker["outcome"], marker["skipReason"], marker["cases"]) == (
            "skipped",
            reason,
            [],
        )
    assert env.account_marker(A)["cases"] == ["3001"]

    # A redelivery within the same run replays the recorded skip without calling AWS again.
    calls = len(env.sts.calls)
    assert env.handle(message(B), request_id="request-2") == {"batchItemFailures": []}
    assert len(env.sts.calls) == calls
    assert env.metrics() == {"DuplicateAccountMessages": 1}

    # The next daily run evaluates the accounts again; access and entitlement have been fixed.
    env.sts.denied.clear()
    next_window = params(after="2025-11-14T00:00:00Z", before="2026-11-14T00:00:00Z")
    for account, display in ((B, "4001"), (C, "4002"), (D, "4003")):
        env.support.stub(account).add_response(
            "describe_cases", {"cases": [case(account, display)]}, next_window
        )
    response = env.handle(
        message(B, NEXT_RUN), message(C, NEXT_RUN), message(D, NEXT_RUN), request_id="request-3"
    )

    assert response == {"batchItemFailures": []}
    assert sorted(m["displayId"] for m in env.annotation_messages()) == ["4001", "4002", "4003"]
    metrics = env.metrics()
    assert metrics["AccountsProcessed"] == 3
    assert "AccountsSkipped" not in metrics


def test_duplicate_account_message_in_the_same_run_does_not_repeat_retrieval(env: Env) -> None:
    env.support.stub(A).add_response("describe_cases", {"cases": [case(A, "5001")]}, params())
    env.handle(message(A))
    assert len(env.annotation_messages()) == 1

    response = env.handle(message(A), request_id="request-2")

    assert response == {"batchItemFailures": []}
    assert env.annotation_messages() == []
    assert len(env.sts.calls) == 1


def test_case_processed_by_a_previous_run_is_never_queued_again(env: Env) -> None:
    env.mark_processed(A, "6001")
    env.support.stub(A).add_response(
        "describe_cases",
        {"cases": [case(A, "6001"), case(A, "6002")]},
        params(after="2025-11-14T00:00:00Z", before="2026-11-14T00:00:00Z"),
    )

    env.handle(message(A, NEXT_RUN))

    assert [m["displayId"] for m in env.annotation_messages()] == ["6002"]
    assert env.raw_keys() == [raw_key(A, "6002")]


def test_transient_failure_fails_only_that_record_and_is_retried_not_cached(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.support.stub(A).add_response("describe_cases", {"cases": [case(A, "7001")]}, params())
    env.support.stub(B).add_response("describe_cases", {"cases": [case(B, "7002")]}, params())

    def flaky_send(sqs: SQSClient, url: str, bodies: list[dict[str, Any]]) -> int:
        if bodies and bodies[0]["accountId"] == A:
            raise SqsSendError(1, ("InternalError",))
        return send_messages(sqs, url, bodies)

    with monkeypatch.context() as patch:
        patch.setattr(app, "send_messages", flaky_send)
        response = env.handle(message(A), message(B))

    assert response == {"batchItemFailures": [{"itemIdentifier": "m0"}]}
    assert [m["accountId"] for m in env.annotation_messages()] == [B]
    assert env.metrics()["CaseRetrievalFailures"] == 1

    env.support.stub(A).add_response("describe_cases", {"cases": [case(A, "7001")]}, params())
    assert env.handle(message(A), request_id="request-2") == {"batchItemFailures": []}
    assert [m["displayId"] for m in env.annotation_messages()] == ["7001"]


def test_support_server_error_fails_the_record_without_logging_error_text(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    env.support.stub(A).add_client_error(
        "describe_cases",
        service_error_code="InternalServerError",
        service_message="SENTINEL-ERROR-TEXT",
        http_status_code=500,
    )

    response = env.handle(message(A))

    assert response == {"batchItemFailures": [{"itemIdentifier": "m0"}]}
    assert env.raw_keys("") == []
    logs = env.kit.logs()
    assert '"error_type":"InternalServerError","error_code":"InternalServerError"' in logs
    assert "SENTINEL-ERROR-TEXT" not in logs + caplog.text


def test_every_record_failing_is_reported_without_raising(env: Env) -> None:
    response = env.handle("not json", {"accountId": A})
    assert response == {"batchItemFailures": [{"itemIdentifier": "m0"}, {"itemIdentifier": "m1"}]}


@pytest.mark.parametrize(
    "body",
    [
        "{not json",
        ["list"],
        {"accountId": A, "runId": RUN},
        {"accountId": "12", "accountName": "x", "runId": RUN},
        {"accountId": A, "accountName": "x", "runId": "yesterday"},
        {**message(A), "extra": "field"},
    ],
    ids=["invalid-json", "not-object", "missing-name", "bad-account", "bad-run", "extra-field"],
)
def test_malformed_messages_fail_visibly_while_valid_records_succeed(
    env: Env, body: object
) -> None:
    env.support.stub(A).add_response("describe_cases", {"cases": []}, params())

    response = env.handle(body, message(A))

    assert response == {"batchItemFailures": [{"itemIdentifier": "m0"}]}
    metrics = env.metrics()
    assert metrics["InvalidMessages"] == 1
    assert metrics["CaseRetrievalFailures"] == 1
    assert metrics["AccountsProcessed"] == 1


def test_malformed_cases_are_counted_and_skipped(env: Env) -> None:
    bad_time = case(A, "8001", created="not-a-date")
    bad_display = case(A, "../8002")
    duplicate = case(A, "8003")
    env.support.stub(A).add_response(
        "describe_cases", {"cases": [bad_time, bad_display, duplicate, duplicate]}, params()
    )

    env.handle(message(A))

    assert [m["displayId"] for m in env.annotation_messages()] == ["8003"]
    metrics = env.metrics()
    assert metrics["CasesInvalid"] == 2
    assert metrics["CasesRetrieved"] == 1


def test_account_with_no_eligible_cases_succeeds_without_messages(env: Env) -> None:
    env.support.stub(A).add_response("describe_cases", {"cases": []}, params())
    assert env.handle(message(A)) == {"batchItemFailures": []}
    assert env.annotation_messages() == []
    assert env.raw_keys() == []
    # A zero-case account is settled for the run.
    assert env.account_marker(A)["cases"] == []
    assert env.account_marker(A)["outcome"] == "retrieved"


def test_account_is_not_settled_until_every_reference_is_queued(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.support.stub(A).add_response("describe_cases", {"cases": [case(A, "9001")]}, params())

    def failing_send(*_: Any, **__: Any) -> int:  # noqa: ANN401
        raise SqsSendError(1, ("InternalError",))

    monkeypatch.setattr(app, "send_messages", failing_send)

    assert env.handle(message(A)) == {"batchItemFailures": [{"itemIdentifier": "m0"}]}
    assert env.raw_keys("runs/") == []

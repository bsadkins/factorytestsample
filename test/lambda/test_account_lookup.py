"""Account Lookup handler: scheduled, paginated discovery of active member accounts."""

from __future__ import annotations

import io
import json
import logging
import re
import traceback
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from account_lookup import app
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from common.account_list import ACCOUNT_LIST_PREFIX, account_list_key

if TYPE_CHECKING:
    from types_boto3_organizations.client import OrganizationsClient
    from types_boto3_s3.client import S3Client

BUCKET = "account-lists-test"
MANAGEMENT = "999999999999"
SCHEDULED_EVENT = {"scheduledTime": "2026-11-13T00:00:00Z"}
SCHEDULED_KEY = f"{ACCOUNT_LIST_PREFIX}20261113T000000Z.json"
LOOKUP_METRICS = {
    "AccountsListed",
    "AccountsDiscovered",
    "AccountsExcludedInactive",
    "AccountsExcludedManagement",
    "AccountListsWritten",
}


def account(account_id: str, name: str, **status: str) -> dict[str, Any]:
    return {
        "Id": account_id,
        "Arn": f"arn:aws:organizations::{MANAGEMENT}:account/o-example/{account_id}",
        "Name": name,
        "JoinedTimestamp": datetime(2024, 1, 1),
        **status,
    }


@pytest.fixture
def s3(mocked_aws: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[S3Client]:
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)
    client.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
    monkeypatch.setenv("ACCOUNT_LIST_BUCKET", BUCKET)
    monkeypatch.setenv("MANAGEMENT_ACCOUNT_ID", MANAGEMENT)
    cached_clients = app.clients
    cached_clients.cache_clear()
    yield client
    cached_clients.cache_clear()


def use_organizations(monkeypatch: pytest.MonkeyPatch, client: OrganizationsClient) -> None:
    real = app.clients()
    monkeypatch.setattr(app, "clients", lambda: app.Clients(organizations=client, s3=real.s3))


def stubbed_organizations(
    monkeypatch: pytest.MonkeyPatch, pages: list[list[dict[str, Any]]], repeat: int = 1
) -> Stubber:
    client: OrganizationsClient = boto3.client("organizations", region_name="us-east-1")
    stub = Stubber(client)
    for _ in range(repeat):
        for index, page in enumerate(pages):
            response: dict[str, Any] = {"Accounts": page}
            if index < len(pages) - 1:
                response["NextToken"] = f"token-{index + 1}"
            expected = {} if index == 0 else {"NextToken": f"token-{index}"}
            stub.add_response("list_accounts", response, expected)
    stub.activate()
    use_organizations(monkeypatch, client)
    return stub


@pytest.fixture
def log_stream(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """Receives everything the handler's Powertools Logger writes. Logger binds its stream at
    import time, so `capsys` only sees EMF metrics."""
    stream = io.StringIO()
    for log_handler in logging.getLogger(app.logger.service).handlers:
        if isinstance(log_handler, logging.StreamHandler):
            monkeypatch.setattr(log_handler, "stream", stream)
    return stream


def log_records(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def assert_safe_discovery_error(error: BaseException, *sentinels: str) -> None:
    """`error` is what Lambda reports as errorType/errorMessage/stackTrace and records in the
    OnFailure destination: a fixed message, no displayed chain and no attached AWS response."""
    assert type(error) is app.AccountDiscoveryError
    assert error.args == ("account discovery failed",)
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert vars(error) == {}
    rendered = "".join(traceback.format_exception(error))
    assert "During handling of the above exception" not in rendered
    assert "direct cause" not in rendered
    for sentinel in sentinels:
        assert sentinel not in str(error)
        assert sentinel not in repr(error)
        assert sentinel not in rendered


def stored_document(s3: S3Client, key: str) -> dict[str, Any]:
    body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    document: dict[str, Any] = json.loads(body)
    return document


MIXED_PAGES = [
    [
        account("111111111111", "SENTINEL-NAME-A", State="ACTIVE", Status="ACTIVE"),
        account(MANAGEMENT, "Management", State="ACTIVE", Status="ACTIVE"),
        account("222222222222", "Suspended", State="SUSPENDED", Status="SUSPENDED"),
    ],
    [
        account("555555555555", "Legacy status only", Status="ACTIVE"),
        account("333333333333", "SENTINEL-NAME-C", State="ACTIVE"),
        account("444444444444", "Closing", State="PENDING_CLOSURE", Status="ACTIVE"),
    ],
    [
        account("666666666666", "Closed", State="CLOSED"),
        account("777777777777", "Pending", State="PENDING_ACTIVATION"),
    ],
]


def test_multipage_discovery_selects_only_active_members(
    s3: S3Client,
    monkeypatch: pytest.MonkeyPatch,
    lambda_context: LambdaContext,
    capsys: pytest.CaptureFixture[str],
    metrics_in: Callable[[str], dict[str, float]],
) -> None:
    stub = stubbed_organizations(monkeypatch, MIXED_PAGES)

    result = app.handler(SCHEDULED_EVENT, lambda_context)

    stub.assert_no_pending_responses()
    assert result == {
        "runId": "20261113T000000Z",
        "accountListKey": SCHEDULED_KEY,
        "accountsSelected": 3,
        "created": True,
    }
    document = stored_document(s3, SCHEDULED_KEY)
    assert document["accounts"] == [
        {"accountId": "111111111111", "accountName": "SENTINEL-NAME-A"},
        {"accountId": "333333333333", "accountName": "SENTINEL-NAME-C"},
        {"accountId": "555555555555", "accountName": "Legacy status only"},
    ]
    assert document["counts"] == {
        "listed": 8,
        "selected": 3,
        "excludedInactive": 4,
        "excludedManagement": 1,
    }
    assert document["runId"] == "20261113T000000Z"
    assert document["schemaVersion"] == 1
    head = s3.head_object(Bucket=BUCKET, Key=SCHEDULED_KEY)
    assert head["ServerSideEncryption"] == "AES256"

    output = capsys.readouterr().out
    metrics = metrics_in(output)
    assert metrics == {
        "AccountsListed": 8,
        "AccountsDiscovered": 3,
        "AccountsExcludedInactive": 4,
        "AccountsExcludedManagement": 1,
        "AccountListsWritten": 1,
    }
    # Discovery is not pipeline completion: no heartbeat-like metric may be emitted here.
    assert set(metrics) == LOOKUP_METRICS
    assert not any("heartbeat" in name.lower() or "complet" in name.lower() for name in metrics)
    assert "SENTINEL-NAME" not in output


def test_organisation_with_only_the_management_account_writes_a_valid_empty_list(
    s3: S3Client,
    monkeypatch: pytest.MonkeyPatch,
    lambda_context: LambdaContext,
    capsys: pytest.CaptureFixture[str],
    metrics_in: Callable[[str], dict[str, float]],
) -> None:
    stubbed_organizations(monkeypatch, [[account(MANAGEMENT, "Management", State="ACTIVE")]])

    result = app.handler(SCHEDULED_EVENT, lambda_context)

    assert result["accountsSelected"] == 0
    assert result["created"] is True
    document = stored_document(s3, SCHEDULED_KEY)
    assert document["accounts"] == []
    assert document["counts"] == {
        "listed": 1,
        "selected": 0,
        "excludedInactive": 0,
        "excludedManagement": 1,
    }
    metrics = metrics_in(capsys.readouterr().out)
    assert metrics["AccountsDiscovered"] == 0
    assert set(metrics) == LOOKUP_METRICS


def test_duplicate_scheduled_invocation_keeps_the_first_list(
    s3: S3Client,
    monkeypatch: pytest.MonkeyPatch,
    make_context: Callable[[str], LambdaContext],
    capsys: pytest.CaptureFixture[str],
    metrics_in: Callable[[str], dict[str, float]],
) -> None:
    stubbed_organizations(monkeypatch, MIXED_PAGES, repeat=2)
    first = app.handler(SCHEDULED_EVENT, make_context("request-1"))
    capsys.readouterr()

    second = app.handler(SCHEDULED_EVENT, make_context("request-2"))

    assert first["created"] is True
    assert second == {**first, "created": False}
    versions = s3.list_object_versions(Bucket=BUCKET, Prefix=SCHEDULED_KEY)["Versions"]
    assert len(versions) == 1
    assert metrics_in(capsys.readouterr().out) == {"DuplicateDiscoveryInvocations": 1}


def test_manual_invocation_gets_a_unique_run(
    s3: S3Client, monkeypatch: pytest.MonkeyPatch, lambda_context: LambdaContext
) -> None:
    stubbed_organizations(monkeypatch, MIXED_PAGES, repeat=2)

    first = app.handler({}, lambda_context)
    second = app.handler({}, lambda_context)

    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", first["runId"])
    assert first["runId"] != second["runId"]
    keys = [item["Key"] for item in s3.list_objects_v2(Bucket=BUCKET)["Contents"]]
    assert sorted(keys) == sorted(account_list_key(run["runId"]) for run in (first, second))


@pytest.mark.parametrize(
    "event",
    [
        {"scheduledTime": "not-a-time"},
        {"scheduledTime": "2026-11-13T00:00:00"},
        {"scheduledTime": 5},
        ["not", "an", "object"],
    ],
)
def test_invalid_schedule_input_fails_before_any_call(
    s3: S3Client, monkeypatch: pytest.MonkeyPatch, lambda_context: LambdaContext, event: object
) -> None:
    stub = stubbed_organizations(monkeypatch, [])
    with pytest.raises(app.AccountDiscoveryError) as raised:
        app.handler(event, lambda_context)
    assert_safe_discovery_error(raised.value)
    assert isinstance(raised.value.__context__, ValueError)
    stub.assert_no_pending_responses()
    assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET)


SERVICE_SENTINEL = "SENTINEL-SERVICE-MESSAGE"


def test_organizations_failure_writes_nothing(
    s3: S3Client,
    monkeypatch: pytest.MonkeyPatch,
    lambda_context: LambdaContext,
    log_stream: io.StringIO,
) -> None:
    client: OrganizationsClient = boto3.client("organizations", region_name="us-east-1")
    stub = Stubber(client)
    stub.add_response("list_accounts", {"Accounts": MIXED_PAGES[0], "NextToken": "token-1"}, {})
    stub.add_client_error(
        "list_accounts",
        service_error_code="AccessDeniedException",
        service_message=SERVICE_SENTINEL,
        http_status_code=403,
    )
    stub.activate()
    use_organizations(monkeypatch, client)

    with pytest.raises(app.AccountDiscoveryError) as raised:
        app.handler(SCHEDULED_EVENT, lambda_context)

    stub.assert_no_pending_responses()
    assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET)
    assert_safe_discovery_error(raised.value, SERVICE_SENTINEL, "SENTINEL-NAME")
    assert isinstance(raised.value.__context__, client.exceptions.AccessDeniedException)
    assert SERVICE_SENTINEL not in log_stream.getvalue()
    [failure] = [r for r in log_records(log_stream) if r["message"] == "Account discovery failed"]
    assert failure["level"] == "ERROR"
    assert failure["error_type"] == "AccessDeniedException"
    assert failure["error_code"] == "AccessDeniedException"
    assert failure["http_status"] == 403
    assert "exception" not in failure


CUSTOMER_SENTINEL = "SYNTHETIC-CUSTOMER-NAME"


def test_account_list_write_failure_raises_a_safe_error_for_lambda_retries(
    monkeypatch: pytest.MonkeyPatch,
    lambda_context: LambdaContext,
    capsys: pytest.CaptureFixture[str],
    log_stream: io.StringIO,
    aws_env: None,
) -> None:
    """The S3 error echoes the customer account name from the attempted document; neither the
    exception Lambda reports (and keeps in the OnFailure record) nor any log may carry it."""
    monkeypatch.setenv("ACCOUNT_LIST_BUCKET", BUCKET)
    monkeypatch.setenv("MANAGEMENT_ACCOUNT_ID", MANAGEMENT)
    organizations: OrganizationsClient = boto3.client("organizations", region_name="us-east-1")
    s3: S3Client = boto3.client("s3", region_name="us-east-1")
    org_stub, s3_stub = Stubber(organizations), Stubber(s3)
    org_stub.add_response(
        "list_accounts",
        {"Accounts": [account("222222222222", CUSTOMER_SENTINEL, State="ACTIVE")]},
        {},
    )
    s3_stub.add_client_error(
        "put_object",
        service_error_code="InternalError",
        service_message=f"Rejected document for {CUSTOMER_SENTINEL}",
        http_status_code=500,
    )
    bodies: list[bytes] = []
    s3.meta.events.register_first(
        "before-parameter-build.s3.PutObject",
        lambda params, **_: bodies.append(params["Body"]),
    )
    monkeypatch.setattr(app, "clients", lambda: app.Clients(organizations=organizations, s3=s3))

    with org_stub, s3_stub, pytest.raises(app.AccountDiscoveryError) as raised:
        app.handler(SCHEDULED_EVENT, lambda_context)

    org_stub.assert_no_pending_responses()
    s3_stub.assert_no_pending_responses()
    assert len(bodies) == 1
    assert CUSTOMER_SENTINEL.encode() in bodies[0]
    error = raised.value
    # The SDK error stays reachable only as suppressed context, which Lambda does not render.
    assert isinstance(error.__context__, ClientError)
    assert CUSTOMER_SENTINEL in str(error.__context__)
    assert_safe_discovery_error(error, CUSTOMER_SENTINEL)
    logs = log_stream.getvalue()
    assert CUSTOMER_SENTINEL not in logs + capsys.readouterr().out
    assert "Traceback" not in logs
    [failure] = [r for r in log_records(log_stream) if r["message"] == "Account discovery failed"]
    assert failure["level"] == "ERROR"
    assert failure["error_type"] == "ClientError"
    assert failure["error_code"] == "InternalError"
    assert failure["http_status"] == 500
    assert failure["run_id"] == "20261113T000000Z"


def test_retry_after_a_failed_write_creates_the_list(
    s3: S3Client,
    monkeypatch: pytest.MonkeyPatch,
    make_context: Callable[[str], LambdaContext],
) -> None:
    stubbed_organizations(monkeypatch, MIXED_PAGES, repeat=2)

    def failing_write(*_args: object, **_kwargs: object) -> None:
        raise ClientError(
            {
                "Error": {"Code": "SlowDown", "Message": SERVICE_SENTINEL},
                "ResponseMetadata": {"HTTPStatusCode": 503},  # type: ignore[typeddict-item]
            },
            "PutObject",
        )

    with monkeypatch.context() as patch:
        patch.setattr(app, "write_json", failing_write)
        with pytest.raises(app.AccountDiscoveryError):
            app.handler(SCHEDULED_EVENT, make_context("request-1"))
    assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET)

    retried = app.handler(SCHEDULED_EVENT, make_context("request-1"))

    assert retried["created"] is True
    assert stored_document(s3, SCHEDULED_KEY)["counts"]["selected"] == 3


def test_missing_configuration_is_rejected(
    s3: S3Client, monkeypatch: pytest.MonkeyPatch, lambda_context: LambdaContext
) -> None:
    monkeypatch.setenv("MANAGEMENT_ACCOUNT_ID", "not-an-account")
    with pytest.raises(app.AccountDiscoveryError) as raised:
        app.handler(SCHEDULED_EVENT, lambda_context)
    assert_safe_discovery_error(raised.value, "not-an-account")
    assert isinstance(raised.value.__context__, ValueError)
    monkeypatch.setenv("MANAGEMENT_ACCOUNT_ID", MANAGEMENT)
    monkeypatch.delenv("ACCOUNT_LIST_BUCKET")
    with pytest.raises(app.AccountDiscoveryError) as raised:
        app.handler(SCHEDULED_EVENT, lambda_context)
    assert_safe_discovery_error(raised.value)
    assert isinstance(raised.value.__context__, KeyError)


def test_moto_organisation_pages_and_closed_accounts(
    s3: S3Client, monkeypatch: pytest.MonkeyPatch, lambda_context: LambdaContext
) -> None:
    organizations = boto3.client("organizations", region_name="us-east-1")
    organizations.create_organization(FeatureSet="ALL")
    management = organizations.describe_organization()["Organization"]["MasterAccountId"]
    monkeypatch.setenv("MANAGEMENT_ACCOUNT_ID", management)
    created = []
    for index in range(45):
        status = organizations.create_account(
            AccountName=f"member-{index}", Email=f"member-{index}@example.com"
        )["CreateAccountStatus"]
        created.append(status["AccountId"])
    organizations.close_account(AccountId=created[0])

    result = app.handler(SCHEDULED_EVENT, lambda_context)

    document = stored_document(s3, result["accountListKey"])
    selected = {entry["accountId"] for entry in document["accounts"]}
    assert selected == set(created[1:])
    assert management not in selected
    assert document["counts"] == {
        "listed": 46,
        "selected": 44,
        "excludedInactive": 1,
        "excludedManagement": 1,
    }

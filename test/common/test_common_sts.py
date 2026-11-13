"""Member-role assumption helper."""

import datetime
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from common.retry import RetryPolicy
from common.sts import (
    AccountAccessDeniedError,
    AssumeRoleError,
    assume_role,
    member_role_arn,
    safe_session_name,
)
from types_boto3_sts.client import STSClient

ROLE = "arn:aws:iam::222222222222:role/Support-Case-Analysis-Role-scidev"
FAST = RetryPolicy(max_attempts=2, base_delay_seconds=0.0, max_delay_seconds=0.0)


def credentials() -> dict[str, Any]:
    return {
        "Credentials": {
            "AccessKeyId": "ASIAEXAMPLEEXAMPLE00",
            "SecretAccessKey": "secret-value-should-never-be-logged",
            "SessionToken": "token-value",
            "Expiration": datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC),
        },
        "AssumedRoleUser": {"AssumedRoleId": "AROAEXAMPLE:session", "Arn": ROLE + "/session"},
    }


def sts_client() -> STSClient:
    return boto3.client(
        "sts", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y"
    )


def test_member_role_arn() -> None:
    assert member_role_arn("222222222222", "Support-Case-Analysis-Role-scidev") == ROLE
    assert (
        member_role_arn("222222222222", "R", partition="aws-cn")
        == "arn:aws-cn:iam::222222222222:role/R"
    )
    with pytest.raises(ValueError, match="account"):
        member_role_arn("2222", "R")
    with pytest.raises(ValueError, match="role name"):
        member_role_arn("222222222222", "bad name!")


def test_safe_session_name_is_sanitised_and_bounded() -> None:
    assert safe_session_name("case-retrieval", "run:2026/11/13") == "case-retrieval-run-2026-11-13"
    assert len(safe_session_name("x" * 100)) == 64
    assert safe_session_name("") == "case-insights"


def test_assume_role_returns_session_with_temporary_credentials() -> None:
    client = sts_client()
    with Stubber(client) as stub:
        stub.add_response(
            "assume_role",
            credentials(),
            {"RoleArn": ROLE, "RoleSessionName": "retrieval", "DurationSeconds": 900},
        )
        session = assume_role(ROLE, "retrieval", sts_client=client, region_name="us-east-1")
    creds = session.get_credentials()
    assert creds is not None
    assert creds.access_key == "ASIAEXAMPLEEXAMPLE00"
    assert session.region_name == "us-east-1"


def test_access_denied_is_reported_as_account_inaccessible() -> None:
    client = sts_client()
    with Stubber(client) as stub:
        stub.add_client_error(
            "assume_role", service_error_code="AccessDenied", http_status_code=403
        )
        with pytest.raises(AccountAccessDeniedError) as info:
            assume_role(ROLE, "retrieval", sts_client=client, policy=FAST, sleep=lambda _: None)
    assert info.value.account_id == "222222222222"
    assert info.value.code == "AccessDenied"


def test_throttling_is_retried_then_succeeds() -> None:
    client = sts_client()
    with Stubber(client) as stub:
        stub.add_client_error("assume_role", service_error_code="Throttling", http_status_code=400)
        stub.add_response("assume_role", credentials())
        session = assume_role(
            ROLE, "retrieval", sts_client=client, policy=FAST, sleep=lambda _: None
        )
    assert session.get_credentials() is not None


def test_other_errors_raise_assume_role_error() -> None:
    client = sts_client()
    with Stubber(client) as stub:
        stub.add_client_error(
            "assume_role", service_error_code="RegionDisabledException", http_status_code=403
        )
        with pytest.raises(AssumeRoleError) as info:
            assume_role(ROLE, "retrieval", sts_client=client, policy=FAST, sleep=lambda _: None)
    assert not isinstance(info.value, AccountAccessDeniedError)
    assert info.value.code == "RegionDisabledException"


@pytest.mark.parametrize("arn", ["arn:aws:iam::1234:role/x", "arn:aws:s3:::bucket", "not-an-arn"])
def test_invalid_role_arns_are_rejected_before_calling_sts(arn: str) -> None:
    with pytest.raises(ValueError, match="role ARN"):
        assume_role(arn, "s", sts_client=sts_client())


def test_duration_is_bounded() -> None:
    with pytest.raises(ValueError, match="duration"):
        assume_role(ROLE, "s", sts_client=sts_client(), duration_seconds=60)


def test_credentials_never_appear_in_errors_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    client = sts_client()
    with Stubber(client) as stub:
        stub.add_response("assume_role", credentials())
        session = assume_role(ROLE, "retrieval", sts_client=client)
    assert "secret-value" not in repr(session)
    assert all("secret-value" not in repr(r.__dict__) for r in caplog.records)

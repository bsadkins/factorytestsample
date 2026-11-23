"""Assume the member-account Support role with bounded retries."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Final

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

from common.aws import client_config
from common.retry import RetryPolicy, retry_with_backoff
from common.safe_logging import aws_error_code, error_fields

if TYPE_CHECKING:
    from types_boto3_sts.client import STSClient

logger = Logger(child=True)

_ACCOUNT_ID = re.compile(r"^\d{12}$")
_ROLE_NAME = re.compile(r"^[\w+=,.@-]{1,64}$")
_ROLE_ARN = re.compile(r"^arn:(aws[a-z-]*):iam::(\d{12}):role/(?:[\w+=,.@-]+/)*[\w+=,.@-]{1,64}$")
_SESSION_INVALID = re.compile(r"[^\w+=,.@-]+")
ACCESS_DENIED_CODES: Final = frozenset({"AccessDenied", "AccessDeniedException"})
MIN_DURATION_SECONDS: Final = 900
MAX_DURATION_SECONDS: Final = 3600
STS_RETRY_POLICY: Final = RetryPolicy(max_attempts=3, base_delay_seconds=0.5, max_delay_seconds=4.0)


class AssumeRoleError(Exception):
    """Role assumption failed. Carries the account ID and AWS error code, never credentials."""

    def __init__(self, account_id: str, code: str | None) -> None:
        super().__init__(f"Could not assume the member role in account {account_id} ({code})")
        self.account_id = account_id
        self.code = code


class AccountAccessDeniedError(AssumeRoleError):
    """The role is missing or does not trust the caller; the account is skipped for this run."""


def member_role_arn(account_id: str, role_name: str, *, partition: str = "aws") -> str:
    """Build the member role ARN, validating the account ID and role name."""
    if not _ACCOUNT_ID.match(account_id):
        raise ValueError("account ID must be 12 digits")
    if not _ROLE_NAME.match(role_name):
        raise ValueError("role name contains invalid characters")
    return f"arn:{partition}:iam::{account_id}:role/{role_name}"


def safe_session_name(*parts: str) -> str:
    """Join `parts` into a valid 2-64 character STS role session name."""
    name = _SESSION_INVALID.sub("-", "-".join(p for p in parts if p)).strip("-")[:64]
    return name if len(name) >= 2 else "case-insights"  # noqa: PLR2004


def assume_role(  # noqa: PLR0913
    role_arn: str,
    session_name: str,
    *,
    sts_client: STSClient | None = None,
    duration_seconds: int = MIN_DURATION_SECONDS,
    region_name: str | None = None,
    policy: RetryPolicy = STS_RETRY_POLICY,
    sleep: Callable[[float], None] = time.sleep,
) -> boto3.Session:
    """Assume `role_arn` and return a boto3 session that uses the temporary credentials.

    Throttling and transient errors are retried within `policy`. `AccessDenied` (missing role or
    trust mismatch) raises `AccountAccessDeniedError` so callers can count and skip the account;
    other failures raise `AssumeRoleError`.
    """
    match = _ROLE_ARN.match(role_arn)
    if match is None:
        raise ValueError("Invalid IAM role ARN")
    if not MIN_DURATION_SECONDS <= duration_seconds <= MAX_DURATION_SECONDS:
        raise ValueError("duration_seconds must be between 900 and 3600")
    account_id = match.group(2)
    client = sts_client or boto3.client("sts", config=client_config(max_attempts=1))
    session = safe_session_name(session_name)

    try:
        response = retry_with_backoff(
            lambda: client.assume_role(
                RoleArn=role_arn, RoleSessionName=session, DurationSeconds=duration_seconds
            ),
            policy=policy,
            sleep=sleep,
            operation_name="sts:AssumeRole",
        )
    except ClientError as exc:
        code = aws_error_code(exc)
        logger.warning(
            "Member role assumption failed", extra={"account_id": account_id, **error_fields(exc)}
        )
        if code in ACCESS_DENIED_CODES:
            raise AccountAccessDeniedError(account_id, code) from None
        raise AssumeRoleError(account_id, code) from None

    credentials = response["Credentials"]
    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=region_name,
    )

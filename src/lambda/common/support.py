"""Read-only AWS Support API access through the member-account role.

Only `DescribeCases` and `DescribeCommunications` are called; nothing here can create, update,
resolve or add a communication to a case. Missing roles, `AccessDenied` and
`SubscriptionRequiredException` raise `SupportAccessSkippedError`, which callers count as a skip
for the current run only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

import boto3
from botocore.exceptions import ClientError

from common.aws import client_config
from common.case_window import CreationWindow
from common.safe_logging import aws_error_code
from common.sts import AccountAccessDeniedError, assume_role, member_role_arn

if TYPE_CHECKING:
    from types_boto3_sts.client import STSClient
    from types_boto3_support.client import SupportClient
    from types_boto3_support.type_defs import CaseDetailsTypeDef, CommunicationTypeDef

# The Support API has a single endpoint per partition.
SUPPORT_API_REGIONS: Final = {
    "aws": "us-east-1",
    "aws-us-gov": "us-gov-west-1",
    "aws-cn": "cn-north-1",
}
RESOLVED_STATUS: Final = "resolved"
DESCRIBE_CASES_PAGE_SIZE: Final = 100
DESCRIBE_COMMUNICATIONS_PAGE_SIZE: Final = 100
ACCESS_DENIED_CODES: Final = frozenset({"AccessDenied", "AccessDeniedException"})
SUBSCRIPTION_REQUIRED_CODES: Final = frozenset({"SubscriptionRequiredException"})
CASE_NOT_FOUND_CODES: Final = frozenset({"CaseIdNotFound"})

SkipReason = Literal["AccessDenied", "SubscriptionRequired"]


class SupportAccessSkippedError(Exception):
    """The account cannot be read this run (no role/access or no Support entitlement)."""

    def __init__(self, account_id: str, reason: SkipReason) -> None:
        super().__init__(f"Support data for account {account_id} is not accessible ({reason})")
        self.account_id = account_id
        self.reason: SkipReason = reason


class CaseNotFoundError(Exception):
    """DescribeCommunications reported that the case does not exist."""


def support_api_region(partition: str) -> str:
    """The Support API Region for an AWS partition."""
    try:
        return SUPPORT_API_REGIONS[partition]
    except KeyError:
        raise ValueError("unsupported AWS partition") from None


def skip_reason(exc: BaseException) -> SkipReason | None:
    """The skip reason for an access or entitlement error, otherwise `None`."""
    if isinstance(exc, AccountAccessDeniedError):
        return "AccessDenied"
    code = aws_error_code(exc)
    if code in ACCESS_DENIED_CODES:
        return "AccessDenied"
    if code in SUBSCRIPTION_REQUIRED_CODES:
        return "SubscriptionRequired"
    return None


def support_client(session: boto3.Session, partition: str) -> SupportClient:
    """A Support client using `session`'s credentials and botocore standard retries."""
    return session.client(
        "support", region_name=support_api_region(partition), config=client_config()
    )


@dataclass(frozen=True, slots=True)
class MemberSupport:
    """Opens Support clients in member accounts through the scoped member role."""

    role_name: str
    partition: str
    sts_client: STSClient
    client_factory: Callable[[boto3.Session, str], SupportClient] = support_client

    def open(self, account_id: str, session_name: str) -> SupportClient:
        """Assume the member role in `account_id` and return a Support client.

        Raises `SupportAccessSkippedError` when the role is missing or untrusted.
        """
        role_arn = member_role_arn(account_id, self.role_name, partition=self.partition)
        try:
            session = assume_role(role_arn, session_name, sts_client=self.sts_client)
        except AccountAccessDeniedError:
            raise SupportAccessSkippedError(account_id, "AccessDenied") from None
        return self.client_factory(session, self.partition)


def _translate(exc: ClientError, account_id: str) -> Exception:
    reason = skip_reason(exc)
    if reason is not None:
        return SupportAccessSkippedError(account_id, reason)
    if aws_error_code(exc) in CASE_NOT_FOUND_CODES:
        return CaseNotFoundError("case not found")
    return exc


def iter_cases(
    client: SupportClient, account_id: str, window: CreationWindow
) -> Iterator[CaseDetailsTypeDef]:
    """Yield every case (resolved or not) created in `window`, across all pages."""
    paginator = client.get_paginator("describe_cases")
    window_parameters = window.request_parameters()
    pages = paginator.paginate(
        includeResolvedCases=True,
        includeCommunications=False,
        afterTime=window_parameters["afterTime"],
        beforeTime=window_parameters["beforeTime"],
        PaginationConfig={"PageSize": DESCRIBE_CASES_PAGE_SIZE},
    )
    try:
        for page in pages:
            yield from page.get("cases", [])
    except ClientError as exc:
        raise _translate(exc, account_id) from None


def list_communications(
    client: SupportClient, account_id: str, case_id: str
) -> list[CommunicationTypeDef]:
    """Return every communication of a case, across all pages, in API order."""
    paginator = client.get_paginator("describe_communications")
    pages = paginator.paginate(
        caseId=case_id, PaginationConfig={"PageSize": DESCRIBE_COMMUNICATIONS_PAGE_SIZE}
    )
    communications: list[CommunicationTypeDef] = []
    try:
        for page in pages:
            communications.extend(page.get("communications", []))
    except ClientError as exc:
        raise _translate(exc, account_id) from None
    return communications


def case_metadata(case: CaseDetailsTypeDef | dict[str, Any]) -> dict[str, Any]:
    """The case fields kept as raw metadata (no communications)."""
    return {
        "caseId": case.get("caseId"),
        "displayId": case.get("displayId"),
        "subject": case.get("subject"),
        "status": case.get("status"),
        "serviceCode": case.get("serviceCode"),
        "categoryCode": case.get("categoryCode"),
        "severityCode": case.get("severityCode"),
        "submittedBy": case.get("submittedBy"),
        "timeCreated": case.get("timeCreated"),
        "language": case.get("language"),
    }

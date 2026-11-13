"""Account Lookup: discover the active member accounts of the organization for one run.

Invoked by EventBridge Scheduler (input `{"scheduledTime": "<aws.scheduler.scheduled-time>"}`)
or manually with an empty event. Pages through Organizations ListAccounts, keeps ACTIVE accounts
other than the management account, and writes one run-scoped account-list object, which
triggers the account reader. Writing the list only starts a run: this function never reports
pipeline completion.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any, Final

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from common.account_list import (
    MAX_ACCOUNT_LIST_BYTES,
    AccountRef,
    DiscoveryCounts,
    account_list_key,
    build_account_list_document,
    manual_run_id,
    scheduled_run_id,
)
from common.aws import client_config
from common.processed import validate_account_id
from common.s3_json import S3PreconditionFailedError, write_json
from common.tracing import withhold_exception_messages

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext
    from types_boto3_organizations.client import OrganizationsClient
    from types_boto3_organizations.type_defs import AccountTypeDef
    from types_boto3_s3.client import S3Client

logger = Logger()
tracer = Tracer()
metrics = Metrics()
withhold_exception_messages()

ACTIVE: Final = "ACTIVE"
# Organizations throttles ListAccounts aggressively; let the SDK back off for longer.
ORGANIZATIONS_MAX_ATTEMPTS: Final = 10


@dataclass(frozen=True, slots=True)
class Settings:
    account_list_bucket: str
    management_account_id: str

    @classmethod
    def from_env(cls) -> Settings:
        management = os.environ["MANAGEMENT_ACCOUNT_ID"]
        try:
            validate_account_id(management)
        except ValueError:
            raise ValueError("MANAGEMENT_ACCOUNT_ID must be a 12-digit account ID") from None
        return cls(
            account_list_bucket=os.environ["ACCOUNT_LIST_BUCKET"],
            management_account_id=management,
        )


@dataclass(frozen=True, slots=True)
class Clients:
    organizations: OrganizationsClient
    s3: S3Client


@cache
def clients() -> Clients:
    return Clients(
        organizations=boto3.client(
            "organizations", config=client_config(max_attempts=ORGANIZATIONS_MAX_ATTEMPTS)
        ),
        s3=boto3.client("s3", config=client_config()),
    )


def resolve_run_id(event: object, now: datetime) -> str:
    """Run ID from the Scheduler `scheduledTime`, or a unique ID for a manual invocation."""
    if not isinstance(event, dict):
        raise ValueError("event must be a JSON object")
    scheduled = event.get("scheduledTime")
    if scheduled is None:
        return manual_run_id(now, secrets.token_hex(4))
    if not isinstance(scheduled, str):
        raise ValueError("scheduledTime must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(scheduled)
    except ValueError:
        raise ValueError("scheduledTime must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError("scheduledTime must include a timezone")
    return scheduled_run_id(parsed)


def is_active(account: AccountTypeDef) -> bool:
    """True when the account is ACTIVE. `State` supersedes the retired `Status` field."""
    state = account.get("State")
    if state is not None:
        return state == ACTIVE
    return account.get("Status") == ACTIVE


@tracer.capture_method(capture_response=False)
def list_member_accounts(
    organizations: OrganizationsClient, management_account_id: str
) -> tuple[list[AccountRef], DiscoveryCounts]:
    """Page through every ListAccounts page; return selected accounts sorted by ID and counts."""
    selected: dict[str, AccountRef] = {}
    seen: set[str] = set()
    excluded_inactive = excluded_management = 0
    for page in organizations.get_paginator("list_accounts").paginate():
        for account in page.get("Accounts", []):
            account_id = validate_account_id(account["Id"])
            if account_id in seen:
                continue
            seen.add(account_id)
            if account_id == management_account_id:
                excluded_management += 1
            elif not is_active(account):
                excluded_inactive += 1
            else:
                selected[account_id] = AccountRef(account_id, account.get("Name") or account_id)
    accounts = [selected[key] for key in sorted(selected)]
    counts = DiscoveryCounts(
        listed=len(seen),
        selected=len(accounts),
        excluded_inactive=excluded_inactive,
        excluded_management=excluded_management,
    )
    return accounts, counts


def discover(event: object, settings: Settings, aws: Clients, now: datetime) -> dict[str, Any]:
    run_id = resolve_run_id(event, now)
    key = account_list_key(run_id)
    logger.append_keys(run_id=run_id)
    accounts, counts = list_member_accounts(aws.organizations, settings.management_account_id)
    document = build_account_list_document(run_id, now, accounts, counts)
    try:
        write_json(
            aws.s3,
            settings.account_list_bucket,
            key,
            document,
            if_none_match=True,
            max_bytes=MAX_ACCOUNT_LIST_BYTES,
        )
    except S3PreconditionFailedError:
        # A repeated invocation for the same schedule: the first list stands and no second
        # object event (and so no second set of account messages) is produced.
        logger.warning("Account list for this run already exists", extra={"object_key": key})
        metrics.add_metric(name="DuplicateDiscoveryInvocations", unit=MetricUnit.Count, value=1)
        return {
            "runId": run_id,
            "accountListKey": key,
            "accountsSelected": counts.selected,
            "created": False,
        }

    metrics.add_metric(name="AccountsListed", unit=MetricUnit.Count, value=counts.listed)
    metrics.add_metric(name="AccountsDiscovered", unit=MetricUnit.Count, value=counts.selected)
    metrics.add_metric(
        name="AccountsExcludedInactive", unit=MetricUnit.Count, value=counts.excluded_inactive
    )
    metrics.add_metric(
        name="AccountsExcludedManagement", unit=MetricUnit.Count, value=counts.excluded_management
    )
    metrics.add_metric(name="AccountListsWritten", unit=MetricUnit.Count, value=1)
    logger.info("Account list written", extra={"object_key": key, **counts.to_document()})
    return {
        "runId": run_id,
        "accountListKey": key,
        "accountsSelected": counts.selected,
        "created": True,
    }


@logger.inject_lambda_context(log_event=False, clear_state=True)
@tracer.capture_lambda_handler(capture_response=False)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Discover member accounts and write `account-lists/<runId>.json`."""
    return discover(event, Settings.from_env(), clients(), datetime.now(UTC))

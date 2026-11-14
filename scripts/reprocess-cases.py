#!/usr/bin/env python3
"""Opt-in reprocessing of committed Support Case Insights records in an explicit date range.

Committed processed records are never analysed again automatically: every scheduled run, duplicate
delivery and ordinary redrive skips a case that has `data.json` in the processed bucket. This
script is the one deliberate exception. It selects committed records whose chosen date field falls
in an explicit, inclusive range of UTC calendar days, previews them, and only after confirmation
makes each selected case eligible for analysis again:

- requeue: when the raw inputs (`data.json` and `communications.json`) are still in the raw bucket,
  a summary-queue reference is sent first, with a delivery delay, under a fresh run ID and redrive
  ID, so the Workflow Starter claims a new execution name that no closed execution can hold. The
  stage checkpoints and then the processed record are removed while the message is delayed. A
  message that arrives while the record still exists is answered as already processed, so an
  interrupted reprocess leaves the case as it was. If removal took longer than the delay, a
  follow-up reference is sent; duplicate references never start a second execution.
- rediscover: when the raw inputs have expired but the case is still inside the collection
  lookback window, the checkpoints and processed record are removed and the next scheduled run
  collects and analyses the case again.

Cases with active work (a RUNNING or PENDING_REDRIVE execution, an execution being started, or a
Case Cleanup reset in progress), unknown status, or neither path available are left unchanged.

Removals are plain deletes, which add an S3 delete marker. Earlier versions are never deleted by
version ID, so no older processed record or checkpoint can become current again; the previous
processed version stays restorable until the bucket's noncurrent-version expiry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    PartialCredentialsError,
    ProfileNotFound,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "lambda"))

from common.case_refs import (  # noqa: E402
    COMMUNICATIONS_FILE,
    MAX_COMMUNICATION_COUNT,
    SummaryRef,
    communications_key,
    validate_case_id,
)
from common.case_window import parse_support_time, subtract_calendar_months  # noqa: E402
from common.checkpoints import STAGES, checkpoint_key  # noqa: E402
from common.conversation import MAX_CONVERSATION_BYTES  # noqa: E402
from common.processed import (  # noqa: E402
    case_partition_prefix,
    case_record_key,
    parse_case_record_key,
    validate_account_id,
    validate_display_id,
)
from common.s3_json import (  # noqa: E402
    S3JsonError,
    delete_if_unchanged,
    read_json_object,
)
from common.workflow import (  # noqa: E402
    ACTIVE_STATUSES,
    InvalidOwnershipMarkerError,
    OwnershipMarker,
    ResetClaim,
    describe_status,
    display_component,
    execution_arn,
    parse_execution_name,
    read_ownership,
)

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client
    from types_boto3_sqs.client import SQSClient
    from types_boto3_stepfunctions.client import SFNClient

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 2
EXIT_CANCELLED: Final = 3

DATE_FIELDS: Final = ("timeCreated", "Case_Retrieval_Date")
DEFAULT_DATE_FIELD: Final = "timeCreated"
DEFAULT_MAX_CASES: Final = 100
MAX_CASES_LIMIT: Final = 5000
DEFAULT_DELAY_SECONDS: Final = 60
MAX_DELAY_SECONDS: Final = 900  # the SQS per-message delay limit
# A follow-up reference is sent when removal finished this close to (or after) the delay ending.
FOLLOW_UP_MARGIN_SECONDS: Final = 5
# Rediscovery needs the case to stay inside the next scheduled runs' creation window.
REDISCOVERY_MARGIN: Final = timedelta(days=2)
# An ownership marker whose execution does not exist yet may still be started by a starter.
CLAIM_START_GRACE: Final = timedelta(hours=1)
MAX_RECORD_BYTES: Final = 1024 * 1024
RAW_RECORD_FILE: Final = "data.json"
CORE_STACK: Final = "CaseInsightsCoreStack-{unique_id}"
STATE_MACHINE_NAME: Final = "CaseAnalysis-{unique_id}"
SUMMARY_QUEUE_NAME: Final = "CaseInsights-CaseSummary-{unique_id}"
CORE_OUTPUTS: Final = {
    "raw_bucket": "RawBucketName",
    "processed_bucket": "ProcessedBucketName",
    "state_machine_arn": "CaseAnalysisStateMachineArn",
    "summary_queue_url": "CaseSummaryQueueUrl",
}
USABLE_STACK_STATUSES: Final = frozenset(
    {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE", "IMPORT_COMPLETE"}
)
ENV_NAME: Final = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
UNIQUE_ID: Final = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
REGION: Final = re.compile(r"^[a-z]{2}(-[a-z]+)+-[0-9]$")
ISO_DATE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_SCOPE_VALUES: Final = 100

ACTION_REQUEUE: Final = "requeue"
ACTION_REDISCOVER: Final = "rediscover"
ACTION_SKIP: Final = "skip"


class UsageError(Exception):
    """Invalid options or an unusable target. Nothing has been changed."""


class TargetError(UsageError):
    """The credentials, configuration or deployed resources are not the intended deployment."""


# --- options -----------------------------------------------------------------------------------


def iso_date(value: str) -> date:
    """`YYYY-MM-DD` only; other ISO forms such as `20260101` are rejected to avoid surprises."""
    if not ISO_DATE.match(value):
        raise argparse.ArgumentTypeError(f"{value!r} is not a YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid calendar date") from None


def account_id(value: str) -> str:
    try:
        return validate_account_id(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a 12-digit AWS account ID") from None


def display_id(value: str) -> str:
    try:
        return validate_display_id(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid case display ID") from None


def bounded_int(low: int, high: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            number = low - 1
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"must be an integer between {low} and {high}")
        return number

    return parse


EPILOG: Final = """\
Date selection:
  The date field is read from each committed processed record. timeCreated is the Support case
  creation time; Case_Retrieval_Date is when the pipeline retrieved the case for the committed
  analysis. Both are converted to UTC. --from and --to are UTC calendar days and both are
  inclusive: the range is [--from 00:00:00Z, the day after --to 00:00:00Z). Both are required;
  there is no "all records" default, and a range that matches nothing changes nothing.

Safety:
  Every run previews the plan first. Without --dry-run the script asks you to type the plan token
  shown in the preview, or accepts it through --confirm for non-interactive use. The token covers
  the exact records and versions previewed, so a changed record set needs a fresh preview.
  Credentials must belong to the deployment's managementAccountId, and every target comes from the
  CaseInsightsCoreStack-<uniqueId> outputs in the configuration's Region.

Versioned objects and execution identity:
  The processed record and stage checkpoints are removed with plain deletes (S3 delete markers).
  No version is deleted by version ID, so an older record cannot become current again. The
  preview and report print the previous processed version ID, restorable until the bucket's
  noncurrent-version expiry. Requeued cases use a fresh run ID and redrive ID, so the Workflow
  Starter starts attempt 1 of a new run and never reuses a closed execution name.

Exit status: 0 done or nothing to do, 1 some cases failed, 2 invalid options or target,
3 cancelled or not confirmed.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reprocess-cases.py",
        description=(
            "Explicitly reprocess committed Support Case Insights records whose date falls in an "
            "inclusive UTC date range. Normal runs and redrives never reanalyse committed cases; "
            "this is the opt-in exception."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--env",
        required=True,
        help="Environment whose config/<env>.json names the deployment (for example dev).",
    )
    parser.add_argument(
        "--from",
        dest="start",
        required=True,
        type=iso_date,
        help="First UTC day to include (YYYY-MM-DD, inclusive).",
    )
    parser.add_argument(
        "--to",
        dest="end",
        required=True,
        type=iso_date,
        help="Last UTC day to include (YYYY-MM-DD, inclusive).",
    )
    parser.add_argument(
        "--date-field",
        choices=DATE_FIELDS,
        default=DEFAULT_DATE_FIELD,
        help=f"Processed-record date field to select on (default: {DEFAULT_DATE_FIELD}).",
    )
    parser.add_argument(
        "--account-id",
        dest="account_ids",
        action="append",
        type=account_id,
        default=[],
        help="Only records of this member account. Repeatable; default: every account.",
    )
    parser.add_argument(
        "--case",
        dest="display_ids",
        action="append",
        type=display_id,
        default=[],
        help="Only this case display ID (requires exactly one --account-id). Repeatable.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only: show the plan and its token, change nothing.",
    )
    parser.add_argument(
        "--confirm",
        metavar="TOKEN",
        help="Plan token from the preview, to execute without an interactive prompt.",
    )
    parser.add_argument(
        "--max-cases",
        type=bounded_int(1, MAX_CASES_LIMIT),
        default=DEFAULT_MAX_CASES,
        help=f"Refuse plans with more cases to change than this (default: {DEFAULT_MAX_CASES}).",
    )
    parser.add_argument(
        "--delay-seconds",
        type=bounded_int(0, MAX_DELAY_SECONDS),
        default=DEFAULT_DELAY_SECONDS,
        help=(
            "Delivery delay of requeued summary references, giving the script time to remove the "
            f"old record first (0-{MAX_DELAY_SECONDS}, default: {DEFAULT_DELAY_SECONDS})."
        ),
    )
    parser.add_argument("--profile", help="AWS named profile (default: standard credential chain).")
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=REPO_ROOT / "config",
        help="Directory holding <env>.json (default: the repository's config directory).",
    )
    return parser


def check_options(args: argparse.Namespace) -> None:
    if not ENV_NAME.match(args.env):
        raise UsageError("--env must be a configuration name such as dev or prod")
    if args.start > args.end:
        raise UsageError("--from must not be after --to")
    if args.display_ids and len(args.account_ids) != 1:
        raise UsageError("--case requires exactly one --account-id")
    if len(args.account_ids) > MAX_SCOPE_VALUES or len(args.display_ids) > MAX_SCOPE_VALUES:
        raise UsageError(f"at most {MAX_SCOPE_VALUES} --account-id and --case values")


# --- deployment --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeploymentConfig:
    env: str
    unique_id: str
    region: str
    management_account_id: str
    lookback_months: int


def load_config(config_dir: Path, env: str) -> DeploymentConfig:
    """The deployment identity from `config/<env>.json`; only the fields this script needs."""
    path = config_dir / f"{env}.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TargetError(f"configuration {path} does not exist") from None
    except (OSError, ValueError):
        raise TargetError(f"configuration {path} is not readable JSON") from None
    if not isinstance(document, dict):
        raise TargetError(f"configuration {path} is not a JSON object")
    unique_id = document.get("uniqueId")
    region = document.get("region", "us-east-1")
    management = document.get("managementAccountId")
    lookback = document.get("lookbackMonths", 12)
    if document.get("environment") != env:
        raise TargetError(f"configuration {path} is not for environment {env}")
    if not isinstance(unique_id, str) or not UNIQUE_ID.match(unique_id):
        raise TargetError(f"configuration {path} has no valid uniqueId")
    if not isinstance(region, str) or not REGION.match(region):
        raise TargetError(f"configuration {path} has no valid region")
    if not isinstance(management, str) or not re.fullmatch(r"\d{12}", management):
        raise TargetError(f"configuration {path} has no valid managementAccountId")
    if isinstance(lookback, bool) or not isinstance(lookback, int) or not 1 <= lookback <= 12:  # noqa: PLR2004
        raise TargetError(f"configuration {path} has no valid lookbackMonths")
    return DeploymentConfig(env, unique_id, region, management, lookback)


@dataclass(frozen=True, slots=True)
class Clients:
    sts: Any
    cloudformation: Any
    s3: S3Client
    sqs: SQSClient
    sfn: SFNClient


def create_clients(profile: str | None, region: str) -> Clients:
    """AWS clients in the deployment's Region from the standard credential provider chain."""
    session = boto3.Session(profile_name=profile, region_name=region)
    config = Config(retries={"mode": "standard", "max_attempts": 5})
    return Clients(
        sts=session.client("sts", config=config),
        cloudformation=session.client("cloudformation", config=config),
        s3=session.client("s3", config=config),
        sqs=session.client("sqs", config=config),
        sfn=session.client("stepfunctions", config=config),
    )


@dataclass(frozen=True, slots=True)
class Deployment:
    config: DeploymentConfig
    account_id: str
    raw_bucket: str
    processed_bucket: str
    state_machine_arn: str
    summary_queue_url: str


def _stack_outputs(clients: Clients, stack_name: str) -> dict[str, str]:
    try:
        stacks = clients.cloudformation.describe_stacks(StackName=stack_name)["Stacks"]
    except ClientError as exc:
        raise TargetError(
            f"stack {stack_name} was not found in this account and Region "
            f"({exc.response.get('Error', {}).get('Code', 'error')})"
        ) from None
    if len(stacks) != 1 or stacks[0].get("StackStatus") not in USABLE_STACK_STATUSES:
        raise TargetError(f"stack {stack_name} is not in a usable state")
    return {
        str(item.get("OutputKey")): str(item.get("OutputValue"))
        for item in stacks[0].get("Outputs", [])
    }


def resolve_deployment(config: DeploymentConfig, clients: Clients) -> Deployment:
    """Check that the credentials and every resource belong to the configured deployment."""
    identity = clients.sts.get_caller_identity()
    account = str(identity["Account"])
    if account != config.management_account_id:
        raise TargetError(
            f"the credentials are for account {account}, but deployment {config.unique_id} "
            f"runs in management account {config.management_account_id}"
        )
    partition = str(identity["Arn"]).split(":")[1]
    stack_name = CORE_STACK.format(unique_id=config.unique_id)
    outputs = _stack_outputs(clients, stack_name)
    missing = [key for key in CORE_OUTPUTS.values() if not outputs.get(key)]
    if missing:
        raise TargetError(
            f"stack {stack_name} has no {', '.join(missing)} output; deploy the current version"
        )
    values = {name: outputs[key] for name, key in CORE_OUTPUTS.items()}
    expected_machine = (
        f"arn:{partition}:states:{config.region}:{account}:stateMachine:"
        f"{STATE_MACHINE_NAME.format(unique_id=config.unique_id)}"
    )
    if values["state_machine_arn"] != expected_machine:
        raise TargetError("the stack's state machine is not this deployment's CaseAnalysis")
    queue_name = SUMMARY_QUEUE_NAME.format(unique_id=config.unique_id)
    queue_arn = clients.sqs.get_queue_attributes(
        QueueUrl=values["summary_queue_url"], AttributeNames=["QueueArn"]
    )["Attributes"].get("QueueArn")
    if queue_arn != f"arn:{partition}:sqs:{config.region}:{account}:{queue_name}":
        raise TargetError("the stack's summary queue is not this deployment's summary queue")
    for name in ("raw_bucket", "processed_bucket"):
        versioning = clients.s3.get_bucket_versioning(
            Bucket=values[name], ExpectedBucketOwner=account
        )
        if versioning.get("Status") != "Enabled":
            raise TargetError(f"bucket {values[name]} does not have versioning enabled")
    return Deployment(config=config, account_id=account, **values)


# --- selection ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DateRange:
    field: str
    first_day: date
    last_day: date

    @property
    def start(self) -> datetime:
        return datetime.combine(self.first_day, datetime.min.time(), tzinfo=UTC)

    @property
    def end(self) -> datetime:
        """Exclusive end: midnight UTC after the last included day."""
        return datetime.combine(self.last_day + timedelta(days=1), datetime.min.time(), tzinfo=UTC)

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def describe(self) -> str:
        return (
            f"{self.field} (UTC) from {stamp(self.start)} inclusive to {stamp(self.end)} "
            f"exclusive ({self.first_day.isoformat()} to {self.last_day.isoformat()} inclusive)"
        )


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class Candidate:
    """A committed record in range. Holds identifiers, dates and provenance only."""

    account_id: str
    display_id: str
    case_id: str
    key: str
    version_id: str
    etag: str
    selected_value: datetime
    time_created: datetime | None
    model_id: str
    prompt_version: str


@dataclass(slots=True)
class Selection:
    candidates: list[Candidate] = field(default_factory=list)
    examined: int = 0
    out_of_range: int = 0
    unreadable: list[tuple[str, str]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def _record_keys(s3: S3Client, bucket: str, account_ids: Sequence[str]) -> Iterator[str]:
    prefixes = [case_partition_prefix(a) for a in account_ids] or ["account_number="]
    paginator = s3.get_paginator("list_objects_v2")
    for prefix in prefixes:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                key = item.get("Key", "")
                if parse_case_record_key(key) is not None:
                    yield key


def _read_record(s3: S3Client, bucket: str, key: str) -> tuple[dict[str, Any], str, str] | None:
    """The record, its version ID and ETag; `None` when absent."""
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
            return None
        raise
    body = response["Body"]
    try:
        raw = body.read(MAX_RECORD_BYTES + 1)
    finally:
        body.close()
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError("record too large")
    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict):
        raise ValueError("record is not a JSON object")
    return document, str(response.get("VersionId") or "null"), str(response.get("ETag", ""))


def _candidate(
    key: str, document: dict[str, Any], version_id: str, etag: str, date_field: str
) -> Candidate:
    parsed = parse_case_record_key(key)
    if parsed is None:  # pragma: no cover - keys are filtered before reading
        raise ValueError("not a record key")
    account, display = parsed
    if document.get("displayId") != display:
        raise ValueError("record displayId does not match its key")
    case_id = validate_case_id(document.get("caseId"))  # type: ignore[arg-type]
    selected = parse_support_time(document.get(date_field))
    if selected is None:
        raise ValueError(f"{date_field} is missing or not a timestamp with a timezone")
    return Candidate(
        account_id=account,
        display_id=display,
        case_id=case_id,
        key=key,
        version_id=version_id,
        etag=etag,
        selected_value=selected,
        time_created=parse_support_time(document.get("timeCreated")),
        model_id=str(document.get("Model_Id", "")),
        prompt_version=str(document.get("Prompt_Version", "")),
    )


def select_records(
    s3: S3Client,
    bucket: str,
    date_range: DateRange,
    account_ids: Sequence[str],
    display_ids: Sequence[str],
) -> Selection:
    """Committed records whose selected date is in range; nothing outside the scope is read."""
    selection = Selection()
    if display_ids:
        keys: Iterator[str] = iter(case_record_key(account_ids[0], d) for d in display_ids)
    else:
        keys = _record_keys(s3, bucket, account_ids)
    for key in keys:
        try:
            read = _read_record(s3, bucket, key)
        except (ValueError, UnicodeDecodeError):
            selection.examined += 1
            selection.unreadable.append((key, "record is not readable JSON"))
            continue
        if read is None:
            selection.missing.append(key)
            continue
        selection.examined += 1
        try:
            candidate = _candidate(key, *read, date_range.field)
        except ValueError as exc:
            selection.unreadable.append((key, str(exc)))
            continue
        if date_range.contains(candidate.selected_value):
            selection.candidates.append(candidate)
        else:
            selection.out_of_range += 1
    return selection


# --- assessment --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedCase:
    candidate: Candidate
    action: str
    reason: str
    communications_version_id: str | None = None
    communication_count: int | None = None


def running_cases(sfn: SFNClient, state_machine_arn: str) -> set[tuple[str, str]]:
    """`(accountId, caseComponent)` of every RUNNING CaseAnalysis execution."""
    running: set[tuple[str, str]] = set()
    paginator = sfn.get_paginator("list_executions")
    for page in paginator.paginate(stateMachineArn=state_machine_arn, statusFilter="RUNNING"):
        for execution in page.get("executions", []):
            identity = parse_execution_name(execution["name"])
            if identity is not None:
                running.add((identity.account_id, identity.case_component))
    return running


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", "ClientError"))
    return type(exc).__name__


def active_work(
    candidate: Candidate,
    deployment: Deployment,
    clients: Clients,
    running: set[tuple[str, str]],
    now: datetime,
) -> tuple[str | None, OwnershipMarker | None]:
    """Why the case must be left alone (or `None`), and its ownership marker if it has one."""
    if (candidate.account_id, display_component(candidate.display_id)) in running:
        return "a RUNNING CaseAnalysis execution belongs to the case", None
    try:
        current = read_ownership(
            clients.s3, deployment.raw_bucket, candidate.account_id, candidate.display_id
        )
    except InvalidOwnershipMarkerError:
        return "the ownership marker is unreadable; status unknown", None
    except (ClientError, S3JsonError) as exc:
        return f"the ownership marker could not be read ({_error_code(exc)})", None
    if current is None:
        return None, None
    owner, _ = current
    if isinstance(owner, ResetClaim):
        return (None if owner.is_stale(now) else "Case Cleanup is resetting the case"), None
    return _execution_reason(owner, deployment, clients, now), owner


def _execution_reason(
    owner: OwnershipMarker, deployment: Deployment, clients: Clients, now: datetime
) -> str | None:
    try:
        status = describe_status(
            clients.sfn, execution_arn(deployment.state_machine_arn, owner.execution_name)
        )
    except ClientError as exc:
        return f"execution status unknown ({_error_code(exc)})"
    if status in ACTIVE_STATUSES:
        return f"execution {owner.execution_name} is {status}"
    if status is None and now - owner.claimed_at < CLAIM_START_GRACE:
        return f"execution {owner.execution_name} is being started"
    return None


def _head_version(s3: S3Client, bucket: str, key: str) -> str | None:
    """Current version ID of `key`, or `None` when it has no current version."""
    try:
        response = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status == 404 or exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey"}:  # noqa: PLR2004
            return None
        raise
    return str(response.get("VersionId") or "null")


def _communication_count(
    s3: S3Client,
    deployment: Deployment,
    candidate: Candidate,
    version_id: str,
    marker: OwnershipMarker | None,
) -> int | None:
    """Count of the stored conversation version; never reads or returns message content."""
    if marker is not None:
        ref = marker.case_ref
        if ref.communications_version_id == version_id:
            return ref.communication_count
    try:
        document = read_json_object(
            s3,
            deployment.raw_bucket,
            communications_key(candidate.account_id, candidate.display_id),
            max_bytes=MAX_CONVERSATION_BYTES,
            version_id=version_id,
        )
    except (ClientError, S3JsonError):
        return None
    count = document.get("communicationCount")
    identity = (document.get("accountId"), document.get("caseId"), document.get("displayId"))
    communications = document.get("communications")
    if (
        identity != (candidate.account_id, candidate.case_id, candidate.display_id)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or not 0 <= count <= MAX_COMMUNICATION_COUNT
        or not isinstance(communications, list)
        or len(communications) != count
    ):
        return None
    return count


def within_lookback(candidate: Candidate, lookback_months: int, now: datetime) -> bool:
    """True when the next scheduled runs still collect the case (with a safety margin)."""
    if candidate.time_created is None:
        return False
    earliest = subtract_calendar_months(now + REDISCOVERY_MARGIN, lookback_months)
    return earliest <= candidate.time_created < now


def assess(
    candidate: Candidate,
    deployment: Deployment,
    clients: Clients,
    running: set[tuple[str, str]],
    now: datetime,
) -> PlannedCase:
    """Decide how (or whether) one in-range case can be reprocessed. Read-only."""
    reason, marker = active_work(candidate, deployment, clients, running, now)
    if reason is not None:
        return PlannedCase(candidate, ACTION_SKIP, reason)
    prefix = case_record_key(candidate.account_id, candidate.display_id).rsplit("/", 1)[0]
    try:
        raw_version = _head_version(
            clients.s3, deployment.raw_bucket, f"{prefix}/{RAW_RECORD_FILE}"
        )
        comms_version = _head_version(
            clients.s3, deployment.raw_bucket, f"{prefix}/{COMMUNICATIONS_FILE}"
        )
    except ClientError as exc:
        return PlannedCase(candidate, ACTION_SKIP, f"raw inputs unknown ({_error_code(exc)})")
    if raw_version is not None and comms_version is not None:
        count = _communication_count(clients.s3, deployment, candidate, comms_version, marker)
        if count is not None:
            return PlannedCase(
                candidate,
                ACTION_REQUEUE,
                "raw inputs available; a new execution is queued",
                communications_version_id=None if comms_version == "null" else comms_version,
                communication_count=count,
            )
    if within_lookback(candidate, deployment.config.lookback_months, now):
        return PlannedCase(
            candidate, ACTION_REDISCOVER, "raw inputs expired; the next scheduled run collects it"
        )
    return PlannedCase(
        candidate,
        ACTION_SKIP,
        "raw inputs expired and the case is outside the lookback window; record kept",
    )


def plan_token(
    deployment: Deployment, date_range: DateRange, args: argparse.Namespace, plan: list[PlannedCase]
) -> str:
    """Short digest of exactly what would change, so a confirmation cannot apply to another plan."""
    document = {
        "uniqueId": deployment.config.unique_id,
        "account": deployment.account_id,
        "field": date_range.field,
        "from": date_range.first_day.isoformat(),
        "to": date_range.last_day.isoformat(),
        "accounts": sorted(args.account_ids),
        "cases": sorted(args.display_ids),
        "changes": [
            [p.candidate.key, p.candidate.version_id, p.action, p.communications_version_id]
            for p in sorted(plan, key=lambda p: p.candidate.key)
            if p.action != ACTION_SKIP
        ],
    }
    digest = hashlib.sha256(json.dumps(document, sort_keys=True).encode("utf-8")).hexdigest()
    return f"rp-{digest[:12]}"


# --- execution ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReprocessIds:
    """Fresh identity of one confirmed reprocess: a new run and an operator redrive ID."""

    run_id: str
    redrive_id: str

    @classmethod
    def new(cls, now: datetime) -> ReprocessIds:
        token = secrets.token_hex(4)
        return cls(
            run_id=f"{now.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}-{token}",
            redrive_id=f"reprocess-{now.astimezone(UTC).strftime('%Y%m%d%H%M%S')}-{token}",
        )


@dataclass(slots=True)
class Outcome:
    planned: PlannedCase
    status: str
    detail: str
    messages: list[dict[str, Any]] = field(default_factory=list)


def summary_message(planned: PlannedCase, ids: ReprocessIds, redrive_id: str) -> dict[str, Any]:
    candidate = planned.candidate
    if planned.communication_count is None:  # pragma: no cover - set for every requeue
        raise ValueError("requeue without a communication count")
    ref = SummaryRef(
        account_id=candidate.account_id,
        case_id=candidate.case_id,
        display_id=candidate.display_id,
        run_id=ids.run_id,
        communications_version_id=planned.communications_version_id,
        communication_count=planned.communication_count,
        redrive_id=redrive_id,
    )
    return ref.to_message()


class Executor:
    """Applies a confirmed plan one case at a time; see the module docstring for the order."""

    def __init__(
        self,
        deployment: Deployment,
        clients: Clients,
        ids: ReprocessIds,
        delay_seconds: int,
        now: Callable[[], datetime],
    ) -> None:
        self.deployment = deployment
        self.clients = clients
        self.ids = ids
        self.delay_seconds = delay_seconds
        self.now = now
        self.running = running_cases(clients.sfn, deployment.state_machine_arn)

    def _send(self, message: dict[str, Any], delay: int) -> None:
        self.clients.sqs.send_message(
            QueueUrl=self.deployment.summary_queue_url,
            MessageBody=json.dumps(message, sort_keys=True, separators=(",", ":")),
            DelaySeconds=delay,
        )

    def _recheck(self, planned: PlannedCase) -> str | None:
        """Why the case can no longer be changed as previewed, or `None`."""
        candidate = planned.candidate
        current = _head_version(self.clients.s3, self.deployment.processed_bucket, candidate.key)
        if current != candidate.version_id:
            return "the processed record changed since the preview; record kept"
        reason, _ = active_work(candidate, self.deployment, self.clients, self.running, self.now())
        return reason

    def _remove_checkpoints(self, candidate: Candidate) -> int:
        removed = 0
        for stage in STAGES:
            key = checkpoint_key(candidate.account_id, candidate.display_id, stage)
            if _head_version(self.clients.s3, self.deployment.raw_bucket, key) is not None:
                self.clients.s3.delete_object(Bucket=self.deployment.raw_bucket, Key=key)
                removed += 1
        return removed

    def apply(self, planned: PlannedCase) -> Outcome:
        try:
            return self._apply(planned)
        except (ClientError, BotoCoreError, S3JsonError) as exc:
            return Outcome(planned, "failed", f"{_error_code(exc)}; see the detail above")

    def _apply(self, planned: PlannedCase) -> Outcome:
        try:
            reason = self._recheck(planned)
        except ClientError as exc:
            return Outcome(planned, "failed", f"status check failed ({_error_code(exc)}); kept")
        if reason is not None:
            return Outcome(planned, "skipped", reason)
        messages: list[dict[str, Any]] = []
        if planned.action == ACTION_REQUEUE:
            message = summary_message(planned, self.ids, self.ids.redrive_id)
            try:
                self._send(message, self.delay_seconds)
            except (ClientError, BotoCoreError) as exc:
                return Outcome(
                    planned,
                    "failed",
                    f"summary reference not sent ({_error_code(exc)}); nothing changed",
                )
            messages.append(message)
        return self._remove(planned, messages, monotonic())

    def _remove(
        self, planned: PlannedCase, messages: list[dict[str, Any]], sent_at: float
    ) -> Outcome:
        candidate = planned.candidate
        try:
            self._remove_checkpoints(candidate)
            removed = delete_if_unchanged(
                self.clients.s3, self.deployment.processed_bucket, candidate.key, candidate.etag
            )
        except (ClientError, BotoCoreError) as exc:
            return Outcome(
                planned,
                "failed",
                f"removal failed ({_error_code(exc)}); the processed record was kept and any "
                "queued reference is answered as already processed",
                messages,
            )
        if not removed:
            return Outcome(
                planned,
                "skipped",
                "the processed record changed during reprocessing; record kept",
                messages,
            )
        if planned.action == ACTION_REDISCOVER:
            return Outcome(
                planned, "rediscover", "record removed; the next scheduled run collects the case"
            )
        if monotonic() - sent_at >= self.delay_seconds - FOLLOW_UP_MARGIN_SECONDS:
            # The first reference may have arrived before the record was removed.
            follow_up = summary_message(planned, self.ids, f"{self.ids.redrive_id}-f")
            try:
                self._send(follow_up, 0)
            except (ClientError, BotoCoreError) as exc:
                return Outcome(
                    planned,
                    "failed",
                    f"record removed but the follow-up reference was not sent "
                    f"({_error_code(exc)}); send this message to the summary queue: "
                    f"{json.dumps(follow_up, sort_keys=True)}",
                    messages,
                )
            messages.append(follow_up)
        return Outcome(planned, "requeued", "record removed; new execution queued", messages)


# --- output ------------------------------------------------------------------------------------


def print_preview(
    deployment: Deployment,
    date_range: DateRange,
    args: argparse.Namespace,
    selection: Selection,
    plan: list[PlannedCase],
) -> None:
    config = deployment.config
    print(
        f"Reprocess preview for deployment {config.unique_id} (env {config.env}, account "
        f"{deployment.account_id}, {config.region})"
    )
    print(f"Date field: {date_range.describe()}")
    if args.display_ids:
        scope = f"account {args.account_ids[0]}, cases {', '.join(args.display_ids)}"
    elif args.account_ids:
        scope = f"accounts {', '.join(args.account_ids)}"
    else:
        scope = "every account"
    print(f"Scope: {scope}")
    print(
        f"Examined {selection.examined} processed records: {len(selection.candidates)} in range, "
        f"{selection.out_of_range} outside the range (unchanged), "
        f"{len(selection.unreadable)} unreadable (unchanged)."
    )
    for key in selection.missing:
        print(f"  no processed record: {key}")
    for key, reason in selection.unreadable:
        print(f"  unreadable, skipped: {key} ({reason})")
    if plan:
        print(
            f"{'ACTION':<11} {'ACCOUNT':<12} {'CASE':<20} {date_range.field:<20} "
            f"{'PROMPT':<19} {'PROCESSED VERSION':<34} REASON"
        )
    for planned in plan:
        c = planned.candidate
        print(
            f"{planned.action:<11} {c.account_id:<12} {c.display_id:<20} "
            f"{stamp(c.selected_value):<20} {c.prompt_version:<19} {c.version_id:<34} "
            f"{planned.reason}"
        )


def print_report(outcomes: list[Outcome], ids: ReprocessIds) -> None:
    print(f"Reprocess run ID {ids.run_id}, redrive ID {ids.redrive_id}")
    for outcome in outcomes:
        c = outcome.planned.candidate
        print(
            f"{outcome.status:<11} {c.account_id} {c.display_id} previous version "
            f"{c.version_id}: {outcome.detail}"
        )
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    print("Totals: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    print(
        "Queued references only start new executions; check the CaseAnalysis executions, the "
        "workflow failure queue and the processed records for the actual results."
    )


def confirmed(token: str, supplied: str | None, count: int) -> bool:
    if supplied is not None:
        if supplied != token:
            print(
                "The confirmation token does not match this plan (it may have changed since the "
                "preview). Nothing was changed.",
                file=sys.stderr,
            )
            return False
        return True
    if not sys.stdin.isatty():
        print(
            f"Confirmation required: rerun with --confirm {token} to reprocess {count} case(s). "
            "Nothing was changed.",
            file=sys.stderr,
        )
        return False
    try:
        reply = input(f"Type the plan token {token} to reprocess {count} case(s): ")
    except (EOFError, KeyboardInterrupt):
        reply = ""
    if reply.strip() != token:
        print("Cancelled. Nothing was changed.", file=sys.stderr)
        return False
    return True


def now() -> datetime:
    return datetime.now(UTC)


def monotonic() -> float:
    return time.monotonic()


def run(args: argparse.Namespace) -> int:
    check_options(args)
    config = load_config(args.config_dir, args.env)
    clients = create_clients(args.profile, config.region)
    deployment = resolve_deployment(config, clients)
    date_range = DateRange(args.date_field, args.start, args.end)
    selection = select_records(
        clients.s3, deployment.processed_bucket, date_range, args.account_ids, args.display_ids
    )
    running = (
        running_cases(clients.sfn, deployment.state_machine_arn) if selection.candidates else set()
    )
    moment = now()
    plan = [assess(c, deployment, clients, running, moment) for c in selection.candidates]
    actionable = [p for p in plan if p.action != ACTION_SKIP]
    if len(actionable) > args.max_cases:
        print_preview(deployment, date_range, args, selection, plan)
        raise UsageError(
            f"{len(actionable)} cases would change, more than --max-cases {args.max_cases}; "
            "narrow the range or scope, or raise --max-cases"
        )
    print_preview(deployment, date_range, args, selection, plan)
    if not actionable:
        print("Nothing to reprocess. No changes were made.")
        return EXIT_OK
    token = plan_token(deployment, date_range, args, plan)
    print(f"Plan token: {token} ({len(actionable)} case(s) to change)")
    if args.dry_run:
        print(f"Dry run: no changes were made. To execute this plan, rerun with --confirm {token}.")
        return EXIT_OK
    if not confirmed(token, args.confirm, len(actionable)):
        return EXIT_CANCELLED
    ids = ReprocessIds.new(now())
    executor = Executor(deployment, clients, ids, args.delay_seconds, now)
    outcomes = [executor.apply(p) for p in actionable]
    print_report(outcomes, ids)
    return EXIT_FAILED if any(o.status == "failed" for o in outcomes) else EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except UsageError as exc:
        print(f"error: {exc}. Nothing was changed.", file=sys.stderr)
        return EXIT_USAGE
    except (NoCredentialsError, PartialCredentialsError, ProfileNotFound) as exc:
        print(f"error: AWS credentials are not usable ({type(exc).__name__}).", file=sys.stderr)
        return EXIT_USAGE
    except (ClientError, BotoCoreError) as exc:
        print(
            f"error: AWS request failed ({_error_code(exc)}). Nothing more was changed.",
            file=sys.stderr,
        )
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())

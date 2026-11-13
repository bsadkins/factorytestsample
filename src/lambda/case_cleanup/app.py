"""Case Cleanup: make abandoned, unfinished cases eligible for a later run.

Runs daily from EventBridge Scheduler. It pages through the raw bucket one case partition at a
time and resets a case only when every check agrees the work is abandoned:

- the case has no committed processed record (processed cases are never touched);
- every object for the case is older than the grace period;
- no RUNNING or PENDING_REDRIVE execution of the CaseAnalysis state machine belongs to the case;
- the case's ownership marker, if any, names an execution that is closed (or that never started);
- the processed record is checked again immediately before deleting.

A reset deletes only the current versions of `data.json`, `communications.json` and the
ownership marker `workflow.json`, each with an S3 conditional delete on the ETag that was checked,
so anything a new run wrote meanwhile survives. Stage checkpoints and terminal failure markers
are kept: valid checkpoints are reused by the next attempt and stale ones are ignored. Unknown or
failed status checks never lead to a deletion; they are counted as cleanup errors.

The next daily run retrieves the case again because it still has no processed record. When the
time budget runs out the position is saved under `cleanup-state/` and the next run resumes there.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import TYPE_CHECKING, Any, Final

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from common.aws import client_config
from common.processed import case_record_key, get_existing_cases_batch
from common.s3_json import (
    S3JsonDecodeError,
    S3ObjectTooLargeError,
    delete_if_unchanged,
    object_exists,
    read_json_if_exists,
    read_json_with_etag,
    write_json,
)
from common.safe_logging import error_fields
from common.workflow import (
    ACTIVE_STATUSES,
    MARKER_FILE,
    MAX_MARKER_BYTES,
    SUCCEEDED_STATUS,
    InvalidOwnershipMarkerError,
    OwnershipMarker,
    describe_status,
    display_component,
    execution_arn,
    parse_execution_name,
)

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext
    from types_boto3_s3.client import S3Client
    from types_boto3_stepfunctions.client import SFNClient

logger = Logger()
tracer = Tracer()
metrics = Metrics()

CASE_PREFIX: Final = "account_number="
CURSOR_KEY: Final = "cleanup-state/cursor.json"
RAW_FILE: Final = "data.json"
COMMUNICATIONS_FILE: Final = "communications.json"
# Deleted in this order: data.json is rewritten first by any new run, so a changed ETag there
# stops the reset before anything else is removed.
RESETTABLE_FILES: Final = (RAW_FILE, COMMUNICATIONS_FILE, MARKER_FILE)
TIME_MARGIN_SECONDS: Final = 30
LIST_PAGE_SIZE = 1000
_CASE_KEY: Final = re.compile(r"^account_number=(\d{12})/case_number=([A-Za-z0-9_-]{1,64})/(.+)$")
_SKIP_METRICS: Final = {
    "processed": "CleanupSkippedProcessed",
    "recent": "CleanupSkippedRecent",
    "active": "CleanupSkippedActive",
    "unknown": "CleanupSkippedUnknown",
    "conflict": "CleanupDeleteConflicts",
    "nothing": "CleanupSkippedNothingToReset",
}


class CleanupError(Exception):
    """Cleanup could not establish a safe view of active work; nothing was deleted."""


@dataclass(frozen=True, slots=True)
class Settings:
    raw_bucket: str
    processed_bucket: str
    state_machine_arn: str
    grace: timedelta

    @classmethod
    def from_env(cls) -> Settings:
        hours = int(os.environ["CLEANUP_GRACE_HOURS"])
        if hours < 1:
            raise ValueError("CLEANUP_GRACE_HOURS must be at least 1")
        return cls(
            raw_bucket=os.environ["RAW_BUCKET"],
            processed_bucket=os.environ["PROCESSED_BUCKET"],
            state_machine_arn=os.environ["STATE_MACHINE_ARN"],
            grace=timedelta(hours=hours),
        )


@dataclass(frozen=True, slots=True)
class Clients:
    s3: S3Client
    sfn: SFNClient


@cache
def clients() -> Clients:
    return Clients(
        s3=boto3.client("s3", config=client_config()),
        sfn=boto3.client("stepfunctions", config=client_config()),
    )


def now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    etag: str
    last_modified: datetime


@dataclass(slots=True)
class CaseObjects:
    """Current objects of one case partition in the raw bucket."""

    account_id: str
    display_id: str
    objects: dict[str, StoredObject] = field(default_factory=dict)

    @property
    def last_key(self) -> str:
        return max(item.key for item in self.objects.values())


@dataclass(slots=True)
class Summary:
    evaluated: int = 0
    reset: int = 0
    deleted: int = 0
    errors: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    complete: bool = True

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def active_cases(sfn: SFNClient, state_machine_arn: str) -> set[tuple[str, str]]:
    """`(accountId, caseComponent)` of every RUNNING or PENDING_REDRIVE execution."""
    active: set[tuple[str, str]] = set()
    paginator = sfn.get_paginator("list_executions")
    for status in sorted(ACTIVE_STATUSES):
        for page in paginator.paginate(stateMachineArn=state_machine_arn, statusFilter=status):  # type: ignore[arg-type]
            for execution in page.get("executions", []):
                identity = parse_execution_name(execution["name"])
                if identity is not None:
                    active.add((identity.account_id, identity.case_component))
    return active


def iter_cases(s3: S3Client, bucket: str, start_after: str | None) -> Iterator[CaseObjects]:
    """Group the raw bucket's case objects by partition; listing order keeps a case contiguous."""
    paginator = s3.get_paginator("list_objects_v2")
    request: dict[str, Any] = {
        "Bucket": bucket,
        "Prefix": CASE_PREFIX,
        "PaginationConfig": {"PageSize": LIST_PAGE_SIZE},
    }
    if start_after:
        request["StartAfter"] = start_after
    current: CaseObjects | None = None
    for page in paginator.paginate(**request):
        for item in page.get("Contents", []):
            match = _CASE_KEY.match(item["Key"])
            if match is None:
                continue
            account_id, display_id, name = match.groups()
            if current is None or (current.account_id, current.display_id) != (
                account_id,
                display_id,
            ):
                if current is not None:
                    yield current
                current = CaseObjects(account_id, display_id)
            current.objects[name] = StoredObject(item["Key"], item["ETag"], item["LastModified"])
    if current is not None:
        yield current


class Cleaner:
    """Evaluates and resets case partitions; see the module docstring for the rules."""

    def __init__(self, settings: Settings, aws: Clients, started: datetime) -> None:
        self.settings = settings
        self.aws = aws
        self.started = started
        self.active = active_cases(aws.sfn, settings.state_machine_arn)
        self._processed: dict[str, set[str]] = {}

    def processed(self, account_id: str) -> set[str]:
        if account_id not in self._processed:
            self._processed[account_id] = get_existing_cases_batch(
                self.aws.s3, self.settings.processed_bucket, account_id
            )
        return self._processed[account_id]

    def is_processed_now(self, case: CaseObjects) -> bool:
        key = case_record_key(case.account_id, case.display_id)
        return object_exists(self.aws.s3, self.settings.processed_bucket, key)

    def marker_closed(self, case: CaseObjects) -> str:
        """`closed`, `active`, `processed` or `unknown` for the case's ownership marker."""
        key = case.objects[MARKER_FILE].key
        try:
            document = read_json_if_exists(
                self.aws.s3, self.settings.raw_bucket, key, max_bytes=MAX_MARKER_BYTES
            )
            if document is None:
                return "closed"
            marker = OwnershipMarker.from_document(document)
        except (S3JsonDecodeError, S3ObjectTooLargeError, InvalidOwnershipMarkerError):
            return "unknown"
        if (marker.account_id, marker.display_id) != (case.account_id, case.display_id):
            return "unknown"
        arn = execution_arn(self.settings.state_machine_arn, marker.execution_name)
        status = describe_status(self.aws.sfn, arn)
        if status in ACTIVE_STATUSES:
            return "active"
        if status == SUCCEEDED_STATUS and self.is_processed_now(case):
            return "processed"
        return "closed"

    def evaluate(self, case: CaseObjects) -> str:  # noqa: PLR0911
        """Return `reset` after deleting, or the reason the case was left alone."""
        if not any(name in case.objects for name in RESETTABLE_FILES):
            return "nothing"
        if case.display_id in self.processed(case.account_id):
            return "processed"
        newest = max(item.last_modified for item in case.objects.values())
        if self.started - newest < self.settings.grace:
            return "recent"
        if (case.account_id, display_component(case.display_id)) in self.active:
            return "active"
        if MARKER_FILE in case.objects:
            state = self.marker_closed(case)
            if state != "closed":
                return state
        if self.is_processed_now(case):
            return "processed"
        return self.reset(case)

    def reset(self, case: CaseObjects) -> str:
        for name in RESETTABLE_FILES:
            item = case.objects.get(name)
            if item is None:
                continue
            if not delete_if_unchanged(self.aws.s3, self.settings.raw_bucket, item.key, item.etag):
                return "conflict"
            metrics.add_metric(name="CleanupObjectsDeleted", unit=MetricUnit.Count, value=1)
        return "reset"


def _read_cursor(s3: S3Client, bucket: str) -> tuple[str | None, str | None]:
    stored = read_json_with_etag(s3, bucket, CURSOR_KEY, max_bytes=4096)
    if stored is None:
        return None, None
    start_after = stored.document.get("startAfter")
    if not isinstance(start_after, str) or not start_after.startswith(CASE_PREFIX):
        return None, stored.etag
    return start_after, stored.etag


@tracer.capture_method(capture_response=False)
def run_cleanup(settings: Settings, aws: Clients, context: LambdaContext) -> Summary:
    started = now()
    summary = Summary()
    try:
        cleaner = Cleaner(settings, aws, started)
    except Exception as exc:
        raise CleanupError("active executions could not be listed") from exc
    start_after, cursor_etag = _read_cursor(aws.s3, settings.raw_bucket)
    last_done: str | None = None
    for case in iter_cases(aws.s3, settings.raw_bucket, start_after):
        if context.get_remaining_time_in_millis() < TIME_MARGIN_SECONDS * 1000:
            summary.complete = False
            break
        summary.evaluated += 1
        fields = {"account_id": case.account_id, "display_id": case.display_id}
        try:
            outcome = cleaner.evaluate(case)
        except Exception as exc:
            summary.errors += 1
            summary.skip("unknown")
            logger.warning(
                "Case status could not be checked; left untouched",
                extra={**fields, **error_fields(exc)},
            )
        else:
            if outcome == "reset":
                summary.reset += 1
                logger.info("Abandoned case reset for a later run", extra=fields)
            else:
                summary.skip(outcome)
                if outcome == "unknown":
                    summary.errors += 1
                    logger.warning("Case ownership is unknown; left untouched", extra=fields)
        last_done = case.last_key
    if summary.complete:
        if cursor_etag is not None:
            delete_if_unchanged(aws.s3, settings.raw_bucket, CURSOR_KEY, cursor_etag)
    elif last_done is not None:
        write_json(
            aws.s3,
            settings.raw_bucket,
            CURSOR_KEY,
            {"startAfter": last_done, "savedAt": started.isoformat(timespec="seconds")},
        )
    return summary


def publish(summary: Summary) -> None:
    metrics.add_metric(name="CleanupCasesEvaluated", unit=MetricUnit.Count, value=summary.evaluated)
    metrics.add_metric(name="CleanupCasesReset", unit=MetricUnit.Count, value=summary.reset)
    metrics.add_metric(name="CleanupErrors", unit=MetricUnit.Count, value=summary.errors)
    for reason, count in summary.skipped.items():
        metrics.add_metric(name=_SKIP_METRICS[reason], unit=MetricUnit.Count, value=count)
    if not summary.complete:
        metrics.add_metric(name="CleanupIncomplete", unit=MetricUnit.Count, value=1)
    logger.info(
        "Cleanup finished",
        extra={
            "evaluated": summary.evaluated,
            "reset": summary.reset,
            "errors": summary.errors,
            "skipped": summary.skipped,
            "complete": summary.complete,
        },
    )


@logger.inject_lambda_context(log_event=False, clear_state=True)
@tracer.capture_lambda_handler(capture_response=False)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Scheduled cleanup; returns counts only."""
    del event
    try:
        summary = run_cleanup(Settings.from_env(), clients(), context)
    except Exception as exc:
        metrics.add_metric(name="CleanupErrors", unit=MetricUnit.Count, value=1)
        logger.error("Cleanup failed; nothing further was deleted", extra=error_fields(exc))
        raise CleanupError("cleanup failed") from None
    publish(summary)
    return {
        "evaluated": summary.evaluated,
        "reset": summary.reset,
        "errors": summary.errors,
        "complete": summary.complete,
    }

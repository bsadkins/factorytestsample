"""Case Cleanup: make abandoned, unfinished cases eligible for a later run.

Runs daily from EventBridge Scheduler. It pages through the raw bucket one case partition at a
time and resets a case only when every check agrees the work is abandoned:

- the case has no committed processed record (processed cases are never touched);
- every object for the case is older than the grace period;
- no RUNNING execution of the CaseAnalysis state machine belongs to the case (a paginated
  top-level listing; an owner waiting in PENDING_REDRIVE is found through DescribeExecution);
- the case's ownership marker, if any, names an execution that is closed (or that never started);
- a closed FAILED, TIMED_OUT or ABORTED execution has its failure record confirmed on the
  workflow failure queue first (see `common.failure_delivery`): a pending failure-delivery
  marker is written and delivered before the ownership marker can be replaced, because a
  whole-execution timeout or abort bypasses the workflow's failure handler;
- the processed record is checked again after the reset claim below is written.

Before any input is deleted, cleanup replaces exactly the ownership marker it judged (or its
absence) with a reset claim (`common.workflow.ResetClaim`), conditional on that marker's ETag. A
starter that took ownership in the meantime therefore always wins and keeps the inputs, and
starters never overwrite a recent claim. Then `data.json` and `communications.json` are deleted,
each conditional on the ETag that was listed, and finally the claim itself. If an input changed
or the processed record appeared, the claim is released and the inputs stay. A claim left by an
interrupted cleanup protects the case while recent and is completed by a later cleanup (or taken
over by a starter) once stale; another cleanup's recent claim is skipped as recent work.

Stage checkpoints and terminal failure markers are kept: valid checkpoints are reused by the next
attempt and stale ones are ignored. Failure-delivery markers are never deleted and do not count
as recent activity. Unknown or failed status checks, and failure records that could not be
preserved or sent, never lead to a deletion; they are counted as cleanup errors and retried on
the next run.

Every case partition visited, including processed, recent and active ones, also has its pending
failure-delivery markers delivered, so evidence kept by the workflow starter reaches the queue.

The next daily run retrieves the case again because it still has no processed record. When the
time budget runs out the position is saved under `cleanup-state/` and the next run resumes there.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import TYPE_CHECKING, Any, Final

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from botocore.exceptions import ClientError
from common.aws import client_config
from common.failure_delivery import (
    DELIVERY_DIR,
    DeliveryTally,
    FailureQueue,
    deliver_pending_markers,
    reconcile_closed_attempt,
)
from common.processed import case_record_key, get_existing_cases_batch
from common.s3_json import (
    S3PreconditionFailedError,
    delete_if_unchanged,
    is_not_found_error,
    object_exists,
    read_json_with_etag,
    write_json,
)
from common.safe_logging import error_fields
from common.workflow import (
    ACTIVE_STATUSES,
    FAILED_STATUSES,
    MARKER_FILE,
    MAX_MARKER_BYTES,
    SUCCEEDED_STATUS,
    InvalidOwnershipMarkerError,
    OwnershipMarker,
    ResetClaim,
    describe_status,
    display_component,
    execution_arn,
    marker_key,
    parse_execution_name,
    read_ownership,
)

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext
    from types_boto3_s3.client import S3Client
    from types_boto3_sqs.client import SQSClient
    from types_boto3_stepfunctions.client import SFNClient

logger = Logger()
tracer = Tracer()
metrics = Metrics()

CASE_PREFIX: Final = "account_number="
CURSOR_KEY: Final = "cleanup-state/cursor.json"
RAW_FILE: Final = "data.json"
COMMUNICATIONS_FILE: Final = "communications.json"
# Deleted in this order after the reset claim: data.json is rewritten first by any new run, so a
# changed ETag there stops the reset before anything else is removed.
INPUT_FILES: Final = (RAW_FILE, COMMUNICATIONS_FILE)
RESETTABLE_FILES: Final = (RAW_FILE, COMMUNICATIONS_FILE, MARKER_FILE)
LISTED_STATUS: Final = "RUNNING"
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
    failure_queue_url: str
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
            failure_queue_url=os.environ["FAILURE_QUEUE_URL"],
            grace=timedelta(hours=hours),
        )


@dataclass(frozen=True, slots=True)
class Clients:
    s3: S3Client
    sfn: SFNClient
    sqs: SQSClient


@cache
def clients() -> Clients:
    return Clients(
        s3=boto3.client("s3", config=client_config()),
        sfn=boto3.client("stepfunctions", config=client_config()),
        sqs=boto3.client("sqs", config=client_config()),
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

    @property
    def work_objects(self) -> list[StoredObject]:
        """Objects written by case work; failure-delivery markers are bookkeeping only."""
        return [
            item for name, item in self.objects.items() if not name.startswith(f"{DELIVERY_DIR}/")
        ]


@dataclass(slots=True)
class Summary:
    evaluated: int = 0
    reset: int = 0
    deleted: int = 0
    errors: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    complete: bool = True
    deliveries: DeliveryTally = field(default_factory=DeliveryTally)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def active_cases(sfn: SFNClient, state_machine_arn: str) -> set[tuple[str, str]]:
    """`(accountId, caseComponent)` of every RUNNING execution of the state machine.

    ListExecutions accepts PENDING_REDRIVE only for Map Runs, so it is not listed here; an
    execution waiting for a redrive is caught through the owner's DescribeExecution status.
    """
    active: set[tuple[str, str]] = set()
    paginator = sfn.get_paginator("list_executions")
    for page in paginator.paginate(stateMachineArn=state_machine_arn, statusFilter=LISTED_STATUS):
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
        self.queue = FailureQueue(aws.sqs, settings.failure_queue_url)
        self.deliveries = DeliveryTally()
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

    def deliver_pending(self, case: CaseObjects) -> None:
        """Deliver failure records kept as pending markers in the case partition."""
        deliver_pending_markers(
            self.aws.s3,
            self.settings.raw_bucket,
            self.queue,
            account_id=case.account_id,
            display_id=case.display_id,
            names=list(case.objects),
            now=now(),
            tally=self.deliveries,
        )

    def ownership(self, case: CaseObjects) -> tuple[str, str | None]:
        """Judge the case's `workflow.json` and return the verdict with the judged ETag.

        The verdict is `closed` (no marker, a closed or never-started owner, or a stale reset
        claim), `recent` (a reset claim another cleanup may still be working on), `active`,
        `processed` or `unknown`. The ETag is `None` when there is no marker.
        """
        try:
            current = read_ownership(
                self.aws.s3, self.settings.raw_bucket, case.account_id, case.display_id
            )
        except InvalidOwnershipMarkerError:
            return "unknown", None
        if current is None:
            return "closed", None
        owner, etag = current
        if isinstance(owner, ResetClaim):
            return ("closed" if owner.is_stale(now()) else "recent"), etag
        return self.marker_closed(case, owner), etag

    def marker_closed(self, case: CaseObjects, marker: OwnershipMarker) -> str:
        """`closed`, `active` or `processed` for the execution named by the ownership marker.

        A closed failed execution is `closed` only once its failure record is preserved and sent;
        S3 or SQS errors propagate, so the marker is not replaced. PENDING_REDRIVE cannot be
        listed for a state machine, so the owner's described status is what protects it.
        """
        arn = execution_arn(self.settings.state_machine_arn, marker.execution_name)
        status = describe_status(self.aws.sfn, arn)
        if status in ACTIVE_STATUSES:
            return "active"
        if status == SUCCEEDED_STATUS and self.is_processed_now(case):
            return "processed"
        if status in FAILED_STATUSES:
            try:
                outcome, created = reconcile_closed_attempt(
                    self.aws.s3,
                    self.settings.raw_bucket,
                    self.queue,
                    marker.case_ref,
                    attempt=marker.attempt,
                    execution_status=status,
                    now=now(),
                )
            except Exception:
                self.deliveries.errors += 1
                raise
            self.deliveries.add(outcome, created=created)
        return "closed"

    def evaluate(self, case: CaseObjects) -> str:  # noqa: PLR0911
        """Return `reset` after deleting, or the reason the case was left alone."""
        if not any(name in case.objects for name in RESETTABLE_FILES):
            return "nothing"
        if case.display_id in self.processed(case.account_id):
            return "processed"
        newest = max(item.last_modified for item in case.work_objects)
        if self.started - newest < self.settings.grace:
            return "recent"
        if (case.account_id, display_component(case.display_id)) in self.active:
            return "active"
        judged: str | None = None
        if MARKER_FILE in case.objects:
            state, judged = self.ownership(case)
            if state != "closed":
                return state
        if self.is_processed_now(case):
            return "processed"
        return self.reset(case, judged)

    def claim(self, case: CaseObjects, judged: str | None) -> str | None:
        """Replace exactly the judged `workflow.json` (or its absence) with a reset claim.

        Returns the claim's ETag, or `None` when a starter changed ownership meanwhile.
        """
        claim = ResetClaim(case.account_id, case.display_id, now())
        try:
            written = write_json(
                self.aws.s3,
                self.settings.raw_bucket,
                claim.key,
                claim.to_document(),
                if_none_match=judged is None,
                if_match=judged,
                max_bytes=MAX_MARKER_BYTES,
            )
        except S3PreconditionFailedError:
            return None
        except ClientError as exc:
            if is_not_found_error(exc):
                return None
            raise
        return written.etag

    def release(self, case: CaseObjects, claim_etag: str) -> None:
        """Remove our reset claim so a new run can take ownership; the inputs stay."""
        delete_if_unchanged(
            self.aws.s3,
            self.settings.raw_bucket,
            marker_key(case.account_id, case.display_id),
            claim_etag,
        )

    def reset(self, case: CaseObjects, judged: str | None) -> str:
        claim_etag = self.claim(case, judged)
        if claim_etag is None:
            return "conflict"
        deleted_input = False
        try:
            # A record committed between the first check and the claim must keep its inputs.
            if self.is_processed_now(case):
                self.release(case, claim_etag)
                return "processed"
            for name in INPUT_FILES:
                item = case.objects.get(name)
                if item is None:
                    continue
                if not delete_if_unchanged(
                    self.aws.s3, self.settings.raw_bucket, item.key, item.etag
                ):
                    if not deleted_input:
                        self.release(case, claim_etag)
                    return "conflict"
                deleted_input = True
                metrics.add_metric(name="CleanupObjectsDeleted", unit=MetricUnit.Count, value=1)
        except Exception:
            # Once an input is gone the claim must stay, so starters wait until a later cleanup
            # (or the stale age) completes the reset instead of running on partial inputs.
            if not deleted_input:
                with suppress(Exception):
                    self.release(case, claim_etag)
            raise
        if delete_if_unchanged(
            self.aws.s3,
            self.settings.raw_bucket,
            marker_key(case.account_id, case.display_id),
            claim_etag,
        ):
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
        errors_before = cleaner.deliveries.errors
        cleaner.deliver_pending(case)
        if cleaner.deliveries.errors > errors_before:
            summary.errors += cleaner.deliveries.errors - errors_before
            logger.warning("Pending failure record could not be delivered; kept", extra=fields)
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
    summary.deliveries = cleaner.deliveries
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
    for name, value in summary.deliveries.metrics().items():
        metrics.add_metric(name=name, unit=MetricUnit.Count, value=value)
    logger.info(
        "Cleanup finished",
        extra={
            "evaluated": summary.evaluated,
            "reset": summary.reset,
            "errors": summary.errors,
            "skipped": summary.skipped,
            "complete": summary.complete,
            "failureDeliveries": summary.deliveries.metrics(),
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

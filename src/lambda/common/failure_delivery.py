"""Durable, execution-specific failure-delivery markers for CaseAnalysis attempts.

A whole-execution timeout or abort bypasses every task Catch, and the failure handler can exhaust
its own retries, so some closed attempts never put a record on the workflow failure queue. The
marker `account_number=<id>/case_number=<displayId>/failure-deliveries/<executionName>.json` in the
raw bucket records, for one execution:

- `pending`: the execution closed FAILED, TIMED_OUT or ABORTED and no record is confirmed;
- `sent`: a failure record for the execution is confirmed on the workflow failure queue.

The workflow starter and cleanup write a `pending` marker before they replace or delete the
ownership marker of a closed failed execution, so the evidence survives both. Cleanup and run
completion then deliver pending markers. S3 and SQS cannot change atomically, so delivery is at
least once: the message is sent before the marker becomes `sent`, and a crash or failed write in
between repeats the message, with the same `deliveryKey` (the execution name), on the next
reconciliation. A failed send leaves the marker `pending` for the next attempt.

A `sent` marker only stops further failure records for that one execution. It never stops a later
run from retrying the case, and it is unrelated to annotation's terminal oversized-conversation
marker. Cleanup cannot delete these markers; they expire with the raw bucket's retention.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from common.case_refs import InvalidReferenceError, SummaryRef
from common.conversation import utc_timestamp
from common.failures import WORKFLOW_EXECUTION_CLOSED, failure_message
from common.processed import case_record_key
from common.s3_json import (
    S3JsonDecodeError,
    S3ObjectTooLargeError,
    S3PreconditionFailedError,
    read_json_with_etag,
    write_json,
)
from common.sqs import send_messages
from common.workflow import FAILED_STATUSES, execution_name

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client
    from types_boto3_sqs.client import SQSClient

DELIVERY_DIR: Final = "failure-deliveries"
DELIVERY_SCHEMA_VERSION: Final = 1
MAX_DELIVERY_MARKER_BYTES: Final = 16 * 1024
PENDING: Final = "pending"
SENT: Final = "sent"
RECONCILIATION_STAGE: Final = "workflow"
_NAME_IN_CASE: Final = re.compile(rf"^{DELIVERY_DIR}/([A-Za-z0-9_-]{{1,80}})\.json$")
_KEYS: Final = frozenset(
    {
        "schemaVersion",
        "deliveryKey",
        "status",
        "executionStatus",
        "attempt",
        "case",
        "recordedAt",
        "sentAt",
    }
)


class InvalidDeliveryMarkerError(ValueError):
    """A failure-delivery marker does not match its contract. Never echoes its content."""


def delivery_marker_key(account_id: str, display_id: str, name: str) -> str:
    """Return `account_number=<id>/case_number=<displayId>/failure-deliveries/<name>.json`."""
    prefix = case_record_key(account_id, display_id).rsplit("/", 1)[0]
    return f"{prefix}/{DELIVERY_DIR}/{name}.json"


def execution_from_case_file(name: str) -> str | None:
    """Execution name of a delivery marker given its path inside a case partition."""
    match = _NAME_IN_CASE.match(name)
    return match.group(1) if match else None


@dataclass(frozen=True, slots=True)
class DeliveryMarker:
    """Delivery state of the failure record for one closed (or failing) execution."""

    case: SummaryRef
    attempt: int
    status: str
    execution_status: str | None
    recorded_at: str
    sent_at: str | None = None

    @property
    def delivery_key(self) -> str:
        """Stable key of the failure record: the execution name."""
        return execution_name(
            self.case.account_id, self.case.display_id, self.case.run_id, self.attempt
        )

    @property
    def key(self) -> str:
        return delivery_marker_key(self.case.account_id, self.case.display_id, self.delivery_key)

    def to_document(self) -> dict[str, Any]:
        return {
            "schemaVersion": DELIVERY_SCHEMA_VERSION,
            "deliveryKey": self.delivery_key,
            "status": self.status,
            "executionStatus": self.execution_status,
            "attempt": self.attempt,
            "case": {k: v for k, v in self.case.to_message().items() if k != "redriveId"},
            "recordedAt": self.recorded_at,
            "sentAt": self.sent_at,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> DeliveryMarker:
        if set(document) != _KEYS or document["schemaVersion"] != DELIVERY_SCHEMA_VERSION:
            raise InvalidDeliveryMarkerError("delivery marker fields do not match the contract")
        attempt = document["attempt"]
        status = document["status"]
        execution_status = document["executionStatus"]
        if (
            not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or status not in (PENDING, SENT)
            or (execution_status is not None and execution_status not in FAILED_STATUSES)
            or not isinstance(document["recordedAt"], str)
            or not isinstance(document["sentAt"], (str, type(None)))
        ):
            raise InvalidDeliveryMarkerError("delivery marker values are malformed")
        try:
            case = SummaryRef.parse(document["case"])
            marker = cls(
                case=case,
                attempt=attempt,
                status=status,
                execution_status=execution_status,
                recorded_at=document["recordedAt"],
                sent_at=document["sentAt"],
            )
            if marker.delivery_key != document["deliveryKey"]:
                raise InvalidDeliveryMarkerError("delivery marker key is inconsistent")
        except (InvalidReferenceError, ValueError, TypeError) as exc:
            if isinstance(exc, InvalidDeliveryMarkerError):
                raise
            raise InvalidDeliveryMarkerError("delivery marker content is malformed") from None
        return marker


@dataclass(frozen=True, slots=True)
class StoredMarker:
    marker: DeliveryMarker
    etag: str


def read_delivery_marker(
    s3: S3Client, bucket: str, account_id: str, display_id: str, name: str
) -> StoredMarker | None:
    """The stored marker for execution `name`; `None` when absent."""
    key = delivery_marker_key(account_id, display_id, name)
    try:
        stored = read_json_with_etag(s3, bucket, key, max_bytes=MAX_DELIVERY_MARKER_BYTES)
    except (S3JsonDecodeError, S3ObjectTooLargeError):
        raise InvalidDeliveryMarkerError("delivery marker is unreadable") from None
    if stored is None:
        return None
    marker = DeliveryMarker.from_document(stored.document)
    if marker.delivery_key != name or (marker.case.account_id, marker.case.display_id) != (
        account_id,
        display_id,
    ):
        raise InvalidDeliveryMarkerError("delivery marker is for another execution")
    return StoredMarker(marker, stored.etag)


def preserve_pending(  # noqa: PLR0913
    s3: S3Client,
    bucket: str,
    ref: SummaryRef,
    *,
    attempt: int,
    execution_status: str,
    now: datetime,
) -> tuple[StoredMarker, bool]:
    """Record a `pending` marker unless one (pending or sent) exists; True when created.

    Raises on S3 errors, so callers never discard ownership evidence without it.
    """
    if execution_status not in FAILED_STATUSES:
        raise ValueError("only closed failed executions need failure delivery")
    marker = DeliveryMarker(
        case=ref,
        attempt=attempt,
        status=PENDING,
        execution_status=execution_status,
        recorded_at=utc_timestamp(now),
    )
    try:
        written = write_json(
            s3,
            bucket,
            marker.key,
            marker.to_document(),
            if_none_match=True,
            max_bytes=MAX_DELIVERY_MARKER_BYTES,
        )
    except S3PreconditionFailedError:
        existing = read_delivery_marker(
            s3, bucket, ref.account_id, ref.display_id, marker.delivery_key
        )
        if existing is None:  # deleted between the two requests; never expected
            raise InvalidDeliveryMarkerError("delivery marker vanished") from None
        return existing, False
    return StoredMarker(marker, written.etag), True


def record_sent(s3: S3Client, bucket: str, marker: DeliveryMarker, now: datetime) -> None:
    """Confirm delivery for `marker`'s execution, replacing any pending marker."""
    sent = replace(marker, status=SENT, sent_at=utc_timestamp(now))
    write_json(s3, bucket, sent.key, sent.to_document(), max_bytes=MAX_DELIVERY_MARKER_BYTES)


def reconciled_failure(marker: DeliveryMarker) -> dict[str, Any]:
    """The failure-queue message for a closed attempt that reconciliation records."""
    return failure_message(
        marker.case,
        failure_type=WORKFLOW_EXECUTION_CLOSED,
        stage=RECONCILIATION_STAGE,
        failed_at=marker.recorded_at,
        retryable=True,
        executionStatus=marker.execution_status,
        executionName=marker.delivery_key,
        attempt=marker.attempt,
        deliveryKey=marker.delivery_key,
    )


@dataclass(frozen=True, slots=True)
class FailureQueue:
    """Where reconciliation sends failure records."""

    sqs: SQSClient
    queue_url: str


def deliver(
    s3: S3Client, bucket: str, queue: FailureQueue, stored: StoredMarker, now: datetime
) -> str:
    """Deliver a pending marker's failure record.

    Returns `alreadySent`, `sent`, or `sentUnconfirmed` (sent, but the marker could not be
    updated, so a later reconciliation repeats the record under the same delivery key). A send
    error propagates and leaves the marker pending.
    """
    if stored.marker.status == SENT:
        return "alreadySent"
    send_messages(queue.sqs, queue.queue_url, [reconciled_failure(stored.marker)])
    sent = replace(stored.marker, status=SENT, sent_at=utc_timestamp(now))
    try:
        write_json(
            s3,
            bucket,
            sent.key,
            sent.to_document(),
            if_match=stored.etag,
            max_bytes=MAX_DELIVERY_MARKER_BYTES,
        )
    except S3PreconditionFailedError:
        # Another reconciler changed the marker meanwhile; it either confirmed delivery or will.
        return "sentUnconfirmed"
    except Exception:  # the marker stays pending; reported by the caller
        return "sentUnconfirmed"
    return "sent"


def reconcile_closed_attempt(  # noqa: PLR0913
    s3: S3Client,
    bucket: str,
    queue: FailureQueue,
    ref: SummaryRef,
    *,
    attempt: int,
    execution_status: str,
    now: datetime,
) -> tuple[str, bool]:
    """Preserve and deliver the failure record of a closed failed attempt.

    Returns the delivery outcome (see `deliver`) and whether a pending marker was created.
    """
    stored, created = preserve_pending(
        s3, bucket, ref, attempt=attempt, execution_status=execution_status, now=now
    )
    return deliver(s3, bucket, queue, stored, now), created


@dataclass(slots=True)
class DeliveryTally:
    """Bounded per-invocation counts of failure-delivery reconciliation (never case IDs)."""

    sent: int = 0
    unconfirmed: int = 0
    already_sent: int = 0
    preserved: int = 0
    errors: int = 0

    def add(self, outcome: str, *, created: bool = False) -> None:
        self.preserved += int(created)
        if outcome == "sent":
            self.sent += 1
        elif outcome == "sentUnconfirmed":
            self.sent += 1
            self.unconfirmed += 1
        else:
            self.already_sent += 1

    def metrics(self) -> dict[str, int]:
        """Non-zero counts by metric name."""
        counts = {
            "FailureDeliveriesSent": self.sent,
            "FailureDeliveriesUnconfirmed": self.unconfirmed,
            "FailureDeliveriesPreserved": self.preserved,
            "FailureDeliveryErrors": self.errors,
        }
        return {name: value for name, value in counts.items() if value}


def deliver_pending_markers(  # noqa: PLR0913
    s3: S3Client,
    bucket: str,
    queue: FailureQueue,
    *,
    account_id: str,
    display_id: str,
    names: Iterable[str],
    now: datetime,
    tally: DeliveryTally,
) -> None:
    """Deliver every pending marker among a case partition's object `names`.

    Errors are counted in `tally` and leave the marker pending for the next reconciliation.
    """
    for name in names:
        execution = execution_from_case_file(name)
        if execution is None:
            continue
        try:
            stored = read_delivery_marker(s3, bucket, account_id, display_id, execution)
            if stored is None or stored.marker.status == SENT:
                continue
            tally.add(deliver(s3, bucket, queue, stored, now))
        except Exception:  # counted; the pending marker is the retry evidence
            tally.errors += 1

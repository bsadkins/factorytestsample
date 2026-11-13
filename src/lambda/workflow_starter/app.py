"""Workflow Starter: start one CaseAnalysis execution per case attempt.

Consumes summary-queue references. For each case it checks the processed record, then reconciles
the case's S3 ownership marker with the Step Functions status of the execution it names:

- a committed processed record means nothing is started, ever, by this function;
- a RUNNING (or PENDING_REDRIVE) execution owns the case, whichever run started it, and is left
  alone, so duplicate deliveries and overlapping runs never create a second active execution;
- a claim whose execution does not exist yet is completed with the claim's own name and input
  (an idempotent StartExecution), unless it is older than the claim settle time, when it is
  treated as abandoned;
- a closed FAILED, TIMED_OUT or ABORTED execution is recovered with the next attempt name for
  this run (or attempt 1 of a new run), claimed with a conditional write on the marker's ETag.
  Before the marker is replaced, a pending failure-delivery marker is kept for the closed attempt
  unless its failure record is already confirmed (see `common.failure_delivery`), so a timeout or
  abort that bypassed HandleFailure still reaches the workflow failure queue.

The decision is recorded in the run marker `runs/<runId>/cases/<accountId>/<displayId>.json`
(the owning execution, or `alreadyProcessed`) for run completion.

`ExecutionAlreadyExists` is never treated as success: the name is described on the next round.
Event source maximum concurrency limits how fast executions start; it is not a limit on running
executions. Bedrock concurrency is bounded by the analysis stage function's reserved concurrency.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import TYPE_CHECKING, Any, Final

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from common.aws import client_config
from common.case_refs import InvalidReferenceError, SummaryRef
from common.failure_delivery import preserve_pending
from common.idempotency import idempotency_config
from common.processed import case_record_key
from common.run_markers import HANDOFF_OUTCOMES, CaseMarker, write_case_marker
from common.s3_json import (
    S3JsonDecodeError,
    S3ObjectTooLargeError,
    S3PreconditionFailedError,
    is_not_found_error,
    object_exists,
    read_json_with_etag,
    write_json,
)
from common.sqs import InvalidMessageError, parse_message_body
from common.sqs_batch import RecordFailedError, process_sqs_batch, run_idempotent
from common.workflow import (
    ACTIVE_STATUSES,
    FAILED_STATUSES,
    MAX_ATTEMPTS_PER_RUN,
    MAX_MARKER_BYTES,
    SUCCEEDED_STATUS,
    InvalidOwnershipMarkerError,
    OwnershipMarker,
    describe_status,
    execution_arn,
    marker_key,
    start_execution,
)

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.batch.types import PartialItemFailureResponse
    from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord
    from aws_lambda_powertools.utilities.typing import LambdaContext
    from types_boto3_s3.client import S3Client
    from types_boto3_stepfunctions.client import SFNClient

logger = Logger()
tracer = Tracer()
metrics = Metrics()

IDEMPOTENCY_EXPIRY_SECONDS: Final = 7 * 24 * 60 * 60
IDEMPOTENCY_CONFIG: Final = idempotency_config(expires_after_seconds=IDEMPOTENCY_EXPIRY_SECONDS)
MAX_RECONCILE_ROUNDS: Final = 6

_OUTCOME_METRICS: Final = {
    "started": "WorkflowsStarted",
    "alreadyActive": "WorkflowsAlreadyActive",
    "alreadyProcessed": "CasesAlreadyProcessed",
}


class OwnershipConflictError(Exception):
    """The case's ownership could not be settled within the bounded reconcile rounds."""


class AttemptsExhaustedError(Exception):
    """The run already used every attempt name for this case."""


@dataclass(frozen=True, slots=True)
class Settings:
    raw_bucket: str
    processed_bucket: str
    state_machine_arn: str
    claim_settle: timedelta

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            raw_bucket=os.environ["RAW_BUCKET"],
            processed_bucket=os.environ["PROCESSED_BUCKET"],
            state_machine_arn=os.environ["STATE_MACHINE_ARN"],
            claim_settle=timedelta(seconds=int(os.environ["CLAIM_SETTLE_SECONDS"])),
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


@dataclass(slots=True)
class Reconciliation:
    """Counters gathered while settling one case."""

    name_conflicts: int = 0
    lost_claims: int = 0
    recovered_status: str | None = None
    preserved_deliveries: int = 0


def _read_marker(
    s3: S3Client, settings: Settings, ref: SummaryRef
) -> tuple[OwnershipMarker, str] | None:
    try:
        stored = read_json_with_etag(
            s3,
            settings.raw_bucket,
            marker_key(ref.account_id, ref.display_id),
            max_bytes=MAX_MARKER_BYTES,
        )
    except (S3JsonDecodeError, S3ObjectTooLargeError):
        raise InvalidOwnershipMarkerError("marker is unreadable") from None
    if stored is None:
        return None
    marker = OwnershipMarker.from_document(stored.document)
    if not marker.owns(ref):
        raise InvalidOwnershipMarkerError("marker is for another case")
    return marker, stored.etag


def _claim(s3: S3Client, settings: Settings, marker: OwnershipMarker, etag: str | None) -> bool:
    """Write `marker` if the stored marker is still the one we read; False when we lost."""
    try:
        write_json(
            s3,
            settings.raw_bucket,
            marker.key,
            marker.to_document(),
            if_none_match=etag is None,
            if_match=etag,
            max_bytes=MAX_MARKER_BYTES,
        )
    except S3PreconditionFailedError:
        return False
    except Exception as exc:
        if is_not_found_error(exc):
            return False
        raise
    return True


def _outcome(status: str, ref: SummaryRef, **fields: Any) -> dict[str, Any]:  # noqa: ANN401
    return {"outcome": status, "accountId": ref.account_id, "displayId": ref.display_id, **fields}


def reconcile_case(ref: SummaryRef, settings: Settings, aws: Clients) -> dict[str, Any]:
    """Start, confirm or skip the execution for one case attempt; see the module docstring."""
    stats = Reconciliation()
    for _ in range(MAX_RECONCILE_ROUNDS):
        if object_exists(
            aws.s3, settings.processed_bucket, case_record_key(ref.account_id, ref.display_id)
        ):
            return _outcome("alreadyProcessed", ref)
        current = _read_marker(aws.s3, settings, ref)
        etag: str | None = None
        attempt = 1
        if current is not None:
            marker, etag = current
            arn = execution_arn(settings.state_machine_arn, marker.execution_name)
            status = describe_status(aws.sfn, arn)
            if status in ACTIVE_STATUSES:
                return _outcome(
                    "alreadyActive",
                    ref,
                    executionName=marker.execution_name,
                    ownerRunId=marker.run_id,
                    sameRun=marker.run_id == ref.run_id,
                    nameConflicts=stats.name_conflicts,
                )
            if status is None and now() - marker.claimed_at < settings.claim_settle:
                if start_execution(
                    aws.sfn, settings.state_machine_arn, marker.execution_name, marker.input
                ):
                    return _outcome(
                        "started",
                        ref,
                        executionName=marker.execution_name,
                        attempt=marker.attempt,
                        completedClaim=True,
                        nameConflicts=stats.name_conflicts,
                        lostClaims=stats.lost_claims,
                    )
                stats.name_conflicts += 1
                continue
            # A succeeded execution committed (or found) the processed record. Only a record
            # removed since then (an explicit reprocess) lets the case run again.
            if status == SUCCEEDED_STATUS and object_exists(
                aws.s3, settings.processed_bucket, case_record_key(ref.account_id, ref.display_id)
            ):
                return _outcome("alreadyProcessed", ref)
            if status in FAILED_STATUSES:
                stats.recovered_status = status
                # The marker is about to be replaced; keep durable evidence that this attempt
                # still needs its failure record (a timeout or abort bypasses HandleFailure).
                _, created = preserve_pending(
                    aws.s3,
                    settings.raw_bucket,
                    marker.case_ref,
                    attempt=marker.attempt,
                    execution_status=status,
                    now=now(),
                )
                stats.preserved_deliveries += int(created)
            attempt = marker.attempt + 1 if marker.run_id == ref.run_id else 1
        if attempt > MAX_ATTEMPTS_PER_RUN:
            raise AttemptsExhaustedError
        claim = OwnershipMarker.claim(ref, attempt, now())
        if not _claim(aws.s3, settings, claim, etag):
            stats.lost_claims += 1
            continue
        if start_execution(aws.sfn, settings.state_machine_arn, claim.execution_name, claim.input):
            return _outcome(
                "started",
                ref,
                executionName=claim.execution_name,
                attempt=attempt,
                completedClaim=False,
                recoveredFrom=stats.recovered_status,
                preservedDeliveries=stats.preserved_deliveries,
                nameConflicts=stats.name_conflicts,
                lostClaims=stats.lost_claims,
            )
        # The name belongs to an earlier execution (for example after cleanup removed its
        # marker); the marker now names it, so the next round describes it and moves on.
        stats.name_conflicts += 1
    raise OwnershipConflictError


@tracer.capture_method(capture_response=False)
def start_case(
    key: dict[str, Any], invocation: str, ref: SummaryRef, settings: Settings, aws: Clients
) -> dict[str, Any]:
    """Idempotent per summary reference; errors carry only type and AWS error code."""
    del key
    try:
        result = reconcile_case(ref, settings, aws)
        outcome = str(result["outcome"])
        write_case_marker(
            aws.s3,
            settings.raw_bucket,
            CaseMarker(
                ref.run_id,
                ref.account_id,
                ref.display_id,
                outcome,
                result.get("executionName") if outcome in HANDOFF_OUTCOMES else None,
            ),
        )
    except Exception as exc:
        raise RecordFailedError.from_exception(exc) from None
    return {"invocation": invocation, **result}


def record_outcome(result: dict[str, Any], log_fields: dict[str, Any]) -> None:
    outcome = str(result["outcome"])
    metrics.add_metric(name=_OUTCOME_METRICS[outcome], unit=MetricUnit.Count, value=1)
    if result.get("recoveredFrom"):
        metrics.add_metric(name="FailedAttemptsRecovered", unit=MetricUnit.Count, value=1)
    if result.get("preservedDeliveries"):
        metrics.add_metric(
            name="FailureDeliveriesPreserved",
            unit=MetricUnit.Count,
            value=int(result["preservedDeliveries"]),
        )
    if result.get("completedClaim"):
        metrics.add_metric(name="OwnershipClaimsCompleted", unit=MetricUnit.Count, value=1)
    if result.get("nameConflicts"):
        metrics.add_metric(
            name="ExecutionNameConflicts", unit=MetricUnit.Count, value=int(result["nameConflicts"])
        )
    fields = {
        **log_fields,
        "outcome": outcome,
        **{
            name: result[key]
            for key, name in (
                ("executionName", "execution_name"),
                ("attempt", "attempt"),
                ("recoveredFrom", "recovered_from"),
                ("ownerRunId", "owner_run_id"),
                ("sameRun", "same_run"),
                ("lostClaims", "lost_claims"),
            )
            if result.get(key) is not None
        },
    }
    logger.info("Case workflow start reconciled", extra=fields)


def handle_record(record: SQSRecord, settings: Settings, aws: Clients) -> None:
    log_fields: dict[str, Any] = {"message_id": record.message_id}
    try:
        ref = SummaryRef.parse(parse_message_body(record.raw_event))
    except (InvalidMessageError, InvalidReferenceError) as exc:
        metrics.add_metric(name="InvalidMessages", unit=MetricUnit.Count, value=1)
        metrics.add_metric(name="WorkflowStartFailures", unit=MetricUnit.Count, value=1)
        logger.error("Summary message is malformed", extra={**log_fields, "reason": str(exc)})
        raise RecordFailedError(type(exc).__name__) from None
    log_fields |= {
        "account_id": ref.account_id,
        "display_id": ref.display_id,
        "run_id": ref.run_id,
        "redrive": ref.redrive_id is not None,
    }
    try:
        outcome = run_idempotent(
            start_case,
            ref.idempotency_key(),
            config=IDEMPOTENCY_CONFIG,
            ref=ref,
            settings=settings,
            aws=aws,
        )
    except Exception as exc:
        failure = RecordFailedError.from_exception(exc)
        metrics.add_metric(name="WorkflowStartFailures", unit=MetricUnit.Count, value=1)
        logger.error("Case workflow start failed", extra={**log_fields, **failure.log_fields()})
        raise failure from None
    if outcome.duplicate or outcome.result is None:
        metrics.add_metric(name="DuplicateSummaryMessages", unit=MetricUnit.Count, value=1)
        logger.info(
            "Duplicate summary message skipped",
            extra={**log_fields, "in_progress": outcome.in_progress},
        )
        return
    record_outcome(outcome.result, log_fields)


@logger.inject_lambda_context(log_event=False, clear_state=True)
@tracer.capture_lambda_handler(capture_response=False)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> PartialItemFailureResponse:
    """Start or reconcile one execution per summary reference; report failed records only."""
    IDEMPOTENCY_CONFIG.register_lambda_context(context)
    settings = Settings.from_env()
    aws = clients()
    return process_sqs_batch(event, context, lambda record: handle_record(record, settings, aws))

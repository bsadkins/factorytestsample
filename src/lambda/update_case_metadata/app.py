"""Update Case Metadata: the final CaseAnalysis workflow stage.

Merges the case's original metadata with the summary, RCA and lifecycle checkpoints into one
complete 18-field record, validates it, and commits it with a conditional create to
`account_number=<id>/case_number=<displayId>/data.json` in the processed bucket. An existing
processed record always wins, so a duplicate or concurrent attempt never overwrites or
duplicates a committed record, and a missing or invalid input fails the stage without writing
anything.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any, Final

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from common.analysis import (
    STAGE_LIFECYCLE,
    STAGE_RCA,
    STAGE_SUMMARY,
    STAGE_UPDATE,
    IncompleteAnalysisError,
    is_processed,
    log_fields,
    parse_workflow_input,
    stage_result,
    text_digest,
)
from common.aws import client_config
from common.bedrock import validate_model_id
from common.case_refs import SummaryRef
from common.checkpoints import FALLBACK_FIELD, Checkpoint, Expectation, load_checkpoint
from common.processed_record import IncompleteRecordError, build_processed_record
from common.s3_json import (
    S3JsonDecodeError,
    S3ObjectNotFoundError,
    S3ObjectTooLargeError,
    S3PreconditionFailedError,
    read_json_object,
    write_json,
)
from common.templates import prompt_version
from common.tracing import withhold_exception_messages

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext
    from types_boto3_s3.client import S3Client

logger = Logger()
tracer = Tracer()
metrics = Metrics()
withhold_exception_messages()

MAX_RAW_METADATA_BYTES: Final = 1024 * 1024
_CHECKPOINT_CODES: Final = {
    STAGE_SUMMARY: "SummaryCheckpointMissing",
    STAGE_RCA: "RcaCheckpointMissing",
    STAGE_LIFECYCLE: "LifecycleCheckpointMissing",
}


@dataclass(frozen=True, slots=True)
class Settings:
    raw_bucket: str
    processed_bucket: str
    model_id: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            raw_bucket=os.environ["RAW_BUCKET"],
            processed_bucket=os.environ["PROCESSED_BUCKET"],
            model_id=validate_model_id(os.environ["BEDROCK_MODEL_ID"]),
        )


@dataclass(frozen=True, slots=True)
class Clients:
    s3: S3Client


@cache
def clients() -> Clients:
    return Clients(s3=boto3.client("s3", config=client_config()))


def read_raw_metadata(s3: S3Client, settings: Settings, ref: SummaryRef) -> dict[str, Any]:
    """The case metadata Case Retrieval stored, checked against the case reference."""
    try:
        raw = read_json_object(
            s3, settings.raw_bucket, ref.raw_key, max_bytes=MAX_RAW_METADATA_BYTES
        )
    except S3ObjectNotFoundError:
        raise IncompleteAnalysisError("RawMetadataMissing") from None
    except (S3JsonDecodeError, S3ObjectTooLargeError):
        raise IncompleteAnalysisError("RawMetadataUnreadable") from None
    if (raw.get("accountId"), raw.get("caseId"), raw.get("displayId")) != (
        ref.account_id,
        ref.case_id,
        ref.display_id,
    ):
        raise IncompleteAnalysisError("RawMetadataMismatch")
    return raw


def load_stage_checkpoints(
    s3: S3Client, settings: Settings, ref: SummaryRef
) -> tuple[Checkpoint, Checkpoint, Checkpoint]:
    """Load the three stage checkpoints for the current model and prompt version.

    Each classification must have been made from the summary checkpoint being committed.
    """
    version = prompt_version()

    def load(stage: str, digest: str | None) -> Checkpoint:
        expected = Expectation(stage, ref, settings.model_id, version, digest)
        checkpoint = load_checkpoint(s3, settings.raw_bucket, expected)
        if checkpoint is None:
            raise IncompleteAnalysisError(_CHECKPOINT_CODES[stage])
        return checkpoint

    summary = load(STAGE_SUMMARY, None)
    digest = text_digest(str(summary.output["Case_Summary"]))
    return summary, load(STAGE_RCA, digest), load(STAGE_LIFECYCLE, digest)


@tracer.capture_method(capture_response=False)
def commit_case(event: object, settings: Settings, aws: Clients) -> dict[str, Any]:
    """Commit one complete processed record unless the case already has one."""
    ref = parse_workflow_input(event)
    fields = {**log_fields(ref), "stage": STAGE_UPDATE}
    if is_processed(aws.s3, settings.processed_bucket, ref):
        metrics.add_metric(name="StagesSkippedAlreadyProcessed", unit=MetricUnit.Count, value=1)
        logger.info("Case already processed; existing record kept", extra=fields)
        return stage_result(STAGE_UPDATE, "alreadyProcessed", ref)
    summary, rca, lifecycle = load_stage_checkpoints(aws.s3, settings, ref)
    raw = read_raw_metadata(aws.s3, settings, ref)
    try:
        record = build_processed_record(raw, summary, rca, lifecycle)
    except IncompleteRecordError as exc:
        metrics.add_metric(name="IncompleteRecords", unit=MetricUnit.Count, value=1)
        logger.error(
            "Processed record failed validation; nothing written",
            extra={**fields, "reason": exc.code, "field": exc.field},
        )
        raise IncompleteAnalysisError("RecordInvalid") from None
    try:
        write_json(aws.s3, settings.processed_bucket, ref.raw_key, record, if_none_match=True)
    except S3PreconditionFailedError:
        metrics.add_metric(name="DuplicateCommitsPrevented", unit=MetricUnit.Count, value=1)
        logger.info("A processed record was committed concurrently; it was kept", extra=fields)
        return stage_result(STAGE_UPDATE, "alreadyProcessed", ref)
    metrics.add_metric(name="CasesProcessed", unit=MetricUnit.Count, value=1)
    unclassified = sum(bool(c.output[FALLBACK_FIELD]) for c in (rca, lifecycle))
    if unclassified:
        metrics.add_metric(name="UnclassifiedFields", unit=MetricUnit.Count, value=unclassified)
    logger.info(
        "Processed record committed",
        extra={
            **fields,
            "rca_fallback": bool(rca.output[FALLBACK_FIELD]),
            "lifecycle_fallback": bool(lifecycle.output[FALLBACK_FIELD]),
        },
    )
    return stage_result(STAGE_UPDATE, "committed", ref, key=ref.raw_key)


@logger.inject_lambda_context(log_event=False, clear_state=True)
@tracer.capture_lambda_handler(capture_response=False)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """UpdateCaseMetadata task: returns `{stage, status, accountId, displayId, processedKey}`."""
    del context
    return commit_case(event, Settings.from_env(), clients())

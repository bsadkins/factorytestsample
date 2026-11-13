"""Case Annotation: store a case's complete conversation and queue it for summarisation.

Consumes annotation references from the annotation queue. For each case it assumes the same
member role as retrieval, pages through `DescribeCommunications`, stores every communication at
`account_number=<id>/case_number=<displayId>/communications.json` in the raw bucket and queues a
summary reference. "Annotation" means reading the conversation into S3: no Support write API is
ever called.

A conversation too large for the summary prompt is still stored in full, but instead of being
queued for summarisation it is reported to the workflow failure queue, so it fails visibly rather
than being truncated. Cases that already have a processed record are skipped. Access and
entitlement errors are counted skips for this run only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any, Final

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from common.aws import client_config
from common.case_refs import AnnotationRef, InvalidReferenceError, SummaryRef
from common.conversation import (
    MAX_CASE_ANNOTATION_CHARS,
    MAX_CONVERSATION_BYTES,
    ConversationTooLargeError,
    build_conversation_document,
    check_annotation_size,
    render_case_annotation,
    utc_timestamp,
)
from common.idempotency import idempotency_config
from common.processed import get_existing_cases_batch
from common.s3_json import write_json
from common.sqs import InvalidMessageError, parse_message_body, send_messages
from common.sqs_batch import RecordFailedError, process_sqs_batch, run_idempotent
from common.sts import safe_session_name
from common.support import (
    CaseNotFoundError,
    MemberSupport,
    SupportAccessSkippedError,
    list_communications,
    support_api_region,
    support_client,
)

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.batch.types import PartialItemFailureResponse
    from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord
    from aws_lambda_powertools.utilities.typing import LambdaContext
    from types_boto3_s3.client import S3Client
    from types_boto3_sqs.client import SQSClient
    from types_boto3_sts.client import STSClient

logger = Logger()
tracer = Tracer()
metrics = Metrics()

IDEMPOTENCY_EXPIRY_SECONDS: Final = 7 * 24 * 60 * 60
IDEMPOTENCY_CONFIG: Final = idempotency_config(expires_after_seconds=IDEMPOTENCY_EXPIRY_SECONDS)
OVERSIZED_FAILURE_TYPE: Final = "ConversationTooLarge"

_OUTCOME_METRICS: Final = {
    "annotated": "CasesAnnotated",
    "alreadyProcessed": "CasesAlreadyProcessed",
    "notFound": "CasesNotFound",
    "oversized": "OversizedConversations",
    "skippedAccessDenied": "CasesSkippedAccessDenied",
    "skippedSubscriptionRequired": "CasesSkippedSubscriptionRequired",
}


@dataclass(frozen=True, slots=True)
class Settings:
    raw_bucket: str
    processed_bucket: str
    summary_queue_url: str
    failure_queue_url: str
    member_role_name: str
    partition: str
    max_annotation_chars: int = MAX_CASE_ANNOTATION_CHARS

    @classmethod
    def from_env(cls) -> Settings:
        partition = os.environ.get("MEMBER_ROLE_PARTITION", "aws")
        support_api_region(partition)
        return cls(
            raw_bucket=os.environ["RAW_BUCKET"],
            processed_bucket=os.environ["PROCESSED_BUCKET"],
            summary_queue_url=os.environ["SUMMARY_QUEUE_URL"],
            failure_queue_url=os.environ["FAILURE_QUEUE_URL"],
            member_role_name=os.environ["MEMBER_ROLE_NAME"],
            partition=partition,
        )


@dataclass(frozen=True, slots=True)
class Clients:
    s3: S3Client
    sqs: SQSClient
    sts: STSClient


@cache
def clients() -> Clients:
    return Clients(
        s3=boto3.client("s3", config=client_config()),
        sqs=boto3.client("sqs", config=client_config()),
        sts=boto3.client("sts", config=client_config(max_attempts=1)),
    )


def now() -> datetime:
    return datetime.now(UTC)


@tracer.capture_method(capture_response=False)
def annotate_case(
    key: dict[str, str], invocation: str, ref: AnnotationRef, settings: Settings, aws: Clients
) -> dict[str, Any]:
    """Store one case's complete conversation and hand it on. Idempotent per case and run.

    Errors are re-raised as `RecordFailedError` without their message or cause, so neither the
    tracer's exception log nor the batch report can carry AWS error text.
    """
    try:
        return _annotate_case(key, invocation, ref, settings, aws)
    except Exception as exc:
        raise RecordFailedError.from_exception(exc) from None


def _annotate_case(
    key: dict[str, str], invocation: str, ref: AnnotationRef, settings: Settings, aws: Clients
) -> dict[str, Any]:
    result: dict[str, Any] = {"invocation": invocation, **key}
    if get_existing_cases_batch(
        aws.s3, settings.processed_bucket, ref.account_id, [ref.display_id]
    ):
        return {**result, "outcome": "alreadyProcessed"}
    member = MemberSupport(
        role_name=settings.member_role_name,
        partition=settings.partition,
        sts_client=aws.sts,
        client_factory=support_client,
    )
    try:
        support = member.open(ref.account_id, safe_session_name("case-annotation", ref.run_id))
        communications = list_communications(support, ref.account_id, ref.case_id)
    except SupportAccessSkippedError as exc:
        return {**result, "outcome": f"skipped{exc.reason}"}
    except CaseNotFoundError:
        return {**result, "outcome": "notFound"}

    retrieved_at = now()
    document = build_conversation_document(
        account_id=ref.account_id,
        case_id=ref.case_id,
        display_id=ref.display_id,
        run_id=ref.run_id,
        retrieved_at=retrieved_at,
        communications=communications,
    )
    written = write_json(
        aws.s3,
        settings.raw_bucket,
        ref.communications_key,
        document,
        max_bytes=MAX_CONVERSATION_BYTES,
    )
    count = int(document["communicationCount"])
    result |= {"communicationCount": count}
    try:
        chars = check_annotation_size(
            render_case_annotation(document["communications"]), settings.max_annotation_chars
        )
    except ConversationTooLargeError as exc:
        send_messages(
            aws.sqs,
            settings.failure_queue_url,
            [
                {
                    "failureType": OVERSIZED_FAILURE_TYPE,
                    "stage": "annotation",
                    "accountId": ref.account_id,
                    "caseId": ref.case_id,
                    "displayId": ref.display_id,
                    "runId": ref.run_id,
                    "rawKey": ref.raw_key,
                    "communicationsKey": ref.communications_key,
                    "communicationsVersionId": written.version_id,
                    "annotationChars": exc.chars,
                    "limitChars": exc.limit,
                    "failedAt": utc_timestamp(retrieved_at),
                }
            ],
        )
        return {**result, "outcome": "oversized", "annotationChars": exc.chars}
    summary = SummaryRef(
        account_id=ref.account_id,
        case_id=ref.case_id,
        display_id=ref.display_id,
        run_id=ref.run_id,
        communications_version_id=written.version_id,
        communication_count=count,
    )
    send_messages(aws.sqs, settings.summary_queue_url, [summary.to_message()])
    return {**result, "outcome": "annotated", "annotationChars": chars}


def record_outcome(result: dict[str, Any], log_fields: dict[str, Any]) -> None:
    outcome = str(result["outcome"])
    metrics.add_metric(name=_OUTCOME_METRICS[outcome], unit=MetricUnit.Count, value=1)
    fields = {
        **log_fields,
        "outcome": outcome,
        "communications": result.get("communicationCount"),
        "annotation_chars": result.get("annotationChars"),
    }
    if outcome == "annotated":
        metrics.add_metric(
            name="CommunicationsRetrieved",
            unit=MetricUnit.Count,
            value=int(result["communicationCount"]),
        )
        logger.info("Case conversation stored and queued for summary", extra=fields)
    elif outcome == "oversized":
        logger.error(
            "Case conversation exceeds the summary input limit; stored and reported as failed",
            extra=fields,
        )
    else:
        logger.warning("Case not annotated", extra=fields)


def handle_record(record: SQSRecord, settings: Settings, aws: Clients) -> None:
    log_fields: dict[str, Any] = {"message_id": record.message_id}
    try:
        ref = AnnotationRef.parse(parse_message_body(record.raw_event))
    except (InvalidMessageError, InvalidReferenceError) as exc:
        metrics.add_metric(name="InvalidMessages", unit=MetricUnit.Count, value=1)
        metrics.add_metric(name="CaseAnnotationFailures", unit=MetricUnit.Count, value=1)
        logger.error("Annotation message is malformed", extra={**log_fields, "reason": str(exc)})
        raise RecordFailedError(type(exc).__name__) from None
    log_fields |= {
        "account_id": ref.account_id,
        "display_id": ref.display_id,
        "run_id": ref.run_id,
    }
    try:
        outcome = run_idempotent(
            annotate_case,
            ref.idempotency_key(),
            config=IDEMPOTENCY_CONFIG,
            ref=ref,
            settings=settings,
            aws=aws,
        )
    except Exception as exc:
        failure = RecordFailedError.from_exception(exc)
        metrics.add_metric(name="CaseAnnotationFailures", unit=MetricUnit.Count, value=1)
        logger.error("Case annotation failed", extra={**log_fields, **failure.log_fields()})
        raise failure from None
    if outcome.duplicate or outcome.result is None:
        metrics.add_metric(name="DuplicateAnnotationMessages", unit=MetricUnit.Count, value=1)
        logger.info(
            "Duplicate annotation message skipped",
            extra={**log_fields, "in_progress": outcome.in_progress},
        )
        return
    record_outcome(outcome.result, log_fields)


@logger.inject_lambda_context(log_event=False, clear_state=True)
@tracer.capture_lambda_handler(capture_response=False)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> PartialItemFailureResponse:
    """Store and hand on each referenced case conversation; report failed records only."""
    IDEMPOTENCY_CONFIG.register_lambda_context(context)
    settings = Settings.from_env()
    aws = clients()
    return process_sqs_batch(event, context, lambda record: handle_record(record, settings, aws))

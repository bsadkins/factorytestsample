"""Case Summary: the first CaseAnalysis workflow stage.

Reads the case's complete conversation from the raw bucket, asks the configured Bedrock model
for a 600-650 character, 4-6 sentence plain-prose summary and stores the validated summary as
the stage checkpoint. A summary that fails validation receives one corrective follow-up; if the
correction also fails the stage fails with `SummaryValidationError`. Summaries are never padded
or truncated to pass, and a conversation too large for the prompt fails with
`ConversationTooLargeError` rather than being summarised in part.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any, Final

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from common.analysis import (
    STAGE_SUMMARY,
    AnalysisClients,
    AnalysisSettings,
    IncompleteAnalysisError,
    StageSupersededError,
    SummaryValidationError,
    invocation_deadline,
    invoke_stage_model,
    is_processed,
    log_fields,
    parse_workflow_input,
    stage_result,
    text_digest,
)
from common.aws import client_config
from common.bedrock import Message, bedrock_runtime_client
from common.case_refs import SummaryRef
from common.checkpoints import Checkpoint, Expectation, load_checkpoint, save_checkpoint
from common.conversation import (
    MAX_CONVERSATION_BYTES,
    ConversationTooLargeError,
    check_annotation_size,
    render_case_annotation,
    utc_timestamp,
)
from common.s3_json import (
    S3JsonDecodeError,
    S3ObjectNotFoundError,
    S3ObjectTooLargeError,
    read_json_object,
)
from common.summary import check_summary, describe_issues
from common.templates import prompt_version, render_summary_correction, render_summary_prompt
from common.tracing import withhold_exception_messages

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext
    from types_boto3_s3.client import S3Client

logger = Logger()
tracer = Tracer()
metrics = Metrics()
withhold_exception_messages()

MAX_SUMMARY_CORRECTIONS: Final = 1


@cache
def clients() -> AnalysisClients:
    return AnalysisClients(
        s3=boto3.client("s3", config=client_config()), bedrock=bedrock_runtime_client()
    )


def now() -> datetime:
    return datetime.now(UTC)


def load_case_annotation(s3: S3Client, settings: AnalysisSettings, ref: SummaryRef) -> str:
    """Render the stored conversation version named by `ref` as the prompt's `case_annotation`."""
    try:
        document = read_json_object(
            s3,
            settings.raw_bucket,
            ref.communications_key,
            max_bytes=MAX_CONVERSATION_BYTES,
            version_id=ref.communications_version_id,
        )
    except S3ObjectNotFoundError:
        raise IncompleteAnalysisError("ConversationMissing") from None
    except (S3JsonDecodeError, S3ObjectTooLargeError):
        raise IncompleteAnalysisError("ConversationUnreadable") from None
    identity = (document.get("accountId"), document.get("caseId"), document.get("displayId"))
    communications = document.get("communications")
    if identity != (ref.account_id, ref.case_id, ref.display_id) or not isinstance(
        communications, list
    ):
        raise IncompleteAnalysisError("ConversationMismatch")
    return render_case_annotation(communications)


@tracer.capture_method(capture_response=False)
def summarise_case(
    event: object, context: LambdaContext, settings: AnalysisSettings, aws: AnalysisClients
) -> dict[str, Any]:
    """Produce or reuse the validated summary checkpoint for the case in `event["case"]`."""
    ref = parse_workflow_input(event)
    fields = {**log_fields(ref), "stage": STAGE_SUMMARY}

    def already_processed() -> dict[str, Any]:
        metrics.add_metric(name="StagesSkippedAlreadyProcessed", unit=MetricUnit.Count, value=1)
        logger.info("Case already processed; summary skipped", extra=fields)
        return stage_result(STAGE_SUMMARY, "alreadyProcessed", ref)

    if is_processed(aws.s3, settings.processed_bucket, ref):
        return already_processed()
    annotation = load_case_annotation(aws.s3, settings, ref)
    try:
        check_annotation_size(annotation)
    except ConversationTooLargeError as exc:
        metrics.add_metric(name="OversizedConversations", unit=MetricUnit.Count, value=1)
        logger.error(
            "Case conversation exceeds the summary input limit; not summarised",
            extra={**fields, "annotation_chars": exc.chars, "limit_chars": exc.limit},
        )
        raise
    version = prompt_version()
    expected = Expectation(STAGE_SUMMARY, ref, settings.model_id, version, text_digest(annotation))
    deadline = invocation_deadline(context)

    def durable_result() -> dict[str, Any] | None:
        if is_processed(aws.s3, settings.processed_bucket, ref):
            return already_processed()
        existing = load_checkpoint(aws.s3, settings.raw_bucket, expected)
        if existing is not None:
            metrics.add_metric(name="CheckpointsReused", unit=MetricUnit.Count, value=1)
            logger.info("Valid summary checkpoint reused", extra=fields)
            return stage_result(STAGE_SUMMARY, "reused", ref, key=existing.key)
        return None

    messages = [Message("user", render_summary_prompt(annotation))]
    issues: tuple[str, ...] = ()
    for attempt in range(MAX_SUMMARY_CORRECTIONS + 1):
        found = durable_result()
        if found is not None:
            return found
        if attempt:
            metrics.add_metric(name="SummaryCorrections", unit=MetricUnit.Count, value=1)
        try:
            reply = invoke_stage_model(
                aws,
                settings,
                messages,
                metrics=metrics,
                deadline=deadline,
                durable_result=durable_result,
            )
        except StageSupersededError as superseded:
            return superseded.result
        check = check_summary(reply.text)
        if check.ok:
            checkpoint = Checkpoint(
                stage=STAGE_SUMMARY,
                account_id=ref.account_id,
                case_id=ref.case_id,
                display_id=ref.display_id,
                model_id=settings.model_id,
                prompt_version=version,
                input_digest=text_digest(annotation),
                completed_at=utc_timestamp(now()),
                output={"Case_Summary": check.text},
            )
            stored = save_checkpoint(aws.s3, settings.raw_bucket, checkpoint, ref)
            metrics.add_metric(name="SummariesGenerated", unit=MetricUnit.Count, value=1)
            logger.info(
                "Summary stored",
                extra={
                    **fields,
                    "attempts": attempt + 1,
                    "summary_chars": check.length,
                    "sentences": check.sentences,
                },
            )
            return stage_result(STAGE_SUMMARY, "completed", ref, key=stored.key)
        issues = check.issues
        metrics.add_metric(name="InvalidSummaries", unit=MetricUnit.Count, value=1)
        logger.warning(
            "Summary failed validation",
            extra={
                **fields,
                "attempt": attempt + 1,
                "issues": list(issues),
                "summary_chars": check.length,
                "sentences": check.sentences,
                "stop_reason": reply.stop_reason,
            },
        )
        messages += [
            Message("assistant", reply.text),
            Message("user", render_summary_correction(describe_issues(issues))),
        ]
    metrics.add_metric(name="SummaryValidationFailures", unit=MetricUnit.Count, value=1)
    logger.error(
        "Summary still invalid after the correction; stage failed",
        extra={**fields, "issues": list(issues)},
    )
    raise SummaryValidationError(issues)


@logger.inject_lambda_context(log_event=False, clear_state=True)
@tracer.capture_lambda_handler(capture_response=False)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Summary stage task: returns `{stage, status, accountId, displayId, checkpointKey}`."""
    return summarise_case(event, context, AnalysisSettings.from_env(), clients())

"""RCA and lifecycle classification stages, shared by their two focused stage functions.

A classifier reads the validated summary checkpoint, asks the model for one JSON object with the
taxonomy's category and reason keys, and validates it against the shared taxonomy. An invalid
reply receives exactly one corrective follow-up; if that also fails, the stage records the
explicit `Unclassified` fallback with a short explanation instead of retrying further.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger
from aws_lambda_powertools.metrics import MetricUnit

from common.analysis import (
    STAGE_SUMMARY,
    AnalysisClients,
    AnalysisSettings,
    IncompleteAnalysisError,
    invocation_deadline,
    invoke_stage_model,
    is_processed,
    log_fields,
    parse_workflow_input,
    stage_result,
    text_digest,
)
from common.bedrock import Message
from common.checkpoints import (
    FALLBACK_FIELD,
    Checkpoint,
    Expectation,
    load_checkpoint,
    save_checkpoint,
)
from common.classification import (
    Classification,
    ClassificationError,
    parse_classification,
    unclassified,
)
from common.conversation import utc_timestamp
from common.templates import (
    prompt_version,
    render_classification_correction,
    render_classification_prompt,
)

if TYPE_CHECKING:
    from datetime import datetime

    from aws_lambda_powertools import Metrics
    from aws_lambda_powertools.utilities.typing import LambdaContext

    from common.taxonomy import Taxonomy

logger = Logger(child=True)

MAX_CLASSIFICATION_CORRECTIONS: Final = 1


def run_classification_stage(  # noqa: PLR0913
    event: object,
    context: LambdaContext,
    *,
    stage: str,
    taxonomy: Taxonomy,
    settings: AnalysisSettings,
    aws: AnalysisClients,
    metrics: Metrics,
    now: Callable[[], datetime],
) -> dict[str, Any]:
    """Classify one case's summary with `taxonomy` and store the result as the stage checkpoint."""
    ref = parse_workflow_input(event)
    fields = {**log_fields(ref), "stage": stage}

    def already_processed() -> dict[str, Any]:
        metrics.add_metric(name="StagesSkippedAlreadyProcessed", unit=MetricUnit.Count, value=1)
        logger.info("Case already processed; classification skipped", extra=fields)
        return stage_result(stage, "alreadyProcessed", ref)

    if is_processed(aws.s3, settings.processed_bucket, ref):
        return already_processed()
    version = prompt_version()
    summary_checkpoint = load_checkpoint(
        aws.s3,
        settings.raw_bucket,
        Expectation(STAGE_SUMMARY, ref, settings.model_id, version, None),
    )
    if summary_checkpoint is None:
        raise IncompleteAnalysisError("SummaryCheckpointMissing")
    summary = str(summary_checkpoint.output["Case_Summary"])
    expected = Expectation(stage, ref, settings.model_id, version, text_digest(summary))
    deadline = invocation_deadline(context)
    messages = [Message("user", render_classification_prompt(taxonomy, summary))]
    classification: Classification | None = None
    failure_code = "invalid"
    for attempt in range(MAX_CLASSIFICATION_CORRECTIONS + 1):
        if is_processed(aws.s3, settings.processed_bucket, ref):
            return already_processed()
        existing = load_checkpoint(aws.s3, settings.raw_bucket, expected)
        if existing is not None:
            metrics.add_metric(name="CheckpointsReused", unit=MetricUnit.Count, value=1)
            logger.info("Valid classification checkpoint reused", extra=fields)
            return stage_result(stage, "reused", ref, key=existing.key)
        if attempt:
            metrics.add_metric(name="ClassificationCorrections", unit=MetricUnit.Count, value=1)
        reply = invoke_stage_model(aws, settings, messages, metrics=metrics, deadline=deadline)
        try:
            classification = parse_classification(reply.text, taxonomy)
            break
        except ClassificationError as exc:
            failure_code = exc.code
            metrics.add_metric(name="InvalidClassifications", unit=MetricUnit.Count, value=1)
            logger.warning(
                "Classification reply failed validation",
                extra={**fields, "attempt": attempt + 1, "reason": exc.code},
            )
            messages += [
                Message("assistant", reply.text),
                Message("user", render_classification_correction(taxonomy, exc.correction_hint)),
            ]
    if classification is None:
        classification = unclassified(taxonomy, failure_code)
        metrics.add_metric(name="ClassificationFallbacks", unit=MetricUnit.Count, value=1)
        logger.warning(
            "Classification recorded as Unclassified after one correction",
            extra={**fields, "reason": failure_code},
        )
    checkpoint = Checkpoint(
        stage=stage,
        account_id=ref.account_id,
        case_id=ref.case_id,
        display_id=ref.display_id,
        model_id=settings.model_id,
        prompt_version=version,
        input_digest=expected.input_digest or "",
        completed_at=utc_timestamp(now()),
        output={**classification.as_record(taxonomy), FALLBACK_FIELD: classification.fallback},
    )
    stored = save_checkpoint(aws.s3, settings.raw_bucket, checkpoint, ref)
    metrics.add_metric(name="CasesClassified", unit=MetricUnit.Count, value=1)
    logger.info(
        "Classification stored",
        extra={
            **fields,
            "fallback": classification.fallback,
            "reused_concurrent": stored != checkpoint,
        },
    )
    return stage_result(stage, "completed", ref, key=stored.key)

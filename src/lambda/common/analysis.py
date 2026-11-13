"""Contract and helpers shared by the CaseAnalysis workflow stage functions.

The Standard workflow runs Summary -> RCA -> Lifecycle -> UpdateCaseMetadata for one case.
Every stage receives the workflow state, reads the case reference from its `case` field (the
summary-queue reference written by Case Annotation) and returns only identifiers, a status and S3
keys. Case content, summaries and classifications live in S3 stage checkpoints, never in
workflow state, logs or metrics.

Before every Bedrock invocation a stage confirms that no processed record exists and that no
valid checkpoint for the stage has appeared since it started, so a committed case is never sent
to Bedrock again and a completed stage is reused rather than repeated.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from aws_lambda_powertools.metrics import MetricUnit

from common.bedrock import (
    MAX_MAX_TOKENS,
    MIN_MAX_TOKENS,
    BedrockError,
    BedrockResult,
    Message,
    invoke_bedrock,
    validate_model_id,
)
from common.case_refs import InvalidReferenceError, SummaryRef
from common.s3_json import object_exists

if TYPE_CHECKING:
    from aws_lambda_powertools import Metrics
    from aws_lambda_powertools.utilities.typing import LambdaContext
    from types_boto3_bedrock_runtime.client import BedrockRuntimeClient
    from types_boto3_s3.client import S3Client

Stage = Literal["summary", "rca", "lifecycle"]
StageStatus = Literal["completed", "reused", "alreadyProcessed"]

STAGE_SUMMARY: Final = "summary"
STAGE_RCA: Final = "rca"
STAGE_LIFECYCLE: Final = "lifecycle"
STAGE_UPDATE: Final = "update"
WORKFLOW_CASE_FIELD: Final = "case"
# Time kept back from the Lambda deadline so a retry delay never runs into the function timeout.
DEADLINE_MARGIN_SECONDS: Final = 15.0


class AnalysisError(Exception):
    """Base class for stage failures. Messages carry stable codes only, never case content.

    The class name is the Lambda error type that workflow Retry and Catch rules match.
    """

    def __init__(self, code: str) -> None:
        super().__init__(f"{type(self).__name__}: {code}")
        self.code = code


class InvalidWorkflowInputError(AnalysisError):
    """The workflow state does not contain a valid case reference."""


class IncompleteAnalysisError(AnalysisError):
    """A required input (raw metadata or an earlier stage's checkpoint) is missing or invalid."""


class SummaryValidationError(AnalysisError):
    """The summary still failed validation after the bounded correction. Carries issue codes."""

    def __init__(self, issues: Sequence[str]) -> None:
        super().__init__(",".join(issues) or "invalid")
        self.issues = tuple(issues)


@dataclass(frozen=True, slots=True)
class AnalysisSettings:
    """Stage configuration from the function environment."""

    raw_bucket: str
    processed_bucket: str
    model_id: str
    max_tokens: int

    @classmethod
    def from_env(cls) -> AnalysisSettings:
        max_tokens = int(os.environ["BEDROCK_MAX_TOKENS"])
        if not MIN_MAX_TOKENS <= max_tokens <= MAX_MAX_TOKENS:
            raise ValueError(
                f"BEDROCK_MAX_TOKENS must be between {MIN_MAX_TOKENS} and {MAX_MAX_TOKENS}"
            )
        return cls(
            raw_bucket=os.environ["RAW_BUCKET"],
            processed_bucket=os.environ["PROCESSED_BUCKET"],
            model_id=validate_model_id(os.environ["BEDROCK_MODEL_ID"]),
            max_tokens=max_tokens,
        )


@dataclass(frozen=True, slots=True)
class AnalysisClients:
    """AWS clients used by the stage functions."""

    s3: S3Client
    bedrock: BedrockRuntimeClient


def parse_workflow_input(event: object) -> SummaryRef:
    """Return the case reference in `event["case"]`; other workflow state keys are ignored."""
    if not isinstance(event, Mapping) or not isinstance(event.get(WORKFLOW_CASE_FIELD), Mapping):
        raise InvalidWorkflowInputError("MissingCaseReference")
    try:
        return SummaryRef.parse(event[WORKFLOW_CASE_FIELD])
    except InvalidReferenceError:
        raise InvalidWorkflowInputError("InvalidCaseReference") from None


def is_processed(s3: S3Client, processed_bucket: str, ref: SummaryRef) -> bool:
    """True when the case already has a committed processed record."""
    return object_exists(s3, processed_bucket, ref.raw_key)


def text_digest(text: str) -> str:
    """`sha256:<hex>` of UTF-8 `text`; binds a checkpoint to the exact input it was made from."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def invocation_deadline(context: LambdaContext) -> float:
    """A `time.monotonic()` deadline for Bedrock retries, inside the function timeout."""
    remaining = context.get_remaining_time_in_millis() / 1000
    return time.monotonic() + max(remaining - DEADLINE_MARGIN_SECONDS, 0.0)


def sleep(seconds: float) -> None:
    """Backoff sleep used for Bedrock retries."""
    time.sleep(seconds)


def invoke_stage_model(
    aws: AnalysisClients,
    settings: AnalysisSettings,
    messages: Sequence[Message],
    *,
    metrics: Metrics,
    deadline: float | None,
) -> BedrockResult:
    """Invoke the configured model and publish latency, token and throttle metrics."""
    try:
        result = invoke_bedrock(
            aws.bedrock,
            settings.model_id,
            messages,
            max_tokens=settings.max_tokens,
            deadline=deadline,
            sleep=sleep,
        )
    except BedrockError as exc:
        metrics.add_metric(name="BedrockInvocationFailures", unit=MetricUnit.Count, value=1)
        if exc.throttle_count:
            metrics.add_metric(
                name="BedrockThrottles", unit=MetricUnit.Count, value=exc.throttle_count
            )
        raise
    metrics.add_metric(name="BedrockInvocations", unit=MetricUnit.Count, value=1)
    metrics.add_metric(name="BedrockLatency", unit=MetricUnit.Milliseconds, value=result.latency_ms)
    metrics.add_metric(name="BedrockInputTokens", unit=MetricUnit.Count, value=result.input_tokens)
    metrics.add_metric(
        name="BedrockOutputTokens", unit=MetricUnit.Count, value=result.output_tokens
    )
    if result.throttle_count:
        metrics.add_metric(
            name="BedrockThrottles", unit=MetricUnit.Count, value=result.throttle_count
        )
    return result


def stage_result(
    stage: str, status: str, ref: SummaryRef, *, key: str | None = None
) -> dict[str, Any]:
    """The state a stage returns to the workflow: identifiers, status and an S3 key only."""
    result: dict[str, Any] = {
        "stage": stage,
        "status": status,
        "accountId": ref.account_id,
        "displayId": ref.display_id,
    }
    if status == "alreadyProcessed":
        result["processedKey"] = ref.raw_key
    elif key is not None:
        result["checkpointKey" if stage != STAGE_UPDATE else "processedKey"] = key
    return result


def log_fields(ref: SummaryRef) -> dict[str, Any]:
    """Safe identifiers for stage log records."""
    return {"account_id": ref.account_id, "display_id": ref.display_id, "run_id": ref.run_id}

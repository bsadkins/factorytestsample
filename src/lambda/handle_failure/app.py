"""Handle Failure: record a case-analysis attempt whose task retries are exhausted.

Every CaseAnalysis task catches its errors into this handler. It sends one failure message with
safe references (case identifiers, S3 keys, execution name, failed stage, error type and a stable
error code) to the terminal workflow failure queue, then the workflow ends FAILED so the starter
can recover the case on a later run or after an operator redrive. It never writes processed
output and never logs or forwards error text, which can echo case content.

If the message cannot be delivered the handler raises `FailureDeliveryError`; the workflow retries
it and, when retries run out, fails with a distinct error, so a lost failure record is visible in
Step Functions metrics rather than silently acknowledged.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
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
    InvalidWorkflowInputError,
    parse_workflow_input,
)
from common.aws import client_config
from common.conversation import utc_timestamp
from common.failures import (
    CONVERSATION_TOO_LARGE,
    FAILURE_SCHEMA_VERSION,
    INVALID_WORKFLOW_INPUT,
    WORKFLOW_TASK_FAILED,
    failure_message,
)
from common.sqs import send_messages
from common.workflow import parse_execution_name

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext
    from types_boto3_sqs.client import SQSClient

logger = Logger()
tracer = Tracer()
metrics = Metrics()

# Errors whose messages are stable, content-free codes (see common.analysis and common.bedrock).
SAFE_CODE_ERRORS: Final = frozenset(
    {
        "BedrockThrottlingError",
        "BedrockTransientError",
        "BedrockInvocationError",
        "BedrockResponseError",
        "ConversationTooLargeError",
        "IncompleteAnalysisError",
        "InvalidWorkflowInputError",
        "PromptTooLargeError",
        "SummaryValidationError",
        "UnsupportedModelError",
    }
)
# A conversation that is too large fails identically until its content changes, so it is not
# retried automatically; everything else is retried by the next daily run.
NON_RETRYABLE_ERRORS: Final = frozenset({"ConversationTooLargeError", "InvalidWorkflowInputError"})
_ERROR_TYPE: Final = re.compile(r"^[A-Za-z0-9_.]{1,128}$")
_CODE: Final = re.compile(r"(?:^\w+: ([A-Za-z0-9_,]{1,200})$)|(?:\(([A-Za-z0-9_.]{1,100})\)$)")
_STAGE_ORDER: Final = (STAGE_SUMMARY, STAGE_RCA, STAGE_LIFECYCLE)


class FailureDeliveryError(Exception):
    """The failure message could not be sent to the workflow failure queue."""


@dataclass(frozen=True, slots=True)
class Settings:
    failure_queue_url: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(failure_queue_url=os.environ["FAILURE_QUEUE_URL"])


@dataclass(frozen=True, slots=True)
class Clients:
    sqs: SQSClient


@cache
def clients() -> Clients:
    return Clients(sqs=boto3.client("sqs", config=client_config()))


def now() -> datetime:
    return datetime.now(UTC)


def failed_stage(state: Mapping[str, Any]) -> str:
    """The first stage without a result in the state; tasks run strictly in order."""
    for stage in _STAGE_ORDER:
        if not isinstance(state.get(stage), Mapping):
            return stage
    return STAGE_UPDATE


def error_details(error: object) -> tuple[str, str | None]:
    """Return `(errorType, errorCode)` from a Catch result without keeping any error text."""
    if not isinstance(error, Mapping):
        return "Unknown", None
    error_type = error.get("Error")
    if not isinstance(error_type, str) or not _ERROR_TYPE.match(error_type):
        return "Unknown", None
    if error_type not in SAFE_CODE_ERRORS:
        return error_type, None
    try:
        cause = json.loads(str(error.get("Cause") or "{}"))
    except ValueError:
        return error_type, None
    message = cause.get("errorMessage") if isinstance(cause, dict) else None
    match = _CODE.search(message) if isinstance(message, str) else None
    if match is None:
        return error_type, None
    return error_type, match.group(1) or match.group(2)


def _execution_fields(state: Mapping[str, Any]) -> dict[str, Any]:
    attempt = state.get("attempt")
    if not isinstance(attempt, Mapping):
        return {}
    name = attempt.get("executionName")
    if not isinstance(name, str) or parse_execution_name(name) is None:
        return {}
    number = attempt.get("number")
    return {"executionName": name, "attempt": number if isinstance(number, int) else None}


def build_failure(state: Mapping[str, Any], failed_at: datetime) -> dict[str, Any]:
    """The failure-queue message for a caught workflow error. References only."""
    error_type, code = error_details(state.get("error"))
    stage = failed_stage(state)
    stamp = utc_timestamp(failed_at)
    try:
        ref = parse_workflow_input(state)
    except InvalidWorkflowInputError:
        return {
            "schemaVersion": FAILURE_SCHEMA_VERSION,
            "failureType": INVALID_WORKFLOW_INPUT,
            "stage": stage,
            "retryable": False,
            "failedAt": stamp,
            "errorType": error_type,
            **_execution_fields(state),
        }
    failure_type = (
        CONVERSATION_TOO_LARGE
        if error_type == "ConversationTooLargeError"
        else WORKFLOW_TASK_FAILED
    )
    return failure_message(
        ref,
        failure_type=failure_type,
        stage=stage,
        failed_at=stamp,
        retryable=error_type not in NON_RETRYABLE_ERRORS,
        errorType=error_type,
        errorCode=code,
        **_execution_fields(state),
    )


@tracer.capture_method(capture_response=False)
def record_failure(state: Mapping[str, Any], settings: Settings, aws: Clients) -> dict[str, Any]:
    message = build_failure(state, now())
    fields = {
        "failure_type": message["failureType"],
        "stage": message["stage"],
        "error_type": message["errorType"],
        "error_code": message.get("errorCode"),
        "account_id": message.get("accountId"),
        "display_id": message.get("displayId"),
        "run_id": message.get("runId"),
        "execution_name": message.get("executionName"),
    }
    try:
        send_messages(aws.sqs, settings.failure_queue_url, [message])
    except Exception as exc:
        metrics.add_metric(name="WorkflowFailureDeliveryErrors", unit=MetricUnit.Count, value=1)
        logger.error(
            "Workflow failure could not be recorded",
            extra={**fields, "delivery_error": type(exc).__name__},
        )
        raise FailureDeliveryError("failure message not delivered") from None
    metrics.add_metric(name="WorkflowFailuresRecorded", unit=MetricUnit.Count, value=1)
    if message["failureType"] == CONVERSATION_TOO_LARGE:
        metrics.add_metric(name="OversizedConversations", unit=MetricUnit.Count, value=1)
    logger.error("Case analysis attempt failed and was recorded", extra=fields)
    return {
        "status": "recorded",
        "failureType": message["failureType"],
        "stage": message["stage"],
        "errorType": message["errorType"],
        "retryable": message["retryable"],
    }


@logger.inject_lambda_context(log_event=False, clear_state=True)
@tracer.capture_lambda_handler(capture_response=False)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """HandleFailure task: returns `{status, failureType, stage, errorType, retryable}`."""
    del context
    return record_failure(event, Settings.from_env(), clients())

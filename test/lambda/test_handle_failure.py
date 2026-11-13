"""Handle Failure: safe, redriveable failure records for exhausted CaseAnalysis attempts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from common.case_refs import SummaryRef
from common.failures import summary_ref_from_failure
from common.workflow import execution_input
from handle_failure import app

ACCOUNT = "111122223333"
DISPLAY = "170000000001"
RUN = "20261113T000000Z"
NOW = datetime(2026, 11, 13, 2, 0, 0, tzinfo=UTC)
SENTINEL = "SENTINEL-CASE-TEXT-77ab"
REF = SummaryRef(
    account_id=ACCOUNT,
    case_id=f"case-{ACCOUNT}-2026-abc",
    display_id=DISPLAY,
    run_id=RUN,
    communications_version_id="version-1",
    communication_count=4,
)
SUMMARY_RESULT = {"stage": "summary", "status": "completed", "checkpointKey": "k"}


def lambda_error(error_type: str, message: str) -> dict[str, str]:
    """A Catch result as Step Functions records it for a failed Lambda task."""
    cause = {
        "errorMessage": message,
        "errorType": error_type,
        "requestId": "r-1",
        "stackTrace": [f'  File "/var/task/app.py", line 10, in handler: {SENTINEL}'],
    }
    return {"Error": error_type, "Cause": json.dumps(cause)}


def state(error: object, **results: Any) -> dict[str, Any]:  # noqa: ANN401
    return {**execution_input(REF, 2), **results, "error": error}


@dataclass
class Env:
    sqs: Any
    queue_url: str
    capsys: pytest.CaptureFixture[str]
    kit: Any
    stdout: str = ""

    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = app.handler(event, self.kit.context())
        return result

    def failures(self) -> list[dict[str, Any]]:
        received: list[dict[str, Any]] = self.kit.drain(self.sqs, self.queue_url)
        return received

    def metrics(self) -> dict[str, float]:
        self.stdout = self.capsys.readouterr().out
        metrics: dict[str, float] = self.kit.metrics(self.stdout)
        return metrics


@pytest.fixture
def env(
    mocked_aws: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case_collection_kit: Any,  # noqa: ANN401
) -> Env:
    sqs = boto3.client("sqs", region_name="us-east-1")
    url = sqs.create_queue(QueueName="workflow-failures")["QueueUrl"]
    monkeypatch.setenv("FAILURE_QUEUE_URL", url)
    clients = app.Clients(sqs=sqs)
    monkeypatch.setattr(app, "clients", lambda: clients)
    monkeypatch.setattr(app, "now", lambda: NOW)
    return Env(sqs, url, capsys, case_collection_kit)


@pytest.mark.parametrize(
    ("results", "error", "stage", "code"),
    [
        (
            {},
            lambda_error("BedrockThrottlingError", "Bedrock invocation of m failed (Throttling)"),
            "summary",
            "Throttling",
        ),
        (
            {"summary": SUMMARY_RESULT},
            lambda_error(
                "BedrockTransientError",
                "Bedrock invocation of m failed (ServiceUnavailableException)",
            ),
            "rca",
            "ServiceUnavailableException",
        ),
        (
            {"summary": SUMMARY_RESULT, "rca": SUMMARY_RESULT},
            lambda_error(
                "IncompleteAnalysisError", "IncompleteAnalysisError: SummaryCheckpointMissing"
            ),
            "lifecycle",
            "SummaryCheckpointMissing",
        ),
        (
            {"summary": SUMMARY_RESULT, "rca": SUMMARY_RESULT, "lifecycle": SUMMARY_RESULT},
            lambda_error("ClientError", f"An error occurred (AccessDenied): {SENTINEL}"),
            "update",
            None,
        ),
    ],
    ids=["summary", "rca", "lifecycle", "update"],
)
def test_failure_at_each_stage_is_recorded_with_safe_references(
    env: Env, results: dict[str, Any], error: dict[str, str], stage: str, code: str | None
) -> None:
    result = env.handle(state(error, **results))

    failures = env.failures()
    assert len(failures) == 1
    failure = failures[0]
    expected = {
        "schemaVersion": 1,
        "failureType": "WorkflowTaskFailed",
        "stage": stage,
        "retryable": True,
        "failedAt": "2026-11-13T02:00:00Z",
        **REF.to_message(),
        "errorType": error["Error"],
        "executionName": f"{ACCOUNT}-{DISPLAY}-{RUN}-a2",
        "attempt": 2,
    }
    if code is not None:
        expected["errorCode"] = code
    assert failure == expected
    assert result == {
        "status": "recorded",
        "failureType": "WorkflowTaskFailed",
        "stage": stage,
        "errorType": error["Error"],
        "retryable": True,
    }
    # The record can be redriven to the summary queue as-is.
    assert summary_ref_from_failure(failure) == REF
    assert env.metrics() == {"WorkflowFailuresRecorded": 1}
    output = env.stdout + env.kit.logs()
    assert SENTINEL not in output + json.dumps(failure)
    assert "Case analysis attempt failed and was recorded" in output


def test_oversized_conversation_is_terminal_and_not_automatically_retryable(env: Env) -> None:
    error = lambda_error(
        "ConversationTooLargeError", "Case conversation has 400000 characters; the limit is 300000"
    )

    result = env.handle(state(error))

    [failure] = env.failures()
    assert failure["failureType"] == "ConversationTooLarge"
    assert failure["stage"] == "summary"
    assert failure["retryable"] is False
    assert "errorCode" not in failure
    assert result["retryable"] is False
    assert env.metrics() == {"WorkflowFailuresRecorded": 1, "OversizedConversations": 1}


@pytest.mark.parametrize(
    "error",
    [
        {"Error": "States.Timeout", "Cause": f"Task timed out {SENTINEL}"},
        {"Error": "Lambda.TooManyRequestsException", "Cause": SENTINEL},
        {"Error": "BedrockResponseError", "Cause": "not json"},
        {"Error": "SummaryValidationError", "Cause": json.dumps({"errorMessage": SENTINEL})},
        {"Error": "has spaces and text", "Cause": SENTINEL},
        "not-a-mapping",
    ],
    ids=["timeout", "lambda-throttle", "bad-cause", "unsafe-message", "bad-type", "no-error"],
)
def test_error_text_is_never_forwarded(env: Env, error: object) -> None:
    env.handle(state(error))

    [failure] = env.failures()
    assert SENTINEL not in json.dumps(failure)
    assert "errorCode" not in failure
    assert failure["retryable"] is True


def test_invalid_workflow_input_is_recorded_without_a_case_reference(env: Env) -> None:
    error = lambda_error("InvalidWorkflowInputError", "InvalidWorkflowInputError: missing_case")

    result = env.handle({"error": error, "attempt": "not-a-mapping"})

    [failure] = env.failures()
    assert failure == {
        "schemaVersion": 1,
        "failureType": "InvalidWorkflowInput",
        "stage": "summary",
        "retryable": False,
        "failedAt": "2026-11-13T02:00:00Z",
        "errorType": "InvalidWorkflowInputError",
    }
    assert result["retryable"] is False


def test_unrecognised_execution_name_is_not_copied(env: Env) -> None:
    event = state(lambda_error("BedrockThrottlingError", "x (Throttling)"))
    event["attempt"] = {"executionName": SENTINEL, "number": 1}

    env.handle(event)

    [failure] = env.failures()
    assert "executionName" not in failure


def test_delivery_failure_raises_so_the_workflow_retries_and_fails_visibly(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*_: Any, **__: Any) -> None:  # noqa: ANN401
        raise ClientError(
            {"Error": {"Code": "ServiceUnavailable", "Message": SENTINEL}}, "SendMessageBatch"
        )

    monkeypatch.setattr(app, "send_messages", unavailable)

    with pytest.raises(app.FailureDeliveryError):
        env.handle(state(lambda_error("BedrockThrottlingError", "x (Throttling)")))

    assert env.failures() == []
    assert env.metrics() == {"WorkflowFailureDeliveryErrors": 1}
    output = env.stdout + env.kit.logs()
    assert "Workflow failure could not be recorded" in output
    assert SENTINEL not in output


def test_missing_queue_is_a_delivery_failure(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAILURE_QUEUE_URL", env.queue_url + "-missing")

    with pytest.raises(app.FailureDeliveryError):
        env.handle(state(lambda_error("BedrockThrottlingError", "x (Throttling)")))

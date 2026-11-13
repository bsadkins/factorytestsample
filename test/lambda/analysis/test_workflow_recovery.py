"""CaseAnalysis fault injection and replay: a failure at any stage is recorded safely without a
processed record, and the recovered attempt completes without repeating finished stages.

`run_workflow` drives the handlers with the same payloads and result paths as the state machine:
the Bedrock stages run through the shared Analysis Stages entry point with `{stage, case}`,
results land in `$.summary`, `$.rca`, `$.lifecycle` and `$.update`, and a caught error goes to
Handle Failure with the state plus `$.error`.
"""

from __future__ import annotations

import json
import traceback
from collections.abc import Callable
from typing import Any

import boto3
import pytest
from analysis_kit import AnalysisEnv
from analysis_stages import app as stages_app
from botocore.exceptions import ClientError
from common.analysis import InvalidWorkflowInputError
from common.conversation import MAX_CASE_ANNOTATION_CHARS
from common.failures import summary_ref_from_failure
from common.s3_json import WriteResult
from common.workflow import execution_input
from handle_failure import app as failure_app
from update_case_metadata import app as update_app

RCA_REPLY = '{"RCA_Category": "Customer Release", "RCA_Reason": "The release changed the port."}'
LIFECYCLE_REPLY = '{"Lifecycle_Category": "CICD", "Lifecycle_Reason": "A pipeline check helps."}'
STAGES = ("summary", "rca", "lifecycle", "update")
REPLIES = {"rca": RCA_REPLY, "lifecycle": LIFECYCLE_REPLY}


def catch_result(exc: BaseException) -> dict[str, str]:
    """The `$.error` value Step Functions records when a Lambda task raises `exc`."""
    cause = {
        "errorMessage": str(exc),
        "errorType": type(exc).__name__,
        "stackTrace": traceback.format_tb(exc.__traceback__),
    }
    return {"Error": type(exc).__name__, "Cause": json.dumps(cause)}


class Workflow:
    def __init__(self, env: AnalysisEnv, queue_url: str, sqs: Any) -> None:  # noqa: ANN401
        self.env = env
        self.queue_url = queue_url
        self.sqs = sqs

    def task(self, stage: str, state: dict[str, Any]) -> dict[str, Any]:
        context = self.env.context()
        if stage == "update":
            result: dict[str, Any] = update_app.handler({"case": state["case"]}, context)
            return result
        result = stages_app.handler({"stage": stage, "case": state["case"]}, context)
        return result

    def run(self, attempt: int) -> dict[str, Any]:
        state = execution_input(self.env.ref, attempt)
        for stage in STAGES:
            try:
                state[stage] = self.task(stage, state)
            except Exception as exc:  # mirrors a States.ALL Catch
                outcome = failure_app.handler(
                    {**state, "error": catch_result(exc)}, self.env.context()
                )
                return {"status": "FAILED", "failure": outcome, "state": state}
        return {"status": "SUCCEEDED", "state": state}

    def failures(self) -> list[dict[str, Any]]:
        received: list[dict[str, Any]] = []
        while True:
            batch = self.sqs.receive_message(QueueUrl=self.queue_url, MaxNumberOfMessages=10)
            messages = batch.get("Messages", [])
            if not messages:
                return received
            for message in messages:
                received.append(json.loads(message["Body"]))
                self.sqs.delete_message(
                    QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"]
                )


@pytest.fixture
def workflow(analysis_env: AnalysisEnv, monkeypatch: pytest.MonkeyPatch) -> Workflow:
    sqs = boto3.client("sqs", region_name="us-east-1")
    url = sqs.create_queue(QueueName="workflow-failures")["QueueUrl"]
    monkeypatch.setenv("FAILURE_QUEUE_URL", url)
    clients = failure_app.Clients(sqs=sqs)
    monkeypatch.setattr(failure_app, "clients", lambda: clients)
    return Workflow(analysis_env, url, sqs)


def throttle(env: AnalysisEnv) -> None:
    env.bedrock.error("ThrottlingException", 429, times=4)


def failing_write(*_: Any, **__: Any) -> WriteResult:  # noqa: ANN401
    raise ClientError({"Error": {"Code": "InternalError", "Message": "x"}}, "PutObject")


@pytest.mark.parametrize("failed_stage", STAGES)
def test_fault_at_each_stage_is_recorded_and_replay_reuses_completed_stages(
    workflow: Workflow,
    analysis_env: AnalysisEnv,
    monkeypatch: pytest.MonkeyPatch,
    failed_stage: str,
) -> None:
    env = analysis_env
    env.seed_case()
    before = STAGES[: STAGES.index(failed_stage)]
    replies = [REPLIES.get(s, env.valid_summary) for s in before if s != "update"]
    if replies:
        env.bedrock.reply(*replies)

    with monkeypatch.context() as patch:
        if failed_stage == "update":
            patch.setattr(update_app, "write_json", failing_write)
        else:
            throttle(env)
        first = workflow.run(attempt=1)

    assert first["status"] == "FAILED"
    assert env.processed() is None
    [failure] = workflow.failures()
    assert failure["stage"] == failed_stage
    assert failure["failureType"] == "WorkflowTaskFailed"
    assert failure["retryable"] is True
    assert failure["executionName"].endswith("-a1")
    assert failure["errorType"] == (
        "ClientError" if failed_stage == "update" else "BedrockThrottlingError"
    )
    for secret in (*env.sentinels, env.valid_summary[:40]):
        assert secret not in json.dumps(failure)
    calls_before_replay = len(env.bedrock.requests)

    # The failure message redrives to the summary queue; the starter takes attempt 2.
    assert summary_ref_from_failure(failure) == env.ref
    remaining = [s for s in STAGES[STAGES.index(failed_stage) :] if s != "update"]
    env.bedrock.reply(*[REPLIES.get(s, env.valid_summary) for s in remaining])
    second = workflow.run(attempt=2)

    assert second["status"] == "SUCCEEDED"
    statuses = [second["state"][s]["status"] for s in STAGES]
    assert statuses == ["reused"] * len(before) + ["completed"] * len(remaining) + ["committed"]
    assert len(env.bedrock.requests) == calls_before_replay + len(remaining)
    assert env.processed_versions() == 1
    assert workflow.failures() == []


def test_replay_after_success_makes_no_bedrock_calls_and_no_writes(
    workflow: Workflow, analysis_env: AnalysisEnv
) -> None:
    env = analysis_env
    env.seed_case()
    env.bedrock.reply(env.valid_summary, RCA_REPLY, LIFECYCLE_REPLY)
    assert workflow.run(attempt=1)["status"] == "SUCCEEDED"
    calls = len(env.bedrock.requests)

    replay = workflow.run(attempt=2)

    assert [replay["state"][s]["status"] for s in STAGES] == ["alreadyProcessed"] * 4
    assert len(env.bedrock.requests) == calls
    assert env.processed_versions() == 1


def test_oversized_conversation_is_a_terminal_failure_without_inference(
    workflow: Workflow, analysis_env: AnalysisEnv
) -> None:
    env = analysis_env
    env.seed_case(bodies=[env.sentinels[0] + " " + "x" * MAX_CASE_ANNOTATION_CHARS])

    result = workflow.run(attempt=1)

    assert result["status"] == "FAILED"
    assert result["failure"]["retryable"] is False
    [failure] = workflow.failures()
    assert failure["failureType"] == "ConversationTooLarge"
    assert failure["stage"] == "summary"
    assert failure["retryable"] is False
    assert env.bedrock.requests == []
    assert env.processed() is None
    assert env.sentinels[0] not in json.dumps(failure)


@pytest.mark.parametrize("event", [{}, {"stage": "update"}, {"stage": 3}, ["stage"]])
def test_analysis_stages_rejects_unknown_stages(analysis_env: AnalysisEnv, event: object) -> None:
    with pytest.raises(InvalidWorkflowInputError):
        stages_app.handler(event, analysis_env.context())  # type: ignore[arg-type]
    assert analysis_env.bedrock.requests == []


def test_analysis_stages_maps_each_stage_to_its_focused_handler() -> None:
    handlers: dict[str, Callable[..., Any]] = dict(stages_app.STAGE_HANDLERS)
    assert sorted(handlers) == ["lifecycle", "rca", "summary"]
    assert {h.__module__ for h in handlers.values()} == {
        "case_summary.app",
        "case_rca.app",
        "case_lifecycle.app",
    }

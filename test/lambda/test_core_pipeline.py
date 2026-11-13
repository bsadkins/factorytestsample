"""End-to-end Core pipeline regressions: discovery to processed record through every Core handler.

The `pipeline_kit.Pipeline` harness moves work between the real handlers the way S3
notifications, SQS event source mappings and the CaseAnalysis state machine would, over moto S3,
SQS and DynamoDB and paginated synthetic Organizations, Support and Bedrock backends. Every
expected value below comes from the synthetic organisation and conversations defined here, never
from handler output fed back into the assertions.
"""

from __future__ import annotations

import dataclasses
import io
import json
import logging
import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from case_retrieval import app as retrieval_app
from common.case_refs import SummaryRef
from common.failures import summary_ref_from_failure
from common.idempotency import persistence_layer
from common.run_markers import AccountMarker
from common.workflow import execution_input, execution_name
from pipeline_kit import (
    ACCOUNT_LISTS,
    HAIKU,
    LIFECYCLE_REPLY,
    MANAGEMENT,
    MAX_RECEIVE_COUNT,
    PROCESSED,
    RAW,
    RCA_REPLY,
    SENTINEL,
    SENTINEL_PERSON,
    TASK_RETRIES,
    WORK_QUEUES,
    FakeOrganizations,
    FakeSupport,
    OrgAccount,
    Pipeline,
    SyntheticCase,
    case_key,
    client_error,
    model_summary,
    run_day,
    run_id_for,
)

ALPHA, BETA, GAMMA, DELTA, EPSILON = (
    "111111111111",
    "222222222222",
    "333333333333",
    "444444444444",
    "555555555555",
)
ORIGINAL_FIELDS = (
    "caseId",
    "displayId",
    "subject",
    "serviceCode",
    "categoryCode",
    "severityCode",
    "submittedBy",
    "timeCreated",
)
PROCESSED_FIELDS = {
    *ORIGINAL_FIELDS,
    "Case_Retrieval_Date",
    "Case_Summary",
    "RCA_Category",
    "RCA_Reason",
    "RCA_Retrieval_Date",
    "Lifecycle_Category",
    "Lifecycle_Reason",
    "Lifecycle_Retrieval_Date",
    "Model_Id",
    "Prompt_Version",
}
LOGGER_SERVICE = "handler-test"
CORE_FUNCTIONS = {
    "AccountLookup",
    "AccountReader",
    "CaseRetrieval",
    "CaseAnnotation",
    "WorkflowStarter",
    "AnalysisStages",
    "UpdateCaseMetadata",
    "HandleFailure",
    "RunCompletion",
    "CaseCleanup",
}


def organisation() -> list[OrgAccount]:
    """Six accounts over three ListAccounts pages: four selected, one suspended, management."""
    return [
        OrgAccount(ALPHA, "alpha-prod"),
        OrgAccount(MANAGEMENT, "management"),
        OrgAccount(BETA, "beta-no-role"),
        OrgAccount(DELTA, "delta-suspended", state="SUSPENDED"),
        OrgAccount(GAMMA, "gamma-basic-support"),
        OrgAccount(EPSILON, "epsilon-dev"),
    ]


@pytest.fixture
def start() -> datetime:
    """Shortly after today's 00:00 UTC schedule. Cleanup compares against S3's real
    LastModified, so the synthetic clock starts at the real date."""
    return run_day(datetime.now(UTC)) + timedelta(minutes=5)


@pytest.fixture
def log_stream(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    stream = io.StringIO()
    for handler in logging.getLogger(LOGGER_SERVICE).handlers:
        if isinstance(handler, logging.StreamHandler):
            monkeypatch.setattr(handler, "stream", stream)
    return stream


@pytest.fixture
def make_pipeline(  # noqa: PLR0913, PLR0917 - fixtures are injected by name
    mocked_aws: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    make_context: Callable[[str], LambdaContext],
    metrics_in: Callable[[str], dict[str, float]],
    start: datetime,
    log_stream: io.StringIO,
) -> Iterator[Callable[..., Pipeline]]:
    del mocked_aws, log_stream

    def build(
        accounts: list[OrgAccount] | None = None, support: FakeSupport | None = None
    ) -> Pipeline:
        return Pipeline(
            monkeypatch=monkeypatch,
            capsys=capsys,
            make_context=make_context,
            parse_metrics=metrics_in,
            org=FakeOrganizations(organisation() if accounts is None else accounts),
            support=support or FakeSupport(),
            clock=[start],
        )

    yield build
    # The persistence layer is cached per process; later tests must not inherit this table.
    persistence_layer.cache_clear()


def standard_support(start: datetime) -> FakeSupport:
    day = run_day(start)
    support = FakeSupport()
    support.add(
        SyntheticCase(ALPHA, "1001", day - timedelta(days=3), messages=3),
        SyntheticCase(ALPHA, "1002", day - timedelta(days=40), messages=5),
        SyntheticCase(ALPHA, "1003", day - timedelta(days=2), status="opened"),
        SyntheticCase(ALPHA, "1004", day - timedelta(days=500)),
        SyntheticCase(ALPHA, "1005", day + timedelta(minutes=1)),
        SyntheticCase(GAMMA, "3001", day - timedelta(days=4)),
        SyntheticCase(EPSILON, "5001", day - timedelta(days=10), messages=1),
    )
    support.subscription_required.add(GAMMA)
    return support


@pytest.fixture
def pipeline(make_pipeline: Callable[..., Pipeline], start: datetime) -> Pipeline:
    built = make_pipeline(support=standard_support(start))
    built.sts.denied.add(BETA)
    return built


def run_key(run: str, name: str) -> str:
    return f"runs/{run}/{name}"


def assert_no_case_content(pipeline: Pipeline, log_stream: io.StringIO, *extra: object) -> None:
    """Handler results, execution inputs, logs and failure messages carry references only."""
    exposed = [*pipeline.outputs(), log_stream.getvalue(), *(json.dumps(e) for e in extra)]
    for text in exposed:
        assert SENTINEL not in text
        assert SENTINEL_PERSON not in text


def assert_enriched(pipeline: Pipeline, case: SyntheticCase) -> None:
    """One committed record with every field, from the complete conversation."""
    account, display = case.account, case.display
    record = pipeline.processed(account, display)
    assert record is not None
    assert set(record) == PROCESSED_FIELDS
    assert len(record) == 18
    original = case.details()
    assert {field: record[field] for field in ORIGINAL_FIELDS} == {
        field: original[field] for field in ORIGINAL_FIELDS
    }
    assert record["Case_Summary"] == model_summary(display)
    assert (record["RCA_Category"], record["RCA_Reason"]) == tuple(RCA_REPLY.values())
    assert (record["Lifecycle_Category"], record["Lifecycle_Reason"]) == tuple(
        LIFECYCLE_REPLY.values()
    )
    for date_field in ("Case_Retrieval_Date", "RCA_Retrieval_Date", "Lifecycle_Retrieval_Date"):
        assert record[date_field] == pipeline.now_stamp()
    assert record["Model_Id"] == HAIKU
    assert re.fullmatch(r"pv-[0-9a-f]{16}", record["Prompt_Version"])
    assert pipeline.versions(PROCESSED, case_key(account, display)) == 1

    # The complete conversation was stored and every message reached the summary prompt.
    stored = pipeline.read(RAW, case_key(account, display, "communications.json"))
    assert stored["communicationCount"] == case.messages
    assert [c["body"] for c in stored["communications"]] == case.bodies()
    [summary_call] = pipeline.bedrock.calls_for(display, "summary")
    positions = [summary_call.prompt.index(body) for body in case.bodies()]
    assert positions == sorted(positions)


def redrive(pipeline: Pipeline, failure: dict[str, object], redrive_id: str) -> None:
    """What the operator redrive does with a failure message: back to the summary queue."""
    ref = dataclasses.replace(summary_ref_from_failure(failure), redrive_id=redrive_id)
    pipeline.send("summary", ref.to_message())


def test_daily_run_collects_and_enriches_every_eligible_case(
    pipeline: Pipeline, start: datetime, log_stream: io.StringIO
) -> None:
    run = run_id_for(start)
    support = pipeline.support

    assert pipeline.discover() == {
        "runId": run,
        "accountListKey": f"account-lists/{run}.json",
        "accountsSelected": 4,
        "created": True,
    }
    assert pipeline.org.pages_served == 3
    assert pipeline.metric("AccountsListed") == 6
    assert pipeline.metric("AccountsExcludedInactive") == 1
    assert pipeline.metric("AccountsExcludedManagement") == 1
    assert pipeline.keys(ACCOUNT_LISTS) == [f"account-lists/{run}.json"]

    pipeline.settle()

    # Every selected account, and only those, was entered through the member role.
    assumed = {call["RoleArn"] for call in pipeline.sts.calls}
    assert assumed == {
        f"arn:aws:iam::{account}:role/Support-Case-Analysis-Role-e2e"
        for account in (ALPHA, BETA, GAMMA, EPSILON)
    }
    # Paged DescribeCases (3 in-window cases, 2 per page) and paged communications (3 + 5 + 1).
    assert support.calls.count((ALPHA, "DescribeCases")) == 2
    assert support.calls.count((ALPHA, "DescribeCommunications")) == 2 + 3
    assert support.calls.count((EPSILON, "DescribeCommunications")) == 1
    assert support.calls.count((GAMMA, "DescribeCases")) == 1
    assert not [call for call in support.calls if call[0] in (BETA, DELTA, MANAGEMENT)]
    assert support.operations() == {"DescribeCases", "DescribeCommunications"}
    assert set(support.partitions) == {"aws"}
    assert pipeline.metric("AccountsSkippedAccessDenied", "CaseRetrieval") == 1
    assert pipeline.metric("AccountsSkippedSubscriptionRequired", "CaseRetrieval") == 1
    assert pipeline.metric("CasesListed", "CaseRetrieval") == 4
    assert pipeline.metric("CasesExcludedUnresolved", "CaseRetrieval") == 1
    assert pipeline.metric("CasesRetrieved", "CaseRetrieval") == 3

    expected = {(ALPHA, "1001"), (ALPHA, "1002"), (EPSILON, "5001")}
    assert pipeline.keys(PROCESSED) == sorted(case_key(a, d) for a, d in expected)
    cases = {(c.account, c.display): c for cs in support.cases.values() for c in cs}
    for identity in expected:
        assert_enriched(pipeline, cases[identity])

    assert sorted((c.display, c.stage) for c in pipeline.bedrock.calls) == sorted(
        (display, stage) for _, display in expected for stage in ("summary", "rca", "lifecycle")
    )
    assert {c.model_id for c in pipeline.bedrock.calls} == {HAIKU}
    assert {e.status for e in pipeline.sfn.executions.values()} == {"SUCCEEDED"}
    assert sorted(name[-3:] for name in pipeline.sfn.executions) == ["-a1"] * 3
    for queue in WORK_QUEUES:
        assert pipeline.depth(queue) == 0
        assert pipeline.depth(queue, dead_letter=True) == 0
    assert pipeline.drain("workflowFailures") == []

    assert pipeline.complete_runs() == {
        "runsChecked": 1,
        "runsCompleted": 1,
        "runsIncomplete": 0,
        "errors": 0,
    }
    completion = pipeline.read(RAW, run_key(run, "completion.json"))
    assert completion["counts"] == {
        "accounts": 4,
        "accountsRetrieved": 2,
        "accountsSkipped": 2,
        "cases": 3,
        "casesProcessed": 3,
        "casesSkipped": 0,
        "casesFailed": 0,
    }
    assert pipeline.heartbeats() == 1
    # Later hourly checks see the completion marker and never repeat the heartbeat.
    assert pipeline.complete_runs()["runsCompleted"] == 0
    assert pipeline.heartbeats() == 1
    assert_no_case_content(pipeline, log_stream)


def test_heartbeat_waits_for_every_stage_of_the_run(pipeline: Pipeline) -> None:
    def incomplete() -> None:
        result = pipeline.complete_runs()
        assert (result["runsCompleted"], result["runsIncomplete"]) == (0, 1)
        assert pipeline.heartbeats() == 0

    pipeline.discover()
    incomplete()  # the account list exists but no account has been read
    pipeline.notify_account_lists()
    incomplete()  # accounts queued, not retrieved
    pipeline.deliver("activeAccounts")
    incomplete()  # cases queued for annotation
    pipeline.deliver("annotation")
    incomplete()  # conversations stored, workflows not started
    pipeline.deliver("summary")
    incomplete()  # executions started, none finished
    pipeline.held.add("1002")
    assert len(pipeline.run_workflows()) == 2
    incomplete()  # one execution still running
    assert pipeline.metric("RunCasesPending", "RunCompletion") >= 1
    assert pipeline.processed(ALPHA, "1002") is None

    pipeline.release("1002")
    assert len(pipeline.run_workflows()) == 1
    assert pipeline.complete_runs()["runsCompleted"] == 1
    assert pipeline.heartbeats() == 1


@pytest.mark.parametrize(
    ("accounts", "denied", "counts"),
    [
        pytest.param(
            [OrgAccount(MANAGEMENT, "management"), OrgAccount(DELTA, "d", state="SUSPENDED")],
            set(),
            {"accounts": 0, "accountsRetrieved": 0, "accountsSkipped": 0, "cases": 0},
            id="no-member-accounts",
        ),
        pytest.param(
            [OrgAccount(BETA, "beta"), OrgAccount(GAMMA, "gamma")],
            {BETA},
            {"accounts": 2, "accountsRetrieved": 0, "accountsSkipped": 2, "cases": 0},
            id="every-account-skipped",
        ),
        pytest.param(
            [OrgAccount(EPSILON, "epsilon")],
            set(),
            {"accounts": 1, "accountsRetrieved": 1, "accountsSkipped": 0, "cases": 0},
            id="no-cases",
        ),
    ],
)
def test_runs_without_cases_complete_once_every_account_is_settled(
    make_pipeline: Callable[..., Pipeline],
    accounts: list[OrgAccount],
    denied: set[str],
    counts: dict[str, int],
) -> None:
    support = FakeSupport()
    support.subscription_required.add(GAMMA)
    pipeline = make_pipeline(accounts=accounts, support=support)
    pipeline.sts.denied.update(denied)
    run = pipeline.discover()["runId"]
    if counts["accounts"]:
        assert pipeline.complete_runs()["runsCompleted"] == 0
        assert pipeline.heartbeats() == 0

    pipeline.settle()
    assert pipeline.complete_runs()["runsCompleted"] == 1
    assert pipeline.heartbeats() == 1
    stored = pipeline.read(RAW, run_key(run, "completion.json"))["counts"]
    assert {key: stored[key] for key in counts} == counts
    assert pipeline.bedrock.calls == []
    assert pipeline.sfn.executions == {}
    assert pipeline.keys(PROCESSED) == []


def test_replayed_deliveries_and_later_runs_never_reinfer_committed_cases(
    pipeline: Pipeline, start: datetime, log_stream: io.StringIO
) -> None:
    run = pipeline.discover()["runId"]
    pipeline.settle()
    assert pipeline.complete_runs()["runsCompleted"] == 1
    inferences = len(pipeline.bedrock.calls)
    executions = len(pipeline.sfn.executions)

    # S3 delivers the account-list notification again.
    [version] = pipeline.s3.list_object_versions(Bucket=ACCOUNT_LISTS)["Versions"]
    replay = pipeline.notify(version["Key"], version["VersionId"])
    assert (replay["duplicateEvents"], replay["accountsEnqueued"]) == (1, 0)
    # SQS delivers an account message and a summary reference again.
    pipeline.send("activeAccounts", {"accountId": ALPHA, "accountName": "alpha-prod", "runId": run})
    case_ref = pipeline.read(RAW, case_key(ALPHA, "1001", "workflow.json"))["input"]["case"]
    pipeline.send("summary", case_ref)
    pipeline.settle()
    assert pipeline.metric("DuplicateAccountMessages", "CaseRetrieval") == 1
    assert pipeline.metric("DuplicateSummaryMessages", "WorkflowStarter") == 1
    # An operator redrive of a committed case is answered from the processed record.
    pipeline.send("summary", {**case_ref, "redriveId": "replay-1"})
    pipeline.settle()
    assert pipeline.metric("CasesAlreadyProcessed", "WorkflowStarter") == 1
    # A stray second execution of a committed case runs every task without inference.
    ref = SummaryRef.parse(case_ref)
    stray = execution_name(ALPHA, "1001", run, 2)
    pipeline.sfn.add(stray, "RUNNING", json.dumps(execution_input(ref, 2)))
    pipeline.settle()
    assert pipeline.sfn.status(stray) == "SUCCEEDED"
    assert pipeline.metric("StagesSkippedAlreadyProcessed") == 4
    assert len(pipeline.sfn.executions) == executions + 1
    assert len(pipeline.bedrock.calls) == inferences

    # The next daily run finds the committed cases and analyses only the new ones: 1005 was
    # created after the first run started and 1006 after it finished.
    pipeline.advance(days=1)
    pipeline.support.add(SyntheticCase(ALPHA, "1006", run_day(start) + timedelta(hours=6)))
    pipeline.discover()
    pipeline.settle()
    assert pipeline.metric("CasesAlreadyProcessed", "CaseRetrieval") == 3
    assert sorted((c.display, c.stage) for c in pipeline.bedrock.calls[inferences:]) == sorted(
        (display, stage)
        for display in ("1005", "1006")
        for stage in ("summary", "rca", "lifecycle")
    )
    for account, display in ((ALPHA, "1001"), (ALPHA, "1002"), (EPSILON, "5001")):
        assert pipeline.versions(PROCESSED, case_key(account, display)) == 1
    assert pipeline.processed(ALPHA, "1006") is not None
    assert pipeline.complete_runs()["runsCompleted"] == 1
    assert pipeline.heartbeats() == 2
    assert_no_case_content(pipeline, log_stream)


def test_failed_stage_leaves_no_output_and_redrive_resumes_from_checkpoints(
    pipeline: Pipeline, log_stream: io.StringIO
) -> None:
    pipeline.bedrock.throttle("1001", "rca")
    pipeline.discover()
    pipeline.settle()

    [first] = pipeline.executions_for("1001")
    assert first.status == "FAILED"
    assert pipeline.processed(ALPHA, "1001") is None
    assert pipeline.versions(PROCESSED, case_key(ALPHA, "1001")) == 0
    assert pipeline.processed(ALPHA, "1002") is not None
    # Throttling was retried inside the stage and by the workflow before the Catch.
    assert len(pipeline.bedrock.calls_for("1001", "rca")) > TASK_RETRIES + 1
    assert len(pipeline.bedrock.calls_for("1001", "summary")) == 1
    [failure] = pipeline.drain("workflowFailures")
    assert failure["displayId"] == "1001"
    assert failure["stage"] == "rca"
    assert failure["errorType"] == "BedrockThrottlingError"
    assert failure["retryable"] is True
    assert failure["executionName"] == first.name

    # A failed case is a terminal outcome for the run once its failure record is confirmed.
    assert pipeline.complete_runs()["runsCompleted"] == 1
    assert pipeline.heartbeats() == 1
    assert pipeline.depth("workflowFailures") == 0

    pipeline.bedrock.heal()
    redrive(pipeline, failure, "redrive-1")
    pipeline.settle()
    first_attempt, second_attempt = sorted(pipeline.executions_for("1001"), key=lambda e: e.name)
    assert (first_attempt.status, second_attempt.status) == ("FAILED", "SUCCEEDED")
    assert second_attempt.name.endswith("-a2")
    assert pipeline.metric("FailedAttemptsRecovered", "WorkflowStarter") == 1
    # The summary checkpoint was reused; only the unfinished stages called Bedrock again.
    assert len(pipeline.bedrock.calls_for("1001", "summary")) == 1
    assert len(pipeline.bedrock.calls_for("1001", "lifecycle")) == 1
    record = pipeline.processed(ALPHA, "1001")
    assert record is not None
    assert record["Case_Summary"] == model_summary("1001")
    assert record["RCA_Category"] == RCA_REPLY["RCA_Category"]
    assert_no_case_content(pipeline, log_stream, failure)


def test_commit_fault_never_leaves_half_a_record_and_the_retry_commits_once(
    pipeline: Pipeline, log_stream: io.StringIO
) -> None:
    key = case_key(ALPHA, "1001")
    pipeline.fail_puts("update", key)
    pipeline.discover()
    pipeline.settle()

    assert pipeline.executions_for("1001")[0].status == "FAILED"
    assert key not in pipeline.keys(PROCESSED)
    assert pipeline.versions(PROCESSED, key) == 0
    [failure] = pipeline.drain("workflowFailures")
    assert (failure["stage"], failure["errorType"]) == ("update", "ClientError")
    # Each UpdateCaseMetadata attempt was retried by the workflow before the Catch.
    assert (
        len([i for i in pipeline.invocations if i.function == "UpdateCaseMetadata"])
        == (TASK_RETRIES + 1) + 2
    )
    inferences = len(pipeline.bedrock.calls_for("1001"))
    assert inferences == 3

    pipeline.heal_puts()
    redrive(pipeline, failure, "redrive-commit")
    pipeline.settle()
    assert pipeline.versions(PROCESSED, key) == 1
    assert pipeline.processed(ALPHA, "1001") is not None
    # Every stage checkpoint was reused: the retry committed without inference.
    assert len(pipeline.bedrock.calls_for("1001")) == inferences
    assert_no_case_content(pipeline, log_stream, failure)


def test_next_daily_run_recovers_a_closed_failed_execution(
    pipeline: Pipeline, log_stream: io.StringIO
) -> None:
    pipeline.bedrock.throttle("5001", "summary")
    pipeline.discover()
    pipeline.settle()
    [failed] = pipeline.executions_for("5001")
    assert failed.status == "FAILED"
    assert pipeline.processed(EPSILON, "5001") is None
    [failure] = pipeline.drain("workflowFailures")
    assert failure["stage"] == "summary"
    assert pipeline.complete_runs()["runsCompleted"] == 1
    inferences = len(pipeline.bedrock.calls)

    pipeline.bedrock.heal()
    pipeline.advance(days=1)
    next_run = pipeline.discover()["runId"]
    pipeline.settle()

    recovered = [e for e in pipeline.executions_for("5001") if e is not failed]
    assert [e.status for e in recovered] == ["SUCCEEDED"]
    assert recovered[0].name == execution_name(EPSILON, "5001", next_run, 1)
    assert pipeline.metric("FailedAttemptsRecovered", "WorkflowStarter") == 1
    assert pipeline.processed(EPSILON, "5001") is not None
    # Committed cases were skipped by retrieval; besides the recovered case only 1005, created
    # after the first run started, was analysed.
    assert {c.display for c in pipeline.bedrock.calls[inferences:]} == {"5001", "1005"}
    # The confirmed failure record of the closed attempt is not sent a second time.
    assert pipeline.drain("workflowFailures") == []
    assert pipeline.complete_runs()["runsCompleted"] == 1
    assert pipeline.heartbeats() == 2
    assert_no_case_content(pipeline, log_stream, failure)


def test_cleanup_protects_active_work_and_the_next_run_recovers_abandoned_cases(
    pipeline: Pipeline, start: datetime, log_stream: io.StringIO
) -> None:
    pipeline.held.add("1002")
    pipeline.pause_after["5001"] = "summary"
    first_run = pipeline.discover()["runId"]
    pipeline.settle()
    # 5001's execution hits the whole-execution timeout, which bypasses HandleFailure.
    abandoned = pipeline.time_out("5001")
    assert pipeline.processed(EPSILON, "5001") is None
    assert pipeline.drain("workflowFailures") == []

    # Run completion delivers the missing failure record but waits for the running execution.
    assert pipeline.complete_runs()["runsCompleted"] == 0
    assert pipeline.heartbeats() == 0
    [timed_out] = pipeline.drain("workflowFailures")
    assert (timed_out["displayId"], timed_out["executionName"]) == ("5001", abandoned)

    # Within the grace period nothing is reset.
    assert pipeline.cleanup()["reset"] == 0
    assert pipeline.invocations[-1].metrics["CleanupSkippedRecent"] == 2

    pipeline.advance(days=2)
    raw_1002 = [case_key(ALPHA, "1002", n) for n in ("communications.json", "data.json")]
    assert set(raw_1002) <= set(pipeline.keys(RAW, case_key(ALPHA, "1002", "")))
    result = pipeline.cleanup()
    assert (result["reset"], result["errors"]) == (1, 0)
    cleaned = pipeline.invocations[-1].metrics
    assert (cleaned["CleanupSkippedActive"], cleaned["CleanupSkippedProcessed"]) == (1, 1)
    # The running case keeps its work; the abandoned one loses only its resettable objects.
    assert set(raw_1002) | {case_key(ALPHA, "1002", "workflow.json")} <= set(
        pipeline.keys(RAW, case_key(ALPHA, "1002", ""))
    )
    remaining = pipeline.keys(RAW, case_key(EPSILON, "5001", ""))
    for name in ("data.json", "communications.json", "workflow.json"):
        assert case_key(EPSILON, "5001", name) not in remaining
    assert pipeline.drain("workflowFailures") == []
    summary_calls = len(pipeline.bedrock.calls_for("5001", "summary"))

    # The next daily run collects the abandoned case again and leaves the running one alone.
    second_run = pipeline.discover()["runId"]
    pipeline.settle()
    assert pipeline.processed(EPSILON, "5001") is not None
    assert [e.status for e in pipeline.executions_for("5001")] == ["TIMED_OUT", "SUCCEEDED"]
    assert pipeline.executions_for("5001")[1].name == execution_name(EPSILON, "5001", second_run, 1)
    assert len(pipeline.bedrock.calls_for("5001", "summary")) == summary_calls
    assert len(pipeline.executions_for("1002")) == 1
    assert pipeline.metric("WorkflowsAlreadyActive", "WorkflowStarter") == 1
    assert pipeline.complete_runs()["runsCompleted"] == 0

    pipeline.release("1002")
    pipeline.settle()
    assert pipeline.processed(ALPHA, "1002") is not None
    result = pipeline.complete_runs()
    assert result["runsCompleted"] == 2
    assert pipeline.heartbeats() == 2
    for run in (first_run, second_run):
        assert run_key(run, "completion.json") in pipeline.keys(RAW, f"runs/{run}/")
    assert {i.function for i in pipeline.invocations} >= CORE_FUNCTIONS - {"HandleFailure"}
    assert_no_case_content(pipeline, log_stream, timed_out)


def test_failed_account_retrieval_is_retried_redriven_and_never_completes_the_run_early(
    pipeline: Pipeline,
) -> None:
    pipeline.fail_puts("retrieval", case_key(EPSILON, "5001"))
    pipeline.discover()
    pipeline.settle()

    # The failed record alone was retried until SQS moved it to the dead-letter queue.
    assert pipeline.metric("CaseRetrievalFailures", "CaseRetrieval") == MAX_RECEIVE_COUNT
    [dead] = pipeline.drain("activeAccounts", dead_letter=True)
    assert dead["accountId"] == EPSILON
    assert pipeline.processed(ALPHA, "1001") is not None
    assert pipeline.processed(EPSILON, "5001") is None
    assert pipeline.complete_runs()["runsCompleted"] == 0
    assert pipeline.heartbeats() == 0

    pipeline.heal_puts()
    pipeline.send("activeAccounts", dead)
    pipeline.settle()
    assert pipeline.processed(EPSILON, "5001") is not None
    assert pipeline.complete_runs()["runsCompleted"] == 1
    assert pipeline.heartbeats() == 1


def test_dispatched_case_is_awaited_after_a_settling_failure_and_lost_access(
    pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = pipeline.discover()["runId"]
    original = retrieval_app.record_account

    def settling_fails(s3: object, bucket: str, marker: AccountMarker) -> None:
        if (marker.account_id, marker.outcome) == (EPSILON, "retrieved"):
            raise client_error("InternalError", "PutObject", 500)
        original(s3, bucket, marker)  # type: ignore[arg-type]

    # 5001 is queued for annotation, but EPSILON's account cannot be settled.
    with monkeypatch.context() as patch:
        patch.setattr(retrieval_app, "record_account", settling_fails)
        pipeline.notify_account_lists()
        pipeline.deliver("activeAccounts")
    marker = pipeline.read(RAW, run_key(run, f"accounts/{EPSILON}.json"))
    assert (marker["outcome"], marker["cases"]) == ("dispatching", ["5001"])

    # 5001 is annotated and its execution starts; by the account's retry the member role is gone.
    pipeline.held.add("5001")
    pipeline.deliver("annotation")
    pipeline.deliver("summary")
    assert [e.status for e in pipeline.executions_for("5001")] == ["RUNNING"]
    pipeline.sts.denied.add(EPSILON)
    pipeline.settle()
    marker = pipeline.read(RAW, run_key(run, f"accounts/{EPSILON}.json"))
    assert (marker["outcome"], marker["skipReason"], marker["cases"]) == (
        "skipped",
        "AccessDenied",
        ["5001"],
    )
    assert pipeline.metric("CasesRedispatched", "CaseRetrieval") == 1
    result = pipeline.complete_runs()
    assert (result["runsCompleted"], result["runsIncomplete"]) == (0, 1)
    assert pipeline.heartbeats() == 0

    pipeline.release("5001")
    pipeline.settle()
    assert pipeline.processed(EPSILON, "5001") is not None
    assert len(pipeline.executions_for("5001")) == 1
    assert len(pipeline.bedrock.calls_for("5001", "summary")) == 1
    assert pipeline.complete_runs()["runsCompleted"] == 1
    assert pipeline.heartbeats() == 1
    counts = pipeline.read(RAW, run_key(run, "completion.json"))["counts"]
    assert (counts["accountsSkipped"], counts["cases"], counts["casesProcessed"]) == (3, 3, 3)

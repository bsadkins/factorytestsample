"""Run Completion: the daily heartbeat is emitted only after every account and case of a run has
reached a terminal outcome, including zero-case and fully skipped runs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from common import failure_delivery
from common.account_list import (
    AccountRef,
    DiscoveryCounts,
    account_list_key,
    build_account_list_document,
)
from common.case_refs import SummaryRef
from common.failure_delivery import DeliveryMarker, delivery_marker_key
from common.run_markers import (
    CaseMarker,
    completion_key,
    dispatching_account,
    retrieved_account,
    skipped_account,
    write_account_marker,
    write_case_marker,
    write_completion,
)
from common.sqs import SqsSendError
from common.workflow import encode_input, execution_input, execution_name
from run_completion import app
from workflow_kit import STATE_MACHINE_ARN, FakeStepFunctions, sfn_error

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

LISTS = "account-lists-completion-test"
RAW = "raw-completion-test"
MANAGEMENT = "999999999999"
A, B, C = "111111111111", "222222222222", "333333333333"
RUN = "20261113T000000Z"
OLD_RUN = "20261101T000000Z"
NOW = datetime(2026, 11, 13, 6, 0, 0, tzinfo=UTC)
SENTINEL_NAME = "SENTINEL-ACCOUNT-NAME-5d1e"


def dispatched(account: str, *displays: str) -> dict[str, str]:
    return {display: f"case-{account}-2026-{display}" for display in displays}


def case_ref(account: str, display: str, run_id: str = RUN) -> SummaryRef:
    return SummaryRef(
        account_id=account,
        case_id=f"case-{account}-2026-{display}",
        display_id=display,
        run_id=run_id,
        communications_version_id="v1",
        communication_count=2,
    )


@dataclass
class Env:
    s3: S3Client
    sfn: FakeStepFunctions
    sqs: Any
    queue_url: str
    kit: Any
    capsys: pytest.CaptureFixture[str]
    clock: list[datetime] = field(default_factory=lambda: [NOW])
    remaining_millis: int = 600_000
    stdout: str = ""

    def account_list(self, run_id: str, *accounts: str) -> None:
        refs = [AccountRef(account, f"{SENTINEL_NAME}-{account}") for account in accounts]
        counts = DiscoveryCounts(len(refs) + 1, len(refs), 0, 1)
        generated = datetime.strptime(run_id[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        document = build_account_list_document(run_id, generated, refs, counts)
        self.s3.put_object(Bucket=LISTS, Key=account_list_key(run_id), Body=json.dumps(document))

    def retrieved(self, account: str, *displays: str, run_id: str = RUN) -> None:
        write_account_marker(
            self.s3, RAW, retrieved_account(run_id, account, dispatched(account, *displays))
        )

    def skipped(
        self,
        account: str,
        reason: str = "AccessDenied",
        run_id: str = RUN,
        cases: tuple[str, ...] = (),
    ) -> None:
        marker = skipped_account(run_id, account, reason, dispatched(account, *cases))
        write_account_marker(self.s3, RAW, marker)

    def outcome(self, account: str, display: str, outcome: str, run_id: str = RUN) -> None:
        write_case_marker(self.s3, RAW, CaseMarker(run_id, account, display, outcome))

    def handed_off(
        self, account: str, display: str, status: str | None, attempt: int = 1, run_id: str = RUN
    ) -> str:
        """The starter's record of the execution owning the case, which has reached `status`."""
        ref = case_ref(account, display, run_id)
        name = execution_name(account, display, run_id, attempt)
        write_case_marker(self.s3, RAW, CaseMarker(run_id, account, display, "started", name))
        if status is not None:
            self.sfn.add(name, status, encode_input(execution_input(ref, attempt)))
        return name

    def run(self) -> dict[str, Any]:
        context = self.kit.context()
        context._remaining_millis = self.remaining_millis
        result: dict[str, Any] = app.handler({}, context)
        return result

    def completion(self, run_id: str = RUN) -> dict[str, Any] | None:
        try:
            body = self.s3.get_object(Bucket=RAW, Key=completion_key(run_id))["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None
        document: dict[str, Any] = json.loads(body)
        return document

    def failures(self) -> list[dict[str, Any]]:
        received: list[dict[str, Any]] = self.kit.drain(self.sqs, self.queue_url)
        return received

    def delivery(self, account: str, display: str, name: str) -> dict[str, Any]:
        body = self.s3.get_object(Bucket=RAW, Key=delivery_marker_key(account, display, name))
        document: dict[str, Any] = json.loads(body["Body"].read())
        return document

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
    s3 = boto3.client("s3", region_name="us-east-1")
    for bucket in (LISTS, RAW):
        s3.create_bucket(Bucket=bucket)
    sqs = boto3.client("sqs", region_name="us-east-1")
    queue_url = sqs.create_queue(QueueName="workflow-failures")["QueueUrl"]
    for name, value in {
        "ACCOUNT_LIST_BUCKET": LISTS,
        "RAW_BUCKET": RAW,
        "STATE_MACHINE_ARN": STATE_MACHINE_ARN,
        "FAILURE_QUEUE_URL": queue_url,
        "MANAGEMENT_ACCOUNT_ID": MANAGEMENT,
        "RUN_LOOKBACK_HOURS": "72",
    }.items():
        monkeypatch.setenv(name, value)
    sfn = FakeStepFunctions()
    environment = Env(s3, sfn, sqs, queue_url, case_collection_kit, capsys)
    clients = app.Clients(s3=s3, sfn=sfn, sqs=sqs)  # type: ignore[arg-type]
    monkeypatch.setattr(app, "clients", lambda: clients)
    monkeypatch.setattr(app, "now", lambda: environment.clock[0])
    monkeypatch.setattr(app, "LIST_PAGE_SIZE", 2)
    return environment


def test_finished_lookup_alone_never_completes_a_run(env: Env) -> None:
    env.account_list(RUN, A, B)

    result = env.run()

    assert result == {"runsChecked": 1, "runsCompleted": 0, "runsIncomplete": 1, "errors": 0}
    assert env.completion() is None
    assert env.metrics() == {
        "DailyRunCompleted": 0,
        "RunsIncomplete": 1,
        "RunCasesPending": 0,
        "RunCompletionErrors": 0,
    }
    assert "Run is not complete yet" in env.kit.logs()


def test_run_with_no_accounts_completes_immediately(env: Env) -> None:
    env.account_list(RUN)

    assert env.run()["runsCompleted"] == 1

    completion = env.completion()
    assert completion is not None
    assert completion["counts"] == {
        "accounts": 0,
        "accountsRetrieved": 0,
        "accountsSkipped": 0,
        "cases": 0,
        "casesProcessed": 0,
        "casesSkipped": 0,
        "casesFailed": 0,
    }
    metrics = env.metrics()
    assert metrics["DailyRunCompleted"] == 1
    assert metrics["RunDuration"] == 6 * 3600


def test_fully_skipped_and_zero_case_accounts_complete_the_run(env: Env) -> None:
    env.account_list(RUN, A, B, C)
    env.skipped(A)
    env.skipped(B, "SubscriptionRequired")
    env.retrieved(C)

    env.run()

    completion = env.completion()
    assert completion is not None
    assert completion["counts"]["accountsSkipped"] == 2
    assert completion["counts"]["accountsRetrieved"] == 1
    metrics = env.metrics()
    assert metrics["DailyRunCompleted"] == 1
    assert metrics["RunAccountsSkipped"] == 2
    assert metrics["RunAccountsRetrieved"] == 1


def test_staged_multi_account_run_completes_only_when_every_case_is_terminal(env: Env) -> None:
    env.account_list(RUN, A, B, C)
    env.retrieved(A, "1001", "1002", "1003", "1004", "1005", "1006")
    env.skipped(B)

    # Retrieval of C and annotation of most of A's cases are still in flight.
    assert env.run()["runsCompleted"] == 0
    assert env.metrics()["RunCasesPending"] == 6

    env.retrieved(C)
    env.outcome(A, "1001", "skippedAccessDenied")
    env.outcome(A, "1002", "oversized")
    env.outcome(A, "1003", "alreadyProcessed")
    running = env.handed_off(A, "1004", "RUNNING")
    env.handed_off(A, "1005", "SUCCEEDED")
    # 1006 is queued for summary but the starter has not handled it yet.
    assert env.run()["runsCompleted"] == 0
    assert env.metrics()["RunCasesPending"] == 2

    env.outcome(A, "1006", "notFound")
    env.sfn.executions[running].status = "SUCCEEDED"
    assert env.run()["runsCompleted"] == 1

    completion = env.completion()
    assert completion is not None
    assert completion["counts"] == {
        "accounts": 3,
        "accountsRetrieved": 2,
        "accountsSkipped": 1,
        "cases": 6,
        "casesProcessed": 3,
        "casesSkipped": 2,
        "casesFailed": 1,
    }
    metrics = env.metrics()
    assert metrics["DailyRunCompleted"] == 1
    assert metrics["RunCasesProcessed"] == 3
    assert metrics["RunCasesSkipped"] == 2
    assert metrics["RunCasesFailed"] == 1

    # The heartbeat is emitted once per run.
    assert env.run() == {"runsChecked": 0, "runsCompleted": 0, "runsIncomplete": 0, "errors": 0}
    assert env.metrics()["DailyRunCompleted"] == 0


@pytest.mark.parametrize("status", ["TIMED_OUT", "ABORTED", "FAILED"])
def test_closed_failed_execution_is_terminal_once_its_failure_record_is_delivered(
    env: Env, status: str
) -> None:
    env.account_list(RUN, A)
    env.retrieved(A, "2001")
    name = env.handed_off(A, "2001", status)

    assert env.run()["runsCompleted"] == 1

    [failure] = env.failures()
    assert (failure["failureType"], failure["executionStatus"], failure["deliveryKey"]) == (
        "WorkflowExecutionClosed",
        status,
        name,
    )
    assert env.delivery(A, "2001", name)["status"] == "sent"
    completion = env.completion()
    assert completion is not None
    assert completion["counts"]["casesFailed"] == 1
    metrics = env.metrics()
    assert metrics["FailureDeliveriesSent"] == 1
    assert metrics["FailureDeliveriesPreserved"] == 1


def test_record_confirmed_by_the_failure_handler_is_not_sent_again(env: Env) -> None:
    env.account_list(RUN, A)
    env.retrieved(A, "2101")
    name = env.handed_off(A, "2101", "FAILED")
    sent = DeliveryMarker(case_ref(A, "2101"), 1, "sent", None, "t", "t")
    env.s3.put_object(Bucket=RAW, Key=sent.key, Body=json.dumps(sent.to_document()))

    assert env.run()["runsCompleted"] == 1
    assert env.failures() == []
    assert env.delivery(A, "2101", name)["status"] == "sent"


def test_failed_delivery_keeps_the_run_incomplete_until_the_record_is_sent(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.account_list(RUN, A)
    env.retrieved(A, "2201")
    name = env.handed_off(A, "2201", "TIMED_OUT")

    def unavailable(*_: Any, **__: Any) -> int:  # noqa: ANN401
        raise SqsSendError(1, ("ServiceUnavailable",))

    with monkeypatch.context() as patch:
        patch.setattr(failure_delivery, "send_messages", unavailable)
        result = env.run()

    assert result == {"runsChecked": 1, "runsCompleted": 0, "runsIncomplete": 1, "errors": 1}
    assert env.delivery(A, "2201", name)["status"] == "pending"
    assert env.completion() is None
    metrics = env.metrics()
    assert metrics["FailureDeliveryErrors"] == 1
    assert metrics["RunCompletionErrors"] == 1

    assert env.run()["runsCompleted"] == 1
    assert [f["deliveryKey"] for f in env.failures()] == [name]


@pytest.mark.parametrize("problem", ["missing", "throttled", "foreign-input", "bad-name"])
def test_unknown_execution_state_never_completes_the_run(env: Env, problem: str) -> None:
    env.account_list(RUN, A)
    env.retrieved(A, "2301")
    if problem == "missing":
        env.handed_off(A, "2301", None)
    elif problem == "throttled":
        env.handed_off(A, "2301", "SUCCEEDED")
        env.sfn.failures["describe"] = sfn_error("ThrottlingException", "DescribeExecution")
    elif problem == "foreign-input":
        name = env.handed_off(A, "2301", None)
        env.sfn.add(name, "FAILED", "{}")
    else:
        other = execution_name(B, "9999", RUN, 1)
        write_case_marker(env.s3, RAW, CaseMarker(RUN, A, "2301", "started", other))

    result = env.run()

    assert result["runsCompleted"] == 0
    assert result["errors"] == 1
    assert env.failures() == []
    assert "Case outcome could not be reconciled; run stays incomplete" in env.kit.logs()


def test_unreadable_markers_keep_the_run_incomplete(env: Env) -> None:
    env.account_list(RUN, A, B)
    env.s3.put_object(Bucket=RAW, Key=f"runs/{RUN}/accounts/{A}.json", Body=b"{bad")
    env.retrieved(B, "2401")
    env.s3.put_object(Bucket=RAW, Key=f"runs/{RUN}/cases/{B}/2401.json", Body=b"[]")

    result = env.run()

    assert result["errors"] == 2
    assert env.completion() is None
    assert "Account run marker is unreadable" in env.kit.logs()


def test_runs_are_independent_and_bounded_by_the_lookback(env: Env) -> None:
    env.account_list(OLD_RUN, A)  # outside the 72-hour lookback: never checked
    env.account_list("20261111T000000Z", A)
    env.s3.put_object(Bucket=LISTS, Key=account_list_key("20261112T000000Z"), Body=b"{bad")
    env.account_list(RUN)

    result = env.run()

    assert result == {"runsChecked": 3, "runsCompleted": 1, "runsIncomplete": 2, "errors": 1}
    assert env.completion(RUN) is not None
    assert env.completion(OLD_RUN) is None
    assert "Run could not be checked" in env.kit.logs()


def test_time_budget_leaves_runs_for_the_next_invocation(env: Env) -> None:
    env.account_list(RUN, A)
    env.retrieved(A)
    env.remaining_millis = 1_000

    result = env.run()

    assert result == {"runsChecked": 0, "runsCompleted": 0, "runsIncomplete": 1, "errors": 0}
    assert env.completion() is None


def test_time_budget_inside_a_run_does_not_complete_it(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.account_list(RUN, A)
    env.retrieved(A)
    calls = {"count": 0}

    def budget(self: Any) -> int:  # noqa: ANN401
        calls["count"] += 1
        return 600_000 if calls["count"] == 1 else 1_000

    monkeypatch.setattr(type(env.kit.context()), "get_remaining_time_in_millis", budget)

    assert env.run()["runsCompleted"] == 0
    assert env.completion() is None


def test_completion_is_recorded_once_even_when_invocations_overlap(env: Env) -> None:
    assert write_completion(env.s3, RAW, RUN, "t", {"accounts": 0})
    assert not write_completion(env.s3, RAW, RUN, "t", {"accounts": 1})
    stored = env.completion()
    assert stored is not None
    assert stored["counts"] == {"accounts": 0}


def test_listing_failure_fails_the_invocation_visibly(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: Any, **__: Any) -> list[str]:  # noqa: ANN401
        raise sfn_error("AccessDenied", "ListObjectsV2", 403)

    monkeypatch.setattr(app, "candidate_runs", broken)

    with pytest.raises(app.RunCompletionError):
        env.run()
    assert env.metrics() == {"RunCompletionErrors": 1}
    assert "Run completion check failed" in env.kit.logs()


def test_logs_and_metrics_carry_no_account_names_or_case_content(env: Env) -> None:
    env.account_list(RUN, A, B)
    env.retrieved(A, "2501")
    env.handed_off(A, "2501", "TIMED_OUT")
    env.skipped(B)

    env.run()

    output = env.capsys.readouterr().out + env.kit.logs()
    assert SENTINEL_NAME not in output
    assert SENTINEL_NAME not in json.dumps(env.failures())


def test_settings_validate_the_lookback(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "ACCOUNT_LIST_BUCKET": LISTS,
        "RAW_BUCKET": RAW,
        "STATE_MACHINE_ARN": STATE_MACHINE_ARN,
        "FAILURE_QUEUE_URL": "https://sqs.example/queue",
        "MANAGEMENT_ACCOUNT_ID": MANAGEMENT,
        "RUN_LOOKBACK_HOURS": "48",
    }.items():
        monkeypatch.setenv(name, value)
    assert app.Settings.from_env().lookback == timedelta(hours=48)
    monkeypatch.setenv("RUN_LOOKBACK_HOURS", "12")
    with pytest.raises(ValueError, match="at least 24"):
        app.Settings.from_env()
    monkeypatch.setenv("RUN_LOOKBACK_HOURS", "48")
    monkeypatch.setenv("MANAGEMENT_ACCOUNT_ID", "not-an-account")
    with pytest.raises(ValueError, match="account"):
        app.Settings.from_env()


# Account markers keep every case an attempt may have handed off, so neither an unsettled
# dispatch nor a later access skip can let the heartbeat fire before that work is terminal.


def test_dispatching_account_is_pending_even_when_its_cases_are_terminal(env: Env) -> None:
    env.account_list(RUN, A)
    write_account_marker(env.s3, RAW, dispatching_account(RUN, A, dispatched(A, "2601")))
    env.outcome(A, "2601", "alreadyProcessed")

    result = env.run()

    assert result == {"runsChecked": 1, "runsCompleted": 0, "runsIncomplete": 1, "errors": 0}
    assert env.completion() is None

    env.retrieved(A, "2601")
    assert env.run()["runsCompleted"] == 1


def test_skipped_account_with_earlier_dispatches_waits_for_those_cases(env: Env) -> None:
    env.account_list(RUN, A)
    env.skipped(A, cases=("2701", "2702"))
    running = env.handed_off(A, "2701", "RUNNING")

    assert env.run()["runsCompleted"] == 0
    assert env.metrics()["RunCasesPending"] == 2

    env.sfn.executions[running].status = "SUCCEEDED"
    assert env.run()["runsCompleted"] == 0
    assert env.metrics()["RunCasesPending"] == 1

    env.outcome(A, "2702", "skippedAccessDenied")
    assert env.run()["runsCompleted"] == 1
    completion = env.completion()
    assert completion is not None
    assert completion["counts"] == {
        "accounts": 1,
        "accountsRetrieved": 0,
        "accountsSkipped": 1,
        "cases": 2,
        "casesProcessed": 1,
        "casesSkipped": 1,
        "casesFailed": 0,
    }

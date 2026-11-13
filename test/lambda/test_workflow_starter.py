"""Workflow Starter: one CaseAnalysis execution per case attempt, reconciled through the S3
ownership marker and Step Functions execution status."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from common.case_refs import SummaryRef
from common.idempotency import persistence_layer
from common.workflow import (
    MAX_ATTEMPTS_PER_RUN,
    OwnershipMarker,
    encode_input,
    execution_input,
    execution_name,
    marker_key,
)
from workflow_kit import STATE_MACHINE_ARN, FakeStepFunctions, sfn_error
from workflow_starter import app

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

RAW = "raw-starter-test"
PROCESSED = "processed-starter-test"
TABLE = "idempotency-starter-test"
ACCOUNT = "111122223333"
DISPLAY = "170000000001"
RUN = "20261113T000000Z"
NEXT_RUN = "20261114T000000Z"
START = datetime(2026, 11, 13, 1, 0, 0, tzinfo=UTC)


def summary_ref(
    run_id: str = RUN, display: str = DISPLAY, version: str = "v1", redrive: str | None = None
) -> SummaryRef:
    return SummaryRef(
        account_id=ACCOUNT,
        case_id=f"case-{ACCOUNT}-2026-{display}",
        display_id=display,
        run_id=run_id,
        communications_version_id=version,
        communication_count=3,
        redrive_id=redrive,
    )


def name(attempt: int = 1, run_id: str = RUN, display: str = DISPLAY) -> str:
    return execution_name(ACCOUNT, display, run_id, attempt)


@dataclass
class Env:
    s3: S3Client
    sfn: FakeStepFunctions
    kit: Any
    capsys: pytest.CaptureFixture[str]
    clock: list[datetime] = field(default_factory=lambda: [START])
    stdout: str = ""
    requests: int = 0

    def handle(self, *refs: SummaryRef | object) -> Any:  # noqa: ANN401
        self.requests += 1
        bodies = [r.to_message() if isinstance(r, SummaryRef) else r for r in refs]
        return app.handler(self.kit.event(*bodies), self.kit.context(f"request-{self.requests}"))

    def metrics(self) -> dict[str, float]:
        self.stdout = self.capsys.readouterr().out
        metrics: dict[str, float] = self.kit.metrics(self.stdout)
        return metrics

    def logs(self) -> str:
        logs: str = self.kit.logs()
        return logs

    def marker(self, display: str = DISPLAY) -> dict[str, Any] | None:
        try:
            body = self.s3.get_object(Bucket=RAW, Key=marker_key(ACCOUNT, display))
        except self.s3.exceptions.NoSuchKey:
            return None
        document: dict[str, Any] = json.loads(body["Body"].read())
        return document

    def put_marker(self, ref: SummaryRef, attempt: int, claimed_at: datetime = START) -> str:
        claim = OwnershipMarker.claim(ref, attempt, claimed_at)
        self.s3.put_object(Bucket=RAW, Key=claim.key, Body=json.dumps(claim.to_document()))
        return claim.execution_name

    def previous_attempt(self, ref: SummaryRef, attempt: int, status: str) -> str:
        """An earlier execution of `ref` that has reached `status`, with its marker."""
        execution = self.put_marker(ref, attempt, START - timedelta(days=1))
        self.sfn.add(execution, status, encode_input(execution_input(ref, attempt)))
        return execution

    def mark_processed(self, display: str = DISPLAY) -> None:
        self.s3.put_object(
            Bucket=PROCESSED,
            Key=f"account_number={ACCOUNT}/case_number={display}/data.json",
            Body=b"{}",
        )

    def advance(self, **delta: float) -> None:
        self.clock[0] += timedelta(**delta)


@pytest.fixture
def env(
    mocked_aws: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case_collection_kit: Any,  # noqa: ANN401
) -> Iterator[Env]:
    s3 = boto3.client("s3", region_name="us-east-1")
    for bucket in (RAW, PROCESSED):
        s3.create_bucket(Bucket=bucket)
        s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    case_collection_kit.create_table(TABLE)
    for key, value in {
        "RAW_BUCKET": RAW,
        "PROCESSED_BUCKET": PROCESSED,
        "STATE_MACHINE_ARN": STATE_MACHINE_ARN,
        "CLAIM_SETTLE_SECONDS": "300",
        "IDEMPOTENCY_TABLE_NAME": TABLE,
    }.items():
        monkeypatch.setenv(key, value)
    sfn = FakeStepFunctions()
    environment = Env(s3=s3, sfn=sfn, kit=case_collection_kit, capsys=capsys)
    clients = app.Clients(s3=s3, sfn=sfn)  # type: ignore[arg-type]
    monkeypatch.setattr(app, "clients", lambda: clients)
    monkeypatch.setattr(app, "now", lambda: environment.clock[0])
    persistence_layer.cache_clear()
    yield environment
    persistence_layer.cache_clear()


def test_new_case_claims_ownership_and_starts_attempt_one(env: Env) -> None:
    ref = summary_ref()

    response = env.handle(ref)

    assert response == {"batchItemFailures": []}
    assert env.sfn.started() == [name(1)]
    execution = env.sfn.executions[name(1)]
    assert json.loads(execution.input) == {
        "case": ref.to_message(),
        "attempt": {"runId": RUN, "number": 1, "executionName": name(1)},
    }
    marker = env.marker()
    assert marker is not None
    assert marker["executionName"] == name(1)
    assert marker["attempt"] == 1
    assert marker["input"] == json.loads(execution.input)
    assert env.metrics() == {"WorkflowsStarted": 1}


def test_execution_name_is_stable_and_within_step_functions_limits() -> None:
    assert name(1) == f"{ACCOUNT}-{DISPLAY}-{RUN}-a1"
    long_display = "9" * 40
    hashed = name(3, display=long_display)
    assert len(hashed) <= 80
    assert hashed == name(3, display=long_display)
    assert long_display not in hashed


def test_duplicate_delivery_in_one_batch_and_across_batches_starts_once(env: Env) -> None:
    ref = summary_ref()

    first = env.handle(ref, ref)
    second = env.handle(ref)

    assert first == second == {"batchItemFailures": []}
    assert env.sfn.started() == [name(1)]
    assert len(env.sfn.executions) == 1
    assert env.metrics() == {"WorkflowsStarted": 1, "DuplicateSummaryMessages": 2}


def test_overlapping_run_never_steals_a_running_execution(env: Env) -> None:
    env.handle(summary_ref())
    owner = env.marker()

    response = env.handle(summary_ref(NEXT_RUN, version="v2"))

    assert response == {"batchItemFailures": []}
    assert env.sfn.started() == [name(1)]
    assert env.marker() == owner
    assert env.metrics() == {"WorkflowsStarted": 1, "WorkflowsAlreadyActive": 1}


@pytest.mark.parametrize("status", ["RUNNING", "PENDING_REDRIVE"])
def test_active_execution_of_the_same_run_is_left_alone(env: Env, status: str) -> None:
    ref = summary_ref()
    env.previous_attempt(ref, 1, status)

    response = env.handle(summary_ref(redrive="operator-1"))

    assert response == {"batchItemFailures": []}
    assert env.sfn.started() == []
    assert env.metrics() == {"WorkflowsAlreadyActive": 1}


def test_processed_case_is_never_started(env: Env) -> None:
    env.mark_processed()

    response = env.handle(summary_ref(), summary_ref(NEXT_RUN, redrive="r1"))

    assert response == {"batchItemFailures": []}
    assert env.sfn.calls == []
    assert env.marker() is None
    assert env.metrics() == {"CasesAlreadyProcessed": 2}


def test_succeeded_execution_with_processed_record_is_not_restarted(env: Env) -> None:
    ref = summary_ref()
    env.previous_attempt(ref, 1, "SUCCEEDED")
    # The record is committed while the starter is describing the execution.
    env.sfn.effects["describe"] = env.mark_processed

    response = env.handle(summary_ref(NEXT_RUN))

    assert response == {"batchItemFailures": []}
    assert env.sfn.started() == []
    assert env.metrics() == {"CasesAlreadyProcessed": 1}


@pytest.mark.parametrize("status", ["FAILED", "TIMED_OUT", "ABORTED"])
def test_closed_failed_attempt_is_recovered_by_a_later_run(env: Env, status: str) -> None:
    ref = summary_ref()
    failed = env.previous_attempt(ref, 1, status)

    response = env.handle(summary_ref(NEXT_RUN, version="v2"))

    assert response == {"batchItemFailures": []}
    assert env.sfn.status(failed) == status
    assert env.sfn.started() == [name(1, NEXT_RUN)]
    marker = env.marker()
    assert marker is not None
    assert (marker["runId"], marker["attempt"]) == (NEXT_RUN, 1)
    assert env.metrics() == {"WorkflowsStarted": 1, "FailedAttemptsRecovered": 1}


def test_operator_redrive_of_the_same_run_takes_the_next_attempt_name(env: Env) -> None:
    ref = summary_ref()
    env.handle(ref)
    env.sfn.executions[name(1)].status = "FAILED"

    # The original message is a duplicate; only an explicit redrive starts attempt 2.
    env.handle(ref)
    env.handle(summary_ref(redrive="redrive-1"))

    assert env.sfn.started() == [name(1), name(2)]
    assert env.sfn.status(name(1)) == "FAILED"
    assert env.sfn.status(name(2)) == "RUNNING"
    marker = env.marker()
    assert marker is not None
    assert marker["attempt"] == 2
    # Redrive identity never reaches the execution input or its name.
    assert "redriveId" not in marker["input"]["case"]
    assert env.metrics() == {
        "WorkflowsStarted": 2,
        "DuplicateSummaryMessages": 1,
        "FailedAttemptsRecovered": 1,
    }


def test_execution_already_exists_is_reconciled_not_treated_as_success(env: Env) -> None:
    ref = summary_ref()
    # The marker is gone (for example removed by cleanup) but Step Functions still reserves
    # the attempt-1 name for a closed execution.
    env.sfn.add(name(1), "FAILED", encode_input(execution_input(ref, 1)))

    response = env.handle(ref)

    assert response == {"batchItemFailures": []}
    assert env.sfn.started() == [name(1), name(2)]
    assert env.sfn.status(name(2)) == "RUNNING"
    marker = env.marker()
    assert marker is not None
    assert marker["attempt"] == 2
    metrics = env.metrics()
    assert metrics["ExecutionNameConflicts"] == 1
    assert metrics["WorkflowsStarted"] == 1


def test_execution_already_exists_for_a_running_execution_is_already_active(env: Env) -> None:
    # Same name but different input (a newer conversation version of the same run).
    env.sfn.add(name(1), "RUNNING", encode_input(execution_input(summary_ref(version="v0"), 1)))

    response = env.handle(summary_ref())

    assert response == {"batchItemFailures": []}
    assert len(env.sfn.executions) == 1
    assert env.metrics() == {"WorkflowsAlreadyActive": 1, "ExecutionNameConflicts": 1}


def test_claim_whose_start_was_interrupted_is_completed_with_its_own_input(env: Env) -> None:
    claimed = summary_ref(version="v1")
    env.put_marker(claimed, 1, START - timedelta(seconds=30))

    response = env.handle(summary_ref(version="v1", redrive="retry"))

    assert response == {"batchItemFailures": []}
    assert env.sfn.started() == [name(1)]
    assert json.loads(env.sfn.executions[name(1)].input) == execution_input(claimed, 1)
    assert env.metrics() == {"WorkflowsStarted": 1, "OwnershipClaimsCompleted": 1}


def test_abandoned_claim_older_than_the_settle_time_moves_to_the_next_attempt(env: Env) -> None:
    ref = summary_ref()
    env.put_marker(ref, 1, START - timedelta(hours=1))

    env.handle(summary_ref(redrive="retry"))

    assert env.sfn.started() == [name(2)]
    assert env.sfn.status(name(1)) is None


def test_lost_claim_race_defers_to_the_winning_starter(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = summary_ref()
    real_claim = app._claim

    def competing_claim(*args: Any, **kwargs: Any) -> bool:  # noqa: ANN401
        monkeypatch.setattr(app, "_claim", real_claim)
        # Another starter claims and starts the case between our read and our write.
        assert real_claim(*args, **kwargs)
        env.sfn.add(name(1), "RUNNING", encode_input(execution_input(ref, 1)))
        return real_claim(*args, **kwargs)

    monkeypatch.setattr(app, "_claim", competing_claim)
    response = env.handle(ref)

    assert response == {"batchItemFailures": []}
    assert env.sfn.started() == []
    assert len(env.sfn.executions) == 1
    assert env.metrics() == {"WorkflowsAlreadyActive": 1}


def test_conditional_claim_rejects_a_stale_etag(env: Env) -> None:
    ref = summary_ref()
    settings = app.Settings.from_env()
    first = OwnershipMarker.claim(ref, 1, START)
    assert app._claim(env.s3, settings, first, None)
    assert not app._claim(env.s3, settings, first, None)
    assert not app._claim(env.s3, settings, OwnershipMarker.claim(ref, 2, START), '"stale"')


def test_exhausted_attempts_fail_the_record_visibly(env: Env) -> None:
    ref = summary_ref()
    env.previous_attempt(ref, MAX_ATTEMPTS_PER_RUN, "FAILED")

    response = env.handle(summary_ref(redrive="again"))

    assert response == {"batchItemFailures": [{"itemIdentifier": "m0"}]}
    assert env.sfn.started() == []
    assert env.metrics() == {"WorkflowStartFailures": 1}


def test_unreadable_or_foreign_marker_is_never_overwritten(env: Env) -> None:
    key = marker_key(ACCOUNT, DISPLAY)
    env.s3.put_object(Bucket=RAW, Key=key, Body=b"{not json")
    other = summary_ref(display="170000000002")
    foreign = OwnershipMarker.claim(other, 1, START).to_document()
    env.s3.put_object(Bucket=RAW, Key=marker_key(ACCOUNT, "170000000003"), Body=json.dumps(foreign))

    response = env.handle(summary_ref(), summary_ref(display="170000000003"))

    assert response == {"batchItemFailures": [{"itemIdentifier": "m0"}, {"itemIdentifier": "m1"}]}
    assert env.s3.get_object(Bucket=RAW, Key=key)["Body"].read() == b"{not json"
    assert env.sfn.started() == []
    assert env.metrics() == {"WorkflowStartFailures": 2}


def test_mixed_batch_fails_only_bad_records_and_failed_records_retry(env: Env) -> None:
    good, throttled = summary_ref(), summary_ref(display="170000000002")
    original_start = env.sfn.start_execution

    def throttle_second(**kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        if kwargs["name"] == name(1, display="170000000002"):
            raise sfn_error("ThrottlingException", "StartExecution")
        return original_start(**kwargs)

    env.sfn.start_execution = throttle_second  # type: ignore[method-assign]
    response = env.handle(good, {"accountId": ACCOUNT}, throttled, "not json")

    assert response == {
        "batchItemFailures": [
            {"itemIdentifier": "m1"},
            {"itemIdentifier": "m2"},
            {"itemIdentifier": "m3"},
        ]
    }
    assert env.metrics() == {
        "WorkflowsStarted": 1,
        "InvalidMessages": 2,
        "WorkflowStartFailures": 3,
    }
    logs = env.stdout + env.logs()

    # SQS redelivers only the failed records; the throttled one now starts, and the claim it
    # wrote before failing is completed rather than abandoned.
    env.sfn.start_execution = original_start  # type: ignore[method-assign]
    assert env.handle(throttled) == {"batchItemFailures": []}
    assert env.handle(good) == {"batchItemFailures": []}
    assert sorted(env.sfn.executions) == sorted([name(1), name(1, display="170000000002")])
    assert env.metrics() == {
        "WorkflowsStarted": 1,
        "OwnershipClaimsCompleted": 1,
        "DuplicateSummaryMessages": 1,
    }
    assert "synthetic" not in logs


def test_status_check_failure_fails_the_record_and_starts_nothing(env: Env) -> None:
    ref = summary_ref()
    env.previous_attempt(ref, 1, "FAILED")
    env.sfn.failures["describe"] = sfn_error("ThrottlingException", "DescribeExecution")

    response = env.handle(summary_ref(NEXT_RUN))

    assert response == {"batchItemFailures": [{"itemIdentifier": "m0"}]}
    assert env.sfn.started() == []
    assert env.metrics() == {"WorkflowStartFailures": 1}


def test_settings_require_every_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAW_BUCKET", RAW)
    monkeypatch.setenv("PROCESSED_BUCKET", PROCESSED)
    monkeypatch.setenv("STATE_MACHINE_ARN", STATE_MACHINE_ARN)
    monkeypatch.setenv("CLAIM_SETTLE_SECONDS", "120")
    assert app.Settings.from_env().claim_settle == timedelta(minutes=2)
    monkeypatch.delenv("STATE_MACHINE_ARN")
    with pytest.raises(KeyError):
        app.Settings.from_env()

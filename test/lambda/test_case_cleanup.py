"""Case Cleanup: resets only abandoned, unfinished cases so a later run can retry them."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from case_cleanup import app
from common import failure_delivery
from common.case_refs import SummaryRef
from common.checkpoints import checkpoint_key
from common.failure_delivery import DeliveryMarker, delivery_marker_key, reconcile_closed_attempt
from common.failures import summary_ref_from_failure, terminal_marker_key
from common.idempotency import persistence_layer
from common.s3_json import write_json
from common.sqs import SqsSendError
from common.workflow import OwnershipMarker, encode_input, execution_input, marker_key
from workflow_kit import STATE_MACHINE_ARN, FakeStepFunctions, sfn_error
from workflow_starter import app as starter_app

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

RAW = "raw-cleanup-test"
PROCESSED = "processed-cleanup-test"
TABLE = "idempotency-cleanup-test"
ACCOUNT = "111122223333"
OTHER_ACCOUNT = "444455556666"
RUN = "20261113T000000Z"
NEXT_RUN = "20261114T000000Z"
RESETTABLE = ("data.json", "communications.json", "workflow.json")


def ref(display: str, account: str = ACCOUNT, run_id: str = RUN) -> SummaryRef:
    return SummaryRef(
        account_id=account,
        case_id=f"case-{account}-2026-{display}",
        display_id=display,
        run_id=run_id,
        communications_version_id="v1",
        communication_count=2,
    )


def key(display: str, name: str, account: str = ACCOUNT) -> str:
    return f"account_number={account}/case_number={display}/{name}"


@dataclass
class Env:
    s3: S3Client
    sfn: FakeStepFunctions
    sqs: Any
    queue_url: str
    kit: Any
    capsys: pytest.CaptureFixture[str]
    clock: list[datetime] = field(default_factory=list)
    remaining_millis: int = 600_000
    stdout: str = ""

    def seed(self, display: str, account: str = ACCOUNT, *, checkpoint: bool = False) -> None:
        """Raw objects of a retrieved, annotated case that has no processed record."""
        for name in ("data.json", "communications.json"):
            self.s3.put_object(Bucket=RAW, Key=key(display, name, account), Body=b"{}")
        if checkpoint:
            self.s3.put_object(
                Bucket=RAW, Key=checkpoint_key(account, display, "summary"), Body=b"{}"
            )

    def claim(self, display: str, status: str | None, attempt: int = 1) -> str:
        case = ref(display)
        marker = OwnershipMarker.claim(case, attempt, self.clock[0] - timedelta(days=2))
        self.s3.put_object(Bucket=RAW, Key=marker.key, Body=json.dumps(marker.to_document()))
        if status is not None:
            self.sfn.add(
                marker.execution_name, status, encode_input(execution_input(case, attempt))
            )
        return marker.execution_name

    def put_delivery(self, display: str, state: str, status: str | None, attempt: int = 1) -> str:
        marker = DeliveryMarker(ref(display), attempt, state, status, "2026-11-13T01:00:00Z")
        self.s3.put_object(Bucket=RAW, Key=marker.key, Body=json.dumps(marker.to_document()))
        return marker.delivery_key

    def delivery(self, display: str, execution: str) -> dict[str, Any]:
        body = self.s3.get_object(Bucket=RAW, Key=delivery_marker_key(ACCOUNT, display, execution))
        document: dict[str, Any] = json.loads(body["Body"].read())
        return document

    def failures(self) -> list[dict[str, Any]]:
        received: list[dict[str, Any]] = self.kit.drain(self.sqs, self.queue_url)
        return received

    def start_next_run(self, display: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        """Deliver the case's summary reference of the next run to the workflow starter."""
        self.kit.create_table(TABLE)
        starter_clients = starter_app.Clients(s3=self.s3, sfn=self.sfn)  # type: ignore[arg-type]
        monkeypatch.setattr(starter_app, "clients", lambda: starter_clients)
        monkeypatch.setattr(starter_app, "now", lambda: self.clock[0])
        persistence_layer.cache_clear()
        try:
            response: dict[str, Any] = starter_app.handler(
                self.kit.event(ref(display, run_id=NEXT_RUN).to_message()), self.kit.context()
            )
        finally:
            persistence_layer.cache_clear()
        return response

    def mark_processed(self, display: str, account: str = ACCOUNT) -> None:
        self.s3.put_object(Bucket=PROCESSED, Key=key(display, "data.json", account), Body=b"{}")

    def keys(self, display: str, account: str = ACCOUNT) -> list[str]:
        prefix = key(display, "", account)
        listing = self.s3.list_objects_v2(Bucket=RAW, Prefix=prefix)
        return sorted(item["Key"].removeprefix(prefix) for item in listing.get("Contents", []))

    def run(self) -> dict[str, Any]:
        context = self.kit.context()
        context._remaining_millis = self.remaining_millis
        result: dict[str, Any] = app.handler({}, context)
        return result

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
    for bucket in (RAW, PROCESSED):
        s3.create_bucket(Bucket=bucket)
        s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    sqs = boto3.client("sqs", region_name="us-east-1")
    queue_url = sqs.create_queue(QueueName="workflow-failures")["QueueUrl"]
    for name, value in {
        "FAILURE_QUEUE_URL": queue_url,
        "RAW_BUCKET": RAW,
        "PROCESSED_BUCKET": PROCESSED,
        "STATE_MACHINE_ARN": STATE_MACHINE_ARN,
        "CLEANUP_GRACE_HOURS": "24",
        "CLAIM_SETTLE_SECONDS": "300",
        "IDEMPOTENCY_TABLE_NAME": TABLE,
    }.items():
        monkeypatch.setenv(name, value)
    sfn = FakeStepFunctions()
    # Objects are stamped with the real time; cleanup runs as if two days later.
    environment = Env(
        s3=s3,
        sfn=sfn,
        sqs=sqs,
        queue_url=queue_url,
        kit=case_collection_kit,
        capsys=capsys,
        clock=[datetime.now(UTC) + timedelta(days=2)],
    )
    clients = app.Clients(s3=s3, sfn=sfn, sqs=sqs)  # type: ignore[arg-type]
    monkeypatch.setattr(app, "clients", lambda: clients)
    monkeypatch.setattr(app, "now", lambda: environment.clock[0])
    monkeypatch.setattr(app, "LIST_PAGE_SIZE", 2)
    return environment


def test_abandoned_case_is_reset_and_reusable_artefacts_are_kept(env: Env) -> None:
    env.seed("1001", checkpoint=True)
    execution = env.claim("1001", "FAILED")
    env.s3.put_object(Bucket=RAW, Key=terminal_marker_key(ACCOUNT, "1001"), Body=b"{}")

    result = env.run()

    assert result == {"evaluated": 1, "reset": 1, "errors": 0, "complete": True}
    assert env.keys("1001") == [
        "checkpoints/summary.json",
        f"failure-deliveries/{execution}.json",
        "terminal-failure.json",
    ]
    assert env.metrics() == {
        "CleanupCasesEvaluated": 1,
        "CleanupCasesReset": 1,
        "CleanupErrors": 0,
        "CleanupObjectsDeleted": 3,
        "FailureDeliveriesSent": 1,
        "FailureDeliveriesPreserved": 1,
    }
    # Earlier versions stay recoverable until lifecycle expiry; only delete markers were added.
    versions = env.s3.list_object_versions(Bucket=RAW, Prefix=key("1001", "data.json"))
    assert len(versions["Versions"]) == 1
    assert len(versions["DeleteMarkers"]) == 1


def test_processed_active_young_and_empty_cases_are_untouched(env: Env) -> None:
    env.seed("2001")
    env.mark_processed("2001")
    env.seed("2002")
    env.claim("2002", "RUNNING")
    env.seed("2003")
    env.claim("2003", "PENDING_REDRIVE")
    env.seed("2004")
    env.claim("2004", "SUCCEEDED")
    env.mark_processed("2004")
    env.s3.put_object(Bucket=RAW, Key=terminal_marker_key(ACCOUNT, "2005"), Body=b"{}")
    before = {d: env.keys(d) for d in ("2001", "2002", "2003", "2004", "2005")}

    result = env.run()

    assert result == {"evaluated": 5, "reset": 0, "errors": 0, "complete": True}
    assert {d: env.keys(d) for d in before} == before
    assert env.failures() == []
    assert env.metrics() == {
        "CleanupCasesEvaluated": 5,
        "CleanupCasesReset": 0,
        "CleanupErrors": 0,
        "CleanupSkippedProcessed": 2,
        "CleanupSkippedActive": 2,
        "CleanupSkippedNothingToReset": 1,
    }


def test_work_inside_the_grace_period_is_untouched(env: Env) -> None:
    env.clock[0] = datetime.now(UTC) + timedelta(hours=23)
    env.seed("3001")

    assert env.run()["reset"] == 0
    assert env.keys("3001") == ["communications.json", "data.json"]
    assert env.metrics()["CleanupSkippedRecent"] == 1


def test_execution_started_after_the_snapshot_is_detected_through_the_marker(env: Env) -> None:
    env.seed("4001")
    execution = env.claim("4001", "FAILED")
    original = app.active_cases

    def snapshot_then_start(*args: Any, **kwargs: Any) -> set[tuple[str, str]]:  # noqa: ANN401
        active = original(*args, **kwargs)
        env.sfn.executions[execution].status = "RUNNING"
        return active

    app_active = pytest.MonkeyPatch()
    app_active.setattr(app, "active_cases", snapshot_then_start)
    try:
        assert env.run()["reset"] == 0
    finally:
        app_active.undo()
    assert env.keys("4001") == ["communications.json", "data.json", "workflow.json"]


@pytest.mark.parametrize(
    "marker_body",
    [b"{not json", json.dumps({"schemaVersion": 1}).encode(), b"x" * 70_000],
    ids=["malformed", "incomplete", "oversized"],
)
def test_unknown_ownership_never_justifies_deletion(env: Env, marker_body: bytes) -> None:
    env.seed("5001")
    env.s3.put_object(Bucket=RAW, Key=marker_key(ACCOUNT, "5001"), Body=marker_body)

    result = env.run()

    assert result == {"evaluated": 1, "reset": 0, "errors": 1, "complete": True}
    assert env.keys("5001") == ["communications.json", "data.json", "workflow.json"]
    metrics = env.metrics()
    assert metrics["CleanupSkippedUnknown"] == 1
    assert metrics["CleanupErrors"] == 1


def test_marker_for_another_case_is_unknown(env: Env) -> None:
    env.seed("5002")
    foreign = OwnershipMarker.claim(ref("5003"), 1, env.clock[0]).to_document()
    env.s3.put_object(Bucket=RAW, Key=marker_key(ACCOUNT, "5002"), Body=json.dumps(foreign))

    assert env.run()["errors"] == 1
    assert "workflow.json" in env.keys("5002")


def test_status_check_failure_leaves_the_case_untouched(env: Env) -> None:
    env.seed("5101")
    env.claim("5101", "FAILED")
    env.sfn.failures["describe"] = sfn_error("ThrottlingException", "DescribeExecution")

    result = env.run()

    assert result["errors"] == 1
    assert env.keys("5101") == ["communications.json", "data.json", "workflow.json"]
    assert "Case status could not be checked; left untouched" in env.kit.logs()


def test_failure_to_list_active_executions_deletes_nothing(env: Env) -> None:
    env.seed("6001")
    env.sfn.failures["list"] = sfn_error("AccessDeniedException", "ListExecutions")

    with pytest.raises(app.CleanupError):
        env.run()

    assert env.keys("6001") == ["communications.json", "data.json"]
    assert env.metrics() == {"CleanupErrors": 1}


def test_running_executions_on_later_pages_are_respected(env: Env) -> None:
    for display in ("7001", "7002", "7003", "7004", "7005"):
        env.seed(display)
        env.claim(display, "RUNNING")
    # Listing pages hold two executions; the marker is removed to prove the snapshot is used.
    env.s3.delete_object(Bucket=RAW, Key=marker_key(ACCOUNT, "7005"))

    assert env.run()["reset"] == 0
    assert env.keys("7005") == ["communications.json", "data.json"]
    assert [call for call in env.sfn.calls if call[0] == "list"] == [
        ("list", "PENDING_REDRIVE"),
        ("list", "RUNNING"),
        ("list", "RUNNING"),
        ("list", "RUNNING"),
    ]


def test_cases_spanning_listing_pages_across_accounts_are_evaluated_once(env: Env) -> None:
    for display in ("8001", "8002", "8003"):
        env.seed(display, checkpoint=True)
    env.seed("8001", OTHER_ACCOUNT)
    env.mark_processed("8002")

    result = env.run()

    assert result == {"evaluated": 4, "reset": 3, "errors": 0, "complete": True}
    assert env.metrics()["CleanupSkippedProcessed"] == 1
    assert env.keys("8002") == ["checkpoints/summary.json", "communications.json", "data.json"]
    assert env.keys("8001") == env.keys("8003") == ["checkpoints/summary.json"]
    assert env.keys("8001", OTHER_ACCOUNT) == []


def test_processed_record_committed_during_cleanup_stops_the_reset(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.seed("9001")
    original = app.Cleaner.processed

    def stale_listing(self: app.Cleaner, account_id: str) -> set[str]:
        found = original(self, account_id)
        env.mark_processed("9001")
        return found

    monkeypatch.setattr(app.Cleaner, "processed", stale_listing)

    assert env.run()["reset"] == 0
    assert env.keys("9001") == ["communications.json", "data.json"]
    assert env.metrics()["CleanupSkippedProcessed"] == 1


def test_case_rewritten_by_a_new_run_during_cleanup_is_not_reset(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.seed("9101")
    original = app.Cleaner.is_processed_now

    def new_run_writes(self: app.Cleaner, case: app.CaseObjects) -> bool:
        env.s3.put_object(Bucket=RAW, Key=key("9101", "data.json"), Body=b'{"run": "new"}')
        return original(self, case)

    monkeypatch.setattr(app.Cleaner, "is_processed_now", new_run_writes)

    result = env.run()

    assert result["reset"] == 0
    assert env.keys("9101") == ["communications.json", "data.json"]
    body = env.s3.get_object(Bucket=RAW, Key=key("9101", "data.json"))["Body"].read()
    assert body == b'{"run": "new"}'
    assert env.metrics()["CleanupDeleteConflicts"] == 1


def test_time_budget_saves_a_cursor_and_the_next_run_resumes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    for display in ("9201", "9202", "9203"):
        env.seed(display)
    calls = {"count": 0}

    def budget(self: Any) -> int:  # noqa: ANN401
        calls["count"] += 1
        return 600_000 if calls["count"] <= 2 else 1_000

    monkeypatch.setattr(type(env.kit.context()), "get_remaining_time_in_millis", budget)
    first = env.run()

    assert first == {"evaluated": 2, "reset": 2, "errors": 0, "complete": False}
    cursor = json.loads(env.s3.get_object(Bucket=RAW, Key=app.CURSOR_KEY)["Body"].read())
    assert cursor["startAfter"] == key("9202", "data.json")
    assert env.metrics()["CleanupIncomplete"] == 1
    assert env.keys("9203") == ["communications.json", "data.json"]

    monkeypatch.setattr(type(env.kit.context()), "get_remaining_time_in_millis", lambda _: 600_000)
    second = env.run()

    assert second == {"evaluated": 1, "reset": 1, "errors": 0, "complete": True}
    assert env.keys("9203") == []
    assert "cleanup-state/cursor.json" not in [
        o["Key"] for o in env.s3.list_objects_v2(Bucket=RAW).get("Contents", [])
    ]


def test_cleanup_then_next_run_recovers_the_case(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.seed("9301", checkpoint=True)
    failed = env.claim("9301", "FAILED")
    assert env.run()["reset"] == 1
    assert env.keys("9301") == ["checkpoints/summary.json", f"failure-deliveries/{failed}.json"]
    assert len(env.failures()) == 1

    # The next daily run retrieves the case again (no processed record) and the starter
    # starts a fresh attempt under the new run's identity; the confirmed failure record of the
    # earlier attempt never stops that retry.
    env.seed("9301")
    response = env.start_next_run("9301", monkeypatch)

    assert response == {"batchItemFailures": []}
    assert env.sfn.status(failed) == "FAILED"
    new_name = f"{ACCOUNT}-9301-{NEXT_RUN}-a1"
    assert env.sfn.status(new_name) == "RUNNING"


def test_grace_hours_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAILURE_QUEUE_URL", "https://sqs.example/queue")
    monkeypatch.setenv("RAW_BUCKET", RAW)
    monkeypatch.setenv("PROCESSED_BUCKET", PROCESSED)
    monkeypatch.setenv("STATE_MACHINE_ARN", STATE_MACHINE_ARN)
    monkeypatch.setenv("CLEANUP_GRACE_HOURS", "0")
    with pytest.raises(ValueError, match="at least 1"):
        app.Settings.from_env()


# Failure-delivery reconciliation: a closed failed attempt reaches the workflow failure queue
# before its ownership marker can be removed, at least once and under a stable delivery key.


@pytest.mark.parametrize("status", ["TIMED_OUT", "ABORTED", "FAILED"])
def test_closed_attempt_without_a_confirmed_record_is_delivered_before_reset(
    env: Env, status: str
) -> None:
    # TIMED_OUT and ABORTED bypass every task Catch; FAILED without a confirmed marker is an
    # attempt whose failure handler exhausted its own retries.
    env.seed("1101", checkpoint=True)
    execution = env.claim("1101", status)

    result = env.run()

    assert result == {"evaluated": 1, "reset": 1, "errors": 0, "complete": True}
    [failure] = env.failures()
    assert failure == {
        "schemaVersion": 1,
        "failureType": "WorkflowExecutionClosed",
        "stage": "workflow",
        "retryable": True,
        "failedAt": failure["failedAt"],
        **ref("1101").to_message(),
        "executionStatus": status,
        "executionName": execution,
        "attempt": 1,
        "deliveryKey": execution,
    }
    # The record redrives to the summary queue like any other failure record.
    assert summary_ref_from_failure(failure) == ref("1101")
    assert env.delivery("1101", execution)["status"] == "sent"
    assert env.keys("1101") == ["checkpoints/summary.json", f"failure-deliveries/{execution}.json"]
    metrics = env.metrics()
    assert metrics["FailureDeliveriesSent"] == 1
    assert metrics["FailureDeliveriesPreserved"] == 1
    assert "FailureDeliveryErrors" not in metrics


def test_record_confirmed_by_the_failure_handler_is_not_sent_again(env: Env) -> None:
    env.seed("1201")
    execution = env.claim("1201", "FAILED")
    env.put_delivery("1201", "sent", status=None)

    assert env.run()["reset"] == 1
    assert env.failures() == []
    assert env.delivery("1201", execution)["status"] == "sent"
    assert "FailureDeliveriesSent" not in env.metrics()


def test_send_failure_keeps_the_owner_and_pending_evidence_for_the_next_run(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.seed("1301")
    execution = env.claim("1301", "TIMED_OUT")

    def unavailable(*_: Any, **__: Any) -> int:  # noqa: ANN401
        raise SqsSendError(1, ("ServiceUnavailable",))

    with monkeypatch.context() as patch:
        patch.setattr(failure_delivery, "send_messages", unavailable)
        result = env.run()

    assert result == {"evaluated": 1, "reset": 0, "errors": 1, "complete": True}
    assert env.failures() == []
    assert env.delivery("1301", execution)["status"] == "pending"
    assert "workflow.json" in env.keys("1301")
    metrics = env.metrics()
    assert metrics["FailureDeliveryErrors"] == 1
    assert metrics["CleanupErrors"] == 1

    second = env.run()

    assert second == {"evaluated": 1, "reset": 1, "errors": 0, "complete": True}
    assert [f["deliveryKey"] for f in env.failures()] == [execution]
    assert env.delivery("1301", execution)["status"] == "sent"


def test_evidence_that_cannot_be_written_never_allows_deletion(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.seed("1401")
    env.claim("1401", "ABORTED")

    def unavailable(*_: Any, **__: Any) -> None:  # noqa: ANN401
        raise sfn_error("InternalError", "PutObject", 500)

    monkeypatch.setattr(failure_delivery, "write_json", unavailable)
    result = env.run()

    assert result["reset"] == 0
    assert result["errors"] == 1
    assert env.failures() == []
    assert env.keys("1401") == ["communications.json", "data.json", "workflow.json"]


def test_crash_after_send_repeats_the_record_with_the_same_key_then_stops(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.seed("1501")
    execution = env.claim("1501", "TIMED_OUT")
    original = write_json

    def lose_confirmation(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if kwargs.get("if_match"):
            raise sfn_error("InternalError", "PutObject", 500)
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(failure_delivery, "write_json", lose_confirmation)
        first = env.run()

    # The record was sent, so the owner is released; the unconfirmed marker stays pending.
    assert first["reset"] == 1
    assert env.delivery("1501", execution)["status"] == "pending"
    assert env.metrics()["FailureDeliveriesUnconfirmed"] == 1

    env.run()
    env.run()

    keys = [f["deliveryKey"] for f in env.failures()]
    assert keys == [execution, execution]
    assert env.delivery("1501", execution)["status"] == "sent"


def test_repeated_reconciliation_sends_one_record(env: Env) -> None:
    env.seed("1601")
    env.claim("1601", "TIMED_OUT")

    for _ in range(3):
        env.run()

    assert len(env.failures()) == 1


def test_pending_evidence_kept_by_the_starter_is_delivered_without_touching_active_work(
    env: Env,
) -> None:
    # The starter replaced the owner of a timed-out attempt with a running new attempt.
    env.seed("1701")
    env.claim("1701", "RUNNING", attempt=2)
    timed_out = env.put_delivery("1701", "pending", status="TIMED_OUT")
    # A processed case may also hold evidence from an earlier failed attempt.
    env.seed("1702")
    env.mark_processed("1702")
    earlier = env.put_delivery("1702", "pending", status="FAILED")

    result = env.run()

    assert result["reset"] == 0
    assert sorted(f["deliveryKey"] for f in env.failures()) == sorted([timed_out, earlier])
    assert "workflow.json" in env.keys("1701")
    assert env.keys("1702") == [
        "communications.json",
        "data.json",
        f"failure-deliveries/{earlier}.json",
    ]


def test_unreadable_pending_evidence_is_counted_and_kept(env: Env) -> None:
    env.seed("1801")
    env.mark_processed("1801")
    bad = key("1801", "failure-deliveries/" + f"{ACCOUNT}-1801-{RUN}-a1.json")
    env.s3.put_object(Bucket=RAW, Key=bad, Body=b"{bad")

    result = env.run()

    assert result["errors"] == 1
    assert env.failures() == []
    assert env.metrics()["FailureDeliveryErrors"] == 1
    assert "Pending failure record could not be delivered; kept" in env.kit.logs()


def test_unknown_or_unavailable_status_never_sends_a_record(env: Env) -> None:
    env.seed("1901")
    env.claim("1901", "FAILED")
    env.sfn.failures["describe"] = sfn_error("ThrottlingException", "DescribeExecution")
    env.seed("1902")
    foreign = OwnershipMarker.claim(ref("1903"), 1, env.clock[0]).to_document()
    env.s3.put_object(Bucket=RAW, Key=marker_key(ACCOUNT, "1902"), Body=json.dumps(foreign))

    assert env.run()["errors"] == 2
    assert env.failures() == []


def test_new_run_racing_the_reset_keeps_one_record_and_its_files(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.seed("2101")
    execution = env.claim("2101", "TIMED_OUT")
    original = reconcile_closed_attempt

    def new_run_arrives(*args: Any, **kwargs: Any) -> tuple[str, bool]:  # noqa: ANN401
        outcome = original(*args, **kwargs)
        # The next run rewrites the case and its starter recovers the timed-out attempt.
        for name in ("data.json", "communications.json"):
            env.s3.put_object(Bucket=RAW, Key=key("2101", name), Body=b'{"run": "next"}')
        env.start_next_run("2101", monkeypatch)
        return outcome

    monkeypatch.setattr(app, "reconcile_closed_attempt", new_run_arrives)
    result = env.run()

    assert result["reset"] == 0
    assert env.metrics()["CleanupDeleteConflicts"] == 1
    assert env.keys("2101") == [
        "communications.json",
        "data.json",
        f"failure-deliveries/{execution}.json",
        "workflow.json",
    ]
    assert [f["deliveryKey"] for f in env.failures()] == [execution]
    assert env.sfn.status(f"{ACCOUNT}-2101-{NEXT_RUN}-a1") == "RUNNING"


def test_delivery_markers_are_bookkeeping_not_case_work() -> None:
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    case = app.CaseObjects(ACCOUNT, "1001")
    case.objects["data.json"] = app.StoredObject("k1", "e1", stamp)
    case.objects["failure-deliveries/x.json"] = app.StoredObject("k2", "e2", stamp)
    assert [item.key for item in case.work_objects] == ["k1"]

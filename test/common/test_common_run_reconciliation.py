"""Failure-delivery markers and run markers: contracts, conditional writes and malformed input."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from common.case_refs import SummaryRef
from common.failure_delivery import (
    DeliveryMarker,
    DeliveryTally,
    InvalidDeliveryMarkerError,
    delivery_marker_key,
    execution_from_case_file,
    preserve_pending,
    read_delivery_marker,
    record_sent,
)
from common.run_markers import (
    AccountMarker,
    CaseMarker,
    InvalidRunMarkerError,
    dispatching_account,
    retrieved_account,
    skipped_account,
)
from common.workflow import encode_input, execution_input, parse_execution_input

ACCOUNT = "111122223333"
RUN = "20261113T000000Z"
NOW = datetime(2026, 11, 13, tzinfo=UTC)
REF = SummaryRef(
    account_id=ACCOUNT,
    case_id="case-abc",
    display_id="1001",
    run_id=RUN,
    communications_version_id="v1",
    communication_count=2,
)
NAME = f"{ACCOUNT}-1001-{RUN}-a1"


def pending() -> DeliveryMarker:
    return DeliveryMarker(REF, 1, "pending", "TIMED_OUT", "2026-11-13T00:00:00Z")


def test_delivery_marker_round_trips_and_is_keyed_by_the_execution() -> None:
    marker = pending()
    assert marker.delivery_key == NAME
    assert marker.key == delivery_marker_key(ACCOUNT, "1001", NAME)
    assert marker.key == f"account_number={ACCOUNT}/case_number=1001/failure-deliveries/{NAME}.json"
    assert DeliveryMarker.from_document(marker.to_document()) == marker
    assert execution_from_case_file(f"failure-deliveries/{NAME}.json") == NAME
    assert execution_from_case_file("failure-deliveries/../x.json") is None
    assert execution_from_case_file("workflow.json") is None


@pytest.mark.parametrize(
    "change",
    [
        {"extra": 1},
        {"schemaVersion": 2},
        {"attempt": True},
        {"attempt": "1"},
        {"status": "done"},
        {"executionStatus": "SUCCEEDED"},
        {"recordedAt": 5},
        {"sentAt": 5},
        {"case": {"accountId": "bad"}},
        {"deliveryKey": "other"},
        {"attempt": 0},
    ],
)
def test_malformed_delivery_markers_are_rejected(change: dict[str, Any]) -> None:
    document = {**pending().to_document(), **change}
    with pytest.raises(InvalidDeliveryMarkerError):
        DeliveryMarker.from_document(document)


def test_preserve_keeps_the_first_marker_and_rejects_unreadable_ones(bucket: str) -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    first, created = preserve_pending(
        s3, bucket, REF, attempt=1, execution_status="TIMED_OUT", now=NOW
    )
    again, created_again = preserve_pending(
        s3, bucket, REF, attempt=1, execution_status="FAILED", now=NOW
    )
    assert (created, created_again) == (True, False)
    assert again.marker.execution_status == "TIMED_OUT"
    assert again.etag == first.etag

    record_sent(s3, bucket, first.marker, NOW)
    stored = read_delivery_marker(s3, bucket, ACCOUNT, "1001", NAME)
    assert stored is not None
    assert stored.marker.status == "sent"

    with pytest.raises(ValueError, match="closed failed"):
        preserve_pending(s3, bucket, REF, attempt=1, execution_status="SUCCEEDED", now=NOW)

    s3.put_object(Bucket=bucket, Key=pending().key, Body=b"{bad")
    with pytest.raises(InvalidDeliveryMarkerError):
        read_delivery_marker(s3, bucket, ACCOUNT, "1001", NAME)
    other = DeliveryMarker(REF, 2, "pending", "FAILED", "t")
    s3.put_object(Bucket=bucket, Key=pending().key, Body=json.dumps(other.to_document()))
    with pytest.raises(InvalidDeliveryMarkerError, match="another execution"):
        read_delivery_marker(s3, bucket, ACCOUNT, "1001", NAME)


def test_delivery_tally_reports_only_non_zero_counts() -> None:
    tally = DeliveryTally()
    assert tally.metrics() == {}
    tally.add("sent", created=True)
    tally.add("sentUnconfirmed")
    tally.add("alreadySent")
    assert tally.metrics() == {
        "FailureDeliveriesSent": 2,
        "FailureDeliveriesUnconfirmed": 1,
        "FailureDeliveriesPreserved": 1,
    }
    assert tally.already_sent == 1


def test_execution_input_is_recognised_only_for_its_own_execution() -> None:
    raw = encode_input(execution_input(REF, 1))
    assert parse_execution_input(NAME, raw) == (REF, 1)
    assert parse_execution_input(NAME.replace("-a1", "-a2"), raw) is None
    assert parse_execution_input(NAME, None) is None
    assert parse_execution_input(NAME, "{bad") is None
    tampered = json.loads(raw)
    tampered["extra"] = True
    assert parse_execution_input(NAME, json.dumps(tampered)) is None


def test_run_markers_round_trip() -> None:
    cases = {"1001": "case-1001", "1002": "case-1002"}
    retrieved = retrieved_account(RUN, ACCOUNT, cases)
    skipped = skipped_account(RUN, ACCOUNT, "AccessDenied")
    dispatching = dispatching_account(RUN, ACCOUNT, cases)
    skipped_after_dispatch = skipped_account(RUN, ACCOUNT, "SubscriptionRequired", cases)
    for marker in (retrieved, skipped, dispatching, skipped_after_dispatch):
        assert AccountMarker.from_document(marker.to_document()) == marker
    assert skipped_after_dispatch.dispatched == cases
    assert retrieved.to_document()["caseIds"] == ["case-1001", "case-1002"]
    assert retrieved.key == f"runs/{RUN}/accounts/{ACCOUNT}.json"
    handoff = CaseMarker(RUN, ACCOUNT, "1001", "started", NAME)
    assert CaseMarker.from_document(handoff.to_document()) == handoff
    assert handoff.key == f"runs/{RUN}/cases/{ACCOUNT}/1001.json"


@pytest.mark.parametrize(
    "change",
    [
        {"schemaVersion": 2},
        {"cases": "1001"},
        {"cases": ["../x"]},
        {"runId": "yesterday"},
        {"outcome": "lost"},
        {"outcome": "skipped", "skipReason": None},
        {"outcome": "skipped", "skipReason": "Access Denied!"},
        {"skipReason": "AccessDenied"},
        {"outcome": "dispatching", "skipReason": "AccessDenied"},
        {"caseIds": []},
        {"caseIds": "case-1001"},
        {"caseIds": ["bad case id!"]},
        {"cases": ["1001", "1001"], "caseIds": ["case-1001", "case-1001"]},
    ],
)
def test_malformed_account_markers_are_rejected(change: dict[str, Any]) -> None:
    document = {**retrieved_account(RUN, ACCOUNT, {"1001": "case-1001"}).to_document(), **change}
    with pytest.raises(InvalidRunMarkerError):
        AccountMarker.from_document(document)


@pytest.mark.parametrize(
    "change",
    [
        {"outcome": "annotated"},
        {"executionName": None},
        {"executionName": "bad name"},
        {"outcome": "notFound"},
        {"displayId": "../x"},
    ],
)
def test_malformed_case_markers_are_rejected(change: dict[str, Any]) -> None:
    document = {**CaseMarker(RUN, ACCOUNT, "1001", "started", NAME).to_document(), **change}
    with pytest.raises(InvalidRunMarkerError):
        CaseMarker.from_document(document)

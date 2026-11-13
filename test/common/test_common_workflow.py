"""Execution identity, ownership markers, failure messages and conditional S3 helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from common.case_refs import InvalidReferenceError, SummaryRef
from common.failures import (
    TerminalMarker,
    failure_message,
    summary_ref_from_failure,
    terminal_marker_key,
)
from common.s3_json import (
    delete_if_unchanged,
    key_exists_by_listing,
    read_json_with_etag,
)
from common.workflow import (
    MAX_ATTEMPTS_PER_RUN,
    InvalidOwnershipMarkerError,
    OwnershipMarker,
    execution_arn,
    execution_input,
    execution_name,
    marker_key,
    parse_execution_name,
)

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
STATE_MACHINE = "arn:aws:states:us-east-1:123456789012:stateMachine:CaseAnalysis-dev"


def test_execution_names_round_trip_and_reject_out_of_range_attempts() -> None:
    name = execution_name(ACCOUNT, "1001", RUN, 7)
    identity = parse_execution_name(name)
    assert identity is not None
    assert (identity.account_id, identity.case_component, identity.run_id, identity.attempt) == (
        ACCOUNT,
        "1001",
        RUN,
        7,
    )
    assert parse_execution_name("manual-execution") is None
    for attempt in (0, MAX_ATTEMPTS_PER_RUN + 1):
        with pytest.raises(ValueError, match="attempt"):
            execution_name(ACCOUNT, "1001", RUN, attempt)
    with pytest.raises(ValueError, match="run"):
        execution_name(ACCOUNT, "1001", "not-a-run", 1)


def test_execution_arn_is_derived_from_the_state_machine_arn() -> None:
    assert execution_arn(STATE_MACHINE, "n1") == (
        "arn:aws:states:us-east-1:123456789012:execution:CaseAnalysis-dev:n1"
    )
    with pytest.raises(ValueError, match="state machine"):
        execution_arn("arn:aws:lambda:us-east-1:123456789012:function:x", "n1")


def test_redrive_id_is_optional_validated_and_excluded_from_execution_input() -> None:
    redriven = SummaryRef.parse({**REF.to_message(), "redriveId": "ops-42"})
    assert redriven.redrive_id == "ops-42"
    assert redriven.idempotency_key() != REF.idempotency_key()
    assert execution_input(redriven, 1) == execution_input(REF, 1)
    with pytest.raises(InvalidReferenceError, match="redriveId"):
        SummaryRef.parse({**REF.to_message(), "redriveId": "bad id!"})


def test_marker_round_trips_and_rejects_tampering() -> None:
    marker = OwnershipMarker.claim(REF, 3, NOW)
    document = marker.to_document()
    assert OwnershipMarker.from_document(document) == marker
    assert marker.key == marker_key(ACCOUNT, "1001")

    tampered: list[dict[str, Any]] = [
        {**document, "extra": 1},
        {**document, "schemaVersion": 2},
        {**document, "attempt": True},
        {**document, "input": "x"},
        {**document, "claimedAt": "yesterday"},
        {**document, "claimedAt": "2026-11-13T00:00:00"},
        {**document, "runId": "20261114T000000Z"},
        {**document, "executionName": execution_name(ACCOUNT, "1001", RUN, 4)},
        {**document, "input": {**document["input"], "case": {"accountId": ACCOUNT}}},
        {**document, "attempt": 4},
    ]
    for candidate in tampered:
        with pytest.raises(InvalidOwnershipMarkerError):
            OwnershipMarker.from_document(candidate)


def test_failure_messages_carry_references_only_and_redrive() -> None:
    message = failure_message(
        SummaryRef.parse({**REF.to_message(), "redriveId": "r1"}),
        failure_type="WorkflowTaskFailed",
        stage="rca",
        failed_at="2026-11-13T00:00:00Z",
        retryable=True,
        errorCode=None,
        errorType="BedrockThrottlingError",
    )
    assert "redriveId" not in message
    assert "errorCode" not in message
    assert summary_ref_from_failure(message) == REF
    assert terminal_marker_key(ACCOUNT, "1001").endswith("case_number=1001/terminal-failure.json")


def test_terminal_marker_parsing() -> None:
    marker = TerminalMarker("ConversationTooLarge", "digest", "2026-11-13T00:00:00Z", RUN)
    assert TerminalMarker.from_document(marker.to_document()) == marker
    assert marker.matches("ConversationTooLarge", "digest")
    assert not marker.matches("ConversationTooLarge", "other")
    assert TerminalMarker.from_document({"schemaVersion": 2}) is None
    assert TerminalMarker.from_document({**marker.to_document(), "runId": 5}) is None


def test_conditional_delete_and_etag_reads(bucket: str) -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    written = s3.put_object(Bucket=bucket, Key="a/doc.json", Body=json.dumps({"v": 1}).encode())
    stored = read_json_with_etag(s3, bucket, "a/doc.json")
    assert stored is not None
    assert stored.document == {"v": 1}
    assert stored.etag == written["ETag"]
    assert read_json_with_etag(s3, bucket, "a/missing.json") is None
    assert key_exists_by_listing(s3, bucket, "a/doc.json")
    assert not key_exists_by_listing(s3, bucket, "a/doc")

    s3.put_object(Bucket=bucket, Key="a/doc.json", Body=b'{"v": 2}')
    assert not delete_if_unchanged(s3, bucket, "a/doc.json", stored.etag)
    assert read_json_with_etag(s3, bucket, "a/doc.json") is not None
    current = read_json_with_etag(s3, bucket, "a/doc.json")
    assert current is not None
    assert delete_if_unchanged(s3, bucket, "a/doc.json", current.etag)
    assert read_json_with_etag(s3, bucket, "a/doc.json") is None

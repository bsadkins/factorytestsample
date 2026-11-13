"""Processed-record validation: exact field set, types, dates, taxonomy values and provenance."""

from __future__ import annotations

from typing import Any

import pytest
from common.checkpoints import Checkpoint
from common.processed_record import (
    PROCESSED_FIELDS,
    IncompleteRecordError,
    build_processed_record,
    validate_processed_record,
)

SUMMARY = (
    "The customer reported intermittent HTTP 503 errors from an Application Load Balancer in "
    "us-east-1 after deploying a new release of a web application. Target group health checks "
    "were failing because the updated container image listened on port 8081 instead of 8080. "
    "Amazon ECS repeatedly replaced the unhealthy tasks, which reduced capacity and increased "
    "the error rate during peak traffic. The engineer advised aligning the container port with "
    "the target group and enabling the ECS deployment circuit breaker. After the task definition "
    "was corrected and redeployed, health checks passed and the errors stopped."
)
HAIKU = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
VERSION = "pv-0123456789abcdef"


def checkpoint(stage: str, output: dict[str, Any], **overrides: str) -> Checkpoint:
    fields: dict[str, Any] = {
        "stage": stage,
        "account_id": "111122223333",
        "case_id": "case-1",
        "display_id": "1001",
        "model_id": HAIKU,
        "prompt_version": VERSION,
        "input_digest": "sha256:" + "a" * 64,
        "completed_at": "2026-11-13T01:00:00Z",
        "output": output,
    }
    fields.update(overrides)
    return Checkpoint(**fields)


def stages(**overrides: str) -> tuple[Checkpoint, Checkpoint, Checkpoint]:
    return (
        checkpoint("summary", {"Case_Summary": SUMMARY}),
        checkpoint(
            "rca",
            {"RCA_Category": "Throttling", "RCA_Reason": "Rate limits.", "fallback": False},
            **overrides,
        ),
        checkpoint(
            "lifecycle",
            {
                "Lifecycle_Category": "Unclassified",
                "Lifecycle_Reason": "Fallback.",
                "fallback": True,
            },
        ),
    )


RAW = {
    "accountId": "111122223333",
    "caseId": "case-1",
    "displayId": "1001",
    "subject": "Synthetic subject",
    "status": "resolved",
    "serviceCode": "amazon-dynamodb",
    "categoryCode": "throttling",
    "severityCode": "normal",
    "submittedBy": "someone@example.com",
    "timeCreated": "2026-06-01T10:00:00.000Z",
    "language": "en",
    "Case_Retrieval_Date": "2026-11-13T00:05:12Z",
}


def test_build_merges_exactly_the_eighteen_fields() -> None:
    record = build_processed_record(RAW, *stages())
    assert list(record) == list(PROCESSED_FIELDS)
    assert len(record) == 18
    assert "status" not in record
    assert "language" not in record
    assert record["Lifecycle_Category"] == "Unclassified"
    assert record["Model_Id"] == HAIKU
    assert record["Prompt_Version"] == VERSION


def test_mixed_provenance_is_rejected() -> None:
    with pytest.raises(IncompleteRecordError, match="mixed_provenance"):
        build_processed_record(RAW, *stages(prompt_version="pv-ffffffffffffffff"))


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"extra": "x"}, "field_set"),
        ({"Case_Summary": "Too short."}, "invalid_summary"),
        ({"RCA_Category": "Throttled"}, "invalid_category"),
        ({"RCA_Reason": "x" * 501}, "reason_too_long"),
        ({"RCA_Retrieval_Date": "2026-11-13"}, "invalid_date"),
        ({"Model_Id": "anthropic.claude-v2"}, "unsupported_model"),
        ({"Prompt_Version": "v1"}, "invalid_prompt_version"),
        ({"caseId": ""}, "empty"),
        ({"Lifecycle_Reason": None}, "not_a_string"),
    ],
)
def test_validation_rejects_incomplete_or_inconsistent_records(
    change: dict[str, Any], code: str
) -> None:
    record = {**build_processed_record(RAW, *stages()), **change}
    with pytest.raises(IncompleteRecordError) as info:
        validate_processed_record(record)
    assert info.value.code == code
    assert "x" * 20 not in str(info.value)


def test_missing_raw_field_is_rejected() -> None:
    raw = {k: v for k, v in RAW.items() if k != "Case_Retrieval_Date"}
    with pytest.raises(IncompleteRecordError, match="raw_metadata_fields"):
        build_processed_record(raw, *stages())


@pytest.mark.parametrize("emphasised", ["*customer*", "_customer_"])
def test_inline_markdown_emphasis_in_the_summary_is_rejected(emphasised: str) -> None:
    record = build_processed_record(RAW, *stages())
    record["Case_Summary"] = SUMMARY.replace("customer", emphasised, 1)
    with pytest.raises(IncompleteRecordError) as info:
        validate_processed_record(record)
    assert info.value.code == "invalid_summary"


def test_technical_underscores_and_arithmetic_in_the_summary_are_accepted() -> None:
    technical = SUMMARY.replace("port 8081", "port 8081 on ec2_instance_id hosts of a * b/2*3", 1)
    assert technical != SUMMARY
    summary = checkpoint("summary", {"Case_Summary": technical})
    record = build_processed_record(RAW, summary, *stages()[1:])
    assert record["Case_Summary"] == technical

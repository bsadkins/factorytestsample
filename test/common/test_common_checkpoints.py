"""Stage checkpoint keys, validation and conditional storage."""

from __future__ import annotations

import json
from typing import Any

import boto3
import pytest
from common.case_refs import SummaryRef
from common.checkpoints import (
    Checkpoint,
    Expectation,
    checkpoint_key,
    load_checkpoint,
    output_issue,
    save_checkpoint,
)

REF = SummaryRef(
    account_id="111122223333",
    case_id="case-1",
    display_id="1001",
    run_id="20261113T000000Z",
    communications_version_id=None,
    communication_count=1,
)
HAIKU = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


def rca(category: str = "Throttling", **overrides: Any) -> Checkpoint:  # noqa: ANN401
    fields: dict[str, Any] = {
        "stage": "rca",
        "account_id": REF.account_id,
        "case_id": REF.case_id,
        "display_id": REF.display_id,
        "model_id": HAIKU,
        "prompt_version": "pv-0123456789abcdef",
        "input_digest": "sha256:abc",
        "completed_at": "2026-11-13T01:00:00Z",
        "output": {"RCA_Category": category, "RCA_Reason": "Rate limits.", "fallback": False},
    }
    fields.update(overrides)
    return Checkpoint(**fields)


def expect(digest: str | None = "sha256:abc") -> Expectation:
    return Expectation("rca", REF, HAIKU, "pv-0123456789abcdef", digest)


def test_checkpoint_keys_live_beside_the_raw_case() -> None:
    assert (
        checkpoint_key("111122223333", "1001", "lifecycle")
        == "account_number=111122223333/case_number=1001/checkpoints/lifecycle.json"
    )
    with pytest.raises(ValueError, match="Unknown stage"):
        checkpoint_key("111122223333", "1001", "update")
    with pytest.raises(ValueError, match="display ID"):
        checkpoint_key("111122223333", "../x", "rca")


@pytest.mark.parametrize(
    ("stage", "output", "issue"),
    [
        ("summary", [], "output_not_object"),
        ("summary", {"Case_Summary": 1}, "summary_fields"),
        ("summary", {"Case_Summary": "Short.", "extra": 1}, "summary_fields"),
        ("summary", {"Case_Summary": "Short."}, "summary_invalid"),
        ("rca", {"RCA_Category": "Throttling", "RCA_Reason": "x"}, "classification_fields"),
        ("rca", {"RCA_Category": "Nope", "RCA_Reason": "x", "fallback": False}, "category_invalid"),
        (
            "rca",
            {"RCA_Category": "Unclassified", "RCA_Reason": "x", "fallback": False},
            "fallback_mismatch",
        ),
        (
            "rca",
            {"RCA_Category": "Throttling", "RCA_Reason": "", "fallback": False},
            "reason_invalid",
        ),
        (
            "lifecycle",
            {"Lifecycle_Category": "CICD", "Lifecycle_Reason": "x", "fallback": 0},
            "fallback_mismatch",
        ),
    ],
)
def test_output_validation(stage: str, output: object, issue: str) -> None:
    assert output_issue(stage, output) == issue


def test_save_and_load_round_trip_with_conditional_create(bucket: str) -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    assert load_checkpoint(s3, bucket, expect()) is None

    first = save_checkpoint(s3, bucket, rca(), REF)
    second = save_checkpoint(s3, bucket, rca("Feature Request"), REF)

    assert second == first
    assert load_checkpoint(s3, bucket, expect()) == first
    assert load_checkpoint(s3, bucket, expect(digest=None)) == first
    assert load_checkpoint(s3, bucket, expect(digest="sha256:other")) is None


def test_stale_checkpoint_is_replaced(bucket: str) -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    save_checkpoint(
        s3, bucket, rca(model_id="global.anthropic.claude-sonnet-4-5-20250929-v1:0"), REF
    )

    stored = save_checkpoint(s3, bucket, rca("Feature Request"), REF)

    assert stored.output["RCA_Category"] == "Feature Request"
    assert load_checkpoint(s3, bucket, expect()) == stored


@pytest.mark.parametrize(
    "body",
    [b"not json", json.dumps({"schemaVersion": 1}).encode(), json.dumps([1]).encode()],
    ids=["invalid-json", "wrong-shape", "not-an-object"],
)
def test_unreadable_or_malformed_checkpoint_is_ignored(bucket: str, body: bytes) -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.put_object(Bucket=bucket, Key=rca().key, Body=body)
    assert load_checkpoint(s3, bucket, expect()) is None


def test_invalid_checkpoint_is_never_stored(bucket: str) -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    with pytest.raises(ValueError, match="invalid rca checkpoint"):
        save_checkpoint(s3, bucket, rca("Not a category"), REF)

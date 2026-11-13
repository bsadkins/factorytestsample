"""UpdateCaseMetadata handler and the complete Summary -> RCA -> Lifecycle -> Update sequence:
one complete 18-field processed record, committed conditionally and only after validation."""

from __future__ import annotations

import json
from typing import Any

import pytest
from analysis_kit import AnalysisEnv
from botocore.exceptions import ClientError
from common.analysis import IncompleteAnalysisError
from common.processed_record import PROCESSED_FIELDS
from common.s3_json import WriteResult, write_json
from common.templates import prompt_version
from types_boto3_s3.client import S3Client
from update_case_metadata import app as update_app

RCA_REPLY = '{"RCA_Category": "Customer Release", "RCA_Reason": "The release changed the port."}'
LIFECYCLE_REPLY = (
    '```json\n{"Lifecycle_Category": "CICD", "Lifecycle_Reason": "A pipeline check would have '
    'caught the {port} change."}\n```'
)


def analysed(env: AnalysisEnv) -> dict[str, Any]:
    """Run Summary, RCA and Lifecycle through their handlers; return the workflow state."""
    state = env.seed_case()
    env.bedrock.reply(env.valid_summary, RCA_REPLY, LIFECYCLE_REPLY)
    for stage in (env.summary, env.rca, env.lifecycle):
        assert stage(state)["status"] == "completed"
    env.metrics()
    return state


def test_commits_one_complete_record_with_exact_fields_and_provenance(
    analysis_env: AnalysisEnv,
) -> None:
    env = analysis_env
    state = analysed(env)

    result = env.update(state)

    assert result == {
        "stage": "update",
        "status": "committed",
        "accountId": env.account,
        "displayId": env.display,
        "processedKey": "account_number=111122223333/case_number=170000000001/data.json",
    }
    record = env.processed()
    assert record is not None
    assert list(PROCESSED_FIELDS) == [
        "caseId",
        "displayId",
        "subject",
        "serviceCode",
        "categoryCode",
        "severityCode",
        "submittedBy",
        "timeCreated",
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
    ]
    raw = env.raw_metadata()
    assert record == {
        "caseId": env.case_id,
        "displayId": env.display,
        "subject": raw["subject"],
        "serviceCode": "elastic-load-balancing",
        "categoryCode": "performance",
        "severityCode": "high",
        "submittedBy": raw["submittedBy"],
        "timeCreated": "2026-06-01T10:00:00.000Z",
        "Case_Retrieval_Date": env.retrieved_at,
        "Case_Summary": env.valid_summary,
        "RCA_Category": "Customer Release",
        "RCA_Reason": "The release changed the port.",
        "RCA_Retrieval_Date": "2026-11-13T01:02:03Z",
        "Lifecycle_Category": "CICD",
        "Lifecycle_Reason": "A pipeline check would have caught the {port} change.",
        "Lifecycle_Retrieval_Date": "2026-11-13T01:02:03Z",
        "Model_Id": env.haiku,
        "Prompt_Version": prompt_version(directory=env.templates),
    }
    assert env.processed_versions() == 1
    head = env.s3.head_object(Bucket=env.processed_bucket, Key=env.processed_key())
    assert head["ServerSideEncryption"] == "AES256"
    assert head["ContentType"] == "application/json"
    assert env.metrics() == {"CasesProcessed": 1}
    output = env.logs() + env.stdout
    assert "Processed record committed" in output
    for secret in (*env.sentinels, env.valid_summary[:60], "The release changed the port."):
        assert secret not in output


def test_replayed_workflow_after_commit_makes_no_bedrock_calls_or_writes(
    analysis_env: AnalysisEnv,
) -> None:
    env = analysis_env
    state = analysed(env)
    env.update(state)
    committed = env.processed()
    calls = len(env.bedrock.requests)
    env.metrics()

    statuses = [stage(state)["status"] for stage in (env.summary, env.rca, env.lifecycle)]
    statuses.append(env.update(state)["status"])

    assert statuses == ["alreadyProcessed"] * 4
    assert len(env.bedrock.requests) == calls
    assert env.processed() == committed
    assert env.processed_versions() == 1
    assert env.metrics() == {"StagesSkippedAlreadyProcessed": 4}


def test_resumed_workflow_reuses_completed_stage_checkpoints(analysis_env: AnalysisEnv) -> None:
    env = analysis_env
    state = env.seed_case()
    env.bedrock.reply(env.valid_summary, RCA_REPLY)
    env.summary(state)
    env.rca(state)
    env.bedrock.reply(LIFECYCLE_REPLY)

    statuses = [stage(state)["status"] for stage in (env.summary, env.rca, env.lifecycle)]

    assert statuses == ["reused", "reused", "completed"]
    assert len(env.bedrock.requests) == 3
    assert env.update(state)["status"] == "committed"


def test_concurrent_commit_wins_and_is_never_overwritten(
    analysis_env: AnalysisEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = analysis_env
    state = analysed(env)
    winner = {"committedBy": "concurrent attempt"}
    original = update_app.read_raw_metadata

    def commit_concurrently(*args: Any, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        env.mark_processed(winner)
        return original(*args, **kwargs)

    monkeypatch.setattr(update_app, "read_raw_metadata", commit_concurrently)

    result = env.update(state)

    assert result["status"] == "alreadyProcessed"
    assert env.processed() == winner
    assert env.processed_versions() == 1
    assert env.metrics() == {"DuplicateCommitsPrevented": 1}


def test_existing_record_is_kept_without_reading_inputs(analysis_env: AnalysisEnv) -> None:
    env = analysis_env
    env.seed_case()
    env.mark_processed({"committedBy": "earlier run"})

    assert env.update(env.state())["status"] == "alreadyProcessed"
    assert env.processed() == {"committedBy": "earlier run"}
    assert env.processed_versions() == 1


@pytest.mark.parametrize(
    ("stage", "code"),
    [
        ("summary", "SummaryCheckpointMissing"),
        ("rca", "RcaCheckpointMissing"),
        ("lifecycle", "LifecycleCheckpointMissing"),
    ],
)
def test_missing_stage_checkpoint_writes_nothing(
    analysis_env: AnalysisEnv, stage: str, code: str
) -> None:
    env = analysis_env
    state = analysed(env)
    env.s3.delete_object(
        Bucket=env.raw_bucket,
        Key=f"account_number={env.account}/case_number={env.display}/checkpoints/{stage}.json",
    )

    with pytest.raises(IncompleteAnalysisError, match=code):
        env.update(state)
    assert env.processed() is None


def test_classification_of_a_different_summary_is_not_committed(
    analysis_env: AnalysisEnv,
) -> None:
    env = analysis_env
    state = analysed(env)
    summary = env.checkpoint("summary")
    assert summary is not None
    summary["output"]["Case_Summary"] = env.other_summary
    env.put_checkpoint("summary", summary)

    with pytest.raises(IncompleteAnalysisError, match="RcaCheckpointMissing"):
        env.update(state)
    assert env.processed() is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("promptVersion", "pv-0123456789abcdef"),
        ("modelId", "global.anthropic.claude-sonnet-4-5-20250929-v1:0"),
        (
            "output",
            {"Lifecycle_Category": "Networking", "Lifecycle_Reason": "x", "fallback": False},
        ),
        ("output", {"Lifecycle_Category": "CICD", "Lifecycle_Reason": "x", "fallback": True}),
        ("output", {"Lifecycle_Category": "CICD", "Lifecycle_Reason": " ", "fallback": False}),
    ],
    ids=["stale-prompt", "other-model", "invalid-category", "fallback-flag", "blank-reason"],
)
def test_stale_or_invalid_lifecycle_checkpoint_writes_nothing(
    analysis_env: AnalysisEnv, field: str, value: object
) -> None:
    env = analysis_env
    state = analysed(env)
    lifecycle = env.checkpoint("lifecycle")
    assert lifecycle is not None
    env.put_checkpoint("lifecycle", {**lifecycle, field: value})

    with pytest.raises(IncompleteAnalysisError, match="LifecycleCheckpointMissing"):
        env.update(state)
    assert env.processed() is None


@pytest.mark.parametrize(
    ("metadata", "code"),
    [
        (None, "RawMetadataMissing"),
        ({"caseId": "case-other"}, "RawMetadataMismatch"),
        ({"accountId": "999999999999"}, "RawMetadataMismatch"),
        ({"Case_Retrieval_Date": "13/11/2026"}, "RecordInvalid"),
        ({"timeCreated": None}, "RecordInvalid"),
        ({"severityCode": 3}, "RecordInvalid"),
    ],
    ids=["missing", "other-case", "other-account", "bad-date", "null-time", "non-string"],
)
def test_incomplete_raw_metadata_writes_nothing(
    analysis_env: AnalysisEnv, metadata: dict[str, Any] | None, code: str
) -> None:
    env = analysis_env
    state = analysed(env)
    key = env.processed_key()
    if metadata is None:
        env.s3.delete_object(Bucket=env.raw_bucket, Key=key)
    else:
        env.s3.put_object(
            Bucket=env.raw_bucket, Key=key, Body=json.dumps(env.raw_metadata(**metadata)).encode()
        )

    with pytest.raises(IncompleteAnalysisError, match=code):
        env.update(state)
    assert env.processed() is None
    if code == "RecordInvalid":
        assert env.metrics() == {"IncompleteRecords": 1}


def test_missing_metadata_field_writes_nothing(analysis_env: AnalysisEnv) -> None:
    env = analysis_env
    state = analysed(env)
    metadata = env.raw_metadata()
    del metadata["categoryCode"]
    env.s3.put_object(
        Bucket=env.raw_bucket, Key=env.processed_key(), Body=json.dumps(metadata).encode()
    )

    with pytest.raises(IncompleteAnalysisError, match="RecordInvalid"):
        env.update(state)
    assert env.processed() is None


def test_null_descriptive_metadata_is_preserved(analysis_env: AnalysisEnv) -> None:
    env = analysis_env
    state = analysed(env)
    env.s3.put_object(
        Bucket=env.raw_bucket,
        Key=env.processed_key(),
        Body=json.dumps(env.raw_metadata(categoryCode=None)).encode(),
    )

    assert env.update(state)["status"] == "committed"
    record = env.processed()
    assert record is not None
    assert record["categoryCode"] is None
    assert len(record) == 18


def test_unclassified_fallbacks_are_committed_explicitly(analysis_env: AnalysisEnv) -> None:
    env = analysis_env
    state = env.seed_case()
    env.bedrock.reply(env.valid_summary, "no json", "{}", RCA_REPLY.replace("Customer ", "X "), "?")
    for stage in (env.summary, env.rca, env.lifecycle):
        stage(state)
    env.metrics()

    assert env.update(state)["status"] == "committed"

    record = env.processed()
    assert record is not None
    assert record["RCA_Category"] == "Unclassified"
    assert record["Lifecycle_Category"] == "Unclassified"
    assert "after one correction" in record["RCA_Reason"]
    assert env.metrics() == {"CasesProcessed": 1, "UnclassifiedFields": 2}


def test_failed_write_leaves_no_record_and_the_retry_commits_once(
    analysis_env: AnalysisEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = analysis_env
    state = analysed(env)

    def failing_write(*args: Any, **kwargs: Any) -> WriteResult:  # noqa: ANN401
        raise ClientError({"Error": {"Code": "InternalError", "Message": "x"}}, "PutObject")

    with monkeypatch.context() as patch:
        patch.setattr(update_app, "write_json", failing_write)
        with pytest.raises(ClientError):
            env.update(state)
    assert env.processed() is None

    assert env.update(state)["status"] == "committed"
    assert env.processed_versions() == 1


def test_commit_uses_a_conditional_create(
    analysis_env: AnalysisEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = analysis_env
    state = analysed(env)
    seen: list[dict[str, Any]] = []

    def recording_write(
        s3: S3Client,
        bucket: str,
        key: str,
        data: object,
        **kwargs: Any,  # noqa: ANN401
    ) -> WriteResult:
        seen.append({"bucket": bucket, "key": key, **kwargs})
        return write_json(s3, bucket, key, data, **kwargs)

    monkeypatch.setattr(update_app, "write_json", recording_write)
    env.update(state)

    assert seen == [
        {"bucket": env.processed_bucket, "key": env.processed_key(), "if_none_match": True}
    ]

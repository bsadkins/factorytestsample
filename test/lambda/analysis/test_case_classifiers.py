"""RCA and Lifecycle stage handlers: exact requests, defensive JSON extraction, one correction
then the explicit Unclassified fallback, checkpoints and processed-record checks."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from analysis_kit import AnalysisEnv
from common.analysis import IncompleteAnalysisError, text_digest
from common.bedrock import BedrockThrottlingError
from common.classification import parse_classification
from common.taxonomy import LIFECYCLE, RCA, UNCLASSIFIED, Taxonomy
from common.templates import (
    prompt_version,
    render_classification_correction,
    render_classification_prompt,
)

STAGES = ["rca", "lifecycle"]
TAXONOMIES = {"rca": RCA, "lifecycle": LIFECYCLE}
VALID = {
    "rca": ("Customer Release", "The new container image listened on the wrong port."),
    "lifecycle": ("CICD", "A deployment pipeline check would have caught the port change."),
}


def run(env: AnalysisEnv, stage: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    return env.rca if stage == "rca" else env.lifecycle


def summarised(env: AnalysisEnv) -> dict[str, Any]:
    """Seed a case and produce its summary checkpoint through the Summary handler."""
    state = env.seed_case()
    env.bedrock.reply(env.valid_summary)
    env.summary(state)
    env.bedrock.requests.clear()
    env.metrics()
    return state


def reply_for(stage: str, category: str | None = None, reason: str | None = None) -> str:
    taxonomy = TAXONOMIES[stage]
    label, why = VALID[stage]
    return json.dumps(
        {taxonomy.category_key: category or label, taxonomy.reason_key: reason or why}
    )


def user(text: str) -> dict[str, object]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def assistant(text: str) -> dict[str, object]:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


@pytest.mark.parametrize("stage", STAGES)
def test_request_is_exact_and_classification_is_checkpointed(
    analysis_env: AnalysisEnv, stage: str
) -> None:
    env = analysis_env
    taxonomy = TAXONOMIES[stage]
    state = summarised(env)
    env.bedrock.reply(reply_for(stage))

    result = run(env, stage)(state)

    key = f"account_number={env.account}/case_number={env.display}/checkpoints/{stage}.json"
    assert result == {
        "stage": stage,
        "status": "completed",
        "accountId": env.account,
        "displayId": env.display,
        "checkpointKey": key,
    }
    prompt = render_classification_prompt(taxonomy, env.valid_summary, directory=env.templates)
    assert env.bedrock.requests == [
        {
            "modelId": env.haiku,
            "contentType": "application/json",
            "accept": "application/json",
            "body": {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1000,
                "temperature": 0.0,
                "messages": [user(prompt)],
            },
        }
    ]
    for category in taxonomy.categories:
        assert f"- {category.label}: {category.definition}" in prompt
    label, reason = VALID[stage]
    assert env.checkpoint(stage) == {
        "schemaVersion": 1,
        "stage": stage,
        "accountId": env.account,
        "caseId": env.case_id,
        "displayId": env.display,
        "modelId": env.haiku,
        "promptVersion": prompt_version(directory=env.templates),
        "inputDigest": text_digest(env.valid_summary),
        "completedAt": "2026-11-13T01:02:03Z",
        "output": {taxonomy.category_key: label, taxonomy.reason_key: reason, "fallback": False},
    }
    assert label not in json.dumps(result)
    metrics = env.metrics()
    assert metrics["CasesClassified"] == 1
    assert metrics["BedrockInvocations"] == 1
    assert reason not in env.logs() + env.stdout
    assert env.valid_summary[:60] not in env.logs() + env.stdout


def classifier_fixtures(*, valid: bool) -> list[dict[str, Any]]:
    data: dict[str, Any] = json.loads(
        (
            AnalysisEnv.templates.parent.parent / "test/fixtures/prompts/classifications.json"
        ).read_text(encoding="utf-8")
    )
    return [case for case in data["cases"] if ("error" not in case) == valid]


@pytest.mark.parametrize(
    "fixture", classifier_fixtures(valid=True), ids=lambda case: str(case["id"])
)
def test_fenced_nested_and_escaped_replies_are_parsed_by_the_handler(
    analysis_env: AnalysisEnv, fixture: dict[str, Any]
) -> None:
    env = analysis_env
    stage = fixture["taxonomy"]
    taxonomy = TAXONOMIES[stage]
    state = summarised(env)
    env.bedrock.reply(fixture["response"])

    run(env, stage)(state)

    checkpoint = env.checkpoint(stage)
    assert checkpoint is not None
    assert checkpoint["output"] == {**fixture["expected"], "fallback": False}
    assert set(checkpoint["output"]) == {taxonomy.category_key, taxonomy.reason_key, "fallback"}
    assert len(env.bedrock.requests) == 1


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize(
    ("bad_reply", "code"),
    [
        ("I think this is a release problem.", "no_json_object"),
        ('{"RCA_Category": "Customer Release", "RCA_Reason": "unterminated', "no_json_object"),
        ("```json\n{}\n```", "missing_category"),
    ],
    ids=["prose", "truncated-json", "empty-object"],
)
def test_malformed_reply_gets_exactly_one_correction(
    analysis_env: AnalysisEnv, stage: str, bad_reply: str, code: str
) -> None:
    env = analysis_env
    taxonomy = TAXONOMIES[stage]
    state = summarised(env)
    env.bedrock.reply(bad_reply, reply_for(stage))

    assert run(env, stage)(state)["status"] == "completed"

    hint = _hint(bad_reply, taxonomy)
    assert env.bedrock.prompts(1)[1:] == [
        assistant(bad_reply),
        user(render_classification_correction(taxonomy, hint, directory=env.templates)),
    ]
    checkpoint = env.checkpoint(stage)
    assert checkpoint is not None
    assert checkpoint["output"]["fallback"] is False
    metrics = env.metrics()
    assert metrics["InvalidClassifications"] == 1
    assert metrics["ClassificationCorrections"] == 1
    assert code in env.logs()


def _hint(reply: str, taxonomy: Taxonomy) -> str:
    try:
        parse_classification(reply, taxonomy)
    except ValueError as exc:
        return str(exc.correction_hint)  # type: ignore[attr-defined]
    raise AssertionError("reply unexpectedly valid")


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize(
    "bad_category",
    ["Customer release", "Networking", UNCLASSIFIED, " "],
    ids=["wrong-case", "unknown", "unclassified-from-model", "blank"],
)
def test_repeated_invalid_category_falls_back_to_unclassified_after_one_correction(
    analysis_env: AnalysisEnv, stage: str, bad_category: str
) -> None:
    env = analysis_env
    taxonomy = TAXONOMIES[stage]
    state = summarised(env)
    bad = reply_for(stage, category=bad_category)
    env.bedrock.reply(bad, bad)

    assert run(env, stage)(state)["status"] == "completed"

    assert len(env.bedrock.requests) == 2
    checkpoint = env.checkpoint(stage)
    assert checkpoint is not None
    output = checkpoint["output"]
    assert output[taxonomy.category_key] == UNCLASSIFIED
    assert output["fallback"] is True
    assert "failed validation after one correction" in output[taxonomy.reason_key]
    assert len(output[taxonomy.reason_key]) <= 160
    metrics = env.metrics()
    assert metrics["ClassificationFallbacks"] == 1
    assert metrics["InvalidClassifications"] == 2
    assert metrics["ClassificationCorrections"] == 1


@pytest.mark.parametrize("stage", STAGES)
def test_repeated_malformed_json_falls_back_without_further_retries(
    analysis_env: AnalysisEnv, stage: str
) -> None:
    env = analysis_env
    taxonomy = TAXONOMIES[stage]
    state = summarised(env)
    env.bedrock.reply("not json", "still {not json")

    run(env, stage)(state)

    checkpoint = env.checkpoint(stage)
    assert checkpoint is not None
    assert checkpoint["output"][taxonomy.category_key] == UNCLASSIFIED
    assert "(no_json_object)" in checkpoint["output"][taxonomy.reason_key]
    assert len(env.bedrock.requests) == 2


@pytest.mark.parametrize("stage", STAGES)
def test_missing_or_invalid_summary_checkpoint_fails_before_inference(
    analysis_env: AnalysisEnv, stage: str
) -> None:
    env = analysis_env
    state = env.seed_case()
    with pytest.raises(IncompleteAnalysisError, match="SummaryCheckpointMissing"):
        run(env, stage)(state)

    env.bedrock.reply(env.valid_summary)
    env.summary(state)
    tampered = env.checkpoint("summary")
    assert tampered is not None
    tampered["output"]["Case_Summary"] = "A short, invalid summary."
    env.put_checkpoint("summary", tampered)
    with pytest.raises(IncompleteAnalysisError, match="SummaryCheckpointMissing"):
        run(env, stage)(state)
    assert len(env.bedrock.requests) == 1
    assert env.checkpoint(stage) is None


@pytest.mark.parametrize("stage", STAGES)
def test_checkpoint_is_reused_and_a_changed_summary_is_reclassified(
    analysis_env: AnalysisEnv, stage: str
) -> None:
    env = analysis_env
    state = summarised(env)
    env.bedrock.reply(reply_for(stage))
    run(env, stage)(state)
    env.metrics()

    assert run(env, stage)(state)["status"] == "reused"
    assert len(env.bedrock.requests) == 1
    assert env.metrics() == {"CheckpointsReused": 1}

    summary = env.checkpoint("summary")
    assert summary is not None
    summary["output"]["Case_Summary"] = env.other_summary
    env.put_checkpoint("summary", summary)
    env.bedrock.reply(reply_for(stage))

    assert run(env, stage)(state)["status"] == "completed"
    assert len(env.bedrock.requests) == 2
    checkpoint = env.checkpoint(stage)
    assert checkpoint is not None
    assert checkpoint["inputDigest"] == text_digest(env.other_summary)


@pytest.mark.parametrize("stage", STAGES)
def test_processed_case_is_never_sent_to_bedrock(analysis_env: AnalysisEnv, stage: str) -> None:
    env = analysis_env
    state = env.seed_case()
    env.mark_processed()

    assert run(env, stage)(state)["status"] == "alreadyProcessed"
    assert env.bedrock.requests == []
    assert env.metrics() == {"StagesSkippedAlreadyProcessed": 1}


@pytest.mark.parametrize("stage", STAGES)
def test_record_committed_during_first_call_stops_the_correction(
    analysis_env: AnalysisEnv, stage: str
) -> None:
    env = analysis_env
    state = summarised(env)
    env.bedrock.during_call[1] = env.mark_processed
    env.bedrock.reply("not json")

    assert run(env, stage)(state)["status"] == "alreadyProcessed"
    assert len(env.bedrock.requests) == 1
    assert env.checkpoint(stage) is None


RETRYABLE_ERRORS = [("ThrottlingException", 429), ("ModelTimeoutException", 408)]


def classified_winner(env: AnalysisEnv, stage: str, state: dict[str, Any]) -> dict[str, Any]:
    """Produce a valid classification checkpoint, then remove it so a 'concurrent attempt' can
    write it back later."""
    env.bedrock.reply(reply_for(stage))
    run(env, stage)(state)
    winner = env.checkpoint(stage)
    assert winner is not None
    env.s3.delete_object(
        Bucket=env.raw_bucket,
        Key=f"account_number={env.account}/case_number={env.display}/checkpoints/{stage}.json",
    )
    env.bedrock.requests.clear()
    env.metrics()
    return winner


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize(("code", "status"), RETRYABLE_ERRORS)
def test_record_committed_during_backoff_stops_the_retry(
    analysis_env: AnalysisEnv, stage: str, code: str, status: int
) -> None:
    env = analysis_env
    state = summarised(env)
    env.bedrock.error(code, status)
    env.during_sleep[1] = env.mark_processed

    result = run(env, stage)(state)

    assert result == {
        "stage": stage,
        "status": "alreadyProcessed",
        "accountId": env.account,
        "displayId": env.display,
        "processedKey": env.processed_key(),
    }
    assert len(env.bedrock.requests) == 1
    assert len(env.sleeps) == 1
    assert env.checkpoint(stage) is None
    metrics = env.metrics()
    assert metrics["StagesSkippedAlreadyProcessed"] == 1
    assert "BedrockInvocations" not in metrics
    output = env.logs() + env.stdout
    assert env.valid_summary[:60] not in output
    assert env.sentinels[0] not in output


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize(("code", "status"), RETRYABLE_ERRORS)
def test_checkpoint_saved_during_backoff_is_reused_instead_of_retrying(
    analysis_env: AnalysisEnv, stage: str, code: str, status: int
) -> None:
    env = analysis_env
    state = summarised(env)
    winner = classified_winner(env, stage, state)
    env.bedrock.error(code, status)
    env.during_sleep[1] = lambda: env.put_checkpoint(stage, winner)

    result = run(env, stage)(state)

    assert result["status"] == "reused"
    assert len(env.bedrock.requests) == 1
    assert env.checkpoint(stage) == winner
    assert env.metrics()["CheckpointsReused"] == 1


@pytest.mark.parametrize("stage", STAGES)
def test_record_committed_during_correction_backoff_stops_the_correction(
    analysis_env: AnalysisEnv, stage: str
) -> None:
    env = analysis_env
    state = summarised(env)
    env.bedrock.reply("not json")
    env.bedrock.error("ThrottlingException", 429)
    env.during_sleep[1] = env.mark_processed

    assert run(env, stage)(state)["status"] == "alreadyProcessed"

    assert len(env.bedrock.requests) == 2
    assert env.bedrock.prompts(1)[1] == assistant("not json")
    assert env.checkpoint(stage) is None


@pytest.mark.parametrize("stage", STAGES)
def test_checkpoint_saved_during_correction_backoff_is_reused(
    analysis_env: AnalysisEnv, stage: str
) -> None:
    env = analysis_env
    state = summarised(env)
    winner = classified_winner(env, stage, state)
    env.bedrock.reply("not json")
    env.bedrock.error("ServiceUnavailableException", 503)
    env.during_sleep[1] = lambda: env.put_checkpoint(stage, winner)

    assert run(env, stage)(state)["status"] == "reused"

    assert len(env.bedrock.requests) == 2
    assert env.checkpoint(stage) == winner


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize(("code", "status"), RETRYABLE_ERRORS)
def test_retry_without_durable_state_is_still_attempted_within_bounds(
    analysis_env: AnalysisEnv, stage: str, code: str, status: int
) -> None:
    env = analysis_env
    state = summarised(env)
    env.bedrock.error(code, status, times=2)
    env.bedrock.reply(reply_for(stage))

    assert run(env, stage)(state)["status"] == "completed"

    assert len(env.bedrock.requests) == 3
    assert len(env.sleeps) == 2
    checkpoint = env.checkpoint(stage)
    assert checkpoint is not None
    assert checkpoint["output"]["fallback"] is False


@pytest.mark.parametrize("stage", STAGES)
def test_persistent_throttling_without_durable_state_fails_after_bounded_retries(
    analysis_env: AnalysisEnv, stage: str
) -> None:
    env = analysis_env
    state = summarised(env)
    env.bedrock.error("ThrottlingException", 429, times=4)

    with pytest.raises(BedrockThrottlingError):
        run(env, stage)(state)

    assert len(env.bedrock.requests) == 4
    assert len(env.sleeps) == 3
    assert env.checkpoint(stage) is None

"""Case Summary stage handler: exact Bedrock requests, summary constraints, bounded correction,
checkpoints and processed-record checks before every invocation."""

from __future__ import annotations

import json
import unicodedata
from typing import Any

import pytest
from analysis_kit import AnalysisEnv, summary_fixture
from common.analysis import IncompleteAnalysisError, InvalidWorkflowInputError
from common.bedrock import BedrockThrottlingError, UnsupportedModelError
from common.conversation import (
    MAX_CASE_ANNOTATION_CHARS,
    ConversationTooLargeError,
    render_case_annotation,
)
from common.summary import check_summary, describe_issues
from common.templates import prompt_version, render_summary_correction, render_summary_prompt

CHECKPOINT_KEY = "account_number=111122223333/case_number=170000000001/checkpoints/summary.json"


def prose(length: int, sentences: int = 5, letter: str = "é") -> str:
    """Synthetic plain prose of exactly `length` characters and `sentences` sentences."""
    parts = [f"Sentence {n} describes the synthetic Amazon EC2 issue" for n in range(sentences)]
    deficit = length - len(". ".join(parts) + ".")
    assert deficit >= 2
    parts[-1] += " " + letter * (deficit - 1)
    text = ". ".join(parts) + "."
    assert len(text) == length
    return text


def expected_prompt(env: AnalysisEnv, bodies: list[str] | None = None) -> str:
    annotation = render_case_annotation(env.communications(bodies))
    return render_summary_prompt(annotation, directory=env.templates)


def user(text: str) -> dict[str, object]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def assistant(text: str) -> dict[str, object]:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def test_summary_request_is_exact_and_valid_summary_is_checkpointed(
    analysis_env: AnalysisEnv,
) -> None:
    env = analysis_env
    state = env.seed_case()
    env.bedrock.reply(env.valid_summary)

    result = env.summary(state)

    assert result == {
        "stage": "summary",
        "status": "completed",
        "accountId": env.account,
        "displayId": env.display,
        "checkpointKey": CHECKPOINT_KEY,
    }
    assert env.bedrock.requests == [
        {
            "modelId": env.haiku,
            "contentType": "application/json",
            "accept": "application/json",
            "body": {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1000,
                "temperature": 0.0,
                "messages": [user(expected_prompt(env))],
            },
        }
    ]
    prompt = env.bedrock.prompts()[0]["content"][0]["text"]
    assert env.sentinels[0] in prompt
    assert env.sentinels[1] not in prompt
    checkpoint = env.checkpoint("summary")
    assert checkpoint is not None
    annotation = render_case_annotation(env.communications())
    assert checkpoint["output"] == {"Case_Summary": env.valid_summary}
    assert checkpoint["modelId"] == env.haiku
    assert checkpoint["promptVersion"] == prompt_version(directory=env.templates)
    assert checkpoint["completedAt"] == "2026-11-13T01:02:03Z"
    assert checkpoint["inputDigest"].startswith("sha256:")
    assert annotation not in json.dumps(result)
    metrics = env.metrics()
    assert metrics["SummariesGenerated"] == 1
    assert metrics["BedrockInvocations"] == 1
    assert metrics["BedrockInputTokens"] == 900
    assert metrics["BedrockOutputTokens"] == 160
    assert "BedrockLatency" in metrics
    output = env.stdout + env.logs()
    assert "Summary stored" in env.logs()
    for secret in (*env.sentinels, env.valid_summary[:60]):
        assert secret not in output


def test_configured_sonnet_profile_and_token_limit_are_used(
    analysis_env: AnalysisEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = analysis_env
    monkeypatch.setenv("BEDROCK_MODEL_ID", env.sonnet)
    monkeypatch.setenv("BEDROCK_MAX_TOKENS", "700")
    env.bedrock.reply(env.valid_summary)

    env.summary(env.seed_case())

    request = env.bedrock.requests[0]
    assert request["modelId"] == env.sonnet
    assert request["body"]["max_tokens"] == 700
    assert request["body"]["anthropic_version"] == "bedrock-2023-05-31"


@pytest.mark.parametrize(
    "model",
    [
        "anthropic.claude-3-haiku-20240307-v1:0",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "global.anthropic.claude-opus-4-1-20250805-v1:0",
        "",
    ],
)
def test_models_outside_the_allowlist_are_rejected_before_any_call(
    analysis_env: AnalysisEnv, monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    env = analysis_env
    monkeypatch.setenv("BEDROCK_MODEL_ID", model)
    with pytest.raises(UnsupportedModelError):
        env.summary(env.seed_case())
    assert env.bedrock.requests == []


@pytest.mark.parametrize("tokens", ["255", "4097", "many"])
def test_token_limit_outside_the_supported_range_is_rejected_before_any_call(
    analysis_env: AnalysisEnv, monkeypatch: pytest.MonkeyPatch, tokens: str
) -> None:
    env = analysis_env
    monkeypatch.setenv("BEDROCK_MAX_TOKENS", tokens)
    with pytest.raises(ValueError, match=r"BEDROCK_MAX_TOKENS|invalid literal"):
        env.summary(env.seed_case())
    assert env.bedrock.requests == []


@pytest.mark.parametrize(
    ("text", "accepted"),
    [
        (prose(600), True),
        (prose(650), True),
        (prose(599), False),
        (prose(651), False),
        (prose(620, sentences=4), True),
        (prose(620, sentences=6), True),
        (prose(620, sentences=3), False),
        (prose(620, sentences=7), False),
        (unicodedata.normalize("NFD", prose(650)), True),
        ("  " + prose(650) + "\n", True),
        ("Summary: " + prose(620), False),
        (prose(620).replace("Sentence 2", "**Sentence 2**"), False),
        (prose(600).replace(". Sentence 3", ".\nSentence 3"), False),
        (prose(610) + " (610 characters)", False),
        (summary_fixture("markdown-emphasis-asterisk"), False),
        (summary_fixture("markdown-emphasis-underscore"), False),
        (summary_fixture("technical-underscore-arithmetic"), True),
    ],
    ids=[
        "600-chars",
        "650-chars",
        "599-chars",
        "651-chars",
        "4-sentences",
        "6-sentences",
        "3-sentences",
        "7-sentences",
        "decomposed-unicode-650",
        "surrounding-whitespace",
        "preamble",
        "markdown",
        "line-break",
        "self-audit",
        "emphasis-asterisk",
        "emphasis-underscore",
        "technical-underscore-arithmetic",
    ],
)
def test_summary_constraints_are_enforced_through_the_handler(
    analysis_env: AnalysisEnv, text: str, accepted: bool
) -> None:
    env = analysis_env
    state = env.seed_case()
    if accepted:
        env.bedrock.reply(text)
        assert env.summary(state)["status"] == "completed"
        checkpoint = env.checkpoint("summary")
        assert checkpoint is not None
        stored = checkpoint["output"]["Case_Summary"]
        assert stored == unicodedata.normalize("NFC", text).strip()
        assert 600 <= len(stored) <= 650
        assert len(env.bedrock.requests) == 1
    else:
        env.bedrock.reply(text, text)
        with pytest.raises(Exception, match="SummaryValidationError") as info:
            env.summary(state)
        assert type(info.value).__name__ == "SummaryValidationError"
        assert env.checkpoint("summary") is None
        assert len(env.bedrock.requests) == 2


def test_invalid_summary_gets_one_correction_in_the_same_conversation(
    analysis_env: AnalysisEnv,
) -> None:
    env = analysis_env
    state = env.seed_case()
    too_short = "The customer saw errors. Support fixed the policy. It works. Case closed."
    env.bedrock.reply(too_short, env.valid_summary)

    result = env.summary(state)

    assert result["status"] == "completed"
    correction = render_summary_correction(
        describe_issues(check_summary(too_short).issues), directory=env.templates
    )
    assert env.bedrock.prompts(1) == [
        user(expected_prompt(env)),
        assistant(too_short),
        user(correction),
    ]
    assert "at least 600 characters" in correction
    checkpoint = env.checkpoint("summary")
    assert checkpoint is not None
    assert checkpoint["output"]["Case_Summary"] == env.valid_summary
    metrics = env.metrics()
    assert metrics["InvalidSummaries"] == 1
    assert metrics["SummaryCorrections"] == 1
    assert metrics["BedrockInvocations"] == 2
    assert too_short not in env.logs()


def test_correction_exhaustion_fails_without_truncating_or_padding(
    analysis_env: AnalysisEnv,
) -> None:
    env = analysis_env
    state = env.seed_case()
    too_long = prose(700)
    env.bedrock.reply(too_long, too_long)

    with pytest.raises(Exception, match="too_long") as info:
        env.summary(state)

    assert type(info.value).__name__ == "SummaryValidationError"
    assert len(env.bedrock.requests) == 2
    assert env.checkpoint("summary") is None
    assert env.processed() is None
    metrics = env.metrics()
    assert metrics["SummaryValidationFailures"] == 1
    assert metrics["InvalidSummaries"] == 2
    assert "Summary still invalid after the correction" in env.logs()


def test_valid_checkpoint_is_reused_without_calling_bedrock(analysis_env: AnalysisEnv) -> None:
    env = analysis_env
    state = env.seed_case()
    env.bedrock.reply(env.valid_summary)
    env.summary(state)
    first = env.checkpoint("summary")
    env.metrics()

    result = env.summary(state)

    assert result["status"] == "reused"
    assert result["checkpointKey"] == CHECKPOINT_KEY
    assert len(env.bedrock.requests) == 1
    assert env.checkpoint("summary") == first
    assert env.metrics() == {"CheckpointsReused": 1}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("modelId", "global.anthropic.claude-sonnet-4-5-20250929-v1:0"),
        ("promptVersion", "pv-0000000000000000"),
        ("inputDigest", "sha256:" + "0" * 64),
        ("output", {"Case_Summary": "Too short to be a valid summary."}),
    ],
    ids=["other-model", "other-prompt-version", "other-conversation", "invalid-output"],
)
def test_stale_or_invalid_checkpoint_is_regenerated(
    analysis_env: AnalysisEnv, field: str, value: object
) -> None:
    env = analysis_env
    state = env.seed_case()
    env.bedrock.reply(env.valid_summary)
    env.summary(state)
    stale = env.checkpoint("summary")
    assert stale is not None
    env.put_checkpoint("summary", {**stale, field: value})
    env.bedrock.reply(env.other_summary)

    assert env.summary(state)["status"] == "completed"

    assert len(env.bedrock.requests) == 2
    refreshed = env.checkpoint("summary")
    assert refreshed is not None
    assert refreshed["output"] == {"Case_Summary": env.other_summary}
    assert refreshed["modelId"] == env.haiku


def test_new_conversation_version_invalidates_the_summary_checkpoint(
    analysis_env: AnalysisEnv,
) -> None:
    env = analysis_env
    env.bedrock.reply(env.valid_summary, env.other_summary)
    env.summary(env.seed_case())

    changed = env.store_conversation(["A different synthetic conversation about Amazon RDS."])
    env.summary(changed)

    assert len(env.bedrock.requests) == 2
    assert "Amazon RDS" in env.bedrock.prompts(1)[0]["content"][0]["text"]


def test_processed_case_is_never_sent_to_bedrock(analysis_env: AnalysisEnv) -> None:
    env = analysis_env
    state = env.seed_case()
    env.mark_processed()

    result = env.summary(state)

    assert result == {
        "stage": "summary",
        "status": "alreadyProcessed",
        "accountId": env.account,
        "displayId": env.display,
        "processedKey": env.processed_key(),
    }
    assert env.bedrock.requests == []
    assert env.checkpoint("summary") is None
    assert env.metrics() == {"StagesSkippedAlreadyProcessed": 1}


def test_record_committed_during_first_call_stops_the_correction(
    analysis_env: AnalysisEnv,
) -> None:
    env = analysis_env
    state = env.seed_case()
    env.bedrock.during_call[1] = env.mark_processed
    env.bedrock.reply("Too short.")

    assert env.summary(state)["status"] == "alreadyProcessed"
    assert len(env.bedrock.requests) == 1
    assert env.checkpoint("summary") is None


def test_checkpoint_written_by_a_concurrent_attempt_is_reused_before_correction(
    analysis_env: AnalysisEnv,
) -> None:
    env = analysis_env
    state = env.seed_case()
    env.bedrock.reply(env.valid_summary)
    env.summary(state)
    winner = env.checkpoint("summary")
    assert winner is not None
    env.s3.delete_object(Bucket=env.raw_bucket, Key=CHECKPOINT_KEY)
    env.bedrock.during_call[2] = lambda: env.put_checkpoint("summary", winner)
    env.bedrock.reply("Too short.")

    assert env.summary(state)["status"] == "reused"
    assert len(env.bedrock.requests) == 2


def test_concurrent_checkpoint_write_keeps_the_first_valid_result(
    analysis_env: AnalysisEnv,
) -> None:
    env = analysis_env
    state = env.seed_case()
    env.bedrock.reply(env.valid_summary)
    env.summary(state)
    winner = env.checkpoint("summary")
    assert winner is not None
    env.s3.delete_object(Bucket=env.raw_bucket, Key=CHECKPOINT_KEY)
    env.bedrock.during_call[2] = lambda: env.put_checkpoint("summary", winner)
    env.bedrock.reply(env.other_summary)

    assert env.summary(state)["status"] == "completed"

    assert env.checkpoint("summary") == winner


def test_oversized_conversation_fails_explicitly_without_inference(
    analysis_env: AnalysisEnv,
) -> None:
    env = analysis_env
    body = env.sentinels[0] + " " + "x" * MAX_CASE_ANNOTATION_CHARS
    state = env.seed_case(bodies=[body])

    with pytest.raises(ConversationTooLargeError) as info:
        env.summary(state)

    assert info.value.limit == MAX_CASE_ANNOTATION_CHARS
    assert info.value.chars > MAX_CASE_ANNOTATION_CHARS
    assert env.bedrock.requests == []
    assert env.checkpoint("summary") is None
    assert env.metrics() == {"OversizedConversations": 1}
    assert env.sentinels[0] not in env.logs() + str(info.value)


def test_persistent_throttling_raises_a_named_retryable_error_after_bounded_backoff(
    analysis_env: AnalysisEnv,
) -> None:
    env = analysis_env
    state = env.seed_case()
    env.bedrock.error("ThrottlingException", 429, times=4)

    with pytest.raises(BedrockThrottlingError):
        env.summary(state)

    assert len(env.bedrock.requests) == 4
    assert len(env.sleeps) == 3
    assert all(delay <= 16 for delay in env.sleeps)
    assert env.checkpoint("summary") is None
    metrics = env.metrics()
    assert metrics["BedrockThrottles"] == 4
    assert metrics["BedrockInvocationFailures"] == 1


def test_transient_throttle_is_retried_and_counted(analysis_env: AnalysisEnv) -> None:
    env = analysis_env
    state = env.seed_case()
    env.bedrock.error("ThrottlingException", 429)
    env.bedrock.reply(env.valid_summary)

    assert env.summary(state)["status"] == "completed"
    assert env.metrics()["BedrockThrottles"] == 1


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"case": "not-an-object"},
        {"case": {"accountId": "111122223333"}},
        ["case"],
    ],
    ids=["missing", "string", "incomplete", "not-a-mapping"],
)
def test_invalid_workflow_input_fails_visibly(analysis_env: AnalysisEnv, state: object) -> None:
    with pytest.raises(InvalidWorkflowInputError):
        analysis_env.summary(state)  # type: ignore[arg-type]
    assert analysis_env.bedrock.requests == []


def test_missing_or_mismatched_conversation_is_an_incomplete_analysis(
    analysis_env: AnalysisEnv,
) -> None:
    env = analysis_env
    case = env.seed_case()["case"]
    foreign = env.s3.put_object(
        Bucket=env.raw_bucket,
        Key=case["communicationsKey"],
        Body=json.dumps({"accountId": "999999999999", "communications": []}).encode(),
    )
    with pytest.raises(IncompleteAnalysisError, match="ConversationMismatch"):
        env.summary({"case": {**case, "communicationsVersionId": foreign["VersionId"]}})

    env.s3.delete_object(Bucket=env.raw_bucket, Key=case["communicationsKey"])
    with pytest.raises(IncompleteAnalysisError, match="ConversationMissing"):
        env.summary({"case": {**case, "communicationsVersionId": None}})
    assert env.bedrock.requests == []


@pytest.mark.parametrize("fixture", ["markdown-emphasis-asterisk", "markdown-emphasis-underscore"])
def test_inline_emphasis_gets_a_markdown_correction(
    analysis_env: AnalysisEnv, fixture: str
) -> None:
    env = analysis_env
    state = env.seed_case()
    emphasised = summary_fixture(fixture)
    env.bedrock.reply(emphasised, env.valid_summary)

    assert env.summary(state)["status"] == "completed"

    correction = render_summary_correction(describe_issues(["markdown"]), directory=env.templates)
    assert env.bedrock.prompts(1) == [
        user(expected_prompt(env)),
        assistant(emphasised),
        user(correction),
    ]
    checkpoint = env.checkpoint("summary")
    assert checkpoint is not None
    assert checkpoint["output"]["Case_Summary"] == env.valid_summary
    assert '"markdown"' in env.logs()
    assert emphasised[:60] not in env.logs()


@pytest.mark.parametrize("fixture", ["markdown-emphasis-asterisk", "markdown-emphasis-underscore"])
def test_repeated_inline_emphasis_fails_without_checkpoint_or_processed_record(
    analysis_env: AnalysisEnv, fixture: str
) -> None:
    env = analysis_env
    state = env.seed_case()
    emphasised = summary_fixture(fixture)
    env.bedrock.reply(emphasised, emphasised)

    with pytest.raises(Exception, match="markdown") as info:
        env.summary(state)

    assert type(info.value).__name__ == "SummaryValidationError"
    assert len(env.bedrock.requests) == 2
    assert env.checkpoint("summary") is None
    with pytest.raises(IncompleteAnalysisError):
        env.update(state)
    assert env.processed() is None


def winning_checkpoint(env: AnalysisEnv, state: dict[str, Any]) -> dict[str, Any]:
    """Produce a valid summary checkpoint, then remove it so a 'concurrent attempt' can
    write it back later."""
    env.bedrock.reply(env.valid_summary)
    env.summary(state)
    winner = env.checkpoint("summary")
    assert winner is not None
    env.s3.delete_object(Bucket=env.raw_bucket, Key=CHECKPOINT_KEY)
    env.bedrock.requests.clear()
    env.metrics()
    return winner


RETRYABLE_ERRORS = [("ThrottlingException", 429), ("ModelTimeoutException", 408)]


@pytest.mark.parametrize(("code", "status"), RETRYABLE_ERRORS)
def test_record_committed_during_backoff_stops_the_retry(
    analysis_env: AnalysisEnv, code: str, status: int
) -> None:
    env = analysis_env
    state = env.seed_case()
    env.bedrock.error(code, status)
    env.during_sleep[1] = env.mark_processed

    result = env.summary(state)

    assert result == {
        "stage": "summary",
        "status": "alreadyProcessed",
        "accountId": env.account,
        "displayId": env.display,
        "processedKey": env.processed_key(),
    }
    assert len(env.bedrock.requests) == 1
    assert len(env.sleeps) == 1
    assert env.checkpoint("summary") is None
    metrics = env.metrics()
    assert metrics["StagesSkippedAlreadyProcessed"] == 1
    assert "BedrockInvocations" not in metrics
    assert "BedrockInvocationFailures" not in metrics
    if code == "ThrottlingException":
        assert metrics["BedrockThrottles"] == 1
    output = env.logs() + env.stdout
    for secret in env.sentinels:
        assert secret not in output


@pytest.mark.parametrize(("code", "status"), RETRYABLE_ERRORS)
def test_checkpoint_saved_during_backoff_is_reused_instead_of_retrying(
    analysis_env: AnalysisEnv, code: str, status: int
) -> None:
    env = analysis_env
    state = env.seed_case()
    winner = winning_checkpoint(env, state)
    env.bedrock.error(code, status)
    env.during_sleep[1] = lambda: env.put_checkpoint("summary", winner)

    result = env.summary(state)

    assert result["status"] == "reused"
    assert result["checkpointKey"] == CHECKPOINT_KEY
    assert len(env.bedrock.requests) == 1
    assert env.checkpoint("summary") == winner
    assert env.metrics()["CheckpointsReused"] == 1


def test_checkpoint_saved_during_correction_backoff_is_reused(
    analysis_env: AnalysisEnv,
) -> None:
    env = analysis_env
    state = env.seed_case()
    winner = winning_checkpoint(env, state)
    env.bedrock.reply("Too short.")
    env.bedrock.error("ThrottlingException", 429)
    env.during_sleep[1] = lambda: env.put_checkpoint("summary", winner)

    assert env.summary(state)["status"] == "reused"

    assert len(env.bedrock.requests) == 2
    assert env.bedrock.prompts(1)[1] == assistant("Too short.")
    assert len(env.sleeps) == 1
    assert env.checkpoint("summary") == winner


def test_record_committed_during_correction_backoff_stops_the_correction(
    analysis_env: AnalysisEnv,
) -> None:
    env = analysis_env
    state = env.seed_case()
    env.bedrock.reply("Too short.")
    env.bedrock.error("ServiceUnavailableException", 503)
    env.during_sleep[1] = env.mark_processed

    assert env.summary(state)["status"] == "alreadyProcessed"

    assert len(env.bedrock.requests) == 2
    assert env.checkpoint("summary") is None


@pytest.mark.parametrize(("code", "status"), RETRYABLE_ERRORS)
def test_retry_without_durable_state_is_still_attempted_within_bounds(
    analysis_env: AnalysisEnv, code: str, status: int
) -> None:
    env = analysis_env
    state = env.seed_case()
    env.bedrock.error(code, status, times=2)
    env.bedrock.reply(env.valid_summary)

    assert env.summary(state)["status"] == "completed"

    assert len(env.bedrock.requests) == 3
    assert len(env.sleeps) == 2
    checkpoint = env.checkpoint("summary")
    assert checkpoint is not None
    assert checkpoint["output"] == {"Case_Summary": env.valid_summary}

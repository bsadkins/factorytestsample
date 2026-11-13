"""Bedrock invocation helper using botocore Stubber."""

import io
import json
import logging
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber
from common.bedrock import (
    ANTHROPIC_VERSION,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL_ID,
    SUPPORTED_MODEL_IDS,
    BedrockError,
    BedrockInvocationError,
    BedrockResponseError,
    BedrockThrottlingError,
    BedrockTransientError,
    Message,
    PromptTooLargeError,
    RetrySupersededError,
    UnsupportedModelError,
    build_request_body,
    invoke_bedrock,
)
from common.retry import RetryPolicy
from types_boto3_bedrock_runtime.client import BedrockRuntimeClient

HAIKU = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
SONNET = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
FAST = RetryPolicy(max_attempts=3, base_delay_seconds=0.0, max_delay_seconds=0.0)


def client() -> BedrockRuntimeClient:
    return boto3.client(
        "bedrock-runtime", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y"
    )


def response(text: str = "Model reply.", stop: str = "end_turn") -> dict[str, Any]:
    payload = json.dumps(
        {
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop,
            "usage": {"input_tokens": 120, "output_tokens": 45},
        }
    ).encode()
    return {
        "body": StreamingBody(io.BytesIO(payload), len(payload)),
        "contentType": "application/json",
    }


def test_constants_match_the_approved_values() -> None:
    assert ANTHROPIC_VERSION == "bedrock-2023-05-31"
    assert SUPPORTED_MODEL_IDS == (HAIKU, SONNET)
    assert DEFAULT_MODEL_ID == HAIKU
    assert DEFAULT_MAX_TOKENS == 1000


def test_request_body_uses_anthropic_messages_format() -> None:
    body = build_request_body([Message("user", "Hello")], max_tokens=1000)
    assert body == {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1000,
        "temperature": 0.0,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
    }


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [Message("assistant", "x")],
        [Message("user", "a"), Message("user", "b")],
        [Message("user", "")],
    ],
)
def test_invalid_conversations_are_rejected(messages: list[Message]) -> None:
    with pytest.raises(ValueError, match="messages"):
        build_request_body(messages, max_tokens=1000)


@pytest.mark.parametrize("model", [HAIKU, SONNET])
def test_invoke_sends_exact_payload_and_parses_reply(model: str) -> None:
    c = client()
    expected_body = json.dumps(
        build_request_body([Message("user", "Summarise")], max_tokens=700)
    ).encode()
    with Stubber(c) as stub:
        stub.add_response(
            "invoke_model",
            response("  A summary.  "),
            {
                "modelId": model,
                "body": expected_body,
                "contentType": "application/json",
                "accept": "application/json",
            },
        )
        result = invoke_bedrock(c, model, "Summarise", max_tokens=700)
    assert result.text == "A summary."
    assert result.model_id == model
    assert (result.input_tokens, result.output_tokens) == (120, 45)
    assert result.stop_reason == "end_turn"
    assert result.attempts == 1
    assert result.throttle_count == 0
    assert result.latency_ms >= 0
    assert not result.truncated


def test_multi_turn_correction_messages() -> None:
    c = client()
    with Stubber(c) as stub:
        stub.add_response("invoke_model", response())
        result = invoke_bedrock(
            c,
            HAIKU,
            [Message("user", "classify"), Message("assistant", "bad"), Message("user", "fix it")],
            policy=FAST,
        )
    assert result.text == "Model reply."


@pytest.mark.parametrize(
    "model",
    ["anthropic.claude-3-haiku-20240307-v1:0", "us.anthropic.claude-haiku-4-5-20251001-v1:0", ""],
)
def test_unsupported_models_fail_without_calling_bedrock(model: str) -> None:
    c = client()
    with Stubber(c), pytest.raises(UnsupportedModelError):
        invoke_bedrock(c, model, "x")


@pytest.mark.parametrize("tokens", [0, 255, 4097])
def test_max_tokens_is_bounded(tokens: int) -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        invoke_bedrock(client(), HAIKU, "x", max_tokens=tokens)


def test_oversized_prompt_fails_explicitly_without_truncation() -> None:
    c = client()
    with Stubber(c), pytest.raises(PromptTooLargeError) as info:
        invoke_bedrock(c, HAIKU, "x" * 101, max_prompt_chars=100)
    assert info.value.code == "PromptTooLarge"


def test_throttling_is_retried_and_counted() -> None:
    c = client()
    with Stubber(c) as stub:
        stub.add_client_error(
            "invoke_model", service_error_code="ThrottlingException", http_status_code=429
        )
        stub.add_response("invoke_model", response())
        result = invoke_bedrock(c, HAIKU, "x", policy=FAST, sleep=lambda _: None)
    assert result.attempts == 2
    assert result.throttle_count == 1


def test_exhausted_throttling_raises_throttling_error() -> None:
    c = client()
    with Stubber(c) as stub:
        for _ in range(3):
            stub.add_client_error(
                "invoke_model", service_error_code="ThrottlingException", http_status_code=429
            )
        with pytest.raises(BedrockThrottlingError) as info:
            invoke_bedrock(c, HAIKU, "x", policy=FAST, sleep=lambda _: None)
    assert info.value.throttle_count == 3
    assert info.value.code == "ThrottlingException"


def test_exhausted_service_unavailable_raises_transient_error() -> None:
    c = client()
    with Stubber(c) as stub:
        for _ in range(3):
            stub.add_client_error(
                "invoke_model",
                service_error_code="ServiceUnavailableException",
                http_status_code=503,
            )
        with pytest.raises(BedrockTransientError):
            invoke_bedrock(c, HAIKU, "x", policy=FAST, sleep=lambda _: None)


def test_validation_errors_are_not_retried() -> None:
    c = client()
    with Stubber(c) as stub:
        stub.add_client_error(
            "invoke_model", service_error_code="AccessDeniedException", http_status_code=403
        )
        with pytest.raises(BedrockInvocationError) as info:
            invoke_bedrock(c, HAIKU, "x", policy=FAST, sleep=lambda _: None)
    assert not isinstance(info.value, BedrockTransientError)
    assert info.value.code == "AccessDeniedException"


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b'{"content": []}',
        b'{"content": [{"type": "text", "text": "   "}]}',
        b'{"content": "x"}',
    ],
)
def test_malformed_responses_raise(payload: bytes) -> None:
    c = client()
    with Stubber(c) as stub:
        stub.add_response(
            "invoke_model",
            {
                "body": StreamingBody(io.BytesIO(payload), len(payload)),
                "contentType": "application/json",
            },
        )
        with pytest.raises(BedrockResponseError):
            invoke_bedrock(c, HAIKU, "x", policy=FAST)


def test_max_tokens_stop_reason_marks_truncation() -> None:
    c = client()
    with Stubber(c) as stub:
        stub.add_response("invoke_model", response(stop="max_tokens"))
        assert invoke_bedrock(c, HAIKU, "x").truncated


def test_prompts_and_replies_are_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    c = client()
    with Stubber(c) as stub:
        stub.add_client_error(
            "invoke_model",
            service_error_code="ThrottlingException",
            service_message="SENTINEL-CASE-TEXT-7f3a in message",
            http_status_code=429,
        )
        stub.add_response("invoke_model", response("SENTINEL-CASE-TEXT-7f3a reply"))
        invoke_bedrock(
            c, HAIKU, "SENTINEL-CASE-TEXT-7f3a prompt", policy=FAST, sleep=lambda _: None
        )
    assert caplog.records
    assert all("SENTINEL" not in repr(r.__dict__) for r in caplog.records)


def test_error_strings_never_contain_prompts() -> None:
    c = client()
    with Stubber(c) as stub:
        stub.add_client_error(
            "invoke_model",
            service_error_code="ValidationException",
            service_message="SENTINEL-CASE-TEXT-7f3a",
        )
        with pytest.raises(BedrockInvocationError) as info:
            invoke_bedrock(c, HAIKU, "SENTINEL-CASE-TEXT-7f3a", policy=FAST)
    assert "SENTINEL" not in str(info.value)


def recording_client(events: list[str]) -> BedrockRuntimeClient:
    c = client()
    c.meta.events.register(
        "provide-client-params.bedrock-runtime.InvokeModel",
        lambda **_: events.append("invoke"),
    )
    return c


def test_retry_guard_runs_after_each_backoff_immediately_before_the_retry() -> None:
    events: list[str] = []
    c = recording_client(events)

    def guard() -> None:
        events.append("guard")

    with Stubber(c) as stub:
        for code in ("ThrottlingException", "ModelTimeoutException"):
            stub.add_client_error("invoke_model", service_error_code=code, http_status_code=429)
        stub.add_response("invoke_model", response())
        result = invoke_bedrock(
            c, HAIKU, "x", policy=FAST, sleep=lambda _: events.append("sleep"), before_retry=guard
        )
    assert events == ["invoke", "sleep", "guard", "invoke", "sleep", "guard", "invoke"]
    assert result.attempts == 3
    assert result.throttle_count == 1


def test_retry_guard_outcome_stops_retrying_without_another_invocation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    events: list[str] = []
    c = recording_client(events)
    outcome = {"status": "alreadyProcessed"}
    with Stubber(c) as stub:
        stub.add_client_error(
            "invoke_model", service_error_code="ThrottlingException", http_status_code=429
        )
        with pytest.raises(RetrySupersededError) as info:
            invoke_bedrock(
                c,
                HAIKU,
                "SENTINEL-CASE-TEXT-7f3a prompt",
                policy=FAST,
                sleep=lambda _: events.append("sleep"),
                before_retry=lambda: outcome,
            )
        stub.assert_no_pending_responses()
    assert events == ["invoke", "sleep"]
    assert info.value.outcome is outcome
    assert info.value.attempts == 1
    assert info.value.throttle_count == 1
    assert info.value.model_id == HAIKU
    assert "SENTINEL" not in str(info.value)
    assert any("Bedrock retry skipped" in r.getMessage() for r in caplog.records)
    assert all("SENTINEL" not in repr(r.__dict__) for r in caplog.records)


def test_retry_guard_errors_propagate_unchanged() -> None:
    c = client()
    guard_error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "synthetic"}}, "HeadObject"
    )

    def guard() -> None:
        raise guard_error

    with Stubber(c) as stub:
        stub.add_client_error(
            "invoke_model", service_error_code="ThrottlingException", http_status_code=429
        )
        with pytest.raises(ClientError) as info:
            invoke_bedrock(c, HAIKU, "x", policy=FAST, sleep=lambda _: None, before_retry=guard)
    assert info.value is guard_error
    assert not isinstance(info.value, BedrockError)


def test_retry_guard_is_not_consulted_without_a_retry() -> None:
    calls: list[str] = []
    c = client()
    with Stubber(c) as stub:
        stub.add_response("invoke_model", response())
        stub.add_client_error(
            "invoke_model", service_error_code="AccessDeniedException", http_status_code=403
        )
        invoke_bedrock(c, HAIKU, "x", policy=FAST, before_retry=lambda: calls.append("guard"))
        with pytest.raises(BedrockInvocationError):
            invoke_bedrock(c, HAIKU, "x", policy=FAST, before_retry=lambda: calls.append("guard"))
    assert calls == []

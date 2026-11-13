"""Anthropic messages requests to the approved global Bedrock inference profiles.

Only the two approved global profiles are accepted and there is no implicit fallback model.
Prompts over the configured size fail explicitly rather than being truncated. Prompts and model
replies are never logged.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Final, Literal

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

from common.aws import client_config
from common.retry import (
    THROTTLING_ERROR_CODES,
    RetryPolicy,
    is_retryable_error,
    is_throttling_error,
    retry_with_backoff,
)
from common.safe_logging import aws_error_code, error_fields

if TYPE_CHECKING:
    from types_boto3_bedrock_runtime.client import BedrockRuntimeClient
    from types_boto3_bedrock_runtime.type_defs import InvokeModelResponseTypeDef

logger = Logger(child=True)

ANTHROPIC_VERSION: Final = "bedrock-2023-05-31"
HAIKU_4_5_GLOBAL_PROFILE: Final = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
SONNET_4_5_GLOBAL_PROFILE: Final = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
SUPPORTED_MODEL_IDS: Final = (HAIKU_4_5_GLOBAL_PROFILE, SONNET_4_5_GLOBAL_PROFILE)
DEFAULT_MODEL_ID: Final = HAIKU_4_5_GLOBAL_PROFILE
DEFAULT_MAX_TOKENS: Final = 1000
MIN_MAX_TOKENS: Final = 256
MAX_MAX_TOKENS: Final = 4096
DEFAULT_MAX_PROMPT_CHARS: Final = 400_000
MAX_RESPONSE_BYTES: Final = 1024 * 1024
BEDROCK_RETRY_POLICY: Final = RetryPolicy(
    max_attempts=4, base_delay_seconds=1.0, max_delay_seconds=16.0
)

Role = Literal["user", "assistant"]


@dataclass(frozen=True, slots=True)
class Message:
    """One conversation turn."""

    role: Role
    text: str


@dataclass(frozen=True, slots=True)
class BedrockResult:
    """A parsed model reply plus safe telemetry (tokens, latency, attempts, throttles)."""

    text: str
    model_id: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None
    latency_ms: int
    attempts: int
    throttle_count: int

    @property
    def truncated(self) -> bool:
        """True when the model stopped because it reached `max_tokens`."""
        return self.stop_reason == "max_tokens"


class BedrockError(Exception):
    """Base error. Messages carry the model ID and error code, never prompt or reply text."""

    def __init__(self, code: str, model_id: str, throttle_count: int = 0) -> None:
        super().__init__(f"Bedrock invocation of {model_id} failed ({code})")
        self.code = code
        self.model_id = model_id
        self.throttle_count = throttle_count


class UnsupportedModelError(BedrockError, ValueError):
    """The model ID is not one of the approved global inference profiles."""


class PromptTooLargeError(BedrockError):
    """The prompt exceeds the configured size and was not sent."""


class BedrockInvocationError(BedrockError):
    """InvokeModel failed with a non-retryable error."""


class BedrockTransientError(BedrockInvocationError):
    """InvokeModel kept failing with transient service errors after bounded retries."""


class BedrockThrottlingError(BedrockTransientError):
    """InvokeModel kept being throttled after bounded retries."""


class BedrockResponseError(BedrockError):
    """The model response could not be parsed or contained no text."""


class RetrySupersededError(Exception):
    """A `before_retry` guard found a durable result, so the retry was not sent.

    Not a failure: `outcome` is the guard's return value for the caller to use instead of a
    model reply. The message carries the model ID and counts only.
    """

    def __init__(self, outcome: object, model_id: str, attempts: int, throttle_count: int) -> None:
        super().__init__(f"Bedrock retry of {model_id} superseded after {attempts} attempt(s)")
        self.outcome = outcome
        self.model_id = model_id
        self.attempts = attempts
        self.throttle_count = throttle_count


class _GuardFailure(Exception):  # noqa: N818
    """Carries an exception raised by a `before_retry` guard past the InvokeModel error mapping."""

    def __init__(self, error: Exception) -> None:
        super().__init__(type(error).__name__)
        self.error = error


def validate_model_id(model_id: str) -> str:
    """Return `model_id` if approved, otherwise raise `UnsupportedModelError`."""
    if model_id not in SUPPORTED_MODEL_IDS:
        raise UnsupportedModelError("UnsupportedModel", model_id or "<empty>")
    return model_id


def _validate_messages(messages: Sequence[Message]) -> None:
    if not messages or messages[0].role != "user" or messages[-1].role != "user":
        raise ValueError("messages must start and end with a user turn")
    for previous, current in pairwise(messages):
        if previous.role == current.role:
            raise ValueError("messages must alternate between user and assistant turns")
    if any(not message.text.strip() for message in messages):
        raise ValueError("messages must not be empty")


def build_request_body(
    messages: Sequence[Message],
    *,
    max_tokens: int,
    temperature: float = 0.0,
    system: str | None = None,
) -> dict[str, Any]:
    """Build the Anthropic messages request body with `anthropic_version` `bedrock-2023-05-31`."""
    _validate_messages(messages)
    body: dict[str, Any] = {
        "anthropic_version": ANTHROPIC_VERSION,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [
            {"role": m.role, "content": [{"type": "text", "text": m.text}]} for m in messages
        ],
    }
    if system is not None:
        body["system"] = system
    return body


def _parse_response(raw: bytes, model_id: str) -> tuple[str, int, int, str | None]:
    try:
        payload = json.loads(raw)
    except ValueError:
        raise BedrockResponseError("MalformedResponse", model_id) from None
    content = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(content, list):
        raise BedrockResponseError("MalformedResponse", model_id)
    text = "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()
    if not text:
        raise BedrockResponseError("EmptyResponse", model_id)
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    stop_reason = payload.get("stop_reason")
    return (
        text,
        int(usage.get("input_tokens", 0)),
        int(usage.get("output_tokens", 0)),
        stop_reason if isinstance(stop_reason, str) else None,
    )


def _guarded_sleep(
    sleep: Callable[[float], None],
    before_retry: Callable[[], object | None] | None,
    superseded: Callable[[object], RetrySupersededError],
) -> Callable[[float], None]:
    """Wrap a backoff `sleep` so `before_retry` runs after the delay, before the next attempt."""
    if before_retry is None:
        return sleep

    def pause(delay: float) -> None:
        sleep(delay)
        try:
            outcome = before_retry()
        except Exception as exc:
            raise _GuardFailure(exc) from exc
        if outcome is not None:
            raise superseded(outcome)

    return pause


def _read_reply(
    response: InvokeModelResponseTypeDef, model_id: str, throttles: int
) -> tuple[str, int, int, str | None]:
    stream = response["body"]
    try:
        raw = stream.read(MAX_RESPONSE_BYTES + 1)
    finally:
        stream.close()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise BedrockResponseError("ResponseTooLarge", model_id, throttles)
    return _parse_response(raw, model_id)


def invoke_bedrock(  # noqa: PLR0913
    client: BedrockRuntimeClient,
    model_id: str,
    prompt: str | Sequence[Message],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.0,
    system: str | None = None,
    max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS,
    policy: RetryPolicy = BEDROCK_RETRY_POLICY,
    sleep: Callable[[float], None] = time.sleep,
    deadline: float | None = None,
    before_retry: Callable[[], object | None] | None = None,
) -> BedrockResult:
    """Invoke an approved model with the Anthropic messages format and return its text reply.

    Throttling and transient errors are retried with bounded exponential backoff; exhausted
    retries raise `BedrockThrottlingError` or `BedrockTransientError`, which workflow Retry rules
    can match by name. Other service errors raise `BedrockInvocationError`.

    `before_retry` runs after every backoff delay, immediately before the retried InvokeModel
    call. When it returns a value other than `None` no further call is made and
    `RetrySupersededError` carrying that value is raised. Exceptions it raises propagate
    unchanged.
    """
    validate_model_id(model_id)
    if not MIN_MAX_TOKENS <= max_tokens <= MAX_MAX_TOKENS:
        raise ValueError(f"max_tokens must be between {MIN_MAX_TOKENS} and {MAX_MAX_TOKENS}")
    messages = [Message("user", prompt)] if isinstance(prompt, str) else list(prompt)
    prompt_chars = sum(len(m.text) for m in messages) + len(system or "")
    if prompt_chars > max_prompt_chars:
        logger.warning(
            "Prompt exceeds the configured size and was not sent",
            extra={"model_id": model_id, "prompt_chars": prompt_chars, "limit": max_prompt_chars},
        )
        raise PromptTooLargeError("PromptTooLarge", model_id)
    body = json.dumps(
        build_request_body(messages, max_tokens=max_tokens, temperature=temperature, system=system)
    ).encode("utf-8")

    attempts = 0
    throttles = 0

    def call() -> InvokeModelResponseTypeDef:
        nonlocal attempts, throttles
        attempts += 1
        try:
            return client.invoke_model(
                modelId=model_id,
                body=body,
                contentType="application/json",
                accept="application/json",
            )
        except ClientError as exc:
            if is_throttling_error(exc):
                throttles += 1
            raise

    started = time.monotonic()
    try:
        response = retry_with_backoff(
            call,
            policy=policy,
            sleep=_guarded_sleep(
                sleep,
                before_retry,
                lambda outcome: RetrySupersededError(outcome, model_id, attempts, throttles),
            ),
            deadline=deadline,
            operation_name="bedrock:InvokeModel",
        )
    except _GuardFailure as failure:
        raise failure.error from None
    except RetrySupersededError:
        logger.info(
            "Bedrock retry skipped; a durable result already exists",
            extra={"model_id": model_id, "attempts": attempts, "throttles": throttles},
        )
        raise
    except ClientError as exc:
        code = aws_error_code(exc) or "Unknown"
        logger.warning(
            "Bedrock invocation failed",
            extra={
                "model_id": model_id,
                "attempts": attempts,
                "throttles": throttles,
                **error_fields(exc),
            },
        )
        if code in THROTTLING_ERROR_CODES:
            raise BedrockThrottlingError(code, model_id, throttles) from None
        if is_retryable_error(exc):
            raise BedrockTransientError(code, model_id, throttles) from None
        raise BedrockInvocationError(code, model_id, throttles) from None

    text, input_tokens, output_tokens, stop_reason = _read_reply(response, model_id, throttles)
    latency_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "Bedrock invocation complete",
        extra={
            "model_id": model_id,
            "latency_ms": latency_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "stop_reason": stop_reason,
            "attempts": attempts,
            "throttles": throttles,
        },
    )
    return BedrockResult(
        text=text,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        stop_reason=stop_reason,
        latency_ms=latency_ms,
        attempts=attempts,
        throttle_count=throttles,
    )


def bedrock_runtime_client(
    *, region_name: str | None = None, session: boto3.Session | None = None
) -> BedrockRuntimeClient:
    """Create a Bedrock runtime client whose retries are owned by `invoke_bedrock`."""
    config = client_config(max_attempts=1, read_timeout=120)
    factory = session or boto3.Session()
    return factory.client("bedrock-runtime", region_name=region_name, config=config)

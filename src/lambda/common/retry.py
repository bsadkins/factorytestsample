"""Bounded exponential backoff with full jitter for AWS calls."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from aws_lambda_powertools import Logger
from botocore.exceptions import (
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from common.safe_logging import aws_error_code, error_fields, http_status

logger = Logger(child=True)

THROTTLING_ERROR_CODES: Final = frozenset(
    {
        "Throttling",
        "ThrottlingException",
        "ThrottledException",
        "RequestThrottled",
        "RequestThrottledException",
        "RequestLimitExceeded",
        "TooManyRequestsException",
        "ProvisionedThroughputExceededException",
        "SlowDown",
        "BandwidthLimitExceeded",
    }
)
TRANSIENT_ERROR_CODES: Final = frozenset(
    {
        "InternalError",
        "InternalFailure",
        "InternalServerError",
        "InternalServerException",
        "ServiceUnavailable",
        "ServiceUnavailableException",
        "ModelNotReadyException",
        "ModelTimeoutException",
        "RequestTimeout",
        "RequestTimeoutException",
        "PriorRequestNotComplete",
        "IDPCommunicationError",
    }
)
_RETRYABLE_STATUS: Final = frozenset({500, 502, 503, 504})
_NETWORK_ERRORS: Final = (
    EndpointConnectionError,
    ConnectionClosedError,
    ConnectTimeoutError,
    ReadTimeoutError,
)
_SYSTEM_RANDOM: Final = random.SystemRandom()


def error_code(exc: BaseException) -> str | None:
    """Return the AWS error code for a botocore `ClientError`, otherwise `None`."""
    return aws_error_code(exc)


def is_throttling_error(exc: BaseException) -> bool:
    """True for AWS throttling errors."""
    return error_code(exc) in THROTTLING_ERROR_CODES


def is_retryable_error(exc: BaseException) -> bool:
    """True for throttling, transient server errors, 5xx responses and network failures."""
    if isinstance(exc, _NETWORK_ERRORS):
        return True
    code = error_code(exc)
    if code is None:
        return False
    return (
        code in THROTTLING_ERROR_CODES
        or code in TRANSIENT_ERROR_CODES
        or http_status(exc) in _RETRYABLE_STATUS
    )


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Retry bounds. `max_attempts` counts the first call; delays are capped at 60 seconds."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 10:  # noqa: PLR2004
            raise ValueError("max_attempts must be between 1 and 10")
        if self.base_delay_seconds < 0:
            raise ValueError("base_delay_seconds must not be negative")
        if not self.base_delay_seconds <= self.max_delay_seconds <= 60:  # noqa: PLR2004
            raise ValueError("max_delay_seconds must be between base_delay_seconds and 60")

    def ceiling(self, attempt: int) -> float:
        """Upper bound of the delay after failed attempt number `attempt` (1-based)."""
        return float(min(self.max_delay_seconds, self.base_delay_seconds * 2 ** (attempt - 1)))

    def delay(self, attempt: int) -> float:
        """Full-jitter delay after failed attempt `attempt`."""
        return _SYSTEM_RANDOM.uniform(0, self.ceiling(attempt))


DEFAULT_RETRY_POLICY: Final = RetryPolicy()


def retry_with_backoff[T](  # noqa: PLR0913
    operation: Callable[[], T],
    *,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    is_retryable: Callable[[BaseException], bool] = is_retryable_error,
    sleep: Callable[[float], None] = time.sleep,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    operation_name: str = "aws_call",
) -> T:
    """Call `operation` until it succeeds, retrying retryable errors with bounded backoff.

    The last exception is re-raised unchanged when attempts run out, when an error is not
    retryable, or when the next delay would pass `deadline` (a `clock()` timestamp). Log
    records contain only the attempt number, delay and error type/code.
    """
    attempt = 1
    while True:
        try:
            return operation()
        except Exception as exc:
            if attempt >= policy.max_attempts or not is_retryable(exc):
                raise
            delay = policy.delay(attempt)
            if deadline is not None and clock() + delay >= deadline:
                raise
            logger.warning(
                "Retrying after retryable error",
                extra={
                    "operation": operation_name,
                    "attempt": attempt,
                    "max_attempts": policy.max_attempts,
                    "delay_seconds": round(delay, 3),
                    **error_fields(exc),
                },
            )
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            sleep(delay)
            attempt += 1

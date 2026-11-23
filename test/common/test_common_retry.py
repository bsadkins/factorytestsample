"""Bounded exponential backoff with jitter."""

import logging
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError
from common.retry import (
    RetryPolicy,
    error_code,
    is_retryable_error,
    is_throttling_error,
    retry_with_backoff,
)


def client_error(code: str, status: int = 400, message: str = "boom") -> ClientError:
    response: Any = {
        "Error": {"Code": code, "Message": message},
        "ResponseMetadata": {"HTTPStatusCode": status},
    }
    return ClientError(response, "Operation")


class Flaky:
    def __init__(self, failures: list[BaseException], result: str = "ok") -> None:
        self.failures = failures
        self.calls = 0
        self.result = result

    def __call__(self) -> str:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.result


def test_succeeds_after_transient_failures_with_bounded_delays() -> None:
    sleeps: list[float] = []
    op = Flaky([client_error("ThrottlingException"), client_error("ServiceUnavailable", 503)])
    policy = RetryPolicy(max_attempts=3, base_delay_seconds=1.0, max_delay_seconds=1.5)
    assert retry_with_backoff(op, policy=policy, sleep=sleeps.append) == "ok"
    assert op.calls == 3
    assert len(sleeps) == 2
    assert all(0 <= delay <= 1.5 for delay in sleeps)


def test_reraises_last_error_after_max_attempts() -> None:
    op = Flaky([client_error("ThrottlingException")] * 5)
    with pytest.raises(ClientError):
        retry_with_backoff(op, policy=RetryPolicy(max_attempts=3), sleep=lambda _: None)
    assert op.calls == 3


def test_non_retryable_errors_fail_immediately() -> None:
    op = Flaky([client_error("AccessDeniedException", 403)])
    with pytest.raises(ClientError):
        retry_with_backoff(op, sleep=lambda _: None)
    assert op.calls == 1


def test_deadline_stops_retries_early() -> None:
    op = Flaky([client_error("ThrottlingException")] * 5)
    with pytest.raises(ClientError):
        retry_with_backoff(
            op,
            policy=RetryPolicy(max_attempts=5, base_delay_seconds=1.0),
            sleep=lambda _: None,
            deadline=50.0,
            clock=lambda: 100.0,
        )
    assert op.calls == 1


def test_on_retry_callback_receives_attempt_and_error() -> None:
    seen: list[tuple[int, str | None]] = []
    op = Flaky([client_error("TooManyRequestsException")])
    retry_with_backoff(
        op,
        sleep=lambda _: None,
        on_retry=lambda attempt, exc, _d: seen.append((attempt, error_code(exc))),
    )
    assert seen == [(1, "TooManyRequestsException")]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_attempts": 0}, "max_attempts"),
        ({"max_attempts": 11}, "max_attempts"),
        ({"base_delay_seconds": -1}, "base_delay_seconds"),
        ({"max_delay_seconds": 61}, "max_delay_seconds"),
        ({"base_delay_seconds": 5, "max_delay_seconds": 1}, "max_delay_seconds"),
    ],
)
def test_policy_is_bounded(kwargs: dict[str, float], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        RetryPolicy(**kwargs)  # type: ignore[arg-type]


def test_delay_grows_exponentially_up_to_cap() -> None:
    policy = RetryPolicy(max_attempts=10, base_delay_seconds=0.5, max_delay_seconds=4.0)
    assert [policy.ceiling(n) for n in range(1, 6)] == [0.5, 1.0, 2.0, 4.0, 4.0]


def test_error_classification() -> None:
    assert is_throttling_error(client_error("Throttling"))
    assert is_throttling_error(client_error("SlowDown", 503))
    assert not is_throttling_error(client_error("InternalServerException", 500))
    assert is_retryable_error(client_error("InternalServerException", 500))
    assert is_retryable_error(client_error("SomethingNew", 502))
    assert is_retryable_error(EndpointConnectionError(endpoint_url="https://example.invalid"))
    assert is_retryable_error(ReadTimeoutError(endpoint_url="https://example.invalid"))
    assert not is_retryable_error(client_error("ValidationException"))
    assert not is_retryable_error(ValueError("x"))
    assert error_code(ValueError("x")) is None


def test_retry_logs_never_include_error_messages(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    op = Flaky([client_error("ThrottlingException", message="SENTINEL-CASE-TEXT-7f3a")])
    retry_with_backoff(op, sleep=lambda _: None, operation_name="unit")
    assert caplog.records
    for record in caplog.records:
        assert "SENTINEL" not in repr(record.__dict__)

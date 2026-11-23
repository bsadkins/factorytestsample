"""SQS batch handling with per-record Powertools idempotency and partial-batch failures.

Each consumer passes a record handler to `process_sqs_batch`; failed records are reported in
`batchItemFailures` so only they return to the queue. `run_idempotent` wraps one record's work
in Powertools idempotency keyed by a business key that includes the run scope, so a cached
result can only suppress duplicates within the same run.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aws_lambda_powertools.utilities.batch import (
    BatchProcessor,
    EventType,
    process_partial_response,
)
from aws_lambda_powertools.utilities.idempotency import IdempotencyConfig, idempotent_function
from aws_lambda_powertools.utilities.idempotency.exceptions import (
    IdempotencyAlreadyInProgressError,
)

from common.idempotency import persistence_layer
from common.safe_logging import aws_error_code

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.batch.types import PartialItemFailureResponse
    from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord
    from aws_lambda_powertools.utilities.typing import LambdaContext


class RecordFailedError(Exception):
    """A record failed and should be retried. Carries an error type and code, never content."""

    def __init__(self, error_type: str, code: str | None = None) -> None:
        super().__init__(f"Record processing failed ({error_type}{', ' + code if code else ''})")
        self.error_type = error_type
        self.code = code

    @classmethod
    def from_exception(cls, exc: BaseException) -> RecordFailedError:
        """Describe `exc` by type and AWS error code only; raise the result `from None`."""
        if isinstance(exc, RecordFailedError):
            return exc
        return cls(type(exc).__name__, aws_error_code(exc))

    def log_fields(self) -> dict[str, Any]:
        """Safe log fields for this failure."""
        fields: dict[str, Any] = {"error_type": self.error_type}
        if self.code is not None:
            fields["error_code"] = self.code
        return fields


@dataclass(frozen=True, slots=True)
class IdempotentOutcome:
    """Result of `run_idempotent`. `duplicate` is True for replays and in-progress duplicates."""

    result: dict[str, Any] | None
    duplicate: bool
    in_progress: bool = False


def run_idempotent(
    func: Callable[..., dict[str, Any]],
    key: Mapping[str, Any],
    *,
    config: IdempotencyConfig,
    **kwargs: Any,  # noqa: ANN401
) -> IdempotentOutcome:
    """Call `func(key=..., invocation=..., **kwargs)` at most once per `key`.

    `func` must return a JSON-serialisable dict that includes the `invocation` it received;
    a returned token that differs from this call's token means the result was replayed from the
    idempotency store. A failure is not cached, so a retry runs `func` again.
    """
    invocation = uuid.uuid4().hex
    wrapped = idempotent_function(
        func,
        data_keyword_argument="key",
        persistence_store=persistence_layer(),
        config=config,
    )
    try:
        result = wrapped(key=dict(key), invocation=invocation, **kwargs)
    except IdempotencyAlreadyInProgressError:
        return IdempotentOutcome(result=None, duplicate=True, in_progress=True)
    return IdempotentOutcome(result=result, duplicate=result.get("invocation") != invocation)


def process_sqs_batch(
    event: dict[str, Any],
    context: LambdaContext,
    record_handler: Callable[[SQSRecord], Any],
) -> PartialItemFailureResponse:
    """Process every SQS record and report only the failed ones.

    A batch in which every record failed is reported the same way rather than raised, so error
    text never reaches the Lambda error log; failures are counted by the handlers' metrics.
    """
    processor = BatchProcessor(event_type=EventType.SQS, raise_on_entire_batch_failure=False)
    return process_partial_response(
        event=event, record_handler=record_handler, processor=processor, context=context
    )

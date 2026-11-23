"""Batched SQS sends with bounded retries, and SQS event-record parsing.

Queue messages carry references (account, case, run and S3 keys), never case content.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger

from common.retry import RetryPolicy, retry_with_backoff

if TYPE_CHECKING:
    from types_boto3_sqs.client import SQSClient
    from types_boto3_sqs.type_defs import (
        SendMessageBatchRequestEntryTypeDef,
        SendMessageBatchResultTypeDef,
    )

logger = Logger(child=True)

MAX_BATCH_ENTRIES: Final = 10
MAX_MESSAGE_BYTES: Final = 256 * 1024
SQS_RETRY_POLICY: Final = RetryPolicy(max_attempts=3, base_delay_seconds=0.2, max_delay_seconds=2.0)


class SqsSendError(Exception):
    """Some messages could not be sent. Carries counts and error codes only."""

    def __init__(self, failed: int, codes: tuple[str, ...]) -> None:
        super().__init__(f"{failed} SQS message(s) could not be sent ({', '.join(codes)})")
        self.failed = failed
        self.codes = codes


class InvalidMessageError(ValueError):
    """An SQS record body is not a JSON object. Carries the message ID only."""

    def __init__(self, message_id: str | None) -> None:
        super().__init__(f"SQS message {message_id} does not contain a JSON object body")
        self.message_id = message_id


def encode_body(body: Mapping[str, Any]) -> str:
    """Serialise a message body as compact, key-sorted JSON within the SQS size limit."""
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("SQS message body is too large")
    return text


def _batches(bodies: Sequence[str]) -> list[list[tuple[str, str]]]:
    batches: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    size = 0
    for index, body in enumerate(bodies):
        body_size = len(body.encode("utf-8"))
        if current and (len(current) == MAX_BATCH_ENTRIES or size + body_size > MAX_MESSAGE_BYTES):
            batches.append(current)
            current, size = [], 0
        current.append((str(index), body))
        size += body_size
    if current:
        batches.append(current)
    return batches


def _send_batch(
    sqs: SQSClient,
    queue_url: str,
    batch: list[tuple[str, str]],
    policy: RetryPolicy,
    sleep: Callable[[float], None],
) -> tuple[int, list[str]]:
    pending = dict(batch)
    sent = 0
    codes: list[str] = []
    for attempt in range(1, policy.max_attempts + 1):
        entries: list[SendMessageBatchRequestEntryTypeDef] = [
            {"Id": entry_id, "MessageBody": body} for entry_id, body in pending.items()
        ]

        def send(
            entries: list[SendMessageBatchRequestEntryTypeDef] = entries,
        ) -> SendMessageBatchResultTypeDef:
            return sqs.send_message_batch(QueueUrl=queue_url, Entries=entries)

        response = retry_with_backoff(
            send,
            policy=policy,
            sleep=sleep,
            operation_name="sqs:SendMessageBatch",
        )
        for success in response.get("Successful", []):
            if pending.pop(success["Id"], None) is not None:
                sent += 1
        failed = response.get("Failed", [])
        codes = [failure.get("Code", "Unknown") for failure in failed]
        permanent = [f for f in failed if f.get("SenderFault")]
        if not pending or permanent or attempt == policy.max_attempts:
            break
        logger.warning(
            "Retrying failed SQS batch entries",
            extra={"attempt": attempt, "failed_entries": len(pending), "error_codes": codes},
        )
        sleep(policy.delay(attempt))
    return sent, codes if pending else []


def send_messages(
    sqs: SQSClient,
    queue_url: str,
    bodies: Sequence[Mapping[str, Any]],
    *,
    policy: RetryPolicy = SQS_RETRY_POLICY,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Send each body as one message, in batches of at most 10 entries and 256 KiB.

    Failed entries without sender fault are retried within `policy`. Raises `SqsSendError` if any
    message remains unsent, so callers can report the SQS record as a partial-batch failure.
    """
    encoded = [encode_body(body) for body in bodies]
    total_sent = 0
    unsent = 0
    codes: set[str] = set()
    for batch in _batches(encoded):
        sent, failed_codes = _send_batch(sqs, queue_url, batch, policy, sleep)
        total_sent += sent
        unsent += len(batch) - sent
        codes.update(failed_codes)
    if unsent:
        logger.error(
            "SQS messages were not sent", extra={"failed": unsent, "error_codes": sorted(codes)}
        )
        raise SqsSendError(unsent, tuple(sorted(codes)) or ("Unknown",))
    return total_sent


def parse_message_body(record: Mapping[str, Any]) -> dict[str, Any]:
    """Parse the JSON object body of an SQS event record; raise `InvalidMessageError` otherwise."""
    message_id = record.get("messageId")
    body = record.get("body")
    if not isinstance(body, str):
        raise InvalidMessageError(message_id)
    try:
        parsed = json.loads(body)
    except ValueError:
        raise InvalidMessageError(message_id) from None
    if not isinstance(parsed, dict):
        raise InvalidMessageError(message_id)
    return parsed

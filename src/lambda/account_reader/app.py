"""Account Reader: turn a new account-list object into one queue message per account.

Triggered by S3 `ObjectCreated` notifications for `account-lists/*.json`. Each valid list
produces `{accountId, accountName, runId}` messages on the active-accounts queue. Events for
other objects are ignored; malformed lists fail the invocation so they are retried and then
reach the failure queue, and are never treated as processed. Powertools idempotency keyed by
bucket, key and version stops duplicate S3 deliveries from enqueueing the run twice.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import unquote_plus

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.idempotency import idempotent_function
from aws_lambda_powertools.utilities.idempotency.exceptions import (
    IdempotencyAlreadyInProgressError,
)
from common.account_list import (
    MAX_ACCOUNT_LIST_BYTES,
    InvalidAccountListError,
    parse_account_list_document,
    parse_account_list_key,
)
from common.aws import client_config
from common.idempotency import idempotency_config, persistence_layer
from common.processed import validate_account_id
from common.s3_json import S3JsonDecodeError, S3ObjectTooLargeError, read_json
from common.safe_logging import error_fields
from common.sqs import send_messages
from common.tracing import withhold_exception_messages

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext
    from types_boto3_s3.client import S3Client
    from types_boto3_sqs.client import SQSClient

logger = Logger()
tracer = Tracer()
metrics = Metrics()
withhold_exception_messages()

# Long enough to cover S3's duplicate deliveries and Lambda's asynchronous retries; the key
# includes the run's object identity, so expiry never lets another run collide.
IDEMPOTENCY_EXPIRY_SECONDS: Final = 7 * 24 * 60 * 60
IDEMPOTENCY_CONFIG: Final = idempotency_config(expires_after_seconds=IDEMPOTENCY_EXPIRY_SECONDS)


class AccountListProcessingError(Exception):
    """One or more account lists in the event could not be processed. Carries counts only."""

    def __init__(self, failed: int) -> None:
        super().__init__(f"{failed} account list(s) could not be processed")
        self.failed = failed


class AccountReaderError(Exception):
    """The event failed before its account lists could be evaluated. Carries no details.

    Lambda records the message of an unhandled exception in its logs and in the asynchronous
    OnFailure record, and AWS error messages can echo request content.
    """


@dataclass(frozen=True, slots=True)
class Settings:
    account_list_bucket: str
    queue_url: str
    management_account_id: str

    @classmethod
    def from_env(cls) -> Settings:
        management = os.environ["MANAGEMENT_ACCOUNT_ID"]
        try:
            validate_account_id(management)
        except ValueError:
            raise ValueError("MANAGEMENT_ACCOUNT_ID must be a 12-digit account ID") from None
        return cls(
            account_list_bucket=os.environ["ACCOUNT_LIST_BUCKET"],
            queue_url=os.environ["ACTIVE_ACCOUNTS_QUEUE_URL"],
            management_account_id=management,
        )


@dataclass(frozen=True, slots=True)
class Clients:
    s3: S3Client
    sqs: SQSClient


@cache
def clients() -> Clients:
    return Clients(
        s3=boto3.client("s3", config=client_config()),
        sqs=boto3.client("sqs", config=client_config()),
    )


def account_list_object(record: object, bucket: str) -> dict[str, str | None] | None:
    """The `{bucket, key, versionId}` of an account-list creation event, else `None`."""
    obj = _created_object(record, bucket)
    raw_key = None if obj is None else obj.get("key")
    if not isinstance(raw_key, str):
        return None
    # S3 event keys are URL-encoded with '+' for spaces.
    key = unquote_plus(raw_key)
    if parse_account_list_key(key) is None:
        return None
    version_id = obj.get("versionId") if obj is not None else None
    return {
        "bucket": bucket,
        "key": key,
        "versionId": version_id if isinstance(version_id, str) and version_id else None,
    }


def _created_object(record: object, bucket: str) -> dict[str, Any] | None:
    """The `s3.object` of an ObjectCreated record for `bucket`, else `None`."""
    if not isinstance(record, dict) or record.get("eventSource") != "aws:s3":
        return None
    s3_info = record.get("s3")
    if not str(record.get("eventName", "")).startswith("ObjectCreated:") or not isinstance(
        s3_info, dict
    ):
        return None
    bucket_info, obj = s3_info.get("bucket"), s3_info.get("object")
    if not isinstance(bucket_info, dict) or bucket_info.get("name") != bucket:
        return None
    return obj if isinstance(obj, dict) else None


@tracer.capture_method(capture_response=False)
def enqueue_account_list(
    target: dict[str, str | None], invocation: str, settings: Settings, aws: Clients
) -> dict[str, Any]:
    """Validate one account list and send its account references. Idempotent per `target`."""
    key = str(target["key"])
    run_id = parse_account_list_key(key)
    if run_id is None:
        raise InvalidAccountListError("object key is not an account-list key")
    try:
        document = read_json(
            aws.s3,
            settings.account_list_bucket,
            key,
            max_bytes=MAX_ACCOUNT_LIST_BYTES,
            version_id=target["versionId"],
        )
    except (S3JsonDecodeError, S3ObjectTooLargeError) as exc:
        raise InvalidAccountListError(type(exc).__name__) from None
    account_list = parse_account_list_document(
        document, run_id=run_id, management_account_id=settings.management_account_id
    )
    sent = send_messages(
        aws.sqs, settings.queue_url, [ref.to_message(run_id) for ref in account_list.accounts]
    )
    return {"runId": run_id, "accountsEnqueued": sent, "invocation": invocation}


@dataclass(slots=True)
class Tally:
    """Per-invocation outcome counts."""

    processed: int = 0
    enqueued: int = 0
    duplicates: int = 0
    ignored: int = 0
    invalid: int = 0
    failed: int = 0

    def record_metrics(self) -> None:
        if self.processed:
            metrics.add_metric(
                name="AccountListsProcessed", unit=MetricUnit.Count, value=self.processed
            )
            metrics.add_metric(name="AccountsEnqueued", unit=MetricUnit.Count, value=self.enqueued)
        for name, value in (
            ("DuplicateAccountListEvents", self.duplicates),
            ("UnrelatedObjectEventsIgnored", self.ignored),
            ("InvalidAccountLists", self.invalid),
            ("AccountListFailures", self.failed),
        ):
            if value:
                metrics.add_metric(name=name, unit=MetricUnit.Count, value=value)

    def summary(self) -> dict[str, int]:
        return {
            "accountListsProcessed": self.processed,
            "accountsEnqueued": self.enqueued,
            "duplicateEvents": self.duplicates,
            "ignoredEvents": self.ignored,
        }


def process_event(event: object, settings: Settings, aws: Clients) -> dict[str, int]:
    records = event.get("Records") if isinstance(event, dict) else None
    if not isinstance(records, list):
        # For example the s3:TestEvent sent when a notification is configured.
        records = [event]
    idempotent_enqueue = idempotent_function(
        enqueue_account_list,
        data_keyword_argument="target",
        persistence_store=persistence_layer(),
        config=IDEMPOTENCY_CONFIG,
    )
    # Distinguishes a result computed now from one replayed from the idempotency store.
    invocation = uuid.uuid4().hex
    tally = Tally()
    for record in records:
        target = account_list_object(record, settings.account_list_bucket)
        if target is None:
            tally.ignored += 1
            continue
        log_fields = {"object_key": target["key"], "version_id": target["versionId"]}
        try:
            result = idempotent_enqueue(
                target=target, invocation=invocation, settings=settings, aws=aws
            )
        except IdempotencyAlreadyInProgressError:
            # Another invocation owns this list; its own retries cover any failure.
            logger.info("Account list is already being processed", extra=log_fields)
            tally.duplicates += 1
            continue
        except InvalidAccountListError as exc:
            logger.error("Account list is malformed", extra={**log_fields, "reason": str(exc)})
            tally.invalid += 1
            continue
        except Exception as exc:
            logger.error(
                "Account list processing failed", extra={**log_fields, **error_fields(exc)}
            )
            tally.failed += 1
            continue
        if result.get("invocation") != invocation:
            logger.info("Duplicate account-list event skipped", extra=log_fields)
            tally.duplicates += 1
            continue
        tally.processed += 1
        tally.enqueued += int(result["accountsEnqueued"])
        logger.info(
            "Account references enqueued",
            extra={**log_fields, "run_id": result["runId"], "accounts": result["accountsEnqueued"]},
        )

    tally.record_metrics()
    if tally.invalid or tally.failed:
        raise AccountListProcessingError(tally.invalid + tally.failed)
    return tally.summary()


@logger.inject_lambda_context(log_event=False, clear_state=True)
@tracer.capture_lambda_handler(capture_response=False)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, int]:
    """Enqueue the accounts of each new account list in the S3 event."""
    try:
        IDEMPOTENCY_CONFIG.register_lambda_context(context)
        return process_event(event, Settings.from_env(), clients())
    except AccountListProcessingError:
        raise
    except Exception as exc:
        logger.error("Account-list event processing failed", extra=error_fields(exc))
        raise AccountReaderError("account-list event could not be processed") from None

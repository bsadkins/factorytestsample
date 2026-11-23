"""Case Retrieval: collect resolved Support cases for one account and queue them for annotation.

Consumes `{accountId, accountName, runId}` messages from the active-accounts queue. For each
account it assumes `Support-Case-Analysis-Role-<uniqueId>`, pages through `DescribeCases` for the
run's trailing calendar-month creation window, keeps resolved cases that have no processed record,
writes their metadata to `account_number=<id>/case_number=<displayId>/data.json` in the raw bucket
and queues one annotation reference per case. The run marker
`runs/<runId>/accounts/<accountId>.json` (see `common.run_markers`) records the cases as
`dispatching` before any reference is sent and as `retrieved` once all are queued; Run Completion
uses it to decide when the run has finished.

Recorded cases never shrink within a run. A retry keeps every case an earlier attempt recorded,
queues it again (annotation is idempotent per case and run) without rewriting its `data.json`,
and writes metadata only for newly admitted cases. A retry that loses access still queues the
earlier cases and keeps them in its `skipped` marker, so Run Completion waits for them.

A missing or untrusted role, `AccessDenied` and `SubscriptionRequiredException` are counted skips,
not failures. Idempotency is keyed by account and run, so a skip only lasts for that run and the
next daily run evaluates the account again. Other errors fail only that SQS record.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any, Final

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from common.aws import client_config
from common.case_refs import AccountMessage, AnnotationRef, InvalidReferenceError, validate_case_id
from common.case_window import (
    CreationWindow,
    creation_window,
    parse_support_time,
    validate_lookback_months,
)
from common.conversation import utc_timestamp
from common.idempotency import idempotency_config
from common.processed import case_record_key, get_existing_cases_batch, validate_display_id
from common.run_markers import (
    AccountMarker,
    dispatching_account,
    read_account_marker,
    retrieved_account,
    skipped_account,
    write_account_marker,
)
from common.s3_json import write_json
from common.sqs import InvalidMessageError, parse_message_body, send_messages
from common.sqs_batch import RecordFailedError, process_sqs_batch, run_idempotent
from common.sts import safe_session_name
from common.support import (
    RESOLVED_STATUS,
    MemberSupport,
    SupportAccessSkippedError,
    case_metadata,
    iter_cases,
    support_api_region,
    support_client,
)
from common.tracing import withhold_exception_messages

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.batch.types import PartialItemFailureResponse
    from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord
    from aws_lambda_powertools.utilities.typing import LambdaContext
    from types_boto3_s3.client import S3Client
    from types_boto3_sqs.client import SQSClient
    from types_boto3_sts.client import STSClient
    from types_boto3_support.type_defs import CaseDetailsTypeDef

logger = Logger()
tracer = Tracer()
metrics = Metrics()
withhold_exception_messages()

RAW_SCHEMA_VERSION: Final = 1
# Longer than the 4-day source-queue retention; the key includes the run ID, so expiry never lets
# one run's result affect another run.
IDEMPOTENCY_EXPIRY_SECONDS: Final = 7 * 24 * 60 * 60
IDEMPOTENCY_CONFIG: Final = idempotency_config(expires_after_seconds=IDEMPOTENCY_EXPIRY_SECONDS)

_COUNT_METRICS: Final = (
    ("casesListed", "CasesListed"),
    ("excludedUnresolved", "CasesExcludedUnresolved"),
    ("excludedOutsideWindow", "CasesExcludedOutsideWindow"),
    ("invalidCases", "CasesInvalid"),
    ("alreadyProcessed", "CasesAlreadyProcessed"),
    ("casesRetrieved", "CasesRetrieved"),
)


@dataclass(frozen=True, slots=True)
class Settings:
    raw_bucket: str
    processed_bucket: str
    annotation_queue_url: str
    member_role_name: str
    partition: str
    lookback_months: int

    @classmethod
    def from_env(cls) -> Settings:
        partition = os.environ.get("MEMBER_ROLE_PARTITION", "aws")
        support_api_region(partition)
        return cls(
            raw_bucket=os.environ["RAW_BUCKET"],
            processed_bucket=os.environ["PROCESSED_BUCKET"],
            annotation_queue_url=os.environ["ANNOTATION_QUEUE_URL"],
            member_role_name=os.environ["MEMBER_ROLE_NAME"],
            partition=partition,
            lookback_months=validate_lookback_months(int(os.environ["LOOKBACK_MONTHS"])),
        )


@dataclass(frozen=True, slots=True)
class Clients:
    s3: S3Client
    sqs: SQSClient
    sts: STSClient


@cache
def clients() -> Clients:
    return Clients(
        s3=boto3.client("s3", config=client_config()),
        sqs=boto3.client("sqs", config=client_config()),
        sts=boto3.client("sts", config=client_config(max_attempts=1)),
    )


def now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class Selection:
    """Cases admitted for annotation plus exclusion counts."""

    admitted: list[CaseDetailsTypeDef] = field(default_factory=list)
    listed: int = 0
    unresolved: int = 0
    outside_window: int = 0
    invalid: int = 0


def select_cases(cases: Any, window: CreationWindow) -> Selection:  # noqa: ANN401
    """Keep resolved, well-formed cases created inside `window`; first occurrence wins."""
    selection = Selection()
    seen: set[str] = set()
    for case in cases:
        selection.listed += 1
        if case.get("status") != RESOLVED_STATUS:
            selection.unresolved += 1
            continue
        created = parse_support_time(case.get("timeCreated"))
        try:
            validate_display_id(str(case.get("displayId", "")))
            validate_case_id(str(case.get("caseId", "")))
        except ValueError:
            selection.invalid += 1
            continue
        if created is None:
            selection.invalid += 1
            continue
        if not window.contains(created):
            selection.outside_window += 1
            continue
        if case["displayId"] in seen:
            continue
        seen.add(case["displayId"])
        selection.admitted.append(case)
    return selection


@tracer.capture_method(capture_response=False)
def retrieve_account(
    key: dict[str, str], invocation: str, settings: Settings, aws: Clients
) -> dict[str, Any]:
    """Collect one account's eligible cases for one run. Idempotent per `{accountId, runId}`.

    Errors are re-raised as `RecordFailedError` without their message or cause, so neither the
    tracer's exception log nor the batch report can carry AWS error text.
    """
    try:
        return _retrieve_account(key, invocation, settings, aws)
    except Exception as exc:
        raise RecordFailedError.from_exception(exc) from None


def _retrieve_account(
    key: dict[str, str], invocation: str, settings: Settings, aws: Clients
) -> dict[str, Any]:
    account_id, run_id = key["accountId"], key["runId"]
    result: dict[str, Any] = {"invocation": invocation, "accountId": account_id, "runId": run_id}
    window = creation_window(run_id, settings.lookback_months)
    member = MemberSupport(
        role_name=settings.member_role_name,
        partition=settings.partition,
        sts_client=aws.sts,
        client_factory=support_client,
    )
    # Cases an earlier attempt of this run recorded before sending may already be with
    # annotation; they stay recorded, and are queued again, whatever this attempt finds.
    earlier = read_account_marker(aws.s3, settings.raw_bucket, run_id, account_id)
    dispatched = earlier.dispatched if earlier is not None else {}
    try:
        support = member.open(account_id, safe_session_name("case-retrieval", run_id))
        selection = select_cases(iter_cases(support, account_id, window), window)
    except SupportAccessSkippedError as exc:
        dispatch(aws, settings, run_id, account_id, dispatched)
        record_account(
            aws.s3, settings.raw_bucket, skipped_account(run_id, account_id, exc.reason, dispatched)
        )
        return {**result, "skipped": exc.reason, "casesRedispatched": len(dispatched)}

    candidates = [case["displayId"] for case in selection.admitted]
    processed = get_existing_cases_batch(aws.s3, settings.processed_bucket, account_id, candidates)
    pending = [case for case in selection.admitted if case["displayId"] not in processed]
    new = [case for case in pending if case["displayId"] not in dispatched]
    retrieved_at = utc_timestamp(now())
    for case in new:
        document = {
            "schemaVersion": RAW_SCHEMA_VERSION,
            "accountId": account_id,
            "runId": run_id,
            **case_metadata(case),
            "Case_Retrieval_Date": retrieved_at,
        }
        write_json(
            aws.s3, settings.raw_bucket, case_record_key(account_id, case["displayId"]), document
        )
    queued = {**dispatched, **{case["displayId"]: case["caseId"] for case in new}}
    if queued:
        # Recorded before sending, so a partial send or a later access loss can never leave a
        # handed-off case out of the run's evidence.
        record_account(aws.s3, settings.raw_bucket, dispatching_account(run_id, account_id, queued))
    dispatch(aws, settings, run_id, account_id, queued)
    # The account counts as settled for run completion only once every case reference is queued.
    record_account(aws.s3, settings.raw_bucket, retrieved_account(run_id, account_id, queued))
    return {
        **result,
        "skipped": None,
        "casesListed": selection.listed,
        "excludedUnresolved": selection.unresolved,
        "excludedOutsideWindow": selection.outside_window,
        "invalidCases": selection.invalid,
        "alreadyProcessed": len(selection.admitted) - len(pending),
        "casesRetrieved": len(new),
        "casesRedispatched": len(dispatched),
        "window": window.request_parameters(),
    }


def dispatch(
    aws: Clients, settings: Settings, run_id: str, account_id: str, cases: Mapping[str, str]
) -> None:
    """Queue one annotation reference per case; annotation is idempotent per case and run."""
    refs = [
        AnnotationRef(account_id=account_id, case_id=case_id, display_id=display_id, run_id=run_id)
        for display_id, case_id in cases.items()
    ]
    send_messages(aws.sqs, settings.annotation_queue_url, [ref.to_message() for ref in refs])


def record_account(s3: S3Client, bucket: str, marker: AccountMarker) -> None:
    write_account_marker(s3, bucket, marker)


def record_outcome(result: dict[str, Any], log_fields: dict[str, Any]) -> None:
    skipped = result.get("skipped")
    redispatched = int(result.get("casesRedispatched") or 0)
    if redispatched:
        metrics.add_metric(name="CasesRedispatched", unit=MetricUnit.Count, value=redispatched)
    if skipped:
        metrics.add_metric(name="AccountsSkipped", unit=MetricUnit.Count, value=1)
        metrics.add_metric(name=f"AccountsSkipped{skipped}", unit=MetricUnit.Count, value=1)
        logger.warning("Account skipped for this run", extra={**log_fields, "reason": skipped})
        return
    metrics.add_metric(name="AccountsProcessed", unit=MetricUnit.Count, value=1)
    for result_key, metric in _COUNT_METRICS:
        metrics.add_metric(name=metric, unit=MetricUnit.Count, value=int(result[result_key]))
    logger.info(
        "Account cases retrieved",
        extra={**log_fields, **{k: result[k] for k, _ in _COUNT_METRICS}, **result["window"]},
    )


def handle_record(record: SQSRecord, settings: Settings, aws: Clients) -> None:
    log_fields: dict[str, Any] = {"message_id": record.message_id}
    try:
        message = AccountMessage.parse(parse_message_body(record.raw_event))
    except (InvalidMessageError, InvalidReferenceError) as exc:
        metrics.add_metric(name="InvalidMessages", unit=MetricUnit.Count, value=1)
        metrics.add_metric(name="CaseRetrievalFailures", unit=MetricUnit.Count, value=1)
        logger.error(
            "Active-account message is malformed", extra={**log_fields, "reason": str(exc)}
        )
        raise RecordFailedError(type(exc).__name__) from None
    log_fields |= {"account_id": message.account_id, "run_id": message.run_id}
    try:
        outcome = run_idempotent(
            retrieve_account,
            message.idempotency_key(),
            config=IDEMPOTENCY_CONFIG,
            settings=settings,
            aws=aws,
        )
    except Exception as exc:
        failure = RecordFailedError.from_exception(exc)
        metrics.add_metric(name="CaseRetrievalFailures", unit=MetricUnit.Count, value=1)
        logger.error("Case retrieval failed", extra={**log_fields, **failure.log_fields()})
        raise failure from None
    if outcome.duplicate or outcome.result is None:
        metrics.add_metric(name="DuplicateAccountMessages", unit=MetricUnit.Count, value=1)
        logger.info(
            "Duplicate account message skipped",
            extra={**log_fields, "in_progress": outcome.in_progress},
        )
        return
    record_outcome(outcome.result, log_fields)


@logger.inject_lambda_context(log_event=False, clear_state=True)
@tracer.capture_lambda_handler(capture_response=False)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> PartialItemFailureResponse:
    """Retrieve eligible cases for each account message; report failed records only."""
    IDEMPOTENCY_CONFIG.register_lambda_context(context)
    settings = Settings.from_env()
    aws = clients()
    return process_sqs_batch(event, context, lambda record: handle_record(record, settings, aws))

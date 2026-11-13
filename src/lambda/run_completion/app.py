"""Run Completion: emit the daily heartbeat only after a run's work has really finished.

Runs hourly from EventBridge Scheduler. Every account list written by account discovery within
the lookback window is one run; finishing the lookup does not complete it. A run is complete
when, using the run markers in `common.run_markers`:

- every account in the run's account list has an account marker (retrieved or skipped);
- every case a retrieved account queued has a case marker; and
- every case reached a terminal outcome: skipped, already processed, a terminal oversized
  conversation, a SUCCEEDED CaseAnalysis execution, or a closed FAILED, TIMED_OUT or ABORTED
  execution whose failure record is confirmed on the workflow failure queue. Run Completion
  preserves and delivers that record itself when it is missing (see `common.failure_delivery`).

A run with no accounts, or with only skipped accounts or zero cases, completes immediately.
Pending work, unreadable markers and status lookups or failure deliveries that fail keep the run
incomplete; it is checked again on the next invocation. The first invocation to see a complete
run writes `runs/<runId>/completion.json` with IfNoneMatch and emits `DailyRunCompleted`, which
the missing-heartbeat alarm watches. Metrics carry counts only, never account or case IDs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import TYPE_CHECKING, Any, Final

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from common.account_list import (
    ACCOUNT_LIST_PREFIX,
    MAX_ACCOUNT_LIST_BYTES,
    AccountList,
    account_list_key,
    parse_account_list_document,
    parse_account_list_key,
    scheduled_run_id,
)
from common.aws import client_config
from common.conversation import utc_timestamp
from common.failure_delivery import DeliveryTally, FailureQueue, reconcile_closed_attempt
from common.processed import validate_account_id
from common.run_markers import (
    ACCOUNT_SKIPPED,
    CASE_ALREADY_PROCESSED,
    FAILED_OUTCOMES,
    MAX_ACCOUNT_MARKER_BYTES,
    MAX_CASE_MARKER_BYTES,
    SKIPPED_OUTCOMES,
    AccountMarker,
    CaseMarker,
    account_marker_key,
    case_marker_key,
    completion_key,
    run_prefix,
    write_completion,
)
from common.s3_json import read_json_object
from common.safe_logging import error_fields
from common.workflow import (
    ACTIVE_STATUSES,
    FAILED_STATUSES,
    SUCCEEDED_STATUS,
    describe_execution,
    display_component,
    execution_arn,
    parse_execution_name,
)

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext
    from types_boto3_s3.client import S3Client
    from types_boto3_sqs.client import SQSClient
    from types_boto3_stepfunctions.client import SFNClient

logger = Logger()
tracer = Tracer()
metrics = Metrics()

TIME_MARGIN_SECONDS: Final = 30
MIN_LOOKBACK_HOURS: Final = 24
LIST_PAGE_SIZE = 1000


class RunCompletionError(Exception):
    """The run completion check failed before any run was evaluated. Carries no details."""


@dataclass(frozen=True, slots=True)
class Settings:
    account_list_bucket: str
    raw_bucket: str
    state_machine_arn: str
    failure_queue_url: str
    management_account_id: str
    lookback: timedelta

    @classmethod
    def from_env(cls) -> Settings:
        hours = int(os.environ["RUN_LOOKBACK_HOURS"])
        if hours < MIN_LOOKBACK_HOURS:
            raise ValueError("RUN_LOOKBACK_HOURS must be at least 24")
        management = os.environ["MANAGEMENT_ACCOUNT_ID"]
        validate_account_id(management)
        return cls(
            account_list_bucket=os.environ["ACCOUNT_LIST_BUCKET"],
            raw_bucket=os.environ["RAW_BUCKET"],
            state_machine_arn=os.environ["STATE_MACHINE_ARN"],
            failure_queue_url=os.environ["FAILURE_QUEUE_URL"],
            management_account_id=management,
            lookback=timedelta(hours=hours),
        )


@dataclass(frozen=True, slots=True)
class Clients:
    s3: S3Client
    sfn: SFNClient
    sqs: SQSClient


@cache
def clients() -> Clients:
    return Clients(
        s3=boto3.client("s3", config=client_config()),
        sfn=boto3.client("stepfunctions", config=client_config()),
        sqs=boto3.client("sqs", config=client_config()),
    )


def now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class RunTally:
    """Settled and outstanding work of one run."""

    accounts: int = 0
    accounts_retrieved: int = 0
    accounts_skipped: int = 0
    cases: int = 0
    cases_processed: int = 0
    cases_skipped: int = 0
    cases_failed: int = 0
    pending_accounts: int = 0
    pending_cases: int = 0
    errors: int = 0
    timed_out: bool = False

    @property
    def complete(self) -> bool:
        return not (self.pending_accounts or self.pending_cases or self.errors or self.timed_out)

    def counts(self) -> dict[str, int]:
        return {
            "accounts": self.accounts,
            "accountsRetrieved": self.accounts_retrieved,
            "accountsSkipped": self.accounts_skipped,
            "cases": self.cases,
            "casesProcessed": self.cases_processed,
            "casesSkipped": self.cases_skipped,
            "casesFailed": self.cases_failed,
        }


@dataclass(slots=True)
class Summary:
    runs_checked: int = 0
    runs_completed: int = 0
    runs_incomplete: int = 0
    pending_cases: int = 0
    errors: int = 0
    completed: list[tuple[RunTally, float]] = field(default_factory=list)
    deliveries: DeliveryTally = field(default_factory=DeliveryTally)


def candidate_runs(s3: S3Client, bucket: str, oldest: datetime) -> list[str]:
    """Run IDs of account lists whose run time is not older than `oldest`, oldest first."""
    paginator = s3.get_paginator("list_objects_v2")
    runs: list[str] = []
    for page in paginator.paginate(
        Bucket=bucket,
        Prefix=ACCOUNT_LIST_PREFIX,
        StartAfter=f"{ACCOUNT_LIST_PREFIX}{scheduled_run_id(oldest)}",
        PaginationConfig={"PageSize": LIST_PAGE_SIZE},
    ):
        for item in page.get("Contents", []):
            run_id = parse_account_list_key(item["Key"])
            if run_id is not None:
                runs.append(run_id)
    return sorted(runs)


def run_keys(s3: S3Client, bucket: str, run_id: str) -> set[str]:
    """Every run-marker key written for `run_id`."""
    paginator = s3.get_paginator("list_objects_v2")
    keys: set[str] = set()
    for page in paginator.paginate(
        Bucket=bucket, Prefix=run_prefix(run_id), PaginationConfig={"PageSize": LIST_PAGE_SIZE}
    ):
        keys.update(item["Key"] for item in page.get("Contents", []))
    return keys


class RunChecker:
    """Decides whether runs are complete; see the module docstring for the rules."""

    def __init__(
        self, settings: Settings, aws: Clients, context: LambdaContext, deliveries: DeliveryTally
    ) -> None:
        self.settings = settings
        self.aws = aws
        self.context = context
        self.deliveries = deliveries
        self.queue = FailureQueue(aws.sqs, settings.failure_queue_url)

    def out_of_time(self) -> bool:
        return self.context.get_remaining_time_in_millis() < TIME_MARGIN_SECONDS * 1000

    def account_list(self, run_id: str) -> AccountList:
        document = read_json_object(
            self.aws.s3,
            self.settings.account_list_bucket,
            account_list_key(run_id),
            max_bytes=MAX_ACCOUNT_LIST_BYTES,
        )
        return parse_account_list_document(
            document, run_id=run_id, management_account_id=self.settings.management_account_id
        )

    def check(self, account_list: AccountList, keys: set[str]) -> RunTally:
        tally = RunTally(accounts=len(account_list.accounts))
        run_id = account_list.run_id
        for account in account_list.accounts:
            if self.out_of_time():
                tally.timed_out = True
                return tally
            key = account_marker_key(run_id, account.account_id)
            if key not in keys:
                tally.pending_accounts += 1
                continue
            fields = {"run_id": run_id, "account_id": account.account_id}
            try:
                marker = AccountMarker.from_document(
                    read_json_object(
                        self.aws.s3,
                        self.settings.raw_bucket,
                        key,
                        max_bytes=MAX_ACCOUNT_MARKER_BYTES,
                    )
                )
                if (marker.run_id, marker.account_id) != (run_id, account.account_id):
                    raise ValueError("account marker is for another run or account")
            except Exception as exc:
                tally.errors += 1
                logger.warning(
                    "Account run marker is unreadable", extra={**fields, **error_fields(exc)}
                )
                continue
            if marker.outcome == ACCOUNT_SKIPPED:
                tally.accounts_skipped += 1
                continue
            tally.accounts_retrieved += 1
            for display_id in marker.cases:
                tally.cases += 1
                self.check_case(run_id, account.account_id, display_id, keys, tally)
        return tally

    def check_case(
        self, run_id: str, account_id: str, display_id: str, keys: set[str], tally: RunTally
    ) -> None:
        key = case_marker_key(run_id, account_id, display_id)
        if key not in keys:
            tally.pending_cases += 1
            return
        fields = {"run_id": run_id, "account_id": account_id, "display_id": display_id}
        try:
            marker = CaseMarker.from_document(
                read_json_object(
                    self.aws.s3, self.settings.raw_bucket, key, max_bytes=MAX_CASE_MARKER_BYTES
                )
            )
            if (marker.run_id, marker.account_id, marker.display_id) != (
                run_id,
                account_id,
                display_id,
            ):
                raise ValueError("case marker is for another case")
            state = self.case_state(marker)
        except Exception as exc:
            tally.errors += 1
            logger.warning(
                "Case outcome could not be reconciled; run stays incomplete",
                extra={**fields, **error_fields(exc)},
            )
            return
        if state == "pending":
            tally.pending_cases += 1
        elif state == "processed":
            tally.cases_processed += 1
        elif state == "skipped":
            tally.cases_skipped += 1
        else:
            tally.cases_failed += 1

    def case_state(self, marker: CaseMarker) -> str:
        """`processed`, `skipped`, `failed` or `pending`; raises when the state is unknown."""
        if marker.outcome == CASE_ALREADY_PROCESSED:
            return "processed"
        if marker.outcome in SKIPPED_OUTCOMES:
            return "skipped"
        if marker.outcome in FAILED_OUTCOMES:
            return "failed"
        name = str(marker.execution_name)
        identity = parse_execution_name(name)
        if identity is None or (identity.account_id, identity.case_component) != (
            marker.account_id,
            display_component(marker.display_id),
        ):
            raise ValueError("case marker names an execution of another case")
        described = describe_execution(
            self.aws.sfn, execution_arn(self.settings.state_machine_arn, name)
        )
        if described is None:
            raise LookupError("execution named by the case marker does not exist")
        if described.status in ACTIVE_STATUSES:
            return "pending"
        if described.status == SUCCEEDED_STATUS:
            return "processed"
        if described.status not in FAILED_STATUSES:
            raise ValueError("execution status is not recognised")
        if described.case is None or described.attempt is None:
            raise ValueError("execution input does not identify the case attempt")
        try:
            outcome, created = reconcile_closed_attempt(
                self.aws.s3,
                self.settings.raw_bucket,
                self.queue,
                described.case,
                attempt=described.attempt,
                execution_status=described.status,
                now=now(),
            )
        except Exception:
            self.deliveries.errors += 1
            raise
        self.deliveries.add(outcome, created=created)
        return "failed"


def _run_duration_seconds(account_list: AccountList, completed_at: datetime) -> float:
    generated = datetime.fromisoformat(account_list.generated_at)
    return max(0.0, (completed_at - generated).total_seconds())


@tracer.capture_method(capture_response=False)
def run_completion(settings: Settings, aws: Clients, context: LambdaContext) -> Summary:
    started = now()
    summary = Summary()
    checker = RunChecker(settings, aws, context, summary.deliveries)
    try:
        runs = candidate_runs(aws.s3, settings.account_list_bucket, started - settings.lookback)
    except Exception as exc:
        raise RunCompletionError("account lists could not be listed") from exc
    for run_id in runs:
        if checker.out_of_time():
            summary.runs_incomplete += 1
            continue
        fields = {"run_id": run_id}
        try:
            keys = run_keys(aws.s3, settings.raw_bucket, run_id)
            if completion_key(run_id) in keys:
                continue
            account_list = checker.account_list(run_id)
        except Exception as exc:
            summary.runs_checked += 1
            summary.runs_incomplete += 1
            summary.errors += 1
            logger.warning("Run could not be checked", extra={**fields, **error_fields(exc)})
            continue
        summary.runs_checked += 1
        tally = checker.check(account_list, keys)
        summary.errors += tally.errors
        if not tally.complete:
            summary.runs_incomplete += 1
            summary.pending_cases += tally.pending_cases
            logger.info(
                "Run is not complete yet",
                extra={
                    **fields,
                    **tally.counts(),
                    "pendingAccounts": tally.pending_accounts,
                    "pendingCases": tally.pending_cases,
                    "errors": tally.errors,
                    "timedOut": tally.timed_out,
                },
            )
            continue
        completed_at = now()
        if not write_completion(
            aws.s3, settings.raw_bucket, run_id, utc_timestamp(completed_at), tally.counts()
        ):
            continue
        summary.runs_completed += 1
        summary.completed.append((tally, _run_duration_seconds(account_list, completed_at)))
        logger.info("Run complete", extra={**fields, **tally.counts()})
    return summary


def publish(summary: Summary) -> None:
    metrics.add_metric(
        name="DailyRunCompleted", unit=MetricUnit.Count, value=summary.runs_completed
    )
    metrics.add_metric(name="RunsIncomplete", unit=MetricUnit.Count, value=summary.runs_incomplete)
    metrics.add_metric(name="RunCasesPending", unit=MetricUnit.Count, value=summary.pending_cases)
    metrics.add_metric(name="RunCompletionErrors", unit=MetricUnit.Count, value=summary.errors)
    for tally, duration in summary.completed:
        counts = tally.counts()
        for metric, value in (
            ("RunAccountsRetrieved", counts["accountsRetrieved"]),
            ("RunAccountsSkipped", counts["accountsSkipped"]),
            ("RunCasesProcessed", counts["casesProcessed"]),
            ("RunCasesSkipped", counts["casesSkipped"]),
            ("RunCasesFailed", counts["casesFailed"]),
        ):
            metrics.add_metric(name=metric, unit=MetricUnit.Count, value=value)
        metrics.add_metric(name="RunDuration", unit=MetricUnit.Seconds, value=duration)
    for name, value in summary.deliveries.metrics().items():
        metrics.add_metric(name=name, unit=MetricUnit.Count, value=value)


@logger.inject_lambda_context(log_event=False, clear_state=True)
@tracer.capture_lambda_handler(capture_response=False)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, int]:
    """Scheduled completion check; returns counts only."""
    del event
    try:
        summary = run_completion(Settings.from_env(), clients(), context)
    except Exception as exc:
        metrics.add_metric(name="RunCompletionErrors", unit=MetricUnit.Count, value=1)
        logger.error("Run completion check failed", extra=error_fields(exc))
        raise RunCompletionError("run completion check failed") from None
    publish(summary)
    logger.info(
        "Run completion check finished",
        extra={
            "runsChecked": summary.runs_checked,
            "runsCompleted": summary.runs_completed,
            "runsIncomplete": summary.runs_incomplete,
            "errors": summary.errors,
        },
    )
    return {
        "runsChecked": summary.runs_checked,
        "runsCompleted": summary.runs_completed,
        "runsIncomplete": summary.runs_incomplete,
        "errors": summary.errors,
    }

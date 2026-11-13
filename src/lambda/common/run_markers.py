"""Minimal S3 run markers used to reconcile daily-run completion.

Account discovery starts a run by writing `account-lists/<runId>.json`; finishing the lookup is
not completion. These raw-bucket markers let Run Completion decide when every account and case of
a run has reached a terminal outcome:

- `runs/<runId>/accounts/<accountId>.json`: written by Case Retrieval once an account is settled
  for the run: `retrieved` with the display IDs it queued for annotation, or `skipped`;
- `runs/<runId>/cases/<accountId>/<displayId>.json`: the case's hand-off in that run. Annotation
  writes outcomes that end the case's work for the run (skips, not found, already processed,
  conversations too large to summarise); the workflow starter writes the execution that owns the
  case (`started` or `alreadyActive`) or `alreadyProcessed`;
- `runs/<runId>/completion.json`: written once, with IfNoneMatch, when the run is complete.

They are evidence for completion reconciliation only. SQS and Step Functions still coordinate the
work; nothing reads these markers to decide whether to start or skip a case. They contain
identifiers and counts, never case content, and expire with the raw bucket's retention.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from common.account_list import validate_run_id
from common.processed import validate_account_id, validate_display_id
from common.s3_json import S3PreconditionFailedError, WriteResult, write_json

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

RUNS_PREFIX: Final = "runs/"
RUN_MARKER_SCHEMA_VERSION: Final = 1
MAX_ACCOUNT_MARKER_BYTES: Final = 16 * 1024 * 1024
MAX_CASE_MARKER_BYTES: Final = 4 * 1024
ACCOUNT_RETRIEVED: Final = "retrieved"
ACCOUNT_SKIPPED: Final = "skipped"
CASE_STARTED: Final = "started"
CASE_ALREADY_ACTIVE: Final = "alreadyActive"
CASE_ALREADY_PROCESSED: Final = "alreadyProcessed"
#: Case outcomes that hand the case to a CaseAnalysis execution named in the marker.
HANDOFF_OUTCOMES: Final = frozenset({CASE_STARTED, CASE_ALREADY_ACTIVE})
#: Annotation outcomes that end the case's work for the run, grouped as Run Completion counts them.
SKIPPED_OUTCOMES: Final = frozenset(
    {"notFound", "skippedAccessDenied", "skippedSubscriptionRequired"}
)
FAILED_OUTCOMES: Final = frozenset({"oversized", "oversizedUnchanged"})
CASE_OUTCOMES: Final = (
    HANDOFF_OUTCOMES | SKIPPED_OUTCOMES | FAILED_OUTCOMES | frozenset({CASE_ALREADY_PROCESSED})
)
_EXECUTION_NAME: Final = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_SKIP_REASON: Final = re.compile(r"^[A-Za-z]{1,64}$")


class InvalidRunMarkerError(ValueError):
    """A run marker does not match its contract. Never echoes its content."""


def run_prefix(run_id: str) -> str:
    return f"{RUNS_PREFIX}{validate_run_id(run_id)}/"


def account_marker_key(run_id: str, account_id: str) -> str:
    """Return `runs/<runId>/accounts/<accountId>.json`."""
    return f"{run_prefix(run_id)}accounts/{validate_account_id(account_id)}.json"


def case_markers_prefix(run_id: str, account_id: str) -> str:
    """Return `runs/<runId>/cases/<accountId>/`."""
    return f"{run_prefix(run_id)}cases/{validate_account_id(account_id)}/"


def case_marker_key(run_id: str, account_id: str, display_id: str) -> str:
    """Return `runs/<runId>/cases/<accountId>/<displayId>.json`."""
    return f"{case_markers_prefix(run_id, account_id)}{validate_display_id(display_id)}.json"


def completion_key(run_id: str) -> str:
    """Return `runs/<runId>/completion.json`."""
    return f"{run_prefix(run_id)}completion.json"


def _require(document: Mapping[str, Any], keys: frozenset[str]) -> None:
    if set(document) != keys or document.get("schemaVersion") != RUN_MARKER_SCHEMA_VERSION:
        raise InvalidRunMarkerError("run marker fields do not match the contract")


@dataclass(frozen=True, slots=True)
class AccountMarker:
    """How one account was settled in one run."""

    run_id: str
    account_id: str
    outcome: str
    cases: tuple[str, ...] = ()
    skip_reason: str | None = None

    _KEYS = frozenset({"schemaVersion", "runId", "accountId", "outcome", "cases", "skipReason"})

    @property
    def key(self) -> str:
        return account_marker_key(self.run_id, self.account_id)

    def to_document(self) -> dict[str, Any]:
        return {
            "schemaVersion": RUN_MARKER_SCHEMA_VERSION,
            "runId": self.run_id,
            "accountId": self.account_id,
            "outcome": self.outcome,
            "cases": list(self.cases),
            "skipReason": self.skip_reason,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> AccountMarker:
        _require(document, cls._KEYS)
        outcome = document["outcome"]
        cases = document["cases"]
        reason = document["skipReason"]
        if not isinstance(cases, list):
            raise InvalidRunMarkerError("account marker cases are malformed")
        try:
            marker = cls(
                run_id=validate_run_id(document["runId"]),
                account_id=validate_account_id(document["accountId"]),
                outcome=outcome,
                cases=tuple(validate_display_id(case) for case in cases),
                skip_reason=reason,
            )
        except (ValueError, TypeError):
            raise InvalidRunMarkerError("account marker values are malformed") from None
        retrieved = outcome == ACCOUNT_RETRIEVED and reason is None
        skipped = (
            outcome == ACCOUNT_SKIPPED
            and not cases
            and isinstance(reason, str)
            and _SKIP_REASON.match(reason) is not None
        )
        if not (retrieved or skipped):
            raise InvalidRunMarkerError("account marker outcome is malformed")
        return marker


@dataclass(frozen=True, slots=True)
class CaseMarker:
    """A case's terminal outcome or hand-off in one run."""

    run_id: str
    account_id: str
    display_id: str
    outcome: str
    execution_name: str | None = None

    _KEYS = frozenset(
        {"schemaVersion", "runId", "accountId", "displayId", "outcome", "executionName"}
    )

    @property
    def key(self) -> str:
        return case_marker_key(self.run_id, self.account_id, self.display_id)

    def to_document(self) -> dict[str, Any]:
        return {
            "schemaVersion": RUN_MARKER_SCHEMA_VERSION,
            "runId": self.run_id,
            "accountId": self.account_id,
            "displayId": self.display_id,
            "outcome": self.outcome,
            "executionName": self.execution_name,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> CaseMarker:
        _require(document, cls._KEYS)
        outcome = document["outcome"]
        name = document["executionName"]
        try:
            marker = cls(
                run_id=validate_run_id(document["runId"]),
                account_id=validate_account_id(document["accountId"]),
                display_id=validate_display_id(document["displayId"]),
                outcome=outcome,
                execution_name=name,
            )
        except (ValueError, TypeError):
            raise InvalidRunMarkerError("case marker values are malformed") from None
        handoff = outcome in HANDOFF_OUTCOMES
        if outcome not in CASE_OUTCOMES or handoff != (
            isinstance(name, str) and _EXECUTION_NAME.match(name) is not None
        ):
            raise InvalidRunMarkerError("case marker outcome is malformed")
        return marker


def write_account_marker(s3: S3Client, bucket: str, marker: AccountMarker) -> WriteResult:
    return write_json(
        s3, bucket, marker.key, marker.to_document(), max_bytes=MAX_ACCOUNT_MARKER_BYTES
    )


def write_case_marker(s3: S3Client, bucket: str, marker: CaseMarker) -> WriteResult:
    return write_json(s3, bucket, marker.key, marker.to_document(), max_bytes=MAX_CASE_MARKER_BYTES)


def retrieved_account(run_id: str, account_id: str, cases: Sequence[str]) -> AccountMarker:
    return AccountMarker(run_id, account_id, ACCOUNT_RETRIEVED, tuple(cases))


def skipped_account(run_id: str, account_id: str, reason: str) -> AccountMarker:
    return AccountMarker(run_id, account_id, ACCOUNT_SKIPPED, (), reason)


def write_completion(
    s3: S3Client, bucket: str, run_id: str, completed_at: str, counts: Mapping[str, int]
) -> bool:
    """Record that `run_id` is complete; False when another invocation already recorded it."""
    document = {
        "schemaVersion": RUN_MARKER_SCHEMA_VERSION,
        "runId": validate_run_id(run_id),
        "completedAt": completed_at,
        "counts": dict(counts),
    }
    try:
        write_json(
            s3,
            bucket,
            completion_key(run_id),
            document,
            if_none_match=True,
            max_bytes=MAX_CASE_MARKER_BYTES,
        )
    except S3PreconditionFailedError:
        return False
    return True

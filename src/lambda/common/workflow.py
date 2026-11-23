"""CaseAnalysis execution identity and the S3 ownership marker.

One Standard execution analyses one case attempt. Its name is derived from the account, case,
run and attempt number, so a duplicate delivery of an attempt maps to the same execution while a
recovery after a closed failed execution takes the next attempt number. Step Functions reserves
closed execution names for 90 days, so `ExecutionAlreadyExists` never means success: the caller
must describe the execution and decide.

The ownership marker `account_number=<id>/case_number=<displayId>/workflow.json` in the raw bucket
names the execution that currently owns the case. Starters write it with S3 conditional writes
before calling StartExecution, and it stores the exact execution input, so any starter can finish
a claim whose owner stopped between the write and StartExecution without creating a second
execution (StartExecution with the same name and input is idempotent while it runs).

While Case Cleanup resets an abandoned case, or `scripts/reprocess-cases.py` removes a case's
checkpoints, `workflow.json` holds a `ResetClaim` instead; see there for how starters and the
resetters share the key.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from botocore.exceptions import ClientError

from common.account_list import validate_run_id
from common.case_refs import InvalidReferenceError, SummaryRef
from common.processed import case_record_key, validate_account_id, validate_display_id
from common.s3_json import S3JsonDecodeError, S3ObjectTooLargeError, read_json_with_etag
from common.safe_logging import aws_error_code

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client
    from types_boto3_stepfunctions.client import SFNClient

MARKER_FILE: Final = "workflow.json"
MARKER_SCHEMA_VERSION: Final = 1
MAX_MARKER_BYTES: Final = 64 * 1024
MAX_EXECUTION_NAME_LENGTH: Final = 80
# Bound on attempts per case and run; each needs a deliberate redrive once the first has failed.
MAX_ATTEMPTS_PER_RUN: Final = 50
ACTIVE_STATUSES: Final = frozenset({"RUNNING", "PENDING_REDRIVE"})
FAILED_STATUSES: Final = frozenset({"FAILED", "TIMED_OUT", "ABORTED"})
SUCCEEDED_STATUS: Final = "SUCCEEDED"
RESET_CLAIM_KIND: Final = "cleanupReset"
# Far longer than any cleanup invocation can run (Lambda stops it within 15 minutes).
RESET_CLAIM_STALE_AFTER: Final = timedelta(hours=1)
_HASHED_PREFIX: Final = "h"
_EXECUTION_NAME: Final = re.compile(
    r"^(\d{12})-(.+)-(\d{8}T\d{6}Z(?:-[0-9a-f]{8})?)-a([1-9][0-9]{0,2})$"
)
_MARKER_KEYS: Final = frozenset(
    {
        "schemaVersion",
        "accountId",
        "caseId",
        "displayId",
        "runId",
        "attempt",
        "executionName",
        "claimedAt",
        "input",
    }
)


class InvalidOwnershipMarkerError(ValueError):
    """The ownership marker does not match its contract. Never echoes its content."""


def marker_key(account_id: str, display_id: str) -> str:
    """Return `account_number=<id>/case_number=<displayId>/workflow.json`."""
    prefix = case_record_key(account_id, display_id).rsplit("/", 1)[0]
    return f"{prefix}/{MARKER_FILE}"


def display_component(display_id: str) -> str:
    """The case part of execution names: the display ID, or a digest when it is too long."""
    if len(display_id) <= 20:  # noqa: PLR2004
        return display_id
    return _HASHED_PREFIX + hashlib.sha256(display_id.encode("utf-8")).hexdigest()[:20]


def execution_name(account_id: str, display_id: str, run_id: str, attempt: int) -> str:
    """Stable execution name `<account>-<case>-<runId>-a<attempt>` (at most 80 characters)."""
    if not 1 <= attempt <= MAX_ATTEMPTS_PER_RUN:
        raise ValueError("attempt out of range")
    name = f"{account_id}-{display_component(display_id)}-{validate_run_id(run_id)}-a{attempt}"
    if len(name) > MAX_EXECUTION_NAME_LENGTH:  # pragma: no cover - guarded by the lengths above
        raise ValueError("execution name too long")
    return name


@dataclass(frozen=True, slots=True)
class ExecutionIdentity:
    """Fields encoded in an execution name."""

    account_id: str
    case_component: str
    run_id: str
    attempt: int


def parse_execution_name(name: str) -> ExecutionIdentity | None:
    """Decode an execution name produced by `execution_name`; `None` for any other name."""
    match = _EXECUTION_NAME.match(name)
    if match is None:
        return None
    return ExecutionIdentity(match.group(1), match.group(2), match.group(3), int(match.group(4)))


def execution_arn(state_machine_arn: str, name: str) -> str:
    """Execution ARN for `name` under a state machine ARN."""
    parts = state_machine_arn.split(":")
    if len(parts) != 7 or parts[5] != "stateMachine":  # noqa: PLR2004
        raise ValueError("invalid state machine ARN")
    return ":".join([*parts[:5], "execution", parts[6], name])


def execution_input(ref: SummaryRef, attempt: int) -> dict[str, Any]:
    """Workflow input: the case reference plus the attempt identity. References only.

    An operator redrive ID is left out so every delivery of an attempt yields identical input.
    """
    return {
        "case": {k: v for k, v in ref.to_message().items() if k != "redriveId"},
        "attempt": {
            "runId": ref.run_id,
            "number": attempt,
            "executionName": execution_name(ref.account_id, ref.display_id, ref.run_id, attempt),
        },
    }


def encode_input(document: Mapping[str, Any]) -> str:
    """Deterministic JSON so a repeated StartExecution sends byte-identical input."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, slots=True)
class OwnershipMarker:
    """Which execution owns a case, and the exact input it was (or will be) started with."""

    account_id: str
    case_id: str
    display_id: str
    run_id: str
    attempt: int
    execution_name: str
    claimed_at: datetime
    input: dict[str, Any]

    @classmethod
    def claim(cls, ref: SummaryRef, attempt: int, claimed_at: datetime) -> OwnershipMarker:
        document = execution_input(ref, attempt)
        return cls(
            account_id=ref.account_id,
            case_id=ref.case_id,
            display_id=ref.display_id,
            run_id=ref.run_id,
            attempt=attempt,
            execution_name=document["attempt"]["executionName"],
            claimed_at=claimed_at,
            input=document,
        )

    @property
    def key(self) -> str:
        return marker_key(self.account_id, self.display_id)

    def to_document(self) -> dict[str, Any]:
        return {
            "schemaVersion": MARKER_SCHEMA_VERSION,
            "accountId": self.account_id,
            "caseId": self.case_id,
            "displayId": self.display_id,
            "runId": self.run_id,
            "attempt": self.attempt,
            "executionName": self.execution_name,
            "claimedAt": self.claimed_at.isoformat(timespec="seconds"),
            "input": self.input,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> OwnershipMarker:
        """Parse and validate a stored marker; raise `InvalidOwnershipMarkerError` otherwise."""
        if set(document) != _MARKER_KEYS or document["schemaVersion"] != MARKER_SCHEMA_VERSION:
            raise InvalidOwnershipMarkerError("marker fields do not match the contract")
        attempt = document["attempt"]
        payload = document["input"]
        if (
            not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or not isinstance(payload, dict)
        ):
            raise InvalidOwnershipMarkerError("marker attempt or input is malformed")
        try:
            claimed_at = datetime.fromisoformat(str(document["claimedAt"]))
            ref = SummaryRef.parse(payload.get("case") or {})
            expected = execution_input(ref, attempt)
        except (ValueError, InvalidReferenceError):
            raise InvalidOwnershipMarkerError("marker content is malformed") from None
        if claimed_at.tzinfo is None:
            raise InvalidOwnershipMarkerError("marker claimedAt has no timezone")
        identity = (document["accountId"], document["caseId"], document["displayId"])
        if (
            identity != (ref.account_id, ref.case_id, ref.display_id)
            or document["runId"] != ref.run_id
            or document["executionName"] != expected["attempt"]["executionName"]
            or payload != expected
        ):
            raise InvalidOwnershipMarkerError("marker identity is inconsistent")
        return cls(
            account_id=ref.account_id,
            case_id=ref.case_id,
            display_id=ref.display_id,
            run_id=ref.run_id,
            attempt=attempt,
            execution_name=expected["attempt"]["executionName"],
            claimed_at=claimed_at,
            input=payload,
        )

    @property
    def case_ref(self) -> SummaryRef:
        """The case reference the execution was (or will be) started with."""
        return SummaryRef.parse(self.input["case"])

    def owns(self, ref: SummaryRef) -> bool:
        """True when the marker is for the same case as `ref`."""
        return (self.account_id, self.case_id, self.display_id) == (
            ref.account_id,
            ref.case_id,
            ref.display_id,
        )


@dataclass(frozen=True, slots=True)
class ResetClaim:
    """A claim on a case about to be reset, stored in place of the ownership marker.

    Case Cleanup (before deleting inputs) and the reprocess script (before deleting checkpoints)
    write it with a conditional write on the exact marker they judged closed (or on there being
    none), so a starter or another resetter that took ownership first always wins.
    Starters never overwrite a claim younger than `RESET_CLAIM_STALE_AFTER`; an older claim was
    left by a resetter that stopped mid-reset and may be taken over.
    """

    account_id: str
    display_id: str
    claimed_at: datetime

    _KEYS = frozenset({"schemaVersion", "kind", "accountId", "displayId", "claimedAt"})

    @property
    def key(self) -> str:
        return marker_key(self.account_id, self.display_id)

    def to_document(self) -> dict[str, Any]:
        return {
            "schemaVersion": MARKER_SCHEMA_VERSION,
            "kind": RESET_CLAIM_KIND,
            "accountId": self.account_id,
            "displayId": self.display_id,
            "claimedAt": self.claimed_at.isoformat(timespec="seconds"),
        }

    @staticmethod
    def is_claim(document: Mapping[str, Any]) -> bool:
        """True when a stored `workflow.json` document is a reset claim, valid or not."""
        return document.get("kind") == RESET_CLAIM_KIND

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> ResetClaim:
        """Parse and validate a stored claim; raise `InvalidOwnershipMarkerError` otherwise."""
        if (
            set(document) != cls._KEYS
            or document["schemaVersion"] != MARKER_SCHEMA_VERSION
            or document["kind"] != RESET_CLAIM_KIND
        ):
            raise InvalidOwnershipMarkerError("reset claim fields do not match the contract")
        try:
            account_id = validate_account_id(document["accountId"])
            display_id = validate_display_id(document["displayId"])
            claimed_at = datetime.fromisoformat(str(document["claimedAt"]))
        except (ValueError, TypeError):
            raise InvalidOwnershipMarkerError("reset claim content is malformed") from None
        if claimed_at.tzinfo is None:
            raise InvalidOwnershipMarkerError("reset claim claimedAt has no timezone")
        return cls(account_id, display_id, claimed_at)

    def is_stale(self, now: datetime) -> bool:
        """True once no cleanup that wrote this claim can still be running."""
        return now - self.claimed_at > RESET_CLAIM_STALE_AFTER


def read_ownership(
    s3: S3Client, bucket: str, account_id: str, display_id: str
) -> tuple[OwnershipMarker | ResetClaim, str] | None:
    """The case's `workflow.json` (marker or reset claim) and its ETag; `None` when absent.

    Unreadable or invalid documents, and documents for another case, raise
    `InvalidOwnershipMarkerError`.
    """
    try:
        stored = read_json_with_etag(
            s3, bucket, marker_key(account_id, display_id), max_bytes=MAX_MARKER_BYTES
        )
    except (S3JsonDecodeError, S3ObjectTooLargeError):
        raise InvalidOwnershipMarkerError("marker is unreadable") from None
    if stored is None:
        return None
    parsed: OwnershipMarker | ResetClaim
    if ResetClaim.is_claim(stored.document):
        parsed = ResetClaim.from_document(stored.document)
    else:
        parsed = OwnershipMarker.from_document(stored.document)
    if (parsed.account_id, parsed.display_id) != (account_id, display_id):
        raise InvalidOwnershipMarkerError("marker is for another case")
    return parsed, stored.etag


def describe_status(sfn: SFNClient, arn: str) -> str | None:
    """Execution status, or `None` when the execution does not exist."""
    described = describe_execution(sfn, arn)
    return None if described is None else described.status


@dataclass(frozen=True, slots=True)
class ExecutionDescription:
    """Status of an execution and the case attempt it was started for, when recognisable."""

    status: str
    case: SummaryRef | None
    attempt: int | None


def parse_execution_input(name: str, raw_input: object) -> tuple[SummaryRef, int] | None:
    """The case reference and attempt of a CaseAnalysis input, if it matches execution `name`."""
    if not isinstance(raw_input, str):
        return None
    try:
        document = json.loads(raw_input)
        attempt = document["attempt"]["number"]
        ref = SummaryRef.parse(document["case"])
        expected = execution_input(ref, attempt)
    except (ValueError, TypeError, KeyError, InvalidReferenceError):
        return None
    if document != expected or expected["attempt"]["executionName"] != name:
        return None
    return ref, attempt


def describe_execution(sfn: SFNClient, arn: str) -> ExecutionDescription | None:
    """Describe an execution; `None` when it does not exist. Input holds references only."""
    try:
        response = sfn.describe_execution(executionArn=arn)
    except ClientError as exc:
        if aws_error_code(exc) == "ExecutionDoesNotExist":
            return None
        raise
    parsed = parse_execution_input(arn.rsplit(":", 1)[-1], response.get("input"))
    return ExecutionDescription(
        status=response["status"],
        case=parsed[0] if parsed else None,
        attempt=parsed[1] if parsed else None,
    )


def start_execution(
    sfn: SFNClient, state_machine_arn: str, name: str, document: Mapping[str, Any]
) -> bool:
    """Start (or idempotently confirm) execution `name`.

    Returns False when the name already belongs to another execution (a closed one, or one with
    different input); the caller must describe it rather than assume success.
    """
    try:
        sfn.start_execution(
            stateMachineArn=state_machine_arn, name=name, input=encode_input(document)
        )
    except ClientError as exc:
        if aws_error_code(exc) == "ExecutionAlreadyExists":
            return False
        raise
    return True

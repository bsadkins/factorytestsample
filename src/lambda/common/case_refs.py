"""Queue message contracts between account discovery, retrieval, annotation and analysis.

Messages carry identifiers and S3 keys only, never case content. Parsers reject anything that is
not exactly the expected shape so a malformed message fails visibly instead of being guessed at.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from common.account_list import MAX_ACCOUNT_NAME_LENGTH, validate_run_id
from common.processed import case_record_key, validate_account_id, validate_display_id

COMMUNICATIONS_FILE: Final = "communications.json"
_CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
_VERSION_ID = re.compile(r"^[A-Za-z0-9._+=/-]{1,1024}$")
_REDRIVE_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
MAX_COMMUNICATION_COUNT: Final = 100_000


class InvalidReferenceError(ValueError):
    """A queue message does not match its contract. Messages never echo field values."""


def validate_case_id(case_id: str) -> str:
    """Return `case_id` if it is a well-formed Support case ID, otherwise raise `ValueError`."""
    if not isinstance(case_id, str) or not _CASE_ID.match(case_id):
        raise ValueError("invalid case ID")
    return case_id


def communications_key(account_id: str, display_id: str) -> str:
    """Return `account_number=<accountId>/case_number=<displayId>/communications.json`."""
    prefix = case_record_key(account_id, display_id).rsplit("/", 1)[0]
    return f"{prefix}/{COMMUNICATIONS_FILE}"


def _fields(body: Mapping[str, Any], required: frozenset[str], optional: frozenset[str]) -> None:
    keys = set(body)
    if not required <= keys or keys - required - optional:
        raise InvalidReferenceError("message fields do not match the contract")


def _string(body: Mapping[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str):
        raise InvalidReferenceError(f"message field {name} must be a string")
    return value


def _checked[T](name: str, validator: Any, value: T) -> T:  # noqa: ANN401
    try:
        validator(value)
    except ValueError:
        raise InvalidReferenceError(f"message field {name} is invalid") from None
    return value


@dataclass(frozen=True, slots=True)
class AccountMessage:
    """Active-accounts queue message `{accountId, accountName, runId}`."""

    account_id: str
    account_name: str
    run_id: str

    _REQUIRED = frozenset({"accountId", "accountName", "runId"})

    @classmethod
    def parse(cls, body: Mapping[str, Any]) -> AccountMessage:
        _fields(body, cls._REQUIRED, frozenset())
        name = _string(body, "accountName")
        if not 0 < len(name) <= MAX_ACCOUNT_NAME_LENGTH:
            raise InvalidReferenceError("message field accountName is invalid")
        return cls(
            account_id=_checked("accountId", validate_account_id, _string(body, "accountId")),
            account_name=name,
            run_id=_checked("runId", validate_run_id, _string(body, "runId")),
        )

    def idempotency_key(self) -> dict[str, str]:
        """One retrieval per account per run; a new run is always evaluated afresh."""
        return {"accountId": self.account_id, "runId": self.run_id}


@dataclass(frozen=True, slots=True)
class AnnotationRef:
    """Annotation queue message: a retrieved case whose raw metadata is stored."""

    account_id: str
    case_id: str
    display_id: str
    run_id: str

    _REQUIRED = frozenset({"accountId", "caseId", "displayId", "runId", "rawKey"})

    @property
    def raw_key(self) -> str:
        return case_record_key(self.account_id, self.display_id)

    @property
    def communications_key(self) -> str:
        return communications_key(self.account_id, self.display_id)

    def to_message(self) -> dict[str, str]:
        return {
            "accountId": self.account_id,
            "caseId": self.case_id,
            "displayId": self.display_id,
            "runId": self.run_id,
            "rawKey": self.raw_key,
        }

    def idempotency_key(self) -> dict[str, str]:
        """One annotation per case per run; a later run re-evaluates unfinished cases."""
        return {"accountId": self.account_id, "caseId": self.case_id, "runId": self.run_id}

    @classmethod
    def parse(cls, body: Mapping[str, Any]) -> AnnotationRef:
        _fields(body, cls._REQUIRED, frozenset())
        ref = cls(
            account_id=_checked("accountId", validate_account_id, _string(body, "accountId")),
            case_id=_checked("caseId", validate_case_id, _string(body, "caseId")),
            display_id=_checked("displayId", validate_display_id, _string(body, "displayId")),
            run_id=_checked("runId", validate_run_id, _string(body, "runId")),
        )
        if _string(body, "rawKey") != ref.raw_key:
            raise InvalidReferenceError("message field rawKey does not match the case partition")
        return ref


@dataclass(frozen=True, slots=True)
class SummaryRef:
    """Summary queue message: a case whose full conversation is stored in the raw bucket.

    `redriveId` is optional and set only by an operator redrive, so a deliberate redrive of an
    attempt that already ran is not suppressed as a duplicate delivery.
    """

    account_id: str
    case_id: str
    display_id: str
    run_id: str
    communications_version_id: str | None
    communication_count: int
    redrive_id: str | None = None

    _REQUIRED = frozenset(
        {
            "accountId",
            "caseId",
            "displayId",
            "runId",
            "rawKey",
            "communicationsKey",
            "communicationsVersionId",
            "communicationCount",
        }
    )
    _OPTIONAL = frozenset({"redriveId"})

    @property
    def raw_key(self) -> str:
        return case_record_key(self.account_id, self.display_id)

    @property
    def communications_key(self) -> str:
        return communications_key(self.account_id, self.display_id)

    def to_message(self) -> dict[str, Any]:
        return {
            "accountId": self.account_id,
            "caseId": self.case_id,
            "displayId": self.display_id,
            "runId": self.run_id,
            "rawKey": self.raw_key,
            "communicationsKey": self.communications_key,
            "communicationsVersionId": self.communications_version_id,
            "communicationCount": self.communication_count,
            **({"redriveId": self.redrive_id} if self.redrive_id is not None else {}),
        }

    def idempotency_key(self) -> dict[str, Any]:
        """One start decision per case, run, conversation version and redrive."""
        return {
            "accountId": self.account_id,
            "caseId": self.case_id,
            "runId": self.run_id,
            "communicationsVersionId": self.communications_version_id,
            "redriveId": self.redrive_id,
        }

    @classmethod
    def parse(cls, body: Mapping[str, Any]) -> SummaryRef:
        _fields(body, cls._REQUIRED, cls._OPTIONAL)
        redrive = body.get("redriveId")
        if redrive is not None and (not isinstance(redrive, str) or not _REDRIVE_ID.match(redrive)):
            raise InvalidReferenceError("message field redriveId is invalid")
        version = body.get("communicationsVersionId")
        if version is not None and (not isinstance(version, str) or not _VERSION_ID.match(version)):
            raise InvalidReferenceError("message field communicationsVersionId is invalid")
        count = body.get("communicationCount")
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or not 0 <= count <= MAX_COMMUNICATION_COUNT
        ):
            raise InvalidReferenceError("message field communicationCount is invalid")
        ref = cls(
            account_id=_checked("accountId", validate_account_id, _string(body, "accountId")),
            case_id=_checked("caseId", validate_case_id, _string(body, "caseId")),
            display_id=_checked("displayId", validate_display_id, _string(body, "displayId")),
            run_id=_checked("runId", validate_run_id, _string(body, "runId")),
            communications_version_id=version,
            communication_count=count,
            redrive_id=redrive,
        )
        if _string(body, "rawKey") != ref.raw_key or (
            _string(body, "communicationsKey") != ref.communications_key
        ):
            raise InvalidReferenceError("message keys do not match the case partition")
        return ref

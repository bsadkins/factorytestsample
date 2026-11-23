"""Run-scoped account-list objects written by account lookup and read by the account reader.

One JSON document per discovery run lives at `account-lists/<runId>.json` in the account-list
bucket. It holds the selected member accounts and the discovery counts, never case content.
Scheduled runs derive `runId` from the schedule time, so a repeated invocation for the same
schedule maps to the same object; manual runs add a random suffix.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from common.processed import validate_account_id

ACCOUNT_LIST_PREFIX: Final = "account-lists/"
ACCOUNT_LIST_SUFFIX: Final = ".json"
SCHEMA_VERSION: Final = 1
MAX_ACCOUNTS: Final = 50_000
MAX_ACCOUNT_LIST_BYTES: Final = 16 * 1024 * 1024
MAX_ACCOUNT_NAME_LENGTH: Final = 256

_RUN_ID_PATTERN: Final = r"\d{8}T\d{6}Z(?:-[0-9a-f]{8})?"
_RUN_ID: Final = re.compile(rf"^{_RUN_ID_PATTERN}$")
_KEY: Final = re.compile(rf"^{re.escape(ACCOUNT_LIST_PREFIX)}({_RUN_ID_PATTERN})\.json$")
_TOKEN: Final = re.compile(r"^[0-9a-f]{8}$")
_RUN_TIME_FORMAT: Final = "%Y%m%dT%H%M%SZ"
_DOCUMENT_KEYS: Final = frozenset({"schemaVersion", "runId", "generatedAt", "accounts", "counts"})
_ACCOUNT_KEYS: Final = frozenset({"accountId", "accountName"})
_COUNT_KEYS: Final = ("listed", "selected", "excludedInactive", "excludedManagement")


class InvalidAccountListError(ValueError):
    """The account-list document is malformed. Messages never contain account names."""


@dataclass(frozen=True, slots=True)
class AccountRef:
    """A selected member account."""

    account_id: str
    account_name: str

    def to_message(self, run_id: str) -> dict[str, str]:
        """The active-accounts queue message for this account in `run_id`."""
        return {"accountId": self.account_id, "accountName": self.account_name, "runId": run_id}


@dataclass(frozen=True, slots=True)
class DiscoveryCounts:
    """Accounts returned by Organizations, selected, and excluded by reason."""

    listed: int
    selected: int
    excluded_inactive: int
    excluded_management: int

    def to_document(self) -> dict[str, int]:
        return {
            "listed": self.listed,
            "selected": self.selected,
            "excludedInactive": self.excluded_inactive,
            "excludedManagement": self.excluded_management,
        }


@dataclass(frozen=True, slots=True)
class AccountList:
    """A validated account-list document."""

    run_id: str
    generated_at: str
    accounts: tuple[AccountRef, ...]
    counts: DiscoveryCounts


def validate_run_id(run_id: str) -> str:
    """Return `run_id` if it is a well-formed run ID, otherwise raise `ValueError`."""
    if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
        raise ValueError("invalid run ID")
    return run_id


def scheduled_run_id(scheduled_time: datetime) -> str:
    """Deterministic run ID for a scheduled invocation, e.g. `20261113T000000Z`."""
    if scheduled_time.tzinfo is None:
        raise ValueError("scheduled time must include a timezone")
    return scheduled_time.astimezone(UTC).strftime(_RUN_TIME_FORMAT)


def manual_run_id(now: datetime, token: str) -> str:
    """Run ID for an ad hoc invocation: the UTC time plus an 8-hex-digit token."""
    if not _TOKEN.match(token):
        raise ValueError("token must be 8 lower-case hex digits")
    return f"{scheduled_run_id(now)}-{token}"


def account_list_key(run_id: str) -> str:
    """Return `account-lists/<runId>.json`."""
    return f"{ACCOUNT_LIST_PREFIX}{validate_run_id(run_id)}{ACCOUNT_LIST_SUFFIX}"


def parse_account_list_key(key: str) -> str | None:
    """Return the run ID of an account-list key, or `None` for any other key."""
    match = _KEY.match(key)
    return match.group(1) if match else None


def build_account_list_document(
    run_id: str,
    generated_at: datetime,
    accounts: Iterable[AccountRef],
    counts: DiscoveryCounts,
) -> dict[str, Any]:
    """Build the account-list document; raises `InvalidAccountListError` if inconsistent."""
    document: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "runId": validate_run_id(run_id),
        "generatedAt": generated_at.astimezone(UTC).isoformat(timespec="seconds"),
        "accounts": [
            {"accountId": ref.account_id, "accountName": ref.account_name} for ref in accounts
        ],
        "counts": counts.to_document(),
    }
    _validate_document(document, run_id=run_id, management_account_id=None)
    return document


def parse_account_list_document(
    document: object, *, run_id: str, management_account_id: str
) -> AccountList:
    """Validate an account-list document read from `account-lists/<run_id>.json`.

    The document must match the key's run ID, contain unique valid accounts (never the
    management account) and carry counts consistent with the account list.
    """
    return _validate_document(document, run_id=run_id, management_account_id=management_account_id)


def _validate_document(
    document: object, *, run_id: str, management_account_id: str | None
) -> AccountList:
    if not isinstance(document, Mapping):
        raise InvalidAccountListError("account list is not a JSON object")
    unexpected = set(document) - _DOCUMENT_KEYS
    if unexpected:
        raise InvalidAccountListError(f"account list has unexpected fields: {sorted(unexpected)}")
    if document.get("schemaVersion") != SCHEMA_VERSION:
        raise InvalidAccountListError("unsupported account list schema version")
    if document.get("runId") != run_id:
        raise InvalidAccountListError("account list run ID does not match its key")
    generated_at = document.get("generatedAt")
    if not isinstance(generated_at, str) or not _is_aware_timestamp(generated_at):
        raise InvalidAccountListError("account list generatedAt is not a UTC timestamp")
    accounts = _validate_accounts(document.get("accounts"), management_account_id)
    counts = _validate_counts(document.get("counts"), len(accounts))
    return AccountList(run_id=run_id, generated_at=generated_at, accounts=accounts, counts=counts)


def _is_aware_timestamp(value: str) -> bool:
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _validate_accounts(
    entries: object, management_account_id: str | None
) -> tuple[AccountRef, ...]:
    if not isinstance(entries, list):
        raise InvalidAccountListError("account list accounts is not an array")
    if len(entries) > MAX_ACCOUNTS:
        raise InvalidAccountListError(f"account list has more than {MAX_ACCOUNTS} accounts")
    refs: list[AccountRef] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or set(entry) != _ACCOUNT_KEYS:
            raise InvalidAccountListError(f"account entry {index} is malformed")
        account_id = entry["accountId"]
        try:
            validate_account_id(account_id if isinstance(account_id, str) else "")
        except ValueError:
            raise InvalidAccountListError(
                f"account entry {index} has an invalid account ID"
            ) from None
        if not _is_valid_name(entry["accountName"]):
            raise InvalidAccountListError(f"account entry {index} has an invalid account name")
        if account_id in seen:
            raise InvalidAccountListError(f"account entry {index} is a duplicate")
        if account_id == management_account_id:
            raise InvalidAccountListError(f"account entry {index} is the management account")
        seen.add(account_id)
        refs.append(AccountRef(account_id=account_id, account_name=entry["accountName"]))
    return tuple(refs)


def _is_valid_name(name: object) -> bool:
    return isinstance(name, str) and 0 < len(name) <= MAX_ACCOUNT_NAME_LENGTH and name.isprintable()


def _validate_counts(counts: object, selected: int) -> DiscoveryCounts:
    if not isinstance(counts, Mapping) or set(counts) != set(_COUNT_KEYS):
        raise InvalidAccountListError("account list counts are malformed")
    values = [counts[name] for name in _COUNT_KEYS]
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in values):
        raise InvalidAccountListError("account list counts are malformed")
    result = DiscoveryCounts(*values)
    if result.selected != selected or result.listed != (
        result.selected + result.excluded_inactive + result.excluded_management
    ):
        raise InvalidAccountListError("account list counts are inconsistent with its accounts")
    return result

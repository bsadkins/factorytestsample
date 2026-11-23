"""Processed-record keys and batched existence checks.

Processed records live at `account_number=<accountId>/case_number=<displayId>/data.json`, the
Hive-style partition layout read by the Analytics stack. Raw case data uses the same layout in
the raw bucket.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

RECORD_FILE: Final = "data.json"
_ACCOUNT_ID = re.compile(r"^\d{12}$")
_DISPLAY_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_RECORD_KEY = re.compile(r"^account_number=(\d{12})/case_number=([A-Za-z0-9_-]{1,64})/data\.json$")


def validate_account_id(account_id: str) -> str:
    """Return `account_id` if it is 12 digits, otherwise raise `ValueError`."""
    if not _ACCOUNT_ID.match(account_id):
        raise ValueError("invalid account ID")
    return account_id


def validate_display_id(display_id: str) -> str:
    """Return `display_id` if it is a safe path segment, otherwise raise `ValueError`."""
    if not _DISPLAY_ID.match(display_id):
        raise ValueError("invalid case display ID")
    return display_id


def case_partition_prefix(account_id: str) -> str:
    """Return `account_number=<accountId>/`."""
    return f"account_number={validate_account_id(account_id)}/"


def case_record_key(account_id: str, display_id: str) -> str:
    """Return `account_number=<accountId>/case_number=<displayId>/data.json`."""
    return (
        f"{case_partition_prefix(account_id)}case_number={validate_display_id(display_id)}/"
        f"{RECORD_FILE}"
    )


def parse_case_record_key(key: str) -> tuple[str, str] | None:
    """Return `(accountId, displayId)` for a record key, or `None` for any other key."""
    match = _RECORD_KEY.match(key)
    return (match.group(1), match.group(2)) if match else None


def get_existing_cases_batch(
    s3: S3Client,
    bucket: str,
    account_id: str,
    display_ids: Iterable[str] | None = None,
    *,
    page_size: int = 1000,
) -> set[str]:
    """Return the display IDs that already have a processed `data.json` for `account_id`.

    Lists the account's partition once (paginated) instead of issuing one request per case. When
    `display_ids` is given, only those IDs are returned; an empty collection makes no request.
    """
    wanted = None if display_ids is None else set(display_ids)
    if wanted is not None and not wanted:
        return set()
    prefix = f"{case_partition_prefix(account_id)}case_number="
    found: set[str] = set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(
        Bucket=bucket, Prefix=prefix, PaginationConfig={"PageSize": page_size}
    ):
        for item in page.get("Contents", []):
            parsed = parse_case_record_key(item.get("Key", ""))
            if parsed is not None and parsed[0] == account_id:
                found.add(parsed[1])
    return found if wanted is None else found & wanted

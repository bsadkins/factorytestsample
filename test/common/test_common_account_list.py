"""Run-scoped account-list keys, run IDs and document validation."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from common.account_list import (
    ACCOUNT_LIST_PREFIX,
    AccountRef,
    DiscoveryCounts,
    InvalidAccountListError,
    account_list_key,
    build_account_list_document,
    manual_run_id,
    parse_account_list_document,
    parse_account_list_key,
    scheduled_run_id,
    validate_run_id,
)

MANAGEMENT = "999999999999"
RUN = "20261113T000000Z"


def document(**overrides: object) -> dict[str, Any]:
    doc = build_account_list_document(
        RUN,
        datetime(2026, 11, 13, 0, 0, 5, tzinfo=UTC),
        [AccountRef("111111111111", "Dev"), AccountRef("222222222222", "Prod")],
        DiscoveryCounts(listed=4, selected=2, excluded_inactive=1, excluded_management=1),
    )
    doc.update(overrides)
    return doc


def test_scheduled_run_id_is_deterministic_in_utc() -> None:
    scheduled = datetime(2026, 11, 13, 1, 0, tzinfo=timezone(timedelta(hours=1)))
    assert scheduled_run_id(scheduled) == RUN
    with pytest.raises(ValueError, match="timezone"):
        scheduled_run_id(datetime(2026, 11, 13))


def test_manual_run_id_is_unique_per_token() -> None:
    now = datetime(2026, 11, 13, 10, 30, 1, tzinfo=UTC)
    assert manual_run_id(now, "0a1b2c3d") == "20261113T103001Z-0a1b2c3d"
    with pytest.raises(ValueError, match="token"):
        manual_run_id(now, "NOT-HEX")


@pytest.mark.parametrize(
    "run_id",
    ["", "2026-11-13", "20261113T000000Z-", "20261113T000000Z/../x", "../20261113T000000Z"],
)
def test_invalid_run_ids_are_rejected(run_id: str) -> None:
    with pytest.raises(ValueError, match="run ID"):
        validate_run_id(run_id)


def test_key_round_trip_and_unrelated_keys() -> None:
    key = account_list_key(RUN)
    assert key == f"{ACCOUNT_LIST_PREFIX}{RUN}.json"
    assert parse_account_list_key(key) == RUN
    assert parse_account_list_key(f"{ACCOUNT_LIST_PREFIX}{RUN}-0a1b2c3d.json") == f"{RUN}-0a1b2c3d"
    for unrelated in (
        f"{ACCOUNT_LIST_PREFIX}{RUN}.json.tmp",
        f"other/{RUN}.json",
        f"{ACCOUNT_LIST_PREFIX}nested/{RUN}.json",
        f"{ACCOUNT_LIST_PREFIX}notes.json",
    ):
        assert parse_account_list_key(unrelated) is None


def test_valid_document_round_trips() -> None:
    parsed = parse_account_list_document(document(), run_id=RUN, management_account_id=MANAGEMENT)
    assert parsed.run_id == RUN
    assert parsed.accounts == (
        AccountRef("111111111111", "Dev"),
        AccountRef("222222222222", "Prod"),
    )
    assert parsed.counts == DiscoveryCounts(4, 2, 1, 1)
    assert parsed.accounts[0].to_message(RUN) == {
        "accountId": "111111111111",
        "accountName": "Dev",
        "runId": RUN,
    }


def test_empty_list_is_valid() -> None:
    doc = build_account_list_document(
        RUN, datetime(2026, 11, 13, tzinfo=UTC), [], DiscoveryCounts(1, 0, 0, 1)
    )
    parsed = parse_account_list_document(doc, run_id=RUN, management_account_id=MANAGEMENT)
    assert parsed.accounts == ()


def test_builder_rejects_inconsistent_counts() -> None:
    with pytest.raises(InvalidAccountListError, match="counts"):
        build_account_list_document(
            RUN,
            datetime(2026, 11, 13, tzinfo=UTC),
            [AccountRef("111111111111", "Dev")],
            DiscoveryCounts(listed=1, selected=2, excluded_inactive=0, excluded_management=0),
        )


@pytest.mark.parametrize(
    ("doc", "reason"),
    [
        ([], "JSON object"),
        (document(schemaVersion=2), "schema version"),
        (document(runId="20261112T000000Z"), "run ID"),
        (document(generatedAt="yesterday"), "generatedAt"),
        (document(accounts="111111111111"), "accounts"),
        (document(accounts=[{"accountId": "1234", "accountName": "Short"}]), "account ID"),
        (document(accounts=[{"accountId": "111111111111"}]), "account entry"),
        (document(accounts=[{"accountId": "111111111111", "accountName": ""}]), "account name"),
        (document(accounts=[{"accountId": "111111111111", "accountName": "a\nb"}]), "account name"),
        (
            document(
                accounts=[
                    {"accountId": "111111111111", "accountName": "A"},
                    {"accountId": "111111111111", "accountName": "B"},
                ]
            ),
            "duplicate",
        ),
        (
            document(accounts=[{"accountId": MANAGEMENT, "accountName": "Mgmt"}]),
            "management",
        ),
        (document(counts={"listed": 4}), "counts"),
        (
            document(
                counts={"listed": 9, "selected": 2, "excludedInactive": 1, "excludedManagement": 1}
            ),
            "counts",
        ),
        (document(extra=True), "unexpected"),
    ],
)
def test_malformed_documents_are_rejected(doc: object, reason: str) -> None:
    with pytest.raises(InvalidAccountListError, match=reason):
        parse_account_list_document(doc, run_id=RUN, management_account_id=MANAGEMENT)


def test_error_messages_never_include_account_names() -> None:
    secret = "SENTINEL-ACCOUNT-NAME"
    doc = document(accounts=[{"accountId": "1", "accountName": secret}])
    with pytest.raises(InvalidAccountListError) as raised:
        parse_account_list_document(doc, run_id=RUN, management_account_id=MANAGEMENT)
    assert secret not in str(raised.value)

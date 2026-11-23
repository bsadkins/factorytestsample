"""Case collection helpers: creation windows, queue references, conversations and Support access."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from common.bedrock import DEFAULT_MAX_PROMPT_CHARS, MAX_MAX_TOKENS
from common.case_refs import (
    AccountMessage,
    AnnotationRef,
    InvalidReferenceError,
    SummaryRef,
    communications_key,
    validate_case_id,
)
from common.case_window import (
    CreationWindow,
    creation_window,
    format_support_time,
    parse_support_time,
    run_started_at,
    subtract_calendar_months,
    validate_lookback_months,
)
from common.conversation import (
    MAX_CASE_ANNOTATION_CHARS,
    SUMMARY_PROMPT_RESERVE_CHARS,
    ConversationTooLargeError,
    author_label,
    check_annotation_size,
    chronological,
    render_case_annotation,
    utc_timestamp,
)
from common.sqs_batch import RecordFailedError
from common.sts import AccountAccessDeniedError
from common.support import (
    CaseNotFoundError,
    SupportAccessSkippedError,
    case_metadata,
    iter_cases,
    list_communications,
    skip_reason,
    support_api_region,
)

TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "templates"
ACCOUNT = "111111111111"
RUN = "20261113T000000Z"


def utc(  # noqa: PLR0913, PLR0917
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "m"}}, "Op")


# Creation windows -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "months", "expected"),
    [
        (utc(2026, 11, 13), 12, utc(2025, 11, 13)),
        (utc(2026, 1, 31), 1, utc(2025, 12, 31)),
        (utc(2026, 3, 31), 1, utc(2026, 2, 28)),
        (utc(2024, 3, 31), 1, utc(2024, 2, 29)),
        (utc(2024, 2, 29), 12, utc(2023, 2, 28)),
        (utc(2025, 5, 31), 3, utc(2025, 2, 28)),
        (utc(2026, 7, 31, 23, 59, 59), 1, utc(2026, 6, 30, 23, 59, 59)),
        (utc(2026, 4, 15), 0, utc(2026, 4, 15)),
    ],
)
def test_subtract_calendar_months_clamps_to_month_end(
    moment: datetime, months: int, expected: datetime
) -> None:
    assert subtract_calendar_months(moment, months) == expected


def test_subtract_calendar_months_rejects_negative() -> None:
    with pytest.raises(ValueError, match="negative"):
        subtract_calendar_months(utc(2026, 1, 1), -1)


def test_run_start_comes_from_scheduled_and_manual_run_ids() -> None:
    assert run_started_at(RUN) == utc(2026, 11, 13)
    assert run_started_at("20260102T030405Z-0a1b2c3d") == utc(2026, 1, 2, 3, 4, 5)
    for bad in ("2026-11-13", "20261313T000000Z", "20261113T250000Z"):
        with pytest.raises(ValueError, match="run ID"):
            run_started_at(bad)


def test_creation_window_is_half_open_and_formats_api_parameters() -> None:
    window = creation_window(RUN, 12)
    assert window == CreationWindow(after=utc(2025, 11, 13), before=utc(2026, 11, 13))
    assert window.request_parameters() == {
        "afterTime": "2025-11-13T00:00:00Z",
        "beforeTime": "2026-11-13T00:00:00Z",
    }
    assert window.contains(utc(2025, 11, 13))
    assert not window.contains(utc(2026, 11, 13))
    assert window.contains(utc(2026, 11, 12, 23, 59, 59))


@pytest.mark.parametrize("months", [0, 13, -1, True])
def test_lookback_months_are_bounded(months: int) -> None:
    with pytest.raises(ValueError, match="lookback"):
        validate_lookback_months(months)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-01-15T10:20:30.123Z", datetime(2026, 1, 15, 10, 20, 30, 123000, tzinfo=UTC)),
        ("2026-01-15T12:20:30+02:00", utc(2026, 1, 15, 10, 20, 30)),
        ("2026-01-15T10:20:30", None),
        ("yesterday", None),
        ("", None),
        (None, None),
        (17, None),
    ],
)
def test_parse_support_time(value: object, expected: datetime | None) -> None:
    assert parse_support_time(value) == expected


def test_timestamps_require_timezones() -> None:
    naive = datetime(2026, 1, 1)
    with pytest.raises(ValueError, match="timezone"):
        format_support_time(naive)
    with pytest.raises(ValueError, match="timezone"):
        utc_timestamp(naive)
    assert utc_timestamp(utc(2026, 1, 1, 2, 3, 4)) == "2026-01-01T02:03:04Z"


# Queue references --------------------------------------------------------------------------------


def test_account_message_parses_and_keys_idempotency_by_account_and_run() -> None:
    message = AccountMessage.parse({"accountId": ACCOUNT, "accountName": "Prod", "runId": RUN})
    assert message.idempotency_key() == {"accountId": ACCOUNT, "runId": RUN}
    with pytest.raises(InvalidReferenceError, match="accountName"):
        AccountMessage.parse({"accountId": ACCOUNT, "accountName": "", "runId": RUN})
    with pytest.raises(InvalidReferenceError, match="string"):
        AccountMessage.parse({"accountId": 111111111111, "accountName": "x", "runId": RUN})


def test_summary_reference_round_trips_and_rejects_tampering() -> None:
    summary = SummaryRef(ACCOUNT, "case-1", "1001", RUN, "v1", 3)
    message = summary.to_message()
    assert message["communicationsKey"] == communications_key(ACCOUNT, "1001")
    assert SummaryRef.parse(message) == summary
    assert (
        SummaryRef.parse({**message, "communicationsVersionId": None}).communications_version_id
        is None
    )
    for bad in (
        {**message, "communicationCount": -1},
        {**message, "communicationCount": True},
        {**message, "communicationsVersionId": "bad version"},
        {**message, "communicationsKey": "elsewhere.json"},
        {**message, "rawKey": "elsewhere.json"},
        {**message, "extra": 1},
    ):
        with pytest.raises(InvalidReferenceError):
            SummaryRef.parse(bad)


def test_annotation_reference_keys_and_case_ids() -> None:
    annotation = AnnotationRef(ACCOUNT, "case-1", "1001", RUN)
    assert annotation.communications_key == (
        f"account_number={ACCOUNT}/case_number=1001/communications.json"
    )
    assert AnnotationRef.parse(annotation.to_message()) == annotation
    assert (
        validate_case_id("case-111111111111-muen-2026-3c1f2b")
        == "case-111111111111-muen-2026-3c1f2b"
    )
    for bad in ("", "-case", "case/1", "c" * 129):
        with pytest.raises(ValueError, match="case ID"):
            validate_case_id(bad)


# Conversations ------------------------------------------------------------------------------------


def test_annotation_is_chronological_and_omits_email_addresses() -> None:
    stored = chronological(
        [
            {
                "body": "Fixed now. ",
                "submittedBy": "Amazon Web Services",
                "timeCreated": "2026-01-02T00:00:00Z",
            },
            {
                "body": "Help with S3 403",
                "submittedBy": "person@example.com",
                "timeCreated": "2026-01-01T00:00:00Z",
                "attachmentSet": [{"attachmentId": "a1", "fileName": "log.txt"}],
            },
        ]
    )
    text = render_case_annotation(stored)
    assert text == (
        "--- Message 1 | 2026-01-01T00:00:00Z | Customer | 1 attachment(s) ---\nHelp with S3 403"
        "\n\n--- Message 2 | 2026-01-02T00:00:00Z | AWS Support ---\nFixed now."
    )
    assert "example.com" not in text
    assert author_label(None) == "Customer"


def test_equal_or_missing_timestamps_keep_reverse_api_order() -> None:
    newest_first = [
        {"body": "second", "timeCreated": "2026-01-01T00:00:00Z"},
        {"body": "first", "timeCreated": "2026-01-01T00:00:00Z"},
        {"body": "undated"},
    ]
    assert [c["body"] for c in chronological(newest_first)] == ["undated", "first", "second"]


def test_annotation_size_limit_fails_instead_of_truncating() -> None:
    assert check_annotation_size("x" * 10, limit=10) == 10
    with pytest.raises(ConversationTooLargeError) as raised:
        check_annotation_size("x" * 11, limit=10)
    assert (raised.value.chars, raised.value.limit) == (11, 10)


def test_summary_prompt_reserve_covers_templates_and_a_full_length_reply() -> None:
    template = (TEMPLATES / "summary-template.txt").read_text(encoding="utf-8")
    correction = (TEMPLATES / "summary-correction-template.txt").read_text(encoding="utf-8")
    # A maximum-length reply is generously assumed to use five characters per token.
    assert len(template) + len(correction) + MAX_MAX_TOKENS * 5 < SUMMARY_PROMPT_RESERVE_CHARS
    assert MAX_CASE_ANNOTATION_CHARS + SUMMARY_PROMPT_RESERVE_CHARS == DEFAULT_MAX_PROMPT_CHARS


# Support access -----------------------------------------------------------------------------------


def test_support_region_per_partition() -> None:
    assert support_api_region("aws") == "us-east-1"
    assert support_api_region("aws-us-gov") == "us-gov-west-1"
    with pytest.raises(ValueError, match="partition"):
        support_api_region("aws-iso")


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (AccountAccessDeniedError(ACCOUNT, "AccessDenied"), "AccessDenied"),
        (client_error("AccessDeniedException"), "AccessDenied"),
        (client_error("SubscriptionRequiredException"), "SubscriptionRequired"),
        (client_error("ThrottlingException"), None),
        (ValueError("x"), None),
    ],
)
def test_skip_reason(exc: BaseException, expected: str | None) -> None:
    assert skip_reason(exc) == expected


def stubbed_support() -> tuple[Any, Stubber]:
    client = boto3.client(
        "support", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y"
    )
    stubber = Stubber(client)
    stubber.activate()
    return client, stubber


def test_non_skip_support_errors_propagate_unchanged() -> None:
    client, stubber = stubbed_support()
    stubber.add_client_error("describe_cases", service_error_code="ThrottlingException")
    with pytest.raises(ClientError):
        list(iter_cases(client, ACCOUNT, creation_window(RUN, 1)))
    stubber.add_client_error("describe_communications", service_error_code="CaseIdNotFound")
    with pytest.raises(CaseNotFoundError):
        list_communications(client, ACCOUNT, "case-1")
    stubber.add_client_error(
        "describe_communications", service_error_code="SubscriptionRequiredException"
    )
    with pytest.raises(SupportAccessSkippedError) as raised:
        list_communications(client, ACCOUNT, "case-1")
    assert raised.value.reason == "SubscriptionRequired"


def test_case_metadata_keeps_only_case_fields() -> None:
    metadata = case_metadata(
        {"caseId": "case-1", "displayId": "1", "recentCommunications": {"communications": []}}
    )
    assert "recentCommunications" not in metadata
    assert metadata["caseId"] == "case-1"
    assert metadata["subject"] is None


def test_record_failed_error_keeps_type_and_code_only() -> None:
    failure = RecordFailedError.from_exception(client_error("InternalError"))
    assert failure.log_fields() == {"error_type": "ClientError", "error_code": "InternalError"}
    assert RecordFailedError.from_exception(failure) is failure
    assert RecordFailedError.from_exception(ValueError("secret")).log_fields() == {
        "error_type": "ValueError"
    }
    assert "secret" not in str(RecordFailedError.from_exception(ValueError("secret")))

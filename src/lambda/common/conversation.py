"""Complete case conversations stored in the raw bucket and rendered for summarisation.

Annotation stores every communication returned by `DescribeCommunications`. The rendered
`case_annotation` text sent to the summary prompt is bounded: a conversation that does not fit
fails explicitly and is never truncated, so no summary is produced from part of a case.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from common.bedrock import DEFAULT_MAX_PROMPT_CHARS
from common.case_window import parse_support_time

SCHEMA_VERSION: Final = 1
# Room left in the summary prompt for the template, one model reply and the correction turn.
SUMMARY_PROMPT_RESERVE_CHARS: Final = 25_000
MAX_CASE_ANNOTATION_CHARS: Final = DEFAULT_MAX_PROMPT_CHARS - SUMMARY_PROMPT_RESERVE_CHARS
MAX_CONVERSATION_BYTES: Final = 64 * 1024 * 1024
AWS_SUPPORT_AUTHOR: Final = "Amazon Web Services"
_EARLIEST: Final = datetime.min.replace(tzinfo=UTC)


class ConversationTooLargeError(Exception):
    """The conversation exceeds the summary input bound. Carries sizes only."""

    def __init__(self, chars: int, limit: int) -> None:
        super().__init__(f"Case conversation has {chars} characters; the limit is {limit}")
        self.chars = chars
        self.limit = limit


def chronological(communications: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Communications oldest first, keeping only the stored fields.

    `DescribeCommunications` returns the newest first, so equal timestamps keep reverse API order.
    """
    indexed = list(enumerate(communications))
    indexed.sort(
        key=lambda item: (parse_support_time(item[1].get("timeCreated")) or _EARLIEST, -item[0])
    )
    return [
        {
            "body": item.get("body", ""),
            "submittedBy": item.get("submittedBy"),
            "timeCreated": item.get("timeCreated"),
            "attachmentSet": [
                {"attachmentId": a.get("attachmentId"), "fileName": a.get("fileName")}
                for a in item.get("attachmentSet", []) or []
            ],
        }
        for _, item in indexed
    ]


def author_label(submitted_by: object) -> str:
    """`AWS Support` or `Customer`; email addresses are not passed to the model."""
    if isinstance(submitted_by, str) and AWS_SUPPORT_AUTHOR.lower() in submitted_by.lower():
        return "AWS Support"
    return "Customer"


def render_case_annotation(communications: Sequence[Mapping[str, Any]]) -> str:
    """Render a stored (chronological) conversation as the summary prompt's `case_annotation`."""
    parts: list[str] = []
    for number, item in enumerate(communications, start=1):
        attachments = len(item.get("attachmentSet") or [])
        header = f"--- Message {number} | {item.get('timeCreated') or 'unknown time'} | "
        header += author_label(item.get("submittedBy"))
        if attachments:
            header += f" | {attachments} attachment(s)"
        parts.append(f"{header} ---\n{str(item.get('body') or '').strip()}")
    return "\n\n".join(parts)


def check_annotation_size(text: str, limit: int = MAX_CASE_ANNOTATION_CHARS) -> int:
    """Return the character count of `text`, raising `ConversationTooLargeError` above `limit`."""
    chars = len(text)
    if chars > limit:
        raise ConversationTooLargeError(chars, limit)
    return chars


def build_conversation_document(  # noqa: PLR0913
    *,
    account_id: str,
    case_id: str,
    display_id: str,
    run_id: str,
    retrieved_at: datetime,
    communications: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The raw `communications.json` document holding the complete conversation."""
    ordered = chronological(communications)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "accountId": account_id,
        "caseId": case_id,
        "displayId": display_id,
        "runId": run_id,
        "retrievedAt": utc_timestamp(retrieved_at),
        "communicationCount": len(ordered),
        "communications": ordered,
    }


def utc_timestamp(moment: datetime) -> str:
    """ISO 8601 UTC timestamp to the second, e.g. `2026-11-13T00:05:12Z`."""
    if moment.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

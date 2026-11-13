"""Workflow failure messages and terminal failure markers.

Failure messages go to the terminal workflow failure queue (`CaseInsights-WorkflowFailures-<id>`)
from three places: Case Annotation, for a conversation too large to summarise; the workflow's
failure handler, for a case-analysis attempt whose task retries are exhausted; and failure-delivery
reconciliation (see `common.failure_delivery`), for a closed attempt the handler never recorded.
All carry the same case reference fields as a summary-queue message, so an operator can redrive
any of them to the summary queue; they never carry case content or error text.

A conversation that is too large fails the same way every time until its content changes, so
annotation records `terminal-failure.json` next to the case with a digest of the rendered
conversation and reports it again only when the content changes (or the marker expires with the
raw bucket's retention). Nothing retries it automatically.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from common.case_refs import SummaryRef
from common.processed import case_record_key

FAILURE_SCHEMA_VERSION: Final = 1
CONVERSATION_TOO_LARGE: Final = "ConversationTooLarge"
WORKFLOW_TASK_FAILED: Final = "WorkflowTaskFailed"
INVALID_WORKFLOW_INPUT: Final = "InvalidWorkflowInput"
# Recorded by reconciliation for a closed execution whose failure handler never confirmed a
# record, for example a whole-execution timeout or abort, which bypasses every task Catch.
WORKFLOW_EXECUTION_CLOSED: Final = "WorkflowExecutionClosed"
TERMINAL_MARKER_FILE: Final = "terminal-failure.json"
TERMINAL_MARKER_SCHEMA_VERSION: Final = 1
MAX_TERMINAL_MARKER_BYTES: Final = 16 * 1024
_SUMMARY_FIELDS: Final = (
    "accountId",
    "caseId",
    "displayId",
    "runId",
    "rawKey",
    "communicationsKey",
    "communicationsVersionId",
    "communicationCount",
)


def failure_message(
    ref: SummaryRef,
    *,
    failure_type: str,
    stage: str,
    failed_at: str,
    retryable: bool,
    **details: Any,  # noqa: ANN401
) -> dict[str, Any]:
    """A failure-queue message: the case reference fields plus safe failure details."""
    message = {
        "schemaVersion": FAILURE_SCHEMA_VERSION,
        "failureType": failure_type,
        "stage": stage,
        "retryable": retryable,
        "failedAt": failed_at,
        **{k: v for k, v in ref.to_message().items() if k != "redriveId"},
    }
    message.update({k: v for k, v in details.items() if v is not None})
    return message


def summary_ref_from_failure(message: Mapping[str, Any]) -> SummaryRef:
    """The summary-queue reference carried by a failure message (for operator redrive)."""
    return SummaryRef.parse({field: message.get(field) for field in _SUMMARY_FIELDS})


def terminal_marker_key(account_id: str, display_id: str) -> str:
    """Return `account_number=<id>/case_number=<displayId>/terminal-failure.json`."""
    prefix = case_record_key(account_id, display_id).rsplit("/", 1)[0]
    return f"{prefix}/{TERMINAL_MARKER_FILE}"


@dataclass(frozen=True, slots=True)
class TerminalMarker:
    """A reported terminal failure for one version of a case's content."""

    failure_type: str
    content_digest: str
    reported_at: str
    run_id: str

    def to_document(self) -> dict[str, Any]:
        return {
            "schemaVersion": TERMINAL_MARKER_SCHEMA_VERSION,
            "failureType": self.failure_type,
            "contentDigest": self.content_digest,
            "reportedAt": self.reported_at,
            "runId": self.run_id,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> TerminalMarker | None:
        """Parse a stored marker; `None` when malformed (treated as never reported)."""
        if document.get("schemaVersion") != TERMINAL_MARKER_SCHEMA_VERSION:
            return None
        fields = ("failureType", "contentDigest", "reportedAt", "runId")
        if not all(isinstance(document.get(name), str) for name in fields):
            return None
        return cls(
            failure_type=document["failureType"],
            content_digest=document["contentDigest"],
            reported_at=document["reportedAt"],
            run_id=document["runId"],
        )

    def matches(self, failure_type: str, content_digest: str) -> bool:
        return (self.failure_type, self.content_digest) == (failure_type, content_digest)

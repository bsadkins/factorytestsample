"""Tool errors with stable codes and client-safe payloads.

Messages and details never contain SQL text, argument values, case content, prompts or model
replies, so they are safe to log and to return. The one exception is `athenaReason` on failed
queries: Athena's explanation of why the caller's own query failed is returned to the caller (it
helps correct the SQL) but is never logged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class ToolError(Exception):
    """A controlled tool failure. `retryable` tells the client whether repeating may succeed."""

    code: str = "TOOL_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.message = message
        self.retryable = retryable
        self.details: dict[str, Any] = dict(details or {})

    def to_payload(self) -> dict[str, Any]:
        """JSON-serialisable error body for the protocol layer."""
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details:
            payload["details"] = self.details
        return payload


class TimeBudgetExceededError(ToolError):
    """Too little of the request's time budget remains to start the next AWS call safely."""

    code = "TIME_BUDGET_EXCEEDED"

    def __init__(
        self,
        message: str = "Not enough time remains in this request to complete the call; retry",
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, retryable=True, details=details)


class UnknownToolError(ToolError):
    """The requested tool name is not one of the registered tools."""

    code = "UNKNOWN_TOOL"

    def __init__(self) -> None:
        super().__init__("Unknown tool")


class ToolInputError(ToolError):
    """Arguments failed validation; nothing was sent to Athena or Bedrock.

    `errors` lists `{path, message}` entries naming the offending argument and the violated
    constraint, never the supplied value.
    """

    code = "INVALID_ARGUMENTS"

    def __init__(self, errors: Sequence[Mapping[str, str]], message: str | None = None) -> None:
        self.errors = [dict(error) for error in errors]
        super().__init__(message or "Invalid tool arguments", details={"errors": self.errors})

    def to_payload(self) -> dict[str, Any]:
        payload = super().to_payload()
        payload["errors"] = self.errors
        return payload


class SqlRejectedError(ToolInputError):
    """`query_athena` SQL is not an allowed read-only statement. `reason` is a stable code."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(
            [{"path": "sql", "message": message, "reason": reason}],
            message=f"SQL rejected: {message}",
        )

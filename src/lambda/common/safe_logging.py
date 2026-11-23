"""Helpers that keep case content, prompts, model replies and credentials out of logs.

AWS error messages and exception strings can echo request content, so log records built here
contain only the exception type, the AWS error code and the HTTP status.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError


def aws_error_code(exc: BaseException) -> str | None:
    """Return the AWS error code of a botocore `ClientError`, otherwise `None`."""
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code")
        return str(code) if code else None
    return None


def http_status(exc: BaseException) -> int | None:
    """Return the HTTP status code of a botocore `ClientError`, otherwise `None`."""
    if isinstance(exc, ClientError):
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return int(status) if isinstance(status, int) else None
    return None


def error_fields(exc: BaseException) -> dict[str, Any]:
    """Describe `exc` for a log record without its message.

    >>> error_fields(ValueError("secret text"))
    {'error_type': 'ValueError'}
    """
    fields: dict[str, Any] = {"error_type": type(exc).__name__}
    code = aws_error_code(exc)
    if code is not None:
        fields["error_code"] = code
    status = http_status(exc)
    if status is not None:
        fields["http_status"] = status
    return fields

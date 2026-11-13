"""JSON-RPC 2.0 message classification and response builders for the MCP server.

Messages follow the MCP rules on top of JSON-RPC 2.0: one message per HTTP body (no batches),
request IDs are strings or integers and never null, `params` is an object, and envelopes carry
no members beyond those JSON-RPC defines. Error messages are fixed strings; they never echo
method names, IDs, parameters or any other client-supplied value.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
INTERNAL_ERROR: Final = -32603

_REQUEST_MEMBERS: Final = frozenset({"jsonrpc", "id", "method", "params"})
_NOTIFICATION_MEMBERS: Final = frozenset({"jsonrpc", "method", "params"})
_RESULT_MEMBERS: Final = frozenset({"jsonrpc", "id", "result"})
_ERROR_MEMBERS: Final = frozenset({"jsonrpc", "id", "error"})

RequestId = str | int


def is_request_id(value: object) -> bool:
    """MCP request IDs are strings or integers (JSON booleans are not integers)."""
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


class InvalidMessage(Exception):  # noqa: N818
    """The body is not an acceptable JSON-RPC message. `request_id` is the echoable ID, if any."""

    def __init__(self, message: str, request_id: RequestId | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.request_id = request_id


@dataclass(frozen=True, slots=True)
class Message:
    """A classified JSON-RPC message.

    `kind` is `request` (has an ID and expects a response), `notification` (no ID, no response)
    or `response` (a client's reply to a server request; accepted and ignored).
    """

    kind: Literal["request", "notification", "response"]
    method: str | None
    request_id: RequestId | None
    params: object
    has_params: bool


def classify(document: object) -> Message:
    """Validate the envelope of a decoded body; raise `InvalidMessage` when it is not valid."""
    if isinstance(document, list):
        raise InvalidMessage("Batch requests are not supported")
    if not isinstance(document, dict):
        raise InvalidMessage("Request must be a JSON object")
    raw_id = document.get("id")
    request_id = raw_id if is_request_id(raw_id) else None
    if document.get("jsonrpc") != "2.0":
        raise InvalidMessage('jsonrpc must be "2.0"', request_id)
    if "method" in document:
        return _classify_call(document, request_id)
    return _classify_response(document, request_id)


def _classify_call(document: dict[str, Any], request_id: RequestId | None) -> Message:
    members = frozenset(document)
    if "id" in document:
        if members - _REQUEST_MEMBERS:
            raise InvalidMessage("Request has unsupported members", request_id)
        if request_id is None:
            raise InvalidMessage("id must be a string or an integer")
        kind: Literal["request", "notification"] = "request"
    else:
        if members - _NOTIFICATION_MEMBERS:
            raise InvalidMessage("Notification has unsupported members")
        kind = "notification"
    method = document["method"]
    if not isinstance(method, str) or not method:
        raise InvalidMessage("method must be a non-empty string", request_id)
    return Message(
        kind=kind,
        method=method,
        request_id=request_id,
        params=document.get("params"),
        has_params="params" in document,
    )


def _classify_response(document: dict[str, Any], request_id: RequestId | None) -> Message:
    members = frozenset(document)
    if members in (_RESULT_MEMBERS, _ERROR_MEMBERS):
        if request_id is None:
            raise InvalidMessage("Response id must be a string or an integer")
        if "error" in document and not _is_error_object(document["error"]):
            raise InvalidMessage("Response error must be an error object", request_id)
        return Message(
            kind="response", method=None, request_id=request_id, params=None, has_params=False
        )
    raise InvalidMessage("Message is not a request, notification or response", request_id)


def _is_error_object(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    code = value.get("code")
    return (
        isinstance(code, int)
        and not isinstance(code, bool)
        and isinstance(value.get("message"), str)
    )


def result_response(request_id: RequestId, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def error_response(
    request_id: RequestId | None,
    code: int,
    message: str,
    data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = dict(data)
    return {"jsonrpc": "2.0", "id": request_id, "error": error}

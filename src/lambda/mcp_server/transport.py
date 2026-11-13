"""HTTP transport for the MCP endpoint behind an API Gateway REST proxy integration.

This is the stateless subset of MCP Streamable HTTP: the client POSTs one JSON-RPC message per
request and receives either one `application/json` response or, for accepted notifications and
client responses, `202 Accepted` with no body. No SSE stream is offered, so GET (like every
other non-POST method) is `405 Method Not Allowed`.

Checks run before any parsing or dispatch, in this order: method, ambiguous repeated headers,
`Origin` (browsers are never allowed; CORS is disabled), `Content-Type`, `Accept`, then the body
size. The 32 KiB limit applies to the request's actual bytes: the UTF-8 encoding of a text body,
or the decoded bytes of a base64 body, so multibyte characters cannot slip past a character
count. Oversized base64 is refused from its encoded length without being decoded.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from mcp_server.jsonrpc import INVALID_REQUEST, PARSE_ERROR, InvalidMessage, Message, classify
from mcp_server.server import Outcome, protocol_error

MAX_BODY_BYTES: Final = 32 * 1024
# Longest unpadded-or-padded base64 text whose decoded size can still be within the limit.
MAX_BASE64_CHARS: Final = 4 * ((MAX_BODY_BYTES + 2) // 3)
PROTOCOL_VERSION_HEADER: Final = "mcp-protocol-version"
_SINGLE_VALUED: Final = ("content-type", "accept", "origin", PROTOCOL_VERSION_HEADER)
_ACCEPTABLE_MEDIA_RANGES: Final = frozenset({"application/json", "application/*", "*/*"})
_UTF8_CHARSETS: Final = frozenset({"utf-8", "utf8"})
_BASE_HEADERS: Final = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


class Rejected(Exception):  # noqa: N818
    """The HTTP request was refused before dispatch; `outcome` is the reply."""

    def __init__(self, outcome: Outcome) -> None:
        super().__init__(outcome.error_code)
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class McpHttpRequest:
    message: Message
    protocol_header: str | None


def _reject(status: int, code: int, message: str) -> Rejected:
    return Rejected(protocol_error(status, None, code, message))


def _headers(event: Mapping[str, Any]) -> dict[str, list[str]]:
    """Lower-cased header names to their values. API Gateway fills `multiValueHeaders` with
    every value and `headers` with the last one, so the former wins when present."""
    source = event.get("multiValueHeaders")
    multi = isinstance(source, dict)
    if not multi:
        source = event.get("headers")
    headers: dict[str, list[str]] = {}
    if not isinstance(source, dict):
        return headers
    for name, value in source.items():
        values = value if multi and isinstance(value, list) else [value]
        headers.setdefault(str(name).lower(), []).extend(str(item) for item in values)
    return headers


def _media_type(value: str) -> tuple[str, dict[str, str]]:
    kind, *raw_params = value.split(";")
    params: dict[str, str] = {}
    for raw in raw_params:
        key, _, param_value = raw.partition("=")
        params[key.strip().lower()] = param_value.strip().strip('"').lower()
    return kind.strip().lower(), params


def _check_headers(headers: Mapping[str, list[str]]) -> str | None:
    for name in _SINGLE_VALUED:
        if len(headers.get(name, [])) > 1:
            raise _reject(400, INVALID_REQUEST, "Repeated header is not allowed")
    if "origin" in headers:
        raise _reject(403, INVALID_REQUEST, "Cross-origin requests are not allowed")
    content_type = headers.get("content-type")
    if not content_type:
        raise _reject(415, INVALID_REQUEST, "Content-Type must be application/json")
    kind, params = _media_type(content_type[0])
    if kind != "application/json" or params.get("charset", "utf-8") not in _UTF8_CHARSETS:
        raise _reject(415, INVALID_REQUEST, "Content-Type must be application/json")
    accept = headers.get("accept")
    if accept and not any(
        _media_type(item)[0] in _ACCEPTABLE_MEDIA_RANGES for item in accept[0].split(",")
    ):
        raise _reject(406, INVALID_REQUEST, "Accept must allow application/json")
    versions = headers.get(PROTOCOL_VERSION_HEADER)
    return versions[0].strip() if versions else None


def body_bytes(event: Mapping[str, Any]) -> bytes:
    """The request body's bytes, refusing anything over `MAX_BODY_BYTES` before decoding it."""
    body = event.get("body")
    if body is None:
        return b""
    if not isinstance(body, str):
        raise _reject(400, PARSE_ERROR, "Parse error")
    if event.get("isBase64Encoded") is True:
        if len(body) > MAX_BASE64_CHARS:
            raise _reject(413, INVALID_REQUEST, "Request body exceeds 32768 bytes")
        try:
            data = base64.b64decode(body, validate=True)
        except (binascii.Error, ValueError):
            raise _reject(400, PARSE_ERROR, "Parse error") from None
    else:
        # Every character encodes to at least one byte, so a long string is refused unencoded.
        if len(body) > MAX_BODY_BYTES:
            raise _reject(413, INVALID_REQUEST, "Request body exceeds 32768 bytes")
        try:
            data = body.encode("utf-8")
        except UnicodeEncodeError:
            raise _reject(400, PARSE_ERROR, "Parse error") from None
    if len(data) > MAX_BODY_BYTES:
        raise _reject(413, INVALID_REQUEST, "Request body exceeds 32768 bytes")
    return data


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document = dict(pairs)
    if len(document) != len(pairs):
        raise ValueError("duplicate object key")
    return document


def _reject_constant(_: str) -> Any:  # noqa: ANN401
    raise ValueError("non-standard JSON constant")


def decode_json(data: bytes) -> object:
    """Strict UTF-8 JSON: no duplicate keys, NaN or Infinity; nesting is bounded by recursion."""
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _reject(400, PARSE_ERROR, "Parse error") from None


def read_request(event: object) -> McpHttpRequest:
    """Validate the API Gateway event and classify its JSON-RPC message; raise `Rejected`."""
    if not isinstance(event, dict):
        raise _reject(400, INVALID_REQUEST, "Invalid request")
    if event.get("httpMethod") != "POST":
        rejected = _reject(405, INVALID_REQUEST, "Method not allowed")
        rejected.outcome.method = "http"
        raise rejected
    protocol_header = _check_headers(_headers(event))
    document = decode_json(body_bytes(event))
    try:
        message = classify(document)
    except InvalidMessage as exc:
        raise Rejected(protocol_error(400, exc.request_id, INVALID_REQUEST, exc.message)) from None
    return McpHttpRequest(message=message, protocol_header=protocol_header)


def http_response(outcome: Outcome) -> dict[str, Any]:
    """API Gateway proxy response for `outcome`."""
    headers = dict(_BASE_HEADERS)
    if outcome.status == 405:  # noqa: PLR2004
        headers["Allow"] = "POST"
    body = ""
    if outcome.response is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(outcome.response, ensure_ascii=False, separators=(",", ":"))
    return {
        "statusCode": outcome.status,
        "headers": headers,
        "body": body,
        "isBase64Encoded": False,
    }

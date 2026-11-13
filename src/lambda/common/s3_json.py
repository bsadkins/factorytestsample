"""Bounded, deterministic JSON reads and writes on S3."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, NoReturn

from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

from common.safe_logging import aws_error_code, error_fields, http_status

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

logger = Logger(child=True)

DEFAULT_MAX_BYTES: Final = 20 * 1024 * 1024
_NOT_FOUND_CODES: Final = frozenset({"NoSuchKey", "404", "NotFound"})
_PRECONDITION_CODES: Final = frozenset({"PreconditionFailed", "ConditionalRequestConflict"})
_HTTP_NOT_FOUND: Final = 404
_HTTP_PRECONDITION: Final = frozenset({409, 412})


class S3JsonError(Exception):
    """Base error for S3 JSON helpers. Messages contain the key, never object content."""


class S3ObjectNotFoundError(S3JsonError):
    """The object does not exist."""


class S3ObjectTooLargeError(S3JsonError):
    """The object or payload exceeds the configured byte limit."""


class S3JsonDecodeError(S3JsonError):
    """The object is not valid UTF-8 JSON."""


class S3PreconditionFailedError(S3JsonError):
    """A conditional write lost: the object already exists or has changed."""


@dataclass(frozen=True, slots=True)
class WriteResult:
    """Identity of a written object."""

    etag: str
    version_id: str | None


def dumps_json(data: object) -> bytes:
    """Serialise `data` as compact, key-sorted UTF-8 JSON; NaN and infinity are rejected."""
    return json.dumps(
        data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"Non-finite JSON number {value}")


def _is_not_found(exc: ClientError) -> bool:
    return aws_error_code(exc) in _NOT_FOUND_CODES or http_status(exc) == _HTTP_NOT_FOUND


def read_json(
    s3: S3Client,
    bucket: str,
    key: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    version_id: str | None = None,
) -> object:
    """Read and parse a JSON document of at most `max_bytes` bytes."""
    request: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if version_id is not None:
        request["VersionId"] = version_id
    try:
        response = s3.get_object(**request)
    except ClientError as exc:
        if _is_not_found(exc):
            raise S3ObjectNotFoundError(f"s3://{bucket}/{key} does not exist") from None
        logger.warning("S3 read failed", extra={"bucket": bucket, "key": key, **error_fields(exc)})
        raise
    body = response["Body"]
    try:
        if response.get("ContentLength", 0) > max_bytes:
            raise S3ObjectTooLargeError(f"s3://{bucket}/{key} exceeds {max_bytes} bytes")
        raw = body.read(max_bytes + 1)
    finally:
        body.close()
    if len(raw) > max_bytes:
        raise S3ObjectTooLargeError(f"s3://{bucket}/{key} exceeds {max_bytes} bytes")
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError):
        raise S3JsonDecodeError(f"s3://{bucket}/{key} is not valid UTF-8 JSON") from None


def read_json_object(
    s3: S3Client,
    bucket: str,
    key: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    version_id: str | None = None,
) -> dict[str, Any]:
    """Like `read_json` but require the document to be a JSON object."""
    data = read_json(s3, bucket, key, max_bytes=max_bytes, version_id=version_id)
    if not isinstance(data, dict):
        raise S3JsonDecodeError(f"s3://{bucket}/{key} does not contain a JSON object")
    return data


def read_json_if_exists(
    s3: S3Client, bucket: str, key: str, *, max_bytes: int = DEFAULT_MAX_BYTES
) -> dict[str, Any] | None:
    """Like `read_json_object` but return `None` when the object does not exist."""
    try:
        return read_json_object(s3, bucket, key, max_bytes=max_bytes)
    except S3ObjectNotFoundError:
        return None


def write_json(  # noqa: PLR0913
    s3: S3Client,
    bucket: str,
    key: str,
    data: object,
    *,
    if_none_match: bool = False,
    if_match: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> WriteResult:
    """Write `data` as JSON with SSE-S3.

    `if_none_match=True` creates the object only if no current version exists (S3 conditional
    write); `if_match` overwrites only the given ETag. A lost condition raises
    `S3PreconditionFailedError` and leaves the existing object untouched.
    """
    body = dumps_json(data)
    if len(body) > max_bytes:
        raise S3ObjectTooLargeError(f"Payload for s3://{bucket}/{key} exceeds {max_bytes} bytes")
    request: dict[str, Any] = {
        "Bucket": bucket,
        "Key": key,
        "Body": body,
        "ContentType": "application/json",
        "ServerSideEncryption": "AES256",
    }
    if if_none_match:
        request["IfNoneMatch"] = "*"
    if if_match is not None:
        request["IfMatch"] = if_match
    try:
        response = s3.put_object(**request)
    except ClientError as exc:
        if aws_error_code(exc) in _PRECONDITION_CODES or http_status(exc) in _HTTP_PRECONDITION:
            raise S3PreconditionFailedError(
                f"Conditional write lost for s3://{bucket}/{key}"
            ) from None
        logger.warning("S3 write failed", extra={"bucket": bucket, "key": key, **error_fields(exc)})
        raise
    return WriteResult(etag=response.get("ETag", ""), version_id=response.get("VersionId"))


def object_exists(s3: S3Client, bucket: str, key: str) -> bool:
    """Return True when a current version of `key` exists."""
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if _is_not_found(exc):
            return False
        raise
    return True

"""S3 JSON helpers using moto."""

import logging

import boto3
import pytest
from common.s3_json import (
    S3JsonDecodeError,
    S3ObjectNotFoundError,
    S3ObjectTooLargeError,
    S3PreconditionFailedError,
    dumps_json,
    object_exists,
    read_json,
    read_json_if_exists,
    read_json_object,
    write_json,
)
from types_boto3_s3.client import S3Client


def s3() -> S3Client:
    return boto3.client("s3", region_name="us-east-1")


def test_round_trip_is_deterministic_utf8_with_sse(bucket: str) -> None:
    client = s3()
    result = write_json(client, bucket, "a/b.json", {"z": 1, "a": "naïve"})
    assert result.etag
    head = client.head_object(Bucket=bucket, Key="a/b.json")
    assert head["ContentType"] == "application/json"
    assert head["ServerSideEncryption"] == "AES256"
    body = client.get_object(Bucket=bucket, Key="a/b.json")["Body"].read()
    assert body == '{"a":"naïve","z":1}'.encode()
    assert read_json(client, bucket, "a/b.json") == {"z": 1, "a": "naïve"}


def test_missing_object(bucket: str) -> None:
    client = s3()
    with pytest.raises(S3ObjectNotFoundError):
        read_json(client, bucket, "missing.json")
    assert read_json_if_exists(client, bucket, "missing.json") is None
    assert object_exists(client, bucket, "missing.json") is False
    write_json(client, bucket, "present.json", [])
    assert object_exists(client, bucket, "present.json") is True
    assert read_json(client, bucket, "present.json") == []
    with pytest.raises(S3JsonDecodeError):
        read_json_object(client, bucket, "present.json")
    write_json(client, bucket, "obj.json", {"k": "v"})
    assert read_json_if_exists(client, bucket, "obj.json") == {"k": "v"}


def test_conditional_create_does_not_overwrite(bucket: str) -> None:
    client = s3()
    write_json(client, bucket, "done.json", {"v": 1}, if_none_match=True)
    with pytest.raises(S3PreconditionFailedError):
        write_json(client, bucket, "done.json", {"v": 2}, if_none_match=True)
    assert read_json(client, bucket, "done.json") == {"v": 1}


def test_malformed_json_raises_without_echoing_content(bucket: str) -> None:
    client = s3()
    client.put_object(Bucket=bucket, Key="bad.json", Body=b'{"SENTINEL-CASE-TEXT-7f3a": ')
    with pytest.raises(S3JsonDecodeError) as info:
        read_json(client, bucket, "bad.json")
    assert "SENTINEL" not in str(info.value)


def test_non_utf8_payload_raises_decode_error(bucket: str) -> None:
    client = s3()
    client.put_object(Bucket=bucket, Key="latin.json", Body=b'"\xff"')
    with pytest.raises(S3JsonDecodeError):
        read_json(client, bucket, "latin.json")


def test_size_limits_apply_to_reads_and_writes(bucket: str) -> None:
    client = s3()
    write_json(client, bucket, "big.json", {"x": "y" * 100})
    with pytest.raises(S3ObjectTooLargeError):
        read_json(client, bucket, "big.json", max_bytes=50)
    with pytest.raises(S3ObjectTooLargeError):
        write_json(client, bucket, "too-big.json", {"x": "y" * 100}, max_bytes=50)
    assert object_exists(client, bucket, "too-big.json") is False


def test_nan_is_never_serialised() -> None:
    with pytest.raises(ValueError, match="Out of range"):
        dumps_json({"x": float("nan")})


def test_logs_do_not_contain_payloads(bucket: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    client = s3()
    write_json(client, bucket, "s.json", {"text": "SENTINEL-CASE-TEXT-7f3a"})
    read_json(client, bucket, "s.json")
    assert all("SENTINEL" not in repr(r.__dict__) for r in caplog.records)

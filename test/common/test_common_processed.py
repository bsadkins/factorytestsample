"""Processed record keys and get_existing_cases_batch."""

from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from common.processed import (
    case_partition_prefix,
    case_record_key,
    get_existing_cases_batch,
    parse_case_record_key,
)


def test_record_key_matches_the_partition_layout() -> None:
    assert (
        case_record_key("123456789012", "1712345678")
        == "account_number=123456789012/case_number=1712345678/data.json"
    )
    assert case_partition_prefix("123456789012") == "account_number=123456789012/"
    assert parse_case_record_key("account_number=123456789012/case_number=17/data.json") == (
        "123456789012",
        "17",
    )
    assert parse_case_record_key("account_number=123456789012/case_number=17/other.json") is None
    assert parse_case_record_key("runs/x.json") is None


@pytest.mark.parametrize(
    ("account", "case"),
    [("12345", "1"), ("123456789012", "a/b"), ("123456789012", ""), ("123456789012", "..")],
)
def test_invalid_identifiers_are_rejected(account: str, case: str) -> None:
    with pytest.raises(ValueError, match="invalid"):
        case_record_key(account, case)


def test_existing_cases_are_found_across_pages(bucket: str) -> None:
    client = boto3.client("s3", region_name="us-east-1")
    for case in ("100", "101", "205"):
        client.put_object(Bucket=bucket, Key=case_record_key("111111111111", case), Body=b"{}")
    client.put_object(
        Bucket=bucket, Key="account_number=111111111111/case_number=300/partial.json", Body=b"{}"
    )
    client.put_object(Bucket=bucket, Key=case_record_key("222222222222", "100"), Body=b"{}")

    found = get_existing_cases_batch(
        client, bucket, "111111111111", ["100", "205", "300", "999"], page_size=1
    )
    assert found == {"100", "205"}
    assert get_existing_cases_batch(client, bucket, "111111111111") == {"100", "101", "205"}


def test_empty_request_makes_no_calls() -> None:
    client: Any = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y"
    )
    with Stubber(client):
        assert get_existing_cases_batch(client, "b", "111111111111", []) == set()


def test_listing_is_scoped_to_the_account_partition() -> None:
    client: Any = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y"
    )
    with Stubber(client) as stub:
        stub.add_response(
            "list_objects_v2",
            {
                "Contents": [{"Key": case_record_key("111111111111", "7")}],
                "IsTruncated": False,
                "KeyCount": 1,
            },
            {"Bucket": "b", "Prefix": "account_number=111111111111/case_number=", "MaxKeys": 1000},
        )
        assert get_existing_cases_batch(client, "b", "111111111111", {"7", "8"}) == {"7"}

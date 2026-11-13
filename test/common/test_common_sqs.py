"""SQS batching helpers."""

import json
from typing import Any

import boto3
import pytest
from botocore.stub import ANY, Stubber
from common.retry import RetryPolicy
from common.sqs import (
    MAX_MESSAGE_BYTES,
    InvalidMessageError,
    SqsSendError,
    encode_body,
    parse_message_body,
    send_messages,
)
from types_boto3_sqs.client import SQSClient

FAST = RetryPolicy(max_attempts=3, base_delay_seconds=0.0, max_delay_seconds=0.0)
QUEUE = "https://sqs.us-east-1.amazonaws.com/111111111111/q"


def stub_client() -> SQSClient:
    return boto3.client(
        "sqs", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y"
    )


def test_sends_in_batches_of_ten_with_moto(mocked_aws: None) -> None:
    client = boto3.client("sqs", region_name="us-east-1")
    url = client.create_queue(QueueName="refs")["QueueUrl"]
    bodies = [{"accountId": f"{i:012d}", "runId": "r1"} for i in range(23)]
    assert send_messages(client, url, bodies) == 23
    received: list[dict[str, Any]] = []
    while True:
        batch = client.receive_message(QueueUrl=url, MaxNumberOfMessages=10).get("Messages", [])
        if not batch:
            break
        received.extend(json.loads(m["Body"]) for m in batch)
        client.delete_message_batch(
            QueueUrl=url,
            Entries=[
                {"Id": str(i), "ReceiptHandle": m["ReceiptHandle"]} for i, m in enumerate(batch)
            ],
        )
    assert sorted(r["accountId"] for r in received) == sorted(b["accountId"] for b in bodies)


def test_empty_input_makes_no_calls() -> None:
    client = stub_client()
    with Stubber(client):
        assert send_messages(client, QUEUE, []) == 0


def test_partial_batch_failures_are_retried() -> None:
    client = stub_client()
    with Stubber(client) as stub:
        stub.add_response(
            "send_message_batch",
            {
                "Successful": [{"Id": "0", "MessageId": "a", "MD5OfMessageBody": "x"}],
                "Failed": [{"Id": "1", "SenderFault": False, "Code": "InternalError"}],
            },
            {"QueueUrl": QUEUE, "Entries": ANY},
        )
        stub.add_response(
            "send_message_batch",
            {"Successful": [{"Id": "1", "MessageId": "b", "MD5OfMessageBody": "x"}], "Failed": []},
            {"QueueUrl": QUEUE, "Entries": [{"Id": "1", "MessageBody": encode_body({"n": 2})}]},
        )
        assert (
            send_messages(client, QUEUE, [{"n": 1}, {"n": 2}], policy=FAST, sleep=lambda _: None)
            == 2
        )
        stub.assert_no_pending_responses()


def test_sender_faults_are_not_retried_and_raise() -> None:
    client = stub_client()
    with Stubber(client) as stub:
        stub.add_response(
            "send_message_batch",
            {
                "Successful": [],
                "Failed": [{"Id": "0", "SenderFault": True, "Code": "InvalidParameterValue"}],
            },
        )
        with pytest.raises(SqsSendError) as info:
            send_messages(client, QUEUE, [{"n": 1}], policy=FAST, sleep=lambda _: None)
    assert info.value.failed == 1
    assert info.value.codes == ("InvalidParameterValue",)


def test_persistent_failures_raise_after_bounded_attempts() -> None:
    client = stub_client()
    with Stubber(client) as stub:
        for _ in range(3):
            stub.add_response(
                "send_message_batch",
                {
                    "Successful": [],
                    "Failed": [{"Id": "0", "SenderFault": False, "Code": "InternalError"}],
                },
            )
        with pytest.raises(SqsSendError):
            send_messages(
                client, QUEUE, [{"n": "SENTINEL-CASE-TEXT-7f3a"}], policy=FAST, sleep=lambda _: None
            )
        stub.assert_no_pending_responses()


def test_whole_call_throttling_is_retried() -> None:
    client = stub_client()
    with Stubber(client) as stub:
        stub.add_client_error(
            "send_message_batch", service_error_code="RequestThrottled", http_status_code=400
        )
        stub.add_response(
            "send_message_batch",
            {"Successful": [{"Id": "0", "MessageId": "a", "MD5OfMessageBody": "x"}], "Failed": []},
        )
        assert send_messages(client, QUEUE, [{"n": 1}], policy=FAST, sleep=lambda _: None) == 1


def test_oversized_message_is_rejected() -> None:
    with pytest.raises(ValueError, match="too large"):
        encode_body({"x": "y" * MAX_MESSAGE_BYTES})


def test_batches_respect_total_payload_limit() -> None:
    client = stub_client()
    big = {"x": "y" * 100_000}
    body = encode_body(big)
    with Stubber(client) as stub:
        # Two 100 KB bodies fit within one 256 KiB batch; the third needs a second request.
        stub.add_response(
            "send_message_batch",
            {
                "Successful": [
                    {"Id": "0", "MessageId": "a", "MD5OfMessageBody": "x"},
                    {"Id": "1", "MessageId": "b", "MD5OfMessageBody": "x"},
                ],
                "Failed": [],
            },
            {
                "QueueUrl": QUEUE,
                "Entries": [{"Id": "0", "MessageBody": body}, {"Id": "1", "MessageBody": body}],
            },
        )
        stub.add_response(
            "send_message_batch",
            {"Successful": [{"Id": "2", "MessageId": "c", "MD5OfMessageBody": "x"}], "Failed": []},
            {"QueueUrl": QUEUE, "Entries": [{"Id": "2", "MessageBody": body}]},
        )
        sent = send_messages(client, QUEUE, [big, big, big], policy=FAST, sleep=lambda _: None)
        stub.assert_no_pending_responses()
    assert sent == 3


def test_parse_message_body() -> None:
    assert parse_message_body({"messageId": "m1", "body": '{"a": 1}'}) == {"a": 1}
    with pytest.raises(InvalidMessageError) as info:
        parse_message_body({"messageId": "m2", "body": "SENTINEL-CASE-TEXT-7f3a"})
    assert "SENTINEL" not in str(info.value)
    assert info.value.message_id == "m2"
    with pytest.raises(InvalidMessageError):
        parse_message_body({"messageId": "m3", "body": "[1]"})
    with pytest.raises(InvalidMessageError):
        parse_message_body({"messageId": "m4"})

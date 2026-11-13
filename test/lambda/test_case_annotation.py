"""Case Annotation handler: complete DescribeCommunications paging into S3 and summary hand-off."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import boto3
import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError
from case_annotation import app
from common.case_refs import AnnotationRef
from common.conversation import MAX_CASE_ANNOTATION_CHARS
from common.failures import summary_ref_from_failure, terminal_marker_key
from common.idempotency import persistence_layer
from common.s3_json import WriteResult, write_json

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client
    from types_boto3_sqs.client import SQSClient

RAW = "raw-test"
PROCESSED = "processed-test"
TABLE = "idempotency-test"
ROLE_NAME = "Support-Case-Analysis-Role-test"
RUN = "20261113T000000Z"
NEXT_RUN = "20261114T000000Z"
A, B = "111111111111", "222222222222"
SENTINEL_BODY = "SENTINEL-BODY-5d1e"
SENTINEL_PERSON = "sentinel-person@example.com"
NOW = datetime(2026, 11, 13, 0, 7, 0, tzinfo=UTC)


class Kit(Protocol):
    sts: Any
    support: Any
    event: Callable[..., dict[str, Any]]
    drain: Callable[[Any, str], list[dict[str, Any]]]
    create_table: Callable[[str], None]
    metrics: Callable[[str], dict[str, float]]

    def context(self, request_id: str = ...) -> LambdaContext: ...

    def logs(self) -> str: ...


def ref(account: str = A, display: str = "1001", run_id: str = RUN) -> AnnotationRef:
    return AnnotationRef(
        account_id=account,
        case_id=f"case-{account}-2026-{display}",
        display_id=display,
        run_id=run_id,
    )


def communication(
    case_id: str, number: int, *, body: str | None = None, aws: bool = False
) -> dict[str, Any]:
    attachments = [{"attachmentId": f"att-{number}", "fileName": "trace.txt"}]
    return {
        "caseId": case_id,
        "body": body if body is not None else f"{SENTINEL_BODY} message {number}",
        "submittedBy": "Amazon Web Services" if aws else SENTINEL_PERSON,
        "timeCreated": f"2026-06-01T10:{number:02d}:00.000Z",
        "attachmentSet": attachments if number == 2 else [],
    }


def comm_params(case_id: str, token: str | None = None) -> dict[str, Any]:
    expected: dict[str, Any] = {"caseId": case_id, "maxResults": 100}
    if token is not None:
        expected["nextToken"] = token
    return expected


def comm_key(account: str, display: str) -> str:
    return f"account_number={account}/case_number={display}/communications.json"


def raw_key(account: str, display: str) -> str:
    return f"account_number={account}/case_number={display}/data.json"


@dataclass
class Env:
    s3: S3Client
    sqs: SQSClient
    summary_url: str
    failure_url: str
    kit: Kit
    capsys: pytest.CaptureFixture[str]
    stdout: str = field(default="")

    @property
    def sts(self) -> Any:  # noqa: ANN401
        return self.kit.sts

    @property
    def support(self) -> Any:  # noqa: ANN401
        return self.kit.support

    def handle(self, *bodies: object, request_id: str = "request-1") -> Any:  # noqa: ANN401
        return app.handler(self.kit.event(*bodies), self.kit.context(request_id))

    def metrics(self) -> dict[str, float]:
        self.stdout = self.capsys.readouterr().out
        return self.kit.metrics(self.stdout)

    def summaries(self) -> list[dict[str, Any]]:
        return self.kit.drain(self.sqs, self.summary_url)

    def failures(self) -> list[dict[str, Any]]:
        return self.kit.drain(self.sqs, self.failure_url)

    def raw(self, key: str) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(
            self.s3.get_object(Bucket=RAW, Key=key)["Body"].read()
        )
        return document

    def version(self, key: str) -> str:
        return self.s3.head_object(Bucket=RAW, Key=key)["VersionId"]

    def raw_keys(self, prefix: str = "account_number=") -> list[str]:
        listing = self.s3.list_objects_v2(Bucket=RAW, Prefix=prefix)
        return sorted(o["Key"] for o in listing.get("Contents", []))

    def case_outcome(self, account: str, display: str, run_id: str = RUN) -> str | None:
        key = f"runs/{run_id}/cases/{account}/{display}.json"
        if key not in self.raw_keys("runs/"):
            return None
        outcome: str = self.raw(key)["outcome"]
        return outcome

    def mark_processed(self, account: str, display: str) -> None:
        self.s3.put_object(Bucket=PROCESSED, Key=raw_key(account, display), Body=b"{}")


@pytest.fixture
def env(
    mocked_aws: None,
    monkeypatch: pytest.MonkeyPatch,
    case_collection_kit: Kit,
    capsys: pytest.CaptureFixture[str],
) -> Iterator[Env]:
    kit = case_collection_kit
    s3 = boto3.client("s3", region_name="us-east-1")
    for name in (RAW, PROCESSED):
        s3.create_bucket(Bucket=name)
        s3.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})
    sqs = boto3.client("sqs", region_name="us-east-1")
    summary_url = sqs.create_queue(QueueName="summary")["QueueUrl"]
    failure_url = sqs.create_queue(QueueName="workflow-failures")["QueueUrl"]
    kit.create_table(TABLE)
    for key, value in {
        "RAW_BUCKET": RAW,
        "PROCESSED_BUCKET": PROCESSED,
        "SUMMARY_QUEUE_URL": summary_url,
        "FAILURE_QUEUE_URL": failure_url,
        "IDEMPOTENCY_TABLE_NAME": TABLE,
        "MEMBER_ROLE_NAME": ROLE_NAME,
        "MEMBER_ROLE_PARTITION": "aws",
    }.items():
        monkeypatch.setenv(key, value)
    clients = app.Clients(s3=s3, sqs=sqs, sts=kit.sts)
    monkeypatch.setattr(app, "clients", lambda: clients)
    monkeypatch.setattr(app, "support_client", kit.support.factory)
    monkeypatch.setattr(app, "now", lambda: NOW)
    persistence_layer.cache_clear()
    yield Env(s3, sqs, summary_url, failure_url, kit, capsys)
    persistence_layer.cache_clear()


def stub_pages(env: Env, account: str, case_id: str, pages: list[list[dict[str, Any]]]) -> None:
    stub = env.support.stub(account)
    for index, page in enumerate(pages):
        response: dict[str, Any] = {"communications": page}
        if index < len(pages) - 1:
            response["nextToken"] = f"c{index + 1}"
        token = f"c{index}" if index else None
        stub.add_response("describe_communications", response, comm_params(case_id, token))


def test_stores_every_communication_page_and_queues_a_summary_reference(env: Env) -> None:
    target = ref()
    # DescribeCommunications returns the newest communications first.
    newest_first = [communication(target.case_id, n, aws=n % 2 == 0) for n in range(5, 0, -1)]
    stub_pages(env, A, target.case_id, [newest_first[:2], newest_first[2:4], newest_first[4:]])

    response = env.handle(target.to_message())

    assert response == {"batchItemFailures": []}
    assert env.sts.calls == [
        {
            "RoleArn": f"arn:aws:iam::{A}:role/{ROLE_NAME}",
            "RoleSessionName": f"case-annotation-{RUN}",
            "DurationSeconds": 900,
        }
    ]
    key = comm_key(A, "1001")
    assert env.raw(key) == {
        "schemaVersion": 1,
        "accountId": A,
        "caseId": target.case_id,
        "displayId": "1001",
        "runId": RUN,
        "retrievedAt": "2026-11-13T00:07:00Z",
        "communicationCount": 5,
        "communications": [
            {
                "body": c["body"],
                "submittedBy": c["submittedBy"],
                "timeCreated": c["timeCreated"],
                "attachmentSet": c["attachmentSet"],
            }
            for c in reversed(newest_first)
        ],
    }
    assert env.summaries() == [
        {
            "accountId": A,
            "caseId": target.case_id,
            "displayId": "1001",
            "runId": RUN,
            "rawKey": raw_key(A, "1001"),
            "communicationsKey": key,
            "communicationsVersionId": env.version(key),
            "communicationCount": 5,
        }
    ]
    assert env.failures() == []
    # A queued summary is handed to the workflow starter, which records the case's outcome.
    assert env.raw_keys("runs/") == []
    assert env.metrics() == {"CasesAnnotated": 1, "CommunicationsRetrieved": 5}
    logs = env.kit.logs()
    assert "Case conversation stored and queued for summary" in logs
    assert SENTINEL_BODY not in env.stdout + logs
    assert SENTINEL_PERSON not in env.stdout + logs


def test_oversized_conversation_is_stored_in_full_and_fails_explicitly(env: Env) -> None:
    target = ref(display="2001")
    filler = "x" * 2_600
    pages = [
        [communication(target.case_id, n, body=f"{page}-{n}-{filler}") for n in range(50)]
        for page in range(3)
    ]
    stub_pages(env, A, target.case_id, pages)

    response = env.handle(target.to_message())

    assert response == {"batchItemFailures": []}
    key = comm_key(A, "2001")
    stored = env.raw(key)
    assert stored["communicationCount"] == 150
    assert sorted(c["body"] for c in stored["communications"]) == sorted(
        c["body"] for page in pages for c in page
    )
    assert env.summaries() == []
    failures = env.failures()
    assert len(failures) == 1
    failure = failures[0]
    assert failure["annotationChars"] > MAX_CASE_ANNOTATION_CHARS
    assert {k: v for k, v in failure.items() if k != "annotationChars"} == {
        "schemaVersion": 1,
        "failureType": "ConversationTooLarge",
        "stage": "annotation",
        "retryable": False,
        "accountId": A,
        "caseId": target.case_id,
        "displayId": "2001",
        "runId": RUN,
        "rawKey": raw_key(A, "2001"),
        "communicationsKey": key,
        "communicationsVersionId": env.version(key),
        "communicationCount": 150,
        "limitChars": MAX_CASE_ANNOTATION_CHARS,
        "failedAt": "2026-11-13T00:07:00Z",
    }
    assert filler not in json.dumps(failure)
    assert env.metrics() == {"OversizedConversations": 1}
    assert env.case_outcome(A, "2001") == "oversized"
    marker = env.raw(terminal_marker_key(A, "2001"))
    assert marker["failureType"] == "ConversationTooLarge"
    assert marker["runId"] == RUN
    assert filler not in json.dumps(marker)
    # An operator can redrive the failure to the summary queue as a valid reference.
    redriven = summary_ref_from_failure(failure)
    assert redriven.communications_version_id == env.version(key)
    assert redriven.raw_key == raw_key(A, "2001")


def oversized_pages(case_id: str, marker: str = "") -> list[list[dict[str, Any]]]:
    filler = "x" * 2_600
    return [
        [communication(case_id, n, body=f"{page}-{n}{marker}-{filler}") for n in range(50)]
        for page in range(3)
    ]


def test_unchanged_oversized_conversation_is_not_reported_again_on_later_runs(env: Env) -> None:
    first = ref(display="2002")
    stub_pages(env, A, first.case_id, oversized_pages(first.case_id))
    env.handle(first.to_message())
    assert len(env.failures()) == 1
    env.metrics()

    later = ref(display="2002", run_id=NEXT_RUN)
    stub_pages(env, A, later.case_id, oversized_pages(later.case_id))
    response = env.handle(later.to_message(), request_id="request-2")

    assert response == {"batchItemFailures": []}
    assert env.failures() == []
    assert env.summaries() == []
    assert env.metrics() == {"OversizedConversationsUnchanged": 1}
    assert env.case_outcome(A, "2002", NEXT_RUN) == "oversizedUnchanged"
    assert "still too large and unchanged" in env.kit.logs()
    # The terminal diagnostic stays visible with the run that reported it.
    assert env.raw(terminal_marker_key(A, "2002"))["runId"] == RUN


def test_changed_oversized_conversation_is_reported_again(env: Env) -> None:
    first = ref(display="2003")
    stub_pages(env, A, first.case_id, oversized_pages(first.case_id))
    env.handle(first.to_message())
    assert len(env.failures()) == 1

    later = ref(display="2003", run_id=NEXT_RUN)
    stub_pages(env, A, later.case_id, oversized_pages(later.case_id, marker="-follow-up"))
    env.handle(later.to_message(), request_id="request-2")

    failures = env.failures()
    assert [f["runId"] for f in failures] == [NEXT_RUN]
    assert failures[0]["retryable"] is False
    assert env.raw(terminal_marker_key(A, "2003"))["runId"] == NEXT_RUN


def test_unreadable_terminal_marker_is_treated_as_never_reported(env: Env) -> None:
    target = ref(display="2004")
    env.s3.put_object(Bucket=RAW, Key=terminal_marker_key(A, "2004"), Body=b"not json")
    stub_pages(env, A, target.case_id, oversized_pages(target.case_id))

    env.handle(target.to_message())

    assert len(env.failures()) == 1
    assert env.raw(terminal_marker_key(A, "2004"))["schemaVersion"] == 1


def test_failed_failure_report_leaves_no_marker_so_the_retry_reports_it(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = ref(display="2005")
    stub_pages(env, A, target.case_id, oversized_pages(target.case_id))

    def broken_send(*_: Any, **__: Any) -> None:  # noqa: ANN401
        raise ClientError(
            {"Error": {"Code": "ServiceUnavailable", "Message": "synthetic"}}, "SendMessage"
        )

    monkeypatch.setattr(app, "send_messages", broken_send)
    response = env.handle(target.to_message())

    assert response == {"batchItemFailures": [{"itemIdentifier": "m0"}]}
    assert terminal_marker_key(A, "2005") not in env.raw_keys()
    assert env.case_outcome(A, "2005") is None


def test_case_with_a_processed_record_is_not_annotated_again(env: Env) -> None:
    env.mark_processed(A, "1001")

    response = env.handle(ref().to_message())

    assert response == {"batchItemFailures": []}
    assert env.sts.calls == []
    assert env.summaries() == []
    assert env.raw_keys() == []
    assert env.case_outcome(A, "1001") == "alreadyProcessed"
    assert env.metrics() == {"CasesAlreadyProcessed": 1}


def test_access_entitlement_and_missing_case_errors_are_counted_skips_reevaluated_next_run(
    env: Env,
) -> None:
    env.sts.denied.add(B)
    denied, unentitled, forbidden, missing = (
        ref(B, "3001"),
        ref(A, "3002"),
        ref(A, "3003"),
        ref(A, "3004"),
    )
    stub = env.support.stub(A)
    for target, code, status in (
        (unentitled, "SubscriptionRequiredException", 400),
        (forbidden, "AccessDeniedException", 403),
        (missing, "CaseIdNotFound", 400),
    ):
        stub.add_client_error(
            "describe_communications",
            service_error_code=code,
            http_status_code=status,
            expected_params=comm_params(target.case_id),
        )

    response = env.handle(*(r.to_message() for r in (denied, unentitled, forbidden, missing)))

    assert response == {"batchItemFailures": []}
    assert env.summaries() == []
    assert env.metrics() == {
        "CasesSkippedAccessDenied": 2,
        "CasesSkippedSubscriptionRequired": 1,
        "CasesNotFound": 1,
    }
    # Each skip ends the case's work for this run, which run completion counts as settled.
    assert [
        env.case_outcome(target.account_id, target.display_id)
        for target in (denied, unentitled, forbidden, missing)
    ] == ["skippedAccessDenied", "skippedSubscriptionRequired", "skippedAccessDenied", "notFound"]

    env.sts.denied.clear()
    recovered = ref(B, "3001", NEXT_RUN)
    stub_pages(env, B, recovered.case_id, [[communication(recovered.case_id, 1)]])
    assert env.handle(recovered.to_message(), request_id="request-2") == {"batchItemFailures": []}
    assert [m["displayId"] for m in env.summaries()] == ["3001"]


def test_duplicate_reference_in_the_same_run_is_not_annotated_twice(env: Env) -> None:
    target = ref()
    stub_pages(env, A, target.case_id, [[communication(target.case_id, 1)]])
    env.handle(target.to_message())
    assert len(env.summaries()) == 1
    env.metrics()

    response = env.handle(target.to_message(), request_id="request-2")

    assert response == {"batchItemFailures": []}
    assert env.summaries() == []
    assert len(env.sts.calls) == 1
    assert env.metrics() == {"DuplicateAnnotationMessages": 1}


def test_storage_failure_fails_only_that_record_and_the_retry_runs_again(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = ref(display="4001"), ref(display="4002")
    for target in (first, second):
        stub_pages(env, A, target.case_id, [[communication(target.case_id, 1)]])

    def flaky_write(
        s3: S3Client,
        bucket: str,
        key: str,
        data: object,
        **kwargs: Any,  # noqa: ANN401
    ) -> WriteResult:
        if "4001" in key:
            raise ClientError({"Error": {"Code": "InternalError", "Message": "x"}}, "PutObject")
        return write_json(s3, bucket, key, data, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(app, "write_json", flaky_write)
        response = env.handle(first.to_message(), second.to_message())

    assert response == {"batchItemFailures": [{"itemIdentifier": "m0"}]}
    assert [m["displayId"] for m in env.summaries()] == ["4002"]
    assert env.metrics()["CaseAnnotationFailures"] == 1

    stub_pages(env, A, first.case_id, [[communication(first.case_id, 1)]])
    assert env.handle(first.to_message(), request_id="request-2") == {"batchItemFailures": []}
    assert [m["displayId"] for m in env.summaries()] == ["4001"]


def test_case_without_communications_is_still_handed_on(env: Env) -> None:
    target = ref()
    stub_pages(env, A, target.case_id, [[]])
    env.handle(target.to_message())
    assert [m["communicationCount"] for m in env.summaries()] == [0]


@pytest.mark.parametrize(
    "body",
    [
        "{oops",
        {**ref().to_message(), "rawKey": raw_key(B, "1001")},
        {**ref().to_message(), "displayId": "../1001"},
        {k: v for k, v in ref().to_message().items() if k != "caseId"},
    ],
    ids=["invalid-json", "foreign-raw-key", "unsafe-display-id", "missing-case-id"],
)
def test_malformed_references_fail_visibly(env: Env, body: object) -> None:
    response = env.handle(body)

    assert response == {"batchItemFailures": [{"itemIdentifier": "m0"}]}
    assert env.sts.calls == []
    assert env.metrics() == {"InvalidMessages": 1, "CaseAnnotationFailures": 1}

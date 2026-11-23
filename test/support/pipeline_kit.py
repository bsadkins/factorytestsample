"""Local stand-in for the deployed Core pipeline that drives the real Lambda handlers.

`Pipeline` connects every Core handler to moto S3, SQS and DynamoDB and to in-memory synthetic
backends for the services moto cannot model precisely. It never fabricates handler output:
account lists, raw objects, run markers, queue messages, ownership markers, executions,
checkpoints, failure records and processed records are all written by the handlers. The kit only
moves work between them the way the deployed services would:

- one S3 `ObjectCreated` notification per new account-list object version (the reader trigger);
- SQS event source mappings: batches of at most 10 received messages, deletion of the records a
  handler does not report in `batchItemFailures`, failed records kept in flight until their
  visibility timeout is expired explicitly, and redrive to the queue's dead-letter queue after
  `MAX_RECEIVE_COUNT` receives (moto applies the redrive policy);
- Standard CaseAnalysis executions run through the state machine definition asserted in
  `test/cdk/constructs/case-analysis.test.ts`: Summary, RCA and Lifecycle through the shared stage
  function with `{stage, case}`, then UpdateCaseMetadata with `{case}`, each result at `$.<stage>`,
  Retry on the named errors (`taskMaxAttempts` retries) and a States.ALL Catch into HandleFailure
  with `$.error`, after which the execution fails;
- Organizations, Support and Bedrock Runtime are real botocore clients (so requests and responses
  are validated against the service models) whose responses come from paginated synthetic
  backends. Step Functions is `workflow_kit.FakeStepFunctions`.

Every account, case and conversation is invented.
"""

from __future__ import annotations

import io
import json
import re
import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote_plus

import boto3
import pytest
from account_lookup import app as lookup_app
from account_reader import app as reader_app
from analysis_stages import app as stages_app
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber
from case_annotation import app as annotation_app
from case_cleanup import app as cleanup_app
from case_lifecycle import app as lifecycle_app
from case_rca import app as rca_app
from case_retrieval import app as retrieval_app
from case_summary import app as summary_app
from common import analysis
from common.analysis import AnalysisClients
from common.conversation import utc_timestamp
from common.idempotency import persistence_layer
from common.summary import check_summary
from handle_failure import app as failure_app
from run_completion import app as completion_app
from update_case_metadata import app as update_app
from workflow_kit import STATE_MACHINE_ARN, Execution, FakeStepFunctions
from workflow_starter import app as starter_app

if TYPE_CHECKING:
    from types_boto3_sqs.literals import QueueAttributeNameType

REGION: Final = "us-east-1"
MANAGEMENT: Final = "999999999999"
ROLE_NAME: Final = "Support-Case-Analysis-Role-e2e"
HAIKU: Final = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
ACCOUNT_LISTS: Final = "e2e-account-lists"
RAW: Final = "e2e-raw"
PROCESSED: Final = "e2e-processed"
IDEMPOTENCY_TABLE: Final = "e2e-idempotency"
MAX_RECEIVE_COUNT: Final = 3
# Long enough that a failed record never reappears by itself during a test.
VISIBILITY_SECONDS: Final = 900
# Defaults of operations.workflow.taskMaxAttempts and FAILURE_HANDLER_RETRY in case-analysis.ts.
TASK_RETRIES: Final = 3
FAILURE_HANDLER_RETRIES: Final = 6
BEDROCK_RETRYABLE: Final = frozenset({"BedrockThrottlingError", "BedrockTransientError"})
TRANSIENT_TASK_ERRORS: Final = frozenset(
    {"ClientError", "EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError"}
)
FAILURE_HANDLER_RETRYABLE: Final = frozenset({"FailureDeliveryError"})
STAGES: Final = ("summary", "rca", "lifecycle", "update")
WORK_QUEUES: Final = ("activeAccounts", "annotation", "summary")
QUEUE_NAMES: Final = (*WORK_QUEUES, "workflowFailures")
SENTINEL: Final = "SENTINEL-E2E-7f3a"
SENTINEL_PERSON: Final = "sentinel-requester@example.com"
SUPPORT_AUTHOR: Final = "Amazon Web Services"
SUMMARY_TEMPLATE: Final = (
    "Synthetic case {display} concerned an Application Load Balancer that returned HTTP 503 "
    "errors immediately after a container release in a production account. The customer "
    "reported that every target in the affected target group had become unhealthy while the "
    "service appeared to be running normally. The engineer compared the listener, target group "
    "and task definition settings and found that the release had changed the container port "
    "without updating the health check. Aligning the container port with the target group "
    "restored healthy targets within minutes. The customer confirmed recovery and agreed to add "
    "a port check to their release pipeline."
)
RCA_REPLY: Final = {
    "RCA_Category": "Customer Release",
    "RCA_Reason": "A customer release changed the container port without the health check.",
}
LIFECYCLE_REPLY: Final = {
    "Lifecycle_Category": "CICD",
    "Lifecycle_Reason": "A pipeline check on port settings would have caught the change.",
}
_CASE_IN_PROMPT: Final = re.compile(r"\bcase (\d{4,})\b")


def model_summary(display: str) -> str:
    """The synthetic model's summary for a case: valid, plain prose and unique to the case."""
    text = SUMMARY_TEMPLATE.format(display=display)
    if not check_summary(text).ok:
        raise ValueError("synthetic summary must satisfy the summary rules")
    return text


def run_day(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def run_id_for(day: datetime) -> str:
    return run_day(day).strftime("%Y%m%dT%H%M%SZ")


def case_key(account: str, display: str, name: str = "data.json") -> str:
    return f"account_number={account}/case_number={display}/{name}"


def client_error(code: str, operation: str, status: int = 400) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "synthetic"},
            "ResponseMetadata": {"HTTPStatusCode": status},  # type: ignore[typeddict-item]
        },
        operation,
    )


def _stubbed(service: str) -> tuple[Any, Stubber]:
    make_client: Callable[..., Any] = boto3.client
    client = make_client(
        service, region_name=REGION, aws_access_key_id="stub", aws_secret_access_key="stub"
    )
    return client, Stubber(client)


# --- Organizations -----------------------------------------------------------------------------


@dataclass(frozen=True)
class OrgAccount:
    account_id: str
    name: str
    state: str = "ACTIVE"


class FakeOrganizations:
    """ListAccounts over a fixed account list, `page_size` accounts per page."""

    def __init__(self, accounts: list[OrgAccount], page_size: int = 2) -> None:
        self.accounts = accounts
        self.page_size = page_size
        self.pages_served = 0
        self.client, self._stubber = _stubbed("organizations")
        self.client.meta.events.register(
            "provide-client-params.organizations.ListAccounts", self._list_accounts
        )
        self._stubber.activate()

    def _list_accounts(self, params: dict[str, Any], **_: Any) -> None:  # noqa: ANN401
        start = int(params.get("NextToken", "0"))
        page = self.accounts[start : start + self.page_size]
        response: dict[str, Any] = {
            "Accounts": [
                {
                    "Id": a.account_id,
                    "Arn": f"arn:aws:organizations::{MANAGEMENT}:account/o-e2e/{a.account_id}",
                    "Name": a.name,
                    "State": a.state,
                    "JoinedTimestamp": datetime(2024, 1, 1, tzinfo=UTC),
                }
                for a in page
            ]
        }
        if start + self.page_size < len(self.accounts):
            response["NextToken"] = str(start + self.page_size)
        self.pages_served += 1
        self._stubber.add_response("list_accounts", response)


# --- Member accounts: STS and Support ---------------------------------------------------------


class FakeSts:
    """AssumeRole into member accounts; accounts in `denied` behave like a missing role."""

    def __init__(self) -> None:
        self.denied: set[str] = set()
        self.calls: list[dict[str, Any]] = []

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        self.calls.append(kwargs)
        account = str(kwargs["RoleArn"]).split(":")[4]
        if account in self.denied:
            raise client_error("AccessDenied", "AssumeRole", 403)
        return {
            "Credentials": {
                "AccessKeyId": f"ASIA{account}",
                "SecretAccessKey": "synthetic-secret",
                "SessionToken": "synthetic-token",
                "Expiration": datetime(2030, 1, 1, tzinfo=UTC),
            }
        }


@dataclass
class SyntheticCase:
    """An invented Support case and its conversation (`messages` communications)."""

    account: str
    display: str
    created: datetime
    status: str = "resolved"
    messages: int = 2

    @property
    def case_id(self) -> str:
        return f"case-{self.account}-2026-{self.display}"

    def details(self) -> dict[str, Any]:
        return {
            "caseId": self.case_id,
            "displayId": self.display,
            "subject": f"{SENTINEL} subject for case {self.display}",
            "status": self.status,
            "serviceCode": "elastic-load-balancing",
            "categoryCode": "performance",
            "severityCode": "high",
            "submittedBy": SENTINEL_PERSON,
            "timeCreated": self.created.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "language": "en",
        }

    def bodies(self) -> list[str]:
        """Message bodies, oldest first."""
        return [f"{SENTINEL} case {self.display} message {n}." for n in range(1, self.messages + 1)]

    def communications(self) -> list[dict[str, Any]]:
        """As DescribeCommunications returns them: newest first."""
        items: list[dict[str, Any]] = [
            {
                "caseId": self.case_id,
                "body": body,
                "submittedBy": SUPPORT_AUTHOR if n % 2 == 0 else SENTINEL_PERSON,
                "timeCreated": (self.created + timedelta(hours=n)).strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"
                ),
                "attachmentSet": [],
            }
            for n, body in enumerate(self.bodies(), start=1)
        ]
        return list(reversed(items))


def _support_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class FakeSupport:
    """Read-only Support API per member account, paginated `page_size` items per page.

    DescribeCases honours `afterTime`/`beforeTime` on the creation time like the real API. Any
    operation other than DescribeCases or DescribeCommunications fails the test.
    """

    READ_OPERATIONS: Final = frozenset({"DescribeCases", "DescribeCommunications"})

    def __init__(self, page_size: int = 2) -> None:
        self.page_size = page_size
        self.cases: dict[str, list[SyntheticCase]] = {}
        self.subscription_required: set[str] = set()
        self.transient_failures: dict[str, int] = {}
        self.calls: list[tuple[str, str]] = []
        self.partitions: list[str] = []
        self._stubbers: dict[str, Stubber] = {}
        self._clients: dict[str, Any] = {}

    def add(self, *cases: SyntheticCase) -> None:
        for case in cases:
            self.cases.setdefault(case.account, []).append(case)

    def factory(self, session: boto3.Session, partition: str) -> Any:  # noqa: ANN401
        """Replacement for `common.support.support_client` used by retrieval and annotation."""
        self.partitions.append(partition)
        credentials = session.get_credentials()
        assert credentials is not None
        account = credentials.access_key.removeprefix("ASIA")
        if account not in self._clients:
            client, stubber = _stubbed("support")
            client.meta.events.register(
                "provide-client-params.support",
                lambda params, model, **_: self._respond(account, model.name, params),
            )
            stubber.activate()
            self._clients[account], self._stubbers[account] = client, stubber
        return self._clients[account]

    def _respond(self, account: str, operation: str, params: dict[str, Any]) -> None:
        self.calls.append((account, operation))
        assert operation in self.READ_OPERATIONS, f"Support write or unexpected call {operation}"
        stubber = self._stubbers[account]
        method = "describe_cases" if operation == "DescribeCases" else "describe_communications"
        if account in self.subscription_required:
            stubber.add_client_error(method, "SubscriptionRequiredException", http_status_code=400)
            return
        if self.transient_failures.get(account, 0) > 0:
            self.transient_failures[account] -= 1
            stubber.add_client_error(method, "InternalServerError", http_status_code=500)
            return
        if operation == "DescribeCases":
            assert params["includeResolvedCases"] is True
            assert params["includeCommunications"] is False
            after, before = _support_time(params["afterTime"]), _support_time(params["beforeTime"])
            items = [
                c.details() for c in self.cases.get(account, []) if after <= c.created < before
            ]
            stubber.add_response(method, self._page(items, params, "cases"))
            return
        matching = [c for c in self.cases.get(account, []) if c.case_id == params["caseId"]]
        if not matching:
            stubber.add_client_error(method, "CaseIdNotFound", http_status_code=400)
            return
        stubber.add_response(method, self._page(matching[0].communications(), params, "comms"))

    def _page(self, items: list[dict[str, Any]], params: dict[str, Any], kind: str) -> Any:  # noqa: ANN401
        start = int(params.get("nextToken", "0"))
        response: dict[str, Any] = {
            "cases" if kind == "cases" else "communications": items[start : start + self.page_size]
        }
        if start + self.page_size < len(items):
            response["nextToken"] = str(start + self.page_size)
        return response

    def operations(self) -> set[str]:
        return {operation for _, operation in self.calls}


# --- Bedrock -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelCall:
    stage: str
    display: str
    model_id: str
    body: dict[str, Any]

    @property
    def prompt(self) -> str:
        return "\n".join(
            part["text"]
            for message in self.body["messages"]
            if message["role"] == "user"
            for part in message["content"]
        )


class FakeBedrock:
    """InvokeModel answered from the prompt: the stage is recognised from the template wording
    and the case from its conversation or summary. `throttle(display, stage, times)` queues
    ThrottlingException responses for that case and stage; `times=None` throttles until healed.
    """

    def __init__(self) -> None:
        self.client, self._stubber = _stubbed("bedrock-runtime")
        self.calls: list[ModelCall] = []
        self.throttles: dict[tuple[str, str], int | None] = {}
        self.client.meta.events.register(
            "provide-client-params.bedrock-runtime.InvokeModel", self._invoke
        )
        self._stubber.activate()

    def throttle(self, display: str, stage: str, times: int | None = None) -> None:
        self.throttles[(display, stage)] = times

    def heal(self) -> None:
        self.throttles.clear()

    def _invoke(self, params: dict[str, Any], **_: Any) -> None:  # noqa: ANN401
        body = json.loads(params["body"])
        probe = ModelCall("?", "?", params["modelId"], body)
        prompt = probe.prompt
        if "root cause analysis (RCA)" in prompt:
            stage = "rca"
        elif "operational lifecycle practice" in prompt:
            stage = "lifecycle"
        elif "Summarise the resolved AWS Support case" in prompt:
            stage = "summary"
        else:  # pragma: no cover - a new template wording must be taught to the fake
            raise AssertionError("unrecognised prompt")
        match = _CASE_IN_PROMPT.search(prompt)
        assert match is not None, "prompt does not identify the synthetic case"
        display = match.group(1)
        self.calls.append(ModelCall(stage, display, params["modelId"], body))
        remaining = self.throttles.get((display, stage), 0)
        if remaining is None or remaining > 0:
            if remaining is not None:
                self.throttles[(display, stage)] = remaining - 1
            self._stubber.add_client_error(
                "invoke_model", "ThrottlingException", http_status_code=429
            )
            return
        text = {
            "summary": model_summary(display),
            "rca": json.dumps(RCA_REPLY),
            "lifecycle": json.dumps(LIFECYCLE_REPLY),
        }[stage]
        payload = json.dumps(
            {
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 850, "output_tokens": 150},
            }
        ).encode()
        self._stubber.add_response(
            "invoke_model",
            {
                "body": StreamingBody(io.BytesIO(payload), len(payload)),
                "contentType": "application/json",
            },
        )

    def calls_for(self, display: str, stage: str | None = None) -> list[ModelCall]:
        return [c for c in self.calls if c.display == display and stage in (None, c.stage)]


# --- The pipeline ------------------------------------------------------------------------------


@dataclass
class Invocation:
    function: str
    metrics: dict[str, float]
    result: Any = None
    error: str | None = None


def catch_result(exc: BaseException) -> dict[str, str]:
    """The `$.error` value Step Functions records when a Lambda task raises `exc`."""
    cause = {
        "errorMessage": str(exc),
        "errorType": type(exc).__name__,
        "stackTrace": traceback.format_tb(exc.__traceback__),
    }
    return {"Error": type(exc).__name__, "Cause": json.dumps(cause)}


@dataclass
class Pipeline:
    """The Core pipeline wired to local stand-ins; see the module docstring."""

    monkeypatch: pytest.MonkeyPatch
    capsys: pytest.CaptureFixture[str]
    make_context: Callable[[str], LambdaContext]
    parse_metrics: Callable[[str], dict[str, float]]
    org: FakeOrganizations
    support: FakeSupport
    clock: list[datetime]
    sts: FakeSts = field(default_factory=FakeSts)
    bedrock: FakeBedrock = field(default_factory=FakeBedrock)
    sfn: FakeStepFunctions = field(default_factory=FakeStepFunctions)
    invocations: list[Invocation] = field(default_factory=list)
    held: set[str] = field(default_factory=set)
    pause_after: dict[str, str] = field(default_factory=dict)
    queue_urls: dict[str, str] = field(default_factory=dict)
    dlq_urls: dict[str, str] = field(default_factory=dict)
    _driven: set[str] = field(default_factory=set)
    _notified: set[tuple[str, str]] = field(default_factory=set)
    _in_flight: dict[str, list[str]] = field(default_factory=dict)
    _put_faults: dict[str, set[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.s3 = boto3.client("s3", region_name=REGION)
        self.sqs = boto3.client("sqs", region_name=REGION)
        for bucket in (ACCOUNT_LISTS, RAW, PROCESSED):
            self.s3.create_bucket(Bucket=bucket)
            self.s3.put_bucket_versioning(
                Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
            )
        for name in QUEUE_NAMES:
            dlq = self.sqs.create_queue(QueueName=f"e2e-{name}-dlq")["QueueUrl"]
            arn = self.sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])
            policy = {
                "deadLetterTargetArn": arn["Attributes"]["QueueArn"],
                "maxReceiveCount": str(MAX_RECEIVE_COUNT),
            }
            attributes: dict[QueueAttributeNameType, str] = (
                {"RedrivePolicy": json.dumps(policy)} if name in WORK_QUEUES else {}
            )
            self.queue_urls[name] = self.sqs.create_queue(
                QueueName=f"e2e-{name}", Attributes=attributes
            )["QueueUrl"]
            self.dlq_urls[name] = dlq
            self._in_flight[name] = []
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=IDEMPOTENCY_TABLE,
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self._configure_functions()

    # Function wiring mirrors the CDK environment of each function.
    def _configure_functions(self) -> None:
        from analysis_kit import TEMPLATES  # noqa: PLC0415 - avoids importing the kit's fixtures early

        environment = {
            "ACCOUNT_LIST_BUCKET": ACCOUNT_LISTS,
            "MANAGEMENT_ACCOUNT_ID": MANAGEMENT,
            "ACTIVE_ACCOUNTS_QUEUE_URL": self.queue_urls["activeAccounts"],
            "ANNOTATION_QUEUE_URL": self.queue_urls["annotation"],
            "SUMMARY_QUEUE_URL": self.queue_urls["summary"],
            "FAILURE_QUEUE_URL": self.queue_urls["workflowFailures"],
            "RAW_BUCKET": RAW,
            "PROCESSED_BUCKET": PROCESSED,
            "IDEMPOTENCY_TABLE_NAME": IDEMPOTENCY_TABLE,
            "MEMBER_ROLE_NAME": ROLE_NAME,
            "MEMBER_ROLE_PARTITION": "aws",
            "LOOKBACK_MONTHS": "12",
            "BEDROCK_MODEL_ID": HAIKU,
            "BEDROCK_MAX_TOKENS": "1000",
            "CASE_INSIGHTS_TEMPLATES_DIR": str(TEMPLATES),
            "STATE_MACHINE_ARN": STATE_MACHINE_ARN,
            "CLAIM_SETTLE_SECONDS": "300",
            "CLEANUP_GRACE_HOURS": "24",
            "RUN_LOOKBACK_HOURS": "72",
        }
        patch = self.monkeypatch
        for name, value in environment.items():
            patch.setenv(name, value)

        def s3(function: str) -> Any:  # noqa: ANN401
            """Each function has its own client, as in Lambda, so faults can target one."""
            client = boto3.client("s3", region_name=REGION)
            client.meta.events.register(
                "provide-client-params.s3.PutObject",
                lambda params, **_: self._maybe_fail_put(function, params),
            )
            return client

        sfn: Any = self.sfn
        bound: list[tuple[Any, Any]] = [
            (lookup_app, lookup_app.Clients(organizations=self.org.client, s3=s3("lookup"))),
            (reader_app, reader_app.Clients(s3=s3("reader"), sqs=self.sqs)),
            (
                retrieval_app,
                retrieval_app.Clients(s3=s3("retrieval"), sqs=self.sqs, sts=self.sts),  # type: ignore[arg-type]
            ),
            (
                annotation_app,
                annotation_app.Clients(s3=s3("annotation"), sqs=self.sqs, sts=self.sts),  # type: ignore[arg-type]
            ),
            (starter_app, starter_app.Clients(s3=s3("starter"), sfn=sfn)),
            (update_app, update_app.Clients(s3=s3("update"))),
            (failure_app, failure_app.Clients(sqs=self.sqs, s3=s3("failure"))),
            (cleanup_app, cleanup_app.Clients(s3=s3("cleanup"), sfn=sfn, sqs=self.sqs)),
            (completion_app, completion_app.Clients(s3=s3("completion"), sfn=sfn, sqs=self.sqs)),
        ]
        stage_clients = AnalysisClients(s3=s3("stages"), bedrock=self.bedrock.client)
        bound += [(module, stage_clients) for module in (summary_app, rca_app, lifecycle_app)]
        for module, clients in bound:
            patch.setattr(module, "clients", lambda clients=clients: clients)
            if hasattr(module, "now"):
                patch.setattr(module, "now", lambda: self.clock[0])
        for module in (retrieval_app, annotation_app):
            patch.setattr(module, "support_client", self.support.factory)
        patch.setattr(analysis, "sleep", lambda _seconds: None)
        patch.setattr(cleanup_app, "LIST_PAGE_SIZE", 2)
        patch.setattr(completion_app, "LIST_PAGE_SIZE", 2)
        persistence_layer.cache_clear()

    def fail_puts(self, function: str, *keys: str) -> None:
        """Make `function`'s S3 PutObject to these keys fail with InternalError until healed."""
        self._put_faults.setdefault(function, set()).update(keys)

    def heal_puts(self) -> None:
        self._put_faults.clear()

    def _maybe_fail_put(self, function: str, params: dict[str, Any]) -> None:
        if params.get("Key") in self._put_faults.get(function, set()):
            raise client_error("InternalError", "PutObject", 500)

    # --- invocation --------------------------------------------------------------------------

    def invoke(self, function: str, handler: Callable[..., Any], event: object) -> Any:  # noqa: ANN401
        """Invoke a handler like Lambda; record its EMF metrics and outcome."""
        self.capsys.readouterr()
        invocation = Invocation(function=function, metrics={})
        self.invocations.append(invocation)
        context = self.make_context(f"{function}-{len(self.invocations)}")
        try:
            invocation.result = handler(event, context)
        except Exception as exc:
            invocation.error = type(exc).__name__
            raise
        finally:
            invocation.metrics = self.parse_metrics(self.capsys.readouterr().out)
        return invocation.result

    def metric(self, name: str, function: str | None = None) -> float:
        return sum(
            i.metrics.get(name, 0.0)
            for i in self.invocations
            if function is None or i.function == function
        )

    def heartbeats(self) -> float:
        return self.metric("DailyRunCompleted", "RunCompletion")

    def advance(self, **delta: float) -> None:
        self.clock[0] += timedelta(**delta)

    # --- triggers ----------------------------------------------------------------------------

    def discover(self) -> dict[str, Any]:
        """The 00:00 UTC schedule firing for the clock's day."""
        scheduled = run_day(self.clock[0]).isoformat().replace("+00:00", "Z")
        result: dict[str, Any] = self.invoke(
            "AccountLookup", lookup_app.handler, {"scheduledTime": scheduled}
        )
        return result

    def notify_account_lists(self) -> int:
        """Deliver one S3 notification per account-list object version not yet notified."""
        listing = self.s3.list_object_versions(Bucket=ACCOUNT_LISTS)
        delivered = 0
        for version in listing.get("Versions", []):
            identity = (version["Key"], version["VersionId"])
            if identity in self._notified:
                continue
            self._notified.add(identity)
            self.notify(*identity)
            delivered += 1
        return delivered

    def notify(self, key: str, version_id: str) -> dict[str, Any]:
        record = {
            "eventVersion": "2.1",
            "eventSource": "aws:s3",
            "awsRegion": REGION,
            "eventTime": self.clock[0].isoformat(),
            "eventName": "ObjectCreated:Put",
            "s3": {
                "s3SchemaVersion": "1.0",
                "bucket": {"name": ACCOUNT_LISTS},
                "object": {"key": quote_plus(key), "versionId": version_id, "size": 1},
            },
        }
        result: dict[str, Any] = self.invoke(
            "AccountReader", reader_app.handler, {"Records": [record]}
        )
        return result

    def send(self, queue: str, *bodies: object) -> None:
        for body in bodies:
            self.sqs.send_message(
                QueueUrl=self.queue_urls[queue],
                MessageBody=body if isinstance(body, str) else json.dumps(body),
            )

    def deliver(self, queue: str) -> int:
        """Run the queue's event source mapping until no message is visible."""
        function, handler = {
            "activeAccounts": ("CaseRetrieval", retrieval_app.handler),
            "annotation": ("CaseAnnotation", annotation_app.handler),
            "summary": ("WorkflowStarter", starter_app.handler),
        }[queue]
        url = self.queue_urls[queue]
        delivered = 0
        while True:
            messages = self.sqs.receive_message(
                QueueUrl=url,
                MaxNumberOfMessages=10,
                VisibilityTimeout=VISIBILITY_SECONDS,
                AttributeNames=["All"],
            ).get("Messages", [])
            if not messages:
                return delivered
            event = {
                "Records": [
                    {
                        "messageId": m["MessageId"],
                        "receiptHandle": m["ReceiptHandle"],
                        "body": m["Body"],
                        "attributes": m.get("Attributes", {}),
                        "messageAttributes": {},
                        "md5OfBody": m["MD5OfBody"],
                        "eventSource": "aws:sqs",
                        "eventSourceARN": f"arn:aws:sqs:{REGION}:123456789012:e2e-{queue}",
                        "awsRegion": REGION,
                    }
                    for m in messages
                ]
            }
            response = self.invoke(function, handler, event)
            failed = {item["itemIdentifier"] for item in response["batchItemFailures"]}
            for message in messages:
                if message["MessageId"] in failed:
                    self._in_flight[queue].append(message["ReceiptHandle"])
                else:
                    self.sqs.delete_message(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"])
            delivered += len(messages)

    def expire_visibility(self) -> int:
        """Let every failed in-flight record become visible again (its timeout has passed)."""
        expired = 0
        for queue, receipts in self._in_flight.items():
            for receipt in receipts:
                self.sqs.change_message_visibility(
                    QueueUrl=self.queue_urls[queue], ReceiptHandle=receipt, VisibilityTimeout=0
                )
                expired += 1
            receipts.clear()
        return expired

    # --- Step Functions ----------------------------------------------------------------------

    def run_workflows(self) -> list[str]:
        """Run every RUNNING execution not yet run, unless its case is held."""
        ran: list[str] = []
        for name, execution in list(self.sfn.executions.items()):
            if execution.status != "RUNNING" or name in self._driven:
                continue
            display = json.loads(execution.input)["case"]["displayId"]
            if display in self.held:
                continue
            self._driven.add(name)
            execution.status = self._execute(execution, self.pause_after.pop(display, None))
            ran.append(name)
        return ran

    def _execute(self, execution: Execution, pause_after: str | None) -> str:
        state: dict[str, Any] = json.loads(execution.input)
        for stage in STAGES:
            if stage == "update":
                function, handler = "UpdateCaseMetadata", update_app.handler
                payload: dict[str, Any] = {"case": state["case"]}
                retryable = TRANSIENT_TASK_ERRORS
            else:
                function, handler = "AnalysisStages", stages_app.handler
                payload = {"stage": stage, "case": state["case"]}
                retryable = BEDROCK_RETRYABLE | TRANSIENT_TASK_ERRORS
            retries = 0
            while True:
                try:
                    state[stage] = self.invoke(function, handler, payload)
                    break
                except Exception as exc:
                    if type(exc).__name__ in retryable and retries < TASK_RETRIES:
                        retries += 1
                        continue
                    self._handle_failure(state, exc)
                    return "FAILED"
            if stage == pause_after:
                return "RUNNING"
        return "SUCCEEDED"

    def _handle_failure(self, state: dict[str, Any], exc: BaseException) -> None:
        event = {**state, "error": catch_result(exc)}
        for _ in range(FAILURE_HANDLER_RETRIES + 1):
            try:
                state["failureRecord"] = self.invoke("HandleFailure", failure_app.handler, event)
            except Exception as error:
                if type(error).__name__ in FAILURE_HANDLER_RETRYABLE:
                    continue
            return

    def release(self, display: str) -> None:
        self.held.discard(display)

    def time_out(self, display: str) -> str:
        """A whole-execution timeout of the case's running execution (bypasses every Catch)."""
        [name] = [
            n
            for n, e in self.sfn.executions.items()
            if e.status == "RUNNING" and json.loads(e.input)["case"]["displayId"] == display
        ]
        self.sfn.executions[name].status = "TIMED_OUT"
        return name

    def executions_for(self, display: str) -> list[Execution]:
        return [
            e
            for e in self.sfn.executions.values()
            if json.loads(e.input)["case"]["displayId"] == display
        ]

    # --- composite steps ---------------------------------------------------------------------

    def settle(self, max_rounds: int = 25) -> None:
        """Move work through every trigger until nothing is left to do."""
        for _ in range(max_rounds):
            progress = self.notify_account_lists()
            for queue in WORK_QUEUES:
                progress += self.deliver(queue)
            progress += len(self.run_workflows())
            if progress == 0 and self.expire_visibility() == 0:
                return
        raise AssertionError("the pipeline did not settle")

    def complete_runs(self) -> dict[str, Any]:
        result: dict[str, Any] = self.invoke("RunCompletion", completion_app.handler, {})
        return result

    def cleanup(self) -> dict[str, Any]:
        result: dict[str, Any] = self.invoke("CaseCleanup", cleanup_app.handler, {})
        return result

    # --- inspection --------------------------------------------------------------------------

    def keys(self, bucket: str, prefix: str = "") -> list[str]:
        paginator = self.s3.get_paginator("list_objects_v2")
        return sorted(
            item["Key"]
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
            for item in page.get("Contents", [])
        )

    def read(self, bucket: str, key: str) -> Any:  # noqa: ANN401
        return json.loads(self.s3.get_object(Bucket=bucket, Key=key)["Body"].read())

    def processed(self, account: str, display: str) -> dict[str, Any] | None:
        try:
            document: dict[str, Any] = self.read(PROCESSED, case_key(account, display))
        except self.s3.exceptions.NoSuchKey:
            return None
        return document

    def versions(self, bucket: str, key: str) -> int:
        listing = self.s3.list_object_versions(Bucket=bucket, Prefix=key)
        return sum(1 for v in listing.get("Versions", []) if v["Key"] == key)

    def drain(self, queue: str, *, dead_letter: bool = False) -> list[dict[str, Any]]:
        url = self.dlq_urls[queue] if dead_letter else self.queue_urls[queue]
        received: list[dict[str, Any]] = []
        while True:
            batch = self.sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10).get(
                "Messages", []
            )
            if not batch:
                return received
            for message in batch:
                try:
                    received.append(json.loads(message["Body"]))
                except ValueError:
                    received.append({"unparsable": message["Body"]})
                self.sqs.delete_message(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"])

    def depth(self, queue: str, *, dead_letter: bool = False) -> int:
        url = self.dlq_urls[queue] if dead_letter else self.queue_urls[queue]
        attributes = self.sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        return int(attributes["ApproximateNumberOfMessages"]) + int(
            attributes["ApproximateNumberOfMessagesNotVisible"]
        )

    def now_stamp(self) -> str:
        return utc_timestamp(self.clock[0])

    def outputs(self) -> Iterable[str]:
        """Everything the pipeline exposed outside storage: handler results, execution inputs
        and execution state results (metrics are numbers by construction)."""
        for invocation in self.invocations:
            yield json.dumps(invocation.result, default=str)
        for execution in self.sfn.executions.values():
            yield execution.input

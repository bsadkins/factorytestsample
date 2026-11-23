"""Test kit for the CaseAnalysis stage handlers: versioned S3 buckets, a stubbed Bedrock runtime
client that records each request, synthetic case data and log capture.

Importable by test modules (pytest `pythonpath` includes test/support); the `analysis_env`
fixture is registered by test/lambda/analysis/conftest.py.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from case_lifecycle import app as lifecycle_app
from case_rca import app as rca_app
from case_summary import app as summary_app
from common import analysis
from common.analysis import AnalysisClients
from common.case_refs import SummaryRef
from common.checkpoints import checkpoint_key
from update_case_metadata import app as update_app

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext
    from types_boto3_s3.client import S3Client

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = REPO_ROOT / "src" / "templates"
FIXTURES = REPO_ROOT / "test" / "fixtures" / "prompts"
RAW = "raw-analysis-test"
PROCESSED = "processed-analysis-test"
HAIKU = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
SONNET = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
ACCOUNT = "111122223333"
DISPLAY = "170000000001"
CASE_ID = "case-111122223333-2026-abcdef01"
RUN = "20261113T000000Z"
RETRIEVED_AT = "2026-11-13T00:05:12Z"
NOW = datetime(2026, 11, 13, 1, 2, 3, tzinfo=UTC)
SENTINEL_BODY = "SENTINEL-CONVERSATION-41c9"
SENTINEL_PERSON = "sentinel-requester@example.com"


def fixture_cases(name: str) -> list[dict[str, Any]]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    cases: list[dict[str, Any]] = data["cases"]
    return cases


def summary_fixture(case_id: str) -> str:
    return str(next(c["text"] for c in fixture_cases("summaries.json") if c["id"] == case_id))


VALID_SUMMARY = summary_fixture("valid-five-sentences")
OTHER_VALID_SUMMARY = summary_fixture("valid-four-sentences-unicode")


def raw_metadata(**overrides: Any) -> dict[str, Any]:  # noqa: ANN401
    document: dict[str, Any] = {
        "schemaVersion": 1,
        "accountId": ACCOUNT,
        "runId": RUN,
        "caseId": CASE_ID,
        "displayId": DISPLAY,
        "subject": "Synthetic: load balancer returns 503",
        "status": "resolved",
        "serviceCode": "elastic-load-balancing",
        "categoryCode": "performance",
        "severityCode": "high",
        "submittedBy": SENTINEL_PERSON,
        "timeCreated": "2026-06-01T10:00:00.000Z",
        "language": "en",
        "Case_Retrieval_Date": RETRIEVED_AT,
    }
    document.update(overrides)
    return document


def communications(bodies: list[str] | None = None) -> list[dict[str, Any]]:
    texts = bodies or [
        f"{SENTINEL_BODY}: our ALB returns HTTP 503 after the release.",
        "Target group health checks fail because the container listens on port 8081.",
        "Aligning the container port fixed the health checks; please close the case.",
    ]
    return [
        {
            "body": text,
            "submittedBy": "Amazon Web Services" if index % 2 else SENTINEL_PERSON,
            "timeCreated": f"2026-06-01T10:{index:02d}:00.000Z",
            "attachmentSet": [],
        }
        for index, text in enumerate(texts)
    ]


class BedrockStub:
    """A real bedrock-runtime client behind Stubber. Every request body is recorded, and an
    optional side effect can run while a given call is in flight (to simulate a concurrent
    attempt). Any call that was not queued fails the test."""

    def __init__(self) -> None:
        self.client = boto3.client(
            "bedrock-runtime",
            region_name="us-east-1",
            aws_access_key_id="stub",
            aws_secret_access_key="stub",
        )
        self.stubber = Stubber(self.client)
        self.requests: list[dict[str, Any]] = []
        self.during_call: dict[int, Callable[[], None]] = {}
        self.client.meta.events.register(
            "provide-client-params.bedrock-runtime.InvokeModel", self._record
        )
        self.stubber.activate()

    def _record(self, params: dict[str, Any], **_: Any) -> None:  # noqa: ANN401
        body = params["body"]
        self.requests.append(
            {
                "modelId": params["modelId"],
                "contentType": params.get("contentType"),
                "accept": params.get("accept"),
                "body": json.loads(body if isinstance(body, (bytes, str)) else body.read()),
            }
        )
        effect = self.during_call.pop(len(self.requests), None)
        if effect is not None:
            effect()

    def reply(self, *texts: str, stop: str = "end_turn") -> None:
        for text in texts:
            payload = json.dumps(
                {
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": stop,
                    "usage": {"input_tokens": 900, "output_tokens": 160},
                }
            ).encode()
            self.stubber.add_response(
                "invoke_model",
                {
                    "body": StreamingBody(io.BytesIO(payload), len(payload)),
                    "contentType": "application/json",
                },
            )

    def error(self, code: str, status: int = 400, times: int = 1) -> None:
        for _ in range(times):
            self.stubber.add_client_error(
                "invoke_model", service_error_code=code, http_status_code=status
            )

    def prompts(self, index: int = 0) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = self.requests[index]["body"]["messages"]
        return messages

    def assert_consumed(self) -> None:
        self.stubber.assert_no_pending_responses()


@dataclass
class AnalysisEnv:
    """Handler entry points plus synthetic case state for one case."""

    account: ClassVar[str] = ACCOUNT
    display: ClassVar[str] = DISPLAY
    case_id: ClassVar[str] = CASE_ID
    run: ClassVar[str] = RUN
    now: ClassVar[datetime] = NOW
    retrieved_at: ClassVar[str] = RETRIEVED_AT
    haiku: ClassVar[str] = HAIKU
    sonnet: ClassVar[str] = SONNET
    raw_bucket: ClassVar[str] = RAW
    processed_bucket: ClassVar[str] = PROCESSED
    templates: ClassVar[Path] = TEMPLATES
    valid_summary: ClassVar[str] = VALID_SUMMARY
    other_summary: ClassVar[str] = OTHER_VALID_SUMMARY
    sentinels: ClassVar[tuple[str, ...]] = (SENTINEL_BODY, SENTINEL_PERSON)

    s3: S3Client
    bedrock: BedrockStub
    log_stream: io.StringIO
    capsys: pytest.CaptureFixture[str]
    sleeps: list[float]
    during_sleep: dict[int, Callable[[], None]]
    make_context: Callable[[], LambdaContext]
    parse_metrics: Callable[[str], dict[str, float]]
    stdout: str = field(default="")
    ref: SummaryRef = field(init=False)

    def seed_case(
        self,
        *,
        bodies: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store raw metadata and a conversation as retrieval/annotation would; return the
        workflow state for the case."""
        self.s3.put_object(
            Bucket=RAW,
            Key=f"account_number={ACCOUNT}/case_number={DISPLAY}/data.json",
            Body=json.dumps(metadata if metadata is not None else raw_metadata()).encode(),
        )
        return self.store_conversation(bodies)

    def store_conversation(self, bodies: list[str] | None = None) -> dict[str, Any]:
        comms = communications(bodies)
        document = {
            "schemaVersion": 1,
            "accountId": ACCOUNT,
            "caseId": CASE_ID,
            "displayId": DISPLAY,
            "runId": RUN,
            "retrievedAt": "2026-11-13T00:07:00Z",
            "communicationCount": len(comms),
            "communications": comms,
        }
        written = self.s3.put_object(
            Bucket=RAW,
            Key=f"account_number={ACCOUNT}/case_number={DISPLAY}/communications.json",
            Body=json.dumps(document).encode(),
        )
        self.ref = SummaryRef(
            account_id=ACCOUNT,
            case_id=CASE_ID,
            display_id=DISPLAY,
            run_id=RUN,
            communications_version_id=written["VersionId"],
            communication_count=len(comms),
        )
        return self.state()

    def state(self) -> dict[str, Any]:
        return {"case": self.ref.to_message(), "attempt": f"{RUN}-1"}

    @staticmethod
    def raw_metadata(**overrides: Any) -> dict[str, Any]:  # noqa: ANN401
        return raw_metadata(**overrides)

    @staticmethod
    def fixture_cases(name: str) -> list[dict[str, Any]]:
        return fixture_cases(name)

    @staticmethod
    def communications(bodies: list[str] | None = None) -> list[dict[str, Any]]:
        return communications(bodies)

    def context(self) -> LambdaContext:
        return self.make_context()

    def summary(self, state: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = summary_app.handler(state, self.context())
        return result

    def rca(self, state: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = rca_app.handler(state, self.context())
        return result

    def lifecycle(self, state: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = lifecycle_app.handler(state, self.context())
        return result

    def update(self, state: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = update_app.handler(state, self.context())
        return result

    def checkpoint(self, stage: str) -> dict[str, Any] | None:
        try:
            body = self.s3.get_object(Bucket=RAW, Key=checkpoint_key(ACCOUNT, DISPLAY, stage))
        except self.s3.exceptions.NoSuchKey:
            return None
        document: dict[str, Any] = json.loads(body["Body"].read())
        return document

    def put_checkpoint(self, stage: str, document: dict[str, Any]) -> None:
        self.s3.put_object(
            Bucket=RAW,
            Key=checkpoint_key(ACCOUNT, DISPLAY, stage),
            Body=json.dumps(document).encode(),
        )

    def processed_key(self) -> str:
        return f"account_number={ACCOUNT}/case_number={DISPLAY}/data.json"

    def processed(self) -> dict[str, Any] | None:
        try:
            body = self.s3.get_object(Bucket=PROCESSED, Key=self.processed_key())
        except self.s3.exceptions.NoSuchKey:
            return None
        document: dict[str, Any] = json.loads(body["Body"].read())
        return document

    def processed_versions(self) -> int:
        listing = self.s3.list_object_versions(Bucket=PROCESSED, Prefix=self.processed_key())
        return len(listing.get("Versions", []))

    def mark_processed(self, document: dict[str, Any] | None = None) -> None:
        self.s3.put_object(
            Bucket=PROCESSED,
            Key=self.processed_key(),
            Body=json.dumps(document or {"committedBy": "earlier attempt"}).encode(),
        )

    def metrics(self) -> dict[str, float]:
        self.stdout = self.capsys.readouterr().out
        return self.parse_metrics(self.stdout)

    def logs(self) -> str:
        return self.log_stream.getvalue()


@pytest.fixture
def analysis_env(
    mocked_aws: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    make_context: Callable[[], LambdaContext],
    metrics_in: Callable[[str], dict[str, float]],
) -> Iterator[AnalysisEnv]:
    s3 = boto3.client("s3", region_name="us-east-1")
    for name in (RAW, PROCESSED):
        s3.create_bucket(Bucket=name)
        s3.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})
    for key, value in {
        "RAW_BUCKET": RAW,
        "PROCESSED_BUCKET": PROCESSED,
        "BEDROCK_MODEL_ID": HAIKU,
        "BEDROCK_MAX_TOKENS": "1000",
        "CASE_INSIGHTS_TEMPLATES_DIR": str(TEMPLATES),
    }.items():
        monkeypatch.setenv(key, value)
    bedrock = BedrockStub()
    analysis_clients = AnalysisClients(s3=s3, bedrock=bedrock.client)
    for module in (summary_app, rca_app, lifecycle_app):
        monkeypatch.setattr(module, "clients", lambda: analysis_clients)
        monkeypatch.setattr(module, "now", lambda: NOW)
    update_clients = update_app.Clients(s3=s3)
    monkeypatch.setattr(update_app, "clients", lambda: update_clients)
    sleeps: list[float] = []
    during_sleep: dict[int, Callable[[], None]] = {}

    def backoff(delay: float) -> None:
        """Record a retry delay and run the side effect queued for that backoff, if any."""
        sleeps.append(delay)
        effect = during_sleep.pop(len(sleeps), None)
        if effect is not None:
            effect()

    monkeypatch.setattr(analysis, "sleep", backoff)

    stream = io.StringIO()
    service_logger = logging.getLogger("handler-test")
    for log_handler in service_logger.handlers:
        if isinstance(log_handler, logging.StreamHandler):
            monkeypatch.setattr(log_handler, "stream", stream)
    env = AnalysisEnv(
        s3=s3,
        bedrock=bedrock,
        log_stream=stream,
        capsys=capsys,
        sleeps=sleeps,
        during_sleep=during_sleep,
        make_context=make_context,
        parse_metrics=metrics_in,
    )
    yield env
    bedrock.assert_consumed()

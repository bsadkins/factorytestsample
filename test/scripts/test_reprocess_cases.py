"""Opt-in reprocess CLI: the real entry point against mocked versioned S3, SQS and Step Functions.

Targets are resolved the way the deployed script resolves them: STS (moto), the Core stack outputs
(a botocore-stubbed CloudFormation client, since moto's CloudFormation needs extra packages) and
moto S3/SQS. Step Functions is `workflow_kit.FakeStepFunctions`. Every record is invented.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from common.case_refs import SummaryRef, communications_key
from common.checkpoints import STAGES, checkpoint_key
from common.conversation import build_conversation_document
from common.processed import case_record_key
from common.workflow import (
    OwnershipMarker,
    ResetClaim,
    encode_input,
    execution_name,
    marker_key,
)
from moto import mock_aws
from workflow_kit import STATE_MACHINE_ARN, FakeStepFunctions, sfn_error
from workflow_starter import app as starter

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "reprocess-cases.py"
UNIQUE = "test"
ACCOUNT = "123456789012"  # moto's caller account, also the state machine's account
MEMBER_A = "111122223333"
MEMBER_B = "444455556666"
REGION = "us-east-1"
RAW = "ci-raw-test"
PROCESSED = "ci-processed-test"
QUEUE = f"CaseInsights-CaseSummary-{UNIQUE}"
HISTORIC_RUN = "20260301T000000Z"
NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)
SENTINEL = "SENTINEL-REPROCESS-5b1e"
WRITE_OPERATIONS = frozenset(
    {"PutObject", "DeleteObject", "DeleteObjects", "CopyObject", "SendMessage", "StartExecution"}
)
RUN_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("reprocess_cases", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script() -> ModuleType:
    return load_script()


def make_record(display: str, account: str, **overrides: str) -> dict[str, Any]:
    record: dict[str, Any] = {
        "caseId": f"case-{account}-2026-{display}",
        "displayId": display,
        "subject": f"{SENTINEL} subject",
        "serviceCode": "amazon-elastic-compute-cloud-linux",
        "categoryCode": "instance-issue",
        "severityCode": "low",
        "submittedBy": "builder@example.com",
        "timeCreated": "2026-06-01T10:00:00.000Z",
        "Case_Retrieval_Date": "2026-07-01T00:05:00Z",
        "Case_Summary": f"{SENTINEL} summary.",
        "RCA_Category": "Customer Release",
        "RCA_Reason": "Synthetic reason.",
        "RCA_Retrieval_Date": "2026-07-01T00:06:00Z",
        "Lifecycle_Category": "CICD",
        "Lifecycle_Reason": "Synthetic reason.",
        "Lifecycle_Retrieval_Date": "2026-07-01T00:07:00Z",
        "Model_Id": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        "Prompt_Version": "pv-0123456789abcdef",
    }
    record.update(overrides)
    return record


@dataclass
class Seeded:
    account: str
    display: str
    case_id: str
    processed_version: str
    communications_version: str | None = None
    execution: str | None = None

    @property
    def key(self) -> str:
        return case_record_key(self.account, self.display)


@dataclass
class Env:
    script: ModuleType
    s3: Any
    sqs: Any
    sfn: FakeStepFunctions
    queue_url: str
    config_dir: Path
    capsys: pytest.CaptureFixture[str]
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)
    stack_status: str = "UPDATE_COMPLETE"
    stack_error: str | None = None
    regions: list[str] = field(default_factory=list)

    # --- seeding ------------------------------------------------------------------------------

    def put(self, bucket: str, key: str, document: object) -> str:
        body = document if isinstance(document, str) else json.dumps(document)
        return str(self.s3.put_object(Bucket=bucket, Key=key, Body=body.encode())["VersionId"])

    def seed(  # noqa: PLR0913
        self,
        display: str,
        created: str = "2026-06-01T10:00:00.000Z",
        *,
        account: str = MEMBER_A,
        raw: bool = True,
        marker: str | None = "SUCCEEDED",
        checkpoints: bool = True,
        **overrides: str,
    ) -> Seeded:
        record = make_record(display, account, timeCreated=created, **overrides)
        version = self.put(PROCESSED, case_record_key(account, display), record)
        seeded = Seeded(account, display, record["caseId"], version)
        if raw:
            self.put(RAW, case_record_key(account, display), {**record, "accountId": account})
            conversation = build_conversation_document(
                account_id=account,
                case_id=record["caseId"],
                display_id=display,
                run_id=HISTORIC_RUN,
                retrieved_at=datetime(2026, 3, 1, tzinfo=UTC),
                communications=[
                    {"body": f"{SENTINEL} question", "submittedBy": "builder@example.com"},
                    {"body": f"{SENTINEL} answer", "submittedBy": "Amazon Web Services"},
                ],
            )
            seeded.communications_version = self.put(
                RAW, communications_key(account, display), conversation
            )
            if marker is not None:
                ref = SummaryRef(
                    account,
                    record["caseId"],
                    display,
                    HISTORIC_RUN,
                    seeded.communications_version,
                    2,
                )
                claim = OwnershipMarker.claim(ref, 1, NOW - timedelta(days=30))
                # Stored exactly as the starter writes it (sorted, compact JSON).
                self.put(RAW, claim.key, encode_input(claim.to_document()))
                self.sfn.add(claim.execution_name, marker, encode_input(claim.input))
                seeded.execution = claim.execution_name
        if checkpoints:
            for stage in STAGES:
                self.put(RAW, checkpoint_key(account, display, stage), {"synthetic": stage})
        return seeded

    # --- running ------------------------------------------------------------------------------

    def run(self, *args: str, env: str = UNIQUE) -> tuple[int, str, str]:
        self.capsys.readouterr()
        self.calls.clear()
        code = self.script.main(["--env", env, "--config-dir", str(self.config_dir), *args])
        captured = self.capsys.readouterr()
        assert SENTINEL not in captured.out + captured.err
        return int(code), captured.out, captured.err

    def token(self, *args: str) -> str:
        code, out, _ = self.run(*args, "--dry-run")
        assert code == 0
        match = re.search(r"Plan token: (rp-[0-9a-f]{12})", out)
        assert match is not None, out
        return match.group(1)

    # --- inspection ---------------------------------------------------------------------------

    def operations(self) -> list[str]:
        return [name for name, _ in self.calls]

    def writes(self) -> list[str]:
        written = [name for name in self.operations() if name in WRITE_OPERATIONS]
        return written + list(self.sfn.started())

    def sent(self) -> list[tuple[dict[str, Any], int]]:
        return [
            (json.loads(params["MessageBody"]), int(params.get("DelaySeconds", 0)))
            for name, params in self.calls
            if name == "SendMessage"
        ]

    def versions(self, bucket: str, key: str) -> list[tuple[str, str, bool]]:
        listing = self.s3.list_object_versions(Bucket=bucket, Prefix=key)
        found = [
            ("version", v["VersionId"], v["IsLatest"])
            for v in listing.get("Versions", [])
            if v["Key"] == key
        ] + [
            ("delete-marker", v["VersionId"], v["IsLatest"])
            for v in listing.get("DeleteMarkers", [])
            if v["Key"] == key
        ]
        return sorted(found, key=lambda item: (not item[2], item[0]))

    def unchanged(self, seeded: Seeded) -> bool:
        return self.versions(PROCESSED, seeded.key) == [("version", seeded.processed_version, True)]

    def removed(self, bucket: str, key: str) -> bool:
        """The key's latest version is a delete marker and its previous versions are kept."""
        versions = self.versions(bucket, key)
        return versions[0][0] == "delete-marker" and versions[0][2] and len(versions) >= 2


def write_config(directory: Path, name: str = UNIQUE, **overrides: object) -> None:
    document: dict[str, object] = {
        "uniqueId": UNIQUE,
        "orgId": "o-exampleorg1",
        "managementAccountId": ACCOUNT,
        "region": REGION,
        "environment": name,
        "memberRoleTargets": ["r-ab12"],
        "lookbackMonths": 12,
    }
    document.update(overrides)
    (directory / f"{name}.json").write_text(json.dumps(document), encoding="utf-8")


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


def _cloudformation(env_ref: list[Env]) -> Any:  # noqa: ANN401
    """A real CloudFormation client whose DescribeStacks answers come from the test's state."""
    client = boto3.client(
        "cloudformation", region_name=REGION, aws_access_key_id="stub", aws_secret_access_key="stub"
    )
    stubber = Stubber(client)

    def describe(params: dict[str, Any], **_: Any) -> None:  # noqa: ANN401
        env = env_ref[0]
        env.calls.append(("DescribeStacks", dict(params)))
        if (
            env.stack_error is not None
            or params.get("StackName") != f"CaseInsightsCoreStack-{UNIQUE}"
        ):
            stubber.add_client_error(
                "describe_stacks", service_error_code=env.stack_error or "ValidationError"
            )
            return
        stubber.add_response(
            "describe_stacks",
            {
                "Stacks": [
                    {
                        "StackName": params["StackName"],
                        "CreationTime": datetime(2026, 1, 1, tzinfo=UTC),
                        "StackStatus": env.stack_status,
                        "Outputs": [
                            {"OutputKey": k, "OutputValue": v} for k, v in env.outputs.items()
                        ],
                    }
                ]
            },
        )

    client.meta.events.register("provide-client-params.cloudformation.DescribeStacks", describe)
    stubber.activate()
    return client


@pytest.fixture
def env(
    script: ModuleType,
    aws_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Iterator[Env]:
    del aws_env
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        sqs = boto3.client("sqs", region_name=REGION)
        for bucket in (RAW, PROCESSED):
            s3.create_bucket(Bucket=bucket)
            s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        queue_url = sqs.create_queue(QueueName=QUEUE)["QueueUrl"]
        write_config(tmp_path)
        sfn = FakeStepFunctions()
        environment = Env(script, s3, sqs, sfn, queue_url, tmp_path, capsys)
        environment.outputs = {
            "RawBucketName": RAW,
            "ProcessedBucketName": PROCESSED,
            "CaseAnalysisStateMachineArn": STATE_MACHINE_ARN,
            "CaseSummaryQueueUrl": queue_url,
        }

        def record(params: dict[str, Any], event_name: str, **_: Any) -> None:  # noqa: ANN401
            environment.calls.append((event_name.rsplit(".", 1)[-1], dict(params)))

        for client in (s3, sqs):
            client.meta.events.register("provide-client-params", record)
        cloudformation = _cloudformation([environment])

        def clients(profile: str | None, region: str) -> Any:  # noqa: ANN401
            assert profile is None
            environment.regions.append(region)
            return script.Clients(
                sts=boto3.client("sts", region_name=region),
                cloudformation=cloudformation,
                s3=s3,
                sqs=sqs,
                sfn=sfn,
            )

        monkeypatch.setattr(script, "create_clients", clients)
        monkeypatch.setattr(script, "now", lambda: NOW)
        yield environment


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin", "AWS_EC2_METADATA_DISABLED": "true"},
    )


# --- help and option validation ----------------------------------------------------------------


def test_help_defines_date_field_timezone_bounds_versions_and_identity() -> None:
    result = run_cli("--help")
    assert result.returncode == 0
    text = " ".join(result.stdout.split())
    for phrase in (
        "--from",
        "--to",
        "--date-field {timeCreated,Case_Retrieval_Date}",
        "converted to UTC",
        "both are inclusive",
        "[--from 00:00:00Z, the day after --to 00:00:00Z)",
        'there is no "all records" default',
        "a range that matches nothing changes nothing",
        "--dry-run",
        "--confirm TOKEN",
        "delete markers",
        "No version is deleted by version ID",
        "fresh run ID and redrive ID",
        "never reuses a closed execution name",
        "managementAccountId",
    ):
        assert phrase in text, phrase


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--env", "test", "--to", "2026-01-31"], "--from"),
        (["--env", "test", "--from", "2026-01-01"], "--to"),
        (["--from", "2026-01-01", "--to", "2026-01-31"], "--env"),
        (["--env", "test", "--from", "20260101", "--to", "2026-01-31"], "not a YYYY-MM-DD date"),
        (["--env", "test", "--from", "2026-1-01", "--to", "2026-01-31"], "not a YYYY-MM-DD date"),
        (["--env", "test", "--from", "2026-02-30", "--to", "2026-03-01"], "not a valid calendar"),
        (["--env", "test", "--from", "2026-02-01", "--to", "2026-01-31"], "--from must not be"),
        (
            ["--env", "test", "--from", "2026-01-01", "--to", "2026-01-31", "--case", "1001"],
            "--case requires exactly one --account-id",
        ),
        (
            [
                "--env",
                "test",
                "--from",
                "2026-01-01",
                "--to",
                "2026-01-31",
                "--account-id",
                MEMBER_A,
                "--account-id",
                MEMBER_B,
                "--case",
                "1001",
            ],
            "--case requires exactly one --account-id",
        ),
        (
            ["--env", "test", "--from", "2026-01-01", "--to", "2026-01-31", "--account-id", "12"],
            "12-digit",
        ),
        (
            [
                "--env",
                "test",
                "--from",
                "2026-01-01",
                "--to",
                "2026-01-31",
                "--case",
                "../x",
                "--account-id",
                MEMBER_A,
            ],
            "display ID",
        ),
        (
            [
                "--env",
                "test",
                "--from",
                "2026-01-01",
                "--to",
                "2026-01-31",
                "--delay-seconds",
                "901",
            ],
            "between 0 and 900",
        ),
        (
            ["--env", "test", "--from", "2026-01-01", "--to", "2026-01-31", "--max-cases", "0"],
            "between 1 and 5000",
        ),
        (["--env", "../prod", "--from", "2026-01-01", "--to", "2026-01-31"], "--env must be"),
        (
            ["--env", "test", "--from", "2026-01-01", "--to", "9999-12-31"],
            "--to must be 9999-12-30 or earlier",
        ),
    ],
)
def test_invalid_options_fail_before_any_aws_client_exists(args: list[str], message: str) -> None:
    # The subprocess has no AWS credentials or configuration at all.
    result = run_cli(*args)
    assert result.returncode == 2
    assert message in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("start", "end"), [("2026-01-01", "9999-12-31"), ("9999-12-31", "9999-12-31")]
)
def test_the_last_representable_day_is_refused_before_any_aws_call(
    env: Env, start: str, end: str
) -> None:
    # The exclusive end is midnight after --to, which does not exist after 9999-12-31.
    seeded = env.seed("1001")

    code, out, err = env.run("--from", start, "--to", end, "--dry-run")

    assert code == 2
    assert "error: --to must be 9999-12-30 or earlier. Nothing was changed." in err
    assert out == ""
    assert env.regions == []
    assert env.calls == []
    assert env.unchanged(seeded)


def test_invalid_configuration_is_rejected_without_aws_calls(env: Env, tmp_path: Path) -> None:
    write_config(tmp_path, "mismatch", environment="prod")
    write_config(tmp_path, "badid", uniqueId="Bad_Id")
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    for name, message in (
        ("absent", "does not exist"),
        ("mismatch", "is not for environment mismatch"),
        ("badid", "no valid uniqueId"),
        ("broken", "not readable JSON"),
    ):
        code, out, err = env.run("--from", "2026-01-01", "--to", "2026-01-31", env=name)
        assert (code, out) == (2, "")
        assert message in err
        assert "Nothing was changed" in err
    assert env.regions == []


# --- target validation -------------------------------------------------------------------------


def _foreign_queue(env: Env) -> None:
    env.outputs["CaseSummaryQueueUrl"] = env.sqs.create_queue(
        QueueName="CaseInsights-CaseSummary-prod"
    )["QueueUrl"]


def _unversioned_bucket(env: Env) -> None:
    env.s3.create_bucket(Bucket="ci-unversioned-test")
    env.outputs["ProcessedBucketName"] = "ci-unversioned-test"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda e: write_config(e.config_dir, managementAccountId="999999999999"),
            "credentials are for account 123456789012",
        ),
        (lambda e: setattr(e, "stack_error", "ValidationError"), "was not found"),
        (lambda e: setattr(e, "stack_status", "ROLLBACK_COMPLETE"), "not in a usable state"),
        (lambda e: e.outputs.pop("CaseSummaryQueueUrl"), "no CaseSummaryQueueUrl output"),
        (
            lambda e: e.outputs.update(
                CaseAnalysisStateMachineArn=STATE_MACHINE_ARN.replace(ACCOUNT, "210987654321")
            ),
            "not this deployment's CaseAnalysis",
        ),
        (
            lambda e: e.outputs.update(
                CaseAnalysisStateMachineArn=STATE_MACHINE_ARN.replace("-test", "-prod")
            ),
            "not this deployment's CaseAnalysis",
        ),
        (_foreign_queue, "not this deployment's summary queue"),
        (_unversioned_bucket, "does not have versioning enabled"),
    ],
)
def test_foreign_or_unusable_targets_are_rejected_before_records_are_read(
    env: Env, change: Callable[[Env], object], message: str
) -> None:
    seeded = env.seed("1001")
    change(env)

    code, out, err = env.run("--from", "2026-01-01", "--to", "2026-12-31", "--confirm", "rp-x")

    assert code == 2
    assert message in err
    assert "Plan token" not in out
    assert not {"ListObjectsV2", "GetObject"} & set(env.operations())
    assert env.writes() == []
    assert env.unchanged(seeded)
    assert env.regions == [REGION]


# --- selection and preview ---------------------------------------------------------------------


def test_dry_run_selects_inclusive_utc_days_exactly_and_writes_nothing(env: Env) -> None:
    included = [
        env.seed("0901", "2026-03-01T00:00:00.000Z"),
        env.seed("0902", "2026-03-31T23:59:59.999Z"),
    ]
    excluded = [
        env.seed("0903", "2026-04-01T00:00:00.000Z"),
        env.seed("0904", "2026-02-28T23:59:59.999Z"),
        # 01:30 at UTC+2 on 1 March is 23:30 UTC on 28 February.
        env.seed("0905", "2026-03-01T01:30:00+02:00"),
    ]

    code, out, _ = env.run("--from", "2026-03-01", "--to", "2026-03-31", "--dry-run")

    assert code == 0
    assert "timeCreated (UTC) from 2026-03-01T00:00:00Z inclusive to 2026-04-01T00:00:00Z " in out
    assert "Examined 5 processed records: 2 in range, 3 outside the range (unchanged)" in out
    rows = [line.split() for line in out.splitlines() if line.startswith("requeue")]
    assert [(row[1], row[2]) for row in rows] == [(MEMBER_A, "0901"), (MEMBER_A, "0902")]
    assert [row[5] for row in rows] == [s.processed_version for s in included]
    assert re.search(r"Plan token: rp-[0-9a-f]{12} \(2 case\(s\) to change\)", out)
    assert "Dry run: no changes were made" in out
    assert env.writes() == []
    assert all(env.unchanged(s) for s in included + excluded)

    # The same records shift with the timezone conversion when February is selected.
    code, out, _ = env.run("--from", "2026-02-28", "--to", "2026-02-28", "--dry-run")
    assert code == 0
    assert [line.split()[2] for line in out.splitlines() if line.startswith("requeue")] == [
        "0904",
        "0905",
    ]


def test_retrieval_date_field_selects_on_the_committed_retrieval_time(env: Env) -> None:
    env.seed("1001", "2026-01-05T00:00:00Z", Case_Retrieval_Date="2026-05-01T00:05:00Z")
    env.seed("1002", "2026-05-01T00:00:00Z", Case_Retrieval_Date="2026-06-01T00:05:00Z")

    code, out, _ = env.run(
        "--from",
        "2026-05-01",
        "--to",
        "2026-05-01",
        "--date-field",
        "Case_Retrieval_Date",
        "--dry-run",
    )

    assert code == 0
    assert [line.split()[2] for line in out.splitlines() if line.startswith("requeue")] == ["1001"]
    assert "Case_Retrieval_Date (UTC) from 2026-05-01T00:00:00Z" in out


def test_empty_range_changes_nothing_and_never_widens_to_all_records(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = [env.seed("1001"), env.seed("2001", account=MEMBER_B)]

    def no_prompt(*_: object) -> str:
        raise AssertionError("an empty plan must not ask for confirmation")

    monkeypatch.setattr("builtins.input", no_prompt)
    for scope in ([], ["--account-id", "777788889999"], ["--account-id", MEMBER_A, "--case", "9"]):
        code, out, _ = env.run("--from", "2025-01-01", "--to", "2025-01-02", *scope)
        assert code == 0
        assert "Nothing to reprocess. No changes were made." in out
        assert "Plan token" not in out
        assert env.writes() == []
    assert all(env.unchanged(s) for s in seeded)


def test_scope_limits_listing_to_the_named_accounts_and_cases(env: Env) -> None:
    env.seed("1001")
    env.seed("1002")
    env.seed("2001", account=MEMBER_B)

    code, out, _ = env.run(
        "--from", "2026-01-01", "--to", "2026-12-31", "--account-id", MEMBER_A, "--dry-run"
    )
    assert code == 0
    assert [line.split()[2] for line in out.splitlines() if line.startswith("requeue")] == [
        "1001",
        "1002",
    ]
    prefixes = [p["Prefix"] for name, p in env.calls if name == "ListObjectsV2" and "Prefix" in p]
    assert f"account_number={MEMBER_B}/" not in prefixes
    assert not any(p == "account_number=" for p in prefixes)

    code, out, _ = env.run(
        "--from",
        "2026-01-01",
        "--to",
        "2026-12-31",
        "--account-id",
        MEMBER_A,
        "--case",
        "1001",
        "--case",
        "9999",
        "--dry-run",
    )
    assert code == 0
    assert [line.split()[2] for line in out.splitlines() if line.startswith("requeue")] == ["1001"]
    assert f"no processed record: {case_record_key(MEMBER_A, '9999')}" in out
    assert "ListObjectsV2" not in env.operations()


def test_unreadable_records_are_reported_and_never_selected(env: Env) -> None:
    good = env.seed("1001")
    env.put(PROCESSED, case_record_key(MEMBER_A, "1002"), "{not json")
    env.put(
        PROCESSED, case_record_key(MEMBER_A, "1003"), make_record("1003", MEMBER_A, timeCreated="")
    )
    env.put(PROCESSED, case_record_key(MEMBER_A, "1004"), make_record("9999", MEMBER_A))

    code, out, _ = env.run("--from", "2026-01-01", "--to", "2026-12-31", "--dry-run")

    assert code == 0
    assert "Examined 4 processed records: 1 in range, 0 outside the range (unchanged), 3 " in out
    assert "1002/data.json (record is not readable JSON)" in out
    assert "1003/data.json (timeCreated is missing or not a timestamp" in out
    assert "1004/data.json (record displayId does not match its key)" in out
    assert [line.split()[2] for line in out.splitlines() if line.startswith("requeue")] == ["1001"]
    assert env.unchanged(good)


def test_plans_larger_than_max_cases_are_refused(env: Env) -> None:
    seeded = [env.seed(display) for display in ("1001", "1002", "1003")]

    code, out, err = env.run(
        "--from", "2026-01-01", "--to", "2026-12-31", "--max-cases", "2", "--confirm", "rp-x"
    )

    assert code == 2
    assert "3 cases would change, more than --max-cases 2" in err
    assert "Plan token" not in out
    assert env.writes() == []
    assert all(env.unchanged(s) for s in seeded)


# --- confirmation ------------------------------------------------------------------------------


def test_missing_wrong_or_declined_confirmation_writes_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = env.seed("1001")
    window = ("--from", "2026-01-01", "--to", "2026-12-31")
    token = env.token(*window)

    code, _, err = env.run(*window)
    assert code == 3
    assert f"Confirmation required: rerun with --confirm {token}" in err

    code, _, err = env.run(*window, "--confirm", "rp-000000000000")
    assert code == 3
    assert "does not match this plan" in err

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    for reply in ("yes", "", f" {token}x"):
        monkeypatch.setattr("builtins.input", lambda _prompt, reply=reply: reply)
        code, _, err = env.run(*window)
        assert code == 3
        assert "Cancelled. Nothing was changed." in err

    def interrupted(_prompt: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupted)
    assert env.run(*window)[0] == 3
    assert env.writes() == []
    assert env.unchanged(seeded)

    prompts: list[str] = []

    def accept(prompt: str) -> str:
        prompts.append(prompt)
        return token

    monkeypatch.setattr("builtins.input", accept)
    code, out, _ = env.run(*window)
    assert code == 0
    assert prompts == [f"Type the plan token {token} to reprocess 1 case(s): "]
    assert "requeued" in out


def test_a_record_set_that_changed_after_the_preview_needs_a_new_token(env: Env) -> None:
    first = env.seed("1001")
    window = ("--from", "2026-01-01", "--to", "2026-12-31")
    token = env.token(*window)
    # A new record in range, and a new version of the previewed one.
    env.seed("1002")
    env.put(PROCESSED, first.key, make_record("1001", MEMBER_A))

    code, _, err = env.run(*window, "--confirm", token)

    assert code == 3
    assert "does not match this plan" in err
    assert env.writes() == []


# --- execution ---------------------------------------------------------------------------------


def test_confirmed_requeue_supersedes_only_selected_records_with_fresh_identity(
    env: Env,
) -> None:
    selected = env.seed("1001", "2026-03-10T08:00:00.000Z")
    outside = env.seed("1002", "2026-04-10T08:00:00.000Z")
    window = ("--from", "2026-03-01", "--to", "2026-03-31")
    token = env.token(*window)

    code, out, _ = env.run(*window, "--confirm", token)

    assert code == 0
    assert f"requeued    {MEMBER_A} 1001 previous version {selected.processed_version}" in out
    assert "Totals: requeued 1" in out
    assert "Queued references only start new executions" in out
    # The processed record and checkpoints are superseded by delete markers; no version is
    # deleted by ID, so the previous record stays restorable and nothing older can resurface.
    assert env.removed(PROCESSED, selected.key)
    assert ("version", selected.processed_version, False) in env.versions(PROCESSED, selected.key)
    for stage in STAGES:
        assert env.removed(RAW, checkpoint_key(MEMBER_A, "1001", stage))
    deletes = [p for name, p in env.calls if name == "DeleteObject"]
    assert deletes
    assert all("VersionId" not in p for p in deletes)
    # The raw inputs and ownership marker stay for the new execution.
    for key in (
        case_record_key(MEMBER_A, "1001"),
        communications_key(MEMBER_A, "1001"),
        marker_key(MEMBER_A, "1001"),
    ):
        assert env.versions(RAW, key)[0][0] == "version"
    # Records outside the range are untouched, checkpoints included.
    assert env.unchanged(outside)
    for stage in STAGES:
        assert env.versions(RAW, checkpoint_key(MEMBER_A, "1002", stage))[0][0] == "version"

    [(message, delay)] = env.sent()
    assert delay == 60
    ref = SummaryRef.parse(message)
    assert (ref.account_id, ref.case_id, ref.display_id) == (MEMBER_A, selected.case_id, "1001")
    assert ref.communications_version_id == selected.communications_version
    assert ref.communication_count == 2
    assert RUN_ID.match(ref.run_id)
    assert ref.run_id != HISTORIC_RUN
    assert ref.redrive_id is not None
    assert ref.redrive_id.startswith("reprocess-")
    assert f"Reprocess run ID {ref.run_id}, redrive ID {ref.redrive_id}" in out
    # The new attempt's execution name was never used by a closed execution.
    assert execution_name(MEMBER_A, "1001", ref.run_id, 1) not in env.sfn.executions
    # The message is sent before the record is removed.
    operations = env.operations()
    assert operations.index("SendMessage") < operations.index("DeleteObject")
    assert env.sfn.started() == []


def test_removal_that_outlasts_the_delay_sends_one_follow_up_reference(
    env: Env, script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.seed("1001")
    window = ("--from", "2026-01-01", "--to", "2026-12-31")
    token = env.token(*window)
    ticks = iter([1000.0, 1056.0])
    monkeypatch.setattr(script, "monotonic", lambda: next(ticks))

    code, _, _ = env.run(*window, "--confirm", token)

    assert code == 0
    [(first, first_delay), (follow_up, follow_up_delay)] = env.sent()
    assert (first_delay, follow_up_delay) == (60, 0)
    assert follow_up["redriveId"] == f"{first['redriveId']}-f"
    assert {k: v for k, v in follow_up.items() if k != "redriveId"} == {
        k: v for k, v in first.items() if k != "redriveId"
    }
    operations = env.operations()
    assert operations.index("DeleteObject") < len(operations) - 1 - operations[::-1].index(
        "SendMessage"
    )


def test_zero_delay_always_follows_removal_with_a_reference(env: Env) -> None:
    env.seed("1001")
    window = ("--from", "2026-01-01", "--to", "2026-12-31")
    token = env.token(*window)

    code, _, _ = env.run(*window, "--confirm", token, "--delay-seconds", "0")

    assert code == 0
    assert [delay for _, delay in env.sent()] == [0, 0]


def test_active_unknown_and_starting_work_is_left_unchanged(env: Env) -> None:
    running = env.seed("2001", marker="RUNNING")
    pending = env.seed("2002", marker="PENDING_REDRIVE")
    resetting = env.seed("2003", marker=None)
    claim = ResetClaim(MEMBER_A, "2003", NOW - timedelta(minutes=5))
    env.put(RAW, claim.key, claim.to_document())
    listed = env.seed("2004", marker=None)
    env.sfn.add(execution_name(MEMBER_A, "2004", "20261114T000000Z", 1), "RUNNING")
    unreadable = env.seed("2005", marker=None)
    env.put(RAW, marker_key(MEMBER_A, "2005"), "{broken")
    starting = env.seed("2006", marker=None)
    seeded_ref = SummaryRef(MEMBER_A, starting.case_id, "2006", HISTORIC_RUN, "v1", 2)
    fresh = OwnershipMarker.claim(seeded_ref, 2, NOW - timedelta(minutes=10))
    env.put(RAW, fresh.key, fresh.to_document())
    stale_reset = env.seed("2007", marker=None)
    old_claim = ResetClaim(MEMBER_A, "2007", NOW - timedelta(hours=3))
    env.put(RAW, old_claim.key, old_claim.to_document())
    eligible = env.seed("2008")
    window = ("--from", "2026-01-01", "--to", "2026-12-31")

    code, out, _ = env.run(*window, "--dry-run")
    assert code == 0
    reasons = {
        line.split()[2]: " ".join(line.split()[6:])
        for line in out.splitlines()
        if line.startswith(("skip", "requeue"))
    }
    assert reasons == {
        "2001": "a RUNNING CaseAnalysis execution belongs to the case",
        "2002": f"execution {pending.execution} is PENDING_REDRIVE",
        "2003": "a Case Cleanup reset or another reprocess is in progress",
        "2004": "a RUNNING CaseAnalysis execution belongs to the case",
        "2005": "the ownership marker is unreadable; status unknown",
        "2006": f"execution {fresh.execution_name} is being started",
        "2007": "raw inputs available; a new execution is queued",
        "2008": "raw inputs available; a new execution is queued",
    }

    code, out, _ = env.run(*window, "--confirm", env.token(*window))
    assert code == 0
    assert "Totals: requeued 2" in out
    for seeded in (running, pending, resetting, listed, unreadable, starting):
        assert env.unchanged(seeded)
        for stage in STAGES:
            key = checkpoint_key(MEMBER_A, seeded.display, stage)
            assert env.versions(RAW, key)[0][0] == "version"
    assert env.removed(PROCESSED, stale_reset.key)
    assert env.removed(PROCESSED, eligible.key)
    assert sorted(m["displayId"] for m, _ in env.sent()) == ["2007", "2008"]


def test_status_lookup_failures_skip_the_case_as_unknown(env: Env) -> None:
    seeded = env.seed("1001")
    env.sfn.failures["describe"] = sfn_error("ThrottlingException", "DescribeExecution")
    window = ("--from", "2026-01-01", "--to", "2026-12-31")

    code, out, _ = env.run(*window, "--confirm", "rp-x")

    assert code == 0
    assert "execution status unknown (ThrottlingException)" in out
    assert "Nothing to reprocess" in out
    assert env.unchanged(seeded)


def test_work_that_became_active_after_confirmation_is_skipped(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = env.seed("1001")
    window = ("--from", "2026-01-01", "--to", "2026-12-31")
    token = env.token(*window)
    assert seeded.execution is not None
    execution = env.sfn.executions[seeded.execution]

    def confirm_while_a_redrive_starts(_prompt: str) -> str:
        execution.status = "RUNNING"
        return token

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", confirm_while_a_redrive_starts)
    code, out, _ = env.run(*window)

    assert code == 0
    assert f"skipped     {MEMBER_A} 1001" in out
    assert "a RUNNING CaseAnalysis execution belongs to the case" in out
    assert env.writes() == []
    assert env.unchanged(seeded)


def test_expired_raw_inputs_use_rediscovery_only_inside_the_lookback_window(env: Env) -> None:
    recent = env.seed("3001", "2026-09-01T00:00:00Z", raw=False)
    old = env.seed("3002", "2025-11-15T00:00:00Z", raw=False)
    window = ("--from", "2025-01-01", "--to", "2026-12-31")

    code, out, _ = env.run(*window, "--dry-run")
    assert code == 0
    assert "raw inputs expired; the next scheduled run collects it" in out
    assert "raw inputs expired and the case is outside the lookback window; record kept" in out

    code, out, _ = env.run(*window, "--confirm", env.token(*window))
    assert code == 0
    assert f"rediscover  {MEMBER_A} 3001" in out
    assert env.removed(PROCESSED, recent.key)
    assert env.unchanged(old)
    assert env.sent() == []


def test_inconsistent_raw_conversation_is_never_requeued(env: Env) -> None:
    seeded = env.seed("4001", "2026-09-01T00:00:00Z", marker=None)
    conversation = {
        "accountId": MEMBER_A,
        "caseId": seeded.case_id,
        "displayId": "4001",
        "communicationCount": 5,
        "communications": [],
    }
    env.put(RAW, communications_key(MEMBER_A, "4001"), conversation)

    code, out, _ = env.run("--from", "2026-01-01", "--to", "2026-12-31", "--dry-run")

    assert code == 0
    assert "rediscover" in out
    assert "requeue " not in out


def _fail(env: Env, operation: str, matches: Callable[[dict[str, Any]], bool]) -> None:
    def hook(params: dict[str, Any], **_: Any) -> None:  # noqa: ANN401
        if matches(params):
            raise ClientError(
                {"Error": {"Code": "InternalError", "Message": "synthetic"}}, operation
            )

    client = env.sqs if operation == "SendMessage" else env.s3
    service = "sqs" if operation == "SendMessage" else "s3"
    client.meta.events.register(f"provide-client-params.{service}.{operation}", hook)


def test_partial_failures_are_reported_and_failed_cases_keep_their_records(env: Env) -> None:
    done = env.seed("4001")
    unsent = env.seed("4002")
    undeleted = env.seed("4003")
    window = ("--from", "2026-01-01", "--to", "2026-12-31")
    token = env.token(*window)
    _fail(env, "SendMessage", lambda p: '"displayId":"4002"' in p["MessageBody"])
    _fail(env, "DeleteObject", lambda p: "case_number=4003/checkpoints/rca" in p["Key"])

    code, out, _ = env.run(*window, "--confirm", token)

    assert code == 1
    assert f"requeued    {MEMBER_A} 4001" in out
    assert f"failed      {MEMBER_A} 4002" in out
    assert "summary reference not sent (InternalError); nothing changed" in out
    assert f"failed      {MEMBER_A} 4003" in out
    assert "removal failed (InternalError); the processed record was kept" in out
    assert "Totals: failed 2, requeued 1" in out
    assert env.removed(PROCESSED, done.key)
    assert env.unchanged(unsent)
    assert env.unchanged(undeleted)
    for stage in STAGES:
        assert env.versions(RAW, checkpoint_key(MEMBER_A, "4002", stage))[0][0] == "version"


def test_follow_up_send_failure_prints_the_reference_to_send(
    env: Env, script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.seed("4001")
    window = ("--from", "2026-01-01", "--to", "2026-12-31")
    token = env.token(*window)
    _fail(env, "SendMessage", lambda p: p.get("DelaySeconds") == 0)
    ticks = iter([0.0, 100.0])
    monkeypatch.setattr(script, "monotonic", lambda: next(ticks))

    code, out, _ = env.run(*window, "--confirm", token)

    assert code == 1
    assert "record removed but the follow-up reference was not sent (InternalError)" in out
    printed = json.loads(
        out.split("send this message to the summary queue: ", 1)[1].splitlines()[0]
    )
    assert SummaryRef.parse(printed).display_id == "4001"


def test_a_record_replaced_during_reprocessing_is_kept(env: Env) -> None:
    seeded = env.seed("4101")
    window = ("--from", "2026-01-01", "--to", "2026-12-31")
    token = env.token(*window)
    replaced: list[str] = []

    def replace_record(params: dict[str, Any], **_: Any) -> None:  # noqa: ANN401
        del params
        if not replaced:
            rewritten = make_record("4101", MEMBER_A, Case_Summary=f"{SENTINEL} rewritten.")
            replaced.append(env.put(PROCESSED, seeded.key, rewritten))

    env.sqs.meta.events.register("provide-client-params.sqs.SendMessage", replace_record)

    code, out, _ = env.run(*window, "--confirm", token)

    assert code == 0
    assert "the processed record changed during reprocessing; record kept" in out
    assert env.versions(PROCESSED, seeded.key)[0] == ("version", replaced[0], True)
    # The rewritten record keeps the checkpoints it was made from.
    for stage in STAGES:
        assert env.versions(RAW, checkpoint_key(MEMBER_A, "4101", stage))[0][0] == "version"
    deleted = [p["Key"] for name, p in env.calls if name == "DeleteObject"]
    assert [key for key in deleted if not key.endswith("/workflow.json")] == []


# --- overlapping work, slow acknowledgements and permissions ------------------------------------


def start_workflow(env: Env, message: dict[str, Any]) -> dict[str, Any]:
    """Hand a summary reference to the real Workflow Starter decision logic."""
    settings = starter.Settings(RAW, PROCESSED, STATE_MACHINE_ARN, timedelta(minutes=15))
    clients = starter.Clients(s3=env.s3, sfn=env.sfn)  # type: ignore[arg-type]
    result: dict[str, Any] = starter.reconcile_case(SummaryRef.parse(message), settings, clients)
    return result


@pytest.fixture
def starter_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(starter, "now", lambda: NOW + timedelta(minutes=2))


@pytest.mark.usefixtures("starter_clock")
@pytest.mark.parametrize("workflow_started", [True, False], ids=["started", "not-yet-started"])
def test_an_overlapping_reprocess_never_touches_the_work_the_first_one_queued(
    env: Env, script: ModuleType, monkeypatch: pytest.MonkeyPatch, *, workflow_started: bool
) -> None:
    seeded = env.seed("5001")
    window = ("--from", "2026-01-01", "--to", "2026-12-31")
    token = env.token(*window)
    marker = marker_key(MEMBER_A, "5001")
    original_marker = env.s3.get_object(Bucket=RAW, Key=marker)["Body"].read()
    summary_checkpoint = checkpoint_key(MEMBER_A, "5001", STAGES[0])
    first: dict[str, Any] = {}
    recheck = script.Executor._recheck

    def overlap(self: Any, planned: Any) -> Any:  # noqa: ANN401
        # The second operator's last check has passed; the first operator's whole reprocess,
        # and the execution its reference starts, run before the second one changes anything.
        result = recheck(self, planned)
        if first:
            return result
        first["mark"] = len(env.calls)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            first["code"] = script.main(
                ["--env", UNIQUE, "--config-dir", str(env.config_dir), *window, "--confirm", token]
            )
        first["out"] = output.getvalue()
        [message] = [
            json.loads(p["MessageBody"])
            for name, p in env.calls[first["mark"] :]
            if name == "SendMessage"
        ]
        first["message"] = message
        if workflow_started:
            assert start_workflow(env, message)["outcome"] == "started"
            first["checkpoint"] = env.put(RAW, summary_checkpoint, {"synthetic": "new run"})
            first["marker"] = env.s3.get_object(Bucket=RAW, Key=marker)["Body"].read()
        first["after"] = len(env.calls)
        return result

    monkeypatch.setattr(script.Executor, "_recheck", overlap)
    code, out, _ = env.run(*window, "--confirm", token)

    assert first["code"] == 0
    assert f"requeued    {MEMBER_A} 5001" in first["out"]
    assert code == 0
    if workflow_started:
        # The starter's new marker defeats the second reprocess's conditional claim.
        assert "the case's ownership changed since the check" in _line(out, "5001")
    else:
        # The restored marker lets the claim through; the recheck under the claim stops it.
        assert "the processed record changed since the preview" in _line(out, "5001")
    assert f"skipped     {MEMBER_A} 5001" in out
    second = env.calls[first["after"] :]
    assert [p for name, p in second if name == "SendMessage"] == []
    deleted = [p["Key"] for name, p in second if name == "DeleteObject"]
    assert [key for key in deleted if not key.endswith("/workflow.json")] == []
    assert env.removed(PROCESSED, seeded.key)
    current_marker = env.s3.get_object(Bucket=RAW, Key=marker)["Body"].read()
    if workflow_started:
        # The running execution keeps its checkpoint and its ownership marker.
        assert env.versions(RAW, summary_checkpoint)[0] == ("version", first["checkpoint"], True)
        assert current_marker == first["marker"]
    else:
        # The ownership marker is back as it was, and the queued reference still starts the
        # new execution.
        assert json.loads(current_marker) == json.loads(original_marker)
        assert start_workflow(env, first["message"])["outcome"] == "started"


@pytest.mark.usefixtures("starter_clock")
def test_a_slowly_acknowledged_reference_consumed_early_still_leaves_one_pending_reference(
    env: Env, script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = env.seed("5101")
    window = ("--from", "2026-01-01", "--to", "2026-12-31", "--delay-seconds", "6")
    token = env.token(*window)
    clock = [1000.0]
    monkeypatch.setattr(script, "monotonic", lambda: clock[0])
    answered: list[str] = []

    def accepted_then_slow_response(**_: Any) -> None:  # noqa: ANN401
        if answered:
            return
        # SQS accepted the message and its delay ran out before the response arrived; the
        # starter consumed it while the processed record still existed.
        [message] = [json.loads(p["MessageBody"]) for n, p in env.calls if n == "SendMessage"]
        answered.append(start_workflow(env, message)["outcome"])
        env.sqs.purge_queue(QueueUrl=env.queue_url)
        clock[0] += 7

    env.sqs.meta.events.register("after-call.sqs.SendMessage", accepted_then_slow_response)
    code, out, _ = env.run(*window, "--confirm", token)

    assert answered == ["alreadyProcessed"]
    assert code == 0
    assert f"requeued    {MEMBER_A} 5101" in out
    assert env.removed(PROCESSED, seeded.key)
    pending = env.sqs.receive_message(QueueUrl=env.queue_url, MaxNumberOfMessages=10).get(
        "Messages", []
    )
    [reference] = [json.loads(m["Body"]) for m in pending]
    assert str(reference["redriveId"]).endswith("-f")
    assert start_workflow(env, reference)["outcome"] == "started"


def _deny(env: Env, operation: str, matches: Callable[[dict[str, Any]], bool], code: str) -> None:
    def hook(params: dict[str, Any], **_: Any) -> None:  # noqa: ANN401
        if matches(params):
            raise ClientError(
                {
                    "Error": {"Code": code, "Message": "synthetic"},
                    "ResponseMetadata": {"HTTPStatusCode": 403},  # type: ignore[typeddict-item]
                },
                operation,
            )

    env.s3.meta.events.register(f"provide-client-params.s3.{operation}", hook)


def _line(out: str, display: str) -> str:
    """The last line about `display`: its preview line, or its report line after execution."""
    return [line for line in out.splitlines() if f" {display} " in line][-1]


def test_a_denied_versioned_conversation_read_is_a_permission_error_not_expired_inputs(
    env: Env,
) -> None:
    seeded = env.seed("6001", marker=None)
    _deny(
        env,
        "GetObject",
        lambda p: "VersionId" in p and p["Key"].endswith("/communications.json"),
        "AccessDenied",
    )
    window = ("--from", "2026-01-01", "--to", "2026-12-31")

    code, out, _ = env.run(*window, "--dry-run")

    assert code == 0
    line = _line(out, "6001")
    assert line.startswith("skip ")
    assert "permission denied (AccessDenied" in line
    assert "s3:GetObjectVersion" in line
    assert "Nothing to reprocess" in out
    assert env.unchanged(seeded)


def test_a_403_for_absent_raw_inputs_is_reported_as_a_missing_list_permission(env: Env) -> None:
    seeded = env.seed("6002", "2026-09-01T00:00:00Z", raw=False)
    _deny(env, "HeadObject", lambda p: p["Bucket"] == RAW, "403")
    window = ("--from", "2026-01-01", "--to", "2026-12-31")

    code, out, _ = env.run(*window, "--dry-run")

    assert code == 0
    line = _line(out, "6002")
    assert line.startswith("skip ")
    assert "permission denied (403" in line
    assert "s3:ListBucket" in line
    assert env.unchanged(seeded)


def test_a_403_while_removing_checkpoints_fails_the_case_as_a_permission_error(env: Env) -> None:
    seeded = env.seed("6003", checkpoints=False)
    window = ("--from", "2026-01-01", "--to", "2026-12-31")
    token = env.token(*window)
    marker = marker_key(MEMBER_A, "6003")
    original_marker = env.s3.get_object(Bucket=RAW, Key=marker)["Body"].read()
    _deny(env, "HeadObject", lambda p: "/checkpoints/" in p["Key"], "403")

    code, out, _ = env.run(*window, "--confirm", token)

    assert code == 1
    line = _line(out, "6003")
    assert line.startswith("failed ")
    assert "permission denied (403" in line
    assert "s3:ListBucket" in line
    assert env.unchanged(seeded)
    restored = env.s3.get_object(Bucket=RAW, Key=marker)["Body"].read()
    assert json.loads(restored) == json.loads(original_marker)


def test_output_keys_match_the_core_stack_outputs(script: ModuleType) -> None:
    snapshot_file = REPO_ROOT / "test" / "cdk" / "__snapshots__" / "app.test.ts.snap"
    snapshot = snapshot_file.read_text("utf-8")
    core_snapshots = snapshot.count("core stack template snapshot 1`] = `")
    assert core_snapshots == 3  # dev, prod and Core-only
    for key in script.CORE_OUTPUTS.values():
        assert snapshot.count(f'\n    "{key}": {{\n') == core_snapshots, key

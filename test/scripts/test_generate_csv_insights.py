"""CSV export CLI: real entry point against synthetic processed records in mocked S3."""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import os
import runpy
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from botocore.model import OperationModel
from botocore.stub import Stubber
from common.processed_record import PROCESSED_FIELDS
from common.taxonomy import LIFECYCLE, RCA
from moto import mock_aws

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "generate_csv_insights.py"
BUCKET = "case-insights-processed-test"
ACCOUNT_A = "111122223333"
ACCOUNT_B = "444455556666"
SENTINEL = "SENTINEL-CASE-TEXT-91c2"
HEADER = ["account_number", *PROCESSED_FIELDS]


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_csv_insights", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script() -> ModuleType:
    return load_script()


def run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=REPO_ROOT,
    )


def make_record(display_id: str, **overrides: str | None) -> dict[str, str | None]:
    record: dict[str, str | None] = {
        "caseId": f"case-{display_id}-synthetic-2026",
        "displayId": display_id,
        "subject": f"Synthetic subject {display_id}",
        "serviceCode": "amazon-elastic-compute-cloud-linux",
        "categoryCode": "instance-issue",
        "severityCode": "low",
        "submittedBy": "builder@example.com",
        "timeCreated": "2026-06-01T10:00:00.000Z",
        "Case_Retrieval_Date": "2026-07-01T00:05:00Z",
        "Case_Summary": f"Synthetic summary for case {display_id}.",
        "RCA_Category": RCA.labels[0],
        "RCA_Reason": "Synthetic RCA reason.",
        "RCA_Retrieval_Date": "2026-07-01T00:06:00Z",
        "Lifecycle_Category": LIFECYCLE.labels[0],
        "Lifecycle_Reason": "Synthetic lifecycle reason.",
        "Lifecycle_Retrieval_Date": "2026-07-01T00:07:00Z",
        "Model_Id": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        "Prompt_Version": "pv-0123456789abcdef",
    }
    record.update(overrides)
    return record


def record_key(account: str, display_id: str) -> str:
    return f"account_number={account}/case_number={display_id}/data.json"


def put(account: str, record: dict[str, Any] | str, display_id: str | None = None) -> str:
    if isinstance(record, dict):
        display_id = display_id or str(record["displayId"])
        body = json.dumps(record)
    else:
        assert display_id is not None
        body = record
    key = record_key(account, display_id)
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=BUCKET, Key=key, Body=body.encode()
    )
    return key


def parse_csv(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    assert reader.fieldnames == HEADER
    return list(reader)


def expected_row(account: str, record: dict[str, str | None]) -> dict[str, str]:
    return {"account_number": account} | {k: "" if v is None else v for k, v in record.items()}


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def s3_bucket(aws_env: None) -> Iterator[str]:
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield BUCKET


@pytest.fixture
def s3_calls(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, dict[str, Any]]]:
    """Record every S3 API call the CLI makes through its real client factory."""
    calls: list[tuple[str, dict[str, Any]]] = []
    original = script.create_s3_client

    def factory(region: str | None, profile: str | None) -> S3Client:
        client: S3Client = original(region, profile)

        def record(params: dict[str, Any], model: OperationModel, **_: object) -> None:
            calls.append((model.name, dict(params)))

        client.meta.events.register("provide-client-params.s3.*", record)
        return client

    monkeypatch.setattr(script, "create_s3_client", factory)
    return calls


def export(
    script: ModuleType, tmp_path: Path, *args: str, capsys: pytest.CaptureFixture[str]
) -> tuple[int, list[dict[str, str]] | None, str]:
    out = tmp_path / "export.csv"
    code = script.main(["--bucket", BUCKET, "--output", str(out), *args])
    err = capsys.readouterr().err
    rows = parse_csv(out.read_text(encoding="utf-8")) if out.exists() else None
    return code, rows, err


# --- help, argument validation and contract ------------------------------------------------


def test_help_is_usable_without_aws() -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    result = run_cli("--help", env=env)
    assert result.returncode == 0, result.stderr
    for option in (
        "--bucket",
        "--output",
        "--account-id",
        "--start-date",
        "--end-date",
        "--date-field",
        "--rca-category",
        "--lifecycle-category",
        "--service-code",
        "--severity-code",
        "--max-records",
        "--page-size",
        "--max-workers",
        "--skip-invalid",
        "--overwrite",
        "--encoding",
        "--no-escape-formulas",
        "--region",
        "--profile",
    ):
        assert option in result.stdout
    assert "s3:ListBucket" in result.stdout
    assert "s3:GetObject" in result.stdout
    assert "Athena is not" in result.stdout


def test_csv_fields_follow_processed_record_schema(script: ModuleType) -> None:
    assert list(script.CSV_FIELDS) == HEADER


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "--bucket"),
        (["--bucket", "Bad_Bucket!"], "bucket name"),
        (["--bucket", BUCKET, "--account-id", "12345"], "12-digit"),
        (["--bucket", BUCKET, "--start-date", "2026-13-01"], "YYYY-MM-DD"),
        (["--bucket", BUCKET, "--start-date", "2026-06-02", "--end-date", "2026-06-01"], "after"),
        (["--bucket", BUCKET, "--date-field", "subject"], "invalid choice"),
        (["--bucket", BUCKET, "--rca-category", "Not A Category"], "RCA category"),
        (["--bucket", BUCKET, "--lifecycle-category", "Nope"], "lifecycle category"),
        (["--bucket", BUCKET, "--service-code", ""], "must not be empty"),
        (["--bucket", BUCKET, "--page-size", "0"], "between 1 and 1000"),
        (["--bucket", BUCKET, "--page-size", "1001"], "between 1 and 1000"),
        (["--bucket", BUCKET, "--max-records", "0"], "between 1 and 1000000"),
        (["--bucket", BUCKET, "--max-workers", "65"], "between 1 and 32"),
        (["--bucket", BUCKET, "--encoding", "latin-1"], "invalid choice"),
    ],
)
def test_invalid_options_are_rejected_before_any_aws_call(args: list[str], message: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    result = run_cli(*args, env=env)
    assert result.returncode == 2
    assert message in result.stderr
    assert "Traceback" not in result.stderr


def test_existing_output_is_not_overwritten_without_flag(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "existing.csv"
    out.write_text("keep me\n")
    code = script.main(["--bucket", BUCKET, "--output", str(out)])
    assert code == 2
    assert "--overwrite" in capsys.readouterr().err
    assert out.read_text() == "keep me\n"


def test_missing_output_directory_is_rejected(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = script.main(["--bucket", BUCKET, "--output", str(tmp_path / "nope" / "x.csv")])
    assert code == 2
    assert "directory" in capsys.readouterr().err


# --- export behaviour ----------------------------------------------------------------------


def test_multipage_export_contains_every_record_and_field_exactly_once(
    script: ModuleType,
    s3_bucket: str,
    s3_calls: list[tuple[str, dict[str, Any]]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    records = {
        ACCOUNT_A: [
            make_record("00012345", subject=None, submittedBy=None),
            make_record("20000000002", timeCreated="2026-01-15T08:30:00Z"),
            make_record("20000000003", RCA_Category="Unclassified"),
            make_record("20000000004"),
        ],
        ACCOUNT_B: [
            make_record("30000000001", serviceCode="amazon-simple-storage-service"),
            make_record("30000000002", Case_Summary='Quoted "value", with comma\nand newline.'),
            make_record("30000000003"),
        ],
    }
    for account, items in records.items():
        for item in items:
            put(account, item)
    client = boto3.client("s3", region_name="us-east-1")
    client.put_object(Bucket=BUCKET, Key=f"account_number={ACCOUNT_B}/notes.txt", Body=b"x")
    client.put_object(
        Bucket=BUCKET, Key=f"account_number={ACCOUNT_A}/case_number=1/x.json", Body=b"x"
    )

    code, rows, err = export(
        script, tmp_path, "--page-size", "2", "--max-workers", "4", capsys=capsys
    )

    assert code == 0, err
    assert rows is not None
    expected = [expected_row(a, r) for a, items in records.items() for r in items]
    assert sorted(rows, key=lambda r: (r["account_number"], r["displayId"])) == sorted(
        expected, key=lambda r: (r["account_number"], r["displayId"])
    )
    assert len({(r["account_number"], r["displayId"]) for r in rows}) == 7
    lists = [params for name, params in s3_calls if name == "ListObjectsV2"]
    assert len(lists) == 5  # 9 objects at 2 keys per page
    assert all(params["MaxKeys"] == 2 for params in lists)
    assert sum(1 for name, _ in s3_calls if name == "GetObject") == 7
    assert "Exported 7 records" in err
    assert "2 non-record objects ignored" in err


def test_rows_are_sorted_by_account_creation_time_and_case(
    script: ModuleType, s3_bucket: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    put(ACCOUNT_B, make_record("9", timeCreated="2026-01-01T00:00:00Z"))
    put(ACCOUNT_A, make_record("2", timeCreated="2026-03-01T00:00:00Z"))
    put(ACCOUNT_A, make_record("1", timeCreated="2026-03-01T00:00:00Z"))
    put(ACCOUNT_A, make_record("3", timeCreated="2026-02-01T00:00:00Z"))
    code, rows, _ = export(script, tmp_path, capsys=capsys)
    assert code == 0
    assert rows is not None
    assert [(r["account_number"], r["displayId"]) for r in rows] == [
        (ACCOUNT_A, "3"),
        (ACCOUNT_A, "1"),
        (ACCOUNT_A, "2"),
        (ACCOUNT_B, "9"),
    ]


def test_stubbed_continuation_tokens_are_followed_once_and_keys_deduplicated(
    script: ModuleType,
    aws_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = boto3.client("s3", region_name="us-east-1")
    stubber = Stubber(client)
    pages = [
        (None, "token-1", ["1", "2"]),
        ("token-1", "token-2", ["3", "2"]),
        ("token-2", None, ["4"]),
    ]
    for token, next_token, ids in pages:
        expected: dict[str, Any] = {"Bucket": BUCKET, "Prefix": "account_number=", "MaxKeys": 2}
        if token:
            expected["ContinuationToken"] = token
        response: dict[str, Any] = {
            "IsTruncated": next_token is not None,
            "Contents": [{"Key": record_key(ACCOUNT_A, i), "Size": 10} for i in ids],
            "KeyCount": len(ids),
        }
        if next_token:
            response["NextContinuationToken"] = next_token
        stubber.add_response("list_objects_v2", response, expected)
        for i in ids:
            if (token, i) == ("token-1", "2"):
                continue
            body = io.BytesIO(json.dumps(make_record(i)).encode())
            stubber.add_response(
                "get_object",
                {"Body": body, "ContentLength": len(body.getvalue())},
                {"Bucket": BUCKET, "Key": record_key(ACCOUNT_A, i)},
            )
    monkeypatch.setattr(script, "create_s3_client", lambda region, profile: client)
    with stubber:
        code, rows, err = export(
            script, tmp_path, "--page-size", "2", "--max-workers", "1", capsys=capsys
        )
        stubber.assert_no_pending_responses()
    assert code == 0, err
    assert rows is not None
    assert [r["displayId"] for r in rows] == ["1", "2", "3", "4"]


def test_empty_bucket_writes_header_only(
    script: ModuleType, s3_bucket: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, rows, err = export(script, tmp_path, capsys=capsys)
    assert code == 0
    assert rows == []
    assert "Exported 0 records" in err


def test_stdout_output_by_default(
    script: ModuleType, s3_bucket: str, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    record = make_record("42")
    put(ACCOUNT_A, record)
    code = script.main(["--bucket", BUCKET])
    captured = capsysbinary.readouterr()
    assert code == 0
    assert parse_csv(captured.out.decode()) == [expected_row(ACCOUNT_A, record)]
    assert b"Exported 1 records" in captured.err


def test_account_filter_lists_only_selected_account_prefixes_once(
    script: ModuleType,
    s3_bucket: str,
    s3_calls: list[tuple[str, dict[str, Any]]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    put(ACCOUNT_A, make_record("1"))
    put(ACCOUNT_B, make_record("2"))
    code, rows, _ = export(
        script, tmp_path, "--account-id", ACCOUNT_A, "--account-id", ACCOUNT_A, capsys=capsys
    )
    assert code == 0
    assert rows is not None
    assert [(r["account_number"], r["displayId"]) for r in rows] == [(ACCOUNT_A, "1")]
    prefixes = [params["Prefix"] for name, params in s3_calls if name == "ListObjectsV2"]
    assert prefixes == [f"account_number={ACCOUNT_A}/"]


def test_date_range_is_inclusive_on_case_creation_date(
    script: ModuleType, s3_bucket: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    put(ACCOUNT_A, make_record("1", timeCreated="2026-05-31T23:59:59.999Z"))
    put(ACCOUNT_A, make_record("2", timeCreated="2026-06-01T00:00:00.000Z"))
    put(ACCOUNT_A, make_record("3", timeCreated="2026-06-30T23:59:59Z"))
    put(ACCOUNT_A, make_record("4", timeCreated="2026-07-01T00:00:00Z"))
    code, rows, err = export(
        script, tmp_path, "--start-date", "2026-06-01", "--end-date", "2026-06-30", capsys=capsys
    )
    assert code == 0
    assert rows is not None
    assert [r["displayId"] for r in rows] == ["2", "3"]
    assert "2 records filtered out" in err


def test_date_field_selects_another_record_date(
    script: ModuleType, s3_bucket: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    put(ACCOUNT_A, make_record("1", Case_Retrieval_Date="2026-08-01T00:00:00Z"))
    put(ACCOUNT_A, make_record("2", Case_Retrieval_Date="2026-09-01T00:00:00Z"))
    code, rows, _ = export(
        script,
        tmp_path,
        "--date-field",
        "Case_Retrieval_Date",
        "--start-date",
        "2026-09-01",
        capsys=capsys,
    )
    assert code == 0
    assert rows is not None
    assert [r["displayId"] for r in rows] == ["2"]


def test_value_filters_combine_with_and_and_repeat_with_or(
    script: ModuleType, s3_bucket: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rca_a, rca_b, rca_c = RCA.labels[:3]
    put(ACCOUNT_A, make_record("1", RCA_Category=rca_a, severityCode="high"))
    put(ACCOUNT_A, make_record("2", RCA_Category=rca_b, severityCode="high"))
    put(ACCOUNT_A, make_record("3", RCA_Category=rca_c, severityCode="high"))
    put(ACCOUNT_A, make_record("4", RCA_Category=rca_a, severityCode="low"))
    put(ACCOUNT_A, make_record("5", RCA_Category=rca_a, severityCode="high", serviceCode="other"))
    put(
        ACCOUNT_A,
        make_record(
            "6", RCA_Category=rca_a, severityCode="high", Lifecycle_Category=LIFECYCLE.labels[1]
        ),
    )
    code, rows, _ = export(
        script,
        tmp_path,
        "--rca-category", rca_a,
        "--rca-category", rca_b,
        "--severity-code", "high",
        "--service-code", "amazon-elastic-compute-cloud-linux",
        "--lifecycle-category", LIFECYCLE.labels[0],
        capsys=capsys,
    )  # fmt: skip
    assert code == 0
    assert rows is not None
    assert [r["displayId"] for r in rows] == ["1", "2"]


def test_unclassified_is_an_accepted_category_filter(
    script: ModuleType, s3_bucket: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    put(ACCOUNT_A, make_record("1", Lifecycle_Category="Unclassified"))
    put(ACCOUNT_A, make_record("2"))
    code, rows, _ = export(script, tmp_path, "--lifecycle-category", "Unclassified", capsys=capsys)
    assert code == 0
    assert rows is not None
    assert [r["displayId"] for r in rows] == ["1"]


def test_formula_like_values_are_escaped_by_default(
    script: ModuleType, s3_bucket: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    put(ACCOUNT_A, make_record("1", subject="=HYPERLINK(1)", RCA_Reason="+1", submittedBy="@x"))
    put(ACCOUNT_A, make_record("2", subject="-2", Lifecycle_Reason="safe"))
    code, rows, _ = export(script, tmp_path, capsys=capsys)
    assert code == 0
    assert rows is not None
    assert [r["subject"] for r in rows] == ["'=HYPERLINK(1)", "'-2"]
    assert rows[0]["RCA_Reason"] == "'+1"
    assert rows[0]["submittedBy"] == "'@x"
    assert rows[1]["Lifecycle_Reason"] == "safe"


def test_no_escape_formulas_keeps_values_verbatim(
    script: ModuleType, s3_bucket: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    put(ACCOUNT_A, make_record("1", subject="=SUM(1)"))
    code, rows, _ = export(script, tmp_path, "--no-escape-formulas", capsys=capsys)
    assert code == 0
    assert rows is not None
    assert rows[0]["subject"] == "=SUM(1)"


def test_overwrite_replaces_file_and_utf8_sig_adds_bom(
    script: ModuleType, s3_bucket: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    put(ACCOUNT_A, make_record("1", subject="Café résumé"))
    out = tmp_path / "export.csv"
    out.write_text("old\n")
    code = script.main(
        ["--bucket", BUCKET, "--output", str(out), "--overwrite", "--encoding", "utf-8-sig"]
    )
    assert code == 0, capsys.readouterr().err
    data = out.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")
    assert out.stat().st_mode & 0o777 == 0o600
    rows = parse_csv(data.decode("utf-8-sig"))
    assert rows[0]["subject"] == "Café résumé"
    assert [p.name for p in tmp_path.iterdir()] == ["export.csv"]


def test_record_limit_fails_without_writing_partial_output(
    script: ModuleType, s3_bucket: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for i in range(3):
        put(ACCOUNT_A, make_record(str(i)))
    code, rows, err = export(script, tmp_path, "--max-records", "2", capsys=capsys)
    assert code == 4
    assert rows is None
    assert "--max-records" in err
    assert list(tmp_path.iterdir()) == []


def test_filtered_out_records_do_not_count_towards_limit(
    script: ModuleType, s3_bucket: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for i in range(3):
        put(ACCOUNT_A, make_record(str(i), severityCode="high" if i == 0 else "low"))
    code, rows, _ = export(
        script, tmp_path, "--max-records", "1", "--severity-code", "high", capsys=capsys
    )
    assert code == 0
    assert rows is not None
    assert len(rows) == 1


# --- bad records -------------------------------------------------------------------------


def write_bad_records() -> dict[str, str]:
    """Store one valid and several invalid records; return key -> expected reason code."""
    put(ACCOUNT_A, make_record("1"))
    missing = make_record("2")
    del missing["Model_Id"]
    extra: dict[str, Any] = make_record("8") | {"unexpected": "x"}
    return {
        put(ACCOUNT_A, f'{{"Case_Summary": "{SENTINEL}"', display_id="3"): "malformed_json",
        put(ACCOUNT_A, "[1, 2]", display_id="4"): "not_an_object",
        put(ACCOUNT_A, missing): "field_set",
        put(ACCOUNT_A, extra): "field_set",
        put(ACCOUNT_A, make_record("5", displayId="6", Case_Summary=SENTINEL), "5"): (
            "display_id_mismatch"
        ),
        put(ACCOUNT_A, make_record("7", RCA_Reason=12)): "not_a_string",  # type: ignore[arg-type]
        put(ACCOUNT_A, make_record("9", timeCreated="yesterday")): "invalid_date",
        put(ACCOUNT_A, make_record("10", caseId=None)): "missing_value",
        put(ACCOUNT_A, b"\xff\xfe".decode("latin-1"), display_id="11"): "malformed_json",
    }


def test_bad_records_fail_the_export_by_default_without_leaking_content(
    script: ModuleType, s3_bucket: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = write_bad_records()
    code, rows, err = export(script, tmp_path, capsys=capsys)
    assert code == 4
    assert rows is None
    assert list(tmp_path.iterdir()) == []
    for key, reason in bad.items():
        assert f"{key}: {reason}" in err
    assert SENTINEL not in err
    assert "--skip-invalid" in err


def test_skip_invalid_exports_only_valid_records(
    script: ModuleType, s3_bucket: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = write_bad_records()
    code, rows, err = export(script, tmp_path, "--skip-invalid", capsys=capsys)
    assert code == 0
    assert rows is not None
    assert [r["displayId"] for r in rows] == ["1"]
    assert f"{len(bad)} invalid records skipped" in err
    assert SENTINEL not in err


def test_oversized_object_is_rejected_unread(
    script: ModuleType, s3_bucket: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = put(ACCOUNT_A, " " * (script.MAX_RECORD_BYTES + 1), display_id="1")
    code, _, err = export(script, tmp_path, capsys=capsys)
    assert code == 4
    assert f"{key}: too_large" in err


def test_object_deleted_between_list_and_get_is_skipped(
    script: ModuleType,
    aws_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = boto3.client("s3", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response(
        "list_objects_v2",
        {"IsTruncated": False, "Contents": [{"Key": record_key(ACCOUNT_A, "1")}]},
    )
    stubber.add_client_error("get_object", "NoSuchKey", http_status_code=404)
    monkeypatch.setattr(script, "create_s3_client", lambda region, profile: client)
    with stubber:
        code, rows, err = export(script, tmp_path, "--max-workers", "1", capsys=capsys)
    assert code == 0
    assert rows == []
    assert "1 records removed during export" in err


# --- AWS failures --------------------------------------------------------------------------


def stubbed_failure(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    configure: Callable[[Stubber], None],
) -> None:
    client = boto3.client("s3", region_name="us-east-1")
    stubber = Stubber(client)
    configure(stubber)
    stubber.activate()
    monkeypatch.setattr(script, "create_s3_client", lambda region, profile: client)


def test_list_access_denied_is_reported_with_required_permission(
    script: ModuleType,
    aws_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stubbed_failure(
        script,
        monkeypatch,
        lambda s: s.add_client_error("list_objects_v2", "AccessDenied", http_status_code=403),
    )
    code, rows, err = export(script, tmp_path, capsys=capsys)
    assert code == 3
    assert rows is None
    assert "AccessDenied" in err
    assert "s3:ListBucket" in err
    assert BUCKET in err
    assert "Traceback" not in err


def test_get_access_denied_is_reported_with_required_permission(
    script: ModuleType,
    aws_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def configure(stubber: Stubber) -> None:
        stubber.add_response(
            "list_objects_v2",
            {"IsTruncated": False, "Contents": [{"Key": record_key(ACCOUNT_A, "1")}]},
        )
        stubber.add_client_error("get_object", "AccessDenied", http_status_code=403)

    stubbed_failure(script, monkeypatch, configure)
    code, rows, err = export(script, tmp_path, "--max-workers", "1", capsys=capsys)
    assert code == 3
    assert rows is None
    assert "s3:GetObject" in err
    assert list(tmp_path.iterdir()) == []


def test_missing_bucket_is_reported(
    script: ModuleType, aws_env: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with mock_aws():
        code, rows, err = export(script, tmp_path, capsys=capsys)
    assert code == 3
    assert rows is None
    assert "NoSuchBucket" in err


def test_missing_credentials_are_reported(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in list(os.environ):
        if name.startswith("AWS_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    code, _, err = export(script, tmp_path, "--region", "us-east-1", capsys=capsys)
    assert code == 3
    assert "credentials" in err.lower()


def test_unknown_profile_is_reported(
    script: ModuleType,
    aws_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "none"))
    code, _, err = export(script, tmp_path, "--profile", "missing-profile", capsys=capsys)
    assert code == 3
    assert "missing-profile" in err


def test_only_s3_is_used_never_athena(
    script: ModuleType,
    s3_bucket: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    services: list[str] = []
    original = boto3.session.Session.client

    def client(
        self: boto3.session.Session, service_name: str, *args: object, **kwargs: object
    ) -> object:
        services.append(service_name)
        return original(self, service_name, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(boto3.session.Session, "client", client)
    put(ACCOUNT_A, make_record("1"))
    services.clear()
    code, rows, _ = export(script, tmp_path, capsys=capsys)
    assert code == 0
    assert rows is not None
    assert services == ["s3"]


def test_script_runs_as_main_module(
    s3_bucket: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = make_record("77")
    put(ACCOUNT_B, record)
    out = tmp_path / "main.csv"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--bucket", BUCKET, "--output", str(out)])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exit_info.value.code == 0
    assert parse_csv(out.read_text()) == [expected_row(ACCOUNT_B, record)]

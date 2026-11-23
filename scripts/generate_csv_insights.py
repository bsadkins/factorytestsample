"""Export processed Support Case Insights records from S3 to a CSV file.

Reads every processed record (`account_number=<id>/case_number=<displayId>/data.json`) directly
from the processed S3 bucket, applies the optional filters and writes one CSV row per record with
the account number and all 18 processed-record fields. Intended for Power BI, Excel and other
consumers that do not use Athena.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import boto3
import pandas as pd
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    PartialCredentialsError,
    ProfileNotFound,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "lambda"))

from common.taxonomy import LIFECYCLE, RCA, UNCLASSIFIED  # noqa: E402

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

# Kept in step with common.processed_record.PROCESSED_FIELDS by the CLI tests. The list is local so
# the exporter needs only boto3 and pandas, not the Lambda runtime dependencies.
RECORD_FIELDS: Final = (
    "caseId",
    "displayId",
    "subject",
    "serviceCode",
    "categoryCode",
    "severityCode",
    "submittedBy",
    "timeCreated",
    "Case_Retrieval_Date",
    "Case_Summary",
    "RCA_Category",
    "RCA_Reason",
    "RCA_Retrieval_Date",
    "Lifecycle_Category",
    "Lifecycle_Reason",
    "Lifecycle_Retrieval_Date",
    "Model_Id",
    "Prompt_Version",
)
CSV_FIELDS: Final = ("account_number", *RECORD_FIELDS)
NULLABLE_FIELDS: Final = frozenset(
    {"subject", "serviceCode", "categoryCode", "severityCode", "submittedBy"}
)
DATE_FIELDS: Final = (
    "timeCreated",
    "Case_Retrieval_Date",
    "RCA_Retrieval_Date",
    "Lifecycle_Retrieval_Date",
)

RECORDS_PREFIX: Final = "account_number="
KEY_PATTERN: Final = re.compile(r"^account_number=(\d{12})/case_number=([^/]+)/data\.json$")
ACCOUNT_ID_PATTERN: Final = re.compile(r"^\d{12}$")
BUCKET_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
MAX_RECORD_BYTES: Final = 1024 * 1024
MAX_FILTER_VALUE_CHARS: Final = 256
PAGE_SIZE_LIMIT: Final = 1000
MAX_RECORDS_LIMIT: Final = 1_000_000
MAX_WORKERS_LIMIT: Final = 32
DEFAULT_MAX_RECORDS: Final = 100_000
DEFAULT_MAX_WORKERS: Final = 8
MAX_REPORTED_INVALID: Final = 50
# Spreadsheet applications evaluate cells starting with these characters as formulas.
FORMULA_PREFIXES: Final = ("=", "+", "-", "@", "\t", "\r")

EXIT_OK: Final = 0
EXIT_USAGE: Final = 2
EXIT_AWS_ERROR: Final = 3
EXIT_DATA_ERROR: Final = 4

PERMISSIONS_EPILOG: Final = """\
Required IAM permissions (no others are used):
  s3:ListBucket  on arn:aws:s3:::<bucket>
  s3:GetObject   on arn:aws:s3:::<bucket>/account_number=*

Athena is not used or required: records are read directly from the processed bucket, so newly
processed cases appear without waiting for the Glue crawler. Credentials come from the standard
AWS credential provider chain (environment, --profile, SSO, instance or container role).

Filters combine with AND; repeating a filter option matches any of its values. Dates are UTC
calendar days and both bounds are inclusive. Null values are written as empty cells. Rows are
sorted by account, case creation time and case display ID.

Exit status: 0 success, 2 invalid options (including an existing --output file without
--overwrite, also one created while the export runs), 3 AWS error (credentials, permissions,
missing bucket), 4 invalid records (without --skip-invalid) or more than --max-records matches. No
output file is written unless the export succeeds.
"""


class InvalidRecordError(ValueError):
    """A stored object is not a valid processed record. `code` never contains record content."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RecordLimitError(RuntimeError):
    """More records matched than `--max-records` allows."""


class OutputError(RuntimeError):
    """The output file cannot be published safely. The message never contains record content."""


@dataclass(frozen=True, slots=True)
class Filters:
    """Record filters; empty sets and `None` dates match everything."""

    start_date: date | None = None
    end_date: date | None = None
    date_field: str = "timeCreated"
    rca_categories: frozenset[str] = frozenset()
    lifecycle_categories: frozenset[str] = frozenset()
    service_codes: frozenset[str] = frozenset()
    severity_codes: frozenset[str] = frozenset()

    def matches(self, record: dict[str, Any], dates: dict[str, datetime]) -> bool:
        """Return True when `record` passes every configured filter."""
        day = dates[self.date_field].date()
        if self.start_date is not None and day < self.start_date:
            return False
        if self.end_date is not None and day > self.end_date:
            return False
        selections = (
            (self.rca_categories, "RCA_Category"),
            (self.lifecycle_categories, "Lifecycle_Category"),
            (self.service_codes, "serviceCode"),
            (self.severity_codes, "severityCode"),
        )
        return all(not wanted or record[key] in wanted for wanted, key in selections)


@dataclass(slots=True)
class ExportStats:
    """Counts reported on stderr; no record content."""

    objects_listed: int = 0
    ignored_objects: int = 0
    filtered_out: int = 0
    removed: int = 0
    invalid: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ParsedRecord:
    """A validated record with its partition account and parsed dates."""

    account: str
    record: dict[str, Any]
    dates: dict[str, datetime]


# --- argument parsing ------------------------------------------------------------------------


def bounded_int(low: int, high: int) -> Callable[[str], int]:
    """Return an argparse type accepting integers in [low, high]."""

    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            number = low - 1
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"must be an integer between {low} and {high}")
        return number

    return parse


def bucket_name(value: str) -> str:
    """Validate an S3 bucket name."""
    if not BUCKET_PATTERN.match(value) or ".." in value:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid S3 bucket name")
    return value


def account_id(value: str) -> str:
    """Validate a 12-digit AWS account ID."""
    if not ACCOUNT_ID_PATTERN.match(value):
        raise argparse.ArgumentTypeError(f"{value!r} is not a 12-digit AWS account ID")
    return value


def calendar_date(value: str) -> date:
    """Parse a YYYY-MM-DD calendar date."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC).date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid YYYY-MM-DD date") from None


def filter_value(value: str) -> str:
    """Validate a free-text filter value."""
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    if len(value) > MAX_FILTER_VALUE_CHARS:
        raise argparse.ArgumentTypeError(f"must be at most {MAX_FILTER_VALUE_CHARS} characters")
    return value


def category(labels: Sequence[str], title: str) -> Callable[[str], str]:
    """Return an argparse type accepting a taxonomy label or the Unclassified fallback."""
    allowed = (*labels, UNCLASSIFIED)

    def parse(value: str) -> str:
        if value not in allowed:
            raise argparse.ArgumentTypeError(
                f"unknown {title} category {value!r}; valid values: {', '.join(allowed)}"
            )
        return value

    return parse


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").strip(),
        epilog=PERMISSIONS_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--bucket", required=True, type=bucket_name, help="Processed-record S3 bucket name."
    )
    parser.add_argument(
        "--output",
        default="-",
        help="CSV file to write, or '-' for standard output (default: '-'). The directory must "
        "exist. Files are created with owner-only permissions.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace --output if it already exists. Without it an existing file is never "
        "replaced, even one created while the export runs.",
    )
    parser.add_argument(
        "--account-id",
        action="append",
        type=account_id,
        default=[],
        metavar="ID",
        help="Only export cases of this 12-digit account. Repeatable. Only the selected "
        "accounts' S3 prefixes are listed.",
    )
    parser.add_argument(
        "--start-date", type=calendar_date, metavar="YYYY-MM-DD", help="Earliest date (inclusive)."
    )
    parser.add_argument(
        "--end-date", type=calendar_date, metavar="YYYY-MM-DD", help="Latest date (inclusive)."
    )
    parser.add_argument(
        "--date-field",
        choices=DATE_FIELDS,
        default="timeCreated",
        help="Record date the date range applies to (default: timeCreated, case creation).",
    )
    parser.add_argument(
        "--rca-category",
        action="append",
        type=category(RCA.labels, "RCA"),
        default=[],
        metavar="LABEL",
        help="Only export this RCA_Category (an RCA taxonomy label or Unclassified). Repeatable.",
    )
    parser.add_argument(
        "--lifecycle-category",
        action="append",
        type=category(LIFECYCLE.labels, "lifecycle"),
        default=[],
        metavar="LABEL",
        help="Only export this Lifecycle_Category (a lifecycle taxonomy label or Unclassified). "
        "Repeatable.",
    )
    parser.add_argument(
        "--service-code",
        action="append",
        type=filter_value,
        default=[],
        metavar="CODE",
        help="Only export this serviceCode, for example amazon-simple-storage-service. Repeatable.",
    )
    parser.add_argument(
        "--severity-code",
        action="append",
        type=filter_value,
        default=[],
        metavar="CODE",
        help="Only export this severityCode, for example low or urgent. Repeatable.",
    )
    parser.add_argument(
        "--max-records",
        type=bounded_int(1, MAX_RECORDS_LIMIT),
        default=DEFAULT_MAX_RECORDS,
        help=f"Fail instead of exporting when more records match (default: {DEFAULT_MAX_RECORDS}, "
        f"maximum: {MAX_RECORDS_LIMIT}).",
    )
    parser.add_argument(
        "--skip-invalid",
        action="store_true",
        help="Skip objects that are not valid processed records instead of failing. Skipped keys "
        "and reasons are reported on stderr.",
    )
    parser.add_argument(
        "--encoding",
        choices=("utf-8", "utf-8-sig"),
        default="utf-8",
        help="Output encoding (default: utf-8). utf-8-sig adds a byte-order mark so older Excel "
        "versions detect UTF-8 when opening the file directly.",
    )
    parser.add_argument(
        "--no-escape-formulas",
        action="store_true",
        help="Write values verbatim. By default a value starting with =, +, -, @, tab or carriage "
        "return is prefixed with a single quote so spreadsheets do not run it as a formula.",
    )
    parser.add_argument(
        "--page-size",
        type=bounded_int(1, PAGE_SIZE_LIMIT),
        default=PAGE_SIZE_LIMIT,
        help=f"Keys per S3 list request (default and maximum: {PAGE_SIZE_LIMIT}).",
    )
    parser.add_argument(
        "--max-workers",
        type=bounded_int(1, MAX_WORKERS_LIMIT),
        default=DEFAULT_MAX_WORKERS,
        help=f"Parallel S3 object reads (default: {DEFAULT_MAX_WORKERS}, maximum: "
        f"{MAX_WORKERS_LIMIT}).",
    )
    parser.add_argument("--region", help="AWS Region of the bucket (default: from AWS config).")
    parser.add_argument("--profile", help="AWS named profile (default: from AWS config).")
    return parser


def option_problem(args: argparse.Namespace) -> str | None:
    """Return a cross-option validation error, or None."""
    if args.start_date and args.end_date and args.start_date > args.end_date:
        return "--start-date must not be after --end-date"
    if args.output != "-":
        output = Path(args.output)
        if not output.parent.is_dir():
            return f"output directory {str(output.parent)!r} does not exist"
        if output.is_dir():
            return f"--output {args.output!r} is a directory"
        if output.exists() and not args.overwrite:
            return f"--output {args.output!r} already exists; pass --overwrite to replace it"
    return None


# --- S3 access and record parsing ------------------------------------------------------------


def create_s3_client(region: str | None, profile: str | None) -> S3Client:
    """Create the S3 client from the standard credential provider chain."""
    session = boto3.Session(region_name=region, profile_name=profile)
    config = Config(
        retries={"max_attempts": 10, "mode": "standard"},
        max_pool_connections=MAX_WORKERS_LIMIT,
    )
    return session.client("s3", config=config)


def list_prefixes(account_ids: Sequence[str]) -> list[str]:
    """Return the S3 prefixes to list: one per selected account, or every account."""
    if not account_ids:
        return [RECORDS_PREFIX]
    return [f"{RECORDS_PREFIX}{account}/" for account in sorted(set(account_ids))]


def iter_record_pages(
    client: S3Client, bucket: str, prefixes: Sequence[str], page_size: int, stats: ExportStats
) -> Iterator[list[tuple[str, str, str]]]:
    """Yield, per S3 list page, the `(key, account, displayId)` of each processed-record key.

    Every key is yielded at most once, even if a listing repeats it.
    """
    seen: set[str] = set()
    paginator = client.get_paginator("list_objects_v2")
    for prefix in prefixes:
        pages = paginator.paginate(
            Bucket=bucket, Prefix=prefix, PaginationConfig={"PageSize": page_size}
        )
        for page in pages:
            batch: list[tuple[str, str, str]] = []
            for item in page.get("Contents", []):
                key = item["Key"]
                if key in seen:
                    continue
                seen.add(key)
                stats.objects_listed += 1
                match = KEY_PATTERN.match(key)
                if match is None:
                    stats.ignored_objects += 1
                    continue
                batch.append((key, match.group(1), match.group(2)))
            yield batch


def read_object(client: S3Client, bucket: str, key: str) -> bytes | None:
    """Return the object body, or None if it was deleted after listing."""
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
            return None
        raise
    body = response["Body"]
    try:
        if response.get("ContentLength", 0) > MAX_RECORD_BYTES:
            raise InvalidRecordError("too_large")
        data = body.read(MAX_RECORD_BYTES + 1)
    finally:
        body.close()
    if len(data) > MAX_RECORD_BYTES:
        raise InvalidRecordError("too_large")
    return data


def parse_date(value: str) -> datetime:
    """Parse an ISO-8601 record timestamp as UTC."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise InvalidRecordError("invalid_date") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_record(account: str, display_id: str, data: bytes) -> ParsedRecord:
    """Validate a processed record against the 18-field schema and its partition path."""
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InvalidRecordError("malformed_json") from None
    if not isinstance(record, dict):
        raise InvalidRecordError("not_an_object")
    if set(record) != set(RECORD_FIELDS):
        raise InvalidRecordError("field_set")
    for name in RECORD_FIELDS:
        value = record[name]
        if value is None and name in NULLABLE_FIELDS:
            continue
        if value is not None and not isinstance(value, str):
            raise InvalidRecordError("not_a_string")
        if name not in NULLABLE_FIELDS and (value is None or not value.strip()):
            raise InvalidRecordError("missing_value")
    if record["displayId"] != display_id:
        raise InvalidRecordError("display_id_mismatch")
    dates = {name: parse_date(record[name]) for name in DATE_FIELDS}
    return ParsedRecord(account=account, record=record, dates=dates)


def load_record(
    client: S3Client, bucket: str, item: tuple[str, str, str]
) -> tuple[str, ParsedRecord | str | None]:
    """Read and validate one key: a record, an invalid-reason code, or None if removed."""
    key, account, display_id = item
    try:
        data = read_object(client, bucket, key)
        if data is None:
            return key, None
        return key, parse_record(account, display_id, data)
    except InvalidRecordError as exc:
        return key, exc.code


def collect_records(
    client: S3Client, args: argparse.Namespace, filters: Filters, stats: ExportStats
) -> list[ParsedRecord]:
    """List and read every processed record, returning those that pass the filters."""
    selected: list[ParsedRecord] = []
    pool = ThreadPoolExecutor(max_workers=args.max_workers)
    try:
        pages = iter_record_pages(
            client, args.bucket, list_prefixes(args.account_id), args.page_size, stats
        )
        for batch in pages:
            for key, outcome in pool.map(
                lambda item: load_record(client, args.bucket, item), batch
            ):
                if outcome is None:
                    stats.removed += 1
                elif isinstance(outcome, str):
                    stats.invalid.append((key, outcome))
                elif not filters.matches(outcome.record, outcome.dates):
                    stats.filtered_out += 1
                else:
                    selected.append(outcome)
                    if len(selected) > args.max_records:
                        raise RecordLimitError
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return selected


# --- CSV output ------------------------------------------------------------------------------


def escape_formula(value: object) -> object:
    """Prefix spreadsheet formula triggers with a single quote."""
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return f"'{value}"
    return value


def render_csv(records: Sequence[ParsedRecord], *, escape_formulas: bool) -> str:
    """Return the CSV text: header plus one row per record, deterministically ordered."""
    rows = [
        {"account_number": item.account, **item.record, "_created": item.dates["timeCreated"]}
        for item in records
    ]
    frame = pd.DataFrame.from_records(rows, columns=[*CSV_FIELDS, "_created"])
    frame = frame.sort_values(["account_number", "_created", "displayId"], kind="stable")
    frame = frame.drop(columns="_created")
    if escape_formulas:
        frame = frame.map(escape_formula)
    return frame.to_csv(index=False, na_rep="", lineterminator="\r\n")


def publish(temp: Path, target: Path, *, overwrite: bool) -> None:
    """Atomically move the complete `temp` file to `target`.

    Without `overwrite` an existing `target` is never replaced, even one created after the early
    existence check: a hard link, unlike a rename, fails instead of replacing the destination.
    Raises OutputError, leaving `target` unchanged, when it exists or the filesystem cannot link.
    """
    if overwrite:
        temp.replace(target)
        return
    try:
        target.hardlink_to(temp)
    except FileExistsError:
        raise OutputError(
            f"--output {str(target)!r} was created while the export ran and was left unchanged; "
            "pass --overwrite to replace it."
        ) from None
    except OSError as exc:
        raise OutputError(
            f"cannot create --output {str(target)!r} without risking replacing another file "
            f"({exc.strerror or type(exc).__name__}); write to a directory on a filesystem "
            "that supports hard links, or pass --overwrite."
        ) from None
    temp.unlink()


def write_output(text: str, output: str, encoding: str, *, overwrite: bool) -> None:
    """Write the CSV to stdout, or atomically to an owner-only file.

    An existing file is replaced only when `overwrite` is set; otherwise OutputError is raised and
    the existing file is left unchanged. No temporary file remains after a failure.
    """
    data = text.encode(encoding)
    if output == "-":
        sys.stdout.flush()
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return
    target = Path(output)
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp", delete=False
    )
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        publish(Path(handle.name), target, overwrite=overwrite)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


# --- entry point -----------------------------------------------------------------------------


def aws_error_message(exc: Exception, bucket: str) -> str:
    """Return an actionable, content-free description of an AWS failure."""
    if isinstance(exc, (NoCredentialsError, PartialCredentialsError)):
        return (
            "AWS credentials were not found. Configure the standard credential provider chain, "
            "for example with --profile, AWS_PROFILE or 'aws sso login'."
        )
    if isinstance(exc, ProfileNotFound):
        return f"AWS profile error: {exc}"
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        operation = exc.operation_name
        message = f"S3 {operation} on bucket {bucket!r} failed with {code}."
        if code in {"AccessDenied", "AllAccessDisabled", "403"}:
            permission = (
                f"s3:ListBucket on arn:aws:s3:::{bucket}"
                if operation == "ListObjectsV2"
                else f"s3:GetObject on arn:aws:s3:::{bucket}/{RECORDS_PREFIX}*"
            )
            message += f" The caller needs {permission}."
        elif code == "NoSuchBucket":
            message += " Check --bucket and --region."
        return message
    return f"AWS request failed: {exc}"


def report(stats: ExportStats, exported: int | None, destination: str) -> None:
    """Write export counts to stderr."""
    lines = [
        f"Listed {stats.objects_listed} objects; "
        f"{stats.ignored_objects} non-record objects ignored.",
        f"{stats.filtered_out} records filtered out; "
        f"{stats.removed} records removed during export.",
    ]
    if exported is not None:
        lines.append(f"{len(stats.invalid)} invalid records skipped.")
        lines.append(f"Exported {exported} records to {destination}.")
    sys.stderr.write("\n".join(lines) + "\n")


def report_invalid(invalid: Sequence[tuple[str, str]], *, skipped: bool) -> None:
    """List invalid object keys and reason codes (never their content) on stderr."""
    if not invalid:
        return
    verb = "Skipped" if skipped else "Found"
    sys.stderr.write(f"{verb} {len(invalid)} invalid records:\n")
    for key, reason in invalid[:MAX_REPORTED_INVALID]:
        sys.stderr.write(f"  {key}: {reason}\n")
    if len(invalid) > MAX_REPORTED_INVALID:
        sys.stderr.write(f"  ... and {len(invalid) - MAX_REPORTED_INVALID} more\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the export and return the process exit status."""
    parser = build_parser()
    args = parser.parse_args(argv)
    problem = option_problem(args)
    if problem is not None:
        parser.print_usage(sys.stderr)
        sys.stderr.write(f"{parser.prog}: error: {problem}\n")
        return EXIT_USAGE
    filters = Filters(
        start_date=args.start_date,
        end_date=args.end_date,
        date_field=args.date_field,
        rca_categories=frozenset(args.rca_category),
        lifecycle_categories=frozenset(args.lifecycle_category),
        service_codes=frozenset(args.service_code),
        severity_codes=frozenset(args.severity_code),
    )
    stats = ExportStats()
    destination = "standard output" if args.output == "-" else args.output
    try:
        client = create_s3_client(args.region, args.profile)
        records = collect_records(client, args, filters, stats)
    except (BotoCoreError, ClientError) as exc:
        sys.stderr.write(f"error: {aws_error_message(exc, args.bucket)}\n")
        return EXIT_AWS_ERROR
    except RecordLimitError:
        sys.stderr.write(
            f"error: more than {args.max_records} records match; narrow the filters or raise "
            "--max-records. Nothing was written.\n"
        )
        return EXIT_DATA_ERROR
    report_invalid(stats.invalid, skipped=args.skip_invalid)
    if stats.invalid and not args.skip_invalid:
        report(stats, None, destination)
        sys.stderr.write(
            "error: invalid records found; nothing was written. Pass --skip-invalid to export "
            "the valid records only.\n"
        )
        return EXIT_DATA_ERROR
    try:
        write_output(
            render_csv(records, escape_formulas=not args.no_escape_formulas),
            args.output,
            args.encoding,
            overwrite=args.overwrite,
        )
    except OutputError as exc:
        report(stats, None, destination)
        sys.stderr.write(f"error: {exc} Nothing was written.\n")
        return EXIT_USAGE
    report(stats, len(records), destination)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

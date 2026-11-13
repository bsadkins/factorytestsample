"""Cross-surface regression, part 1: the shared synthetic processed records in the production S3
layout, the Glue table contract that maps them for Athena, and the CSV exporter run through its
real `main()` against mocked S3. Expected values are written out by hand from the fixture table in
test/support/cross_surface.py, not computed by the code under test."""

from __future__ import annotations

import pytest
from common.processed import case_record_key, parse_case_record_key
from common.processed_record import PROCESSED_FIELDS, validate_processed_record
from common.taxonomy import UNCLASSIFIED
from cross_surface import (
    ACCOUNT_A,
    ACCOUNT_B,
    ACCOUNT_C,
    ACCOUNT_OF,
    BUCKET,
    CONTRACT,
    CSV_ORDER,
    FIXTURE,
    LAMBDA,
    RECORDS,
    CsvExport,
    Endpoint,
    nonzero,
    record_key_pattern,
)
from mcp_kit import COLUMNS
from mcp_tools.context import CASE_SUMMARY_TABLE

# --- processed record shape and the Glue table contract --------------------------------------


def test_fixture_records_are_complete_processed_records_in_the_production_layout() -> None:
    assert len(FIXTURE) == 16
    assert len(RECORDS) == 16
    for item in FIXTURE:
        record = item["record"]
        assert list(record) == list(PROCESSED_FIELDS)
        validate_processed_record(record)
        key = case_record_key(item["accountNumber"], record["displayId"])
        assert (
            key
            == f"account_number={item['accountNumber']}/case_number={record['displayId']}/data.json"
        )
        assert parse_case_record_key(key) == (item["accountNumber"], record["displayId"])
    assert {ACCOUNT_OF[case] for case in RECORDS} == {ACCOUNT_A, ACCOUNT_B, ACCOUNT_C}
    assert RECORDS["7200000004"]["RCA_Category"] == UNCLASSIFIED


def test_glue_table_contract_matches_the_record_schema_and_mcp_queries() -> None:
    serde = CONTRACT["serdeParameters"]
    columns = [column["name"] for column in CONTRACT["columns"]]
    partitions = [partition["name"] for partition in CONTRACT["partitionKeys"]]
    assert CONTRACT["tableName"] == CASE_SUMMARY_TABLE
    assert serde["case.insensitive"] == "false"
    assert [serde[f"mapping.{column}"] for column in columns] == list(PROCESSED_FIELDS)
    assert {c["type"] for c in [*CONTRACT["columns"], *CONTRACT["partitionKeys"]]} == {"string"}
    # The Athena stand-in that executes the MCP SQL has exactly the deployed table's columns.
    assert [*columns, *partitions] == list(COLUMNS)
    key = case_record_key(ACCOUNT_A, "7100000001")
    match = record_key_pattern().fullmatch(key)
    assert match is not None
    assert match.groupdict() == {"account_number": ACCOUNT_A, "case_number": "7100000001"}


def test_s3_records_reach_athena_through_the_case_sensitive_mapping(
    rows: list[dict[str, str | None]],
) -> None:
    assert len(rows) == 16
    by_case = {row["case_number"]: row for row in rows}
    assert set(by_case) == set(RECORDS)
    for case, record in RECORDS.items():
        row = by_case[case]
        assert row["account_number"] == ACCOUNT_OF[case]
        assert [row[field.lower()] for field in PROCESSED_FIELDS] == list(record.values())
    # Only `subject` is stored in lower case: without the explicit mappings 17 of the 18
    # columns would read as null.
    assert [f for f in PROCESSED_FIELDS if f.lower() in RECORDS["7100000001"]] == ["subject"]
    assert by_case["7200000004"]["servicecode"] is None
    assert (
        by_case["7100000002"]["subject"] == 'Throttling, "capacity" and\nlaunch failures in one AZ'
    )


# --- CSV exporter ----------------------------------------------------------------------------


def test_csv_export_preserves_every_field_across_listing_pages(exporter: CsvExport) -> None:
    code, exported = exporter.run("--page-size", "5", "--no-escape-formulas", "--max-workers", "2")

    assert code == 0
    assert [row["displayId"] for row in exported] == CSV_ORDER
    for row in exported:
        record = RECORDS[row["displayId"]]
        expected = {k: "" if v is None else v for k, v in record.items()}
        assert row == {"account_number": ACCOUNT_OF[row["displayId"]], **expected}
    pages = [params for name, params in exporter.calls if name == "ListObjectsV2"]
    assert len(pages) == 4  # 16 objects, 5 keys per page
    assert all(params["MaxKeys"] == 5 for params in pages)
    assert all(params["Bucket"] == BUCKET for params in pages)
    operations = {name for name, _ in exporter.calls}
    assert operations == {"ListObjectsV2", "GetObject"}  # no Athena, Glue or writes
    assert len([name for name, _ in exporter.calls if name == "GetObject"]) == 16


def test_csv_export_escapes_only_the_formula_like_value_by_default(exporter: CsvExport) -> None:
    code, exported = exporter.run()

    assert code == 0
    changed = {
        (row["displayId"], name)
        for row in exported
        for name in PROCESSED_FIELDS
        if row[name]
        != ("" if RECORDS[row["displayId"]][name] is None else RECORDS[row["displayId"]][name])
    }
    assert changed == {("7100000003", "subject")}
    by_case = {row["displayId"]: row for row in exported}
    assert by_case["7100000003"]["subject"] == "'" + RECORDS["7100000003"]["subject"]


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        pytest.param(
            ["--account-id", ACCOUNT_B],
            {"7200000001", "7200000002", "7200000003", "7200000004", "7200000005"},
            id="one-account",
        ),
        pytest.param(
            ["--start-date", "2026-01-01", "--end-date", "2026-01-31"],
            {"7100000001", "7100000002", "7100000003", "7100000004", "7200000001", "7200000002"},
            id="inclusive-january",
        ),
        pytest.param(
            ["--rca-category", "Throttling", "--service-code", LAMBDA],
            {"7100000005", "7100000006"},
            id="rca-and-service",
        ),
        pytest.param(
            ["--severity-code", "normal", "--severity-code", "high"],
            {
                "7100000001",
                "7100000003",
                "7100000005",
                "7200000001",
                "7200000003",
                "7200000005",
                "7300000003",
                "7300000004",
                "7300000005",
            },
            id="severity-or",
        ),
        pytest.param(
            ["--lifecycle-category", "Unclassified"],
            {"7200000004"},
            id="unclassified-lifecycle",
        ),
        pytest.param(
            [
                "--start-date",
                "2026-02-01",
                "--end-date",
                "2026-02-28",
                "--lifecycle-category",
                "Load Testing",
                "--account-id",
                ACCOUNT_A,
                "--account-id",
                ACCOUNT_B,
            ],
            {"7100000005", "7200000003"},
            id="combined",
        ),
    ],
)
def test_csv_filters_select_the_expected_cases(
    exporter: CsvExport, args: list[str], expected: set[str]
) -> None:
    code, exported = exporter.run(*args)

    assert code == 0
    assert {row["displayId"] for row in exported} == expected
    assert [row["displayId"] for row in exported] == [c for c in CSV_ORDER if c in expected]


def test_csv_and_mcp_agree_on_a_filtered_rca_distribution(
    exporter: CsvExport, endpoint: Endpoint
) -> None:
    _, exported = exporter.run("--start-date", "2026-01-01", "--end-date", "2026-01-31")
    from_csv: dict[str, int] = {}
    for row in exported:
        from_csv[row["RCA_Category"]] = from_csv.get(row["RCA_Category"], 0) + 1

    summary = endpoint.ok("get_case_summary", {"startDate": "2026-01-01", "endDate": "2026-01-31"})

    expected = {"ICE Error": 2, "Throttling": 2, "Limit reached": 1, "Customer Question": 1}
    assert from_csv == expected
    assert dict(nonzero(summary["distribution"], "rcaCategory")) == expected
    assert summary["totalCases"] == len(exported) == 6

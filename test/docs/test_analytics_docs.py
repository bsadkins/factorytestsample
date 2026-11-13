"""Documentation checks for the Analytics (Athena, QuickSight) and CSV export guides.

The guides must describe what is actually built: the Glue/Athena names and column mappings
synthesised by `lib/constructs/analytics.ts`, the processed-record schema in
`common.processed_record` and the options, permissions and output of
`scripts/generate_csv_insights.py`. SQL examples are checked against the mapped schema because no
Athena engine is available locally.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from common.processed_record import NULLABLE_FIELDS, PROCESSED_FIELDS
from common.taxonomy import LIFECYCLE, RCA, UNCLASSIFIED

REPO_ROOT = Path(__file__).resolve().parents[2]
ATHENA_DOC = REPO_ROOT / "docs" / "athena.md"
CSV_DOC = REPO_ROOT / "docs" / "csv_export.md"
README = REPO_ROOT / "README.md"
CSV_SCRIPT = REPO_ROOT / "scripts" / "generate_csv_insights.py"
SCHEMA = REPO_ROOT / "config" / "schema.json"
ANALYTICS_CONSTRUCT = REPO_ROOT / "lib" / "constructs" / "analytics.ts"
RENDER_SCRIPT = REPO_ROOT / "scripts" / "render_taxonomy_docs.py"

PARTITION_KEYS = ("account_number", "case_number")
COLUMNS = frozenset(field.lower() for field in PROCESSED_FIELDS) | frozenset(PARTITION_KEYS)

# Presto/Trino keywords, functions and types the query library may use. Anything else in a SQL
# block must be a mapped column, the table name, or an alias or CTE defined in that block.
SQL_WORDS = frozenset(
    {
        "select",
        "from",
        "where",
        "group",
        "by",
        "order",
        "having",
        "limit",
        "with",
        "as",
        "and",
        "or",
        "not",
        "in",
        "is",
        "null",
        "asc",
        "desc",
        "distinct",
        "case",
        "when",
        "then",
        "else",
        "end",
        "on",
        "join",
        "left",
        "inner",
        "cross",
        "union",
        "all",
        "between",
        "like",
        "over",
        "partition",
        "rows",
        "interval",
        "true",
        "false",
        "filter",
        "count",
        "count_if",
        "sum",
        "avg",
        "min",
        "max",
        "round",
        "cast",
        "coalesce",
        "nullif",
        "concat",
        "lower",
        "upper",
        "from_iso8601_timestamp",
        "date_trunc",
        "date_format",
        "format_datetime",
        "day_of_week",
        "day_of_month",
        "day",
        "week",
        "month",
        "quarter",
        "year",
        "current_timestamp",
        "current_date",
        "date_add",
        "timestamp",
        "date",
        "approx_distinct",
        "row_number",
        "rank",
        "dense_rank",
        "max_by",
        "min_by",
        "arbitrary",
        "greatest",
        "least",
        "bigint",
        "double",
        "integer",
        "varchar",
    }
)
FORBIDDEN_ENCRYPTION_CLAIMS = re.compile(r"kms:|SSE[-_]KMS|CSE[-_]KMS|\bCMK\b", re.IGNORECASE)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def fenced_blocks(text: str, language: str) -> list[str]:
    return re.findall(rf"^```{language}\n(.*?)^```", text, flags=re.MULTILINE | re.DOTALL)


def as_list(value: str | list[str]) -> list[str]:
    return [value] if isinstance(value, str) else value


def section(text: str, heading: str) -> str:
    """Return the body of the Markdown section with exactly this heading text."""
    match = re.search(rf"^(#+) {re.escape(heading)}\n", text, flags=re.MULTILINE)
    assert match is not None, f"missing section {heading!r}"
    level = len(match.group(1))
    rest = text[match.end() :]
    end = re.search(rf"^#{{1,{level}}} ", rest, flags=re.MULTILINE)
    return rest if end is None else rest[: end.start()]


def strip_sql(sql: str) -> str:
    """Remove comments and string literals so only identifiers and keywords remain."""
    without_comments = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"'(?:[^']|'')*'", " ", without_comments)


def defined_names(sql: str) -> set[str]:
    """Aliases (`AS name`) and CTE names (`WITH name AS`, `, name AS (`) defined in the block."""
    names = {m.lower() for m in re.findall(r"\bAS\s+([A-Za-z_]\w*)", sql, flags=re.IGNORECASE)}
    names |= {
        m.lower()
        for m in re.findall(r"(?:\bWITH|,)\s*([A-Za-z_]\w*)\s+AS\s*\(", sql, flags=re.IGNORECASE)
    }
    return names


@cache
def csv_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_csv_insights_docs", CSV_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


def cli_options() -> list[argparse.Action]:
    parser: argparse.ArgumentParser = csv_script().build_parser()
    return [action for action in parser._actions if "--help" not in action.option_strings]


def schema_default(*path: str) -> Any:  # noqa: ANN401
    node: Any = json.loads(read(SCHEMA))
    for key in path:
        node = node["properties"][key]
    return node["default"]


# --- Athena guide ------------------------------------------------------------------------------


def test_athena_guide_names_the_synthesised_resources() -> None:
    text = read(ATHENA_DOC)
    for name in (
        "CaseInsightsAnalyticsStack-<uniqueId>",
        "case_insights_<uniqueId>",
        "case_summary",
        "case-insights-partition-crawler-<uniqueId>",
        "CaseInsights-<uniqueId>",
        "query-results/",
        "Athena engine version 3",
        "aws glue start-crawler --name case-insights-partition-crawler-<uniqueId> "
        "--region <region>",
    ):
        assert name in text, name
    construct = read(ANALYTICS_CONSTRUCT)
    for output in re.findall(r"key: '([A-Za-z]+)'", construct):
        assert f"`{output}`" in text, f"stack output {output} is not documented"
    assert f"`{schema_default('schedules', 'crawler')}`" in text
    for key in ("bytesScannedCutoffPerQuery", "resultsRetentionDays"):
        assert f"`operations.athena.{key}`" in text, key
        assert f"`{schema_default('operations', 'athena', key)}`" in text, key


def test_athena_guide_maps_every_column_to_its_json_key_in_order() -> None:
    rows = re.findall(
        r"^\| `([a-z_]+)` \| `([A-Za-z_]+)` \|", section(read(ATHENA_DOC), "Columns"), re.M
    )
    assert rows == [(field.lower(), field) for field in PROCESSED_FIELDS]
    partitions = section(read(ATHENA_DOC), "Partitions")
    for key in PARTITION_KEYS:
        assert f"`{key}`" in partitions
    nullable_section = section(read(ATHENA_DOC), "Columns")
    for field in NULLABLE_FIELDS:
        assert re.search(rf"^\| `{field.lower()}` \|[^\n]*\| Yes \|", nullable_section, re.M)


def test_query_library_covers_every_requested_analysis() -> None:
    library = section(read(ATHENA_DOC), "Query library")
    for heading in (
        "RCA distribution",
        "Lifecycle categories by service",
        "Day-of-week pattern",
        "Week-of-month pattern",
        "Monthly trend",
        "Severity trends",
        "Top accounts",
    ):
        assert fenced_blocks(section(library, heading), "sql"), heading


def test_athena_classification_values_are_generated_from_the_taxonomy() -> None:
    spec = importlib.util.spec_from_file_location("render_taxonomy_docs", RENDER_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    text = read(ATHENA_DOC)
    assert renderer.replace_block(text, renderer.render_markdown()) == text
    values = text[text.index(renderer.START) : text.index(renderer.END)]
    assert text.index(renderer.START) > text.index("### Classification values")
    for label in (*RCA.labels, *LIFECYCLE.labels):
        assert f"| {label} |" in values, label


def test_athena_queries_use_only_the_mapped_schema() -> None:
    blocks = fenced_blocks(read(ATHENA_DOC), "sql")
    assert len(blocks) >= 7
    for sql in blocks:
        assert re.search(r"\bFROM case_summary\b", sql), sql
        assert "CREATE VIEW" not in sql.upper(), "temporal queries use inline CTEs, not views"
        allowed = SQL_WORDS | COLUMNS | {"case_summary"} | defined_names(sql)
        tokens = {token.lower() for token in re.findall(r"[A-Za-z_]\w*", strip_sql(sql))}
        assert tokens <= allowed, f"unknown identifiers {sorted(tokens - allowed)} in:\n{sql}"


def test_category_literals_in_queries_are_taxonomy_labels() -> None:
    text = "\n".join(fenced_blocks(read(ATHENA_DOC), "sql"))
    for column, labels in (("rca_category", RCA.labels), ("lifecycle_category", LIFECYCLE.labels)):
        for match in re.finditer(rf"{column}\s*(?:=|<>|IN)\s*(\([^)]*\)|'[^']*')", text, re.I):
            for literal in re.findall(r"'([^']*)'", match.group(1)):
                assert literal in (*labels, UNCLASSIFIED), literal


def test_athena_policy_examples_are_valid_json_and_scope_the_workgroup() -> None:
    policies = [json.loads(block) for block in fenced_blocks(read(ATHENA_DOC), "json")]
    assert policies
    resources = json.dumps(policies)
    assert "workgroup/CaseInsights-<uniqueId>" in resources
    assert "database/case_insights_<uniqueId>" in resources
    assert "table/case_insights_<uniqueId>/case_summary" in resources
    actions = {
        action
        for policy in policies
        for statement in policy["Statement"]
        for action in as_list(statement["Action"])
    }
    assert not any(
        re.match(r"s3:(Delete|PutBucket)|glue:(Create|Delete|Update)", a) for a in actions
    )


def test_quicksight_guidance_is_optional_manual_and_deferred() -> None:
    quicksight = section(read(ATHENA_DOC), "Optional: Amazon QuickSight")
    assert "CaseInsights-<uniqueId>" in quicksight
    assert "case_insights_<uniqueId>" in quicksight
    not_provisioned = r"not (created|provisioned) by (this|the) (CDK )?(application|solution)"
    assert re.search(not_provisioned, quicksight)
    assert "deferred" in quicksight.lower()
    assert "deferred" in section(read(ATHENA_DOC), "Live verification (deferred)").lower()


def test_athena_guide_documents_crawler_delay_and_manual_refresh() -> None:
    text = read(ATHENA_DOC)
    refresh = section(text, "Making new records visible")
    assert "aws glue start-crawler" in refresh
    assert "aws glue get-crawler" in refresh
    assert "--query 'Crawler.State'" in refresh


# --- CSV guide ---------------------------------------------------------------------------------


def test_csv_guide_documents_every_cli_option_and_choice() -> None:
    text = read(CSV_DOC)
    options = section(text, "Options")
    for action in cli_options():
        for option in action.option_strings:
            assert re.search(rf"^\| `{re.escape(option)}", options, re.M), option
        for choice in action.choices or ():
            assert f"`{choice}`" in options, choice
    documented = set(re.findall(r"^\| `(--[a-z-]+)", options, re.M))
    assert documented == {o for a in cli_options() for o in a.option_strings}
    script = csv_script()
    for value in (
        script.DEFAULT_MAX_RECORDS,
        script.MAX_RECORDS_LIMIT,
        script.DEFAULT_MAX_WORKERS,
        script.MAX_WORKERS_LIMIT,
        script.PAGE_SIZE_LIMIT,
    ):
        assert f"{value:,}" in options or str(value) in options, value


def test_csv_guide_permissions_match_the_cli_epilog() -> None:
    text = read(CSV_DOC)
    permissions = section(text, "Required permissions")
    for line in csv_script().PERMISSIONS_EPILOG.splitlines()[1:3]:
        action, resource = line.split(maxsplit=2)[0], line.split()[-1]
        assert f"`{action}`" in permissions
        assert f"`{resource}`" in permissions
    policies = [json.loads(block) for block in fenced_blocks(permissions, "json")]
    assert len(policies) == 1
    statements = policies[0]["Statement"]
    granted = sorted((s["Action"], s["Resource"]) for s in statements)
    assert granted == [
        ("s3:GetObject", "arn:aws:s3:::<bucket>/account_number=*"),
        ("s3:ListBucket", "arn:aws:s3:::<bucket>"),
    ]


def test_csv_guide_does_not_depend_on_athena() -> None:
    text = read(CSV_DOC)
    assert "Athena is not used or required" in text
    assert not re.search(r"\b(athena|glue):", text)
    assert "start-crawler" not in text


def test_csv_guide_lists_columns_header_and_exit_codes_as_implemented() -> None:
    text = read(CSV_DOC)
    script = csv_script()
    columns = re.findall(r"^\| \d+ \| `([A-Za-z_]+)` \|", section(text, "Output"), re.M)
    assert columns == list(script.CSV_FIELDS)
    assert ",".join(script.CSV_FIELDS) in text
    exit_codes = section(text, "Exit status")
    for code in (script.EXIT_OK, script.EXIT_USAGE, script.EXIT_AWS_ERROR, script.EXIT_DATA_ERROR):
        assert re.search(rf"^\| `{code}` \|", exit_codes, re.M), code


def test_csv_guide_has_power_bi_excel_and_scheduling_guidance() -> None:
    text = read(CSV_DOC)
    for heading in ("Power BI", "Excel", "Scheduling"):
        assert section(text, heading).strip(), heading
    for heading in ("Power BI", "Excel"):
        assert "account_number" in section(text, heading)


def test_csv_guide_commands_use_real_options() -> None:
    valid = {o for a in cli_options() for o in a.option_strings}
    text = read(CSV_DOC)
    commands = [
        block for block in fenced_blocks(text, "bash") if "generate_csv_insights.py" in block
    ]
    assert commands
    for block in commands:
        for option in re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", block):
            assert option in valid or option == "--help", option


# --- shared ------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [ATHENA_DOC, CSV_DOC], ids=lambda p: p.name)
def test_guides_make_no_customer_managed_key_claims(path: Path) -> None:
    assert not FORBIDDEN_ENCRYPTION_CLAIMS.search(read(path))


@pytest.mark.parametrize("path", [README, ATHENA_DOC, CSV_DOC], ids=lambda p: p.name)
def test_relative_links_resolve(path: Path) -> None:
    for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", read(path)):
        if re.match(r"[a-z]+:", target):
            continue
        assert (path.parent / target).exists(), f"{path.name} links to missing {target}"


def test_readme_links_the_analytics_guides() -> None:
    text = read(README)
    assert "(docs/athena.md)" in text
    assert "(docs/csv_export.md)" in text

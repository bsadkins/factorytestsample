"""Read-only SQL validation: a named corpus of accepted and rejected statements, including
comment, semicolon, quoting, CTE and catalog-escape bypass attempts. Rejected SQL must never
reach Athena (asserted through the query_athena tool with the recording Athena stand-in)."""

from __future__ import annotations

import pytest
from mcp_kit import DATABASE, dataset, make_kit
from mcp_tools.errors import SqlRejectedError, ToolInputError
from mcp_tools.registry import build_registry
from mcp_tools.sql_guard import SqlPolicy, validate_read_only_sql

POLICY = SqlPolicy(database=DATABASE)

ACCEPTED: dict[str, tuple[str, str]] = {
    "simple_select": ("SELECT rca_category, count(*) FROM case_summary GROUP BY 1", "select"),
    "mixed_case_keywords": ("sElEcT * fRoM Case_Summary LiMiT 5", "select"),
    "trailing_semicolon": ("SELECT 1 FROM case_summary;", "select"),
    "trailing_semicolon_whitespace": ("SELECT 1 FROM case_summary ;  \n", "select"),
    "qualified_table": (f"SELECT * FROM {DATABASE}.case_summary", "select"),  # noqa: S608
    "catalog_qualified_table": (
        f'SELECT * FROM AwsDataCatalog."{DATABASE}"."case_summary"',  # noqa: S608
        "select",
    ),
    "quoted_table": ('SELECT * FROM "case_summary" AS c', "select"),
    "semicolon_in_string": ("SELECT 'a; DROP TABLE x' AS s FROM case_summary", "select"),
    "comment_text_in_string": (
        "SELECT '-- not a comment /* nor this */' FROM case_summary",
        "select",
    ),
    "escaped_quote_in_string": ("SELECT 'it''s; DELETE' AS s FROM case_summary", "select"),
    "keyword_in_quoted_identifier": ('SELECT servicecode AS "delete" FROM case_summary', "select"),
    "cte": (
        "WITH counts AS (SELECT rca_category, count(*) AS n FROM case_summary GROUP BY 1) "
        "SELECT * FROM counts ORDER BY n DESC",
        "select",
    ),
    "multiple_ctes": (
        "WITH a AS (SELECT * FROM case_summary), b AS (SELECT servicecode FROM a) "
        "SELECT count(*) FROM b",
        "select",
    ),
    "nested_cte": (
        "WITH outer_q AS (WITH inner_q AS (SELECT * FROM case_summary) SELECT * FROM inner_q) "
        "SELECT count(*) FROM outer_q",
        "select",
    ),
    "cte_visible_in_nested_subqueries": (
        "WITH a AS (SELECT * FROM case_summary) SELECT * FROM case_summary WHERE case_number IN "
        "(SELECT case_number FROM (SELECT case_number FROM a) x)",
        "select",
    ),
    "cte_shadowed_by_inner_cte": (
        "WITH a AS (SELECT * FROM case_summary) SELECT * FROM (WITH a AS (SELECT servicecode "
        "FROM a) SELECT * FROM a) x",
        "select",
    ),
    "cte_in_exists_subquery": (
        "SELECT * FROM case_summary WHERE EXISTS (WITH s AS (SELECT 1 AS x FROM case_summary) "
        "SELECT x FROM s)",
        "select",
    ),
    "cte_with_set_operation_body": (
        "WITH a AS (SELECT servicecode FROM case_summary) SELECT * FROM a UNION ALL "
        "SELECT * FROM a",
        "select",
    ),
    "temporal_inline_cte": (
        "WITH cases AS (SELECT from_iso8601_timestamp(timecreated) AS created FROM case_summary) "
        "SELECT day_of_week(created) AS d, count(*) FROM cases GROUP BY 1 UNION ALL "
        "SELECT 0, count(*) FROM cases",
        "select",
    ),
    "unnest_with_ordinality": (
        "SELECT x, n FROM case_summary CROSS JOIN UNNEST(ARRAY[1, 2]) WITH ORDINALITY AS t (x, n)",
        "select",
    ),
    "cte_column_list": (
        "WITH t (svc, n) AS (SELECT servicecode, count(*) FROM case_summary GROUP BY 1) "
        "SELECT svc FROM t",
        "select",
    ),
    "subquery_in_from": ("SELECT * FROM (SELECT * FROM case_summary) s", "select"),
    "subquery_in_where": (
        "SELECT * FROM case_summary WHERE account_number IN "
        "(SELECT account_number FROM case_summary WHERE severitycode = 'urgent')",
        "select",
    ),
    "self_join": (
        "SELECT a.case_number FROM case_summary a JOIN case_summary b "
        "ON a.case_number = b.case_number",
        "select",
    ),
    "comma_join_same_table": ("SELECT 1 FROM case_summary a, case_summary b", "select"),
    "extract_from": (
        "SELECT extract(year FROM from_iso8601_timestamp(timecreated)) FROM case_summary",
        "select",
    ),
    "trim_and_substring_from": (
        "SELECT trim(BOTH 'x' FROM subject), substring(subject FROM 1 FOR 3) FROM case_summary",
        "select",
    ),
    "is_distinct_from": (
        "SELECT * FROM case_summary WHERE servicecode IS DISTINCT FROM 'amazon-s3'",
        "select",
    ),
    "unnest": ("SELECT x FROM case_summary CROSS JOIN UNNEST(ARRAY[1, 2]) AS t (x)", "select"),
    "values_subquery": ("SELECT * FROM (VALUES (1, 'a')) AS v (n, s)", "select"),
    "cast_with_time_zone": (
        "SELECT CAST(from_iso8601_timestamp(timecreated) AS timestamp WITH TIME ZONE) "
        "FROM case_summary",
        "select",
    ),
    "window_and_lambda": (
        "SELECT rank() OVER (PARTITION BY servicecode ORDER BY timecreated), "
        "transform(ARRAY[1], x -> x + 1) FROM case_summary",
        "select",
    ),
    "parenthesised_union": (
        "(SELECT rca_category FROM case_summary) UNION ALL (SELECT lifecycle_category "
        "FROM case_summary)",
        "select",
    ),
    "grouping_sets": (
        "SELECT servicecode, severitycode, count(*) FROM case_summary "
        "GROUP BY GROUPING SETS ((servicecode), (severitycode))",
        "select",
    ),
    "show_tables": ("SHOW TABLES", "show"),
    "show_tables_in_database": (f"SHOW TABLES IN {DATABASE}", "show"),
    "show_columns": ("show columns from case_summary", "show"),
    "show_partitions": ("SHOW PARTITIONS case_summary", "show"),
    "describe": ("DESCRIBE case_summary", "describe"),
    "describe_formatted_qualified": (f"DESCRIBE FORMATTED {DATABASE}.case_summary", "describe"),
}

REJECTED: dict[str, tuple[str, str]] = {
    "empty": ("   ", "empty"),
    "insert": ("INSERT INTO case_summary SELECT * FROM case_summary", "statement_not_allowed"),
    "update": ("UPDATE case_summary SET rca_category = 'x'", "statement_not_allowed"),
    "delete": ("DELETE FROM case_summary", "statement_not_allowed"),
    "drop": ("DROP TABLE case_summary", "statement_not_allowed"),
    "alter": ("ALTER TABLE case_summary ADD COLUMNS (x string)", "statement_not_allowed"),
    "ctas": ("CREATE TABLE copy AS SELECT * FROM case_summary", "statement_not_allowed"),
    "create_view": ("CREATE VIEW v AS SELECT * FROM case_summary", "statement_not_allowed"),
    "unload": (
        "UNLOAD (SELECT * FROM case_summary) TO 's3://bucket/x' WITH (format = 'JSON')",
        "statement_not_allowed",
    ),
    "msck": ("MSCK REPAIR TABLE case_summary", "statement_not_allowed"),
    "explain_analyze": ("EXPLAIN ANALYZE SELECT * FROM case_summary", "statement_not_allowed"),
    "external_function": (
        "USING EXTERNAL FUNCTION f(x varchar) RETURNS varchar LAMBDA 'fn' "
        "SELECT f(subject) FROM case_summary",
        "statement_not_allowed",
    ),
    "stacked_write": ("SELECT 1 FROM case_summary; DROP TABLE case_summary", "stacked_statement"),
    "stacked_select": (
        "SELECT 1 FROM case_summary; SELECT 2 FROM case_summary",
        "stacked_statement",
    ),
    "stacked_lowercase": (
        "select 1 from case_summary;delete from case_summary",
        "stacked_statement",
    ),
    "double_semicolon": ("SELECT 1 FROM case_summary;;", "stacked_statement"),
    "semicolon_after_string": (
        "SELECT 'x;' FROM case_summary; INSERT INTO t VALUES (1)",
        "stacked_statement",
    ),
    "leading_line_comment": ("-- harmless\nSELECT 1 FROM case_summary", "comment"),
    "interleaved_block_comment": ("SELECT /* x */ 1 FROM case_summary", "comment"),
    "comment_hides_stack": ("SELECT 1 FROM case_summary --; DROP TABLE t", "comment"),
    "comment_splits_keyword": ("DR/**/OP TABLE case_summary", "comment"),
    "trailing_comment": ("SELECT 1 FROM case_summary; -- done", "comment"),
    "unterminated_string": ("SELECT 'abc FROM case_summary", "unterminated_literal"),
    "unterminated_identifier": ('SELECT "abc FROM case_summary', "unterminated_literal"),
    "backtick_identifier": ("SELECT * FROM `case_summary`", "unsupported_character"),
    "unicode_space": ("SELECT\u00a01 FROM case_summary", "unsupported_character"),
    "zero_width_space": ("SELECT 1 FROM case_summary;\u200b DROP TABLE t", "unsupported_character"),
    "parameter_placeholder": (
        "SELECT * FROM case_summary WHERE account_number = ?",
        "parameter_placeholder",
    ),
    "cte_hides_insert": (
        "WITH a AS (SELECT * FROM case_summary) INSERT INTO t SELECT * FROM a",
        "forbidden_keyword",
    ),
    "cte_body_delete": ("WITH a AS (DELETE FROM case_summary) SELECT 1", "forbidden_keyword"),
    "nested_cte_ctas": (
        "WITH a AS (WITH b AS (SELECT 1) SELECT * FROM b) CREATE TABLE x AS SELECT * FROM a",
        "forbidden_keyword",
    ),
    "recursive_cte": (
        "WITH RECURSIVE r (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT * FROM r",
        "recursive_cte",
    ),
    "other_table": ("SELECT * FROM secrets", "table_not_allowed"),
    "other_database": ("SELECT * FROM other_db.case_summary", "table_not_allowed"),
    "other_catalog": (f"SELECT * FROM lambda_catalog.{DATABASE}.case_summary", "table_not_allowed"),  # noqa: S608
    "information_schema": ("SELECT * FROM information_schema.columns", "table_not_allowed"),
    "system_catalog": ("SELECT * FROM system.runtime.queries", "table_not_allowed"),
    "hidden_metadata_table": ('SELECT * FROM "case_summary$partitions"', "table_not_allowed"),
    "escape_in_join": (
        "SELECT * FROM case_summary c JOIN other_db.t o ON true",
        "table_not_allowed",
    ),
    "escape_after_join_condition": (
        "SELECT * FROM case_summary a JOIN case_summary b ON true, other_db.secret",
        "table_not_allowed",
    ),
    "escape_in_comma_join": ("SELECT 1 FROM case_summary, other_db.t", "table_not_allowed"),
    "escape_in_subquery": (
        "SELECT * FROM case_summary WHERE EXISTS (SELECT 1 FROM other_db.t)",
        "table_not_allowed",
    ),
    "escape_in_nested_cte": (
        "WITH a AS (WITH b AS (SELECT * FROM other_db.t) SELECT * FROM b) SELECT * FROM a",
        "table_not_allowed",
    ),
    "escape_in_parenthesised_join": (
        "SELECT 1 FROM (case_summary a JOIN other_db.t b ON true)",
        "table_not_allowed",
    ),
    "cte_shadowing_with_escape": (
        "WITH case_summary AS (SELECT * FROM other_db.t) SELECT * FROM case_summary",
        "table_not_allowed",
    ),
    "cte_scope_exists_subquery": (
        "SELECT * FROM secrets WHERE EXISTS (WITH secrets AS (SELECT 1 AS x) SELECT x FROM secrets)",  # noqa: E501
        "table_not_allowed",
    ),
    "cte_scope_in_subquery_in_from": (
        "SELECT * FROM (WITH secrets AS (SELECT * FROM case_summary) SELECT * FROM secrets) s, "
        "secrets",
        "table_not_allowed",
    ),
    "cte_scope_inside_cte_body": (
        "WITH a AS (WITH secrets AS (SELECT 1 AS x) SELECT * FROM secrets) SELECT * FROM secrets",
        "table_not_allowed",
    ),
    "cte_scope_deeply_nested": (
        "SELECT * FROM secrets WHERE x IN (SELECT x FROM (SELECT x FROM (WITH secrets AS "
        "(SELECT 1 AS x) SELECT x FROM secrets) a) b)",
        "table_not_allowed",
    ),
    "cte_scope_sibling_subqueries": (
        "SELECT * FROM case_summary WHERE EXISTS (WITH s AS (SELECT 1 AS x) SELECT x FROM s) "
        "AND EXISTS (SELECT 1 FROM s)",
        "table_not_allowed",
    ),
    "cte_scope_parenthesised_union_branch": (
        "(WITH secrets AS (SELECT 1 AS x) SELECT x FROM secrets) UNION ALL SELECT x FROM secrets",
        "table_not_allowed",
    ),
    "cte_forward_reference": (
        "WITH a AS (SELECT * FROM secrets), secrets AS (SELECT * FROM case_summary) "
        "SELECT * FROM a",
        "table_not_allowed",
    ),
    "cte_self_reference": (
        "WITH secrets AS (SELECT * FROM secrets) SELECT * FROM secrets",
        "table_not_allowed",
    ),
    "cte_scope_join_after_subquery": (
        "SELECT * FROM case_summary c JOIN (WITH secrets AS (SELECT 1 AS x) SELECT x FROM "
        "secrets) t ON true JOIN secrets z ON true",
        "table_not_allowed",
    ),
    "table_function": ("SELECT * FROM TABLE(system.query(query => 'x'))", "forbidden_keyword"),
    "qualified_function": (
        "SELECT system.builtin.lower(subject) FROM case_summary",
        "qualified_function",
    ),
    "show_create_table": ("SHOW CREATE TABLE case_summary", "statement_not_allowed"),
    "show_databases": ("SHOW DATABASES", "statement_not_allowed"),
    "show_tables_other_database": ("SHOW TABLES IN other_db", "table_not_allowed"),
    "show_columns_other_table": ("SHOW COLUMNS FROM other_db.secret", "table_not_allowed"),
    "describe_other_table": ("DESCRIBE other_db.secret", "table_not_allowed"),
    "describe_trailing_statement": ("DESCRIBE case_summary; DROP TABLE x", "stacked_statement"),
    "select_into": ("SELECT * INTO copy FROM case_summary", "forbidden_keyword"),
    "set_session": ("SET SESSION query_max_run_time = '1h'", "statement_not_allowed"),
    "values_statement": ("VALUES (1)", "statement_not_allowed"),
    "unbalanced_parentheses": ("SELECT (1 FROM case_summary", "unbalanced_parentheses"),
    "too_long": ("SELECT 1 FROM case_summary WHERE " + " OR ".join(["1 = 1"] * 2000), "too_long"),  # noqa: S608
}


@pytest.mark.parametrize("name", sorted(ACCEPTED))
def test_accepts_read_only_sql(name: str) -> None:
    sql, statement = ACCEPTED[name]
    validated = validate_read_only_sql(sql, POLICY)
    assert validated.statement == statement
    assert not validated.text.rstrip().endswith(";")


@pytest.mark.parametrize("name", sorted(REJECTED))
def test_rejects_unsafe_sql(name: str) -> None:
    sql, reason = REJECTED[name]
    with pytest.raises(SqlRejectedError) as raised:
        validate_read_only_sql(sql, POLICY)
    assert raised.value.reason == reason
    assert isinstance(raised.value, ToolInputError)


def test_rejection_messages_do_not_echo_sql() -> None:
    secret = "SENTINEL-SQL-9d1e"
    with pytest.raises(SqlRejectedError) as raised:
        validate_read_only_sql(f"SELECT '{secret}' FROM other_db.t", POLICY)  # noqa: S608
    assert secret not in str(raised.value)
    assert secret not in str(raised.value.to_payload())


def test_internal_parameters_counted_only_when_allowed() -> None:
    sql = "SELECT count(*) FROM case_summary WHERE account_number = ? AND servicecode = ?"
    assert validate_read_only_sql(sql, POLICY, allow_parameters=True).parameter_count == 2
    with pytest.raises(SqlRejectedError):
        validate_read_only_sql(sql, POLICY)


def test_deeply_nested_query_is_rejected() -> None:
    sql = "SELECT " + "(" * 40 + "1" + ")" * 40 + " FROM case_summary"
    with pytest.raises(SqlRejectedError) as raised:
        validate_read_only_sql(sql, POLICY)
    assert raised.value.reason == "too_complex"


@pytest.mark.parametrize("name", sorted(REJECTED))
def test_rejected_sql_never_reaches_athena(name: str) -> None:
    kit = make_kit(dataset())
    with pytest.raises(ToolInputError):
        build_registry().call("query_athena", {"sql": REJECTED[name][0]}, kit.context())
    assert kit.athena.calls == []
    assert kit.bedrock.requests == []


@pytest.mark.parametrize(
    "name", ["simple_select", "semicolon_in_string", "nested_cte", "subquery_in_where"]
)
def test_accepted_sql_runs_in_configured_boundary(name: str) -> None:
    kit = make_kit(dataset())
    result = build_registry().call("query_athena", {"sql": ACCEPTED[name][0]}, kit.context())
    assert result["status"] == "SUCCEEDED"
    started = kit.athena.started()
    assert len(started) == 1
    assert started[0]["WorkGroup"] == "CaseInsights-mcp-test"
    assert started[0]["QueryExecutionContext"] == {
        "Catalog": "AwsDataCatalog",
        "Database": DATABASE,
    }
    assert "ResultConfiguration" not in started[0]

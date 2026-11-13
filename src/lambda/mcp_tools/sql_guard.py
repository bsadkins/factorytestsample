"""Read-only SQL guard for `query_athena`.

The SQL is tokenised (string literals and quoted identifiers are recognised with their escape
rules, so keywords, semicolons or comment markers inside them are inert) and then checked
structurally, not by substring or first-keyword matching:

- exactly one statement: one optional trailing semicolon, no other semicolon outside literals;
- no comments, parameter placeholders, backticks or non-ASCII characters outside literals;
- the statement is SELECT or WITH (optionally parenthesised), or one of a few SHOW/DESCRIBE forms
  over the configured table or database;
- no data-changing, DDL, session, procedure, external-function or table-function keyword appears
  anywhere (including after a CTE), recursive CTEs are refused, and qualified function calls are
  refused;
- every relation in a FROM or JOIN clause (including comma joins, parenthesised joins, subqueries
  and CTE bodies) is either the configured `case_summary` table, optionally qualified by the
  configured database and `AwsDataCatalog`, or the name of a CTE that is in lexical scope at that
  point. A CTE is visible only after its own definition: in the later CTEs of the same WITH clause
  and in the query body, including subqueries nested in them, up to the end of the parenthesised
  query that owns the WITH. A CTE name used anywhere else names a physical table and is checked
  as one.

Error messages describe the violated rule and never repeat the SQL text.
"""

from __future__ import annotations

import string
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from mcp_tools.context import CASE_SUMMARY_TABLE, DATA_CATALOG
from mcp_tools.errors import SqlRejectedError

Statement = Literal["select", "show", "describe"]
TokenKind = Literal["word", "ident", "string", "number", "punct", "op"]

_WORD_START: Final = frozenset(string.ascii_letters + "_")
_WORD_CHARS: Final = frozenset(string.ascii_letters + string.digits + "_")
_DIGITS: Final = frozenset(string.digits)
_WHITESPACE: Final = frozenset(" \t\r\n\f")
_PUNCT: Final = frozenset("(),;.[]")
_OPERATORS: Final = (
    "<>",
    "!=",
    "<=",
    ">=",
    "||",
    "->",
    "=>",
    "=",
    "<",
    ">",
    "+",
    "-",
    "*",
    "/",
    "%",
)

FORBIDDEN_KEYWORDS: Final = frozenset(
    {
        "ALTER", "ANALYZE", "CALL", "COMMENT", "COMMIT", "COPY", "CREATE", "DEALLOCATE", "DELETE",
        "DENY", "DESCRIBE", "DROP", "EXECUTE", "EXPLAIN", "EXPORT", "EXTERNAL", "FUNCTION",
        "GRANT", "IMPORT", "INSERT", "INTO", "LAMBDA", "LOAD", "MERGE", "MSCK", "OPTIMIZE",
        "PREPARE", "REFRESH", "RENAME", "REPAIR", "RESET", "REVOKE", "ROLLBACK", "SAGEMAKER",
        "SET", "SHOW", "START", "TABLE", "TRANSACTION", "TRUNCATE", "UNLOAD", "UPDATE", "USE",
        "VACUUM",
    }
)  # fmt: skip
# Words after which "(" opens a subquery, a list or a grouping rather than a function call.
_STRUCTURAL: Final = frozenset(
    {
        "ALL", "AND", "ANY", "AS", "BETWEEN", "BY", "CASE", "CROSS", "DISTINCT", "ELSE", "END",
        "EXCEPT", "EXISTS", "FROM", "FULL", "GROUP", "HAVING", "IN", "INNER", "INTERSECT", "IS",
        "JOIN", "LATERAL", "LEFT", "LIKE", "LIMIT", "NATURAL", "NOT", "OFFSET", "ON", "OR",
        "ORDER", "OUTER", "OVER", "PARTITION", "RIGHT", "SELECT", "SETS", "SOME", "THEN", "UNION",
        "USING", "VALUES", "WHEN", "WHERE", "WINDOW", "WITH",
    }
)  # fmt: skip
_FROM_CLAUSE_END: Final = frozenset(
    {"WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET", "FETCH", "UNION", "INTERSECT",
     "EXCEPT", "WINDOW"}
)  # fmt: skip
_QUERY_START: Final = frozenset({"SELECT", "WITH", "VALUES"})


@dataclass(frozen=True, slots=True)
class SqlPolicy:
    """The data boundary queries may read: one table in one database of the default catalog."""

    database: str
    table: str = CASE_SUMMARY_TABLE
    catalog: str = DATA_CATALOG
    max_chars: int = 8_000
    max_tokens: int = 4_000
    max_depth: int = 32


@dataclass(frozen=True, slots=True)
class ValidatedSql:
    """An accepted statement. `text` has surrounding whitespace and the trailing ";" removed."""

    text: str
    statement: Statement
    parameter_count: int = 0


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    value: str
    # Upper-case keyword form for words; lower-case name form for words and quoted identifiers.
    upper: str = ""
    name: str = ""


@dataclass(slots=True)
class _Structure:
    tokens: list[Token]
    match: dict[int, int] = field(default_factory=dict)
    parent: list[int] = field(default_factory=list)  # index of the enclosing "(", or -1
    function_paren: set[int] = field(default_factory=set)


def _reject(reason: str, message: str) -> SqlRejectedError:
    return SqlRejectedError(reason, message)


def _quoted(sql: str, start: int, quote: str) -> tuple[str, int]:
    """Read a quote-delimited literal starting at `start`; doubled quotes are escapes."""
    index = start + 1
    parts: list[str] = []
    while True:
        end = sql.find(quote, index)
        if end < 0:
            raise _reject(
                "unterminated_literal", "a string literal or quoted identifier is not closed"
            )
        parts.append(sql[index:end])
        if sql.startswith(quote * 2, end):
            parts.append(quote)
            index = end + 2
            continue
        return "".join(parts), end + 1


def tokenize(sql: str, *, allow_parameters: bool = False) -> tuple[list[Token], int]:  # noqa: PLR0912, PLR0915
    """Split `sql` into tokens; return them with the number of `?` placeholders."""
    tokens: list[Token] = []
    parameters = 0
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char in _WHITESPACE:
            index += 1
        elif sql.startswith("--", index) or sql.startswith("/*", index):
            raise _reject("comment", "SQL comments are not allowed")
        elif char == "'":
            value, index = _quoted(sql, index, "'")
            tokens.append(Token("string", value))
        elif char in "xX" and sql.startswith("'", index + 1):
            value, index = _quoted(sql, index + 1, "'")
            tokens.append(Token("string", value))
        elif char == '"':
            value, index = _quoted(sql, index, '"')
            if not value:
                raise _reject("unsupported_construct", "empty quoted identifiers are not allowed")
            tokens.append(Token("ident", value, name=value.lower()))
        elif char in _WORD_START:
            end = index + 1
            while end < length and sql[end] in _WORD_CHARS:
                end += 1
            word = sql[index:end]
            if word.upper() == "U" and sql.startswith("&", end):
                raise _reject("unsupported_construct", "Unicode escape literals are not allowed")
            tokens.append(Token("word", word, upper=word.upper(), name=word.lower()))
            index = end
        elif char in _DIGITS or (char == "." and sql[index + 1 : index + 2].isdigit()):
            end = index + 1
            while end < length and (sql[end] in _DIGITS or sql[end] == "."):
                end += 1
            if end < length and sql[end] in "eE":
                end += 1
                if end < length and sql[end] in "+-":
                    end += 1
                while end < length and sql[end] in _DIGITS:
                    end += 1
            if end < length and sql[end] in _WORD_START:
                raise _reject("unsupported_construct", "malformed numeric literal")
            tokens.append(Token("number", sql[index:end]))
            index = end
        elif char in _PUNCT:
            tokens.append(Token("punct", char))
            index += 1
        elif char == "?":
            if not allow_parameters:
                raise _reject("parameter_placeholder", "parameter placeholders are not supported")
            parameters += 1
            tokens.append(Token("op", "?"))
            index += 1
        else:
            operator = next((op for op in _OPERATORS if sql.startswith(op, index)), None)
            if operator is None:
                raise _reject("unsupported_character", "the SQL contains an unsupported character")
            tokens.append(Token("op", operator))
            index += len(operator)
    return tokens, parameters


def _is(token: Token | None, *words: str) -> bool:
    return token is not None and token.kind == "word" and token.upper in words


def _punct(token: Token | None, char: str) -> bool:
    return token is not None and token.kind == "punct" and token.value == char


def _is_name(token: Token | None) -> bool:
    return token is not None and token.kind in {"word", "ident"}


def _structure(tokens: list[Token], policy: SqlPolicy) -> _Structure:
    result = _Structure(tokens)
    stack: list[int] = []
    for index, token in enumerate(tokens):
        result.parent.append(stack[-1] if stack else -1)
        if _punct(token, "("):
            stack.append(index)
            if len(stack) > policy.max_depth:
                raise _reject("too_complex", "the SQL is nested too deeply")
            previous = tokens[index - 1] if index else None
            if (
                previous is not None
                and previous.kind == "word"
                and previous.upper not in _STRUCTURAL
            ):
                result.function_paren.add(index)
                if index >= 2 and _punct(tokens[index - 2], "."):  # noqa: PLR2004
                    raise _reject("qualified_function", "qualified function names are not allowed")
        elif _punct(token, ")"):
            if not stack:
                raise _reject("unbalanced_parentheses", "parentheses are not balanced")
            result.match[stack.pop()] = index
    if stack:
        raise _reject("unbalanced_parentheses", "parentheses are not balanced")
    return result


@dataclass(frozen=True, slots=True)
class _CteScope:
    """CTE `name` is visible to relations at token indexes `start` <= index < `end`."""

    name: str
    start: int
    end: int


def _cte_scopes(structure: _Structure) -> list[_CteScope]:
    tokens = structure.tokens
    scopes: list[_CteScope] = []
    for index, token in enumerate(tokens):
        if not _is(token, "WITH"):
            continue
        cursor = index + 1
        if _is(tokens[cursor] if cursor < len(tokens) else None, "RECURSIVE"):
            raise _reject("recursive_cte", "recursive common table expressions are not allowed")
        # Only a WITH that opens a query (statement start or just inside "(") defines CTEs;
        # WITH TIME ZONE and WITH ORDINALITY define nothing, so their names stay physical.
        if index > 0 and not _punct(tokens[index - 1], "("):
            continue
        owner = structure.parent[index]
        end = structure.match[owner] if owner >= 0 else len(tokens)
        while cursor < len(tokens) and _is_name(tokens[cursor]):
            name_index = cursor
            cursor += 1
            if cursor < len(tokens) and _punct(tokens[cursor], "("):
                cursor = structure.match[cursor] + 1
            if not (
                cursor + 1 < len(tokens)
                and _is(tokens[cursor], "AS")
                and _punct(tokens[cursor + 1], "(")
            ):
                break
            cursor = structure.match[cursor + 1] + 1
            scopes.append(_CteScope(tokens[name_index].name, cursor, end))
            if not (cursor < len(tokens) and _punct(tokens[cursor], ",")):
                break
            cursor += 1
    return scopes


def _visible_ctes(scopes: Sequence[_CteScope], index: int) -> set[str]:
    return {scope.name for scope in scopes if scope.start <= index < scope.end}


def _qualified_name(tokens: Sequence[Token], start: int) -> tuple[list[str], int]:
    parts = [tokens[start].name]
    cursor = start + 1
    while cursor + 1 < len(tokens) and _punct(tokens[cursor], ".") and _is_name(tokens[cursor + 1]):
        parts.append(tokens[cursor + 1].name)
        cursor += 2
    if cursor < len(tokens) and _punct(tokens[cursor], "."):
        raise _reject("unsupported_construct", "malformed qualified name")
    return parts, cursor


def _check_table(parts: list[str], policy: SqlPolicy, ctes: set[str]) -> None:
    table, database, catalog = policy.table.lower(), policy.database.lower(), policy.catalog.lower()
    allowed = parts in ([table], [database, table], [catalog, database, table]) or (
        len(parts) == 1 and parts[0] in ctes
    )
    if not allowed:
        raise _reject(
            "table_not_allowed",
            f"queries may read only the {policy.table} table of the configured database",
        )


class _RelationChecker:
    """Validates every relation of every FROM clause and parenthesised join."""

    def __init__(self, structure: _Structure, policy: SqlPolicy, ctes: Sequence[_CteScope]) -> None:
        self.s = structure
        self.tokens = structure.tokens
        self.policy = policy
        self.ctes = ctes

    def run(self) -> None:
        for index, token in enumerate(self.tokens):
            if _is(token, "FROM") and self._is_relation_from(index):
                self._relation_list(index + 1, self._clause_end(index + 1))

    def _is_relation_from(self, index: int) -> bool:
        parent = self.s.parent[index]
        if parent in self.s.function_paren:
            # EXTRACT(x FROM y), SUBSTRING(x FROM 1), TRIM(BOTH c FROM x): not relations.
            return False
        previous = self.tokens[index - 1] if index else None
        before = self.tokens[index - 2] if index >= 2 else None  # noqa: PLR2004
        return not (_is(previous, "DISTINCT") and _is(before, "IS", "NOT"))

    def _clause_end(self, start: int) -> int:
        """Index just past the FROM clause beginning at `start` (same nesting level)."""
        cursor = start
        while cursor < len(self.tokens):
            token = self.tokens[cursor]
            if _punct(token, ")"):
                return cursor
            if _punct(token, "("):
                cursor = self.s.match[cursor] + 1
                continue
            if token.kind == "word" and token.upper in _FROM_CLAUSE_END:
                return cursor
            cursor += 1
        return cursor

    def _relation_list(self, start: int, end: int) -> None:
        expect_relation = True
        cursor = start
        while cursor < end:
            token = self.tokens[cursor]
            if expect_relation:
                cursor = self._relation(cursor, end)
                expect_relation = False
                continue
            if _punct(token, "("):
                cursor = self.s.match[cursor] + 1
                continue
            if _punct(token, ",") or _is(token, "JOIN"):
                expect_relation = True
            cursor += 1
        if expect_relation:
            raise _reject("unsupported_construct", "a FROM or JOIN clause has no relation")

    def _relation(self, cursor: int, end: int) -> int:
        token = self.tokens[cursor]
        if _is(token, "LATERAL", "UNNEST"):
            following = cursor + 1
            if following < end and _punct(self.tokens[following], "("):
                return self.s.match[following] + 1
            raise _reject(
                "unsupported_construct", "LATERAL and UNNEST need a parenthesised argument"
            )
        if _punct(token, "("):
            inner = cursor + 1
            close = self.s.match[cursor]
            first = self.tokens[inner] if inner < close else None
            if not (_punct(first, "(") or _is(first, *_QUERY_START)):
                # A parenthesised join: its contents are a relation list of their own.
                self._relation_list(inner, close)
            return close + 1
        if _is_name(token):
            parts, after = _qualified_name(self.tokens, cursor)
            _check_table(parts, self.policy, _visible_ctes(self.ctes, cursor))
            return after
        raise _reject("unsupported_construct", "unsupported relation in a FROM or JOIN clause")


def _check_query(tokens: list[Token], policy: SqlPolicy) -> None:
    for token in tokens:
        if token.kind == "word" and token.upper in FORBIDDEN_KEYWORDS:
            raise _reject("forbidden_keyword", f"{token.upper} is not allowed in read-only queries")
    structure = _structure(tokens, policy)
    ctes = _cte_scopes(structure)
    for index, token in enumerate(tokens):
        if _is(token, "WITH") and _is(
            tokens[index + 1] if index + 1 < len(tokens) else None, "TIME"
        ):
            continue  # The type modifier in CAST(x AS timestamp WITH TIME ZONE).
        if _is(token, "SELECT", "WITH") and structure.parent[index] in structure.function_paren:
            raise _reject("unsupported_construct", "subqueries must be parenthesised")
    _RelationChecker(structure, policy, ctes).run()


def _target(tokens: list[Token], start: int, policy: SqlPolicy, end: int | None = None) -> int:
    """Validate a table name at `start`; return the index after it."""
    if start >= len(tokens) or not _is_name(tokens[start]):
        raise _reject("statement_not_allowed", "this SHOW or DESCRIBE form is not allowed")
    parts, after = _qualified_name(tokens, start)
    _check_table(parts, policy, set())
    if end is not None and after != end:
        raise _reject("statement_not_allowed", "this SHOW or DESCRIBE form is not allowed")
    return after


def _check_database(tokens: list[Token], index: int, policy: SqlPolicy) -> None:
    if index != len(tokens) - 1 or not _is_name(tokens[index]):
        raise _reject("statement_not_allowed", "this SHOW form is not allowed")
    if tokens[index].name != policy.database.lower():
        raise _reject("table_not_allowed", "only the configured database may be inspected")


def _check_show(tokens: list[Token], policy: SqlPolicy) -> None:
    second = tokens[1] if len(tokens) > 1 else None
    if _is(second, "TABLES"):
        if len(tokens) == 2:  # noqa: PLR2004
            return
        if _is(tokens[2], "FROM", "IN"):
            _check_database(tokens, 3, policy)
            return
    elif _is(second, "COLUMNS") and len(tokens) > 3 and _is(tokens[2], "FROM", "IN"):  # noqa: PLR2004
        after = _target(tokens, 3, policy)
        if after == len(tokens):
            return
        if _is(tokens[after], "FROM", "IN"):
            _check_database(tokens, after + 1, policy)
            return
    elif _is(second, "PARTITIONS"):
        _target(tokens, 2, policy, end=len(tokens))
        return
    raise _reject("statement_not_allowed", "this SHOW form is not allowed")


def _check_describe(tokens: list[Token], policy: SqlPolicy) -> None:
    start = 2 if _is(tokens[1] if len(tokens) > 1 else None, "EXTENDED", "FORMATTED") else 1
    after = _target(tokens, start, policy)
    if after == len(tokens):
        return
    if after == len(tokens) - 1 and _is_name(tokens[after]):
        return
    raise _reject("statement_not_allowed", "this DESCRIBE form is not allowed")


def validate_read_only_sql(
    sql: str, policy: SqlPolicy, *, allow_parameters: bool = False
) -> ValidatedSql:
    """Return the accepted statement or raise `SqlRejectedError` with a stable `reason`.

    `allow_parameters` is for the engine's own fixed queries, which bind validated values to `?`
    placeholders; client SQL never allows them.
    """
    if len(sql) > policy.max_chars:
        raise _reject("too_long", f"the SQL exceeds {policy.max_chars} characters")
    tokens, parameters = tokenize(sql, allow_parameters=allow_parameters)
    if tokens and _punct(tokens[-1], ";"):
        tokens.pop()
    if not tokens:
        raise _reject("empty", "the SQL contains no statement")
    if any(_punct(token, ";") for token in tokens):
        raise _reject("stacked_statement", "only a single statement is allowed")
    if len(tokens) > policy.max_tokens:
        raise _reject("too_complex", "the SQL has too many tokens")
    text = sql.strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()

    first = next((t for t in tokens if not _punct(t, "(")), tokens[0])
    if _is(tokens[0], "SHOW"):
        _check_show(tokens, policy)
        return ValidatedSql(text, "show", parameters)
    if _is(tokens[0], "DESCRIBE"):
        _check_describe(tokens, policy)
        return ValidatedSql(text, "describe", parameters)
    if not _is(first, "SELECT", "WITH"):
        raise _reject("statement_not_allowed", "only SELECT, WITH, SHOW and DESCRIBE are allowed")
    _check_query(tokens, policy)
    return ValidatedSql(text, "select", parameters)

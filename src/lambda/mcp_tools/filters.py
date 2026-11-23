"""Shared filter arguments of the analytical tools and their parameterised SQL predicates.

Each filter has one advertised JSON Schema (strict pattern or taxonomy/allowlist enum) and one
fixed predicate. Values are never interpolated into SQL text: they are bound to `?` placeholders
through Athena execution parameters, as SQL string literals, in a fixed order. Dates are calendar
days in UTC; `endDate` is inclusive.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Final

from common.taxonomy import LIFECYCLE, RCA, UNCLASSIFIED

from mcp_tools.errors import ToolInputError
from mcp_tools.limits import McpLimits

MIN_DATE: Final = date(2000, 1, 1)
MAX_DATE: Final = date(2099, 12, 31)
SEVERITY_CODES: Final = ("low", "normal", "high", "urgent", "critical")
TIMESTAMP: Final = "from_iso8601_timestamp(timecreated)"

_DATE_SCHEMA: Final = {
    "type": "string",
    "format": "date",
    "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
    "minLength": 10,
    "maxLength": 10,
}

FILTER_SCHEMAS: Final[dict[str, dict[str, Any]]] = {
    "accountId": {
        "type": "string",
        "pattern": "^[0-9]{12}$",
        "minLength": 12,
        "maxLength": 12,
        "description": "Only cases from this 12-digit member account ID.",
    },
    "startDate": {
        **_DATE_SCHEMA,
        "description": "Only cases created on or after this UTC calendar date (YYYY-MM-DD).",
    },
    "endDate": {
        **_DATE_SCHEMA,
        "description": "Only cases created on or before this UTC calendar date (YYYY-MM-DD, "
        "inclusive).",
    },
    "serviceCode": {
        "type": "string",
        "pattern": "^[a-z0-9][a-z0-9-]*$",
        "minLength": 1,
        "maxLength": 128,
        "description": "Only cases for this AWS Support service code, for example "
        "'amazon-elastic-compute-cloud-linux'.",
    },
    "severityCode": {
        "type": "string",
        "enum": list(SEVERITY_CODES),
        "description": "Only cases with this Support severity code.",
    },
    "rcaCategory": {
        "type": "string",
        "enum": [*RCA.labels, UNCLASSIFIED],
        "description": "Only cases with this RCA category (exact label, or 'Unclassified').",
    },
    "lifecycleCategory": {
        "type": "string",
        "enum": [*LIFECYCLE.labels, UNCLASSIFIED],
        "description": "Only cases with this operational lifecycle category (exact label, or "
        "'Unclassified').",
    },
}

# Fixed predicate per filter, in the order parameters are bound.
_PREDICATES: Final = (
    ("accountId", "account_number = ?"),
    ("startDate", f"{TIMESTAMP} >= from_iso8601_timestamp(?)"),
    ("endDate", f"{TIMESTAMP} < from_iso8601_timestamp(?)"),
    ("serviceCode", "servicecode = ?"),
    ("severityCode", "severitycode = ?"),
    ("rcaCategory", "rca_category = ?"),
    ("lifecycleCategory", "lifecycle_category = ?"),
)


def filter_properties(*names: str) -> dict[str, dict[str, Any]]:
    """Schema properties for the named filters (copied, so callers may adjust descriptions)."""
    return {name: dict(FILTER_SCHEMAS[name]) for name in names}


def sql_literal(value: str) -> str:
    """Quote `value` as a SQL string literal for an Athena execution parameter."""
    return "'" + value.replace("'", "''") + "'"


@dataclass(frozen=True, slots=True)
class Selection:
    """Validated filters as a WHERE clause with bound parameters."""

    filters: dict[str, Any]
    predicates: tuple[str, ...]
    parameters: tuple[str, ...]

    @property
    def where(self) -> str:
        return f"WHERE {' AND '.join(self.predicates)}" if self.predicates else ""


def _check_dates(arguments: Mapping[str, Any], limits: McpLimits) -> None:
    errors: list[dict[str, str]] = []
    dates: dict[str, date] = {}
    for name in ("startDate", "endDate"):
        if name in arguments:
            value = date.fromisoformat(arguments[name])
            if not MIN_DATE <= value <= MAX_DATE:
                errors.append(
                    {"path": name, "message": f"must be between {MIN_DATE} and {MAX_DATE}"}
                )
            dates[name] = value
    if not errors and len(dates) == 2:  # noqa: PLR2004
        if dates["endDate"] < dates["startDate"]:
            errors.append({"path": "endDate", "message": "must not be before startDate"})
        elif (dates["endDate"] - dates["startDate"]).days > limits.max_date_range_days:
            errors.append({"path": "endDate", "message": "the date range is too long"})
    if errors:
        raise ToolInputError(errors)


def build_selection(
    arguments: Mapping[str, Any], names: Sequence[str], limits: McpLimits
) -> Selection:
    """Turn the validated filter arguments among `names` into predicates and parameters."""
    _check_dates(arguments, limits)
    predicates: list[str] = []
    parameters: list[str] = []
    filters: dict[str, Any] = {}
    for name, predicate in _PREDICATES:
        if name not in names or name not in arguments:
            continue
        value = str(arguments[name])
        filters[name] = arguments[name]
        if name == "startDate":
            value = f"{value}T00:00:00Z"
        elif name == "endDate":
            value = f"{date.fromisoformat(value) + timedelta(days=1)}T00:00:00Z"
        predicates.append(predicate)
        parameters.append(sql_literal(value))
    return Selection(filters, tuple(predicates), tuple(parameters))

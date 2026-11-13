"""A small JSON Schema (draft 2020-12 subset) validator for tool arguments.

The runtime layer carries only boto3 and Powertools, so tool schemas are validated here.
`check_schema` refuses any keyword this module does not enforce, which keeps every advertised
constraint enforced. Patterns follow ECMA-262 end-anchor semantics (a final `$` does not match
before a trailing newline). Issue messages name the constraint, never the supplied value.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from functools import cache
from typing import Any, Final

JsonSchema = Mapping[str, Any]

SUPPORTED_KEYWORDS: Final = frozenset(
    {
        "type",
        "title",
        "description",
        "default",
        "examples",
        "properties",
        "required",
        "additionalProperties",
        "enum",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "items",
        "minItems",
        "maxItems",
        "uniqueItems",
        "oneOf",
        "dependentRequired",
    }
)
SUPPORTED_FORMATS: Final = frozenset({"date"})
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TYPES: Final = frozenset({"object", "string", "integer", "boolean", "array"})


def check_schema(schema: JsonSchema, path: str = "") -> None:
    """Raise `ValueError` if `schema` uses a keyword, type or format this validator ignores."""
    unsupported = set(schema) - SUPPORTED_KEYWORDS
    if unsupported:
        raise ValueError(
            f"unsupported schema keyword(s) at {path or '<root>'}: {sorted(unsupported)}"
        )
    if "type" in schema and schema["type"] not in _TYPES:
        raise ValueError(f"unsupported schema type at {path or '<root>'}")
    if "format" in schema and schema["format"] not in SUPPORTED_FORMATS:
        raise ValueError(f"unsupported schema format at {path or '<root>'}")
    for name, child in schema.get("properties", {}).items():
        check_schema(child, f"{path}.{name}" if path else name)
    if "items" in schema:
        check_schema(schema["items"], f"{path}[]")
    for index, alternative in enumerate(schema.get("oneOf", ())):
        check_schema(alternative, f"{path}oneOf[{index}]")
    if schema.get("type") == "object" and schema.get("additionalProperties", True) is not False:
        raise ValueError(f"object schemas must set additionalProperties to false ({path})")


@cache
def _compiled(pattern: str) -> re.Pattern[str]:
    if pattern.endswith("$") and not pattern.endswith("\\$"):
        pattern = pattern[:-1] + r"\Z"
    return re.compile(pattern)


def _issue(path: str, message: str) -> dict[str, str]:
    return {"path": path or "<arguments>", "message": message}


def _type_ok(expected: str, value: Any) -> bool:  # noqa: ANN401
    if expected == "object":
        return isinstance(value, dict)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        if isinstance(value, bool):
            return False
        return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    if expected == "boolean":
        return isinstance(value, bool)
    return isinstance(value, list)


def _validate_string(schema: JsonSchema, value: str, path: str) -> list[dict[str, str]]:
    issues = []
    if "minLength" in schema and len(value) < schema["minLength"]:
        issues.append(_issue(path, f"must be at least {schema['minLength']} characters"))
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        issues.append(_issue(path, f"must be at most {schema['maxLength']} characters"))
    if "pattern" in schema and not _compiled(schema["pattern"]).search(value):
        issues.append(_issue(path, "does not match the required pattern"))
    if schema.get("format") == "date":
        try:
            valid = bool(_DATE.fullmatch(value)) and bool(date.fromisoformat(value))
        except ValueError:
            valid = False
        if not valid:
            issues.append(_issue(path, "must be a calendar date (YYYY-MM-DD)"))
    return issues


def _validate_object(schema: JsonSchema, value: dict[str, Any], path: str) -> list[dict[str, str]]:
    issues = []
    properties: Mapping[str, JsonSchema] = schema.get("properties", {})
    for name in schema.get("required", ()):
        if name not in value:
            issues.append(_issue(f"{path}.{name}" if path else name, "is required"))
    if schema.get("additionalProperties", True) is False:
        for name in value:
            if name not in properties:
                issues.append(
                    _issue(f"{path}.{name}" if path else name, "is not a supported argument")
                )
    for name, dependants in schema.get("dependentRequired", {}).items():
        if name in value:
            for dependant in dependants:
                if dependant not in value:
                    issues.append(
                        _issue(f"{path}.{name}" if path else name, f"requires {dependant}")
                    )
    for name, child in properties.items():
        if name in value:
            issues.extend(
                validate_arguments(child, value[name], f"{path}.{name}" if path else name)
            )
    issues.extend(_one_of_issues(schema, value, path))
    return issues


def _one_of_issues(schema: JsonSchema, value: dict[str, Any], path: str) -> list[dict[str, str]]:
    alternatives = schema.get("oneOf")
    if alternatives is None:
        return []
    matches = sum(1 for alt in alternatives if not validate_arguments(alt, value, path))
    if matches == 1:
        return []
    options = " or ".join(", ".join(alt.get("required", ())) or "<none>" for alt in alternatives)
    return [_issue(path, f"must provide exactly one of: {options}")]


def _validate_array(schema: JsonSchema, value: list[Any], path: str) -> list[dict[str, str]]:
    issues = []
    if "minItems" in schema and len(value) < schema["minItems"]:
        issues.append(_issue(path, f"must have at least {schema['minItems']} item(s)"))
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        issues.append(_issue(path, f"must have at most {schema['maxItems']} item(s)"))
    if schema.get("uniqueItems"):
        seen: list[Any] = []
        for item in value:
            if item in seen:
                issues.append(_issue(path, "items must be unique"))
                break
            seen.append(item)
    if "items" in schema:
        for index, item in enumerate(value):
            issues.extend(validate_arguments(schema["items"], item, f"{path}[{index}]"))
    return issues


def validate_arguments(schema: JsonSchema, value: Any, path: str = "") -> list[dict[str, str]]:  # noqa: ANN401
    """Return validation issues (`{path, message}`) for `value`; an empty list means valid."""
    expected = schema.get("type")
    if expected is not None and not _type_ok(expected, value):
        return [_issue(path, f"must be of type {expected}")]
    issues: list[dict[str, str]] = []
    if "enum" in schema and value not in schema["enum"]:
        issues.append(_issue(path, "must be one of the advertised values"))
    if isinstance(value, str):
        issues.extend(_validate_string(schema, value, path))
    elif isinstance(value, dict):
        issues.extend(_validate_object(schema, value, path))
    elif isinstance(value, list):
        issues.extend(_validate_array(schema, value, path))
    elif isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            issues.append(_issue(path, f"must be at least {schema['minimum']}"))
        if "maximum" in schema and value > schema["maximum"]:
            issues.append(_issue(path, f"must be at most {schema['maximum']}"))
    return issues


def normalise_arguments(schema: JsonSchema, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Copy validated top-level arguments, adding schema defaults and converting integral
    floats to `int`."""
    result: dict[str, Any] = {}
    for name, child in schema.get("properties", {}).items():
        if name in arguments:
            value = arguments[name]
            if child.get("type") == "integer" and isinstance(value, float):
                value = int(value)
            result[name] = value
        elif "default" in child:
            default = child["default"]
            result[name] = list(default) if isinstance(default, list) else default
    return result

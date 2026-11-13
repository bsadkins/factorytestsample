"""Defensive extraction of the first complete JSON object from model output.

Model replies may wrap JSON in prose or Markdown code fences, and string values may contain
braces or escaped quotes. Extraction strips fence markers, then scans for balanced braces while
tracking JSON string and escape state, and returns the first candidate that parses as an object.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any, Final, NoReturn

DEFAULT_MAX_CHARS: Final = 200_000

_FENCE = re.compile(r"```[A-Za-z0-9_+-]*[ \t]*")


class JsonExtractionError(ValueError):
    """No usable JSON object was found. `code` is a stable, content-free reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def strip_code_fences(text: str) -> str:
    """Remove Markdown code-fence markers (with optional language tags), keeping their contents."""
    return _FENCE.sub("", text)


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"Non-finite JSON number {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def iter_balanced_objects(text: str) -> Iterator[str]:
    """Yield each balanced `{...}` span in order of its opening brace, string- and escape-aware.

    A candidate that never closes is skipped and scanning resumes at the next opening brace.
    """
    length = len(text)
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, length):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : index + 1]
                    break
        start = text.find("{", start + 1)


def extract_first_json_object(text: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> dict[str, Any]:
    """Return the first complete JSON object in `text`.

    Raises `JsonExtractionError` when the input exceeds `max_chars` or contains no object that
    parses with finite numbers and unique keys. Error messages never include the input.
    """
    if len(text) > max_chars:
        raise JsonExtractionError("input_too_large", f"Model output exceeds {max_chars} characters")
    cleaned = strip_code_fences(text)
    for candidate in iter_balanced_objects(cleaned):
        try:
            # strict=False accepts raw newlines inside strings, which models sometimes emit.
            parsed = json.loads(
                candidate,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
                strict=False,
            )
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise JsonExtractionError("no_json_object", "Model output contains no complete JSON object")

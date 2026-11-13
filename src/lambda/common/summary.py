"""Validation of case summaries: 600-650 characters, 4-6 sentences of plain prose.

Summaries are never padded or truncated to pass; callers request a correction or fail.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

SUMMARY_MIN_CHARS: Final = 600
SUMMARY_MAX_CHARS: Final = 650
SUMMARY_MIN_SENTENCES: Final = 4
SUMMARY_MAX_SENTENCES: Final = 6

_ABBREVIATIONS: Final = frozenset(
    {"e.g", "i.e", "etc", "vs", "approx", "mr", "ms", "mrs", "dr", "inc", "ltd", "no", "u.s", "st"}
)
_TERMINATOR = re.compile(r"[.!?]+(?=[\"')\]]*(?:\s|$))")
_PREAMBLE = re.compile(
    r"^(?:summary|case summary|here is|here's|below is|sure|certainly|of course)\b", re.IGNORECASE
)
_SELF_AUDIT = re.compile(
    r"(?:\b(?:character|sentence|word)\s+count\b|\(\s*\d+\s*(?:characters|chars|sentences|words)\b"
    r"|\balternative\s+(?:summary|version)\b)",
    re.IGNORECASE,
)
_MARKDOWN = re.compile(r"(\*\*|__|`|^#{1,6}\s|\[[^\]]+\]\([^)]+\))", re.MULTILINE)
_LIST = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s")

_DESCRIPTIONS: Final = {
    "too_short": f"The summary must contain at least {SUMMARY_MIN_CHARS} characters.",
    "too_long": f"The summary must contain at most {SUMMARY_MAX_CHARS} characters.",
    "too_few_sentences": f"The summary must contain at least {SUMMARY_MIN_SENTENCES} sentences.",
    "too_many_sentences": f"The summary must contain at most {SUMMARY_MAX_SENTENCES} sentences.",
    "line_breaks": "The summary must be a single paragraph without line breaks.",
    "markdown": "The summary must not use Markdown formatting such as bold text, code or links.",
    "list_formatting": "The summary must be prose, not a list.",
    "preamble": "Return only the summary text, with no label, preamble or introduction.",
    "self_audit": (
        "Do not include character counts, sentence counts, notes or alternative versions."
    ),
    "unterminated": "The summary must end with complete sentence punctuation.",
}


@dataclass(frozen=True, slots=True)
class SummaryCheck:
    """Result of `check_summary`. `text` is the NFC-normalised, stripped summary."""

    text: str
    length: int
    sentences: int
    issues: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """True when the summary satisfies every constraint."""
        return not self.issues


def normalise_summary(text: str) -> str:
    """Return `text` NFC-normalised with surrounding whitespace removed."""
    return unicodedata.normalize("NFC", text).strip()


def count_sentences(text: str) -> int:
    """Count sentences, ignoring decimals and common abbreviations such as `e.g.`."""
    count = 0
    for match in _TERMINATOR.finditer(text):
        preceding = text[: match.start()].split()
        word = preceding[-1].lower() if preceding else ""
        if match.group().startswith(".") and word.lstrip("(\"'") in _ABBREVIATIONS:
            continue
        count += 1
    stripped = text.rstrip()
    if stripped and not _TERMINATOR.search(stripped[-1] + " "):
        count += 1
    return count


def check_summary(text: str) -> SummaryCheck:
    """Validate a model summary and return every constraint it violates."""
    summary = normalise_summary(text)
    length = len(summary)
    sentences = count_sentences(summary)
    issues: list[str] = []
    if length < SUMMARY_MIN_CHARS:
        issues.append("too_short")
    if length > SUMMARY_MAX_CHARS:
        issues.append("too_long")
    if sentences < SUMMARY_MIN_SENTENCES:
        issues.append("too_few_sentences")
    if sentences > SUMMARY_MAX_SENTENCES:
        issues.append("too_many_sentences")
    if "\n" in summary or "\r" in summary:
        issues.append("line_breaks")
    if _MARKDOWN.search(summary):
        issues.append("markdown")
    if _LIST.match(summary):
        issues.append("list_formatting")
    if _PREAMBLE.match(summary):
        issues.append("preamble")
    if _SELF_AUDIT.search(summary):
        issues.append("self_audit")
    if summary and summary[-1] not in ".!?":
        issues.append("unterminated")
    return SummaryCheck(text=summary, length=length, sentences=sentences, issues=tuple(issues))


def describe_issues(issues: Iterable[str]) -> list[str]:
    """Return generic correction instructions for issue codes; never includes summary text."""
    return [_DESCRIPTIONS[code] for code in issues if code in _DESCRIPTIONS]

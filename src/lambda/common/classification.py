"""Validation of RCA and lifecycle classifier replies against the shared taxonomy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from common.json_extract import JsonExtractionError, extract_first_json_object
from common.taxonomy import UNCLASSIFIED, Taxonomy

REASON_MAX_CHARS: Final = 500
FALLBACK_REASON_MAX_CHARS: Final = 160


@dataclass(frozen=True, slots=True)
class Classification:
    """A validated classification. `fallback` is True only for the explicit Unclassified result."""

    category: str
    reason: str
    fallback: bool = False

    def as_record(self, taxonomy: Taxonomy) -> dict[str, str]:
        """Return the processed-record fields, e.g. `{"RCA_Category": ..., "RCA_Reason": ...}`."""
        return {taxonomy.category_key: self.category, taxonomy.reason_key: self.reason}


class ClassificationError(ValueError):
    """A classifier reply failed validation.

    `code` is a stable reason that is safe to log. `correction_hint` is generic guidance for the
    single corrective prompt; neither ever contains model output.
    """

    def __init__(self, code: str, correction_hint: str) -> None:
        super().__init__(f"Invalid classification: {code}")
        self.code = code
        self.correction_hint = correction_hint


def _normalise_whitespace(value: str) -> str:
    return " ".join(value.split())


def parse_classification(text: str, taxonomy: Taxonomy) -> Classification:
    """Extract and validate a classification reply.

    The reply must contain a JSON object with string `taxonomy.category_key` and
    `taxonomy.reason_key` fields. The category must exactly match a regular category label;
    `Unclassified` is rejected here because it is only assigned by `unclassified()`. Extra keys
    are ignored and never copied into records.
    """
    keys = f"{taxonomy.category_key} and {taxonomy.reason_key}"
    try:
        payload = extract_first_json_object(text)
    except JsonExtractionError as exc:
        raise ClassificationError(
            exc.code, f"Respond with only one JSON object containing the keys {keys}."
        ) from None

    category = payload.get(taxonomy.category_key)
    if not isinstance(category, str) or not category.strip():
        raise ClassificationError(
            "missing_category",
            f"The JSON object must include {taxonomy.category_key} as a string.",
        )
    category = category.strip()
    if not taxonomy.is_valid(category):
        raise ClassificationError(
            "invalid_category",
            f"{taxonomy.category_key} must be exactly one of the allowed category labels, "
            "copied character for character.",
        )

    reason = payload.get(taxonomy.reason_key)
    if not isinstance(reason, str) or not reason.strip():
        raise ClassificationError(
            "missing_reason",
            f"The JSON object must include {taxonomy.reason_key} as a short, non-empty string.",
        )
    reason = _normalise_whitespace(reason)
    if len(reason) > REASON_MAX_CHARS:
        raise ClassificationError(
            "reason_too_long",
            f"{taxonomy.reason_key} must be at most {REASON_MAX_CHARS} characters.",
        )
    return Classification(category=category, reason=reason)


def unclassified(taxonomy: Taxonomy, failure_code: str) -> Classification:
    """Return the explicit fallback used after one corrective attempt has also failed validation."""
    reason = (
        f"Automatic {taxonomy.name} classification failed validation after one correction "
        f"({failure_code})."
    )
    return Classification(
        category=UNCLASSIFIED, reason=reason[:FALLBACK_REASON_MAX_CHARS], fallback=True
    )

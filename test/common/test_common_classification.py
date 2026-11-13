"""Classification validation against the shared taxonomies."""

import json

import pytest
from common.classification import (
    FALLBACK_REASON_MAX_CHARS,
    ClassificationError,
    parse_classification,
    unclassified,
)
from common.taxonomy import LIFECYCLE, RCA, UNCLASSIFIED


def test_valid_rca_classification() -> None:
    result = parse_classification(
        '{"RCA_Category": "Limit reached", "RCA_Reason": "The account hit its vCPU quota."}', RCA
    )
    assert (result.category, result.reason, result.fallback) == (
        "Limit reached",
        "The account hit its vCPU quota.",
        False,
    )
    assert result.as_record(RCA) == {
        "RCA_Category": "Limit reached",
        "RCA_Reason": "The account hit its vCPU quota.",
    }


def test_fenced_lifecycle_classification_with_whitespace_is_normalised() -> None:
    text = (
        '```json\n{"Lifecycle_Category": "Load Testing", '
        '"Lifecycle_Reason": "  Peak load \n was untested. "}\n```'
    )
    result = parse_classification(text, LIFECYCLE)
    assert result.category == "Load Testing"
    assert result.reason == "Peak load was untested."


def test_extra_keys_are_dropped_from_the_record() -> None:
    text = json.dumps({"RCA_Category": "Throttling", "RCA_Reason": "r", "confidence": 0.9})
    assert parse_classification(text, RCA).as_record(RCA) == {
        "RCA_Category": "Throttling",
        "RCA_Reason": "r",
    }


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("I think it is throttling.", "no_json_object"),
        ('{"RCA_Reason": "missing category"}', "missing_category"),
        ('{"RCA_Category": "Throttling"}', "missing_reason"),
        ('{"RCA_Category": "Throttling", "RCA_Reason": "   "}', "missing_reason"),
        ('{"RCA_Category": 3, "RCA_Reason": "r"}', "missing_category"),
        ('{"RCA_Category": "Rate Limit", "RCA_Reason": "r"}', "invalid_category"),
        ('{"RCA_Category": "throttling", "RCA_Reason": "r"}', "invalid_category"),
        ('{"RCA_Category": "Unclassified", "RCA_Reason": "r"}', "invalid_category"),
        ('{"Lifecycle_Category": "Tracing", "Lifecycle_Reason": "wrong keys"}', "missing_category"),
    ],
)
def test_invalid_rca_outputs(text: str, code: str) -> None:
    with pytest.raises(ClassificationError) as info:
        parse_classification(text, RCA)
    assert info.value.code == code
    assert info.value.correction_hint


def test_overlong_reason_is_rejected() -> None:
    text = json.dumps({"RCA_Category": "Throttling", "RCA_Reason": "x" * 501})
    with pytest.raises(ClassificationError) as info:
        parse_classification(text, RCA)
    assert info.value.code == "reason_too_long"


def test_error_string_does_not_echo_model_output() -> None:
    text = '{"RCA_Category": "SENTINEL-CASE-TEXT-7f3a", "RCA_Reason": "r"}'
    with pytest.raises(ClassificationError) as info:
        parse_classification(text, RCA)
    assert "SENTINEL" not in str(info.value)
    assert "SENTINEL" not in info.value.code


def test_unclassified_fallback_is_explicit_and_short() -> None:
    result = unclassified(LIFECYCLE, "invalid_category")
    assert result.category == UNCLASSIFIED
    assert result.fallback is True
    assert 0 < len(result.reason) <= FALLBACK_REASON_MAX_CHARS
    assert "invalid_category" in result.reason
    assert result.as_record(LIFECYCLE)["Lifecycle_Category"] == "Unclassified"

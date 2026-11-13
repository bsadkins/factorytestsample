"""Defensive extraction of the first complete JSON object from model output."""

import pytest
from common.json_extract import JsonExtractionError, extract_first_json_object, strip_code_fences


def test_plain_object() -> None:
    assert extract_first_json_object('{"a": 1}') == {"a": 1}


def test_surrounding_prose_is_ignored() -> None:
    text = 'Here you go: {"RCA_Category": "Throttling", "RCA_Reason": "Rate limits."} Thanks!'
    assert extract_first_json_object(text)["RCA_Category"] == "Throttling"


def test_code_fences_are_stripped() -> None:
    text = '```json\n{"Lifecycle_Category": "Tracing", "Lifecycle_Reason": "x"}\n```'
    assert extract_first_json_object(text)["Lifecycle_Category"] == "Tracing"
    assert strip_code_fences(text).strip().startswith("{")


def test_braces_inside_strings_do_not_break_counting() -> None:
    text = (
        '{"RCA_Reason": "The template used {placeholder} and a stray } brace", "k": 2} trailing {'
    )
    assert extract_first_json_object(text) == {
        "RCA_Reason": "The template used {placeholder} and a stray } brace",
        "k": 2,
    }


def test_escaped_quotes_inside_strings() -> None:
    text = r'{"reason": "He said \"use {braces}\" then left \\", "n": 1}'
    assert extract_first_json_object(text) == {
        "reason": 'He said "use {braces}" then left \\',
        "n": 1,
    }


def test_nested_objects_return_the_outer_object() -> None:
    text = 'x {"outer": {"inner": {"deep": [1, {"z": "}"}]}}, "after": true} {"second": 1}'
    assert extract_first_json_object(text) == {
        "outer": {"inner": {"deep": [1, {"z": "}"}]}},
        "after": True,
    }


def test_invalid_first_candidate_falls_through_to_next_complete_object() -> None:
    text = 'Use {category} wisely. {"RCA_Category": "Throttling", "RCA_Reason": "r"}'
    assert extract_first_json_object(text)["RCA_Category"] == "Throttling"


def test_unterminated_prefix_object_falls_through() -> None:
    text = 'broken { "a": [1, 2 then {"b": 2}'
    assert extract_first_json_object(text) == {"b": 2}


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("", "no_json_object"),
        ("no braces at all", "no_json_object"),
        ('{"a": 1', "no_json_object"),
        ("[1, 2, 3]", "no_json_object"),
        ('{"a": NaN}', "no_json_object"),
        ('{"a": 1, "a": 2}', "no_json_object"),
    ],
)
def test_malformed_inputs_raise(text: str, code: str) -> None:
    with pytest.raises(JsonExtractionError) as info:
        extract_first_json_object(text)
    assert info.value.code == code


def test_oversized_input_is_rejected_without_scanning() -> None:
    with pytest.raises(JsonExtractionError) as info:
        extract_first_json_object("{" * 10, max_chars=5)
    assert info.value.code == "input_too_large"


def test_error_message_never_contains_the_input() -> None:
    secret = "SENTINEL-CASE-TEXT-7f3a"
    with pytest.raises(JsonExtractionError) as info:
        extract_first_json_object(f"{secret} {{ not json")
    assert secret not in str(info.value)

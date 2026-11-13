"""Summary validation: 600-650 characters, 4-6 sentences of plain prose."""

import pytest
from common.summary import (
    SUMMARY_MAX_CHARS,
    SUMMARY_MIN_CHARS,
    check_summary,
    count_sentences,
    describe_issues,
)

BASE_SENTENCES = [
    (
        "The customer reported intermittent 503 errors from an Application Load Balancer "
        "in us-east-1 after a deployment of its customer-facing web application."
    ),
    (
        "Investigation showed the target group health checks were failing because the new "
        "container image listened on port 8081."
    ),
    (
        "Amazon ECS kept replacing tasks, which reduced healthy capacity and caused the "
        "elevated error rate for the web tier."
    ),
    (
        "The engineer recommended aligning the container port with the target group and "
        "adding a deployment circuit breaker."
    ),
    (
        "After the customer updated the task definition and redeployed, health checks "
        "passed and the errors stopped completely."
    ),
]


def valid_summary() -> str:
    text = " ".join(BASE_SENTENCES)
    assert SUMMARY_MIN_CHARS <= len(text) <= SUMMARY_MAX_CHARS, len(text)
    return text


def test_valid_summary_passes() -> None:
    result = check_summary(valid_summary())
    assert result.ok, result.issues
    assert result.sentences == 5
    assert SUMMARY_MIN_CHARS <= result.length <= SUMMARY_MAX_CHARS


def test_surrounding_whitespace_is_stripped_before_measuring() -> None:
    result = check_summary("\n  " + valid_summary() + "  \n")
    assert result.ok
    assert result.text == valid_summary()


def test_length_uses_unicode_characters_not_bytes() -> None:
    text = valid_summary().replace("customer", "cliënt\u00e9", 1)
    assert len(text.encode("utf-8")) > len(text)
    result = check_summary(text)
    assert result.length == len(text)


@pytest.mark.parametrize(
    ("text", "issue"),
    [
        ("Too short. Far too short. Still short. Short.", "too_short"),
        (valid_summary() + " " + "y" * 60 + ".", "too_long"),
    ],
)
def test_length_bounds(text: str, issue: str) -> None:
    assert issue in check_summary(text).issues


def test_sentence_bounds() -> None:
    three = (
        " ".join(s.replace(".", ",") for s in BASE_SENTENCES[:3])
        + ". "
        + BASE_SENTENCES[3]
        + " "
        + BASE_SENTENCES[4]
    )
    assert "too_few_sentences" in check_summary(three).issues
    many = " ".join(["Short sentence here."] * 31)
    assert "too_many_sentences" in check_summary(many).issues


def test_abbreviations_and_decimals_do_not_split_sentences() -> None:
    assert count_sentences("Python 3.12 runs on Lambda, e.g. for layers. It works.") == 2
    assert count_sentences("Is it fixed? Yes! Done...") == 3


@pytest.mark.parametrize(
    ("prefix", "suffix", "issue"),
    [
        ("# Summary\n", "", "line_breaks"),
        ("Summary: ", "", "preamble"),
        ("Here is the summary. ", "", "preamble"),
        ("", " (623 characters)", "self_audit"),
        ("", " Sentence count: 5.", "self_audit"),
        ("**Issue** ", "", "markdown"),
        ("- ", "", "list_formatting"),
    ],
)
def test_formatting_violations(prefix: str, suffix: str, issue: str) -> None:
    assert issue in check_summary(prefix + valid_summary() + suffix).issues


def test_unterminated_summary_is_flagged() -> None:
    assert "unterminated" in check_summary(valid_summary().rstrip(".")).issues


def test_legitimate_numbers_with_characters_are_not_self_audits() -> None:
    text = valid_summary().replace("port 8081", "a password of 8 characters", 1)
    assert "self_audit" not in check_summary(text).issues


def test_issue_descriptions_are_safe_generic_text() -> None:
    result = check_summary("SENTINEL-CASE-TEXT-7f3a.")
    described = describe_issues(result.issues)
    assert described
    assert all("SENTINEL" not in line for line in described)
    assert any("600" in line for line in described)


@pytest.mark.parametrize(
    ("plain", "emphasised"),
    [
        ("customer reported", "*customer* reported"),
        ("customer reported", "_customer_ reported"),
        ("customer reported", "(*customer*) reported"),
        ("in us-east-1 after", "in _us-east-1_, after"),
        ("customer reported", "*customer reported*"),
    ],
    ids=["asterisk", "underscore", "asterisk-in-parentheses", "underscore-before-comma", "phrase"],
)
def test_single_delimiter_emphasis_is_markdown(plain: str, emphasised: str) -> None:
    text = valid_summary().replace(plain, emphasised, 1)
    assert emphasised in text
    result = check_summary(text)
    assert "markdown" in result.issues
    assert SUMMARY_MIN_CHARS <= result.length <= SUMMARY_MAX_CHARS


@pytest.mark.parametrize(
    "technical",
    [
        "the ec2_instance_id tag",
        "a * b/2*3 tasks",
        "my_var and other_var values",
        "the *.log and *.txt files",
        "an s3:Get* action",
        "2*3*4 hosts",
    ],
    ids=["snake-case", "arithmetic", "two-identifiers", "globs", "iam-wildcard", "product"],
)
def test_technical_underscores_and_asterisks_are_not_markdown(technical: str) -> None:
    text = valid_summary().replace("port 8081", technical, 1)
    assert technical in text
    assert "markdown" not in check_summary(text).issues


def test_markdown_issue_description_mentions_emphasis() -> None:
    (description,) = describe_issues(["markdown"])
    assert "emphasis" in description

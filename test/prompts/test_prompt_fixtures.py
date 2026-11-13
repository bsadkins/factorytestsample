"""Prompt fixtures: rendered prompts and synthetic model replies through the shared validators."""

import json
from pathlib import Path
from typing import Any

import pytest
from common.bedrock import DEFAULT_MAX_PROMPT_CHARS
from common.classification import ClassificationError, parse_classification, unclassified
from common.summary import check_summary
from common.taxonomy import UNCLASSIFIED, taxonomy_for
from common.templates import (
    placeholders,
    render_classification_prompt,
    render_summary_prompt,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "test" / "fixtures" / "prompts"
TEMPLATES = REPO_ROOT / "src" / "templates"


def cases(name: str) -> list[dict[str, Any]]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    result: list[dict[str, Any]] = data["cases"]
    return result


def ids(name: str) -> list[str]:
    return [case["id"] for case in cases(name)]


@pytest.mark.parametrize("case", cases("summaries.json"), ids=ids("summaries.json"))
def test_summary_fixtures(case: dict[str, Any]) -> None:
    result = check_summary(case["text"])
    assert sorted(result.issues) == sorted(case["expected_issues"]), (
        result.length,
        result.sentences,
    )


@pytest.mark.parametrize("case", cases("classifications.json"), ids=ids("classifications.json"))
def test_classification_fixtures(case: dict[str, Any]) -> None:
    taxonomy = taxonomy_for(case["taxonomy"])
    if "error" in case:
        with pytest.raises(ClassificationError) as info:
            parse_classification(case["response"], taxonomy)
        assert info.value.code == case["error"]
        fallback = unclassified(taxonomy, info.value.code).as_record(taxonomy)
        assert fallback[taxonomy.category_key] == UNCLASSIFIED
    else:
        record = parse_classification(case["response"], taxonomy).as_record(taxonomy)
        assert record == case["expected"]
        assert set(record) == {taxonomy.category_key, taxonomy.reason_key}


@pytest.mark.parametrize("case", cases("case_annotations.json"), ids=ids("case_annotations.json"))
def test_rendered_prompts_are_complete_and_bounded(case: dict[str, Any]) -> None:
    summary_prompt = render_summary_prompt(case["annotation"], directory=TEMPLATES)
    assert case["annotation"] in summary_prompt
    assert len(summary_prompt) < DEFAULT_MAX_PROMPT_CHARS
    assert summary_prompt.count("<case_annotation>\n") == 1
    for name in ("rca", "lifecycle"):
        taxonomy = taxonomy_for(name)
        prompt = render_classification_prompt(taxonomy, "Synthetic summary.", directory=TEMPLATES)
        assert placeholders(prompt) <= placeholders("Synthetic summary.")
        for label in taxonomy.labels:
            assert f"- {label}: " in prompt


def test_placeholder_text_inside_case_content_is_not_expanded() -> None:
    annotation = next(c for c in cases("case_annotations.json") if c["id"] == "injection-attempt")
    prompt = render_summary_prompt(annotation["annotation"], directory=TEMPLATES)
    assert "{categories} {case_summary}" in prompt
    assert "- Throttling:" not in prompt


def test_live_regression_is_opt_in() -> None:
    source = (REPO_ROOT / "test" / "prompts" / "test_live_bedrock_prompts.py").read_text(
        encoding="utf-8"
    )
    assert "pytest.mark.live_bedrock" in source
    assert 'os.environ.get("CASE_INSIGHTS_LIVE_BEDROCK") != "1"' in source

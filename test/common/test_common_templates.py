"""Prompt templates render from the shared taxonomy and are content-versioned."""

import shutil
from pathlib import Path

import pytest
from common import templates
from common.taxonomy import LIFECYCLE, RCA, Taxonomy
from common.templates import (
    TEMPLATE_FILES,
    TemplateError,
    load_template,
    prompt_version,
    render,
    render_classification_correction,
    render_classification_prompt,
    render_summary_correction,
    render_summary_prompt,
)


def test_expected_template_files_exist(templates_dir: Path) -> None:
    for name in ("summary-template.txt", "rca-template.txt", "lifecycle-template.txt"):
        assert (templates_dir / name).is_file()
        assert name in TEMPLATE_FILES


def test_summary_template_contract(templates_dir: Path) -> None:
    text = load_template("summary-template.txt")
    assert "{case_annotation}" in text
    for phrase in (
        "AWS Support Engineer",
        "4",
        "6",
        "600",
        "650",
        "error",
        "service",
        "resolution",
    ):
        assert phrase.lower() in text.lower()
    for forbidden in ("header", "markdown", "preamble"):
        assert forbidden in text.lower()


def test_summary_prompt_embeds_the_case_annotation(templates_dir: Path) -> None:
    prompt = render_summary_prompt("Customer saw {categories} in logs")
    assert "Customer saw {categories} in logs" in prompt
    assert "{case_annotation}" not in prompt


@pytest.mark.parametrize("taxonomy", [RCA, LIFECYCLE])
def test_classification_prompts_list_every_category_and_definition(
    templates_dir: Path, taxonomy: Taxonomy
) -> None:
    prompt = render_classification_prompt(taxonomy, "A synthetic summary.")
    for category in taxonomy.categories:
        assert f"- {category.label}: {category.definition}" in prompt
    assert taxonomy.category_key in prompt
    assert taxonomy.reason_key in prompt
    assert "A synthetic summary." in prompt
    assert "Unclassified" not in prompt


def test_template_files_do_not_duplicate_taxonomy_definitions(templates_dir: Path) -> None:
    for name in TEMPLATE_FILES:
        raw = (templates_dir / name).read_text(encoding="utf-8")
        for category in (*RCA.categories, *LIFECYCLE.categories):
            assert category.definition not in raw, (name, category.label)


def test_correction_prompts(templates_dir: Path) -> None:
    summary = render_summary_correction(
        ["The summary must contain between 600 and 650 characters."]
    )
    assert "600 and 650" in summary
    correction = render_classification_correction(
        RCA, "The category must be one of the allowed categories."
    )
    assert "RCA_Category" in correction
    assert "- Throttling:" in correction


def test_render_is_single_pass_and_strict() -> None:
    assert render("A {x} B", {"x": "{y}"}) == "A {y} B"
    with pytest.raises(TemplateError, match="missing"):
        render("A {x} {y}", {"x": "1"})
    with pytest.raises(TemplateError, match="unused"):
        render("A {x}", {"x": "1", "z": "2"})
    assert render('{"RCA_Category": "<label>"} {x}', {"x": "1"}) == '{"RCA_Category": "<label>"} 1'


def test_unknown_template_names_are_rejected(templates_dir: Path) -> None:
    with pytest.raises(TemplateError, match="Unknown template"):
        load_template("../etc/passwd")


def test_missing_directory_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(TemplateError, match="not found"):
        load_template("summary-template.txt", directory=tmp_path / "missing")


def test_template_with_wrong_placeholders_is_rejected(tmp_path: Path, templates_dir: Path) -> None:
    copy = tmp_path / "templates"
    shutil.copytree(templates_dir, copy)
    (copy / "summary-template.txt").write_text("No placeholder here", encoding="utf-8")
    with pytest.raises(TemplateError, match="placeholders"):
        load_template("summary-template.txt", directory=copy)


def test_prompt_version_changes_with_template_content(tmp_path: Path, templates_dir: Path) -> None:
    baseline = prompt_version(directory=templates_dir)
    assert baseline.startswith("pv-")
    assert len(baseline) == 3 + 16
    copy = tmp_path / "templates"
    shutil.copytree(templates_dir, copy)
    assert prompt_version(directory=copy) == baseline
    path = copy / "rca-template.txt"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert prompt_version(directory=copy) != baseline


def test_default_directory_is_opt_templates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CASE_INSIGHTS_TEMPLATES_DIR", raising=False)
    assert templates.templates_dir() == Path("/opt/templates")

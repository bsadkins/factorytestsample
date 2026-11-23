"""The taxonomy module is the single source of category labels and definitions."""

import pytest
from common.taxonomy import (
    LIFECYCLE,
    RCA,
    TAXONOMIES,
    UNCLASSIFIED,
    Taxonomy,
    taxonomy_for,
)

# Literal labels from library/brief-reference.md; this list is deliberately independent of the code.
EXPECTED_RCA = [
    "Customer Question",
    "Limit reached",
    "Development Issue",
    "Customer Release",
    "Lack Monitoring",
    "Customer Dependency",
    "Throttling",
    "Feature Request",
    "Exceeding Capability",
    "Security Issue",
    "Transient Issues",
    "ICE Error",
    "Service Event",
    "AWS Release",
    "AWS Single Component Failure",
    "Upgrade Management",
]
EXPECTED_LIFECYCLE = [
    "Anomaly Detection",
    "Alarming",
    "AB Testing",
    "Architecture",
    "Backup/DR",
    "CICD",
    "Code Review",
    "Dependency Mapping",
    "Event Management",
    "Load Testing",
    "Logging",
    "Monitoring",
    "Performance Benchmarking",
    "Resilience",
    "Run Books",
    "Training",
    "Fault Injection and GameDay",
    "Ops Reviews",
    "Tracing",
    "Risk Evaluation",
]


def test_rca_labels_match_the_brief_exactly() -> None:
    assert list(RCA.labels) == EXPECTED_RCA


def test_lifecycle_labels_match_the_brief_exactly() -> None:
    assert list(LIFECYCLE.labels) == EXPECTED_LIFECYCLE


def test_classifier_json_keys_are_exact() -> None:
    assert (RCA.category_key, RCA.reason_key) == ("RCA_Category", "RCA_Reason")
    assert (LIFECYCLE.category_key, LIFECYCLE.reason_key) == (
        "Lifecycle_Category",
        "Lifecycle_Reason",
    )


@pytest.mark.parametrize("taxonomy", [RCA, LIFECYCLE])
def test_every_category_has_a_single_line_definition(taxonomy: Taxonomy) -> None:
    assert TAXONOMIES[taxonomy.name] is taxonomy
    for category in taxonomy.categories:
        assert category.definition.strip() == category.definition
        assert "\n" not in category.definition
        assert 20 <= len(category.definition) <= 200
        assert category.definition.endswith(".")


def test_unclassified_is_a_fallback_not_a_regular_category() -> None:
    assert UNCLASSIFIED == "Unclassified"
    assert UNCLASSIFIED not in RCA.labels
    assert UNCLASSIFIED not in LIFECYCLE.labels
    assert not RCA.is_valid(UNCLASSIFIED)
    assert RCA.is_record_value(UNCLASSIFIED)
    assert LIFECYCLE.is_record_value("Tracing")
    assert not LIFECYCLE.is_record_value("Made Up")


def test_validity_is_exact_and_case_sensitive() -> None:
    assert RCA.is_valid("ICE Error")
    assert not RCA.is_valid("ice error")
    assert not RCA.is_valid(" ICE Error")
    assert LIFECYCLE.definition("Backup/DR").startswith("Maintaining")
    with pytest.raises(KeyError):
        LIFECYCLE.definition("Throttling")


def test_taxonomy_lookup_by_name() -> None:
    assert taxonomy_for("rca") is RCA
    assert taxonomy_for("lifecycle") is LIFECYCLE
    with pytest.raises(ValueError, match="Unknown taxonomy"):
        taxonomy_for("severity")


def test_prompt_rendering_lists_every_label_with_its_definition() -> None:
    rendered = RCA.render_prompt_list()
    lines = rendered.splitlines()
    assert len(lines) == len(EXPECTED_RCA)
    for line, category in zip(lines, RCA.categories, strict=True):
        assert line == f"- {category.label}: {category.definition}"


def test_markdown_rendering_contains_every_definition_once() -> None:
    table = LIFECYCLE.render_markdown_table()
    assert table.splitlines()[0] == "| Category | Definition |"
    for category in LIFECYCLE.categories:
        assert table.count(f"| {category.label} | {category.definition} |") == 1


def test_taxonomies_are_immutable() -> None:
    with pytest.raises(AttributeError):
        RCA.categories = ()  # type: ignore[misc]

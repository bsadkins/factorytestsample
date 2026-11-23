"""Prompt templates from the content-versioned templates layer (`/opt/templates`).

Templates use `{name}` placeholders. Rendering is a single pass, so placeholder-like text inside
case content is never expanded. Category lists are rendered from `common.taxonomy`; template
files never repeat category definitions.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Final

from common.taxonomy import LIFECYCLE, RCA, Taxonomy

TEMPLATES_DIR_ENV: Final = "CASE_INSIGHTS_TEMPLATES_DIR"
DEFAULT_TEMPLATES_DIR: Final = Path("/opt/templates")

SUMMARY_TEMPLATE: Final = "summary-template.txt"
RCA_TEMPLATE: Final = "rca-template.txt"
LIFECYCLE_TEMPLATE: Final = "lifecycle-template.txt"
SUMMARY_CORRECTION_TEMPLATE: Final = "summary-correction-template.txt"
CLASSIFICATION_CORRECTION_TEMPLATE: Final = "classification-correction-template.txt"

TEMPLATE_FILES: Final[Mapping[str, frozenset[str]]] = {
    SUMMARY_TEMPLATE: frozenset({"case_annotation"}),
    RCA_TEMPLATE: frozenset({"case_summary", "categories"}),
    LIFECYCLE_TEMPLATE: frozenset({"case_summary", "categories"}),
    SUMMARY_CORRECTION_TEMPLATE: frozenset({"issues"}),
    CLASSIFICATION_CORRECTION_TEMPLATE: frozenset(
        {"problem", "categories", "category_key", "reason_key"}
    ),
}
_CLASSIFICATION_TEMPLATES: Final = {RCA.name: RCA_TEMPLATE, LIFECYCLE.name: LIFECYCLE_TEMPLATE}
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


class TemplateError(Exception):
    """A template is missing, unknown or has unexpected placeholders."""


def templates_dir(directory: str | Path | None = None) -> Path:
    """Resolve the templates directory.

    Order: the argument, then `CASE_INSIGHTS_TEMPLATES_DIR`, then `/opt/templates`.
    """
    if directory is not None:
        return Path(directory)
    return Path(os.environ.get(TEMPLATES_DIR_ENV, str(DEFAULT_TEMPLATES_DIR)))


def placeholders(text: str) -> set[str]:
    """Return the placeholder names used in `text`."""
    return set(_PLACEHOLDER.findall(text))


@lru_cache(maxsize=32)
def _read_template(path: Path, name: str, mtime_ns: int, size: int) -> str:
    del mtime_ns, size  # Part of the cache key so edited files are re-read.
    text = path.read_text(encoding="utf-8")
    expected = TEMPLATE_FILES[name]
    if placeholders(text) != expected:
        raise TemplateError(f"Template {name} placeholders must be exactly {sorted(expected)}")
    return text


def load_template(name: str, *, directory: str | Path | None = None) -> str:
    """Load and validate one of the known template files."""
    if name not in TEMPLATE_FILES:
        raise TemplateError(f"Unknown template {name!r}")
    path = templates_dir(directory) / name
    if not path.is_file():
        raise TemplateError(f"Template {name} not found in {path.parent}")
    stat = path.stat()
    return _read_template(path, name, stat.st_mtime_ns, stat.st_size)


def render(template: str, values: Mapping[str, str]) -> str:
    """Substitute every `{name}` placeholder exactly once; missing or unused values are errors."""
    used = placeholders(template)
    missing = used - values.keys()
    if missing:
        raise TemplateError(f"Template values missing for {sorted(missing)}")
    unused = values.keys() - used
    if unused:
        raise TemplateError(f"Template values unused: {sorted(unused)}")
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


def render_summary_prompt(case_annotation: str, *, directory: str | Path | None = None) -> str:
    """Render the summary prompt for a full case conversation."""
    return render(
        load_template(SUMMARY_TEMPLATE, directory=directory), {"case_annotation": case_annotation}
    )


def render_classification_prompt(
    taxonomy: Taxonomy, case_summary: str, *, directory: str | Path | None = None
) -> str:
    """Render the RCA or lifecycle prompt with the taxonomy's categories and definitions."""
    template = load_template(_CLASSIFICATION_TEMPLATES[taxonomy.name], directory=directory)
    return render(
        template, {"case_summary": case_summary, "categories": taxonomy.render_prompt_list()}
    )


def render_summary_correction(issues: Sequence[str], *, directory: str | Path | None = None) -> str:
    """Render the single corrective follow-up for a summary that failed validation."""
    bullet_list = "\n".join(f"- {issue}" for issue in issues)
    return render(
        load_template(SUMMARY_CORRECTION_TEMPLATE, directory=directory), {"issues": bullet_list}
    )


def render_classification_correction(
    taxonomy: Taxonomy, problem: str, *, directory: str | Path | None = None
) -> str:
    """Render the single corrective follow-up for a classification that failed validation."""
    return render(
        load_template(CLASSIFICATION_CORRECTION_TEMPLATE, directory=directory),
        {
            "problem": problem,
            "categories": taxonomy.render_prompt_list(),
            "category_key": taxonomy.category_key,
            "reason_key": taxonomy.reason_key,
        },
    )


def prompt_version(*, directory: str | Path | None = None) -> str:
    """Return `pv-<16 hex>`: a digest of every template file and the rendered taxonomies.

    Any template or taxonomy change produces a new version for the `Prompt_Version` record field.
    """
    digest = hashlib.sha256()
    for name in sorted(TEMPLATE_FILES):
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(load_template(name, directory=directory).encode("utf-8") + b"\0")
    for taxonomy in (RCA, LIFECYCLE):
        digest.update(taxonomy.category_key.encode("utf-8") + b"\0")
        digest.update(taxonomy.reason_key.encode("utf-8") + b"\0")
        digest.update(taxonomy.render_prompt_list().encode("utf-8") + b"\0")
    return f"pv-{digest.hexdigest()[:16]}"

"""Taxonomy documentation generator CLI."""

import subprocess
import sys
from pathlib import Path

from common.taxonomy import LIFECYCLE, RCA

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "render_taxonomy_docs.py"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


def test_help() -> None:
    result = run("--help")
    assert result.returncode == 0
    assert "--check" in result.stdout


def test_stdout_contains_every_definition() -> None:
    result = run()
    assert result.returncode == 0
    for category in (*RCA.categories, *LIFECYCLE.categories):
        assert f"| {category.label} | {category.definition} |" in result.stdout
    assert "(16)" in result.stdout
    assert "(20)" in result.stdout


def test_write_then_check(tmp_path: Path) -> None:
    doc = tmp_path / "prompts.md"
    doc.write_text("# Prompts\n<!-- taxonomy:start -->\nstale\n<!-- taxonomy:end -->\nAfter\n")
    assert run("--check", str(doc)).returncode == 1
    assert run("--write", str(doc)).returncode == 0
    text = doc.read_text()
    assert "stale" not in text
    assert text.endswith("<!-- taxonomy:end -->\nAfter\n")
    assert run("--check", str(doc)).returncode == 0


def test_missing_markers(tmp_path: Path) -> None:
    doc = tmp_path / "x.md"
    doc.write_text("no markers")
    result = run("--check", str(doc))
    assert result.returncode == 2
    assert "taxonomy:start" in result.stderr

"""Pull request CI workflow: runs the full local gate with pinned tooling and no credentials."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


def workflow() -> str:
    return WORKFLOW.read_text("utf-8")


def run_commands(text: str) -> list[str]:
    """Shell lines of every step, from single-line and block `run:` entries."""
    commands: list[str] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)(?:- )?run: ?(.*)$", line)
        if match is None:
            continue
        if match.group(2) not in ("|", ">"):
            commands.append(match.group(2).strip())
            continue
        indent = len(match.group(1))
        for body in lines[index + 1 :]:
            if body.strip() and len(body) - len(body.lstrip()) <= indent:
                break
            if body.strip():
                commands.append(body.strip())
    return commands


def python_version() -> str:
    match = re.search(r"^\s*PYTHON_VERSION: '([^']+)'$", workflow(), re.MULTILINE)
    assert match is not None
    return match.group(1)


def test_runs_on_every_pull_request() -> None:
    text = workflow()
    on_block = text.split("\non:\n", 1)[1].split("\n\n", 1)[0]
    assert re.search(r"^  pull_request:\s*$", on_block, re.MULTILINE)


def test_runs_the_complete_verify_gate_after_locked_installs() -> None:
    commands = run_commands(workflow())
    assert "npm run verify" in commands
    install = commands.index("npm ci")
    venv = commands.index("python -m venv .venv")
    pip = commands.index(".venv/bin/python -m pip install --require-hashes -r requirements-dev.txt")
    assert max(install, venv, pip) < commands.index("npm run verify")
    assert not [command for command in commands if "npm install" in command]


def test_fails_when_verify_changes_the_working_tree() -> None:
    commands = run_commands(workflow())
    assert any("git status --porcelain" in command for command in commands)
    assert commands.index("npm run verify") < next(
        i for i, command in enumerate(commands) if "git status --porcelain" in command
    )


def test_every_action_is_pinned_to_a_commit() -> None:
    uses = re.findall(r"uses: (\S+)(.*)$", workflow(), re.MULTILINE)
    assert len(uses) >= 3
    for action, comment in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", action), action
        assert re.fullmatch(r" # v\d+\.\d+\.\d+", comment), action


def test_node_and_python_versions_are_exact() -> None:
    text = workflow()
    assert "node-version-file: .nvmrc" in text
    assert EXACT_VERSION.match((REPO_ROOT / ".nvmrc").read_text("utf-8").strip())
    version = python_version()
    assert EXACT_VERSION.match(version)
    assert version.startswith("3.12.")
    assert "python-version: ${{ env.PYTHON_VERSION }}" in text
    assert re.search(r"^\s*runs-on: ubuntu-\d+\.\d+$", text, re.MULTILINE)


def test_pip_cache_is_repository_local_and_keyed_on_the_locks() -> None:
    text = workflow()
    assert "PIP_CACHE_DIR: .cache/pip" in text
    assert "path: .cache/pip" in text
    assert "hashFiles('requirements.txt', 'requirements-dev.txt')" in text


def test_uses_no_credentials_or_write_permissions() -> None:
    text = workflow()
    permissions = text.split("\npermissions:\n", 1)[1].split("\n\n", 1)[0]
    assert permissions.strip() == "contents: read"
    assert "secrets." not in text
    assert "id-token" not in text
    assert "configure-aws-credentials" not in text
    assert "persist-credentials: false" in text


def test_readme_and_contributing_describe_the_workflow() -> None:
    node = (REPO_ROOT / ".nvmrc").read_text("utf-8").strip()
    for document in ("README.md", "CONTRIBUTING.md", "docs/development.md"):
        text = (REPO_ROOT / document).read_text("utf-8")
        assert ".github/workflows/ci.yml" in text, document
        assert "npm run verify" in text, document
        assert node in text, document
        assert python_version() in text, document

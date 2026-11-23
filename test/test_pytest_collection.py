"""Every pytest module under test/ is collected by the default `npm run test:py` run."""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_every_test_module_is_collected() -> None:
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-cov"]
    result = subprocess.run(  # noqa: S603
        [*command, "-p", "no:cacheprovider"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-2000:]
    collected = {line.split("::", 1)[0] for line in result.stdout.splitlines() if "::" in line}
    expected = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "test").rglob("test_*.py")
        if "__pycache__" not in path.parts
    }
    assert expected - collected == set()

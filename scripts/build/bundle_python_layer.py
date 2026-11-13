"""Build the shared Lambda layer from hash-verified Linux x86_64 CPython 3.12 wheels.

The layer contains every package pinned in the runtime lock plus the `common` package:

    <output>/python/<dependencies>
    <output>/python/common/...

Lambda mounts the layer at /opt, so the shared code imports from /opt/python/common. Only
pre-built wheels are accepted (`--only-binary=:all:` and `--require-hashes`): a missing wheel,
an unpinned requirement or a hash mismatch fails the build. Nothing is compiled on the host and
Docker is never used.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess  # nosec B404
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET_PLATFORM_ARGS = (
    "--platform",
    "manylinux_2_28_x86_64",
    "--implementation",
    "cp",
    "--python-version",
    "3.12",
    "--abi",
    "cp312",
)
IGNORED_NAMES = ("__pycache__", "*.pyc", "*.pyo", ".DS_Store", ".mypy_cache", ".pytest_cache")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(
        description=(
            "Build the shared Lambda layer (python/ dependencies and python/common) from "
            "hash-verified Linux x86_64 cp312 wheels. Docker is not used."
        )
    )
    parser.add_argument("--output", required=True, type=Path, help="Empty layer output directory.")
    parser.add_argument(
        "--requirements",
        type=Path,
        default=REPO_ROOT / "requirements.txt",
        help="Fully hashed runtime lock (default: requirements.txt).",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=REPO_ROOT / "src" / "lambda" / "common",
        help="Shared package copied to python/common (default: src/lambda/common).",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=REPO_ROOT / ".cache" / "pip",
        help="pip download cache (default: .cache/pip in the repository).",
    )
    parser.add_argument(
        "--find-links", type=Path, help="Additional local wheel directory to search."
    )
    parser.add_argument("--no-index", action="store_true", help="Do not contact a package index.")
    return parser.parse_args(argv)


def pip_command(args: argparse.Namespace, target: Path) -> list[str]:
    """Return the wheel-only, hash-verified pip install command."""
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--require-hashes",
        "--no-deps",
        "--only-binary=:all:",
        *TARGET_PLATFORM_ARGS,
        "--no-compile",
        "--target",
        str(target),
        "--disable-pip-version-check",
        "--no-input",
        "--quiet",
        "--cache-dir",
        str(args.cache_dir),
        "--requirement",
        str(args.requirements),
    ]
    if args.find_links is not None:
        command += ["--find-links", str(args.find_links)]
    if args.no_index:
        command.append("--no-index")
    return command


def build(args: argparse.Namespace) -> Path:
    """Install the locked wheels and copy the shared package; return the python/ directory."""
    if not args.requirements.is_file():
        raise SystemExit(f"Requirements lock not found: {args.requirements}")
    if not (args.source / "__init__.py").is_file():
        raise SystemExit(f"Shared package not found: {args.source}")
    target: Path = args.output / "python"
    if target.exists():
        raise SystemExit(f"Output already contains {target}; use an empty directory.")
    target.mkdir(parents=True)

    command = pip_command(args, target)
    result = subprocess.run(command, check=False)  # noqa: S603  # nosec B603
    if result.returncode != 0:
        raise SystemExit(
            f"pip could not install the locked wheels (exit {result.returncode}). Every "
            "requirement needs a hash-matching Linux x86_64 cp312 wheel; nothing is compiled."
        )
    # Console scripts are not importable from a layer and only add size.
    shutil.rmtree(target / "bin", ignore_errors=True)
    shutil.copytree(args.source, target / "common", ignore=shutil.ignore_patterns(*IGNORED_NAMES))
    return target


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    build(parse_args(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())

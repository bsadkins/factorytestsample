"""Wheel-only layer bundler, exercised offline with locally built fixture wheels."""

import base64
import hashlib
import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLER = REPO_ROOT / "scripts" / "build" / "bundle_python_layer.py"


def load_bundler() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bundle_python_layer", BUNDLER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_wheel(directory: Path, name: str = "fixturepkg", version: str = "1.0.0") -> Path:
    """Write a minimal pure-Python wheel without network access or build tools."""
    dist = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": b"VALUE = 'synthetic'\n",
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode(),
        f"{dist}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    record_lines = []
    for path, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record_lines.append(f"{path},sha256={digest},{len(data)}")
    record_lines.append(f"{dist}/RECORD,,")
    files[f"{dist}/RECORD"] = ("\n".join(record_lines) + "\n").encode()
    wheel = directory / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, data in files.items():
            archive.writestr(path, data)
    return wheel


def shared_source(directory: Path) -> Path:
    source = directory / "common"
    (source / "__pycache__").mkdir(parents=True)
    (source / "__init__.py").write_text('"""Synthetic shared package."""\n', encoding="utf-8")
    (source / "helper.py").write_text("X = 1\n", encoding="utf-8")
    (source / "__pycache__" / "helper.cpython-312.pyc").write_bytes(b"bytecode")
    return source


def run(tmp_path: Path, requirements: str, wheels: Path) -> subprocess.CompletedProcess[str]:
    lock = tmp_path / "requirements.txt"
    lock.write_text(requirements, encoding="utf-8")
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(BUNDLER),
            "--output",
            str(tmp_path / "out"),
            "--requirements",
            str(lock),
            "--source",
            str(shared_source(tmp_path)),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--find-links",
            str(wheels),
            "--no-index",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def wheels(tmp_path: Path) -> Path:
    directory = tmp_path / "wheels"
    directory.mkdir()
    return directory


def test_help_describes_wheel_only_docker_free_bundling() -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(BUNDLER), "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert "Docker is not used" in result.stdout
    assert "--requirements" in result.stdout


def test_pip_command_is_wheel_only_hash_verified_for_lambda_platform(tmp_path: Path) -> None:
    bundler = load_bundler()
    args = bundler.parse_args(["--output", str(tmp_path)])
    command = bundler.pip_command(args, tmp_path / "python")
    for flag in ("--require-hashes", "--only-binary=:all:", "--no-deps", "--no-compile"):
        assert flag in command
    joined = " ".join(command)
    assert "--platform manylinux_2_28_x86_64" in joined
    assert "--python-version 3.12" in joined
    assert "--abi cp312" in joined
    assert "--implementation cp" in joined
    assert "--no-index" not in command


def test_platform_matches_the_cdk_asset_hash_inputs() -> None:
    bundler = load_bundler()
    typescript = (REPO_ROOT / "lib" / "packaging" / "python-assets.ts").read_text(encoding="utf-8")
    for value in bundler.TARGET_PLATFORM_ARGS:
        assert f"'{value}'" in typescript


def test_builds_layer_layout_from_hashed_wheel(tmp_path: Path, wheels: Path) -> None:
    wheel = build_wheel(wheels)
    result = run(tmp_path, f"fixturepkg==1.0.0 --hash=sha256:{sha256(wheel)}\n", wheels)
    assert result.returncode == 0, result.stderr
    python = tmp_path / "out" / "python"
    assert (python / "fixturepkg" / "__init__.py").is_file()
    assert (python / "common" / "__init__.py").is_file()
    assert (python / "common" / "helper.py").is_file()
    assert not list(python.rglob("__pycache__"))
    assert not list(python.rglob("*.pyc"))
    assert not (python / "bin").exists()


def test_hash_mismatch_fails(tmp_path: Path, wheels: Path) -> None:
    build_wheel(wheels)
    result = run(tmp_path, f"fixturepkg==1.0.0 --hash=sha256:{'0' * 64}\n", wheels)
    assert result.returncode != 0
    assert "hash" in result.stderr.lower()
    assert not (tmp_path / "out" / "python" / "common").exists()


def test_unhashed_requirement_fails(tmp_path: Path, wheels: Path) -> None:
    build_wheel(wheels)
    result = run(tmp_path, "fixturepkg==1.0.0\n", wheels)
    assert result.returncode != 0
    assert "hash" in result.stderr.lower()


def test_source_distribution_is_never_built(tmp_path: Path, wheels: Path) -> None:
    sdist = wheels / "fixturepkg-1.0.0.tar.gz"
    sdist.write_bytes(b"not a real sdist")
    result = run(tmp_path, f"fixturepkg==1.0.0 --hash=sha256:{sha256(sdist)}\n", wheels)
    assert result.returncode != 0
    assert "locked wheels" in result.stderr


def test_missing_wheel_fails(tmp_path: Path, wheels: Path) -> None:
    result = run(tmp_path, f"absentpkg==1.0.0 --hash=sha256:{'1' * 64}\n", wheels)
    assert result.returncode != 0
    assert "cp312" in result.stderr


def test_non_empty_output_is_rejected(tmp_path: Path, wheels: Path) -> None:
    wheel = build_wheel(wheels)
    (tmp_path / "out" / "python").mkdir(parents=True)
    result = run(tmp_path, f"fixturepkg==1.0.0 --hash=sha256:{sha256(wheel)}\n", wheels)
    assert result.returncode != 0
    assert "empty directory" in result.stderr


def test_runtime_lock_is_fully_hashed_and_pinned() -> None:
    blocks: list[list[str]] = []
    for line in (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        if line[:1].isalpha():
            blocks.append([line])
        elif blocks and line.strip():
            blocks[-1].append(line)
    assert len(blocks) > 5
    for block in blocks:
        assert "==" in block[0], block[0]
        assert any("--hash=sha256:" in line for line in block), block[0]

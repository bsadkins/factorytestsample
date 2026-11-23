"""Shared fixtures for the common Lambda package tests."""

from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = REPO_ROOT / "src" / "templates"
SENTINEL = "SENTINEL-CASE-TEXT-7f3a"


@pytest.fixture
def templates_dir(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CASE_INSIGHTS_TEMPLATES_DIR", str(TEMPLATES_DIR))
    return TEMPLATES_DIR


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def mocked_aws(aws_env: None) -> Iterator[None]:
    with mock_aws():
        yield


@pytest.fixture
def bucket(mocked_aws: None) -> str:
    name = "case-insights-test-bucket"
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=name)
    return name

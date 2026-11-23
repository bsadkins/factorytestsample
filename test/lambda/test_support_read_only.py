"""No Lambda code can change an AWS Support case: annotation only reads communications into S3."""

from __future__ import annotations

import re
from pathlib import Path

LAMBDA_SOURCE = Path(__file__).resolve().parents[2] / "src" / "lambda"
SUPPORT_WRITE_OPERATIONS = (
    "AddCommunicationToCase",
    "AddAttachmentsToSet",
    "CreateCase",
    "ResolveCase",
    "RefreshTrustedAdvisorCheck",
)


def snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def test_no_lambda_source_references_a_support_write_operation() -> None:
    sources = {path: path.read_text(encoding="utf-8") for path in LAMBDA_SOURCE.rglob("*.py")}
    assert any("describe_communications" in text for text in sources.values())
    for path, text in sources.items():
        for operation in SUPPORT_WRITE_OPERATIONS:
            for name in (operation, snake_case(operation)):
                assert name not in text, f"{path} references Support write API {name}"

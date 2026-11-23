"""Opt-in prompt regression against the real Amazon Bedrock API.

Skipped unless CASE_INSIGHTS_LIVE_BEDROCK=1. Requires AWS credentials with bedrock:InvokeModel on
the selected approved global inference profile (CASE_INSIGHTS_LIVE_MODEL_ID, default Haiku 4.5)
in CASE_INSIGHTS_LIVE_REGION (default us-east-1). Inputs are the invented fixtures only. Each
stage allows the same single correction as the pipeline; no output is printed or logged.

    CASE_INSIGHTS_LIVE_BEDROCK=1 .venv/bin/pytest test/prompts/test_live_bedrock_prompts.py --no-cov
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest
from common.bedrock import DEFAULT_MODEL_ID, Message, bedrock_runtime_client, invoke_bedrock
from common.classification import ClassificationError, parse_classification
from common.summary import check_summary, describe_issues
from common.taxonomy import LIFECYCLE, RCA, Taxonomy
from common.templates import (
    render_classification_correction,
    render_classification_prompt,
    render_summary_correction,
    render_summary_prompt,
)
from types_boto3_bedrock_runtime.client import BedrockRuntimeClient

pytestmark = [
    pytest.mark.live_bedrock,
    pytest.mark.skipif(
        os.environ.get("CASE_INSIGHTS_LIVE_BEDROCK") != "1",
        reason="Live Bedrock prompt regression is opt-in (set CASE_INSIGHTS_LIVE_BEDROCK=1).",
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = REPO_ROOT / "src" / "templates"
ANNOTATIONS: list[dict[str, Any]] = json.loads(
    (REPO_ROOT / "test" / "fixtures" / "prompts" / "case_annotations.json").read_text(
        encoding="utf-8"
    )
)["cases"]


@pytest.fixture(scope="module")
def client() -> BedrockRuntimeClient:
    return bedrock_runtime_client(
        region_name=os.environ.get("CASE_INSIGHTS_LIVE_REGION", "us-east-1")
    )


@pytest.fixture(scope="module")
def model_id() -> str:
    return os.environ.get("CASE_INSIGHTS_LIVE_MODEL_ID", DEFAULT_MODEL_ID)


def summarise(client: BedrockRuntimeClient, model_id: str, annotation: str) -> str:
    prompt = render_summary_prompt(annotation, directory=TEMPLATES)
    reply = invoke_bedrock(client, model_id, prompt).text
    check = check_summary(reply)
    if check.ok:
        return check.text
    correction = render_summary_correction(describe_issues(check.issues), directory=TEMPLATES)
    messages = [Message("user", prompt), Message("assistant", reply), Message("user", correction)]
    retry = check_summary(invoke_bedrock(client, model_id, messages).text)
    assert retry.ok, retry.issues
    return retry.text


def classify(
    client: BedrockRuntimeClient, model_id: str, taxonomy: Taxonomy, summary: str
) -> dict[str, str]:
    prompt = render_classification_prompt(taxonomy, summary, directory=TEMPLATES)
    reply = invoke_bedrock(client, model_id, prompt).text
    try:
        return parse_classification(reply, taxonomy).as_record(taxonomy)
    except ClassificationError as exc:
        correction = render_classification_correction(
            taxonomy, exc.correction_hint, directory=TEMPLATES
        )
        messages = [
            Message("user", prompt),
            Message("assistant", reply),
            Message("user", correction),
        ]
        retry = invoke_bedrock(client, model_id, messages).text
        return parse_classification(retry, taxonomy).as_record(taxonomy)


@pytest.mark.parametrize("case", ANNOTATIONS, ids=[c["id"] for c in ANNOTATIONS])
def test_live_summary_and_classifications(
    client: BedrockRuntimeClient, model_id: str, case: dict[str, Any]
) -> None:
    summary = summarise(client, model_id, case["annotation"])
    rca = classify(client, model_id, RCA, summary)
    lifecycle = classify(client, model_id, LIFECYCLE, summary)
    assert RCA.is_valid(rca["RCA_Category"])
    assert LIFECYCLE.is_valid(lifecycle["Lifecycle_Category"])

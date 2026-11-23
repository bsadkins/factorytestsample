"""Security wording checks: the README and guides must describe the alarm topic policy, the
deferred MCP tool checks and Amazon Bedrock model activation exactly as they are implemented.

- The alarm topic policy (`lib/constructs/observability.ts`) denies only `sns:Publish` without
  TLS, and its single Allow is for CloudWatch alarms of this deployment. It does not stop
  same-account principals whose own IAM policies allow `sns:Publish` from publishing over TLS.
- Seven MCP tools read Athena; `analyze_with_bedrock` invokes only the configured Bedrock model
  over the question and data it is given (`src/lambda/mcp_tools/tool_analysis.py`).
- AWS requires the identity that starts a model's first AWS Marketplace subscription to hold
  `aws-marketplace:Subscribe`, `Unsubscribe` and `ViewSubscriptions`. The runtime roles hold no
  `Unsubscribe` (`lib/constructs/bedrock-invoke-policy.ts`), so a separately authorised operator
  activates the model.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
DOCS = REPO_ROOT / "docs"
MODEL_ACCESS_PERMISSIONS = "https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html#model-access-permissions"
ATHENA_TOOLS = (
    "query_athena",
    "get_case_summary",
    "get_rca_analysis",
    "get_lifecycle_analysis",
    "get_service_trends",
    "get_temporal_patterns",
    "analyze_case_summaries",
)
MARKETPLACE_ACTIONS = (
    "aws-marketplace:Subscribe",
    "aws-marketplace:Unsubscribe",
    "aws-marketplace:ViewSubscriptions",
)

# Every document or source file that describes the alarm topic policy.
SNS_SOURCES = (
    README,
    REPO_ROOT / "CHANGELOG.md",
    DOCS / "architecture.md",
    DOCS / "installation.md",
    DOCS / "monitoring.md",
    DOCS / "permissions.md",
    DOCS / "resources.md",
    DOCS / "security.md",
    REPO_ROOT / "lib" / "constructs" / "observability.ts",
    REPO_ROOT / "test" / "cdk" / "constructs" / "observability.test.ts",
)
# Claims broader than the policy: a blanket TLS denial, or an exclusive publisher.
OVERSTATED_SNS_CLAIMS = (
    r"only this deployment's alarms may publish",
    r"only CloudWatch alarms of this deployment may publish",
    r"only `cloudwatch\.amazonaws\.com` may publish",
    r"accepts publishing only from",
    r"denies any request that does not use TLS",
    r"the alarm topic deny requests without TLS",
    r"queues and topics accept only TLS",
    r"TLS-only publishing restricted to",
    r"CloudWatch-only publishing",
    r"alarm topic without TLS are denied",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def flat(text: str) -> str:
    """Text with line breaks and runs of spaces collapsed, so phrases can span wrapped lines."""
    return re.sub(r"\s+", " ", text)


def section(path: Path, heading: str) -> str:
    text = read(path)
    start = text.index(f"\n{heading}\n")
    level = heading.split(" ", 1)[0]
    following = re.search(rf"\n{re.escape(level)} ", text[start + len(heading) + 2 :])
    end = len(text) if following is None else start + len(heading) + 2 + following.start()
    return text[start:end]


# Alarm topic policy ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", SNS_SOURCES, ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_alarm_topic_policy_is_not_overstated(path: Path) -> None:
    text = flat(read(path))
    found = [claim for claim in OVERSTATED_SNS_CLAIMS if re.search(claim, text, re.IGNORECASE)]
    assert found == []


@pytest.mark.parametrize(
    "path", [README, DOCS / "monitoring.md", DOCS / "security.md"], ids=lambda p: p.name
)
def test_alarm_topic_docs_disclose_identity_authorised_publishers(path: Path) -> None:
    text = flat(read(path))
    assert "denies `sns:Publish` without TLS" in text
    assert re.search(r"own IAM policies allow `sns:Publish`", text)
    assert "over TLS" in text


def test_readme_states_the_exact_alarm_topic_policy() -> None:
    text = flat(section(README, "## Security"))
    assert "Buckets and queues deny every request without TLS" in text
    assert "`aws:SourceAccount`" in text
    assert "`CaseInsights-<uniqueId>-*`" in text
    assert "not encrypted at rest" in text
    assert "no customer managed KMS key" in text
    assert "contain no case content" in text


# Deferred MCP tool checks ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "heading"),
    [
        (README, "## Deferred live verification"),
        (DOCS / "installation.md", "## Deferred live verification"),
    ],
    ids=["README.md", "installation.md"],
)
def test_deferred_tool_checks_match_each_tools_backend(path: Path, heading: str) -> None:
    text = flat(section(path, heading))
    assert not re.search(r"(each|every|all)( of the)? eight (MCP )?tools?[^.;]*Athena", text)
    assert "eight tools" in text
    assert "seven Athena-backed tools" in text
    for tool in ATHENA_TOOLS:
        assert f"`{tool}`" in text
    assert re.search(
        r"`analyze_with_bedrock`[^.;]*supplied question and data[^.;]*configured Bedrock model"
        r"[^.;]*no Athena query",
        text,
    )
    assert re.search(r"`analyze_case_summaries`[^.;]*Athena[^.;]*then[^.;]*Bedrock", text)
    assert "unsigned requests" in text.lower()
    assert "403" in text


# Model activation -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [README, DOCS / "permissions.md", DOCS / "security.md", DOCS / "installation.md"],
    ids=lambda p: p.name,
)
def test_model_activation_is_an_operator_step_with_every_prerequisite(path: Path) -> None:
    text = flat(read(path))
    assert "separately authorised operator" in text
    for action in MARKETPLACE_ACTIONS:
        assert f"`{action}`" in text
    assert "valid payment method" in text
    assert re.search(r"first-time use \(FTU\)", text)
    assert MODEL_ACCESS_PERMISSIONS in text


@pytest.mark.parametrize(
    "path",
    [
        README,
        DOCS / "permissions.md",
        DOCS / "security.md",
        DOCS / "installation.md",
        REPO_ROOT / "lib" / "constructs" / "bedrock-invoke-policy.ts",
    ],
    ids=lambda p: p.name,
)
def test_runtime_roles_are_not_promised_to_complete_the_first_subscription(path: Path) -> None:
    text = flat(read(path))
    assert not re.search(r"so (that )?the first invocation can complete", text)
    assert re.search(r"not sufficient to (complete|start)[^.]*first[^.]*subscription", text)


@pytest.mark.parametrize(
    "path", [DOCS / "permissions.md", DOCS / "security.md"], ids=lambda p: p.name
)
def test_only_subscribe_is_scoped_to_the_model_product(path: Path) -> None:
    text = flat(read(path))
    assert re.search(r"[Oo]nly `aws-marketplace:Subscribe` can be (scoped|limited)", text)
    assert "`aws-marketplace:Unsubscribe`" in text
    assert re.search(r"(hold|have) no `aws-marketplace:Unsubscribe`", text)

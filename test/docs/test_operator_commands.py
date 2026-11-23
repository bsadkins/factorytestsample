"""Checks that the operator commands in the README and guides can be copied as written.

Nothing here calls AWS. The rules come from the pinned CLIs: `cdk bootstrap` run from the
repository root synthesises the app in `cdk.json`, so it needs the app-mode context, and the
regional AWS CLI calls must name the deployment Region rather than rely on the profile default.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import botocore.session
import pytest
from botocore import xform_name
from botocore.session import Session

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = sorted([REPO_ROOT / "README.md", *(REPO_ROOT / "docs").glob("*.md")])

# Services whose CLI calls are global or pinned to one Region by AWS.
GLOBAL_SERVICES = frozenset({"iam", "organizations", "sts", "ce", "budgets", "sso", "support"})
AWS_COMMAND = re.compile(r"(?<![\w/.-])aws ([a-z0-9-]+) ([a-z0-9-]+)")


def commands(document: Path) -> Iterator[tuple[int, str, str, str]]:
    """Yield (line, service, operation, command text) for each AWS CLI command in a document.

    Inline code ends at its closing backtick; fenced commands continue across trailing `\\`.
    """
    lines = document.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        for match in AWS_COMMAND.finditer(line):
            text = line[match.start() :]
            if line[: match.start()].count("`") % 2 == 1:
                text = text.split("`", 1)[0]
            else:
                follow = index
                while text.rstrip().endswith("\\") and follow + 1 < len(lines):
                    follow += 1
                    text = text.rstrip()[:-1] + " " + lines[follow].strip()
            yield index + 1, match.group(1), match.group(2), text


def all_commands() -> list[tuple[str, int, str, str, str]]:
    return [
        (doc.relative_to(REPO_ROOT).as_posix(), line, service, operation, text)
        for doc in DOCS
        for line, service, operation, text in commands(doc)
    ]


def test_every_bootstrap_example_selects_an_app_mode() -> None:
    examples = [
        (doc.relative_to(REPO_ROOT).as_posix(), number, line)
        for doc in DOCS
        for number, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), start=1)
        if "npx cdk bootstrap" in line
    ]
    assert examples
    missing = [
        f"{doc}:{number}"
        for doc, number, line in examples
        for command in re.findall(r"npx cdk bootstrap[^`\n]*", line)
        if not re.search(r"-c (env=(dev|prod|<env>)|pipeline=true)(?![\w-])", command)
    ]
    assert missing == []


def test_regional_aws_cli_commands_name_the_region() -> None:
    missing = [
        f"{doc}:{line}: {text[:120]}"
        for doc, line, service, _operation, text in all_commands()
        # A bare `aws <service> <operation>` without options is a reference, not a command.
        if service not in GLOBAL_SERVICES and " --" in text and "--region" not in text
    ]
    assert missing == []


def test_step_functions_listing_uses_the_cli_pagination_option() -> None:
    listings = [
        (doc, line, text)
        for doc, line, service, operation, text in all_commands()
        if (service, operation) == ("stepfunctions", "list-executions")
    ]
    assert listings
    assert [f"{doc}:{line}" for doc, line, text in listings if "--max-results" in text] == []
    assert all("--max-items" in text for _, _, text in listings)


def test_installation_documents_the_programmatic_anthropic_first_use_path() -> None:
    text = (REPO_ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
    assert "aws bedrock put-use-case-for-model-access" in text
    assert "aws bedrock get-use-case-for-model-access" in text
    assert "https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html" in text
    assert "bedrock:PutUseCaseForModelAccess" in text


@pytest.mark.parametrize(
    "phrase",
    [
        "cannot cost more than",
        "Upper bound on the scan cost",
        "a few dollars a month at most",
        "add a few dollars at most",
        "still costs up to about",
        "can scan up to about the cutoff",
    ],
)
def test_cost_guidance_makes_no_hard_ceiling_claims(phrase: str) -> None:
    for document in (REPO_ROOT / "README.md", REPO_ROOT / "docs" / "cost.md"):
        assert phrase not in document.read_text(encoding="utf-8"), (document.name, phrase)


def test_cost_guidance_says_cancelled_athena_queries_are_billed() -> None:
    text = " ".join((REPO_ROOT / "docs" / "cost.md").read_text(encoding="utf-8").split())
    assert "Cancelled queries are billed for the data they scanned" in text


@pytest.mark.parametrize(
    "heading",
    [
        "### Collect from a new organisational unit or account",
        "### Change a Bedrock or Lambda quota",
        "### Change the Bedrock model",
        "### Recover from a failed deployment or update",
    ],
)
def test_runbook_has_the_operational_procedures(heading: str) -> None:
    lines = (REPO_ROOT / "docs" / "runbook.md").read_text(encoding="utf-8").splitlines()
    assert heading in lines


def test_uncertain_checkpoint_recovery_documents_raw_version_listing_permission() -> None:
    runbook = (REPO_ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")
    permissions = runbook.rsplit("Permissions:", 1)[1].split("## Failure records", 1)[0]
    assert "s3:ListBucketVersions" in permissions
    assert "account_number=*/case_number=*/checkpoints/*" in permissions
    assert "s3:prefix" in permissions
    summary = (REPO_ROOT / "docs" / "permissions.md").read_text(encoding="utf-8")
    reprocess = next(
        line for line in summary.splitlines() if "| Reprocess committed cases |" in line
    )
    assert "s3:ListBucketVersions" in reprocess


LINK = re.compile(r"\]\(([^)\s]+)\)")
FENCE = re.compile(r"^\s*(```|~~~)")


def heading_anchors(document: Path) -> set[str]:
    """GitHub-style heading anchors, including the -1, -2 suffixes of repeated headings."""
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    in_fence = False
    for line in document.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        heading = re.match(r"#{1,6} (.+?)\s*$", line)
        if in_fence or not heading:
            continue
        title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading.group(1))
        slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def test_relative_links_and_anchors_resolve() -> None:
    broken = []
    for document in DOCS:
        in_fence = False
        for number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), start=1):
            if FENCE.match(line):
                in_fence = not in_fence
            if in_fence:
                continue
            for target in LINK.findall(re.sub(r"`[^`]*`", "", line)):
                if re.match(r"[a-z][a-z0-9+.-]*:", target):
                    continue
                path, _, anchor = target.partition("#")
                linked = (document.parent / path).resolve() if path else document
                missing_anchor = (
                    bool(anchor)
                    and linked.suffix == ".md"
                    and anchor not in heading_anchors(linked)
                )
                if not linked.exists() or missing_anchor:
                    broken.append(
                        f"{document.relative_to(REPO_ROOT).as_posix()}:{number}: {target}"
                    )
    assert broken == []


BOTOCORE_SERVICE = {"s3api": "s3"}
# AWS CLI options that every command accepts, and the pagination options of paginated operations.
GLOBAL_OPTIONS = frozenset(
    {
        "region",
        "profile",
        "output",
        "query",
        "no-paginate",
        "no-cli-pager",
        "debug",
        "endpoint-url",
        "cli-binary-format",
        "cli-input-json",
        "generate-cli-skeleton",
        "color",
    }
)
PAGINATION_OPTIONS = frozenset({"max-items", "starting-token", "page-size"})
# High-level AWS CLI commands that are not API operations.
CUSTOM_COMMANDS = frozenset(
    {("s3", "cp"), ("s3", "ls"), ("s3", "rm"), ("s3", "sync"), ("s3", "rb"), ("sso", "login")}
)
QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")


def documented_options(text: str) -> list[str]:
    """Option names of one command, ignoring quoted values and anything after the command ends."""
    bare = QUOTED.sub("''", text)
    bare = re.split(r"\s(?:\||;|&&|>|2>)|\)|\|\||\s#", bare, maxsplit=1)[0]
    return re.findall(r"(?<![\w-])--([a-z0-9-]+)", bare)


def operation_options(session: Session, service: str, operation: str) -> set[str] | None:
    """CLI options of one operation from its botocore model, or None for an unknown operation.

    For paginated operations the CLI hides the API's token and limit parameters and offers
    --max-items, --starting-token and --page-size instead.
    """
    name = BOTOCORE_SERVICE.get(service, service)
    model = session.get_service_model(name)
    operations = {xform_name(op, "-"): op for op in model.operation_names}
    if operation not in operations:
        return None
    shape = model.operation_model(operations[operation]).input_shape
    members = dict(shape.members) if shape is not None else {}
    options = set(GLOBAL_OPTIONS)
    options.update(xform_name(member, "-") for member in members)
    options.update(
        f"no-{xform_name(member, '-')}"
        for member, member_shape in members.items()
        if member_shape.type_name == "boolean"
    )
    try:
        paginator = session.get_paginator_model(name).get_paginator(operations[operation])
    except ValueError:
        return options
    for key in ("input_token", "limit_key"):
        values = paginator.get(key) or []
        options.difference_update(
            xform_name(value, "-") for value in ([values] if isinstance(values, str) else values)
        )
    return options | PAGINATION_OPTIONS


def test_documented_aws_cli_options_exist_in_the_botocore_models() -> None:
    session = botocore.session.get_session()
    problems = []
    for doc, line, service, operation, text in all_commands():
        if (service, operation) in CUSTOM_COMMANDS:
            continue
        options = operation_options(session, service, operation)
        if options is None:
            problems.append(f"{doc}:{line}: unknown operation aws {service} {operation}")
            continue
        problems.extend(
            f"{doc}:{line}: aws {service} {operation} --{option}"
            for option in documented_options(text)
            if option not in options
        )
    assert problems == []


def section(document: str, heading: str) -> str:
    """The text of one Markdown section, up to the next heading of the same or a higher level.

    Lines inside fenced code blocks, such as shell comments, are never headings.
    """
    lines = (REPO_ROOT / document).read_text(encoding="utf-8").splitlines()
    start = lines.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    end = len(lines)
    in_fence = False
    for index in range(start + 1, len(lines)):
        if FENCE.match(lines[index]):
            in_fence = not in_fence
        elif not in_fence and re.match(rf"#{{1,{level}}} ", lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])


def test_first_use_form_is_read_from_a_quoted_absolute_path() -> None:
    text = (REPO_ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
    assert re.search(r'^\s*FTU_FORM="\$HOME/[^"\s]+\.json"$', text, re.MULTILINE)
    assert '--form-data "fileb://$FTU_FORM"' in text
    assert re.findall(r"fileb://(?!\$FTU_FORM\"|`)\S*", text) == []


@pytest.mark.parametrize(
    ("document", "heading"),
    [
        ("docs/installation.md", "## Step 7: Validate the deployment"),
        ("README.md", "## Deployment validation"),
    ],
)
def test_validation_waits_for_processing_and_a_successful_crawl(
    document: str, heading: str
) -> None:
    text = section(document, heading)
    completion = text.index("runs/<runId>/completion.json")
    crawl = text.index("aws glue start-crawler")
    assert completion < crawl
    assert "casesProcessed" in text[:crawl]
    assert "LastCrawl" in text[crawl:]
    assert "SUCCEEDED" in text[crawl:]


@pytest.mark.parametrize(
    ("document", "heading"),
    [
        ("docs/installation.md", "## Step 7: Validate the deployment"),
        ("README.md", "## Deployment validation"),
        ("docs/athena.md", "## Running queries"),
    ],
)
def test_athena_results_are_read_only_after_the_query_succeeded(
    document: str, heading: str
) -> None:
    text = section(document, heading)
    start = text.index("aws athena start-query-execution")
    poll = text.index("aws athena get-query-execution", start)
    results = text.index("aws athena get-query-results", poll)
    assert re.search(r"QUEUED.*RUNNING", text[poll - 200 : results], re.DOTALL)
    assert '[ "$STATE" = SUCCEEDED ]' in text[poll:results]


def test_failed_deployment_diagnosis_is_scoped_to_the_failed_operation() -> None:
    text = section("docs/runbook.md", "### Recover from a failed deployment or update")
    assert "The oldest failed event is the cause" not in text
    assert "ClientRequestToken=='<client-request-token>'" in text
    assert "sort_by(" in text


def test_stackset_recovery_creates_missing_instances() -> None:
    text = section("docs/runbook.md", "### Recover from a failed deployment or update")
    create = [
        command
        for _, _, _, operation, command in all_commands()
        if operation == "create-stack-instances"
    ]
    assert "aws cloudformation create-stack-instances" in text
    assert "aws cloudformation update-stack-instances" in text
    assert create
    for command in create:
        assert "--operation-preferences" in command
        assert "AccountFilterType" in command
        assert "--regions <region>" in command


def test_organizations_listings_use_the_account_state_field() -> None:
    for doc, line, service, _operation, text in all_commands():
        if service == "organizations":
            assert not re.search(r"\bStatus\b", text), f"{doc}:{line}"

"""DLQ redrive CLI: the real bash entry point against a stub `aws` command on PATH.

The stub answers STS and SQS from a JSON state file and logs every invocation, so each test can
show exactly which AWS calls happened and that nothing was written before confirmation. Every
account, queue and task is invented; no real AWS call is possible.
"""

from __future__ import annotations

import json
import os
import pty
import re
import select
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "redrive-dlq.sh"
ACCOUNT = "111111111111"
OTHER_ACCOUNT = "222222222222"
REGION = "eu-west-2"
UNIQUE = "scitest"
WRITE_OPERATIONS = frozenset({"start-message-move-task", "cancel-message-move-task"})
TOKEN = re.compile(r"Redrive token: (rd-[0-9a-f]{12})")
STAGES = {
    "active-accounts": "CaseInsights-ActiveAccounts",
    "annotation": "CaseInsights-CaseAnnotation",
    "summary": "CaseInsights-CaseSummary",
}

FAKE_AWS = """#!{python}
import json, os, sys

state = json.load(open(os.environ["FAKE_AWS_STATE"], encoding="utf-8"))
args = sys.argv[1:]
with open(os.environ["FAKE_AWS_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")

# Global options (before the service) take one value; operation options take values up to the
# next option, like --attribute-names.
options, rest = {{}}, []
index = 0
while index < len(args):
    arg = args[index]
    index += 1
    if not arg.startswith("--"):
        rest.append(arg)
        continue
    values = []
    while index < len(args) and not args[index].startswith("--"):
        values.append(args[index])
        index += 1
        if len(rest) < 2:
            break
    options[arg[2:]] = values
service, operation = rest[0], rest[1]
started = os.path.exists(os.environ["FAKE_AWS_STATE"] + ".started")


def fail(code):
    sys.stderr.write(
        f"\\nAn error occurred ({{code}}) when calling the {{operation}} operation: synthetic\\n"
    )
    sys.exit(254)


if state.get("no_credentials"):
    sys.stderr.write("\\nUnable to locate credentials. You can configure credentials by running "
                     "\\"aws configure\\".\\n")
    sys.exit(253)
failures = state.get("failures_after_start" if started else "failures", {{}})
if f"{{service}} {{operation}}" in failures:
    fail(failures[f"{{service}} {{operation}}"])


def queue_name(url):
    return url.rsplit("/", 1)[-1]


if (service, operation) == ("sts", "get-caller-identity"):
    reply = {{"Account": state["account"], "UserId": "AIDASYNTHETIC",
              "Arn": f"arn:aws:iam::{{state['account']}}:user/operator"}}
elif (service, operation) == ("sqs", "get-queue-url"):
    name = options["queue-name"][0]
    if name not in state["queues"]:
        fail("AWS.SimpleQueueService.NonExistentQueue")
    reply = {{"QueueUrl": f"https://sqs.{{options['region'][0]}}.amazonaws.com/"
                          f"{{options['queue-owner-aws-account-id'][0]}}/{{name}}"}}
elif (service, operation) == ("sqs", "get-queue-attributes"):
    attributes = state["queues"][queue_name(options["queue-url"][0])]
    reply = {{"Attributes": {{k: v for k, v in attributes.items()
                             if k in options["attribute-names"]}}}}
elif (service, operation) == ("sqs", "list-message-move-tasks"):
    tasks = state.get("tasks_after_start" if started else "tasks", [])
    reply = {{"Results": tasks[: int(options.get("max-results", ["10"])[0])]}}
elif (service, operation) == ("sqs", "start-message-move-task"):
    open(os.environ["FAKE_AWS_STATE"] + ".started", "w").close()
    # SQS accepted the task, but the caller never sees a usable acknowledgement.
    if state.get("start_outcome") == "lost":
        sys.stderr.write('\\nRead timeout on endpoint URL: "https://sqs.example.invalid/"\\n')
        sys.exit(255)
    if state.get("start_outcome") == "garbled":
        print("<html>gateway error</html>")
        sys.exit(0)
    reply = {{"TaskHandle": "synthetic-task-handle-0001"}}
else:
    fail("UnsupportedOperation")
print(json.dumps(reply))
"""


def queue_arn(name: str, account: str = ACCOUNT, region: str = REGION) -> str:
    return f"arn:aws:sqs:{region}:{account}:{name}"


def deployment_queues(unique_id: str = UNIQUE, visible: int = 7) -> dict[str, dict[str, str]]:
    queues: dict[str, dict[str, str]] = {}
    for prefix in STAGES.values():
        source, dlq = f"{prefix}-{unique_id}", f"{prefix}-{unique_id}-DLQ"
        queues[dlq] = {
            "QueueArn": queue_arn(dlq),
            "ApproximateNumberOfMessages": str(visible),
            "ApproximateNumberOfMessagesNotVisible": "1",
        }
        queues[source] = {
            "QueueArn": queue_arn(source),
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": queue_arn(dlq), "maxReceiveCount": 5}
            ),
        }
    return queues


@dataclass
class Result:
    code: int
    out: str
    err: str
    calls: list[list[str]]

    def operations(self) -> list[str]:
        return [_operation(call) for call in self.calls]

    def writes(self) -> list[str]:
        return [op for op in self.operations() if op.split()[1] in WRITE_OPERATIONS]

    def call(self, operation: str) -> list[str]:
        [found] = [c for c in self.calls if _operation(c) == operation]
        return found


def _operation(call: list[str]) -> str:
    """`<service> <operation>` of an invocation; each global option before them has one value."""
    positional: list[str] = []
    arguments = iter(call)
    for arg in arguments:
        if arg.startswith("--"):
            next(arguments, None)
            continue
        positional.append(arg)
        if len(positional) == 2:
            break
    return " ".join(positional)


def option(call: list[str], name: str) -> list[str]:
    """The value of a single-valued option, as a list so a missing option compares unequal."""
    return [call[call.index(name) + 1]] if name in call else []


@dataclass
class Harness:
    root: Path
    state: dict[str, Any]

    @property
    def log(self) -> Path:
        return self.root / "aws-calls.jsonl"

    def environment(self) -> dict[str, str]:
        state_file = self.root / "aws-state.json"
        state_file.write_text(json.dumps(self.state), encoding="utf-8")
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("AWS_") and key != "PATH"
        }
        environment.update(
            PATH=f"{self.root / 'bin'}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            FAKE_AWS_STATE=str(state_file),
            FAKE_AWS_LOG=str(self.log),
        )
        return environment

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text("utf-8").splitlines()]

    def reset(self) -> None:
        self.log.unlink(missing_ok=True)
        (self.root / "aws-state.json.started").unlink(missing_ok=True)

    def run(self, *args: str, config: bool = True) -> Result:
        self.reset()
        command = [str(SCRIPT), *args]
        if config:
            command += ["--config-dir", str(self.root / "config")]
        completed = subprocess.run(  # noqa: S603 - the repository's own script
            command,
            env=self.environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return Result(completed.returncode, completed.stdout, completed.stderr, self.calls())

    def token(self, *args: str) -> str:
        result = self.run(*args, "--dry-run")
        assert result.code == 0, result.err
        match = TOKEN.search(result.out)
        assert match is not None, result.out
        return match.group(1)

    def interactive(self, reply: str, *args: str) -> Result:
        """Run with a terminal on stdin and stderr (where bash prompts) and type `reply`."""
        self.reset()
        main, secondary = pty.openpty()
        process = subprocess.Popen(  # noqa: S603 - the repository's own script
            [str(SCRIPT), *args, "--config-dir", str(self.root / "config")],
            env=self.environment(),
            stdin=secondary,
            stdout=subprocess.PIPE,
            stderr=secondary,
        )
        os.close(secondary)
        terminal = _read_until(main, b"Type the redrive token")
        os.write(main, reply.encode() + b"\n")
        out, _ = process.communicate(timeout=30)
        terminal += _read_until(main, None)
        os.close(main)
        return Result(
            process.returncode, out.decode(), terminal.decode(errors="replace"), self.calls()
        )


def _read_until(descriptor: int, marker: bytes | None, timeout: float = 30) -> bytes:
    """Read a terminal until `marker` appears, or until it closes when `marker` is None."""
    received = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and (marker is None or marker not in received):
        ready, _, _ = select.select([descriptor], [], [], 0.2)
        if not ready:
            if marker is None:
                break
            continue
        try:
            chunk = os.read(descriptor, 1024)
        except OSError:  # the terminal closed
            break
        if not chunk:
            break
        received += chunk
    return received


def write_config(directory: Path, env: str = "test", **overrides: object) -> None:
    document: dict[str, object] = {
        "uniqueId": UNIQUE,
        "orgId": "o-exampleorg1",
        "managementAccountId": ACCOUNT,
        "region": REGION,
        "environment": env,
        "memberRoleTargets": ["r-ab12"],
    }
    document.update(overrides)
    directory.mkdir(exist_ok=True)
    (directory / f"{env}.json").write_text(json.dumps(document), encoding="utf-8")


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    (tmp_path / "bin").mkdir()
    fake = tmp_path / "bin" / "aws"
    fake.write_text(FAKE_AWS.format(python=sys.executable), encoding="utf-8")
    fake.chmod(0o755)
    write_config(tmp_path / "config")
    state: dict[str, Any] = {
        "account": ACCOUNT,
        "queues": deployment_queues(),
        "tasks": [
            {
                "Status": "COMPLETED",
                "SourceArn": queue_arn(f"CaseInsights-CaseSummary-{UNIQUE}-DLQ"),
                "ApproximateNumberOfMessagesMoved": 3,
                "ApproximateNumberOfMessagesToMove": 3,
            }
        ],
        "tasks_after_start": [
            {
                "Status": "RUNNING",
                "TaskHandle": "synthetic-task-handle-0001",
                "ApproximateNumberOfMessagesMoved": 2,
                "ApproximateNumberOfMessagesToMove": 8,
            }
        ],
    }
    return Harness(tmp_path, state)


TARGET = ("--env", "test", "--queue", "summary")


def test_help_documents_targets_safety_permissions_and_meaning(harness: Harness) -> None:
    result = harness.run("--help", config=False)

    assert result.code == 0
    assert result.calls == []
    text = " ".join(result.out.split())
    for stage, prefix in STAGES.items():
        assert f"{stage} {prefix}-<uniqueId>-DLQ -> {prefix}-<uniqueId>" in text
    for phrase in (
        "managementAccountId",
        "RedrivePolicy target",
        "--dry-run",
        "redrive token",
        "does not mean they were processed",
        "answered as already processed",
        "sqs:StartMessageMoveTask",
        "sqs:SendMessage on the stage queue",
        "docs/runbook.md",
        "scripts/reprocess-cases.py",
        "3 cancelled or not confirmed",
    ):
        assert phrase in text, phrase


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((), "Usage:"),
        (("--queue", "summary"), "--env is required"),
        (("--env", "test"), "--queue is required"),
        (("--env", "Prod!", "--queue", "summary"), "--env must be a configuration name"),
        (("--env", "test", "--queue", "orders"), "--queue must be active-accounts"),
        (("--env", "test", "--queue", ""), "--queue needs a value"),
        (("--env", "--queue", "summary"), "--env needs a value"),
        ((*TARGET, "--rate", "0"), "--rate must be an integer between 1 and 500"),
        ((*TARGET, "--rate", "501"), "--rate must be an integer between 1 and 500"),
        ((*TARGET, "--rate", "ten"), "--rate must be an integer between 1 and 500"),
        ((*TARGET, "--dry-run", "--confirm", "rd-000000000000"), "cannot be combined"),
        ((*TARGET, "--all"), "unknown argument '--all'"),
        (
            ("--env", "test", "--queue", "workflow-failures"),
            "send its case reference fields to the summary queue with a new redriveId",
        ),
        (
            ("--env", "test", "--queue", "account-discovery-failures"),
            "invoke account lookup to start a fresh run",
        ),
    ],
)
def test_invalid_options_are_refused_before_any_aws_call(
    harness: Harness, args: tuple[str, ...], message: str
) -> None:
    result = harness.run(*args, config=False)

    assert result.code == 2
    assert message in result.err
    assert result.calls == []


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (lambda d: None, "configuration {d}/other.json does not exist"),
        (lambda d: (d / "other.json").write_text("{not json"), "is not readable JSON"),
        (lambda d: write_config(d, "other", environment="prod"), "is not for environment other"),
        (lambda d: write_config(d, "other", uniqueId="Bad_Id"), "has no valid uniqueId"),
        (lambda d: write_config(d, "other", region="mars"), "has no valid region"),
        (
            lambda d: write_config(d, "other", managementAccountId="12345"),
            "has no valid managementAccountId",
        ),
    ],
)
def test_unusable_configuration_is_refused_before_any_aws_call(
    harness: Harness, setup: Callable[[Path], object], message: str
) -> None:
    directory = harness.root / "config"
    setup(directory)

    result = harness.run("--env", "other", "--queue", "summary")

    assert result.code == 2
    assert message.format(d=directory) in result.err
    assert "Nothing was changed." in result.err
    assert result.calls == []


def test_credentials_of_another_account_are_refused_before_queues_are_read(
    harness: Harness,
) -> None:
    harness.state["account"] = OTHER_ACCOUNT

    result = harness.run(*TARGET, "--dry-run")

    assert result.code == 2
    assert f"credentials are for account {OTHER_ACCOUNT}" in result.err
    assert f"management account {ACCOUNT}" in result.err
    assert result.operations() == ["sts get-caller-identity"]


def test_missing_credentials_are_reported_without_writes(harness: Harness) -> None:
    harness.state["no_credentials"] = True

    result = harness.run(*TARGET)

    assert result.code == 2
    assert "AWS credentials are not usable" in result.err
    assert result.writes() == []


def _foreign_dlq_arn(state: dict[str, Any]) -> None:
    dlq = f"CaseInsights-CaseSummary-{UNIQUE}-DLQ"
    state["queues"][dlq]["QueueArn"] = queue_arn(dlq, account=OTHER_ACCOUNT)


def _other_region_source(state: dict[str, Any]) -> None:
    source = f"CaseInsights-CaseSummary-{UNIQUE}"
    state["queues"][source]["QueueArn"] = queue_arn(source, region="us-east-1")


def _redrive_to_other_environment(state: dict[str, Any]) -> None:
    other = deployment_queues("sciprod")
    state["queues"].update(other)
    source = f"CaseInsights-CaseSummary-{UNIQUE}"
    state["queues"][source]["RedrivePolicy"] = other["CaseInsights-CaseSummary-sciprod"][
        "RedrivePolicy"
    ]


def _no_redrive_policy(state: dict[str, Any]) -> None:
    del state["queues"][f"CaseInsights-CaseSummary-{UNIQUE}"]["RedrivePolicy"]


def _missing_dlq(state: dict[str, Any]) -> None:
    del state["queues"][f"CaseInsights-CaseSummary-{UNIQUE}-DLQ"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (_missing_dlq, f"queue CaseInsights-CaseSummary-{UNIQUE}-DLQ does not exist"),
        (_foreign_dlq_arn, f"queue CaseInsights-CaseSummary-{UNIQUE}-DLQ is not in account"),
        (_other_region_source, f"queue CaseInsights-CaseSummary-{UNIQUE} is not in account"),
        (
            _redrive_to_other_environment,
            "RedrivePolicy target: " + queue_arn("CaseInsights-CaseSummary-sciprod-DLQ"),
        ),
        (_no_redrive_policy, "RedrivePolicy target: none"),
    ],
)
def test_foreign_or_unpaired_queues_are_refused_without_writes(
    harness: Harness, change: Callable[[dict[str, Any]], None], message: str
) -> None:
    change(harness.state)

    for extra in (("--dry-run",), ("--confirm", "rd-000000000000")):
        result = harness.run(*TARGET, *extra)

        assert result.code == 2
        assert message in result.err
        assert result.writes() == []
        assert "list-message-move-tasks" not in " ".join(result.operations())


def test_dry_run_shows_the_exact_scope_and_writes_nothing(harness: Harness) -> None:
    result = harness.run(*TARGET, "--dry-run", "--profile", "ops-readonly")

    assert result.code == 0
    source, dlq = f"CaseInsights-CaseSummary-{UNIQUE}", f"CaseInsights-CaseSummary-{UNIQUE}-DLQ"
    assert f"Redrive preview for deployment {UNIQUE} (env test, account {ACCOUNT}, {REGION})" in (
        result.out
    )
    assert f"From dead-letter queue: {dlq} ({queue_arn(dlq)})" in result.out
    assert f"To stage queue:         {source} ({queue_arn(source)})" in result.out
    assert "(approximate): 7 visible, 1 in flight" in result.out
    assert TOKEN.search(result.out)
    assert "Dry run: no changes were made" in result.out
    assert result.writes() == []
    assert result.operations() == [
        "sts get-caller-identity",
        "sqs get-queue-url",
        "sqs get-queue-url",
        "sqs get-queue-attributes",
        "sqs get-queue-attributes",
        "sqs list-message-move-tasks",
    ]
    for call in result.calls:
        assert option(call, "--region") == [REGION]
        assert option(call, "--profile") == ["ops-readonly"]
    for call in result.calls[1:3]:
        assert option(call, "--queue-owner-aws-account-id") == [ACCOUNT]


@pytest.mark.parametrize("stage", sorted(STAGES))
def test_each_stage_pairs_its_own_dead_letter_queue_and_source(
    harness: Harness, stage: str
) -> None:
    token = harness.token("--env", "test", "--queue", stage)

    result = harness.run("--env", "test", "--queue", stage, "--confirm", token)

    assert result.code == 0, result.err
    start = result.call("sqs start-message-move-task")
    prefix = STAGES[stage]
    assert option(start, "--source-arn") == [queue_arn(f"{prefix}-{UNIQUE}-DLQ")]
    assert option(start, "--destination-arn") == [queue_arn(f"{prefix}-{UNIQUE}")]


def test_tokens_are_bound_to_the_queue_pair_and_deployment(harness: Harness) -> None:
    summary = harness.token(*TARGET)
    annotation = harness.token("--env", "test", "--queue", "annotation")
    write_config(harness.root / "config", "prod", uniqueId="sciprod")
    harness.state["queues"].update(deployment_queues("sciprod"))
    prod = harness.token("--env", "prod", "--queue", "summary")
    assert len({summary, annotation, prod}) == 3

    for wrong in (annotation, prod):
        result = harness.run(*TARGET, "--confirm", wrong)

        assert result.code == 3
        assert "does not match this redrive" in result.err
        assert result.writes() == []


def test_without_confirmation_nothing_is_started(harness: Harness) -> None:
    result = harness.run(*TARGET)

    assert result.code == 3
    token = TOKEN.search(result.out)
    assert token is not None
    assert f"rerun with --confirm {token.group(1)}" in result.err
    assert result.writes() == []


def test_interactive_cancel_starts_nothing(harness: Harness) -> None:
    result = harness.interactive("no", *TARGET)

    assert result.code == 3
    assert "Cancelled. Nothing was changed." in result.err
    assert result.writes() == []


def test_interactive_token_starts_the_move(harness: Harness) -> None:
    token = harness.token(*TARGET)

    result = harness.interactive(token, *TARGET)

    assert result.code == 0, result.err
    assert result.writes() == ["sqs start-message-move-task"]


def test_confirmed_redrive_starts_one_move_and_reports_it_honestly(harness: Harness) -> None:
    token = harness.token(*TARGET, "--rate", "25")

    result = harness.run(*TARGET, "--rate", "25", "--confirm", token)

    assert result.code == 0, result.err
    assert result.writes() == ["sqs start-message-move-task"]
    start = result.call("sqs start-message-move-task")
    dlq = queue_arn(f"CaseInsights-CaseSummary-{UNIQUE}-DLQ")
    assert option(start, "--source-arn") == [dlq]
    assert option(start, "--destination-arn") == [queue_arn(f"CaseInsights-CaseSummary-{UNIQUE}")]
    assert option(start, "--max-number-of-messages-per-second") == ["25"]
    assert option(start, "--region") == [REGION]
    assert "Started move task synthetic-task-handle-0001." in result.out
    assert "Task status: RUNNING (2 of about 8 messages moved)" in result.out
    assert "does not mean it was processed" in result.out
    assert "Committed cases are answered as already processed" in result.out
    assert f"list-message-move-tasks --source-arn {dlq} --region {REGION}" in result.out
    assert "cancel-message-move-task --task-handle synthetic-task-handle-0001" in result.out


def test_rate_is_left_to_sqs_unless_given(harness: Harness) -> None:
    result = harness.run(*TARGET, "--confirm", harness.token(*TARGET))

    assert result.code == 0
    assert "--max-number-of-messages-per-second" not in result.call("sqs start-message-move-task")


def test_empty_dead_letter_queue_needs_no_redrive(harness: Harness) -> None:
    harness.state["queues"] = deployment_queues(visible=0)
    for name in harness.state["queues"]:
        if name.endswith("-DLQ"):
            harness.state["queues"][name]["ApproximateNumberOfMessagesNotVisible"] = "0"

    result = harness.run(*TARGET, "--confirm", "rd-000000000000")

    assert result.code == 0
    assert "Nothing to redrive. No changes were made." in result.out
    assert not TOKEN.search(result.out)
    assert result.writes() == []


def test_an_active_move_task_blocks_a_second_one(harness: Harness) -> None:
    harness.state["tasks"] = [{"Status": "RUNNING", "TaskHandle": "synthetic-active-task"}]

    result = harness.run(*TARGET, "--confirm", "rd-000000000000")

    assert result.code == 2
    assert "already active (RUNNING synthetic-active-task)" in result.err
    assert result.writes() == []


@pytest.mark.parametrize(
    ("failures", "message"),
    [
        ({"sqs get-queue-url": "AccessDenied"}, "could not be looked up (AccessDenied)"),
        ({"sqs get-queue-attributes": "RequestThrottled"}, "could not be read (RequestThrottled)"),
        ({"sqs list-message-move-tasks": "AccessDenied"}, "could not be listed (AccessDenied)"),
        ({"sts get-caller-identity": "ExpiredToken"}, "caller identity could not be read"),
    ],
)
def test_read_failures_stop_before_any_write(
    harness: Harness, failures: dict[str, str], message: str
) -> None:
    harness.state["failures"] = failures

    result = harness.run(*TARGET, "--confirm", "rd-000000000000")

    assert result.code == 1
    assert message in result.err
    assert "synthetic" not in result.err  # the AWS error message is not echoed
    assert result.writes() == []


def test_a_refused_start_is_reported_as_nothing_moved(harness: Harness) -> None:
    token = harness.token(*TARGET)
    harness.state["failures"] = {"sqs start-message-move-task": "AccessDenied"}

    result = harness.run(*TARGET, "--confirm", token)

    assert result.code == 1
    assert "the move task was not started (AccessDenied). No messages were moved." in result.err
    assert "Started move task" not in result.out


@pytest.mark.parametrize(
    ("state", "detail"),
    [
        ({"start_outcome": "lost"}, "AWS CLI error"),
        ({"start_outcome": "garbled"}, "no task handle in the response"),
        ({"failures": {"sqs start-message-move-task": "InternalError"}}, "InternalError"),
        (
            {
                "failures": {
                    "sqs start-message-move-task": "AWS.SimpleQueueService.UnsupportedOperation"
                }
            },
            "AWS.SimpleQueueService.UnsupportedOperation",
        ),
    ],
    ids=["lost-acknowledgement", "unparseable-response", "server-error", "retry-rejected"],
)
def test_an_unconfirmed_start_is_reported_as_unknown_not_as_nothing_moved(
    harness: Harness, state: dict[str, Any], detail: str
) -> None:
    token = harness.token(*TARGET)
    harness.state.update(state)

    result = harness.run(*TARGET, "--confirm", token)

    assert result.code == 1
    output = result.out + result.err
    assert f"the move task start is UNKNOWN ({detail})" in result.err
    assert "No messages were moved" not in output
    assert "Started move task" not in result.out
    assert "inspect the dead-letter queue and its move tasks before retrying" in result.err
    dlq = f"CaseInsights-CaseSummary-{UNIQUE}-DLQ"
    assert (
        f"Progress: aws sqs list-message-move-tasks --source-arn {queue_arn(dlq)} --region {REGION}"
        in result.out
    )
    dlq_url = f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/{dlq}"
    assert (
        f"DLQ depth: aws sqs get-queue-attributes --queue-url {dlq_url} --attribute-names "
        f"ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible --region {REGION}"
    ) in result.out
    assert result.writes() == ["sqs start-message-move-task"]


@pytest.mark.parametrize("start_outcome", ["lost", "started"])
def test_printed_commands_use_the_selected_profile_and_region(
    harness: Harness, start_outcome: str
) -> None:
    profile = "case operations"  # a name that needs quoting in a shell command
    token = harness.token(*TARGET, "--profile", profile)
    if start_outcome == "lost":
        harness.state["start_outcome"] = "lost"

    result = harness.run(*TARGET, "--profile", profile, "--confirm", token)

    labels = ("Progress:", "DLQ depth:") if start_outcome == "lost" else ("Progress:", "Cancel:")
    assert result.code == (1 if start_outcome == "lost" else 0)
    commands = [
        line.split(":", 1)[1].strip() for line in result.out.splitlines() if line.startswith(labels)
    ]
    assert len(commands) == 2
    for command in commands:
        words = shlex.split(command)
        assert words[0] == "aws"
        assert option(words, "--profile") == [profile]
        assert option(words, "--region") == [REGION]
    if start_outcome == "started":
        return
    # The UNKNOWN-path inspection commands run as printed, with the same credentials.
    for command in commands:
        harness.reset()
        completed = subprocess.run(  # noqa: S603 - the printed command, against the stub
            ["bash", "-c", command],  # noqa: S607
            env=harness.environment(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        [call] = harness.calls()
        assert option(call, "--profile") == [profile]
        assert option(call, "--region") == [REGION]


def test_a_started_task_whose_status_cannot_be_read_is_not_reported_as_success(
    harness: Harness,
) -> None:
    token = harness.token(*TARGET)
    harness.state["failures_after_start"] = {"sqs list-message-move-tasks": "RequestThrottled"}

    result = harness.run(*TARGET, "--confirm", token)

    assert result.code == 1
    assert "Started move task synthetic-task-handle-0001." in result.out
    assert "status could not be read (RequestThrottled). Do not start another" in result.err


def test_a_failed_task_is_reported_with_its_reason(harness: Harness) -> None:
    token = harness.token(*TARGET)
    harness.state["tasks_after_start"] = [
        {
            "Status": "FAILED",
            "ApproximateNumberOfMessagesMoved": 3,
            "ApproximateNumberOfMessagesToMove": 8,
            "FailureReason": "AWS.SimpleQueueService.NonExistentQueue",
        }
    ]

    result = harness.run(*TARGET, "--confirm", token)

    assert result.code == 1
    assert (
        "Task status: FAILED (3 of about 8 messages moved): AWS.SimpleQueueService.NonExistentQueue"
        in result.out
    )

"""SigV4 stdio bridge: the real `scripts/mcp_aws_client.py` as a subprocess, talking to a loopback
HTTPS fixture that verifies every SigV4 signature independently of botocore and then hands the
request to the real MCP Lambda handler (synthetic data) or to a scripted failure.

Nothing leaves the loopback interface, instance metadata is disabled and every credential is
invented. These tests prove the bridge's behaviour, not API Gateway IAM, resource policy or WAF.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
import shlex
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from mcp_kit import dataset, make_kit
from mcp_tools.context import ToolContext
from sigv4_loopback import (
    ENV_KEY,
    ENV_TOKEN,
    POWERTOOLS_ENV,
    PROFILE_KEY,
    PROFILE_SECRET,
    PROFILE_TOKEN,
    PROTOCOL_VERSION,
    REGION,
    REPO_ROOT,
    SCRIPT,
    SERVICE,
    TIMEOUT,
    UNKNOWN_KEY,
    UNKNOWN_SECRET,
    UNKNOWN_TOKEN,
    Backend,
    Bridge,
    Fixture,
    Received,
    Reply,
    assert_clean,
    base_env,
    bridge_env,
    env_credentials,
    handler_backend,
    initialize,
    json_reply,
    make_tls_files,
    result_for,
    rpc,
    start_fixture,
    stop_fixture,
)

KIRO_EXAMPLE = REPO_ROOT / ".kiro" / "settings" / "mcp.json.example"
DOCS = REPO_ROOT / "docs" / "mcp-server-deployment.md"

SENTINEL = "SENTINEL-CASE-TEXT-5e1f"
EXPECTED_TOOLS = [
    "query_athena",
    "analyze_with_bedrock",
    "get_case_summary",
    "analyze_case_summaries",
    "get_temporal_patterns",
    "get_rca_analysis",
    "get_lifecycle_analysis",
    "get_service_trends",
]


@pytest.fixture(scope="module")
def tls_files(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    return make_tls_files(tmp_path_factory.mktemp("mcp-bridge-tls"))


@pytest.fixture
def fixture(tls_files: tuple[Path, Path]) -> Iterator[Fixture]:
    started = start_fixture(tls_files)
    yield started
    stop_fixture(started)


@pytest.fixture
def plain_fixture() -> Iterator[Fixture]:
    started = start_fixture(None)
    yield started
    stop_fixture(started)


@pytest.fixture
def lambda_backend(monkeypatch: pytest.MonkeyPatch) -> Backend:
    """The real MCP Lambda handler behind the fixture, with synthetic Athena/Bedrock stand-ins."""
    for name, value in POWERTOOLS_ENV.items():
        monkeypatch.setenv(name, value)
    app = importlib.import_module("mcp_server.app")

    def tool_context(remaining_millis: int | None) -> ToolContext:
        # The SQLite-backed Athena stand-in is bound to the thread that created it, and the
        # fixture serves each request on a new thread.
        return make_kit(dataset()).context(remaining_millis)

    monkeypatch.setattr(app, "tool_context", tool_context)
    return handler_backend(app)


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mcp_aws_client", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def client_module() -> ModuleType:
    return load_module()


# Transcript --------------------------------------------------------------------------------------


def test_stdio_transcript_is_signed_for_execute_api_and_forwarded(
    fixture: Fixture, lambda_backend: Backend, tmp_path: Path
) -> None:
    fixture.use(lambda_backend)
    bridge = Bridge(["--verbose"], bridge_env(tmp_path, fixture))
    sent: dict[str, bytes] = {}

    sent["initialize"] = bridge.send(initialize(1))
    init = bridge.recv()
    assert init["id"] == 1
    assert init["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert init["result"]["serverInfo"]["name"] == "support-case-insights"

    sent["notifications/initialized"] = bridge.send(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    sent["tools/list"] = bridge.send(rpc("tools/list", "list-1", {}))
    listed = bridge.recv()  # the notification produces no stdout line
    assert listed["id"] == "list-1"
    assert [tool["name"] for tool in listed["result"]["tools"]] == EXPECTED_TOOLS

    sent["tools/call"] = bridge.send(
        rpc(
            "tools/call", 7, {"name": "get_case_summary", "arguments": {"serviceCode": "amazon-s3"}}
        )
    )
    called = bridge.recv()
    assert called["id"] == 7
    assert called["result"]["isError"] is False
    summary = called["result"]["structuredContent"]
    # Synthetic dataset: four amazon-s3 cases, three Throttling and one Customer Question.
    assert summary["totalCases"] == 4
    assert {
        row["rcaCategory"]: row["cases"] for row in summary["distribution"] if row["cases"]
    } == {
        "Throttling": 3,
        "Customer Question": 1,
    }

    sent["ping"] = bridge.send(rpc("ping", "p"))
    assert bridge.recv() == {"jsonrpc": "2.0", "id": "p", "result": {}}

    code, rest, stderr = bridge.finish()
    assert code == 0
    assert rest == []
    assert len(bridge.stdout_lines) == 4  # one line per request, none for the notification

    by_method = {received.message()["method"]: received for received in fixture.received}
    assert set(by_method) == set(sent)
    assert len(fixture.received) == len(sent)
    for method, received in by_method.items():
        signature = received.signature
        assert signature.valid, signature.problem
        assert signature.service == SERVICE
        assert signature.region == REGION
        assert signature.access_key == ENV_KEY
        assert {"host", "x-amz-date", "x-amz-security-token", "content-type"} <= set(
            signature.signed_headers
        )
        assert received.headers["x-amz-security-token"] == ENV_TOKEN
        assert received.path == "/v1/mcp"
        assert received.body == sent[method]  # forwarded byte for byte
        assert received.headers["content-type"] == "application/json"
        assert received.headers["accept"] == "application/json, text/event-stream"
        expected_version = None if method == "initialize" else PROTOCOL_VERSION
        assert received.headers.get("mcp-protocol-version") == expected_version

    assert "tools/call get_case_summary -> HTTP 200" in stderr
    assert "credentials: env" in stderr
    assert_clean(stderr, b"".join(bridge.stdout_lines))
    assert "amazon-s3" not in stderr


@pytest.mark.parametrize("profile_source", ["AWS_PROFILE", "--profile"])
def test_shared_profile_session_credentials_and_profile_region(
    fixture: Fixture, tmp_path: Path, profile_source: str
) -> None:
    fixture.use(lambda received: result_for(received, {}))
    credentials_file = tmp_path / "credentials"
    credentials_file.write_text(
        "[case-insights-test]\n"
        f"aws_access_key_id = {PROFILE_KEY}\n"
        f"aws_secret_access_key = {PROFILE_SECRET}\n"
        f"aws_session_token = {PROFILE_TOKEN}\n"
    )
    config_file = tmp_path / "config"
    config_file.write_text("[profile case-insights-test]\nregion = eu-west-2\n")
    env = base_env(tmp_path, fixture.url, fixture.ca_bundle)
    del env["AWS_REGION"]  # the profile's Region is used
    env["AWS_SHARED_CREDENTIALS_FILE"] = str(credentials_file)
    env["AWS_CONFIG_FILE"] = str(config_file)
    args: list[str] = []
    if profile_source == "AWS_PROFILE":
        env["AWS_PROFILE"] = "case-insights-test"
    else:
        args = ["--profile", "case-insights-test"]

    bridge = Bridge(args, env)
    assert bridge.request(rpc("ping", 1)) == {"jsonrpc": "2.0", "id": 1, "result": {}}
    code, _, stderr = bridge.finish()

    assert code == 0
    (received,) = fixture.received
    assert received.signature.valid, received.signature.problem
    assert received.signature.access_key == PROFILE_KEY
    assert received.signature.region == "eu-west-2"
    assert received.headers["x-amz-security-token"] == PROFILE_TOKEN
    assert "credentials: shared-credentials-file" in stderr
    assert_clean(stderr)


def test_slow_request_does_not_block_others(fixture: Fixture, tmp_path: Path) -> None:
    def backend(received: Received) -> Reply:
        if received.message()["method"] == "tools/call":
            time.sleep(2)
        return result_for(received, {"method": received.message()["method"]})

    fixture.use(backend)
    bridge = Bridge([], bridge_env(tmp_path, fixture))
    bridge.send(rpc("tools/call", "slow", {"name": "get_temporal_patterns", "arguments": {}}))
    time.sleep(0.2)
    bridge.send(rpc("ping", "fast"))
    assert bridge.recv()["id"] == "fast"
    assert bridge.recv()["id"] == "slow"
    assert bridge.finish()[0] == 0


# Response and error propagation ------------------------------------------------------------------


def test_case_content_reaches_stdout_only(fixture: Fixture, tmp_path: Path) -> None:
    def backend(received: Received) -> Reply:
        message = received.message()
        if message["id"] == "ok":
            return result_for(received, {"content": [{"type": "text", "text": SENTINEL}]})
        error = {"code": -32602, "message": f"Invalid {SENTINEL}", "data": {"x": SENTINEL}}
        return json_reply({"jsonrpc": "2.0", "id": message["id"], "error": error})

    fixture.use(backend)
    bridge = Bridge(["--verbose"], bridge_env(tmp_path, fixture))
    arguments = {"name": "query_athena", "arguments": {"sql": f"SELECT '{SENTINEL}'"}}
    ok = bridge.request(rpc("tools/call", "ok", arguments))
    assert ok["result"]["content"][0]["text"] == SENTINEL
    failed = bridge.request(rpc("tools/call", "bad", arguments))
    assert failed["error"] == {
        "code": -32602,
        "message": f"Invalid {SENTINEL}",
        "data": {"x": SENTINEL},
    }
    code, _, stderr = bridge.finish()
    assert code == 0
    assert SENTINEL not in stderr
    assert "SELECT" not in stderr
    assert "tools/call query_athena -> HTTP 200" in stderr
    assert_clean(stderr)


def test_endpoint_jsonrpc_error_without_id_gets_request_id(
    fixture: Fixture, tmp_path: Path
) -> None:
    error = {"code": -32600, "message": "Request body exceeds 32768 bytes"}
    fixture.use(lambda received: json_reply({"jsonrpc": "2.0", "id": None, "error": error}, 413))
    bridge = Bridge([], bridge_env(tmp_path, fixture))
    assert bridge.request(rpc("tools/list", 42)) == {"jsonrpc": "2.0", "id": 42, "error": error}
    assert bridge.finish()[0] == 0


def test_signature_rejection_never_echoes_credentials(fixture: Fixture, tmp_path: Path) -> None:
    env = bridge_env(
        tmp_path, fixture, **env_credentials(UNKNOWN_KEY, UNKNOWN_SECRET, UNKNOWN_TOKEN)
    )
    bridge = Bridge(["--verbose"], env)
    reply = bridge.request(initialize("init"))
    code, _, stderr = bridge.finish()

    (received,) = fixture.received
    assert received.headers["x-amz-security-token"] == UNKNOWN_TOKEN  # the token was sent
    assert reply["id"] == "init"
    assert reply["error"]["code"] == -32000
    assert reply["error"]["data"] == {
        "source": "mcp_aws_client",
        "httpStatus": 403,
        "retryable": False,
        "errorType": "InvalidSignatureException",
    }
    assert "HTTP 403" in reply["error"]["message"]
    assert code == 0
    assert "HTTP 403 (InvalidSignatureException)" in stderr
    assert_clean(stderr, b"".join(bridge.stdout_lines))


@pytest.mark.parametrize(
    ("status", "headers", "body", "error_type", "retryable"),
    [
        (
            403,
            {"Content-Type": "application/json", "x-amzn-ErrorType": "AccessDeniedException"},
            b'{"Message":"User: arn:aws:sts::111122223333:assumed-role/x is not authorized"}',
            "AccessDeniedException",
            False,
        ),
        (
            404,
            {"x-amzn-ErrorType": "MissingAuthenticationTokenException"},
            b'{"message":"Missing Authentication Token"}',
            "MissingAuthenticationTokenException",
            False,
        ),
        (
            429,
            {"Content-Type": "application/json", "x-amzn-ErrorType": "TooManyRequestsException"},
            b'{"message":"Too Many Requests"}',
            "TooManyRequestsException",
            True,
        ),
        (502, {"Content-Type": "text/html"}, b"<html>Bad Gateway</html>", None, True),
        (
            504,
            {"Content-Type": "application/json"},
            b'{"message":"Endpoint timed out"}',
            None,
            True,
        ),
        (500, {"x-amzn-ErrorType": "bad type!"}, b"{}", None, False),
    ],
)
def test_http_errors_become_jsonrpc_errors_with_the_request_id(  # noqa: PLR0913, PLR0917
    fixture: Fixture,
    tmp_path: Path,
    status: int,
    headers: dict[str, str],
    body: bytes,
    error_type: str | None,
    retryable: bool,
) -> None:
    fixture.use(lambda received: (status, headers, body))
    bridge = Bridge([], bridge_env(tmp_path, fixture))
    reply = bridge.request(rpc("tools/call", "c1", {"name": "get_rca_analysis", "arguments": {}}))
    code, _, stderr = bridge.finish()

    assert reply["id"] == "c1"
    assert reply["error"]["code"] == -32000
    assert f"(HTTP {status})" in reply["error"]["message"]
    expected: dict[str, Any] = {
        "source": "mcp_aws_client",
        "httpStatus": status,
        "retryable": retryable,
    }
    if error_type:
        expected["errorType"] = error_type
    assert reply["error"]["data"] == expected
    for fragment in (
        "not authorized",
        "Missing Authentication Token",
        "Bad Gateway",
        "111122223333",
    ):
        assert fragment not in json.dumps(reply)
        assert fragment not in stderr
    assert code == 0
    assert f"HTTP {status}" in stderr


@pytest.mark.parametrize(
    "backend",
    [
        pytest.param(lambda r: (200, {"Content-Type": "text/plain"}, b"ok"), id="not-json"),
        pytest.param(
            lambda r: (200, {"Content-Type": "application/json"}, b"[1]"), id="not-object"
        ),
        pytest.param(
            lambda r: json_reply({"jsonrpc": "2.0", "id": "other", "result": {}}), id="wrong-id"
        ),
        pytest.param(lambda r: (202, {}, b""), id="accepted-request"),
        pytest.param(
            lambda r: json_reply({"jsonrpc": "2.0", "id": "r", "result": {}, "error": {}}),
            id="result-and-error",
        ),
    ],
)
def test_invalid_endpoint_responses(fixture: Fixture, tmp_path: Path, backend: Backend) -> None:
    fixture.use(backend)
    bridge = Bridge([], bridge_env(tmp_path, fixture))
    reply = bridge.request(rpc("tools/list", "r"))
    assert reply["id"] == "r"
    assert reply["error"]["code"] == -32002
    assert reply["error"]["data"]["retryable"] is False
    assert bridge.finish()[0] == 0


def test_failed_notification_produces_no_stdout_and_bridge_continues(
    fixture: Fixture, tmp_path: Path
) -> None:
    def backend(received: Received) -> Reply:
        if "id" not in received.message():
            return 500, {"Content-Type": "text/plain"}, b"boom"
        return result_for(received, {})

    fixture.use(backend)
    bridge = Bridge([], bridge_env(tmp_path, fixture))
    bridge.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    bridge.send_raw(b"")  # blank lines are ignored
    assert bridge.request(rpc("ping", 2)) == {"jsonrpc": "2.0", "id": 2, "result": {}}
    code, rest, stderr = bridge.finish()
    assert code == 0
    assert rest == []
    assert len(bridge.stdout_lines) == 1
    assert "notifications/initialized failed: HTTP 500" in stderr


def test_read_timeout_is_reported_as_retryable(fixture: Fixture, tmp_path: Path) -> None:
    def backend(received: Received) -> Reply:
        time.sleep(3)
        return result_for(received, {})

    fixture.use(backend)
    bridge = Bridge(["--timeout", "1"], bridge_env(tmp_path, fixture))
    reply = bridge.request(rpc("ping", "t"))
    assert reply["id"] == "t"
    assert reply["error"]["code"] == -32001
    assert reply["error"]["data"] == {
        "source": "mcp_aws_client",
        "errorType": "ReadTimeoutError",
        "retryable": True,
    }
    code, _, stderr = bridge.finish()
    assert code == 0
    assert "ping failed: timeout" in stderr


def test_unreachable_endpoint(tmp_path: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {**base_env(tmp_path, f"http://127.0.0.1:{port}/v1/mcp"), **env_credentials()}
    bridge = Bridge([], env)
    reply = bridge.request(rpc("ping", 5))
    assert reply["id"] == 5
    assert reply["error"]["code"] == -32001
    assert reply["error"]["data"]["errorType"] == "EndpointConnectionError"
    assert reply["error"]["data"]["retryable"] is True
    code, _, stderr = bridge.finish()
    assert code == 0
    assert_clean(stderr)


def test_untrusted_certificate_is_refused(fixture: Fixture, tmp_path: Path) -> None:
    fixture.use(lambda received: result_for(received, {}))
    env = bridge_env(tmp_path, fixture)
    del env["AWS_CA_BUNDLE"]  # the self-signed fixture certificate is not trusted
    bridge = Bridge([], env)
    reply = bridge.request(rpc("ping", 6))
    assert reply["error"]["code"] == -32001
    assert reply["error"]["data"]["errorType"] == "SSLError"
    assert reply["error"]["data"]["retryable"] is False
    assert fixture.received == []
    assert bridge.finish()[0] == 0


def test_plain_http_is_accepted_for_loopback(plain_fixture: Fixture, tmp_path: Path) -> None:
    plain_fixture.use(lambda received: result_for(received, {}))
    bridge = Bridge([], bridge_env(tmp_path, plain_fixture))
    assert bridge.request(rpc("ping", 1)) == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert bridge.finish()[0] == 0
    assert plain_fixture.received[0].signature.valid


# Local message validation ------------------------------------------------------------------------


def sized_call(request_id: int, size: int) -> bytes:
    """A valid tools/call line of exactly `size` UTF-8 bytes, padded with two-byte characters."""

    def encode(padding: str) -> bytes:
        arguments = {"sql": f"SELECT '{padding}'"}
        message = rpc("tools/call", request_id, {"name": "query_athena", "arguments": arguments})
        return json.dumps(message, ensure_ascii=False).encode("utf-8")

    needed = size - len(encode(""))
    data = encode("é" * (needed // 2) + "a" * (needed % 2))
    assert len(data) == size
    return data


def test_messages_the_endpoint_cannot_accept_are_answered_locally(
    fixture: Fixture, tmp_path: Path
) -> None:
    fixture.use(lambda received: result_for(received, {"bytes": len(received.body)}))
    bridge = Bridge([], bridge_env(tmp_path, fixture))

    # MCP 2025-11-25 JSONRPCErrorResponse: `id` is optional but never null, so it is omitted.
    bridge.send_raw(b'{"jsonrpc": "2.0", "id": 1, "method": ')
    assert bridge.recv() == {
        "jsonrpc": "2.0",
        "error": {"code": -32700, "message": "Parse error", "data": {"source": "mcp_aws_client"}},
    }
    bridge.send([rpc("ping", 2), rpc("ping", 3)])
    batch = bridge.recv()
    assert "id" not in batch
    assert batch["error"]["code"] == -32600

    bridge.send_raw(sized_call(4, 32769))
    oversized = bridge.recv()
    assert oversized["id"] == 4
    assert oversized["error"]["code"] == -32600
    assert oversized["error"]["data"]["maxBytes"] == 32768

    exact = sized_call(5, 32768)
    bridge.send_raw(exact)
    assert bridge.recv() == {"jsonrpc": "2.0", "id": 5, "result": {"bytes": 32768}}

    bridge.send_raw(
        b'{"jsonrpc":"2.0","method":"notifications/progress","params":{"x":"'
        + b"a" * 32768
        + b'"}}'
    )
    code, rest, stderr = bridge.finish()
    assert code == 0
    assert rest == []  # the oversized notification is dropped without a reply
    assert [received.body for received in fixture.received] == [exact]
    assert "Refused notifications/progress" in stderr


def test_overlong_line_is_answered_without_an_id(fixture: Fixture, tmp_path: Path) -> None:
    fixture.use(lambda received: result_for(received, {}))
    bridge = Bridge([], bridge_env(tmp_path, fixture))
    bridge.send_raw(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":"' + b"a" * 1024 * 1024)
    assert bridge.recv() == {
        "jsonrpc": "2.0",
        "error": {
            "code": -32600,
            "message": "Message is too large",
            "data": {"source": "mcp_aws_client"},
        },
    }
    assert bridge.request(rpc("ping", 2)) == {"jsonrpc": "2.0", "id": 2, "result": {}}
    code, rest, _ = bridge.finish()
    assert code == 0
    assert rest == []
    assert len(fixture.received) == 1


def invalid_method_error(**request_id: object) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        **request_id,
        "error": {
            "code": -32600,
            "message": "Invalid Request: method must be a string",
            "data": {"source": "mcp_aws_client"},
        },
    }


def test_non_string_methods_are_answered_locally(fixture: Fixture, tmp_path: Path) -> None:
    fixture.use(lambda received: result_for(received, {}))
    # One worker, so replies come back in the order the messages were sent.
    bridge = Bridge(["--max-concurrency", "1"], bridge_env(tmp_path, fixture))
    bridge.send({"jsonrpc": "2.0", "id": 17, "method": []})
    bridge.send({"jsonrpc": "2.0", "id": "o", "method": {"name": "ping"}})
    bridge.send({"jsonrpc": "2.0", "method": 42})  # not a valid notification either
    bridge.send({"jsonrpc": "2.0", "id": [1], "method": None})  # unusable id is omitted
    bridge.send(rpc("ping", 19))

    assert [bridge.recv() for _ in range(5)] == [
        invalid_method_error(id=17),
        invalid_method_error(id="o"),
        invalid_method_error(),
        invalid_method_error(),
        {"jsonrpc": "2.0", "id": 19, "result": {}},
    ]
    code, rest, stderr = bridge.finish()
    assert code == 0
    assert rest == []
    assert [received.message()["id"] for received in fixture.received] == [19]
    assert stderr.count("Refused a message whose method is not a string") == 4
    assert "Unexpected bridge failure" not in stderr
    assert_clean(stderr, b"".join(bridge.stdout_lines))


def test_endpoint_error_without_id_for_a_null_id_request_stays_id_less(
    fixture: Fixture, tmp_path: Path
) -> None:
    error = {"code": -32600, "message": "id must be a string or an integer"}
    fixture.use(lambda received: json_reply({"jsonrpc": "2.0", "error": error}, 400))
    bridge = Bridge([], bridge_env(tmp_path, fixture))
    assert bridge.request({"jsonrpc": "2.0", "id": None, "method": "ping"}) == {
        "jsonrpc": "2.0",
        "error": error,
    }
    assert bridge.finish()[0] == 0


# Configuration -----------------------------------------------------------------------------------


def run_once(
    args: list[str], env: dict[str, str], stdin: bytes = b""
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), *args],
        input=stdin,
        capture_output=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=TIMEOUT,
        check=False,
    )


def test_help_documents_configuration(tmp_path: Path) -> None:
    result = run_once(["--help"], base_env(tmp_path, ""))
    assert result.returncode == 0
    output = result.stdout.decode()
    for text in (
        "MCP_SERVER_URL",
        "AWS_REGION",
        "AWS_PROFILE",
        "execute-api:Invoke",
        "/v1/POST/mcp",
    ):
        assert text in output


@pytest.mark.parametrize(
    ("url", "region", "message"),
    [
        ("", REGION, "MCP_SERVER_URL (or --url) is required"),
        ("http://example.com/v1/mcp", REGION, "only for loopback hosts"),
        ("ftp://127.0.0.1/v1/mcp", REGION, "must be an https:// URL"),
        (
            "https://user:pw@abc123.execute-api.eu-west-2.amazonaws.com/v1/mcp",
            REGION,
            "user information",
        ),
        (
            "https://abc123.execute-api.us-east-1.amazonaws.com/v1/mcp",
            "eu-west-2",
            "does not match the endpoint's Region us-east-1",
        ),
        ("https://mcp.example.com/v1/mcp", "", "set AWS_REGION"),
        ("https://mcp.example.com/v1/mcp", "Not A Region", "not a valid AWS Region"),
    ],
)
def test_invalid_configuration_exits_2_without_output(
    tmp_path: Path, url: str, region: str, message: str
) -> None:
    env = {**base_env(tmp_path, url), **env_credentials()}
    if region:
        env["AWS_REGION"] = region
    else:
        del env["AWS_REGION"]
    result = run_once([], env)
    assert result.returncode == 2
    assert result.stdout == b""
    assert message in result.stderr.decode()
    assert_clean(result.stderr)


@pytest.mark.parametrize("args", [["--timeout", "0"], ["--max-concurrency", "17"]])
def test_out_of_range_options_exit_2(tmp_path: Path, args: list[str]) -> None:
    result = run_once(args, base_env(tmp_path, "https://mcp.example.com/v1/mcp"))
    assert result.returncode == 2
    assert result.stdout == b""


def test_missing_credentials_exit_3(tmp_path: Path) -> None:
    result = run_once(
        [], base_env(tmp_path, "https://abc123.execute-api.eu-west-2.amazonaws.com/v1/mcp")
    )
    assert result.returncode == 3
    assert result.stdout == b""
    assert "No AWS credentials found" in result.stderr.decode()


PROVIDER_SENTINEL = "SYNTHETIC-PROVIDER-SESSION-TOKEN-0004"
CREDENTIAL_PROCESSES = {
    "failing-process": (
        f"import sys\nsys.stderr.write('Provider failure: {PROVIDER_SENTINEL}')\nsys.exit(42)\n"
    ),
    "malformed-process-output": (
        f'print(\'{{"Version": 1, "SessionToken": "{PROVIDER_SENTINEL}"\')\n'
    ),
    "unsupported-process-version": (
        f"import json\nprint(json.dumps({{'Version': 2, 'SessionToken': '{PROVIDER_SENTINEL}'}}))\n"
    ),
}


def assert_single_safe_diagnostic(result: subprocess.CompletedProcess[bytes]) -> str:
    stderr = result.stderr.decode()
    assert result.stdout == b""
    assert len(stderr.strip().splitlines()) == 1, stderr
    assert "Traceback" not in stderr
    assert PROVIDER_SENTINEL not in stderr
    assert_clean(result.stderr)
    return stderr


@pytest.mark.parametrize("behaviour", sorted(CREDENTIAL_PROCESSES))
def test_credential_provider_failure_exits_3_with_one_safe_line(
    tmp_path: Path, behaviour: str
) -> None:
    helper = tmp_path / "credential_process.py"
    helper.write_text(CREDENTIAL_PROCESSES[behaviour], encoding="utf-8")
    config = tmp_path / "config"
    config.write_text(
        "[profile broken]\n"
        f"credential_process = {shlex.quote(sys.executable)} {shlex.quote(str(helper))}\n",
        encoding="utf-8",
    )
    env = base_env(tmp_path, "https://abc123.execute-api.eu-west-2.amazonaws.com/v1/mcp")
    env["AWS_CONFIG_FILE"] = str(config)
    env["AWS_PROFILE"] = "broken"
    result = run_once([], env, stdin=json.dumps(rpc("ping", 1)).encode() + b"\n")
    assert result.returncode == 3
    stderr = assert_single_safe_diagnostic(result)
    assert "AWS credentials could not be obtained from the credential provider chain" in stderr


def test_unreadable_aws_config_exits_3_with_one_safe_line(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.write_text(f"{PROVIDER_SENTINEL}\n[profile x]\nregion = eu-west-2\n", encoding="utf-8")
    env = {
        **base_env(tmp_path, "https://abc123.execute-api.eu-west-2.amazonaws.com/v1/mcp"),
        **env_credentials(),
    }
    env["AWS_CONFIG_FILE"] = str(config)
    del env["AWS_REGION"]
    result = run_once([], env)
    assert result.returncode == 3
    stderr = assert_single_safe_diagnostic(result)
    assert "The AWS configuration could not be loaded (ConfigParseError)" in stderr


def test_unknown_profile_exits_3(tmp_path: Path) -> None:
    env = base_env(tmp_path, "https://abc123.execute-api.eu-west-2.amazonaws.com/v1/mcp")
    env["AWS_PROFILE"] = "does-not-exist"
    result = run_once([], env)
    assert result.returncode == 3
    assert result.stdout == b""
    assert "profile was not found" in result.stderr.decode()


def test_region_resolution(client_module: ModuleType) -> None:
    resolve = client_module.resolve_region
    host = "https://a1b2c3d4e5.execute-api.ap-southeast-2.amazonaws.com/v1/mcp"
    assert resolve(host, None, None) == "ap-southeast-2"
    assert resolve(host, None, "us-east-1") == "ap-southeast-2"
    assert resolve(host, "ap-southeast-2", None) == "ap-southeast-2"
    assert resolve("https://mcp.example.com/v1/mcp", None, "eu-central-1") == "eu-central-1"
    assert resolve("https://mcp.example.com/v1/mcp", "us-west-2", "eu-central-1") == "us-west-2"
    china = "https://a1b2c3.execute-api.cn-north-1.amazonaws.com.cn/v1/mcp"
    assert resolve(china, None, None) == "cn-north-1"
    with pytest.raises(client_module.ConfigError):
        resolve(host, "us-east-1", None)


@pytest.mark.parametrize(
    ("url", "valid"),
    [
        ("https://a1b2c3.execute-api.us-east-1.amazonaws.com/v1/mcp", True),
        ("http://127.0.0.1:8080/v1/mcp", True),
        ("http://[::1]:8080/v1/mcp", True),
        ("http://localhost:8080/v1/mcp", True),
        ("http://10.0.0.5/v1/mcp", False),
        ("https://a1b2c3.execute-api.us-east-1.amazonaws.com/v1/mcp?x=1", False),
        ("https://a1b2c3.execute-api.us-east-1.amazonaws.com/v1/mcp#frag", False),
        ("https:///v1/mcp", False),
    ],
)
def test_url_validation(client_module: ModuleType, url: str, valid: bool) -> None:
    if valid:
        assert client_module.validate_url(url) == url
    else:
        with pytest.raises(client_module.ConfigError):
            client_module.validate_url(url)


# Client configuration examples -------------------------------------------------------------------


def kiro_server() -> dict[str, Any]:
    document = json.loads(KIRO_EXAMPLE.read_text(encoding="utf-8"))
    assert set(document) == {"mcpServers"}
    assert set(document["mcpServers"]) == {"support-case-insights"}
    server: dict[str, Any] = document["mcpServers"]["support-case-insights"]
    return server


def claude_server() -> dict[str, Any]:
    section = DOCS.read_text(encoding="utf-8").split("## Claude Desktop", 1)[1].split("\n## ", 1)[0]
    blocks = re.findall(r"```json\n(.*?)```", section, flags=re.DOTALL)
    assert len(blocks) == 1
    document = json.loads(blocks[0])
    assert set(document["mcpServers"]) == {"support-case-insights"}
    server: dict[str, Any] = document["mcpServers"]["support-case-insights"]
    return server


def test_kiro_example_uses_the_bridge_with_non_secret_settings(client_module: ModuleType) -> None:
    server = kiro_server()
    assert server["command"] == ".venv/bin/python"
    assert server["args"] == ["scripts/mcp_aws_client.py"]
    assert (REPO_ROOT / server["args"][0]).resolve() == SCRIPT
    assert server["disabled"] is False
    assert server["autoApprove"] == []
    env = server["env"]
    assert set(env) == {"MCP_SERVER_URL", "AWS_REGION"}
    url = env["MCP_SERVER_URL"]
    assert url.startswith("https://")
    assert url.endswith("/v1/mcp")
    assert client_module.validate_url(url) == url
    assert client_module.resolve_region(url, env["AWS_REGION"], None) == env["AWS_REGION"]
    text = KIRO_EXAMPLE.read_text(encoding="utf-8")
    for forbidden in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "/home/",
        "/Users/",
    ):
        assert forbidden not in text
    assert not Path(server["command"]).is_absolute()


def test_kiro_example_invokes_the_bridge(
    fixture: Fixture, lambda_backend: Backend, tmp_path: Path
) -> None:
    server = kiro_server()
    fixture.use(lambda_backend)
    env = {
        **base_env(tmp_path, fixture.url, fixture.ca_bundle),
        **server["env"],
        **env_credentials(),
        "MCP_SERVER_URL": fixture.url,  # the example's placeholder points at the loopback fixture
    }
    interpreter = REPO_ROOT / server["command"]
    command = [str(interpreter) if interpreter.exists() else sys.executable]
    bridge = Bridge(server["args"], env, command=command)
    init = bridge.request(initialize(1))
    assert init["result"]["protocolVersion"] == PROTOCOL_VERSION
    listed = bridge.request(rpc("tools/list", 2))
    assert [tool["name"] for tool in listed["result"]["tools"]] == EXPECTED_TOOLS
    assert bridge.finish()[0] == 0
    assert all(received.signature.valid for received in fixture.received)
    assert {received.signature.region for received in fixture.received} == {
        server["env"]["AWS_REGION"]
    }


def test_claude_desktop_instructions_match_the_kiro_example() -> None:
    kiro = kiro_server()
    claude = claude_server()
    assert claude["command"] == "/ABSOLUTE/PATH/TO/factorytestsample/" + kiro["command"]
    assert claude["args"] == ["/ABSOLUTE/PATH/TO/factorytestsample/" + kiro["args"][0]]
    assert claude["env"] == kiro["env"]


def test_documented_options_exist(tmp_path: Path) -> None:
    section = (
        DOCS.read_text(encoding="utf-8").split("## The SigV4 bridge", 1)[1].split("\n## ", 1)[0]
    )
    documented = set(re.findall(r"`(--[a-z-]+)`", section))
    assert documented >= {"--url", "--region", "--profile", "--ca-bundle", "--timeout", "--verbose"}
    help_text = run_once(["--help"], base_env(tmp_path, "")).stdout.decode()
    assert documented <= set(re.findall(r"--[a-z-]+", help_text))
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".kiro/settings/mcp.json" in gitignore

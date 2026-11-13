"""Loopback HTTPS endpoint, independent SigV4 verifier and stdio bridge driver for tests of
`scripts/mcp_aws_client.py`.

The endpoint verifies every AWS Signature Version 4 signature from the specification (not with
botocore) and then hands the request to a backend: the real MCP Lambda handler or a scripted
reply. Every credential is invented and nothing leaves the loopback interface, so these helpers
prove the bridge's behaviour, not API Gateway IAM, resource policy or WAF.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import os
import queue
import re
import ssl
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import IO, Any

from aws_lambda_powertools.utilities.typing import LambdaContext
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "mcp_aws_client.py"

REGION = "eu-west-2"
SERVICE = "execute-api"
PROTOCOL_VERSION = "2025-11-25"
ENV_KEY = "ASIAENVSYNTHETIC0001"
ENV_SECRET = "envSecret/synthetic+0001/for-loopback-signing-only"
ENV_TOKEN = "synthetic-session-token-env-0001"
PROFILE_KEY = "ASIAPROFILESYNTH0002"
PROFILE_SECRET = "profileSecret/synthetic+0002/for-loopback-signing-only"
PROFILE_TOKEN = "synthetic-session-token-profile-0002"
UNKNOWN_KEY = "ASIAUNKNOWNSYNTH0003"
UNKNOWN_SECRET = "unknownSecret/synthetic+0003/for-loopback-signing-only"
UNKNOWN_TOKEN = "synthetic-session-token-unknown-0003"
KNOWN_CREDENTIALS = {
    ENV_KEY: (ENV_SECRET, ENV_TOKEN),
    PROFILE_KEY: (PROFILE_SECRET, PROFILE_TOKEN),
}
SECRETS = (
    ENV_KEY,
    ENV_SECRET,
    ENV_TOKEN,
    PROFILE_KEY,
    PROFILE_SECRET,
    PROFILE_TOKEN,
    UNKNOWN_KEY,
    UNKNOWN_SECRET,
    UNKNOWN_TOKEN,
)
POWERTOOLS_ENV = {
    "POWERTOOLS_METRICS_NAMESPACE": "SupportCaseInsights",
    "POWERTOOLS_SERVICE_NAME": "handler-test",
    "POWERTOOLS_TRACE_DISABLED": "true",
    "POWERTOOLS_LOGGER_LOG_EVENT": "false",
}
TIMEOUT = 20.0


# Independent SigV4 verification (AWS Signature Version 4 specification, not botocore) ----------


@dataclass(frozen=True)
class Signature:
    valid: bool
    access_key: str = ""
    region: str = ""
    service: str = ""
    signed_headers: tuple[str, ...] = ()
    problem: str = ""


AUTHORIZATION = re.compile(
    r"^AWS4-HMAC-SHA256 Credential=(?P<key>[A-Z0-9]+)/(?P<date>\d{8})/(?P<region>[a-z0-9-]+)/"
    r"(?P<service>[a-z0-9-]+)/aws4_request, SignedHeaders=(?P<signed>[a-z0-9;-]+), "
    r"Signature=(?P<signature>[0-9a-f]{64})$"
)


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()


def verify_sigv4(method: str, path: str, headers: dict[str, str], body: bytes) -> Signature:
    match = AUTHORIZATION.match(headers.get("authorization", ""))
    if match is None:
        return Signature(valid=False, problem="malformed Authorization header")
    signed = tuple(match["signed"].split(";"))
    problem = _signature_problem(method, path, headers, body, match)
    return Signature(
        valid=problem is None,
        access_key=match["key"],
        region=match["region"],
        service=match["service"],
        signed_headers=signed,
        problem=problem or "",
    )


def _signature_problem(
    method: str, path: str, headers: dict[str, str], body: bytes, match: re.Match[str]
) -> str | None:
    key, date, region, service = (match[name] for name in ("key", "date", "region", "service"))
    signed = match["signed"].split(";")
    if key not in KNOWN_CREDENTIALS:
        return "unknown access key"
    secret, token = KNOWN_CREDENTIALS[key]
    amz_date = headers.get("x-amz-date", "")
    stamp = dt.datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
    if abs((dt.datetime.now(dt.UTC) - stamp).total_seconds()) > 300 or amz_date[:8] != date:
        return "stale or inconsistent date"
    if headers.get("x-amz-security-token") != token or "x-amz-security-token" not in signed:
        return "session token missing or unsigned"
    if signed != sorted(signed) or not {"host", "x-amz-date"} <= set(signed):
        return "signed header list"
    canonical_headers = "".join(
        f"{name}:{' '.join(headers[name].strip().split())}\n" for name in signed
    )
    canonical_request = "\n".join(
        [method, path, "", canonical_headers, ";".join(signed), hashlib.sha256(body).hexdigest()]
    )
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            f"{date}/{region}/{service}/aws4_request",
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signing_key = _hmac(
        _hmac(_hmac(_hmac(f"AWS4{secret}".encode(), date), region), service), "aws4_request"
    )
    expected = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return None if hmac.compare_digest(expected, match["signature"]) else "signature mismatch"


# Loopback fixture --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Received:
    path: str
    headers: dict[str, str]
    body: bytes
    signature: Signature

    def message(self) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(self.body)
        return document


Reply = tuple[int, dict[str, str], bytes]
Backend = Callable[[Received], Reply]


def json_reply(document: object, status: int = 200) -> Reply:
    return status, {"Content-Type": "application/json"}, json.dumps(document).encode()


def result_for(received: Received, result: object) -> Reply:
    return json_reply({"jsonrpc": "2.0", "id": received.message()["id"], "result": result})


class _FixtureServer(ThreadingHTTPServer):
    daemon_threads = True
    backend: Backend
    received: list[Received]


class _Handler(BaseHTTPRequestHandler):
    server: _FixtureServer
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        headers = {name.lower(): value for name, value in self.headers.items()}
        received = Received(
            self.path, headers, body, verify_sigv4("POST", self.path, headers, body)
        )
        self.server.received.append(received)
        if received.signature.valid:
            status, reply_headers, payload = self.server.backend(received)
        else:
            # API Gateway's signature errors echo the canonical request, including the token.
            status = 403
            reply_headers = {
                "Content-Type": "application/json",
                "x-amzn-ErrorType": "InvalidSignatureException:http://internal.amazon.com/coral/",
            }
            echoed = headers.get("x-amz-security-token", "")
            payload = json.dumps(
                {
                    "message": "The request signature we calculated does not match. The "
                    f"Canonical String should have been 'POST\n/v1/mcp\n\nx-amz-security-token:"
                    f"{echoed}\n' {received.signature.access_key}"
                }
            ).encode()
        try:
            self.send_response(status)
            for name, value in reply_headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            pass  # the bridge gave up (timeout tests)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: ANN401
        return


@dataclass
class Fixture:
    server: _FixtureServer
    url: str
    ca_bundle: str
    thread: threading.Thread
    calls: list[Received] = field(default_factory=list)

    def use(self, backend: Backend) -> None:
        self.server.backend = backend

    @property
    def received(self) -> list[Received]:
        return self.server.received


def make_tls_files(directory: Path) -> tuple[Path, Path]:
    """A self-signed certificate and key for localhost/127.0.0.1, written to `directory`."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "loopback.pem"
    key_path = directory / "loopback.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def start_fixture(tls_files: tuple[Path, Path] | None) -> Fixture:
    server = _FixtureServer(("127.0.0.1", 0), _Handler)
    server.received = []
    server.backend = lambda received: json_reply({"unexpected": True}, 500)
    scheme = "http"
    ca_bundle = ""
    if tls_files is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(tls_files[0]), str(tls_files[1]))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https"
        ca_bundle = str(tls_files[0])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return Fixture(server, f"{scheme}://127.0.0.1:{port}/v1/mcp", ca_bundle, thread)


def stop_fixture(started: Fixture) -> None:
    started.server.shutdown()
    started.server.server_close()
    started.thread.join(timeout=5)


class FixtureLambdaContext(LambdaContext):
    def __init__(self) -> None:
        self._aws_request_id = "fixture-request"
        self._function_name = "Lambda-McpServer-test"
        self._function_version = "$LATEST"
        self._invoked_function_arn = (
            "arn:aws:lambda:eu-west-2:123456789012:function:Lambda-McpServer-test"
        )
        self._memory_limit_in_mb = 512
        self._log_group_name = "/aws/lambda/Lambda-McpServer-test"
        self._log_stream_name = "stream"

    def get_remaining_time_in_millis(self) -> int:  # type: ignore[override]
        return 28_000


def handler_backend(app: ModuleType) -> Backend:
    """The real MCP Lambda handler `app.handler` behind the fixture. Its `tool_context` runs on
    the fixture's request thread, so a SQLite-backed Athena stand-in must be created there."""
    lock = threading.Lock()

    def backend(received: Received) -> Reply:
        event = {
            "resource": "/mcp",
            "path": "/mcp",
            "httpMethod": "POST",
            "headers": received.headers,
            "multiValueHeaders": None,
            "queryStringParameters": None,
            "requestContext": {"stage": "v1", "requestId": "fixture-request"},
            "body": received.body.decode("utf-8"),
            "isBase64Encoded": False,
        }
        with lock:
            result = app.handler(event, FixtureLambdaContext())
        return result["statusCode"], result["headers"], result["body"].encode("utf-8")

    return backend


# Bridge subprocess -------------------------------------------------------------------------------


def _pump(stream: IO[bytes], sink: queue.Queue[bytes | None]) -> None:
    for line in stream:
        sink.put(line)
    sink.put(None)


class Bridge:
    def __init__(
        self,
        args: list[str],
        env: dict[str, str],
        command: list[str] | None = None,
    ) -> None:
        self.proc = subprocess.Popen(  # noqa: S603
            [*(command or [sys.executable, str(SCRIPT)]), *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=REPO_ROOT,
        )
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        assert self.proc.stderr is not None
        self.stdin = self.proc.stdin
        self.stdout: queue.Queue[bytes | None] = queue.Queue()
        self.stderr: queue.Queue[bytes | None] = queue.Queue()
        threading.Thread(target=_pump, args=(self.proc.stdout, self.stdout), daemon=True).start()
        threading.Thread(target=_pump, args=(self.proc.stderr, self.stderr), daemon=True).start()
        self.stdout_lines: list[bytes] = []

    def send_raw(self, data: bytes) -> None:
        self.stdin.write(data + b"\n")
        self.stdin.flush()

    def send(self, message: object) -> bytes:
        data = json.dumps(message, ensure_ascii=False).encode("utf-8")
        self.send_raw(data)
        return data

    def recv(self, timeout: float = TIMEOUT) -> dict[str, Any]:
        line = self.stdout.get(timeout=timeout)
        assert line is not None, "bridge closed stdout"
        self.stdout_lines.append(line)
        assert line.endswith(b"\n")
        assert line.count(b"\n") == 1
        document: dict[str, Any] = json.loads(line)
        assert document["jsonrpc"] == "2.0"
        return document

    def request(self, message: dict[str, Any]) -> dict[str, Any]:
        self.send(message)
        return self.recv()

    def finish(self) -> tuple[int, list[bytes], str]:
        """Close stdin; return the exit status, unread stdout lines and all stderr."""
        self.stdin.close()
        code = self.proc.wait(timeout=TIMEOUT)
        rest = _drain(self.stdout)
        self.stdout_lines.extend(rest)
        return code, rest, b"".join(_drain(self.stderr)).decode("utf-8")


def _drain(source: queue.Queue[bytes | None]) -> list[bytes]:
    lines: list[bytes] = []
    while True:
        line = source.get(timeout=TIMEOUT)
        if line is None:
            return lines
        lines.append(line)


def base_env(tmp_path: Path, url: str, ca_bundle: str = "") -> dict[str, str]:
    """An isolated environment: no real profiles, no instance metadata, no proxies."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "LANG": "C.UTF-8",
        "AWS_CONFIG_FILE": str(tmp_path / "no-config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "no-credentials"),
        "AWS_EC2_METADATA_DISABLED": "true",
        "MCP_SERVER_URL": url,
        "AWS_REGION": REGION,
    }
    if ca_bundle:
        env["AWS_CA_BUNDLE"] = ca_bundle
    return env


def env_credentials(
    key: str = ENV_KEY, secret: str = ENV_SECRET, token: str = ENV_TOKEN
) -> dict[str, str]:
    return {"AWS_ACCESS_KEY_ID": key, "AWS_SECRET_ACCESS_KEY": secret, "AWS_SESSION_TOKEN": token}


def bridge_env(tmp_path: Path, fixture: Fixture, **overrides: str) -> dict[str, str]:
    return {**base_env(tmp_path, fixture.url, fixture.ca_bundle), **env_credentials(), **overrides}


def assert_clean(*texts: str | bytes) -> None:
    for item in texts:
        text = item.decode("utf-8") if isinstance(item, bytes) else item
        for secret in SECRETS:
            assert secret not in text
        assert "Signature=" not in text
        assert "Credential=" not in text
        assert "Canonical String" not in text


def initialize(request_id: object = 1) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "synthetic-client", "version": "1.0.0"},
        },
    }


def rpc(method: str, request_id: object, params: object | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message

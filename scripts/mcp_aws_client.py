"""Bridge an MCP client's stdio transport to the Support Case Insights MCP endpoint using SigV4.

MCP clients such as Kiro and Claude Desktop start this script as a local stdio server. Each
newline-delimited JSON-RPC message read from stdin is POSTed to the deployed REST endpoint
(`https://<api-id>.execute-api.<region>.amazonaws.com/v1/mcp`), signed for the `execute-api`
service with credentials from the standard AWS credential provider chain, and the endpoint's
JSON-RPC reply is written to stdout as one line.

Stdout carries MCP protocol messages only. Diagnostics go to stderr and never include
credentials, signatures, request or response bodies, tool arguments or case content. HTTP and
connection failures for a request become JSON-RPC errors carrying the request's own id (omitted
when it is not a valid MCP request id), so the client never waits for a reply that will not arrive.

Only boto3/botocore are required; the script does not import the Lambda code.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import IO, Any, Final
from urllib.parse import urlsplit

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest, AWSResponse
from botocore.credentials import Credentials
from botocore.exceptions import (
    BotoCoreError,
    ConnectTimeoutError,
    ProfileNotFound,
    ReadTimeoutError,
    SSLError,
)
from botocore.httpsession import URLLib3Session
from botocore.utils import get_environ_proxies

SERVICE: Final = "execute-api"
MAX_REQUEST_BYTES: Final = 32 * 1024
# Lines up to this size are read whole so an oversized request can still be answered with its id.
MAX_LINE_BYTES: Final = 1024 * 1024
MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS: Final = 30
DEFAULT_CONCURRENCY: Final = 4
MAX_CONCURRENCY: Final = 16
ACCEPT: Final = "application/json, text/event-stream"
PROTOCOL_VERSION_HEADER: Final = "MCP-Protocol-Version"

EXIT_OK: Final = 0
EXIT_USAGE: Final = 2
EXIT_CREDENTIALS: Final = 3

PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
UPSTREAM_HTTP_ERROR: Final = -32000
UPSTREAM_UNREACHABLE: Final = -32001
UPSTREAM_INVALID_RESPONSE: Final = -32002
CREDENTIALS_UNAVAILABLE: Final = -32003
INTERNAL_ERROR: Final = -32603

EXECUTE_API_HOST: Final = re.compile(
    r"^[a-z0-9]+\.execute-api\.([a-z]{2}(?:-[a-z]+)+-\d+)\.amazonaws\.com(?:\.cn)?$"
)
REGION_PATTERN: Final = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d+$")
ERROR_TYPE_PATTERN: Final = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")
TOOL_NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
KNOWN_METHODS: Final = frozenset(
    {
        "initialize",
        "ping",
        "tools/list",
        "tools/call",
        "notifications/initialized",
        "notifications/cancelled",
        "notifications/progress",
        "notifications/roots/list_changed",
    }
)
HTTP_OK: Final = 200
HTTP_REDIRECT: Final = 300
HTTP_SERVER_ERROR: Final = 500
RETRYABLE_STATUSES: Final = frozenset({429, 502, 503, 504})
STATUS_MESSAGES: Final = {
    400: "The MCP endpoint rejected the request",
    401: "The MCP endpoint did not accept the request signature",
    403: "The MCP endpoint denied the request; check the credentials, Region and that the "
    "principal is in mcpAllowedPrincipals",
    404: "The MCP endpoint was not found; check MCP_SERVER_URL",
    413: "The request is too large for the MCP endpoint",
    429: "The MCP endpoint throttled the request; retry later",
}

HELP_EPILOG: Final = """\
Environment:
  MCP_SERVER_URL   endpoint URL, e.g. https://<api-id>.execute-api.us-east-1.amazonaws.com/v1/mcp
                   (stack output of CaseInsightsMcpStack-<uniqueId>); same as --url
  AWS_REGION       Region used in the SigV4 scope; same as --region. When unset, the Region is
                   taken from an execute-api host name, then from the AWS profile configuration.
  AWS_PROFILE      optional named profile; same as --profile
  AWS_CA_BUNDLE    optional CA bundle for TLS verification; same as --ca-bundle

Credentials come from the standard AWS credential provider chain (environment variables,
shared credentials/config files, IAM Identity Center (SSO), assumed roles, container or instance
roles). Temporary credentials are refreshed automatically and their session token is signed.
Never put credentials in the MCP client configuration.

Required permission: execute-api:Invoke on
  arn:aws:execute-api:<region>:<account-id>:<api-id>/v1/POST/mcp
and the principal must be listed in the deployment's mcpAllowedPrincipals.

Stdout carries MCP JSON-RPC messages only, one per line; diagnostics go to stderr without
credentials or payloads. Endpoint and connection failures are returned to the MCP client as
JSON-RPC errors (-32000 HTTP error, -32001 unreachable or timed out, -32002 invalid response,
-32003 credentials unavailable) with the original request id.

Plain http:// URLs are accepted only for loopback hosts, for local testing.

Messages the endpoint cannot accept are answered locally: -32700 for invalid JSON, -32600 for
batches, oversized messages and non-string methods. An error has no id member when the request
id is missing, null, or not a string or integer (strings must be valid Unicode text).

Exit status: 0 stdin closed, 2 invalid options or configuration, 3 AWS credentials or
configuration could not be loaded.
"""

logger = logging.getLogger("mcp_aws_client")


class ConfigError(ValueError):
    """Invalid command-line or environment configuration."""


@dataclass(frozen=True, slots=True)
class Settings:
    url: str
    region: str
    profile: str | None
    ca_bundle: str | None
    timeout: int
    concurrency: int


def validate_url(url: str) -> str:
    """Return `url` if it is a usable MCP endpoint URL; raise `ConfigError` otherwise."""
    if not url:
        raise ConfigError("MCP_SERVER_URL (or --url) is required")
    parts = urlsplit(url)
    host = parts.hostname
    if parts.scheme not in {"https", "http"} or not host:
        raise ConfigError("the MCP endpoint URL must be an https:// URL")
    if parts.username is not None or parts.password is not None:
        raise ConfigError("the MCP endpoint URL must not contain user information")
    if parts.query or parts.fragment:
        raise ConfigError("the MCP endpoint URL must not contain a query or fragment")
    if parts.scheme == "http" and not _is_loopback(host):
        raise ConfigError("http:// endpoint URLs are allowed only for loopback hosts")
    return url


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_region(url: str, explicit: str | None, profile_region: str | None) -> str:
    """The SigV4 Region: explicit setting, else the execute-api host's Region, else the profile's.

    An explicit Region that contradicts an execute-api host name is rejected, because every
    request would fail signature verification.
    """
    match = EXECUTE_API_HOST.match(urlsplit(url).hostname or "")
    host_region = match.group(1) if match else None
    region = explicit or host_region or profile_region
    if not region:
        raise ConfigError("set AWS_REGION (or --region) to the Region of the MCP endpoint")
    if not REGION_PATTERN.match(region):
        raise ConfigError("AWS_REGION is not a valid AWS Region name")
    if host_region and region != host_region:
        raise ConfigError(f"AWS_REGION {region} does not match the endpoint's Region {host_region}")
    return region


RequestId = str | int


def _usable_id(value: object) -> RequestId | None:
    """`value` if it can identify a request in a reply, else None.

    MCP request ids are strings or integers (booleans are not integers). A string decoded from
    an escaped unpaired surrogate such as `"\\ud800"` is not Unicode text: it cannot be written
    to stdout as UTF-8, so it cannot identify the request in a reply either.
    """
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return None
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _same_id(reply_id: object, request_id: RequestId | None) -> bool:
    """Whether a reply's id is exactly the request's; `0 == False` and `1 == 1.0` do not count."""
    return request_id is not None and type(reply_id) is type(request_id) and reply_id == request_id


def _rpc_error(
    request_id: RequestId | None, code: int, message: str, data: Mapping[str, Any]
) -> dict[str, Any]:
    """A bridge-local JSON-RPC error; without a usable request id it has no `id` member.

    The MCP 2025-11-25 schema makes `JSONRPCErrorResponse.id` optional but never null.
    """
    response: dict[str, Any] = {"jsonrpc": "2.0"}
    request_id = _usable_id(request_id)
    if request_id is not None:
        response["id"] = request_id
    response["error"] = {
        "code": code,
        "message": message,
        "data": {"source": "mcp_aws_client", **data},
    }
    return response


def _error_type(response: AWSResponse) -> str | None:
    """API Gateway's `x-amzn-ErrorType` name, without the documentation URL suffix."""
    raw = response.headers.get("x-amzn-ErrorType") or response.headers.get("x-amzn-errortype")
    if not raw:
        return None
    name = str(raw).split(":", 1)[0].strip()
    return name if ERROR_TYPE_PATTERN.match(name) else None


def _is_success(status: int) -> bool:
    return HTTP_OK <= status < HTTP_REDIRECT


def _is_json_media(response: AWSResponse) -> bool:
    content_type = str(response.headers.get("Content-Type", ""))
    return content_type.split(";", 1)[0].strip().lower() == "application/json"


def _parse_jsonrpc_reply(content: bytes) -> dict[str, Any] | None:
    """A single JSON-RPC 2.0 response object, or None if `content` is anything else."""
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(document, dict) or document.get("jsonrpc") != "2.0":
        return None
    if ("result" in document) == ("error" in document):
        return None
    if "error" in document:
        error = document["error"]
        if not (
            isinstance(error, dict)
            and isinstance(error.get("code"), int)
            and isinstance(error.get("message"), str)
        ):
            return None
    return document


def _method_label(message: Mapping[str, Any]) -> str:
    method = message.get("method")
    if isinstance(method, str) and method in KNOWN_METHODS:
        label = str(method)
        if label == "tools/call":
            params = message.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            if isinstance(name, str) and TOOL_NAME_PATTERN.match(name):
                label = f"{label} {name}"
        return label
    return "response" if method is None else "other"


class Bridge:
    """Forwards one JSON-RPC message at a time to the MCP endpoint and builds the stdout reply."""

    def __init__(
        self,
        settings: Settings,
        credentials: Credentials,
        http: URLLib3Session | None = None,
    ) -> None:
        self._settings = settings
        self._credentials = credentials
        self._http = http or URLLib3Session(
            # botocore accepts a CA bundle path here, although its type stubs say bool.
            verify=settings.ca_bundle or True,  # type: ignore[arg-type]
            proxies=get_environ_proxies(settings.url),
            timeout=settings.timeout,
        )
        self._protocol_version: str | None = None
        self._lock = threading.Lock()

    def handle_line(self, line: bytes) -> dict[str, Any] | None:
        """The reply to write for one stdin line, or None when the client expects no reply."""
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            logger.warning("Discarded a stdin line that is not valid UTF-8 JSON")
            return _rpc_error(None, PARSE_ERROR, "Parse error", {})
        if not isinstance(message, dict):
            logger.warning("Discarded a stdin message that is not a JSON-RPC object")
            return _rpc_error(
                None, INVALID_REQUEST, "Batch and non-object messages are not supported", {}
            )
        is_request = "method" in message and "id" in message
        request_id = _usable_id(message.get("id")) if is_request else None
        try:
            return self._dispatch(line, message, is_request, request_id)
        except Exception as exc:
            logger.error("Unexpected bridge failure (%s)", type(exc).__name__)
            if not is_request:
                return None
            return _rpc_error(request_id, INTERNAL_ERROR, "Internal bridge error", {})

    def _dispatch(
        self,
        line: bytes,
        message: Mapping[str, Any],
        is_request: bool,
        request_id: RequestId | None,
    ) -> dict[str, Any] | None:
        if "method" in message and not isinstance(message["method"], str):
            # Neither a request nor a notification; JSON-RPC answers it even without an id.
            logger.warning("Refused a message whose method is not a string")
            return _rpc_error(
                request_id,
                INVALID_REQUEST,
                "Invalid Request: method must be a string",
                {},
            )
        label = _method_label(message)
        if len(line) > MAX_REQUEST_BYTES:
            logger.warning("Refused %s: message exceeds %d bytes", label, MAX_REQUEST_BYTES)
            if not is_request:
                return None
            return _rpc_error(
                request_id,
                INVALID_REQUEST,
                f"Request body exceeds {MAX_REQUEST_BYTES} bytes",
                {"maxBytes": MAX_REQUEST_BYTES},
            )
        reply = self._forward(line, message, label, request_id)
        return reply if is_request else None

    def _headers(self, message: Mapping[str, Any]) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": ACCEPT}
        with self._lock:
            version = self._protocol_version
        if version is not None and message.get("method") != "initialize":
            headers[PROTOCOL_VERSION_HEADER] = version
        return headers

    def _forward(
        self, body: bytes, message: Mapping[str, Any], label: str, request_id: RequestId | None
    ) -> dict[str, Any] | None:
        started = time.monotonic()
        try:
            # Refreshes temporary credentials (SSO, assumed roles, containers) when they expire.
            frozen = self._credentials.get_frozen_credentials()
        except Exception as exc:
            return self._credentials_error(label, request_id, exc)
        request = AWSRequest(
            method="POST", url=self._settings.url, data=body, headers=self._headers(message)
        )
        SigV4Auth(frozen, SERVICE, self._settings.region).add_auth(request)
        try:
            response = self._http.send(request.prepare())
        except (SSLError, ConnectTimeoutError, ReadTimeoutError) as exc:
            kind = "tls" if isinstance(exc, SSLError) else "timeout"
            logger.warning("%s failed: %s (%s)", label, kind, type(exc).__name__)
            return _rpc_error(
                request_id,
                UPSTREAM_UNREACHABLE,
                "TLS verification with the MCP endpoint failed"
                if kind == "tls"
                else "The MCP endpoint did not respond in time",
                {"errorType": type(exc).__name__, "retryable": kind == "timeout"},
            )
        except BotoCoreError as exc:
            logger.warning("%s failed: endpoint unreachable (%s)", label, type(exc).__name__)
            return _rpc_error(
                request_id,
                UPSTREAM_UNREACHABLE,
                "The MCP endpoint could not be reached",
                {"errorType": type(exc).__name__, "retryable": True},
            )
        elapsed_ms = round((time.monotonic() - started) * 1000)
        return self._reply(response, message, label, request_id, elapsed_ms)

    def _credentials_error(
        self, label: str, request_id: RequestId | None, exc: BaseException
    ) -> dict[str, Any]:
        logger.warning("%s not sent: AWS credentials unavailable (%s)", label, type(exc).__name__)
        return _rpc_error(
            request_id,
            CREDENTIALS_UNAVAILABLE,
            "AWS credentials are unavailable or expired; refresh them and retry",
            {"errorType": type(exc).__name__, "retryable": False},
        )

    def _reply(
        self,
        response: AWSResponse,
        message: Mapping[str, Any],
        label: str,
        request_id: RequestId | None,
        elapsed_ms: int,
    ) -> dict[str, Any] | None:
        status = response.status_code
        content = response.content or b""
        error_type = _error_type(response)
        if len(content) > MAX_RESPONSE_BYTES:
            logger.warning("%s failed: response exceeds %d bytes", label, MAX_RESPONSE_BYTES)
            return _rpc_error(
                request_id,
                UPSTREAM_INVALID_RESPONSE,
                "The MCP endpoint response is too large",
                {"httpStatus": status, "retryable": False},
            )
        document = _parse_jsonrpc_reply(content) if content and _is_json_media(response) else None
        if document is not None and (_is_success(status) or "error" in document):
            reply_id = document.get("id")
            if "error" in document and reply_id is None:
                # The endpoint could not read the id; the bridge knows which request failed.
                if request_id is None:
                    document.pop("id", None)
                else:
                    document["id"] = request_id
            elif not _same_id(reply_id, request_id):
                logger.warning("%s failed: response id does not match the request", label)
                return _rpc_error(
                    request_id,
                    UPSTREAM_INVALID_RESPONSE,
                    "The MCP endpoint returned a response for a different request",
                    {"httpStatus": status, "retryable": False},
                )
            self._remember_protocol(message, document)
            log = logger.debug if "result" in document else logger.warning
            log(
                "%s -> HTTP %d in %d ms%s",
                label,
                status,
                elapsed_ms,
                "" if "result" in document else " (JSON-RPC error)",
            )
            return document
        expects_reply = "method" in message and "id" in message
        if _is_success(status) and not content and not expects_reply:
            logger.debug("%s -> HTTP %d in %d ms", label, status, elapsed_ms)
            return None
        if _is_success(status):
            logger.warning("%s failed: invalid response (HTTP %d)", label, status)
            return _rpc_error(
                request_id,
                UPSTREAM_INVALID_RESPONSE,
                "The MCP endpoint returned an invalid JSON-RPC response",
                {"httpStatus": status, "retryable": False},
            )
        logger.warning(
            "%s failed: HTTP %d%s", label, status, f" ({error_type})" if error_type else ""
        )
        data: dict[str, Any] = {"httpStatus": status, "retryable": status in RETRYABLE_STATUSES}
        if error_type:
            data["errorType"] = error_type
        text = STATUS_MESSAGES.get(status)
        if text is None:
            text = (
                "The MCP endpoint failed"
                if status >= HTTP_SERVER_ERROR
                else "The MCP endpoint rejected the request"
            )
        return _rpc_error(request_id, UPSTREAM_HTTP_ERROR, f"{text} (HTTP {status})", data)

    def _remember_protocol(self, message: Mapping[str, Any], document: Mapping[str, Any]) -> None:
        if message.get("method") != "initialize":
            return
        result = document.get("result")
        version = result.get("protocolVersion") if isinstance(result, dict) else None
        if isinstance(version, str) and 0 < len(version) <= 32:  # noqa: PLR2004
            with self._lock:
                self._protocol_version = version


class StdoutWriter:
    """Writes one JSON-RPC message per line; serialised so concurrent replies never interleave."""

    def __init__(self, stream: IO[bytes]) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def write(self, message: Mapping[str, Any]) -> None:
        try:
            data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except UnicodeEncodeError:
            # Endpoint text may carry an unpaired surrogate from a JSON escape; ASCII output
            # re-escapes it, so the line is still valid UTF-8.
            data = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        with self._lock:
            self._stream.write(data + b"\n")
            self._stream.flush()


def _read_lines(stream: IO[bytes]) -> Iterator[bytes | None]:
    """Yield stripped non-empty lines; None marks a line longer than `MAX_LINE_BYTES`."""
    while True:
        line = stream.readline(MAX_LINE_BYTES + 1)
        if not line:
            return
        if len(line) > MAX_LINE_BYTES and not line.endswith(b"\n"):
            while True:
                rest = stream.readline(MAX_LINE_BYTES)
                if not rest or rest.endswith(b"\n"):
                    break
            yield None
            continue
        stripped = line.strip()
        if stripped:
            yield stripped


def serve(
    handle: Callable[[bytes], dict[str, Any] | None],
    stdin: IO[bytes],
    writer: StdoutWriter,
    concurrency: int,
) -> None:
    """Read stdin until EOF, handling up to `concurrency` messages at once."""
    slots = threading.BoundedSemaphore(concurrency * 2)

    def work(line: bytes) -> None:
        try:
            reply = handle(line)
            if reply is not None:
                writer.write(reply)
        except BrokenPipeError:
            pass
        except Exception as exc:
            logger.error("Unexpected bridge failure (%s)", type(exc).__name__)
        finally:
            slots.release()

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="mcp-forward") as pool:
        for line in _read_lines(stdin):
            if line is None:
                logger.warning("Discarded a stdin line longer than %d bytes", MAX_LINE_BYTES)
                writer.write(_rpc_error(None, INVALID_REQUEST, "Message is too large", {}))
                continue
            slots.acquire()
            pool.submit(work, line)


def parse_args(argv: Sequence[str] | None, environ: Mapping[str, str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mcp_aws_client.py",
        description=(
            "Bridge an MCP client's stdio transport to the Support Case Insights MCP endpoint, "
            "signing each request with AWS SigV4 for execute-api."
        ),
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--url",
        default=environ.get("MCP_SERVER_URL", ""),
        help="MCP endpoint URL (default: $MCP_SERVER_URL)",
    )
    parser.add_argument(
        "--region",
        default=environ.get("AWS_REGION") or None,
        help="Region for the SigV4 scope (default: $AWS_REGION, the endpoint host or the profile)",
    )
    parser.add_argument(
        "--profile",
        default=environ.get("AWS_PROFILE") or None,
        help="named AWS profile (default: $AWS_PROFILE or the default credential chain)",
    )
    parser.add_argument(
        "--ca-bundle",
        default=environ.get("AWS_CA_BUNDLE") or None,
        help="CA bundle for TLS verification (default: $AWS_CA_BUNDLE or the bundled CAs)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"connect and read timeout in seconds, 1-120 (default: {DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"requests forwarded at once, 1-{MAX_CONCURRENCY} (default: {DEFAULT_CONCURRENCY})",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="log every forwarded message's method, HTTP status and duration to stderr",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 120:  # noqa: PLR2004
        parser.error("--timeout must be between 1 and 120 seconds")
    if not 1 <= args.max_concurrency <= MAX_CONCURRENCY:
        parser.error(f"--max-concurrency must be between 1 and {MAX_CONCURRENCY}")
    return args


def _configure_logging(verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s mcp_aws_client: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    # botocore and urllib3 debug logs include signed headers; keep them quiet regardless.
    for name in ("botocore", "urllib3", "boto3"):
        logging.getLogger(name).setLevel(logging.WARNING)


class StartupError(Exception):
    """The bridge cannot start; `message` is safe to log and `exit_code` is the exit status."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


def load_startup(args: argparse.Namespace) -> tuple[Settings, Credentials]:
    """Validate the configuration and resolve credentials, or raise `StartupError`."""
    try:
        url = validate_url(args.url)
        session = boto3.Session(profile_name=args.profile)
        region = resolve_region(url, args.region, session.region_name)
    except ProfileNotFound:
        raise StartupError("The AWS profile was not found", EXIT_CREDENTIALS) from None
    except ConfigError as exc:
        raise StartupError(str(exc), EXIT_USAGE) from None
    except BotoCoreError as exc:
        raise StartupError(
            f"The AWS configuration could not be loaded ({type(exc).__name__})", EXIT_CREDENTIALS
        ) from None
    try:
        credentials = session.get_credentials()
    except Exception as exc:
        # Provider exceptions can carry a credential_process's raw stderr; log the class only.
        raise StartupError(
            "AWS credentials could not be obtained from the credential provider chain "
            f"({type(exc).__name__})",
            EXIT_CREDENTIALS,
        ) from None
    if credentials is None:
        raise StartupError(
            "No AWS credentials found through the credential provider chain", EXIT_CREDENTIALS
        )
    settings = Settings(
        url=url,
        region=region,
        profile=args.profile,
        ca_bundle=args.ca_bundle,
        timeout=args.timeout,
        concurrency=args.max_concurrency,
    )
    return settings, credentials


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv, os.environ)
    protocol_out = sys.stdout.buffer
    # Anything printed by mistake must not corrupt the protocol stream.
    sys.stdout = sys.stderr
    _configure_logging(args.verbose)
    try:
        settings, credentials = load_startup(args)
    except StartupError as exc:
        logger.error("%s", exc.message)
        return exc.exit_code
    parts = urlsplit(settings.url)
    logger.info(
        "Forwarding MCP stdio to %s://%s%s (Region %s, credentials: %s)",
        parts.scheme,
        parts.netloc,
        parts.path,
        settings.region,
        credentials.method,
    )
    bridge = Bridge(settings, credentials)
    try:
        serve(
            bridge.handle_line, sys.stdin.buffer, StdoutWriter(protocol_out), settings.concurrency
        )
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return EXIT_OK
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

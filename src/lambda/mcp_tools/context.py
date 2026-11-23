"""Runtime configuration, AWS clients and the per-call context shared by every tool.

The Athena workgroup, Glue database and Bedrock model are fixed server-side from the function
environment; no tool argument can replace them.
"""

from __future__ import annotations

import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import TYPE_CHECKING, Final, Protocol

import boto3
from common.aws import client_config
from common.bedrock import MAX_MAX_TOKENS, MIN_MAX_TOKENS, validate_model_id

from mcp_tools.errors import TimeBudgetExceededError
from mcp_tools.limits import McpLimits

if TYPE_CHECKING:
    from types_boto3_athena.client import AthenaClient
    from types_boto3_bedrock_runtime.client import BedrockRuntimeClient

CASE_SUMMARY_TABLE: Final = "case_summary"
DATA_CATALOG: Final = "AwsDataCatalog"
_WORKGROUP = re.compile(r"[A-Za-z0-9._-]{1,128}")
_DATABASE = re.compile(r"[a-z0-9_]{1,255}")
# Read timeouts are whole seconds, so each service needs at most one client per possible value.
_CLIENT_CACHE_SIZE: Final = 32


@dataclass(frozen=True, slots=True)
class McpSettings:
    """Validated server-side configuration."""

    workgroup: str
    database: str
    model_id: str
    max_tokens: int

    def __post_init__(self) -> None:
        if not _WORKGROUP.fullmatch(self.workgroup):
            raise ValueError("ATHENA_WORKGROUP is not a valid Athena workgroup name")
        if not _DATABASE.fullmatch(self.database):
            raise ValueError("ATHENA_DATABASE is not a valid Glue database name")
        validate_model_id(self.model_id)
        if not MIN_MAX_TOKENS <= self.max_tokens <= MAX_MAX_TOKENS:
            raise ValueError(f"max_tokens must be between {MIN_MAX_TOKENS} and {MAX_MAX_TOKENS}")

    @classmethod
    def from_env(cls) -> McpSettings:
        """Read ATHENA_WORKGROUP, ATHENA_DATABASE, BEDROCK_MODEL_ID and BEDROCK_MAX_TOKENS."""
        return cls(
            workgroup=os.environ["ATHENA_WORKGROUP"],
            database=os.environ["ATHENA_DATABASE"],
            model_id=os.environ["BEDROCK_MODEL_ID"],
            max_tokens=int(os.environ["BEDROCK_MAX_TOKENS"]),
        )


class TimeoutClients[T](Protocol):
    """Clients of one AWS service, each configured with a given read timeout."""

    def with_timeout(self, read_timeout: float) -> T:
        """A client whose calls fail after the connect timeout plus `read_timeout` seconds."""
        ...


class CachedClients[T]:
    """Clients created on first use for each read timeout and reused afterwards."""

    def __init__(self, factory: Callable[[float], T]) -> None:
        self._factory = lru_cache(maxsize=_CLIENT_CACHE_SIZE)(factory)

    def with_timeout(self, read_timeout: float) -> T:
        return self._factory(read_timeout)


@dataclass(frozen=True, slots=True)
class McpClients:
    """AWS clients used by the tools, obtained per call with a read timeout that fits the
    remaining time budget."""

    athena: TimeoutClients[AthenaClient]
    bedrock: TimeoutClients[BedrockRuntimeClient]

    @classmethod
    def create(
        cls, *, region_name: str | None = None, limits: McpLimits | None = None
    ) -> McpClients:
        """Clients without SDK retries (the engine retries within the budget itself) and with
        the connect timeouts from `limits`."""
        bounds = limits or McpLimits()
        session = boto3.Session()

        def athena(read_timeout: float) -> AthenaClient:
            return session.client(
                "athena",
                region_name=region_name,
                config=client_config(
                    max_attempts=1,
                    connect_timeout=bounds.athena_connect_timeout_seconds,
                    read_timeout=read_timeout,
                ),
            )

        def bedrock(read_timeout: float) -> BedrockRuntimeClient:
            return session.client(
                "bedrock-runtime",
                region_name=region_name,
                config=client_config(
                    max_attempts=1,
                    connect_timeout=bounds.bedrock_connect_timeout_seconds,
                    read_timeout=read_timeout,
                ),
            )

        return cls(athena=CachedClients(athena), bedrock=CachedClients(bedrock))


@dataclass(slots=True)
class ToolContext:
    """Everything a tool call needs, including its deadline on `clock`'s timeline."""

    settings: McpSettings
    clients: McpClients
    deadline: float
    limits: McpLimits = field(default_factory=McpLimits)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    @classmethod
    def start(  # noqa: PLR0913
        cls,
        settings: McpSettings,
        clients: McpClients,
        *,
        limits: McpLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        remaining_millis: int | None = None,
    ) -> ToolContext:
        """Start a call whose budget is the request budget, shortened to the Lambda function's
        remaining time (less a margin) when `remaining_millis` is given."""
        bounds = limits or McpLimits()
        budget = bounds.request_budget_seconds
        if remaining_millis is not None:
            budget = min(budget, remaining_millis / 1000 - bounds.lambda_deadline_margin_seconds)
        return cls(
            settings=settings,
            clients=clients,
            deadline=clock() + max(budget, 0.0),
            limits=bounds,
            clock=clock,
            sleep=sleep,
        )

    def remaining(self) -> float:
        """Seconds left before the call's deadline (never negative)."""
        return max(self.deadline - self.clock(), 0.0)

    def bounded(self, seconds: float) -> ToolContext:
        """This context with its deadline brought forward to at most `seconds` from now."""
        return replace(self, deadline=min(self.deadline, self.clock() + max(seconds, 0.0)))

    def read_timeout(self, *, connect: float, minimum: float, maximum: float, keep: float) -> float:
        """Whole-second read timeout for a call that must end, even when it times out, at least
        `keep` seconds before the deadline. Raises TIME_BUDGET_EXCEEDED when that leaves less
        than `minimum`, so the call is not started."""
        timeout = float(min(math.floor(maximum), math.floor(self.remaining() - connect - keep)))
        if timeout < max(minimum, 1.0):
            raise TimeBudgetExceededError
        return timeout

    def athena(self, *, keep: float) -> AthenaClient:
        """An Athena client for one call that ends at least `keep` seconds before the deadline."""
        limits = self.limits
        return self.clients.athena.with_timeout(
            self.read_timeout(
                connect=limits.athena_connect_timeout_seconds,
                minimum=limits.athena_min_read_timeout_seconds,
                maximum=limits.athena_read_timeout_seconds,
                keep=keep,
            )
        )

    def bedrock(self, *, keep: float) -> BedrockRuntimeClient:
        """A Bedrock client for one call that ends at least `keep` seconds before the deadline."""
        limits = self.limits
        return self.clients.bedrock.with_timeout(
            self.read_timeout(
                connect=limits.bedrock_connect_timeout_seconds,
                minimum=limits.min_bedrock_bounded_read,
                maximum=limits.bedrock_read_timeout_seconds,
                keep=keep,
            )
        )

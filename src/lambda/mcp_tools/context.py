"""Runtime configuration, AWS clients and the per-call context shared by every tool.

The Athena workgroup, Glue database and Bedrock model are fixed server-side from the function
environment; no tool argument can replace them.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import boto3
from common.aws import client_config
from common.bedrock import MAX_MAX_TOKENS, MIN_MAX_TOKENS, validate_model_id

from mcp_tools.limits import McpLimits

if TYPE_CHECKING:
    from types_boto3_athena.client import AthenaClient
    from types_boto3_bedrock_runtime.client import BedrockRuntimeClient

CASE_SUMMARY_TABLE: Final = "case_summary"
DATA_CATALOG: Final = "AwsDataCatalog"
_WORKGROUP = re.compile(r"[A-Za-z0-9._-]{1,128}")
_DATABASE = re.compile(r"[a-z0-9_]{1,255}")


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


@dataclass(frozen=True, slots=True)
class McpClients:
    """AWS clients used by the tools."""

    athena: AthenaClient
    bedrock: BedrockRuntimeClient

    @classmethod
    def create(cls, *, region_name: str | None = None) -> McpClients:
        """Clients with timeouts that fit the request budget; Bedrock retries are owned by
        `common.bedrock.invoke_bedrock`."""
        session = boto3.Session()
        return cls(
            athena=session.client(
                "athena",
                region_name=region_name,
                config=client_config(max_attempts=3, connect_timeout=3, read_timeout=10),
            ),
            bedrock=session.client(
                "bedrock-runtime",
                region_name=region_name,
                config=client_config(max_attempts=1, connect_timeout=3, read_timeout=20),
            ),
        )


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

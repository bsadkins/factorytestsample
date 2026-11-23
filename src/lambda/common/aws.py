"""botocore client configuration shared by the Lambda functions."""

from __future__ import annotations

from typing import Final

from botocore.config import Config

USER_AGENT_APP_ID: Final = "support-case-insights"


def client_config(
    *, max_attempts: int = 3, connect_timeout: float = 5, read_timeout: float = 60
) -> Config:
    """Return a botocore `Config` with standard retry mode and explicit timeouts.

    Use `max_attempts=1` where `common.retry.retry_with_backoff` already owns retries, so
    attempts are not multiplied.
    """
    return Config(
        retries={"mode": "standard", "total_max_attempts": max_attempts},
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        user_agent_extra=USER_AGENT_APP_ID,
    )

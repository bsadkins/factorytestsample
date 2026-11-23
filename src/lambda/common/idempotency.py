"""Powertools idempotency backed by the shared DynamoDB table.

The table holds Powertools idempotency records only; it is never a workflow ledger. Callers
choose an idempotency key that includes the run or attempt scope, so a cached result or failure
cannot block a later run.
"""

from __future__ import annotations

import os
from functools import cache
from typing import Final

from aws_lambda_powertools.utilities.idempotency import (
    DynamoDBPersistenceLayer,
    IdempotencyConfig,
)

from common.aws import client_config

IDEMPOTENCY_TABLE_ENV: Final = "IDEMPOTENCY_TABLE_NAME"


@cache
def persistence_layer() -> DynamoDBPersistenceLayer:
    """DynamoDB persistence layer for the table named by `IDEMPOTENCY_TABLE_NAME`."""
    return DynamoDBPersistenceLayer(
        table_name=os.environ[IDEMPOTENCY_TABLE_ENV], boto_config=client_config()
    )


def idempotency_config(*, expires_after_seconds: int) -> IdempotencyConfig:
    """Idempotency settings without a local cache, so every check reads the table."""
    return IdempotencyConfig(expires_after_seconds=expires_after_seconds, use_local_cache=False)

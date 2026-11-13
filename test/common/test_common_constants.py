"""Shared constants agree with the published configuration, and helper odds and ends."""

import json
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError
from common.aws import client_config
from common.bedrock import DEFAULT_MAX_TOKENS, DEFAULT_MODEL_ID, SUPPORTED_MODEL_IDS
from common.safe_logging import error_fields

SCHEMA = Path(__file__).resolve().parents[2] / "config" / "schema.json"


def test_model_ids_and_token_default_match_config_schema() -> None:
    properties = json.loads(SCHEMA.read_text(encoding="utf-8"))["properties"]
    assert list(SUPPORTED_MODEL_IDS) == properties["bedrockModelId"]["enum"]
    assert properties["bedrockModelId"]["default"] == DEFAULT_MODEL_ID
    assert properties["bedrockMaxTokens"]["default"] == DEFAULT_MAX_TOKENS


def test_error_fields_omit_messages() -> None:
    response: Any = {
        "Error": {"Code": "AccessDenied", "Message": "SENTINEL-CASE-TEXT-7f3a"},
        "ResponseMetadata": {"HTTPStatusCode": 403},
    }
    exc = ClientError(response, "Op")
    assert error_fields(exc) == {
        "error_type": "ClientError",
        "error_code": "AccessDenied",
        "http_status": 403,
    }
    assert error_fields(ValueError("SENTINEL-CASE-TEXT-7f3a")) == {"error_type": "ValueError"}


def test_client_config_owns_retry_mode() -> None:
    config = client_config(max_attempts=1, read_timeout=120)
    options = vars(config)
    assert options["retries"] == {"mode": "standard", "total_max_attempts": 1}
    assert options["read_timeout"] == 120

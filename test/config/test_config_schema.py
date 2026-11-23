"""Independent check of the published configuration schema using the Python jsonschema library.

The CDK loader validates with Ajv; validating the same files here with a different implementation
catches schema constructs that only one validator understands.
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
# Synthetic configurations for fixed assertions; operators edit the files in config/.
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "config"

# library/brief-reference.md, "Original config fields".
ORIGINAL_FIELDS = {
    "uniqueId",
    "orgId",
    "managementAccountId",
    "region",
    "environment",
    "enableAnalytics",
    "enableMcp",
    "memberRoleTargets",
    "bedrockModelId",
    "bedrockMaxTokens",
    "bedrockMaxConcurrency",
    "lookbackMonths",
    "rawRetentionDays",
    "logRetentionDays",
    "alarmEmail",
    "mcpAllowedPrincipals",
    "schedules",
}
HAIKU = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
SONNET = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"


def load(name: str, directory: Path = CONFIG_DIR) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((directory / name).read_text(encoding="utf-8"))
    return data


def fixture(name: str) -> dict[str, Any]:
    return load(name, FIXTURE_DIR)


def validator(schema_name: str) -> Draft202012Validator:
    schema = load(schema_name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def errors(document: dict[str, Any]) -> list[str]:
    return [error.message for error in validator("schema.json").iter_errors(document)]


@pytest.mark.parametrize("name", ["example.json", "dev.json", "prod.json"])
def test_committed_configs_are_valid(name: str) -> None:
    assert errors(load(name)) == []


@pytest.mark.parametrize("name", ["dev.json", "prod.json"])
def test_fixture_configs_are_valid(name: str) -> None:
    assert errors(fixture(name)) == []


def test_pipeline_example_and_fixture_are_valid() -> None:
    pipeline = validator("pipeline.schema.json")
    assert list(pipeline.iter_errors(load("pipeline.example.json"))) == []
    assert list(pipeline.iter_errors(fixture("pipeline.json"))) == []
    if (CONFIG_DIR / "pipeline.json").exists():
        assert list(pipeline.iter_errors(load("pipeline.json"))) == []


def test_pipeline_source_repository_is_fixed() -> None:
    pipeline = validator("pipeline.schema.json")
    for repository in ("example-org/factorytestsample", "bsadkins/another-repository"):
        document = {**fixture("pipeline.json"), "repository": repository}
        assert list(pipeline.iter_errors(document)) != []


def test_dev_fixture_enables_both_optional_stacks() -> None:
    dev = fixture("dev.json")
    assert dev["environment"] == "dev"
    assert dev["enableAnalytics"] is True
    assert dev["enableMcp"] is True


def test_schema_contains_every_original_field() -> None:
    properties = load("schema.json")["properties"]
    assert set(properties) >= ORIGINAL_FIELDS
    assert set(properties["schedules"]["properties"]) == {"accountLookup", "cleanup", "crawler"}


def test_schema_defaults_match_the_brief() -> None:
    properties = load("schema.json")["properties"]
    assert properties["region"]["default"] == "us-east-1"
    assert properties["lookbackMonths"]["default"] == 12
    assert properties["bedrockMaxTokens"]["default"] == 1000
    assert properties["logRetentionDays"]["default"] == 90
    assert properties["bedrockModelId"]["default"] == HAIKU
    assert properties["bedrockModelId"]["enum"] == [HAIKU, SONNET]
    schedules = properties["schedules"]["properties"]
    assert schedules["accountLookup"]["default"] == "cron(0 0 * * ? *)"
    assert schedules["crawler"]["default"] == "cron(0 9 * * ? *)"


def test_prod_requires_alarm_email() -> None:
    prod = fixture("prod.json")
    del prod["alarmEmail"]
    assert "'alarmEmail' is a required property" in errors(prod)


def test_mcp_requires_analytics() -> None:
    dev = fixture("dev.json")
    dev["enableAnalytics"] = False
    assert any("True was expected" in message for message in errors(dev))


def test_mcp_reserved_concurrency_is_fixed_at_20() -> None:
    mcp = load("schema.json")["properties"]["operations"]["properties"]["mcp"]["properties"]
    assert mcp["reservedConcurrency"]["const"] == 20
    assert mcp["reservedConcurrency"]["default"] == 20
    for value in (1, 7, 21, 100):
        document = copy.deepcopy(load("example.json"))
        document["operations"]["mcp"]["reservedConcurrency"] = value
        assert errors(document) != []
    assert errors(load("example.json")) == []


def test_shared_api_logging_modes() -> None:
    dev = fixture("dev.json")
    assert dev["apiGatewayLogging"] == fixture("prod.json")["apiGatewayLogging"]
    existing = copy.deepcopy(dev)
    existing["apiGatewayLogging"] = {
        "mode": "existing",
        "sharedId": "scishared",
        "existingRoleArn": "arn:aws:iam::111111111111:role/ApiGatewayLogs",
    }
    assert errors(existing) == []
    missing = copy.deepcopy(existing)
    del missing["apiGatewayLogging"]["existingRoleArn"]
    assert errors(missing) != []
    managed_with_arn = copy.deepcopy(existing)
    managed_with_arn["apiGatewayLogging"]["mode"] = "managed"
    assert errors(managed_with_arn) != []
    for bad in ({"mode": "off"}, {"sharedId": "Bad_Id"}, {"cloudWatchRole": False}):
        document = copy.deepcopy(dev)
        document["apiGatewayLogging"] = bad
        assert errors(document) != []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bedrockModelId", "anthropic.claude-3-haiku-20240307-v1:0"),
        ("lookbackMonths", 13),
        ("logRetentionDays", 45),
        ("uniqueId", "Invalid_ID"),
        ("managementAccountId", "12345"),
        ("memberRoleTargets", []),
        ("alarmEmail", "not-an-email"),
        ("unexpectedField", True),
    ],
)
def test_invalid_values_are_rejected(field: str, value: object) -> None:
    document = copy.deepcopy(fixture("dev.json"))
    document[field] = value
    assert errors(document) != []

"""Tool definition record and JSON Schema helpers shared by the tool modules."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from mcp_tools.context import ToolContext

ToolHandler = Callable[[dict[str, Any], ToolContext], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """One MCP tool: its advertised metadata and argument schema, and the handler that receives
    arguments already validated against that schema (with defaults applied)."""

    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    idempotent: bool = True
    annotations: dict[str, Any] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "annotations",
            {
                "title": self.title,
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": self.idempotent,
                "openWorldHint": False,
            },
        )

    def descriptor(self) -> dict[str, Any]:
        """The `tools/list` entry for this tool."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": dict(self.annotations),
        }


def object_schema(
    properties: Mapping[str, Mapping[str, Any]],
    required: Sequence[str] = (),
    **extra: Any,  # noqa: ANN401
) -> dict[str, Any]:
    """A closed object schema (`additionalProperties: false`)."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {name: dict(prop) for name, prop in properties.items()},
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    schema.update(extra)
    return schema


def integer_property(minimum: int, maximum: int, default: int, description: str) -> dict[str, Any]:
    return {
        "type": "integer",
        "minimum": minimum,
        "maximum": maximum,
        "default": default,
        "description": description,
    }


def percent(part: int, total: int) -> float:
    """Share of `total` as a percentage rounded to one decimal place (0 when total is 0)."""
    return round(100.0 * part / total, 1) if total else 0.0

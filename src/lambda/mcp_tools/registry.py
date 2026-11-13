"""The MCP tool registry: the eight read-only tools, their advertised schemas and dispatch.

`ToolRegistry.call` validates arguments against the advertised input schema (applying defaults)
before a tool handler runs, so invalid arguments never reach Athena or Bedrock. The protocol layer
uses `list_tools()` for `tools/list` and `call()` for `tools/call`; failures raise `ToolError`
subclasses whose `to_payload()` is safe to return.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final

from mcp_tools import tool_analysis, tool_distributions, tool_query_athena, tool_trends
from mcp_tools.context import ToolContext
from mcp_tools.definitions import ToolDefinition
from mcp_tools.errors import ToolInputError, UnknownToolError
from mcp_tools.limits import McpLimits
from mcp_tools.schema import check_schema, normalise_arguments, validate_arguments

TOOL_NAMES: Final = (
    "query_athena",
    "analyze_with_bedrock",
    "get_case_summary",
    "analyze_case_summaries",
    "get_temporal_patterns",
    "get_rca_analysis",
    "get_lifecycle_analysis",
    "get_service_trends",
)


class ToolRegistry:
    """Registered tools in advertised order."""

    def __init__(self, definitions: Iterable[ToolDefinition]) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        for definition in definitions:
            if definition.name in self._tools:
                raise ValueError(f"duplicate tool {definition.name}")
            check_schema(definition.input_schema)
            self._tools[definition.name] = definition

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def list_tools(self) -> list[dict[str, Any]]:
        """`tools/list` descriptors."""
        return [definition.descriptor() for definition in self._tools.values()]

    def call(self, name: str, arguments: object, ctx: ToolContext) -> dict[str, Any]:
        """Validate `arguments` (None means `{}`) and run tool `name`."""
        definition = self._tools.get(name)
        if definition is None:
            raise UnknownToolError
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ToolInputError([{"path": "<arguments>", "message": "must be an object"}])
        issues = validate_arguments(definition.input_schema, arguments)
        if issues:
            raise ToolInputError(issues)
        return definition.handler(normalise_arguments(definition.input_schema, arguments), ctx)


def build_registry(limits: McpLimits | None = None) -> ToolRegistry:
    """The eight tools, with schema bounds taken from `limits`."""
    bounds = limits or McpLimits()
    query = tool_query_athena.definition(bounds)
    with_bedrock, case_summaries = tool_analysis.definitions(bounds)
    case_summary, rca, lifecycle = tool_distributions.definitions(bounds)
    temporal, trends = tool_trends.definitions(bounds)
    registry = ToolRegistry(
        [query, with_bedrock, case_summary, case_summaries, temporal, rca, lifecycle, trends]
    )
    if registry.names() != TOOL_NAMES:
        raise RuntimeError("tool registry order does not match TOOL_NAMES")
    return registry

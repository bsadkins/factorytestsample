"""Read-only query and analysis engine behind the MCP server's eight tools.

The JSON-RPC handler depends only on `mcp_tools.registry` (`build_registry`, `ToolRegistry`,
`TOOL_NAMES`), `mcp_tools.context` (`McpSettings`, `McpClients`, `ToolContext`) and
`mcp_tools.errors`. Everything else is internal: argument schemas and validation, the read-only
SQL guard, fixed parameterised queries, bounded Athena execution and Bedrock analysis.
"""

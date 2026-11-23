"""Stateless MCP JSON-RPC server for the eight read-only Support Case Insights tools.

`mcp_server.app.handler` is the Lambda entry point; `transport` validates the API Gateway
request, `jsonrpc` classifies the message and `server` runs the MCP lifecycle and dispatch.
"""

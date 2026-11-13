"""Cost and time bounds of the MCP tool engine.

API Gateway REST integrations time out after 29 seconds, so a tool call works within a request
budget below that (and below the Lambda function's remaining time). Athena polling backs off
between polls and stops at the budget: `query_athena` then returns a PENDING outcome with the
query execution ID for a later bounded resume, while the fixed analytical tools stop the query and
fail with QUERY_TIMEOUT. Result rows, pages and response bytes are capped explicitly, analysis input
is limited by the argument schemas, and Bedrock output is bounded by the configured max tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

API_GATEWAY_INTEGRATION_TIMEOUT_SECONDS: Final = 29


@dataclass(frozen=True, slots=True)
class McpLimits:
    """Finite limits; tests override individual values."""

    # Time.
    request_budget_seconds: float = 25.0
    lambda_deadline_margin_seconds: float = 1.0
    analysis_query_budget_seconds: float = 10.0
    min_bedrock_seconds: float = 5.0
    poll_initial_seconds: float = 0.2
    poll_max_seconds: float = 2.0
    max_polls: int = 60
    athena_start_attempts: int = 3
    bedrock_max_attempts: int = 2
    # Input.
    max_sql_chars: int = 8_000
    max_sql_tokens: int = 4_000
    max_sql_depth: int = 32
    max_question_chars: int = 2_000
    max_analysis_data_chars: int = 60_000
    max_prompt_chars: int = 200_000
    max_date_range_days: int = 36_600
    # Results.
    default_max_rows: int = 100
    max_rows: int = 1_000
    results_page_size: int = 1_000
    max_result_bytes: int = 256 * 1024
    fixed_query_max_rows: int = 5_000
    fixed_query_max_bytes: int = 1024 * 1024
    default_group_limit: int = 10
    max_group_limit: int = 50
    default_top_services: int = 10
    max_top_services: int = 50
    default_max_cases: int = 100
    max_cases: int = 200

    def __post_init__(self) -> None:
        if self.request_budget_seconds >= API_GATEWAY_INTEGRATION_TIMEOUT_SECONDS:
            raise ValueError("request_budget_seconds must stay below the API Gateway timeout")
        if not 1 <= self.results_page_size <= 1000:  # noqa: PLR2004
            raise ValueError("results_page_size must be between 1 and 1000 (Athena maximum)")
        if self.analysis_query_budget_seconds + self.min_bedrock_seconds > (
            self.request_budget_seconds
        ):
            raise ValueError("analysis budgets exceed the request budget")

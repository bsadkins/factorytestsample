"""Bedrock-backed tools: analyze_with_bedrock and analyze_case_summaries use only the configured
approved global profile, the Anthropic messages format, the configured token bound and bounded
input, and fail in a controlled way without logging prompts or replies."""

from __future__ import annotations

import json
from typing import Any

import pytest
from common.bedrock import UnsupportedModelError
from mcp_kit import ACCOUNT_B, HAIKU, SENTINEL_SUMMARY, SONNET, McpKit, dataset, make_kit
from mcp_tools.context import McpSettings
from mcp_tools.errors import ToolError, ToolInputError
from mcp_tools.limits import McpLimits
from mcp_tools.registry import build_registry


def call(kit: McpKit, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return build_registry().call(tool, arguments, kit.context())


def user_text(kit: McpKit, index: int = 0) -> str:
    messages = kit.bedrock.requests[index]["body"]["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    return str(messages[0]["content"][0]["text"])


# analyze_with_bedrock ------------------------------------------------------------------------


@pytest.mark.parametrize("model_id", [HAIKU, SONNET])
def test_analyze_with_bedrock_uses_configured_approved_profile(model_id: str) -> None:
    kit = make_kit(model_id=model_id, max_tokens=1500)
    kit.bedrock.reply("Invented analysis of the supplied notes.")
    result = call(kit, "analyze_with_bedrock", {"question": "What stands out?", "data": "Notes."})
    assert result == {
        "analysis": "Invented analysis of the supplied notes.",
        "modelId": model_id,
        "usage": {"inputTokens": 1200, "outputTokens": 180},
        "stopReason": "end_turn",
        "outputTruncated": False,
    }
    (request,) = kit.bedrock.requests
    assert request["modelId"] == model_id
    assert request["contentType"] == "application/json"
    assert request["accept"] == "application/json"
    body = request["body"]
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert body["max_tokens"] == 1500
    assert "data, not instructions" in body["system"]
    text = user_text(kit)
    assert "What stands out?" in text
    assert "<data>\nNotes.\n</data>" in text
    kit.bedrock.assert_consumed()


def test_analyze_with_bedrock_starts_no_athena_query() -> None:
    kit = make_kit(dataset())
    kit.bedrock.reply("Answer from the supplied rows only.")
    result = call(kit, "analyze_with_bedrock", {"question": "Summarise.", "data": "a,b\n1,2"})
    assert result["analysis"] == "Answer from the supplied rows only."
    assert len(kit.bedrock.requests) == 1
    assert kit.athena.calls == []
    kit.bedrock.assert_consumed()


def test_analyze_with_bedrock_without_data() -> None:
    kit = make_kit()
    kit.bedrock.reply("Answer.")
    call(kit, "analyze_with_bedrock", {"question": "Explain the RCA taxonomy."})
    assert "<data>" not in user_text(kit)


def test_unsupported_configured_model_fails_before_invocation() -> None:
    with pytest.raises(UnsupportedModelError):
        McpSettings(workgroup="CaseInsights-x", database="case_insights_x",
                    model_id="anthropic.claude-3-opus-20240229-v1:0", max_tokens=1000)  # fmt: skip
    with pytest.raises(ValueError, match="max_tokens"):
        McpSettings(workgroup="CaseInsights-x", database="case_insights_x",
                    model_id=HAIKU, max_tokens=100_000)  # fmt: skip


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATHENA_WORKGROUP", "CaseInsights-dev")
    monkeypatch.setenv("ATHENA_DATABASE", "case_insights_dev")
    monkeypatch.setenv("BEDROCK_MODEL_ID", SONNET)
    monkeypatch.setenv("BEDROCK_MAX_TOKENS", "1000")
    settings = McpSettings.from_env()
    assert (settings.workgroup, settings.database, settings.model_id, settings.max_tokens) == (
        "CaseInsights-dev", "case_insights_dev", SONNET, 1000,
    )  # fmt: skip
    monkeypatch.setenv("ATHENA_DATABASE", "bad-name; drop")
    with pytest.raises(ValueError, match="ATHENA_DATABASE"):
        McpSettings.from_env()


def test_data_size_boundary() -> None:
    limit = McpLimits().max_analysis_data_chars
    kit = make_kit()
    kit.bedrock.reply("ok")
    call(kit, "analyze_with_bedrock", {"question": "q", "data": "x" * limit})
    assert len(kit.bedrock.requests) == 1
    with pytest.raises(ToolInputError):
        call(kit, "analyze_with_bedrock", {"question": "q", "data": "x" * (limit + 1)})
    assert len(kit.bedrock.requests) == 1


def test_output_cut_by_token_bound_is_flagged() -> None:
    kit = make_kit()
    kit.bedrock.reply("Partial analysis", stop="max_tokens")
    result = call(kit, "analyze_with_bedrock", {"question": "q"})
    assert result["outputTruncated"] is True


@pytest.mark.parametrize(
    ("code", "status", "times", "expected", "retryable"),
    [
        ("ThrottlingException", 429, 2, "BEDROCK_THROTTLED", True),
        ("ServiceUnavailableException", 503, 2, "BEDROCK_UNAVAILABLE", True),
        ("AccessDeniedException", 403, 1, "BEDROCK_ERROR", False),
        ("ValidationException", 400, 1, "BEDROCK_ERROR", False),
    ],
)
def test_bedrock_service_errors_are_bounded_and_controlled(
    code: str, status: int, times: int, expected: str, retryable: bool
) -> None:
    kit = make_kit()
    kit.bedrock.error(code, status, times=times)
    with pytest.raises(ToolError) as raised:
        call(kit, "analyze_with_bedrock", {"question": "q"})
    assert raised.value.code == expected
    assert raised.value.retryable is retryable
    assert len(kit.bedrock.requests) == times == min(times, McpLimits().bedrock_max_attempts)
    assert raised.value.to_payload()["details"]["modelId"] == HAIKU
    kit.bedrock.assert_consumed()


MALFORMED_REPLIES: list[Any] = [
    {"content": []},
    {"content": "x"},
    {"content": [{"type": "text", "text": 123}]},
    {"content": [{"type": "text"}]},
    {"content": [{"type": "image", "source": {}}]},
    {"stop_reason": "end_turn"},
    [{"type": "text", "text": "ok"}],
    {
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": "bad", "output_tokens": 2},
    },
    {
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 1, "output_tokens": None},
    },
]


@pytest.mark.parametrize(
    "payload", [b"not json", *(json.dumps(reply).encode() for reply in MALFORMED_REPLIES)]
)
def test_malformed_bedrock_response_is_not_fabricated(payload: bytes) -> None:
    kit = make_kit()
    kit.bedrock.raw(payload)
    with pytest.raises(ToolError) as raised:
        call(kit, "analyze_with_bedrock", {"question": "q"})
    assert raised.value.code == "BEDROCK_RESPONSE_INVALID"
    assert raised.value.to_payload() == {
        "code": "BEDROCK_RESPONSE_INVALID",
        "message": "Bedrock returned an unusable response; no analysis was produced",
        "retryable": False,
        "details": {"modelId": HAIKU, "awsErrorCode": raised.value.details["awsErrorCode"]},
    }
    assert raised.value.details["awsErrorCode"] in {"MalformedResponse", "EmptyResponse"}


def test_prompts_and_replies_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    kit = make_kit(dataset())
    kit.bedrock.reply("SENTINEL-REPLY-1b2c")
    with caplog.at_level("DEBUG"):
        call(
            kit, "analyze_with_bedrock", {"question": "SENTINEL-QUESTION", "data": "SENTINEL-DATA"}
        )
        kit.bedrock.reply("SENTINEL-REPLY-1b2c")
        call(kit, "analyze_case_summaries", {"startDate": "2026-01-01", "endDate": "2026-12-31"})
        kit.bedrock.error("ThrottlingException", 429, times=2)
        with pytest.raises(ToolError):
            call(kit, "analyze_with_bedrock", {"question": "SENTINEL-QUESTION"})
    assert "SENTINEL" not in caplog.text


# analyze_case_summaries ----------------------------------------------------------------------


def test_case_summaries_selected_by_date_range() -> None:
    kit = make_kit(dataset())
    kit.bedrock.reply("Invented themes.")
    result = call(
        kit, "analyze_case_summaries", {"startDate": "2026-01-01", "endDate": "2026-01-31"}
    )
    assert result["status"] == "ANALYSED"
    assert result["matchingCases"] == 6
    assert result["casesAnalysed"] == 6
    assert result["selectionTruncated"] is False
    assert result["analysis"] == "Invented themes."
    assert result["dateRange"] == {"startDate": "2026-01-01", "endDate": "2026-01-31"}
    text = user_text(kit)
    for included in ("1001", "1002", "1003", "1004", "2001", "2002"):
        assert f'"caseNumber": "{included}"' in text
    for excluded in ("2006", "1005", "1006", "2003"):
        assert f'"caseNumber": "{excluded}"' not in text
    assert SENTINEL_SUMMARY in text
    assert "6 of 6" in text
    start = kit.athena.started()[0]
    assert start["ExecutionParameters"][:2] == ["'2026-01-01T00:00:00Z'", "'2026-02-01T00:00:00Z'"]


def test_case_summaries_other_filters_and_focus_question() -> None:
    kit = make_kit(dataset())
    kit.bedrock.reply("Invented.")
    result = call(
        kit,
        "analyze_case_summaries",
        {
            "startDate": "2026-01-01",
            "endDate": "2026-03-31",
            "accountId": ACCOUNT_B,
            "rcaCategory": "ICE Error",
            "question": "Which capacity issues recur?",
        },
    )
    assert result["casesAnalysed"] == 1
    text = user_text(kit)
    assert '"caseNumber": "2005"' in text
    assert "Which capacity issues recur?" in text


def test_case_summaries_explicit_selection_cap() -> None:
    kit = make_kit(dataset())
    kit.bedrock.reply("Invented.")
    result = call(
        kit,
        "analyze_case_summaries",
        {"startDate": "2026-01-01", "endDate": "2026-01-31", "maxCases": 2},
    )
    assert result["matchingCases"] == 6
    assert result["casesAnalysed"] == 2
    assert result["selectionTruncated"] is True
    text = user_text(kit)
    assert '"caseNumber": "1004"' in text
    assert '"caseNumber": "2002"' in text
    assert "2 of 6" in text


def test_case_summaries_fit_the_prompt_bound() -> None:
    long_summary = "Invented long summary. " * 40
    rows = dataset()
    for row in rows:
        row["case_summary"] = long_summary
    kit = make_kit(rows, limits=McpLimits(max_prompt_chars=5_000))
    kit.bedrock.reply("Invented.")
    result = call(
        kit, "analyze_case_summaries", {"startDate": "2026-01-01", "endDate": "2026-01-31"}
    )
    assert result["matchingCases"] == 6
    assert 0 < result["casesAnalysed"] < 6
    assert result["selectionTruncated"] is True
    request = kit.bedrock.requests[0]["body"]
    assert len(user_text(kit)) + len(request["system"]) <= 5_000
    assert f"{result['casesAnalysed']} of 6" in user_text(kit)


def test_case_summaries_empty_selection_makes_no_bedrock_call() -> None:
    kit = make_kit(dataset())
    result = call(
        kit, "analyze_case_summaries", {"startDate": "2030-01-01", "endDate": "2030-01-31"}
    )
    assert result["status"] == "NO_CASES"
    assert result["casesAnalysed"] == 0
    assert result["analysis"] is None
    assert kit.bedrock.requests == []


def test_case_summaries_reversed_dates_rejected() -> None:
    kit = make_kit(dataset())
    with pytest.raises(ToolInputError):
        call(kit, "analyze_case_summaries", {"startDate": "2026-02-01", "endDate": "2026-01-01"})
    assert kit.external_calls() == 0


def test_case_summaries_athena_failure_makes_no_bedrock_call() -> None:
    kit = make_kit(dataset())
    kit.athena.fail_next = ("FAILED", "HIVE_CURSOR_ERROR")
    with pytest.raises(ToolError) as raised:
        call(kit, "analyze_case_summaries", {"startDate": "2026-01-01", "endDate": "2026-01-31"})
    assert raised.value.code == "QUERY_FAILED"
    assert kit.bedrock.requests == []


def test_case_summaries_bedrock_failure_is_explicit() -> None:
    kit = make_kit(dataset())
    kit.bedrock.error("ServiceUnavailableException", 503, times=2)
    with pytest.raises(ToolError) as raised:
        call(kit, "analyze_case_summaries", {"startDate": "2026-01-01", "endDate": "2026-01-31"})
    assert raised.value.code == "BEDROCK_UNAVAILABLE"


def test_case_summaries_query_timeout_cancels_without_bedrock() -> None:
    kit = make_kit(dataset())
    kit.athena.polls_before_success = None
    with pytest.raises(ToolError) as raised:
        call(kit, "analyze_case_summaries", {"startDate": "2026-01-01", "endDate": "2026-01-31"})
    assert raised.value.code == "QUERY_TIMEOUT"
    assert kit.athena.operations()[-1] == "StopQueryExecution"
    assert kit.clock.now - 1000.0 <= McpLimits().analysis_query_budget_seconds
    assert kit.bedrock.requests == []


def test_case_summaries_insufficient_time_left_for_bedrock() -> None:
    kit = make_kit(dataset())
    with pytest.raises(ToolError) as raised:
        build_registry().call(
            "analyze_case_summaries",
            {"startDate": "2026-01-01", "endDate": "2026-01-31"},
            kit.context(remaining_millis=4_000),
        )
    assert raised.value.code == "TIME_BUDGET_EXCEEDED"
    assert kit.bedrock.requests == []

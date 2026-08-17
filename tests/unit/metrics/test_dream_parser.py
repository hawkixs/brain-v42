"""Unit tests for dream OTEL parser — parse_otel_log function.

Tests use the REAL Claude Code OTEL console format (validated 2026-04-06):
- Event type in `body: "claude_code.api_request"`
- Values as quoted strings in attributes: `input_tokens: "3"`
- Mixed with report text on stdout
"""

from __future__ import annotations

from brain_v42.metrics.dream_parser import PhaseTelemetry, parse_otel_log

# Real OTEL console output from Claude Code (sanitized IDs)
SAMPLE_OTEL_OUTPUT = """
Some report text here that should be ignored.

## Dream Scan Report
Entity counts: 1368 active...

{
  resource: {
    attributes: {
      "service.name": "claude-code",
      "service.version": "2.1.92",
    },
  },
  instrumentationScope: {
    name: "com.anthropic.claude_code.events",
    version: "2.1.92",
    schemaUrl: undefined,
  },
  timestamp: 1775432814259000,
  traceId: undefined,
  spanId: undefined,
  body: "claude_code.api_request",
  attributes: {
    "user.id": "abc123",
    "session.id": "sess-001",
    "event.name": "api_request",
    model: "claude-sonnet-4-6",
    input_tokens: "3500",
    output_tokens: "250",
    cache_read_tokens: "12000",
    cache_creation_tokens: "27603",
    cost_usd: "0.17",
    duration_ms: "3460",
    speed: "normal",
  },
}
{
  resource: {
    attributes: {
      "service.name": "claude-code",
    },
  },
  instrumentationScope: {
    name: "com.anthropic.claude_code.events",
  },
  body: "claude_code.tool_result",
  attributes: {
    "session.id": "sess-001",
    "event.name": "tool_result",
    tool_name: "mcp__brain-v42__brain_decay_status",
    success: "true",
    duration_ms: "45",
  },
}
{
  resource: {
    attributes: {
      "service.name": "claude-code",
    },
  },
  instrumentationScope: {
    name: "com.anthropic.claude_code.events",
  },
  body: "claude_code.tool_result",
  attributes: {
    "session.id": "sess-001",
    "event.name": "tool_result",
    tool_name: "mcp__brain-v42__brain_list",
    success: "true",
    duration_ms: "32",
  },
}
{
  resource: {
    attributes: {
      "service.name": "claude-code",
    },
  },
  instrumentationScope: {
    name: "com.anthropic.claude_code.events",
  },
  body: "claude_code.api_request",
  attributes: {
    "session.id": "sess-001",
    "event.name": "api_request",
    model: "claude-sonnet-4-6",
    input_tokens: "5000",
    output_tokens: "800",
    cache_read_tokens: "15000",
    cache_creation_tokens: "0",
    cost_usd: "0.08",
    duration_ms: "2100",
    speed: "normal",
  },
}

More report text that should also be ignored.
"""

SAMPLE_EMPTY = ""

SAMPLE_REPORT_ONLY = """
## Dream Scan Report

Entity counts: 1368 active, 353 archived.
No OTEL events in this output.
"""

SAMPLE_METRICS_ONLY = """
{
  instrumentationScope: {
    name: "com.anthropic.claude_code.events",
  },
  body: "claude_code.session_init",
  attributes: {
    "session.id": "sess-001",
  },
}
"""


class TestParseOtelLog:
    def test_parse_api_requests_counts(self) -> None:
        """Counts api_request events correctly."""
        result = parse_otel_log(SAMPLE_OTEL_OUTPUT)
        assert result.api_calls == 2

    def test_parse_input_tokens_summed(self) -> None:
        """Sums input_tokens across all api_request events."""
        result = parse_otel_log(SAMPLE_OTEL_OUTPUT)
        assert result.input_tokens == 3500 + 5000

    def test_parse_output_tokens_summed(self) -> None:
        """Sums output_tokens across all api_request events."""
        result = parse_otel_log(SAMPLE_OTEL_OUTPUT)
        assert result.output_tokens == 250 + 800

    def test_parse_cache_read_tokens(self) -> None:
        """Sums cache_read_tokens across events."""
        result = parse_otel_log(SAMPLE_OTEL_OUTPUT)
        assert result.cache_read_tokens == 12000 + 15000

    def test_parse_cache_creation_tokens(self) -> None:
        """Sums cache_creation_tokens across events."""
        result = parse_otel_log(SAMPLE_OTEL_OUTPUT)
        assert result.cache_creation_tokens == 27603 + 0

    def test_parse_cost_summed(self) -> None:
        """Sums cost_usd across all api_request events."""
        result = parse_otel_log(SAMPLE_OTEL_OUTPUT)
        assert abs(result.cost_usd - 0.25) < 0.001

    def test_parse_tool_calls_counted(self) -> None:
        """Counts tool_result events."""
        result = parse_otel_log(SAMPLE_OTEL_OUTPUT)
        assert result.tool_calls == 2

    def test_parse_empty_string(self) -> None:
        """Empty input returns zero telemetry."""
        result = parse_otel_log(SAMPLE_EMPTY)
        assert result == PhaseTelemetry()

    def test_parse_report_only_no_events(self) -> None:
        """Report text without OTEL events returns zero telemetry."""
        result = parse_otel_log(SAMPLE_REPORT_ONLY)
        assert result.api_calls == 0
        assert result.tool_calls == 0

    def test_ignores_non_api_non_tool_events(self) -> None:
        """Non api_request/tool_result events are ignored."""
        result = parse_otel_log(SAMPLE_METRICS_ONLY)
        assert result.api_calls == 0
        assert result.tool_calls == 0

    def test_returns_phase_telemetry_type(self) -> None:
        """Returns a PhaseTelemetry dataclass."""
        result = parse_otel_log(SAMPLE_OTEL_OUTPUT)
        assert isinstance(result, PhaseTelemetry)

    def test_mixed_report_and_otel_extracts_only_otel(self) -> None:
        """Report text mixed with OTEL is handled — only OTEL extracted."""
        result = parse_otel_log(SAMPLE_OTEL_OUTPUT)
        # Should have found events despite report text around them
        assert result.api_calls == 2
        assert result.tool_calls == 2
        assert result.input_tokens > 0


class TestExtractErrorTail:
    """extract_error_tail returns the last meaningful lines from a failed phase log."""

    def test_returns_none_for_empty_content(self) -> None:
        from brain_v42.metrics.dream_parser import extract_error_tail

        assert extract_error_tail("") is None

    def test_captures_max_turns_error(self) -> None:
        from brain_v42.metrics.dream_parser import extract_error_tail

        content = "prior output\nsome stuff\nError: Reached max turns (20)\n{otel blob}\n"
        tail = extract_error_tail(content)
        assert tail is not None
        assert "Reached max turns (20)" in tail

    def test_truncates_to_max_chars(self) -> None:
        from brain_v42.metrics.dream_parser import extract_error_tail

        content = "Error: boom\n" + ("x" * 5000)
        tail = extract_error_tail(content, max_chars=500)
        assert tail is not None
        assert len(tail) <= 500

    def test_strips_otel_json_blobs(self) -> None:
        """OTEL blobs are noise — keep textual error lines."""
        from brain_v42.metrics.dream_parser import extract_error_tail

        content = (
            "Error: Reached max turns (30)\n"
            '{\n  resource: {},\n  body: "claude_code.api_request",\n}\n'
        )
        tail = extract_error_tail(content)
        assert tail is not None
        assert "Reached max turns" in tail
        assert "claude_code.api_request" not in tail


class TestDetectTerminalFailure:
    """detect_terminal_failure flags a phase that exited 0 but failed semantically.

    A phase can exit 0 (clean shell exit) yet fail — most notably when the
    model's tool call could not be parsed — so the night was recorded 'done'
    (2026-06-22 audit). Detection must key on the definitive signature, NEVER on
    heuristics like 0 tool_calls (many nights legitimately no-op).
    """

    def test_returns_none_for_empty(self) -> None:
        from brain_v42.metrics.dream_parser import detect_terminal_failure

        assert detect_terminal_failure("") is None

    def test_returns_none_for_clean_noop_report(self) -> None:
        from brain_v42.metrics.dream_parser import detect_terminal_failure

        # A normal no-op night (no candidates, 0 tool calls) is NOT a failure.
        assert detect_terminal_failure(SAMPLE_REPORT_ONLY) is None

    def test_detects_unparseable_tool_call(self) -> None:
        from brain_v42.metrics.dream_parser import detect_terminal_failure

        content = "## Reorg Report\nThe model's tool call could not be parsed (retry also failed)\n"
        signature = detect_terminal_failure(content)
        assert signature is not None
        assert "could not be parsed" in signature

    def test_is_case_insensitive(self) -> None:
        from brain_v42.metrics.dream_parser import detect_terminal_failure

        # Case-insensitivity holds on the DEFINITIVE post-retry signature. The
        # bare phrase is intentionally non-terminal (see tests below).
        assert (
            detect_terminal_failure("TOOL CALL COULD NOT BE PARSED (RETRY ALSO FAILED)") is not None
        )

    def test_does_not_match_normal_report_mentioning_tools(self) -> None:
        from brain_v42.metrics.dream_parser import detect_terminal_failure

        # Benign prose about tool calls must not trip the detector.
        content = "Made 0 tool calls this run; corpus already clean.\n"
        assert detect_terminal_failure(content) is None

    def test_does_not_match_bare_first_attempt_message(self) -> None:
        from brain_v42.metrics.dream_parser import detect_terminal_failure

        # "could not be parsed" WITHOUT the "(retry also failed)" qualifier is the
        # first-attempt message — Claude Code retries and frequently succeeds, so
        # it is NOT terminal. Only the post-retry failure is definitive.
        content = "Tool call could not be parsed, retrying...\nRetry succeeded.\n"
        assert detect_terminal_failure(content) is None

    def test_does_not_match_synth_report_quoting_signature(self) -> None:
        from brain_v42.metrics.dream_parser import detect_terminal_failure

        # Real 2026-06-25 SYNTH report: it DOCUMENTS this very detector, quoting
        # the signature string in backticks. Claude Code never backtick-wraps its
        # own terminal emission, so a quoted mention must not trip the detector
        # (self-referential false positive that flipped synth done->fail).
        content = (
            "- The masked-failure detector keys on the literal string "
            '`"tool call could not be parsed"` -> silent on the outage signature.\n'
        )
        assert detect_terminal_failure(content) is None

    def test_does_not_match_full_signature_quoted_in_backticks(self) -> None:
        from brain_v42.metrics.dream_parser import detect_terminal_failure

        # Even the FULL definitive phrase, when quoted in inline code by a report
        # discussing the detector, is the model talking — not a real failure.
        content = "We match on `tool call could not be parsed (retry also failed)` now.\n"
        assert detect_terminal_failure(content) is None

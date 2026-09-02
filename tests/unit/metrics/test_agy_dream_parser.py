"""The agy rail's metrics parser — and the cache convention that opposes codex.

Without it, a phase played by agy is played but NOT MEASURED, and that hole
dictated, for a few hours, an absurd chain order: claude placed before agy to
preserve the `dream_runs` rows, thus putting the subscription we wanted to spare
on the front line. Optimizing the measurement against the objective.

THE CENTRAL TRAP, measured on 2026-08-11 on a real run:

    input_tokens      = 20137
    cache_read_tokens = 56950      <-- LARGER than input_tokens
    total_tokens      = 21691      = input + output, the cache is NOT in it

With codex, `cached_input_tokens` is a SUBSET of `input_tokens` and the parser
computes `fresh = input - cached`. With agy the two counters are INDEPENDENT:
`input_tokens` is already the fresh one. Applying codex's formula here would
produce a NEGATIVE number — and nobody looks at a token column closely enough to
notice for weeks.
"""

from __future__ import annotations

import json

import pytest

from brain_v42.metrics.agy_dream_parser import _unrecovered_error, parse_agy_stream

# The exact message agy writes into `result.error` when an MCP call failed.
_TOOL_ERROR = (
    "Error in MCP tool execution: 4 validation errors for call[brain_save_snippet]\n"
    "title\n  Missing required argument"
)

# A complete capture of a real run, not a mock-up.
_REAL_RESULT_USAGE = {
    "input_tokens": 20137,
    "output_tokens": 1554,
    "thinking_tokens": 962,
    "cache_read_tokens": 56950,
    "total_tokens": 21691,
}


def _stream(*events: dict[str, object]) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def _result_event(usage: dict[str, object] | None = None, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {"status": "SUCCESS", "response": "rapport"}
    payload["usage"] = usage if usage is not None else dict(_REAL_RESULT_USAGE)
    payload.update(extra)
    return {"event": "result", "result": payload}


def _tool_step(tool_name: str, state: str = "DONE") -> dict[str, object]:
    return {
        "event": "step_update",
        "step_update": {
            "step_index": 3,
            "step_type": "tool",
            "state": state,
            "tool_name": tool_name,
        },
    }


def _mcp_step(tool: str, state: str, step_index: int) -> dict[str, object]:
    """A `call_mcp_tool` step, with the tool actually called.

    The tool name lives in `tool_info.parameters.ToolName` — `tool_name` is always
    `call_mcp_tool` and therefore distinguishes nothing.
    """
    return {
        "event": "step_update",
        "step_update": {
            "step_index": step_index,
            "step_type": "tool",
            "state": state,
            "tool_name": "call_mcp_tool",
            "tool_info": {"parameters": {"ServerName": "brain-v42", "ToolName": tool}},
        },
    }


def test_a_tool_error_retried_successfully_is_not_terminal() -> None:
    """THE test of this rule, captured on brain-v42/synth of 2026-08-12.

    Step 47: `brain_save_snippet(description=…, topic=…)` -> ERROR, invalid
    arguments. Step 50: `brain_save_snippet(title=…, intention=…)` -> DONE. agy
    latches `result.status=ERROR` nonetheless, and the phase was counted `fail`
    while its three artifacts are in the database (93ae6c8d, adbc5f88, 2989018b).
    """
    content = _stream(
        _mcp_step("brain_save_snippet", "ERROR", 47),
        _mcp_step("brain_save_snippet", "DONE", 50),
        _result_event(status="ERROR", error=_TOOL_ERROR),
    )

    assert _unrecovered_error(content) is None


def test_a_phase_that_ends_on_a_failed_tool_call_stays_terminal() -> None:
    """Captured on watchk-claude/synth: two learnings written, then a final failed
    `brain_learn` that nothing retries. The write is lost for good — that red must
    stay red."""
    content = _stream(
        _mcp_step("brain_learn", "DONE", 42),
        _mcp_step("brain_learn", "ERROR", 46),
        _result_event(status="ERROR", error="Error in MCP tool execution: 'brain_learn'"),
    )

    assert _unrecovered_error(content) == "Error in MCP tool execution: 'brain_learn'"


def test_another_tool_succeeding_later_does_not_prove_recovery() -> None:
    """Captured on refondrre/connect: `brain_assign_domain` fails, then a
    `brain_list` succeeds. Another tool passing says NOTHING about the lost write.
    Only the same call retried proves the recovery."""
    content = _stream(
        _mcp_step("brain_assign_domain", "ERROR", 28),
        _mcp_step("brain_list", "DONE", 36),
        _result_event(status="ERROR", error="Error in MCP tool execution: brain_assign_domain"),
    )

    assert _unrecovered_error(content) is not None


def test_an_agy_level_error_stays_terminal_despite_a_successful_retry() -> None:
    """The suppression applies ONLY to a tool failure. A failure of agy itself
    carries off the phase, whatever the tools did before it."""
    content = _stream(
        _mcp_step("brain_learn", "ERROR", 4),
        _mcp_step("brain_learn", "DONE", 7),
        {"event": "error", "message": "agy: connection reset"},
        _result_event(status="ERROR", error=_TOOL_ERROR),
    )

    assert _unrecovered_error(content) == "agy: connection reset"


def test_a_failure_status_without_a_tool_error_stays_terminal() -> None:
    """A failing `result.status` with no tool message — a timeout, for instance —
    is not recoverable by a successful retry."""
    content = _stream(
        _mcp_step("brain_learn", "ERROR", 4),
        _mcp_step("brain_learn", "DONE", 7),
        _result_event(status="TIMEOUT"),
    )

    assert _unrecovered_error(content) == "TIMEOUT"


def test_steps_without_an_index_cannot_prove_recovery() -> None:
    """Fail-closed: without an index, the step order is unknown and "afterwards"
    loses its meaning. Assuming recovery would yield an unmeasured green."""
    step = _mcp_step("brain_save_snippet", "DONE", 50)
    del step["step_update"]["step_index"]  # type: ignore[index]
    content = _stream(
        _mcp_step("brain_save_snippet", "ERROR", 47),
        step,
        _result_event(status="ERROR", error=_TOOL_ERROR),
    )

    assert _unrecovered_error(content) is not None


def test_cache_reads_are_not_subtracted_from_input() -> None:
    """THE test of this file. agy's convention is the opposite of codex's."""
    telemetry = parse_agy_stream(_stream(_result_event()))

    assert telemetry.input_tokens == 20137, "input_tokens d'agy est DÉJÀ le frais"
    assert telemetry.cache_read_tokens == 56950
    assert telemetry.output_tokens == 1554
    assert telemetry.input_tokens > 0, "la soustraction de codex donnerait un négatif ici"


def test_the_result_event_is_the_authoritative_aggregate() -> None:
    """Measured: the sum of the `step_update` usages equals exactly the `result`'s.
    Adding both would double every counter."""
    telemetry = parse_agy_stream(
        _stream(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 0,
                    "step_type": "agent_response",
                    "state": "DONE",
                    "usage": {
                        "input_tokens": 10480,
                        "output_tokens": 383,
                        "cache_read_tokens": 8141,
                    },
                },
            },
            _result_event(),
        )
    )

    assert telemetry.input_tokens == 20137
    assert telemetry.output_tokens == 1554


def test_only_completed_mcp_steps_are_counted_as_tool_calls() -> None:
    """A run_command REFUSED by the guard produces a tool step. Counting it would
    suggest a phase touched the brain when it was prevented from doing so."""
    telemetry = parse_agy_stream(
        _stream(
            _tool_step("call_mcp_tool"),
            _tool_step("call_mcp_tool"),
            _tool_step("run_command"),
            _tool_step("call_mcp_tool", state="ERROR"),
            _result_event(),
        )
    )

    assert telemetry.tool_calls == 2


def test_subscription_backed_fields_stay_null_instead_of_zero() -> None:
    """agy is backed by a subscription: it exposes neither cost nor API call
    count. Writing 0 would assert "free" and "no call", two lies; NULL says "not
    measured"."""
    telemetry = parse_agy_stream(_stream(_result_event()))

    assert telemetry.cost_usd is None
    assert telemetry.api_calls is None
    assert telemetry.cache_creation_tokens is None


def test_a_stream_without_a_result_event_is_rejected() -> None:
    """Fail-closed: without a `result` event, no total is known. Persisting zeros
    would pass an unmeasured phase off as a free one."""
    with pytest.raises(ValueError, match="result"):
        parse_agy_stream(_stream(_tool_step("call_mcp_tool")))


def test_a_result_without_usage_is_rejected() -> None:
    with pytest.raises(ValueError, match="usage"):
        parse_agy_stream(_stream({"event": "result", "result": {"status": "SUCCESS"}}))


def test_malformed_lines_are_skipped_not_fatal() -> None:
    """The stream may carry diagnostic lines, or lines from a future version."""
    telemetry = parse_agy_stream(
        "pas du json\n" + json.dumps(["liste"]) + "\n" + _stream(_result_event())
    )

    assert telemetry.input_tokens == 20137


def test_negative_or_absurd_counters_are_rejected() -> None:
    with pytest.raises(ValueError):
        parse_agy_stream(_stream(_result_event(usage={"input_tokens": -1, "output_tokens": 5})))


def test_a_failed_run_still_yields_its_usage() -> None:
    """A phase that fails still consumed tokens. Losing them would skew the real
    cost of a degraded night — precisely the night one wants to be able to
    price."""
    telemetry = parse_agy_stream(
        _stream(
            _result_event(
                status="ERROR",
                usage={"input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 3},
            )
        )
    )

    assert telemetry.input_tokens == 10
    assert telemetry.output_tokens == 2


def test_thinking_tokens_are_measured_separately_and_never_summed() -> None:
    """Ticket 76e11c9f: 962 thinking for 1554 output on the real run — ~38 % of the
    tokens were counted nowhere, while the rail order was settled on a compared
    cost. The 049 column carries them SEPARATELY: adding them to output would make
    the rails incomparable in the other direction."""
    telemetry = parse_agy_stream(_stream(_result_event(_REAL_RESULT_USAGE)))

    assert telemetry.thinking_tokens == 962
    assert telemetry.output_tokens == 1554, "jamais gonflé par le thinking"


def test_a_stream_without_thinking_stays_null_not_zero() -> None:
    """Absent from the stream = "this rail does not measure" (NULL), never "measured as zero"."""
    usage = {key: value for key, value in _REAL_RESULT_USAGE.items() if key != "thinking_tokens"}

    telemetry = parse_agy_stream(_stream(_result_event(usage)))

    assert telemetry.thinking_tokens is None

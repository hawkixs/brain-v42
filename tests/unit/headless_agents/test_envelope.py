"""Unwrapping CLI envelopes: pure ``str -> Reply``, never raising."""

from __future__ import annotations

import json

from headless_agents.envelope import Reply, unwrap, unwrap_agy, unwrap_claude, unwrap_codex
from headless_agents.result import TokenUsage


class TestClaude:
    def test_reads_result_model_usage_and_cost(self) -> None:
        envelope = {
            "result": "ANSWER",
            "total_cost_usd": 0.0421,
            "usage": {"input_tokens": 10, "output_tokens": 4692},
            "modelUsage": {
                "claude-sonnet-5": {
                    "inputTokens": 10,
                    "outputTokens": 4709,
                    "cacheReadInputTokens": 3,
                }
            },
        }
        reply = unwrap_claude(json.dumps(envelope), expected_model="sonnet")
        assert reply == Reply(
            text="ANSWER",
            model_reported="claude-sonnet-5",
            tokens=TokenUsage(input=10, output=4709, cached=3),
            cost_usd=0.0421,
        )

    def test_picks_the_expected_model_among_several(self) -> None:
        envelope = {
            "result": "A",
            "modelUsage": {
                "claude-haiku-4-5": {"inputTokens": 1, "outputTokens": 1},
                "claude-opus-5": {"inputTokens": 2, "outputTokens": 2, "canonicalModel": "opus"},
            },
        }
        reply = unwrap_claude(json.dumps(envelope), expected_model="opus")
        assert reply.model_reported == "claude-opus-5"
        assert reply.tokens == TokenUsage(input=2, output=2)

    def test_several_models_without_a_match_fall_back_to_the_global_usage(self) -> None:
        envelope = {
            "result": "A",
            "usage": {"input_tokens": 7, "output_tokens": 8},
            "modelUsage": {"m1": {"inputTokens": 1}, "m2": {"inputTokens": 2}},
        }
        reply = unwrap_claude(json.dumps(envelope))
        assert reply.model_reported is None
        assert reply.tokens == TokenUsage(input=7, output=8)

    def test_an_unreadable_envelope_keeps_the_raw_text(self) -> None:
        assert unwrap_claude("plain text, not json") == Reply(text="plain text, not json")
        assert unwrap_claude("[1, 2]") == Reply(text="[1, 2]")
        assert unwrap_claude("") == Reply(text="")


class TestCodex:
    def test_reads_the_agent_message_and_the_turn_usage(self) -> None:
        lines = [
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "first"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "FINAL"}},
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 40,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 5,
                },
            },
        ]
        stdout = "warning: plain text\n" + "\n".join(json.dumps(line) for line in lines) + "\n"
        reply = unwrap_codex(stdout)
        assert reply == Reply(
            text="FINAL",
            tokens=TokenUsage(input=100, output=20, fresh=60, cached=40, thinking=5),
        )

    def test_no_message_keeps_the_raw_text(self) -> None:
        stdout = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}) + "\n"
        reply = unwrap_codex(stdout)
        assert reply.text == stdout
        assert reply.tokens == TokenUsage(input=1)


class TestAgy:
    def test_reads_a_single_json_object(self) -> None:
        envelope = {
            "response": "ANSWER",
            "usage": {"input_tokens": 3, "output_tokens": 4, "thinking_tokens": 5},
        }
        assert unwrap_agy(json.dumps(envelope)) == Reply(
            text="ANSWER", tokens=TokenUsage(input=3, output=4, thinking=5)
        )

    def test_reads_the_result_event_of_a_stream(self) -> None:
        lines = [
            {"event": "init"},
            {"event": "step_update", "step_update": {"step_type": "tool"}},
            {
                "event": "result",
                "result": {
                    "response": "ANSWER",
                    "usage": {
                        "input_tokens": 170,
                        "output_tokens": 24,
                        "thinking_tokens": 18,
                        "cache_read_tokens": 628,
                    },
                },
            },
        ]
        stdout = "\n".join(json.dumps(line) for line in lines) + "\n"
        assert unwrap_agy(stdout) == Reply(
            text="ANSWER", tokens=TokenUsage(input=170, output=24, cached=628, thinking=18)
        )

    def test_an_unreadable_envelope_keeps_the_raw_text(self) -> None:
        assert unwrap_agy("hello") == Reply(text="hello")


def test_unwrap_dispatches_by_provider_and_text_is_the_identity() -> None:
    assert unwrap("text", "raw") == Reply(text="raw")
    assert unwrap("claude", json.dumps({"result": "A"})) == Reply(text="A")
    assert unwrap("codex", "raw").text == "raw"
    assert unwrap("agy", json.dumps({"response": "A"})) == Reply(text="A")
    assert unwrap("unknown", "raw") == Reply(text="raw")

"""State, ordering, replay, and privacy contract for Codex telemetry."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from brain_v42.metrics.codex_telemetry import (
    ACTIVITY_TTL_SECONDS,
    FINGERPRINT_TTL_SECONDS,
    MAX_ACTIVE_CONVERSATIONS,
    MAX_FINGERPRINTS,
    CodexConversationRegistry,
    CodexTelemetryMalformedError,
)

FAKE_UUID = "12345678-1234-4abc-8def-1234567890ab"


class FakeTime:
    def __init__(self) -> None:
        self.monotonic = 0.0
        self.wall = datetime(2026, 7, 20, 8, 5, tzinfo=UTC)

    def advance(self, seconds: float) -> None:
        self.monotonic += seconds
        self.wall += timedelta(seconds=seconds)

    def monotonic_now(self) -> float:
        return self.monotonic

    def wall_now(self) -> datetime:
        return self.wall


def _attr(key: str, value: dict[str, object]) -> dict[str, object]:
    return {"key": key, "value": value}


def _record(
    conversation_id: str = FAKE_UUID,
    *,
    name: str = "codex.user_prompt",
    timestamp: int | None = 1,
    kind: str | None = None,
    tokens: int | None = None,
    model: str | None = "gpt-5.4",
) -> dict[str, object]:
    attributes = [
        _attr("event.name", {"stringValue": name}),
        _attr("conversation.id", {"stringValue": conversation_id}),
    ]
    if kind is not None:
        attributes.append(_attr("event.kind", {"stringValue": kind}))
    if tokens is not None:
        attributes.append(_attr("input_token_count", {"intValue": tokens}))
    if model is not None:
        attributes.append(_attr("model", {"stringValue": model}))
    record: dict[str, object] = {"attributes": attributes}
    if timestamp is not None:
        record["timeUnixNano"] = str(timestamp)
    return record


def _payload(*records: dict[str, object]) -> bytes:
    return json.dumps(
        {"resourceLogs": [{"scopeLogs": [{"logRecords": list(records)}]}]},
        separators=(",", ":"),
    ).encode()


def _completion(
    tokens: int,
    timestamp: int | None,
    *,
    conversation_id: str = FAKE_UUID,
) -> dict[str, object]:
    return _record(
        conversation_id,
        name="codex.sse_event",
        kind="response.completed",
        tokens=tokens,
        timestamp=timestamp,
    )


def _registry(fake_time: FakeTime | None = None, *, secret: bytes = b"x" * 32):
    fake_time = fake_time or FakeTime()
    return CodexConversationRegistry(
        secret=secret,
        clock=fake_time.monotonic_now,
        wall_clock=fake_time.wall_now,
    )


def test_one_prompt_and_two_completions_count_one_turn_and_latest_context() -> None:
    registry = _registry()

    registry.ingest_otlp_json(
        _payload(
            _record(timestamp=10),
            _completion(500, 30),
            _completion(900, 20),
        )
    )

    assert registry.snapshot()["activeConvs"] == [
        {
            "id": "codex-74f165fb349c73a6b26c68bf9504b070",
            "topic": "[redacted]",
            "agent": "codex",
            "started": "08:05",
            "turns": 1,
            "tokens": 500,
            "model": "gpt-5.4",
            "cost": None,
        }
    ]


def test_conversation_start_never_updates_context_tokens() -> None:
    registry = _registry()

    registry.ingest_otlp_json(
        _payload(
            _record(
                name="codex.conversation_starts",
                timestamp=10,
                tokens=777,
            )
        )
    )

    snapshot = registry.snapshot()
    assert snapshot["ctx_tokens"] == 0
    assert snapshot["activeConvs"][0]["tokens"] == 0
    assert snapshot["activeConvs"][0]["turns"] == 0


def test_pseudonym_is_process_secret_scoped_and_never_exposes_the_uuid() -> None:
    first = _registry(secret=b"x" * 32)
    same_secret = _registry(secret=b"x" * 32)
    other_process = _registry(secret=b"y" * 32)
    payload = _payload(_record())

    for registry in (first, same_secret, other_process):
        registry.ingest_otlp_json(payload)

    first_id = first.snapshot()["activeConvs"][0]["id"]
    same_id = same_secret.snapshot()["activeConvs"][0]["id"]
    other_id = other_process.snapshot()["activeConvs"][0]["id"]
    assert re.fullmatch(r"codex-[0-9a-f]{32}", first_id)
    assert first_id == same_id
    assert first_id != other_id
    assert FAKE_UUID not in json.dumps(first.snapshot())


def test_activity_expires_at_the_exact_monotonic_ttl_boundary() -> None:
    fake = FakeTime()
    registry = _registry(fake)
    registry.ingest_otlp_json(_payload(_record()))

    fake.advance(ACTIVITY_TTL_SECONDS - 0.001)
    assert registry.snapshot()["active_convs"] == 1

    fake.advance(0.001)
    assert registry.snapshot() == {
        "active_convs": 0,
        "ctx_tokens": 0,
        "activeConvs": [],
        "clients": [],
    }


def test_unknown_events_do_not_refresh_activity() -> None:
    fake = FakeTime()
    registry = _registry(fake)
    registry.ingest_otlp_json(_payload(_record()))
    fake.advance(ACTIVITY_TTL_SECONDS - 1)

    registry.ingest_otlp_json(_payload(_record(name="codex.future_event", timestamp=2)))
    fake.advance(1)

    assert registry.snapshot()["active_convs"] == 0


def test_newer_lower_token_snapshot_models_context_compaction() -> None:
    registry = _registry()
    registry.ingest_otlp_json(_payload(_completion(900, 10)))

    registry.ingest_otlp_json(_payload(_completion(400, 20)))

    assert registry.snapshot()["ctx_tokens"] == 400


def test_old_after_new_and_equal_timestamp_never_regress_or_replace() -> None:
    registry = _registry()
    registry.ingest_otlp_json(_payload(_completion(500, 20)))

    registry.ingest_otlp_json(_payload(_completion(900, 10), _completion(700, 20)))

    assert registry.snapshot()["ctx_tokens"] == 500


def test_timestamp_less_tokens_update_only_until_an_ordered_snapshot_exists() -> None:
    registry = _registry()
    registry.ingest_otlp_json(_payload(_completion(100, None), _completion(200, None)))
    assert registry.snapshot()["ctx_tokens"] == 200

    registry.ingest_otlp_json(_payload(_completion(300, 10), _completion(400, None)))

    assert registry.snapshot()["ctx_tokens"] == 300


def test_exact_timestamped_prompt_retry_is_a_noop_and_does_not_refresh_ttl() -> None:
    fake = FakeTime()
    registry = _registry(fake)
    payload = _payload(_record(timestamp=10))
    registry.ingest_otlp_json(payload)
    fake.advance(ACTIVITY_TTL_SECONDS - 1)

    registry.ingest_otlp_json(payload)
    assert registry.snapshot()["activeConvs"][0]["turns"] == 1
    fake.advance(1)

    assert registry.snapshot()["active_convs"] == 0


def test_fingerprint_expires_at_the_exact_monotonic_ttl_boundary() -> None:
    fake = FakeTime()
    registry = _registry(fake)
    prompt = _record(timestamp=10)
    registry.ingest_otlp_json(_payload(prompt))
    fake.advance(FINGERPRINT_TTL_SECONDS - 1)
    registry.ingest_otlp_json(_payload(_record(name="codex.conversation_starts", timestamp=20)))

    registry.ingest_otlp_json(_payload(prompt))
    assert registry.snapshot()["activeConvs"][0]["turns"] == 1

    fake.advance(1)
    registry.ingest_otlp_json(_payload(prompt))

    assert registry.snapshot()["activeConvs"][0]["turns"] == 2


def test_timestamp_less_prompts_are_not_deduplicated() -> None:
    registry = _registry()
    prompt = _record(timestamp=None)

    registry.ingest_otlp_json(_payload(prompt, prompt))

    assert registry.snapshot()["activeConvs"][0]["turns"] == 2


def test_equal_valued_prompts_with_distinct_timestamps_remain_distinct() -> None:
    registry = _registry()

    registry.ingest_otlp_json(_payload(_record(timestamp=10), _record(timestamp=11)))

    assert registry.snapshot()["activeConvs"][0]["turns"] == 2


def test_full_batch_validation_is_atomic() -> None:
    registry = _registry()
    invalid = _record(timestamp=2)
    invalid["attributes"].append(_attr("event.name", {"stringValue": "codex.user_prompt"}))

    with pytest.raises(CodexTelemetryMalformedError):
        registry.ingest_otlp_json(_payload(_record(timestamp=1), invalid))

    assert registry.snapshot()["active_convs"] == 0


def test_lru_capacity_evicts_the_least_recent_and_reappearance_resets_state() -> None:
    fake = FakeTime()
    registry = _registry(fake)
    conversation_ids = [f"00000000-0000-4000-8000-{index:012d}" for index in range(65)]
    for index, conversation_id in enumerate(conversation_ids):
        registry.ingest_otlp_json(_payload(_record(conversation_id, timestamp=index + 1)))
        fake.advance(1)

    snapshot = registry.snapshot()
    first_id = _registry(secret=b"x" * 32)
    first_id.ingest_otlp_json(_payload(_record(conversation_ids[0], timestamp=1000)))
    evicted_pseudonym = first_id.snapshot()["activeConvs"][0]["id"]
    assert snapshot["active_convs"] == MAX_ACTIVE_CONVERSATIONS
    assert evicted_pseudonym not in {item["id"] for item in snapshot["activeConvs"]}

    registry.ingest_otlp_json(_payload(_record(conversation_ids[0], timestamp=1001)))

    reappeared = registry.snapshot()["activeConvs"][0]
    assert reappeared["id"] == evicted_pseudonym
    assert reappeared["started"] == "08:06"
    assert reappeared["turns"] == 1


def test_snapshot_is_ordered_by_most_recent_server_receipt() -> None:
    fake = FakeTime()
    registry = _registry(fake)
    first = "00000000-0000-4000-8000-000000000001"
    second = "00000000-0000-4000-8000-000000000002"
    registry.ingest_otlp_json(_payload(_record(first, timestamp=1)))
    fake.advance(1)
    registry.ingest_otlp_json(_payload(_record(second, timestamp=2)))
    fake.advance(1)
    registry.ingest_otlp_json(_payload(_record(first, timestamp=3)))

    items = registry.snapshot()["activeConvs"]
    expected = _registry(secret=b"x" * 32)
    expected.ingest_otlp_json(_payload(_record(first, timestamp=999)))

    assert items[0]["id"] == expected.snapshot()["activeConvs"][0]["id"]


def test_fingerprint_store_is_bounded_and_evicts_oldest_replay_key() -> None:
    registry = _registry()
    records = [_record(timestamp=index + 1) for index in range(MAX_FINGERPRINTS + 1)]
    for offset in range(0, len(records), 200):
        registry.ingest_otlp_json(_payload(*records[offset : offset + 200]))
    assert registry.snapshot()["activeConvs"][0]["turns"] == MAX_FINGERPRINTS + 1

    registry.ingest_otlp_json(_payload(records[-1]))
    assert registry.snapshot()["activeConvs"][0]["turns"] == MAX_FINGERPRINTS + 1

    registry.ingest_otlp_json(_payload(records[0]))

    assert registry.snapshot()["activeConvs"][0]["turns"] == MAX_FINGERPRINTS + 2


def test_constructor_requires_a_32_byte_process_secret() -> None:
    with pytest.raises(ValueError, match="^secret must contain exactly 32 bytes$"):
        CodexConversationRegistry(secret=b"short")

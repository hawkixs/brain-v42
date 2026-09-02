"""Unit tests for scripts.domain_backfill (proposer-only NVIDIA classifier).

Network forbidden: mocked NVIDIA client (httpx.MockTransport), stubbed
GraphService. The PG tests follow the test_promote_prepare.py convention
(require_test_db_url + skip if the database is absent).
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from scripts import domain_backfill as db
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from tests.conftest import require_test_db_url

# ── Task 1 : env file / labels / cartes ──────────────────────────────


def test_load_env_file_parses_systemd_style(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KEY=VALUE literal after the first '=', comments ignored.

    NEUTRAL keys (X_BACKFILL_TEST_*): load_env_file does a setdefault into
    os.environ — using the real BRAIN_NVIDIA_* keys here would pollute the whole
    pytest session and falsify test_main_missing_api_key_exits_2.
    """
    monkeypatch.delenv("X_BACKFILL_TEST_KEY", raising=False)
    monkeypatch.delenv("X_BACKFILL_TEST_MODEL", raising=False)
    f = tmp_path / "nvidia.env"
    f.write_text(
        "# comment\n"
        "X_BACKFILL_TEST_KEY=nvapi-abc=def\n"
        "\n"
        "X_BACKFILL_TEST_MODEL=moonshotai/kimi-k2-instruct\n"
    )
    got = db.load_env_file(f)
    assert got["X_BACKFILL_TEST_KEY"] == "nvapi-abc=def"
    assert got["X_BACKFILL_TEST_MODEL"] == "moonshotai/kimi-k2-instruct"
    assert os.environ["X_BACKFILL_TEST_KEY"] == "nvapi-abc=def"
    monkeypatch.delenv("X_BACKFILL_TEST_KEY", raising=False)
    monkeypatch.delenv("X_BACKFILL_TEST_MODEL", raising=False)


def test_load_env_file_missing_returns_empty(tmp_path: Path) -> None:
    assert db.load_env_file(tmp_path / "absent.env") == {}


def test_load_env_file_does_not_override_existing_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """os.environ wins over the file (CI/test injection)."""
    monkeypatch.setenv("X_BACKFILL_TEST_KEY", "from-environ")
    f = tmp_path / "nvidia.env"
    f.write_text("X_BACKFILL_TEST_KEY=from-file\n")
    db.load_env_file(f)
    assert os.environ["X_BACKFILL_TEST_KEY"] == "from-environ"


def test_entity_type_from_labels() -> None:
    assert db.entity_type_from_labels(["Learning"]) == "learning"
    assert db.entity_type_from_labels(["Entity", "ADR"]) == "adr"
    assert db.entity_type_from_labels(["Domain"]) is None
    assert db.entity_type_from_labels([]) is None


class _StubGraph:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.seen_limit: int | None = None

    async def find_orphans_for_classification(self, limit: int = 20) -> list[dict]:
        self.seen_limit = limit
        return self._rows[:limit]


@pytest.mark.asyncio
async def test_fetch_orphans_passes_limit_and_normalizes() -> None:
    rows = [
        {"id": "11111111-1111-1111-1111-111111111111", "labels": ["Learning"]},
        {"id": "22222222-2222-2222-2222-222222222222", "labels": ["Domain"]},  # dropped
    ]
    stub = _StubGraph(rows)
    got = await db.fetch_orphans(stub, limit=10)
    assert stub.seen_limit == 10
    assert got == [{"id": "11111111-1111-1111-1111-111111111111", "entity_type": "learning"}]


# ── PG-backed : cartes ───────────────────────────────────────────────


@pytest_asyncio.fixture(scope="module")
async def _engine() -> AsyncEngine:  # type: ignore[misc]
    eng = create_async_engine(require_test_db_url(), poolclass=NullPool, echo=False)
    try:
        async with eng.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL not reachable: {exc}")
    yield eng  # type: ignore[misc]
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.mark.asyncio
async def test_fetch_entity_cards_learning_and_truncation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    lid = uuid.uuid4()
    long_insight = "x" * 1000
    async with session_factory() as s:
        await s.execute(
            sa.text(
                "INSERT INTO learnings (id, topic, insight, project_key, tags)"
                " VALUES (:id, :topic, :insight, :pk, :tags)"
            ),
            {
                "id": str(lid),
                "topic": "T backfill",
                "insight": long_insight,
                "pk": "test-backfill",
                "tags": ["a", "b"],
            },
        )
        await s.commit()
    try:
        cards = await db.fetch_entity_cards(
            session_factory, [{"id": str(lid), "entity_type": "learning"}]
        )
        assert len(cards) == 1
        c = cards[0]
        assert c.entity_id == str(lid)
        assert c.entity_type == "learning"
        assert c.title == "T backfill"
        assert len(c.snippet) == db.SNIPPET_MAX_CHARS
        assert c.project_key == "test-backfill"
        assert c.tags == ["a", "b"]
    finally:
        async with session_factory() as s:
            await s.execute(sa.text("DELETE FROM learnings WHERE id = :id"), {"id": str(lid)})
            await s.commit()


@pytest.mark.asyncio
async def test_fetch_entity_cards_skips_ids_absent_from_pg(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A graph id with no PG row (drift) is ignored without crashing."""
    cards = await db.fetch_entity_cards(
        session_factory, [{"id": str(uuid.uuid4()), "entity_type": "decision"}]
    )
    assert cards == []


@pytest.mark.asyncio
async def test_fetch_entity_cards_skips_non_uuid_graph_ids(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Real graph pollution: a Decision node with id="None" (a leaked str(None),
    found on the first --limit 50 run of 2026-07-03) → skip, no crash. The report's
    orphans_seen vs cards_classified gap makes the drift visible."""
    cards = await db.fetch_entity_cards(
        session_factory,
        [
            {"id": "None", "entity_type": "decision"},
            {"id": "pas-un-uuid", "entity_type": "learning"},
        ],
    )
    assert cards == []


# ── Task 2 : prompt + parse/validate ─────────────────────────────────


def _card(i: int, etype: str = "learning") -> db.EntityCard:
    return db.EntityCard(
        entity_id=f"00000000-0000-0000-0000-{i:012d}",
        entity_type=etype,
        title=f"Titre {i}",
        snippet=f"contenu {i}",
        project_key="brain-v42",
        tags=["t1"],
    )


def test_build_messages_contains_domains_rules_and_entities() -> None:
    batch = [_card(1), _card(2)]
    messages = db.build_messages(batch)
    assert messages[0]["role"] == "system"
    user = messages[1]["content"]
    for domain in sorted(db.VALID_DOMAINS):
        assert domain in user
    assert "unknown" in user
    assert "00000000-0000-0000-0000-000000000001" in user
    assert "Titre 2" in user
    assert "JSON" in messages[0]["content"]


def test_parse_and_validate_happy_path_with_fences() -> None:
    batch = [_card(1)]
    content = (
        "```json\n"
        '[{"entity_id": "00000000-0000-0000-0000-000000000001",'
        ' "domain": "Memory", "confidence": "HIGH", "reason": "brain graph"}]\n'
        "```"
    )
    proposals, rejections = db.parse_and_validate(content, batch)
    assert rejections == []
    assert len(proposals) == 1
    p = proposals[0]
    assert p.domain == "memory"  # normalised to lowercase
    assert p.confidence == "high"
    assert p.title == "Titre 1"  # enriched from the card


def _item(entity_id: str, domain: str, confidence: str, reason: str = "r") -> dict:
    return {
        "entity_id": entity_id,
        "domain": domain,
        "confidence": confidence,
        "reason": reason,
    }


def test_parse_and_validate_rejects_bad_domain_id_confidence_and_dups() -> None:
    batch = [_card(1), _card(2)]
    content = json.dumps(
        [
            _item(batch[0].entity_id, "blockchain", "high"),
            _item("99999999-9999-9999-9999-999999999999", "infra", "high"),
            _item(batch[1].entity_id, "infra", "certain"),
            _item(batch[1].entity_id, "infra", "high"),
            _item(batch[1].entity_id, "ops", "low", reason="dup"),
            "not-a-dict",
        ]
    )
    proposals, rejections = db.parse_and_validate(content, batch)
    assert [p.entity_id for p in proposals] == [batch[1].entity_id]
    codes = sorted(r.reason_code for r in rejections)
    assert codes == [
        "duplicate_entity_id",
        "invalid_confidence",
        "invalid_domain",
        "invalid_item",
        "missing_in_response",  # batch[0] has NO accepted proposal
        "unknown_entity_id",
    ]


def test_parse_and_validate_unknown_domain_is_accepted() -> None:
    batch = [_card(1)]
    content = json.dumps(
        [
            _item(batch[0].entity_id, "unknown", "low", reason="ambigu"),
        ]
    )
    proposals, rejections = db.parse_and_validate(content, batch)
    assert proposals[0].domain == "unknown"
    assert rejections == []


def test_parse_and_validate_raises_on_non_json() -> None:
    with pytest.raises(db.ResponseParseError):
        db.parse_and_validate("désolé, voici la classification :", [_card(1)])


def test_parse_and_validate_raises_on_non_array() -> None:
    with pytest.raises(db.ResponseParseError):
        db.parse_and_validate('{"entity_id": "x"}', [_card(1)])


# ── Task 3 : client NVIDIA ───────────────────────────────────────────


def _ok_payload(batch: list[db.EntityCard]) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        [
                            {
                                "entity_id": c.entity_id,
                                "domain": "memory",
                                "confidence": "high",
                                "reason": "r",
                            }
                            for c in batch
                        ]
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://mock.nvidia.local/v1",
        headers={"Authorization": "Bearer nvapi-test"},
    )


async def _no_sleep(_: float) -> None:
    return None


@pytest.mark.asyncio
async def test_classify_batch_happy_path() -> None:
    batch = [_card(1), _card(2)]
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=_ok_payload(batch))

    async with _client(handler) as client:
        outcome = await db.classify_batch(client, "test-model", batch, sleep=_no_sleep)
    assert not outcome.failed
    assert len(outcome.proposals) == 2
    assert outcome.prompt_tokens == 100
    assert calls[0]["model"] == "test-model"
    assert calls[0]["temperature"] == pytest.approx(0.2)
    assert "tools" not in calls[0]  # never any tool-calling


@pytest.mark.asyncio
async def test_classify_batch_retries_on_429_then_succeeds() -> None:
    batch = [_card(1)]
    statuses = iter([429, 200])
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        if status == 429:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json=_ok_payload(batch))

    async def spy_sleep(seconds: float) -> None:
        slept.append(seconds)

    async with _client(handler) as client:
        outcome = await db.classify_batch(client, "m", batch, sleep=spy_sleep)
    assert not outcome.failed
    assert slept == [2.0]


@pytest.mark.asyncio
async def test_classify_batch_fails_after_exhausted_5xx() -> None:
    batch = [_card(1)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    async with _client(handler) as client:
        outcome = await db.classify_batch(client, "m", batch, sleep=_no_sleep)
    assert outcome.failed
    assert outcome.error is not None and "503" in outcome.error
    assert outcome.proposals == []


@pytest.mark.asyncio
async def test_classify_batch_reprompts_once_on_bad_json_then_succeeds() -> None:
    batch = [_card(1)]
    responses = iter(
        [
            {
                "choices": [{"message": {"content": "je pense que c'est memory"}}],
                "usage": {},
            },
            _ok_payload(batch),
        ]
    )
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=next(responses))

    async with _client(handler) as client:
        outcome = await db.classify_batch(client, "m", batch, sleep=_no_sleep)
    assert not outcome.failed
    assert len(bodies) == 2
    # the re-prompt carries the faulty answer + a corrective instruction
    assert bodies[1]["messages"][-2]["role"] == "assistant"
    assert "JSON" in bodies[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_classify_batch_fails_after_two_bad_json() -> None:
    batch = [_card(1)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "toujours pas du JSON"}}],
                "usage": {},
            },
        )

    async with _client(handler) as client:
        outcome = await db.classify_batch(client, "m", batch, sleep=_no_sleep)
    assert outcome.failed
    assert outcome.error is not None and "parse" in outcome.error.lower()


@pytest.mark.asyncio
async def test_classify_batch_401_raises_auth_error() -> None:
    batch = [_card(1)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    async with _client(handler) as client:
        with pytest.raises(db.NvidiaAuthError):
            await db.classify_batch(client, "m", batch, sleep=_no_sleep)


# ── Task 4 : orchestrateur + rapports ────────────────────────────────


@pytest.mark.asyncio
async def test_run_backfill_batches_and_aggregates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cards = [_card(i) for i in range(1, 6)]  # 5 cartes
    orphan_rows = [{"id": c.entity_id, "labels": ["Learning"]} for c in cards]
    stub_graph = _StubGraph(orphan_rows)

    async def fake_fetch_cards(
        _session_factory: object, orphans: list[dict]
    ) -> list[db.EntityCard]:
        wanted = {o["id"] for o in orphans}
        return [c for c in cards if c.entity_id in wanted]

    monkeypatch.setattr(db, "fetch_entity_cards", fake_fetch_cards)

    seen_batches: list[int] = []

    async def fake_classify(batch: list[db.EntityCard]) -> db.BatchOutcome:
        seen_batches.append(len(batch))
        if len(seen_batches) == 2:
            return db.BatchOutcome(proposals=[], rejections=[], failed=True, error="boom")
        return db.BatchOutcome(
            proposals=[
                db.Proposal(
                    entity_id=c.entity_id,
                    entity_type=c.entity_type,
                    title=c.title,
                    project_key=c.project_key,
                    domain="memory",
                    confidence="high",
                    reason="r",
                )
                for c in batch
            ],
            rejections=[],
            prompt_tokens=10,
            completion_tokens=5,
        )

    result = await db.run_backfill(
        stub_graph,
        None,  # type: ignore[arg-type]  # fetch_entity_cards monkeypatched
        fake_classify,
        limit=5,
        batch_size=2,
    )
    assert seen_batches == [2, 2, 1]
    assert result.orphans_seen == 5
    assert result.cards_classified == 5
    assert len(result.proposals) == 3  # batch 2 failed
    assert result.failed_batches == ["boom"]
    assert result.prompt_tokens == 20  # 2 batches ok × 10


def test_write_reports_jsonl_roundtrip_and_md_sections(tmp_path: Path) -> None:
    result = db.BackfillResult(
        proposals=[
            db.Proposal("id-1", "learning", "T1", "brain-v42", "memory", "high", "r1"),
            db.Proposal("id-2", "decision", "T2", None, "unknown", "low", "r2"),
        ],
        rejections=[db.Rejection("id-3", "invalid_domain", "blockchain")],
        failed_batches=["HTTP 503"],
        orphans_seen=4,
        cards_classified=3,
        prompt_tokens=42,
        completion_tokens=17,
    )
    jsonl_path, md_path = db.write_reports(tmp_path, "2026-07-03", "test-model", result)
    lines = [json.loads(line) for line in jsonl_path.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["entity_id"] == "id-1"
    assert lines[0]["run_date"] == "2026-07-03"
    assert lines[0]["model"] == "test-model"
    md = md_path.read_text()
    assert "memory" in md and "unknown" in md
    assert "invalid_domain" in md
    assert "HTTP 503" in md
    assert "42" in md  # tokens visibles


# ── Task 5 : CLI ─────────────────────────────────────────────────────


def test_main_missing_api_key_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("BRAIN_NVIDIA_API_KEY", raising=False)
    rc = db.main(["--env-file", str(tmp_path / "absent.env")])
    assert rc == 2
    assert "BRAIN_NVIDIA_API_KEY" in capsys.readouterr().err


def test_parse_args_defaults() -> None:
    args = db.parse_args([])
    assert args.limit == 30
    assert args.batch_size == 15
    assert args.model is None  # resolved later: env then DEFAULT_MODEL
    assert args.base_url is None
    assert args.env_file == db.DEFAULT_ENV_FILE
    assert args.out_dir == Path("logs/domain_backfill")


def test_parse_args_rejects_non_positive_batch_size_and_limit() -> None:
    """--batch-size 0 crashed _chunks (range step 0) with a raw ValueError;
    argparse must refuse cleanly (usage + exit 2) before reaching the run."""
    with pytest.raises(SystemExit) as exc_batch:
        db.parse_args(["--batch-size", "0"])
    assert exc_batch.value.code == 2
    with pytest.raises(SystemExit) as exc_limit:
        db.parse_args(["--limit", "-1"])
    assert exc_limit.value.code == 2


def test_resolve_model_and_base_url_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_NVIDIA_MODEL", "env-model")
    monkeypatch.setenv("BRAIN_NVIDIA_BASE_URL", "https://env.example/v1")
    assert db.resolve_option(None, "BRAIN_NVIDIA_MODEL", db.DEFAULT_MODEL) == "env-model"
    assert db.resolve_option("cli-model", "BRAIN_NVIDIA_MODEL", db.DEFAULT_MODEL) == "cli-model"
    monkeypatch.delenv("BRAIN_NVIDIA_MODEL")
    assert db.resolve_option(None, "BRAIN_NVIDIA_MODEL", db.DEFAULT_MODEL) == db.DEFAULT_MODEL


def test_main_warns_on_loose_env_file_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An env file at 0644 → a stderr warning (then exit 2: no key in it)."""
    monkeypatch.delenv("BRAIN_NVIDIA_API_KEY", raising=False)
    f = tmp_path / "nvidia.env"
    f.write_text("# vide\n")
    f.chmod(0o644)
    rc = db.main(["--env-file", str(f)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "chmod 600" in err


# ---------------------------------------------------------------------------
# _post_chat — retryable transport timeouts (extract incident of 2026-07-04:
# ReadTimeout at exactly 180 s, NVIDIA queue latency ~100 s on a trivial prompt;
# the timeout escaped the retry loop and str(exc) == "").
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_chat_retries_transport_timeout_then_succeeds() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ReadTimeout("", request=request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "usage": {}},
        )

    slept: list[float] = []

    async def spy_sleep(seconds: float) -> None:
        slept.append(seconds)

    async with _client(handler) as client:
        content, _usage = await db._post_chat(
            client, "test-model", [{"role": "user", "content": "x"}], spy_sleep
        )

    assert content == "ok"
    assert attempts["n"] == 2
    assert slept == [2.0]  # backoff existant 2**attempt


@pytest.mark.asyncio
async def test_post_chat_retries_529_overloaded() -> None:
    """529 = provider overload, transient like 503.

    ROADMAP canary of 2026-08-05: 2 batches out of 8 in 529 on the new primary.
    Non-retryable, this code failed the batch AND opened the circuit — so a single
    529 sent the WHOLE night onto the fallback model, which recreates exactly the
    silent failure we are fixing.
    """
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(529, text="overloaded")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "usage": {}},
        )

    async def spy_sleep(seconds: float) -> None:
        return None

    async with _client(handler) as client:
        content, _usage = await db._post_chat(
            client, "test-model", [{"role": "user", "content": "x"}], spy_sleep
        )

    assert content == "ok"
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_post_chat_timeout_exhaustion_raises_named_error() -> None:
    """Once exhausted, RuntimeError names the exception type — str(ReadTimeout) is
    often empty, the class name is the only reliable information."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ReadTimeout("", request=request)

    async with _client(handler) as client:
        with pytest.raises(RuntimeError, match="ReadTimeout"):
            await db._post_chat(client, "test-model", [{"role": "user", "content": "x"}], _no_sleep)

    assert attempts["n"] == 3  # MAX_HTTP_ATTEMPTS tentatives totales


@pytest.mark.asyncio
async def test_post_chat_max_tokens_default_and_override() -> None:
    """max_tokens is parameterisable (wet finding of 2026-07-04: the brain-v42
    batch truncated at 4096 by the consolidating prompt) — the 4096 default is
    unchanged for the twins."""
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["max_tokens"])
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "usage": {}},
        )

    msgs = [{"role": "user", "content": "x"}]
    async with _client(handler) as client:
        await db._post_chat(client, "test-model", msgs, _no_sleep)
        await db._post_chat(client, "test-model", msgs, _no_sleep, max_tokens=8192)

    assert seen == [4096, 8192]


# ---------------------------------------------------------------------------
# _post_chat — 410/404, the death of a model at the provider.
# Measured on 2026-08-12: `deepseek-ai/deepseek-v4-pro` returns 410 Gone, and the
# night's 20 tickets failed in 0.907 s on a 540 s budget. A 410 is not transient —
# it must neither be retried nor confused with a 5xx.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_chat_raises_model_gone_on_410_without_retrying() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(410, text="Model deepseek-ai/deepseek-v4-pro has been retired")

    slept: list[float] = []

    async def spy_sleep(seconds: float) -> None:
        slept.append(seconds)

    async with _client(handler) as client:
        with pytest.raises(db.ModelGoneError) as excinfo:
            await db._post_chat(
                client, "deepseek-ai/deepseek-v4-pro", [{"role": "user", "content": "x"}], spy_sleep
            )

    assert attempts["n"] == 1, "un 410 retenté est une seconde perte de temps garantie"
    assert slept == [], "aucun backoff : rien n'attend un modèle supprimé"
    assert "deepseek-ai/deepseek-v4-pro" in str(excinfo.value), "le modèle mort doit être NOMMÉ"


@pytest.mark.asyncio
async def test_post_chat_raises_model_gone_on_404() -> None:
    """404 = a model unknown to the provider. Same conclusion as a 410: the
    configured name designates nothing, and no repetition will make it exist."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="model not found")

    async with _client(handler) as client:
        with pytest.raises(db.ModelGoneError):
            await db._post_chat(
                client, "vendor/typo-in-the-name", [{"role": "user", "content": "x"}], _no_sleep
            )


@pytest.mark.asyncio
async def test_a_retryable_status_is_not_mistaken_for_a_dead_model() -> None:
    """The guard that matters: 503 stays transient. Confusing the two would have a
    perfectly alive model replaced over a passing overload."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

    async with _client(handler) as client:
        content, _usage = await db._post_chat(
            client, "meta/llama-3.3-70b-instruct", [{"role": "user", "content": "x"}], _no_sleep
        )

    assert content == "ok"
    assert attempts["n"] == 3

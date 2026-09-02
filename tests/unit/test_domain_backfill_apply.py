"""Unit tests for scripts.domain_backfill_apply (step C — gated writer).

No network, no real Neo4j: the graph writer is a stub that records the calls. The
input jsonl report comes from scripts.domain_backfill.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from scripts import domain_backfill_apply as ap

from brain_v42.services.durable_graph_service import DurableGraphService

_UUID1 = "11111111-1111-1111-1111-111111111111"
_UUID2 = "22222222-2222-2222-2222-222222222222"
_UUID3 = "33333333-3333-3333-3333-333333333333"


def _proposal(
    entity_id: str = _UUID1,
    domain: str = "infra",
    confidence: str = "high",
) -> dict:
    return {
        "run_date": "2026-07-03",
        "model": "test-model",
        "entity_id": entity_id,
        "entity_type": "learning",
        "title": "T",
        "project_key": "brain-v42",
        "domain": domain,
        "confidence": confidence,
        "reason": "r",
    }


def _write_report(path: Path, proposals: list[dict]) -> Path:
    path.write_text("".join(json.dumps(p) + "\n" for p in proposals))
    return path


class _StubGraphWriter:
    """Records the calls; outcomes programmable per domain/entity."""

    def __init__(
        self,
        upsert_result: str = "ok",
        link_result: str = "created",
    ) -> None:
        self.upsert_result = upsert_result
        self.link_result = link_result
        self.upserted: list[str] = []
        self.linked: list[tuple[str, str]] = []

    async def upsert_domain(self, name: str) -> str:
        self.upserted.append(name)
        return self.upsert_result

    async def link_entity_to_domain(self, entity_id: uuid.UUID, domain_name: str) -> str:
        self.linked.append((str(entity_id), domain_name))
        return self.link_result


# ── load_proposals ───────────────────────────────────────────────────


def test_load_proposals_roundtrip(tmp_path: Path) -> None:
    report = _write_report(tmp_path / "r.jsonl", [_proposal(), _proposal(_UUID2)])
    got = ap.load_proposals(report)
    assert [p["entity_id"] for p in got] == [_UUID1, _UUID2]


def test_load_proposals_malformed_line_raises_with_line_number(tmp_path: Path) -> None:
    f = tmp_path / "bad.jsonl"
    f.write_text(json.dumps(_proposal()) + "\npas du json\n")
    with pytest.raises(ValueError, match="ligne 2"):
        ap.load_proposals(f)


def test_load_proposals_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ap.load_proposals(tmp_path / "absent.jsonl")


# ── filter_appliable ─────────────────────────────────────────────────


def test_filter_appliable_skips_unknown_low_bad_domain_and_bad_id() -> None:
    proposals = [
        _proposal(),  # kept
        _proposal(_UUID2, domain="unknown"),  # skipped_unknown
        _proposal(_UUID3, confidence="low"),  # skipped_confidence
        _proposal("44444444-4444-4444-4444-444444444444", domain="blockchain"),
        _proposal("pas-un-uuid"),  # skipped_bad_id
    ]
    kept, skipped = ap.filter_appliable(proposals, min_confidence="high")
    assert [p["entity_id"] for p in kept] == [_UUID1]
    codes = sorted(s.outcome for s in skipped)
    assert codes == [
        "skipped_bad_id",
        "skipped_confidence",
        "skipped_invalid_domain",
        "skipped_unknown",
    ]


def test_filter_appliable_min_confidence_medium_keeps_medium() -> None:
    proposals = [
        _proposal(confidence="medium"),
        _proposal(_UUID2, confidence="low"),
    ]
    kept, skipped = ap.filter_appliable(proposals, min_confidence="medium")
    assert [p["entity_id"] for p in kept] == [_UUID1]
    assert [s.outcome for s in skipped] == ["skipped_confidence"]


# ── apply_proposals ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_dry_run_makes_zero_graph_calls() -> None:
    graph = _StubGraphWriter()
    outcomes = await ap.apply_proposals(graph, [_proposal(), _proposal(_UUID2)], wet=False)
    assert graph.upserted == []
    assert graph.linked == []
    assert [o.outcome for o in outcomes] == ["dry_run", "dry_run"]


@pytest.mark.asyncio
async def test_apply_wet_upserts_each_domain_once_and_links_each_row() -> None:
    graph = _StubGraphWriter()
    proposals = [
        _proposal(),  # infra
        _proposal(_UUID2),  # infra (same domain)
        _proposal(_UUID3, domain="ops"),  # ops
    ]
    outcomes = await ap.apply_proposals(graph, proposals, wet=True)
    assert sorted(graph.upserted) == ["infra", "ops"]  # once per domain
    assert graph.linked == [(_UUID1, "infra"), (_UUID2, "infra"), (_UUID3, "ops")]
    assert [o.outcome for o in outcomes] == ["created", "created", "created"]


@pytest.mark.asyncio
async def test_apply_wet_upsert_failure_skips_rows_of_that_domain() -> None:
    graph = _StubGraphWriter(upsert_result="error")
    outcomes = await ap.apply_proposals(graph, [_proposal()], wet=True)
    assert graph.linked == []
    assert outcomes[0].outcome == "domain_upsert_error"


@pytest.mark.asyncio
async def test_apply_wet_propagates_link_outcomes() -> None:
    graph = _StubGraphWriter(link_result="matched")
    outcomes = await ap.apply_proposals(graph, [_proposal()], wet=True)
    assert [o.outcome for o in outcomes] == ["matched"]


# ── rapport + CLI ────────────────────────────────────────────────────


def test_write_apply_report_jsonl(tmp_path: Path) -> None:
    outcomes = [
        ap.ApplyOutcome(entity_id=_UUID1, domain="infra", outcome="created"),
        ap.ApplyOutcome(entity_id=_UUID2, domain="unknown", outcome="skipped_unknown"),
    ]
    path = ap.write_apply_report(tmp_path / "r.jsonl", outcomes, wet=True)
    assert path.name == "r-apply.jsonl"
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines[0]["outcome"] == "created"
    assert lines[0]["wet"] is True
    assert lines[1]["outcome"] == "skipped_unknown"


def test_parse_args_defaults_and_wet_flag(tmp_path: Path) -> None:
    args = ap.parse_args(["--report", str(tmp_path / "r.jsonl")])
    assert args.report == tmp_path / "r.jsonl"
    assert args.min_confidence == "high"
    assert args.wet is False
    args_wet = ap.parse_args(["--report", str(tmp_path / "r.jsonl"), "--wet"])
    assert args_wet.wet is True


def test_parse_args_rejects_bad_min_confidence(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        ap.parse_args(["--report", str(tmp_path / "r.jsonl"), "--min-confidence", "sure"])
    assert exc.value.code == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger_enabled", [False, True])
async def test_run_selects_graph_writer_from_ledger_flag(
    tmp_path: Path,
    ledger_enabled: bool,
) -> None:
    events: list[str] = []

    async def assert_schema_ready() -> None:
        events.append("schema_ready")

    async def apply_writes(
        _writer: object,
        _proposals: list[dict],
        *,
        wet: bool,
    ) -> list[ap.ApplyOutcome]:
        assert wet is True
        events.append("apply_write")
        return [ap.ApplyOutcome(_UUID1, "infra", "created")]

    report = _write_report(tmp_path / "r.jsonl", [_proposal()])
    settings = SimpleNamespace(
        neo4j_url="bolt://neo4j",
        neo4j_user="neo4j",
        neo4j_password="secret",
        neo4j_timeout=4.0,
        graph_ledger_write_enabled=ledger_enabled,
        graph_outbox_interval_seconds=5,
        graph_outbox_batch_size=100,
        graph_outbox_max_attempts=10,
    )
    driver = MagicMock()
    driver.close = AsyncMock()
    graph = MagicMock()
    ledger = SimpleNamespace(assert_schema_ready=AsyncMock(side_effect=assert_schema_ready))
    durable_service = DurableGraphService(graph, ledger)
    durable_stack = SimpleNamespace(
        service=durable_service if ledger_enabled else graph,
        ledger=ledger if ledger_enabled else None,
    )
    apply = AsyncMock(side_effect=apply_writes)

    with (
        patch("brain_v42.config.Settings", return_value=settings),
        patch("brain_v42.db.engine.get_session_factory") as session_factory,
        patch("neo4j.AsyncGraphDatabase.driver", return_value=driver),
        patch("brain_v42.services.graph_service.GraphService", return_value=graph),
        patch(
            "brain_v42.services.durable_graph_service.build_durable_graph_stack",
            return_value=durable_stack,
        ),
        patch.object(ap, "apply_proposals", apply),
    ):
        exit_code = await ap._run(SimpleNamespace(report=report, min_confidence="high", wet=True))

    writer = apply.await_args.args[0]
    if ledger_enabled:
        assert isinstance(writer, DurableGraphService)
        assert writer._graph is graph
        session_factory.assert_called_once_with()
        ledger.assert_schema_ready.assert_awaited_once_with()
        assert events == ["schema_ready", "apply_write"]
    else:
        assert writer is graph
        session_factory.assert_not_called()
        ledger.assert_schema_ready.assert_not_awaited()
        assert events == ["apply_write"]
    assert exit_code == 0

"""Unit tests for nightly-ops metrics collection (section `nightly` du sidecar).

Consommé par red-monitor (ticket de1ad785) : le panel nightly-ops du
dashboard doit répliquer le check matinal — killswitches, proposals
roadmap en attente de review (dont les merges retenus par le juge depuis
39fc6a9), extract en attente, dernier échec dream.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.collector_nightly import parse_killswitches

_DROPIN = """\
[Service]
Environment=BRAIN_DREAM_PROMOTE_ENABLED=true
Environment=BRAIN_DREAM_REORG_ENABLED=true
Environment=BRAIN_DREAM_REORG_DRY_RUN=true
Environment=BRAIN_DREAM_EXTRACT_ENABLED=true
Environment=BRAIN_DREAM_EXTRACT_DRY_RUN=true
Environment=BRAIN_DREAM_ROADMAP_ENABLED=true
Environment=BRAIN_DREAM_ROADMAP_DRY_RUN=false
"""


class TestParseKillswitches:
    def test_legacy_module_reexports_the_root_parser_identity(self) -> None:
        from brain_v42.dream_killswitches import parse_killswitches as canonical_parser

        assert parse_killswitches is canonical_parser

    def test_full_dropin(self) -> None:
        ks = parse_killswitches(_DROPIN)
        assert ks == {
            "promote": True,
            "reorg": True,
            "reorg_dry": True,
            "extract": True,
            "extract_dry": True,
            "roadmap": True,
            "roadmap_dry": False,
        }

    def test_multiple_pairs_on_single_environment_line(self) -> None:
        ks = parse_killswitches(
            "Environment=BRAIN_DREAM_ROADMAP_ENABLED=true BRAIN_DREAM_ROADMAP_DRY_RUN=false\n"
        )
        assert ks == {"roadmap": True, "roadmap_dry": False}

    def test_sweep_keys_map_to_their_short_flags(self) -> None:
        ks = parse_killswitches(
            "[Service]\n"
            "Environment=BRAIN_DREAM_SWEEP_ENABLED=true\n"
            "Environment=BRAIN_DREAM_SWEEP_DRY_RUN=false\n"
        )
        assert ks == {"sweep": True, "sweep_dry": False}

    def test_ignores_unknown_keys_and_garbage(self) -> None:
        ks = parse_killswitches(
            "# commentaire\n"
            "[Service]\n"
            "Environment=SOMETHING_ELSE=true\n"
            "Environment=BRAIN_DREAM_PROMOTE_ENABLED=false\n"
            "pas une ligne env\n"
        )
        assert ks == {"promote": False}


def _make_collector(
    side_effects: list,
) -> MetricsCollector:
    """MetricsCollector avec session factory mockée (idiome test_dream_metrics)."""
    collector = MetricsCollector.__new__(MetricsCollector)
    collector._session_factory = MagicMock()
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=side_effects)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    collector._session_factory.return_value = mock_session
    return collector


def _all_result(rows: list) -> MagicMock:
    r = MagicMock()
    r.all.return_value = rows
    return r


def _scalar_result(value: int) -> MagicMock:
    r = MagicMock()
    r.scalar_one.return_value = value
    return r


def _first_result(row) -> MagicMock:
    r = MagicMock()
    r.first.return_value = row
    return r


def _db_side_effects(
    status_rows: list | None = None,
    applied_24h: int = 24,
    extract_pending: int = 9,
    failure_row=None,
) -> list:
    """Ordre d'exécution : statuts roadmap → applied 24h → extract → last failure."""
    return [
        _all_result(
            status_rows if status_rows is not None else [("proposed", 26), ("applied", 80)]
        ),
        _scalar_result(applied_24h),
        _scalar_result(extract_pending),
        _first_result(failure_row),
    ]


class TestCollectNightlyOps:
    @pytest.mark.asyncio
    async def test_full_payload(self, tmp_path) -> None:
        ks_file = tmp_path / "killswitches.conf"
        ks_file.write_text(_DROPIN)
        failure = (
            date(2026, 7, 5),
            "roadmap",
            "unparseable after corrective re-prompt: …",
            datetime(2026, 7, 4, 22, 13, 30, tzinfo=UTC),
        )
        collector = _make_collector(_db_side_effects(failure_row=failure))

        result = await collector.collect_nightly_ops(killswitches_path=ks_file)

        assert result["killswitches"]["roadmap"] is True
        assert result["killswitches"]["roadmap_dry"] is False
        assert result["roadmap"] == {
            "proposed_pending": 26,
            "applied_total": 80,
            "applied_24h": 24,
            "rejected_total": 0,
        }
        assert result["extract"] == {"proposed_pending": 9}
        assert result["last_failure"] == {
            "run_date": "2026-07-05",
            "phase": "roadmap",
            "error": "unparseable after corrective re-prompt: …",
            "created_at": "2026-07-04T22:13:30+00:00",
        }

    @pytest.mark.asyncio
    async def test_no_failure_row_yields_none(self, tmp_path) -> None:
        ks_file = tmp_path / "killswitches.conf"
        ks_file.write_text(_DROPIN)
        collector = _make_collector(_db_side_effects(failure_row=None))

        result = await collector.collect_nightly_ops(killswitches_path=ks_file)

        assert result["last_failure"] is None

    @pytest.mark.asyncio
    async def test_missing_killswitch_file_degrades_to_none(self, tmp_path) -> None:
        """Fichier absent/illisible → killswitches=None, le reste vit sa vie."""
        collector = _make_collector(_db_side_effects())

        result = await collector.collect_nightly_ops(killswitches_path=tmp_path / "absent.conf")

        assert result["killswitches"] is None
        assert result["roadmap"]["proposed_pending"] == 26

    @pytest.mark.asyncio
    async def test_db_error_degrades_to_killswitches_only(self, tmp_path) -> None:
        """Le sidecar ne crashe JAMAIS (pattern collector_dream) : DB en
        échec → seules les killswitches restent."""
        ks_file = tmp_path / "killswitches.conf"
        ks_file.write_text(_DROPIN)
        collector = _make_collector([RuntimeError("db down")])

        result = await collector.collect_nightly_ops(killswitches_path=ks_file)

        assert result["killswitches"]["promote"] is True
        assert "roadmap" not in result
        assert "extract" not in result
        assert "last_failure" not in result

    @pytest.mark.asyncio
    async def test_everything_down_returns_empty(self, tmp_path) -> None:
        """Ni fichier ni DB → {} : le server omet la section nightly."""
        collector = _make_collector([RuntimeError("db down")])

        result = await collector.collect_nightly_ops(killswitches_path=tmp_path / "absent.conf")

        assert result == {}

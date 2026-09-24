"""Unit tests for nightly-ops metrics collection (the sidecar's `nightly` section).

Consumed by red-monitor (ticket de1ad785): the dashboard's nightly-ops panel must
replicate the morning check — killswitches, pending extract (scoped to this
sidecar's own project, ticket 69949ffc), last dream failure (pool-wide, but
with a `project_key` attribution so a reader never mistakes another pool
project's failure for this one's).

The unscoped `roadmap` block (`proposed_pending`/`applied_24h`/`applied_total`/
`rejected_total`) is REMOVED here (ticket 69949ffc thread, 2026-09-23): red-monitor
no longer reads it, and `roadmap_curation_proposals` has no project column to
scope it by anyway. This does not touch the Dream `roadmap` phase, its curation
tables, or the graph `roadmap` group (decision 11dbb4a1 governs those
separately).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.collector_nightly import parse_killswitches
from brain_v42.metrics.slow_block_cache import CollectorDegraded, SlowBlockCache

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
    """MetricsCollector with a mocked session factory (the test_dream_metrics idiom)."""
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
    extract_pending: int = 9,
    failure_row=None,
) -> list:
    """Execution order: extract pending → last failure."""
    return [
        _scalar_result(extract_pending),
        _first_result(failure_row),
    ]


class TestCollectNightlyOps:
    @pytest.mark.asyncio
    async def test_full_payload(self, tmp_path) -> None:
        ks_file = tmp_path / "killswitches.conf"
        ks_file.write_text(_DROPIN)
        # Attributed to ANOTHER pool project on purpose: the payload is
        # pool-wide, and the point of the `project_key` field is precisely to
        # let a reader tell this apart from a brain-v42 failure (ticket
        # 69949ffc).
        failure = (
            "watchk-claude",
            date(2026, 7, 5),
            "reorg",
            "Authentication required (accounts.google.com oauth)",
            datetime(2026, 7, 4, 22, 13, 30, tzinfo=UTC),
        )
        collector = _make_collector(_db_side_effects(failure_row=failure))

        result = await collector.collect_nightly_ops(killswitches_path=ks_file)

        assert result["killswitches"]["roadmap"] is True
        assert result["killswitches"]["roadmap_dry"] is False
        assert "roadmap" not in result
        assert result["extract"] == {"proposed_pending": 9}
        assert result["last_failure"] == {
            "project_key": "watchk-claude",
            "run_date": "2026-07-05",
            "phase": "reorg",
            "error": "Authentication required (accounts.google.com oauth)",
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
    async def test_last_failure_query_excludes_done_and_blank_project(self, tmp_path) -> None:
        """Pins the pool-wide rule (ticket 69949ffc, decision 1669d429 item 4):
        fail/timeout only, 7-day window, NULL/blank project_key excluded — the
        second writer's contamination (ticket 7336a2d5) must never surface."""
        ks_file = tmp_path / "killswitches.conf"
        ks_file.write_text(_DROPIN)
        collector = _make_collector(_db_side_effects(failure_row=None))

        await collector.collect_nightly_ops(killswitches_path=ks_file)

        failure_call = collector._session_factory.return_value.execute.call_args_list[1]
        query = str(failure_call.args[0])
        assert "'fail'" in query
        assert "'timeout'" in query
        assert "'done'" not in query
        assert "project_key IS NOT NULL" in query
        assert "project_key <> ''" in query

    @pytest.mark.asyncio
    async def test_extract_pending_scoped_to_the_sidecars_own_project(self, tmp_path) -> None:
        """`ticket_extraction_proposals` carries `target_project` (unlike
        `roadmap_curation_proposals`, which has no project column at all — the
        reason its counters are removed rather than scoped): this panel is
        brain-v42's own page, so the count is scoped to it (ticket 69949ffc)."""
        ks_file = tmp_path / "killswitches.conf"
        ks_file.write_text(_DROPIN)
        collector = _make_collector(_db_side_effects(extract_pending=3, failure_row=None))

        result = await collector.collect_nightly_ops(killswitches_path=ks_file)

        assert result["extract"] == {"proposed_pending": 3}
        extract_call = collector._session_factory.return_value.execute.call_args_list[0]
        query = str(extract_call.args[0])
        params = extract_call.args[1]
        assert "target_project" in query
        assert params == {"project_key": "brain-v42"}

    @pytest.mark.asyncio
    async def test_missing_killswitch_file_degrades_to_none(self, tmp_path) -> None:
        """File absent/unreadable → killswitches=None, the rest carries on."""
        collector = _make_collector(_db_side_effects())

        result = await collector.collect_nightly_ops(killswitches_path=tmp_path / "absent.conf")

        assert result["killswitches"] is None
        assert result["extract"] == {"proposed_pending": 9}

    @pytest.mark.asyncio
    async def test_db_error_degrades_to_killswitches_only(self, tmp_path) -> None:
        """The sidecar NEVER crashes (the collector_dream pattern): DB failing →
        only the killswitches remain, signalled via CollectorDegraded so
        SlowBlockCache retries under the short error_ttl_seconds (MAJOR review
        finding, PR #201) instead of the full TTL, while the payload it publishes
        is unchanged."""
        ks_file = tmp_path / "killswitches.conf"
        ks_file.write_text(_DROPIN)
        collector = _make_collector([RuntimeError("db down")])

        with pytest.raises(CollectorDegraded) as excinfo:
            await collector.collect_nightly_ops(killswitches_path=ks_file)

        result = excinfo.value.payload
        assert result["killswitches"]["promote"] is True
        assert "roadmap" not in result
        assert "extract" not in result
        assert "last_failure" not in result

    @pytest.mark.asyncio
    async def test_everything_down_raises_collector_degraded_with_empty_payload(
        self, tmp_path
    ) -> None:
        """Neither file nor DB → CollectorDegraded({}): the server still omits
        the nightly section, but the cache now retries it soon instead of for
        the full TTL."""
        collector = _make_collector([RuntimeError("db down")])

        with pytest.raises(CollectorDegraded) as excinfo:
            await collector.collect_nightly_ops(killswitches_path=tmp_path / "absent.conf")

        assert excinfo.value.payload == {}

    @pytest.mark.asyncio
    async def test_transient_sql_failure_recovers_after_the_short_error_ttl(self, tmp_path) -> None:
        """Reviewer's reproduction (PR #201, MAJOR): a failure injected at
        `session.execute` (not a mocked collector that raises) must be retried
        by SlowBlockCache after `error_ttl_seconds`, never held for the full
        `ttl_seconds`. Wires the REAL collect_nightly_ops through a REAL
        SlowBlockCache with an injectable clock."""
        ks_file = tmp_path / "killswitches.conf"
        ks_file.write_text(_DROPIN)
        collector = _make_collector(
            [RuntimeError("db down"), *_db_side_effects(extract_pending=3, failure_row=None)]
        )

        time_box = [0.0]
        cache = SlowBlockCache(
            ttl_seconds=30.0,
            error_ttl_seconds=5.0,
            clock=lambda: time_box[0],
            wall_clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )

        def compute():
            return collector.collect_nightly_ops(killswitches_path=ks_file)

        first = await cache.get("nightly", compute)
        assert first is not None and first["killswitches"]["promote"] is True
        assert "extract" not in first
        assert collector._session_factory.return_value.execute.call_count == 1

        # Still inside the error TTL: no retry yet.
        time_box[0] = 4.9
        still_degraded = await cache.get("nightly", compute)
        assert still_degraded == first
        assert collector._session_factory.return_value.execute.call_count == 1

        # Past the error TTL (5s, not the 30s success TTL): recomputes and recovers.
        time_box[0] = 5.1
        recovered = await cache.get("nightly", compute)
        assert recovered is not None
        assert recovered["extract"] == {"proposed_pending": 3}
        assert collector._session_factory.return_value.execute.call_count == 3

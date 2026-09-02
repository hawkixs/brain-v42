"""process_metrics' READ window must equal its RETENTION window.

Measured on 2026-08-10 (ticket d2a669c6). ``collect_process_metrics`` filtered on
``updated_at > NOW() - INTERVAL '60 seconds'`` while the purge runs at
``INTERVAL '1 hour'`` — and in TWO places (``flusher.py``, ``runtime.py``). The read window
was therefore 60 times narrower than the retention window, with no documented reason.

Consequence measured on live production at 19:36: five rows in the database, including
``codex`` (7 min old, 7 tools) and ``hawixs`` (30 min, 4 tools). The panel showed only three.
Two of five real callers were invisible WHILE THEIR ROWS EXISTED.

These tests guard the invariant rather than the value: three SQL literals that must agree
with nothing linking them will always end up diverging. It is that drift being pinned, not
the number ``3600``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

_METRICS_DIR = Path(__file__).resolve().parents[3] / "src" / "brain_v42" / "metrics"


def test_the_read_window_and_the_purge_window_come_from_one_constant() -> None:
    """Both predicates derive from the same interval, so they can no longer diverge.

    RED before the fix: ``brain_v42.metrics.retention`` does not exist, the import raises
    ModuleNotFoundError — loudly, which is the right failure.
    """
    from brain_v42.metrics.retention import (
        PROCESS_METRICS_FRESH_SQL,
        PROCESS_METRICS_RETENTION_SECONDS,
        PROCESS_METRICS_STALE_SQL,
    )

    seconds_in_fresh = re.findall(r"INTERVAL '(\d+) seconds'", PROCESS_METRICS_FRESH_SQL)
    seconds_in_stale = re.findall(r"INTERVAL '(\d+) seconds'", PROCESS_METRICS_STALE_SQL)

    assert seconds_in_fresh == [str(PROCESS_METRICS_RETENTION_SECONDS)], (
        f"le prédicat de fraîcheur doit nommer {PROCESS_METRICS_RETENTION_SECONDS}s, "
        f"il nomme {seconds_in_fresh}"
    )
    assert seconds_in_stale == [str(PROCESS_METRICS_RETENTION_SECONDS)], (
        f"le prédicat de péremption doit nommer {PROCESS_METRICS_RETENTION_SECONDS}s, "
        f"il nomme {seconds_in_stale}"
    )

    # Strict complements: what survives the purge is readable, and conversely.
    assert ">" in PROCESS_METRICS_FRESH_SQL and "<" in PROCESS_METRICS_STALE_SQL


def test_the_retention_window_is_not_narrower_than_a_flush_period() -> None:
    """A window shorter than the flush period makes active agents invisible.

    A sanity check on the value, not on its exactness: the flush runs every minute, so a
    one-minute retention would make the panel flicker.
    """
    from brain_v42.metrics.retention import PROCESS_METRICS_RETENTION_SECONDS

    assert PROCESS_METRICS_RETENTION_SECONDS >= 300, (
        "une rétention sous 5 minutes fait disparaître du panneau des agents qui "
        "viennent d'appeler un tool"
    )


def test_no_metrics_module_hardcodes_a_process_metrics_window() -> None:
    """The NEGATIVE probe: it must fail if anyone hardcodes a literal again.

    RED before the fix: three sites make it fail — collector_db.py (60 seconds),
    flusher.py (1 hour) and runtime.py (1 hour). That is exactly the ticket's drift.
    """
    offenders: list[str] = []

    # The SQL is written across several lines: searching line by line would miss
    # precisely the original defect's site (collector_db.py, where the table and the
    # interval are 4 lines apart). So we search over the whole text.
    # The ``(?!FROM)`` prevents crossing a query boundary: without it, the pattern
    # traverses the neighbouring DELETE on search_log and reports its INTERVAL
    # '30 days', which has nothing to do with this table.
    near_table = re.compile(r"process_metrics(?:(?!FROM).){0,400}?INTERVAL\s*'([^']+)'", re.DOTALL)

    for module in sorted(_METRICS_DIR.glob("*.py")):
        if module.name == "retention.py":
            continue  # the source of truth is allowed to name the interval
        source = module.read_text(encoding="utf-8")
        for match in near_table.finditer(source):
            line_no = source.count("\n", 0, match.start()) + 1
            offenders.append(f"{module.name}:{line_no}: INTERVAL '{match.group(1)}'")

    assert offenders == [], (
        "un intervalle est codé en dur dans une requête sur process_metrics ; "
        "utiliser brain_v42.metrics.retention à la place :\n" + "\n".join(offenders)
    )


@pytest.mark.asyncio
async def test_collect_process_metrics_reads_with_the_shared_predicate() -> None:
    """The behavioural witness: the real query carries the shared predicate.

    RED before the fix: the executed SQL contains ``INTERVAL '60 seconds'``, not the
    shared predicate, so the assertion bites. If anyone goes back to a literal later, it
    bites again — which the structural witness alone does not guarantee.
    """
    from brain_v42.metrics.collector_db import _DbCollectorsMixin
    from brain_v42.metrics.retention import PROCESS_METRICS_FRESH_SQL

    executed: list[str] = []

    class _FakeResult:
        def all(self) -> list[Any]:
            return []

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, statement: Any) -> _FakeResult:
            executed.append(str(statement))
            return _FakeResult()

    mixin = _DbCollectorsMixin.__new__(_DbCollectorsMixin)
    mixin._session_factory = MagicMock(return_value=_FakeSession())  # type: ignore[attr-defined]

    await mixin.collect_process_metrics()

    assert executed, "collect_process_metrics n'a exécuté aucune requête"
    assert any(PROCESS_METRICS_FRESH_SQL in sql for sql in executed), (
        "la lecture de process_metrics n'utilise pas le prédicat partagé ; SQL exécuté :\n"
        + "\n".join(executed)
    )


def _row(agent_name: str, pid: int, *, is_live: bool) -> tuple[Any, ...]:
    """A process_metrics row as the SELECT returns it (is_live last)."""
    return (
        agent_name,
        pid,
        None,  # started_at
        None,  # updated_at
        {"brain_search": {"calls": 3, "total_latency": 30.0}},  # tool_stats
        {},  # embedding_stats
        0,  # memory_rss_bytes
        is_live,
    )


@pytest.mark.asyncio
async def test_active_processes_counts_only_processes_that_still_refresh() -> None:
    """``active_processes`` is a claim of LIVENESS, not of residency in the database.

    Widening the read window to the retention one without distinguishing the two would
    count a dead process for an hour. Measured on production on 2026-08-10: pid 1082528
    was inside the one-hour window and absent from ``ps``.

    RED before the fix: ``active_processes`` counts the pids of ALL returned rows, hence
    2 instead of 1.
    """
    from brain_v42.metrics.collector_db import _DbCollectorsMixin

    class _FakeResult:
        def all(self) -> list[Any]:
            return [
                _row("brain-v42", 1111, is_live=True),
                _row("codex", 2222, is_live=False),  # has not refreshed for a long time
            ]

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, statement: Any) -> _FakeResult:
            return _FakeResult()

    mixin = _DbCollectorsMixin.__new__(_DbCollectorsMixin)
    mixin._session_factory = MagicMock(return_value=_FakeSession())  # type: ignore[attr-defined]

    result = await mixin.collect_process_metrics()

    assert result["active_processes"] == 1, (
        "un process qui a cessé de rafraîchir sa ligne est compté comme actif : "
        f"active_processes={result['active_processes']}"
    )
    # …but the agent stays visible on the panel: that is the whole point of widening.
    assert result["active_agents"] == 2, (
        "l'agent silencieux doit rester visible dans le panneau pendant la rétention"
    )
    assert set(result["by_agent"]) == {"brain-v42", "codex"}


def test_the_liveness_window_is_strictly_tighter_than_the_retention_window() -> None:
    """Two windows, two questions. Confusing them is exactly the original defect."""
    from brain_v42.metrics.retention import (
        PROCESS_METRICS_LIVE_SECONDS,
        PROCESS_METRICS_RETENTION_SECONDS,
    )

    assert PROCESS_METRICS_LIVE_SECONDS < PROCESS_METRICS_RETENTION_SECONDS, (
        "la vivacité doit être plus étroite que le séjour, sinon un process mort "
        "reste 'actif' aussi longtemps que sa ligne survit"
    )

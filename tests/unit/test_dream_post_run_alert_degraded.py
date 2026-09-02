"""A DEGRADED night must stop printing "no failures" in the morning.

Measured on the seven nights 2026-08-27 → 2026-09-02: roadmap ran clean ONCE.
Three nights were served 10/10 batches by the standby model and three failed
outright. The three degraded ones went unnoticed because `dream_runs.status`
reads `'done'` — the degradation travels in `error_message` — and
`fetch_failed_runs` filters on `FAILED_STATUSES`. The night of 2026-09-02 ends,
verbatim, on `no failures for 2026-09-02`.

The contract these tests protect:

* the mark is read from the PREFIX and from the shared module, never retyped;
* a merely talkative `'done'` row (the empty-pool promote message) is NOT
  degradation, and must not enter the rubric;
* the exit code does NOT move — escalation belongs to coverage alone, and
  `dream.sh` turns `rc=2` into a `coverage` dream_runs row. Making a degraded
  night escalate would engrave a lie about the instrument.
"""

from __future__ import annotations

import datetime as dt
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from scripts.dream import post_run_alert
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42 import dream_degradation
from brain_v42.scripts.roadmap_curate import _degradation_notice

RUN_DATE = dt.date(2026, 9, 2)

#: Copied by SELECT from the live database on 2026-09-02, not retyped from the
#: producer: `select error_message from dream_runs where run_date='2026-09-02'
#: and phase='roadmap' and status='done'`. The accents are part of the value.
PRODUCTION_ROW: dict[str, object] = {
    "phase": "roadmap",
    "project_key": "*",
    "status": "done",
    "model": "openai/gpt-oss-20b",
    "error_message": (
        "DÉGRADÉ : 10/10 batches servis par le modèle de SECOURS, le primaire "
        "mistralai/mistral-nemotron n'a pas répondu — mistralai/mistral-nemotron: "
        "TimeoutError ; mistralai/mistral-nemotron: écarté (circuit ouvert plus "
        "tôt dans ce run)"
    ),
}

#: The other `'done'` row that carries a message. It is NOT a degradation, and
#: the whole point of keying on the prefix is that it stays out of the rubric.
EMPTY_POOL_ROW: dict[str, object] = {
    "phase": "promote",
    "project_key": "red-lab",
    "status": "done",
    "model": None,
    "error_message": (
        "empty candidate pool — no learning met the promotion maturity filter "
        "(access_count_human >= 3); nothing to promote, phase did not run"
    ),
}


@pytest.fixture(autouse=True)
def _no_host_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same cut as the sibling suites: never read the MACHINE's systemd drop-in."""
    monkeypatch.setattr(post_run_alert, "expected_dream_phase_pairs", set)
    monkeypatch.setattr(post_run_alert, "expected_dream_phases", set)


def _result(rows: list[dict[str, object]]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = [SimpleNamespace(_mapping=row) for row in rows]
    return result


def _session_returning(observed: list[dict[str, object]]) -> AsyncMock:
    """Faithful to `fetch_failed_runs`'s THREE reads, in order.

    The persisted failures are derived from the observed rows through the real
    `FAILED_STATUSES` filter, as in the sibling suite: a harness that froze them
    empty would make a row turned failing redden nothing.
    """
    session = AsyncMock(spec=AsyncSession)
    persisted = [row for row in observed if row.get("status") in post_run_alert.FAILED_STATUSES]
    count_result = MagicMock()
    count_result.scalar_one.return_value = len(persisted)
    session.execute = AsyncMock(side_effect=[_result(observed), _result(persisted), count_result])
    return session


# ── The mark: where it lives, and how it is read ─────────────────────────────


def test_the_reader_takes_the_prefix_from_the_shared_module() -> None:
    """No second literal. The module that owns the value says why, at length.

    A copy in the reader would survive a producer-side rename with every test
    green and the rubric silently empty — the exact failure the accented value
    was isolated to prevent.
    """
    assert post_run_alert.DEGRADED_PREFIX is dream_degradation.DEGRADED_PREFIX
    source = inspect.getsource(post_run_alert)
    assert source.count('"DÉGRADÉ"') == 0, "le préfixe se lit depuis brain_v42.dream_degradation"


def test_the_producers_own_sentence_is_what_the_reader_parses() -> None:
    """Built by `_degradation_notice`, not by a literal held in agreement."""
    notice = _degradation_notice(
        "mistralai/mistral-nemotron",
        fallback_batches=10,
        scanned=10,
        primary_errors=["mistralai/mistral-nemotron: TimeoutError"],
    )
    assert notice is not None

    (degraded,) = post_run_alert.degraded_rows(
        [{"phase": "roadmap", "project_key": "*", "status": "done", "error_message": notice}]
    )

    assert degraded.primary_model == "mistralai/mistral-nemotron"
    assert (degraded.fallback_batches, degraded.scanned) == (10, 10)
    assert degraded.cause == "mistralai/mistral-nemotron: TimeoutError"


def test_the_production_row_of_the_night_of_2026_09_02_parses() -> None:
    """The row that printed "no failures", read as it is stored."""
    (degraded,) = post_run_alert.degraded_rows([PRODUCTION_ROW])

    assert degraded.phase == "roadmap"
    assert degraded.project_key == "*"
    assert degraded.served_model == "openai/gpt-oss-20b"
    assert degraded.primary_model == "mistralai/mistral-nemotron"
    assert (degraded.fallback_batches, degraded.scanned) == (10, 10)


def test_a_talkative_done_row_is_not_a_degraded_one() -> None:
    """`extract` and `promote` write on `'done'` too. Only the prefix counts."""
    assert post_run_alert.degraded_rows([EMPTY_POOL_ROW]) == []


def test_a_degraded_sentence_that_changed_shape_is_still_listed() -> None:
    """The rubric must never fall silent because the sentence was reworded.

    The prefix is the contract; the rest is best-effort. A reader that dropped
    an unparsable row would turn a producer-side edit into a blind morning,
    which is the very defect being fixed here.
    """
    (degraded,) = post_run_alert.degraded_rows(
        [
            {
                "phase": "roadmap",
                "project_key": "*",
                "status": "done",
                "model": "openai/gpt-oss-20b",
                "error_message": f"{dream_degradation.DEGRADED_PREFIX} — forme inédite",
            }
        ]
    )

    assert degraded.primary_model is None
    assert degraded.fallback_batches is None
    assert "forme inédite" in degraded.message


# ── The morning verdict ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_night_whose_only_wound_is_degradation_no_longer_says_no_failures() -> None:
    session = _session_returning([PRODUCTION_ROW, EMPTY_POOL_ROW])

    night = await post_run_alert.review_night(session, RUN_DATE)
    rendered = post_run_alert.render_stdout(
        night.report, RUN_DATE, night.coverage, degraded=night.degraded
    )

    assert "no failures" not in rendered
    first_line = rendered.splitlines()[0]
    assert first_line == (
        "no failed phase for 2026-09-02 — but 1 phase ran DEGRADED (standby model)"
    )


@pytest.mark.asyncio
async def test_the_rubric_names_the_primary_the_batches_and_the_night() -> None:
    session = _session_returning([PRODUCTION_ROW])

    night = await post_run_alert.review_night(session, RUN_DATE)
    rendered = post_run_alert.render_stdout(
        night.report, RUN_DATE, night.coverage, degraded=night.degraded
    )

    assert f"### {dream_degradation.DEGRADED_PREFIX} (secours) — 2026-09-02" in rendered
    detail = next(line for line in rendered.splitlines() if line.startswith("- roadmap"))
    assert "10/10 batches" in detail
    assert "mistralai/mistral-nemotron" in detail
    assert "openai/gpt-oss-20b" in detail


@pytest.mark.asyncio
async def test_the_rubric_stays_apart_from_the_failures() -> None:
    """A degraded phase is not a failure and must not be counted as one."""
    session = _session_returning(
        [PRODUCTION_ROW, {"phase": "extract", "project_key": "*", "status": "fail"}]
    )

    night = await post_run_alert.review_night(session, RUN_DATE)
    rendered = post_run_alert.render_stdout(
        night.report, RUN_DATE, night.coverage, degraded=night.degraded
    )

    assert rendered.splitlines()[0] == "Dream run on 2026-09-02 had 1 non-OK phase(s):"
    assert "- extract [fail]" in rendered
    assert f"### {dream_degradation.DEGRADED_PREFIX} (secours)" in rendered


@pytest.mark.asyncio
async def test_a_clean_night_keeps_its_historical_first_line() -> None:
    """Nothing degraded, nothing failed: the line the sibling suites pin."""
    session = _session_returning([{"phase": "roadmap", "project_key": "*", "status": "done"}])

    night = await post_run_alert.review_night(session, RUN_DATE)
    rendered = post_run_alert.render_stdout(
        night.report, RUN_DATE, night.coverage, degraded=night.degraded
    )

    assert rendered.splitlines()[0] == "no failures for 2026-09-02"
    assert dream_degradation.DEGRADED_PREFIX not in rendered


# ── What must NOT move ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reading_the_degradation_costs_no_extra_query() -> None:
    """Still THREE reads. The mark rides the observed SELECT, it does not add one."""
    session = _session_returning([PRODUCTION_ROW])

    await post_run_alert.review_night(session, RUN_DATE)

    assert session.execute.await_count == 3
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_degraded_night_does_not_escalate_the_exit_code() -> None:
    """`rc=2` belongs to COVERAGE alone.

    `dream.sh` reads `rc=2` as "expected rows are missing" and writes a
    `coverage` dream_runs row saying so. A degraded night has all its rows: it
    must be LOUD, not escalating, or the instrument would start lying about
    itself — the exact trap ticket `0a9c067e` documents.
    """
    session = _session_returning([PRODUCTION_ROW])

    night = await post_run_alert.review_night(session, RUN_DATE)

    assert night.degraded
    assert night.coverage.escalates is False

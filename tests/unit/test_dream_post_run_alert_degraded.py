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


def test_the_log_line_and_the_stored_value_are_NOT_the_same_string() -> None:
    """The trap that produced ticket `e7006388`, pinned so it cannot be sprung twice.

    `roadmap_curate` prints `f"! {degraded}"` to the journal and stores `degraded`
    itself in `error_message`. The exclamation mark belongs to the LOG and to the
    log alone -- but it is the form a reader sees first, so it is the form that
    ends up in a hand-written query.

    That is exactly what happened: `error_message LIKE '! DÉGRADÉ%'` returned 0
    rows for 2026-09-02 and a ticket was opened saying the mark never reached
    `dream_runs`. The mark was there, under `LIKE 'DÉGRADÉ%'`, and the whole
    chain -- writer, column, fetch, parser, rubric -- was working.

    Nothing here is broken, and this test is not a fix. It is the missing
    sentence: the two forms differ, the reader keys on the STORED one, and a
    query carrying the log's prefix finds nothing on a perfectly healthy night.
    """
    notice = _degradation_notice(
        "mistralai/mistral-nemotron",
        fallback_batches=10,
        scanned=10,
        primary_errors=["timeout"],
    )
    assert notice is not None

    # What the column holds: no exclamation mark, ever.
    assert notice.startswith(post_run_alert.DEGRADED_PREFIX)
    assert not notice.startswith("! ")

    # What the journal shows, and what a query must therefore NOT copy.
    assert f"! {notice}".startswith(f"! {post_run_alert.DEGRADED_PREFIX}")

    # The reader keys on the stored form; the log form would never match it.
    assert post_run_alert.degraded_rows(
        [{"phase": "roadmap", "project_key": "*", "status": "done", "error_message": notice}]
    )
    assert not post_run_alert.degraded_rows(
        [
            {
                "phase": "roadmap",
                "project_key": "*",
                "status": "done",
                "error_message": f"! {notice}",
            }
        ]
    ), "if the log form ever became storable, every query written for it would be right by accident"


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


# --- The rubric in the ASSEMBLED report, not just in the parser -------------
#
# Until 2026-09-03 every test here stopped at `degraded_rows`: the parser was
# proven, the RENDERING never was. The rubric (PR #73) has never bitten on a
# real night — the first night it could have was the one the chain stopped
# degrading — so nothing but these two tests stands between it and a silent
# removal.


def _blank_coverage() -> post_run_alert.CoverageReport:
    """A coverage report that renders nothing, so the rubric is what is read."""
    return post_run_alert.CoverageReport(
        block=(),
        machine_line="COVERAGE expected=0",
        silent_line=None,
        synthetic=(),
        escalates=False,
        verdict=None,
    )


def test_the_degraded_rubric_reaches_the_assembled_report() -> None:
    """Positive case, built from the REAL row of the night of 2026-09-02."""
    degraded = post_run_alert.degraded_rows([PRODUCTION_ROW])

    rendered = post_run_alert.render_stdout(
        None,
        dt.date(2026, 9, 2),
        _blank_coverage(),
        degraded=degraded,
    )

    assert post_run_alert.DEGRADED_HEADING in rendered, (
        "la rubrique DÉGRADÉ doit atteindre le rapport rendu, pas seulement le parseur"
    )
    assert "roadmap" in rendered, "la phase dégradée doit être NOMMÉE"
    assert "2026-09-02" in rendered


def test_a_night_without_degradation_renders_no_rubric() -> None:
    """Negative case: the night of 2026-09-03, which degraded nothing.

    The counter-witness matters as much as the positive one: a rubric that
    printed its heading every night would say nothing by saying it always.
    """
    rendered = post_run_alert.render_stdout(
        None,
        dt.date(2026, 9, 3),
        _blank_coverage(),
        degraded=(),
    )

    assert post_run_alert.DEGRADED_HEADING not in rendered
    assert "no failures for 2026-09-03" in rendered


# --- Two producers, two units, and no borrowed word ------------------------
#
# `extract` counts TICKETS and falls back because its primary was WITHDRAWN;
# `roadmap` counts BATCHES and falls back because its primary did not ANSWER.
# The writers refused to share a wording to fit one regex (89e37c8's docstring
# says so in as many words), so the reader learns both instead of flattening
# them.


def _extract_notice(scanned: int = 19) -> str:
    """The extract sentence, BUILT by its producer rather than copied."""
    from brain_v42.scripts.ticket_extract import _degradation_notice as extract_notice

    return extract_notice(
        primary="deepseek-ai/deepseek-v4-pro",
        fallback="meta/llama-3.3-70b-instruct",
        switched=True,
        scanned=scanned,
        cause="HTTP 410",
    )


def _row(phase: str, message: str, model: str) -> dict[str, object]:
    return {
        "phase": phase,
        "project_key": "*",
        "status": "done",
        "model": model,
        "error_message": message,
    }


def test_the_reader_parses_the_extract_shape_and_names_its_unit() -> None:
    """`19 tickets`, ONE count and not a ratio: extract has no denominator."""
    (degraded,) = post_run_alert.degraded_rows(
        [_row("extract", _extract_notice(), "meta/llama-3.3-70b-instruct")]
    )

    assert degraded.unit == "tickets"
    assert degraded.scanned == 19
    assert degraded.fallback_batches is None, "extract compte, il ne rapporte pas de ratio"
    assert degraded.primary_model == "deepseek-ai/deepseek-v4-pro"


def test_the_reader_still_parses_the_roadmap_ratio() -> None:
    """The shape that already worked must keep working, unit included."""
    (degraded,) = post_run_alert.degraded_rows([PRODUCTION_ROW])

    assert degraded.unit == "batches"
    assert degraded.fallback_batches == 10
    assert degraded.scanned == 10


def test_neither_phase_borrows_the_other_s_word_in_the_rubric() -> None:
    """The rubric is where a borrowed unit would mislead an operator at 7am."""
    extract_row, roadmap_row = post_run_alert.degraded_rows(
        [_row("extract", _extract_notice(), "meta/llama-3.3-70b-instruct"), PRODUCTION_ROW]
    )
    block = "\n".join(
        post_run_alert.build_degraded_block(dt.date(2026, 9, 3), [extract_row, roadmap_row])
    )

    extract_line = next(line for line in block.splitlines() if line.startswith("- extract"))
    roadmap_line = next(line for line in block.splitlines() if line.startswith("- roadmap"))

    assert "19 tickets" in extract_line
    assert "batches" not in extract_line, "extract ne compte pas des batches"
    assert "10/10 batches" in roadmap_line
    assert "tickets" not in roadmap_line, "roadmap ne compte pas des tickets"

"""`post_run_alert` compares against the pairs DECLARED by the night.

Ticket `0a9c067e`. The comparator already existed and was firing; its expectation
came from the systemd drop-in, which has keys only for `promote` and `reorg`. The
night of 2026-08-16 therefore announced 20 missing phases when 60 were missing.

This file holds three contracts:

1. **Coverage goes from 2 phases to 6** — with no false positive on skips
   (preflight, killswitch) NOR false negative on writes declared as failed.
2. **Exit code 2** exists and rings ONLY on a hole, a write declared as failed,
   or a suspect manifest structure. Never in the fallback path.
3. **The machine line `COVERAGE` is the last line of stdout, ALWAYS** — including
   on green nights. That is the whole point of the ticket: putting side by side,
   every morning, what the night says it did and what it wrote.

The fallback stays today's path: same summary lines, same wording, plus an
explicit warning and a machine line with DIFFERENT FIELD NAMES — because 23 pairs
expected from the drop-in and 62 pairs written the same night are not comparable.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from scripts.dream import post_run_alert
from scripts.dream import run_manifest as rm
from sqlalchemy.ext.asyncio import AsyncSession

RUN_DATE = dt.date(2026, 8, 18)
PROJECTS = tuple(f"p{index}" for index in range(10))
LOOP_PHASES = ("scan", "clean", "connect", "synth", "promote", "reorg")
GLOBALS = ("extract", "roadmap", "sweep")


@pytest.fixture(autouse=True)
def _no_host_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cut the dependency on the MACHINE's systemd drop-in.

    Same reason as in `test_dream_post_run_alert.py`: a dependency that is green
    where nobody looks and red where the system runs.
    """
    monkeypatch.setattr(post_run_alert, "expected_dream_phase_pairs", set)
    monkeypatch.setattr(post_run_alert, "expected_dream_phases", set)


def _line(*parts: str) -> str:
    padded = (*parts, "", "", "")[:4]
    return "\t".join(padded) + "\n"


def _manifest(
    *,
    expected: tuple[tuple[str, str], ...],
    skipped: tuple[tuple[str, str, str], ...] = (),
    failed: tuple[tuple[str, str], ...] = (),
    timed_out: tuple[tuple[str, str], ...] = (),
    meta: dict[str, str] | None = None,
    finished: bool = True,
) -> rm.RunManifest:
    head = {"run_date": RUN_DATE.isoformat(), **(meta or {})}
    text = "".join(_line("meta", key, value) for key, value in head.items())
    text += "".join(_line("expected", phase, project) for phase, project in expected)
    text += "".join(_line("skipped", *entry) for entry in skipped)
    text += "".join(_line("failed", phase, project) for phase, project in failed)
    text += "".join(_line("timeout", phase, project) for phase, project in timed_out)
    if finished:
        text += _line("meta", "finished", "2026-08-18T07:09:32+02:00")
    return rm.parse_run_manifest(text)


def _full_night() -> tuple[tuple[str, str], ...]:
    pairs = tuple((phase, project) for project in PROJECTS for phase in LOOP_PHASES)
    return pairs + tuple((phase, "*") for phase in GLOBALS)


def _result(rows: list[dict[str, object]]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = [SimpleNamespace(_mapping=row) for row in rows]
    return result


def _session(observed: list[dict[str, object]]) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    persisted = [row for row in observed if row.get("status") in post_run_alert.FAILED_STATUSES]
    count_result = MagicMock()
    count_result.scalar_one.return_value = len(persisted)
    # The FOURTH read is `fetch_mute_transitions`'s (step 0 of `55a21fb8`). It is
    # consumed only by `review_and_render`; tests that call `review_night`
    # directly leave one aside, which has no effect. No existing assertion is
    # touched: it is the HARNESS that follows the live path, not the contract that
    # bends.
    session.execute = AsyncMock(
        side_effect=[_result(observed), _result(persisted), count_result, _result([])]
    )
    session.commit = AsyncMock()
    return session


def _rows(pairs: tuple[tuple[str, str], ...], status: str = "done") -> list[dict[str, object]]:
    return [
        {
            "id": index,
            "phase": phase,
            "status": status,
            "project_key": project,
            "error_message": None,
            "created_at": dt.datetime(2026, 8, 18, 6, index % 59),
        }
        for index, (phase, project) in enumerate(pairs)
    ]


# --- The ticket's fact: 60, not 20 ------------------------------------------


@pytest.mark.asyncio
async def test_the_night_that_wrote_two_rows_reports_sixty_silent_phases() -> None:
    """Nights of 2026-08-15 and 08-16 replayed against the DECLARED expectations.

    Today's path reports 20 of them: `LOOP_PHASES` carries only `promote` and
    `reorg`, and the four core phases have no key in `_KS_KEYS`, so widening them
    over there would be a no-op.
    """
    manifest = _manifest(expected=_full_night())
    observed = tuple((phase, "*") for phase in GLOBALS)
    session = _session(_rows(observed))

    night = await post_run_alert.review_night(session, RUN_DATE, manifest=manifest)

    assert len(night.coverage.verdict.silent) == 60
    assert night.report is not None
    silent_lines = [
        line for line in night.report.splitlines() if post_run_alert.COVERAGE_SILENT_MESSAGE in line
    ]
    assert len(silent_lines) == 60, "60 paires, 6 par projet — sous le plafond de 8"
    assert night.coverage.escalates is True


@pytest.mark.asyncio
async def test_a_night_with_no_row_at_all_names_the_connection_first() -> None:
    """`written == 0`: the first move is not to go and look at the phases.

    Reproduces 08-15 and 08-16 (2 rows out of 63) — the DSN regression. Without
    this message, the operator opens 63 phase reports before thinking of the DSN.
    """
    manifest = _manifest(expected=_full_night())
    session = _session([])

    night = await post_run_alert.review_night(session, RUN_DATE, manifest=manifest)

    assert night.report is not None
    rendered = post_run_alert.render_stdout(night.report, RUN_DATE, night.coverage)
    assert post_run_alert.NO_ROW_AT_ALL_MESSAGE in rendered
    assert post_run_alert.NO_ROW_AT_ALL_MESSAGE != post_run_alert.COVERAGE_SILENT_MESSAGE


@pytest.mark.asyncio
async def test_a_night_that_only_wrote_its_global_phases_names_the_connection_too() -> None:
    """08-15 and 08-16 as the DATABASE carries them: 2 rows out of 63.

    Measured read-only on production — both nights wrote `(extract, *)` and
    `(roadmap, *)`, the phases that run IN PROCESS from dream.sh, and not one row
    of the 60 project phases. `written` is therefore 2, not 0: the `not written`
    guard did not fire and the operator of those nights received 61 lines sending
    them back to the phase reports — the very first move this message exists to
    correct.
    """
    manifest = _manifest(expected=_full_night())
    observed = (("extract", "*"), ("roadmap", "*"))
    session = _session(_rows(observed))

    night = await post_run_alert.review_night(session, RUN_DATE, manifest=manifest)

    assert len(night.coverage.verdict.written) == 2, "la nuit réelle, pas une nuit à zéro ligne"
    rendered = post_run_alert.render_stdout(night.report, RUN_DATE, night.coverage)
    assert post_run_alert.NO_ROW_AT_ALL_MESSAGE in rendered


@pytest.mark.asyncio
async def test_one_project_that_wrote_its_rows_is_not_a_connection_problem() -> None:
    """The reverse direction: the guard must not become a permanent scream.

    A night where one project wrote its six rows and the others nothing is a night
    failure, not a connection failure: the write rail worked.
    """
    manifest = _manifest(expected=_full_night())
    observed = tuple((phase, "p0") for phase in LOOP_PHASES)
    session = _session(_rows(observed))

    night = await post_run_alert.review_night(session, RUN_DATE, manifest=manifest)

    rendered = post_run_alert.render_stdout(night.report, RUN_DATE, night.coverage)
    assert post_run_alert.NO_ROW_AT_ALL_MESSAGE not in rendered
    assert night.coverage.escalates is True, "le trou reste rapporté, seul le wording change"


@pytest.mark.asyncio
async def test_a_complete_night_reports_nothing_and_still_prints_coverage() -> None:
    manifest = _manifest(expected=_full_night())
    session = _session(_rows(_full_night()))

    night = await post_run_alert.review_night(session, RUN_DATE, manifest=manifest)

    assert night.report is None
    assert night.coverage.escalates is False
    assert "silent=0" in night.coverage.machine_line
    assert night.coverage.silent_line is None


# --- The four wordings, because four different first moves ------------------


@pytest.mark.asyncio
async def test_a_declared_write_failure_says_so_and_escalates() -> None:
    manifest = _manifest(
        expected=(("promote", "red-lab"),),
        skipped=(("promote", "red-lab", "empty-pool-unrecorded"),),
    )
    session = _session([])

    night = await post_run_alert.review_night(session, RUN_DATE, manifest=manifest)

    assert night.report is not None
    assert post_run_alert.COVERAGE_WRITEFAIL_MESSAGE in night.report
    assert post_run_alert.COVERAGE_SILENT_MESSAGE not in night.report
    assert night.coverage.escalates is True


@pytest.mark.asyncio
async def test_a_declared_failure_keeps_the_historic_wording_and_does_not_escalate() -> None:
    """dream.sh already exits 1 through `FAILED_PHASES`: nothing to escalate here."""
    manifest = _manifest(expected=(("connect", "brain-v42"),), failed=(("connect", "brain-v42"),))
    session = _session([])

    night = await post_run_alert.review_night(session, RUN_DATE, manifest=manifest)

    assert night.report is not None
    assert post_run_alert.MISSING_EXPECTED_MESSAGE in night.report
    assert night.coverage.escalates is False


@pytest.mark.asyncio
async def test_a_row_written_while_the_night_declared_failure_is_reported() -> None:
    """The 19→20 night, replayed as it happened.

    `reorg`/`brain-v42` was declared `failed` by dream.sh, but its `dream_runs`
    marking crashed and the row stayed `done`. The verdict therefore read FULL
    coverage while looking at an input file that said failure. A report that
    throws away the declaration of its own input file is a false green, not
    coverage.

    The assertion is on `render_stdout`, not on the verdict alone: a night with no
    failing row has `report is None`, so a signal lodged in the report body would
    reach NOBODY. The coverage block, by contrast, is printed every night — it is
    the only place where this signal really exists.
    """
    pair = ("reorg", "brain-v42")
    manifest = _manifest(expected=(pair,), failed=(pair,))
    session = _session(_rows((pair,), status="done"))

    night = await post_run_alert.review_night(session, RUN_DATE, manifest=manifest)
    stdout = post_run_alert.render_stdout(night.report, RUN_DATE, night.coverage)

    assert night.coverage.verdict is not None
    assert night.coverage.verdict.mismatch == frozenset({pair})
    assert post_run_alert.COVERAGE_MISMATCH_MESSAGE in stdout
    assert "brain-v42/reorg" in stdout, "l'alerte doit NOMMER la paire"
    assert "mismatch 1" in stdout, "le compteur doit être dans le bloc couverture"
    assert "mismatch=1" in night.coverage.machine_line


@pytest.mark.asyncio
async def test_a_mismatch_reports_without_turning_the_night_red() -> None:
    """RAPPORT SEULEMENT — faire escalader ce signal touche au moteur."""
    pair = ("reorg", "brain-v42")
    manifest = _manifest(
        expected=(pair,),
        failed=(pair,),
        meta={"planned_phases": "1", "total_phases": "1"},
    )
    session = _session(_rows((pair,), status="done"))

    night = await post_run_alert.review_night(session, RUN_DATE, manifest=manifest)

    assert night.coverage.verdict is not None
    assert night.coverage.verdict.mismatch
    assert night.coverage.escalates is False


@pytest.mark.asyncio
async def test_a_clean_night_carries_a_zero_mismatch_counter() -> None:
    """The counter is ALWAYS printed: a missing line would be ambiguous."""
    manifest = _manifest(expected=(("scan", "red"),))
    session = _session(_rows((("scan", "red"),)))

    night = await post_run_alert.review_night(session, RUN_DATE, manifest=manifest)
    stdout = post_run_alert.render_stdout(night.report, RUN_DATE, night.coverage)

    assert "mismatch 0" in stdout
    assert post_run_alert.COVERAGE_MISMATCH_MESSAGE not in stdout
    assert "mismatch=0" in night.coverage.machine_line


@pytest.mark.asyncio
async def test_a_missing_declared_pair_is_not_counted_as_a_mismatch() -> None:
    """`declared` and `mismatch` must never count the same pair."""
    pair = ("connect", "brain-v42")
    manifest = _manifest(expected=(pair,), failed=(pair,))
    session = _session([])

    night = await post_run_alert.review_night(session, RUN_DATE, manifest=manifest)
    stdout = post_run_alert.render_stdout(night.report, RUN_DATE, night.coverage)

    assert night.coverage.verdict is not None
    assert night.coverage.verdict.declared == frozenset({pair})
    assert night.coverage.verdict.mismatch == frozenset()
    assert post_run_alert.COVERAGE_MISMATCH_MESSAGE not in stdout


@pytest.mark.asyncio
async def test_a_mismatch_never_fabricates_a_synthetic_row() -> None:
    """The line EXISTS — synthesising a second one would make a duplicate."""
    pair = ("reorg", "brain-v42")
    manifest = _manifest(expected=(pair,), failed=(pair,))
    session = _session(_rows((pair,), status="done"))

    night = await post_run_alert.review_night(session, RUN_DATE, manifest=manifest)

    assert night.coverage.synthetic == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["preflight", "killswitch"])
async def test_a_declared_skip_is_never_an_alarm(reason: str) -> None:
    manifest = _manifest(expected=(("synth", "red"),), skipped=(("synth", "red", reason),))
    session = _session([])

    night = await post_run_alert.review_night(session, RUN_DATE, manifest=manifest)

    assert night.report is None
    assert night.coverage.escalates is False


@pytest.mark.asyncio
async def test_a_preflight_night_of_thirty_skips_stays_quiet() -> None:
    """THE anti-false-positive test of the 2 → 6 coverage widening."""
    deep = ("synth", "promote", "reorg")
    expected = tuple((phase, project) for project in PROJECTS for phase in deep)
    manifest = _manifest(
        expected=expected,
        skipped=tuple((phase, project, "preflight") for phase, project in expected),
    )
    session = _session([])

    night = await post_run_alert.review_night(session, RUN_DATE, manifest=manifest)

    assert len(night.coverage.verdict.skipped) == 30
    assert night.report is None
    assert night.coverage.escalates is False


# --- Suspect structure: never a green verdict -------------------------------


@pytest.mark.asyncio
async def test_an_interrupted_night_never_reports_green() -> None:
    manifest = _manifest(expected=(("scan", "red"),), meta={"planned_phases": "63"}, finished=False)
    session = _session(_rows((("scan", "red"),)))

    night = await post_run_alert.review_night(session, RUN_DATE, manifest=manifest)

    assert night.coverage.escalates is True
    assert "mode=manifest-partial" in night.coverage.machine_line


@pytest.mark.asyncio
async def test_disagreeing_counters_escalate() -> None:
    expected = tuple(("scan", f"p{index}") for index in range(57))
    manifest = _manifest(expected=expected, meta={"planned_phases": "63", "total_phases": "63"})
    session = _session(_rows(expected))

    night = await post_run_alert.review_night(session, RUN_DATE, manifest=manifest)

    assert night.coverage.verdict.consistent is False
    assert night.coverage.escalates is True


# --- The fallback: today's path, spelled out in full ------------------------


@pytest.mark.asyncio
async def test_without_a_manifest_the_synthesis_lines_are_the_ones_of_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-regression contract carried on `include_missing_expected_phases`.

    Not on stdout's bytes: the fallback explicitly gains a warning and a machine
    line. Promising "byte-identical" while adding lines would be a contract nobody
    can hold.
    """
    monkeypatch.setattr(post_run_alert, "expected_dream_phase_pairs", lambda: {("promote", "red")})
    observed = [{"phase": "scan", "status": "done", "project_key": "red"}]
    session = _session(observed)

    night = await post_run_alert.review_night(session, RUN_DATE, manifest=None)

    expected_rows = post_run_alert.include_missing_expected_phases(
        observed, set(), [], expected_pairs={("promote", "red")}
    )
    assert night.report is not None
    for row in expected_rows:
        assert str(row["error_message"]) in night.report
    rendered = post_run_alert.render_stdout(night.report, RUN_DATE, night.coverage)
    assert post_run_alert.FALLBACK_WARNING in rendered
    assert "mode=fallback" in night.coverage.machine_line
    assert night.coverage.escalates is False


@pytest.mark.asyncio
async def test_the_fallback_line_never_compares_incomparable_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """23 pairs expected from the drop-in against 62 written on 2026-08-18.

    `COVERAGE expected=23 written=62` would reproduce the ticket's flaw: two
    numbers side by side that nothing reconciles.
    """
    expected_pairs = {("promote", f"p{index}") for index in range(23)}
    monkeypatch.setattr(post_run_alert, "expected_dream_phase_pairs", lambda: expected_pairs)
    observed = _rows(tuple(("scan", f"p{index}") for index in range(62)))
    session = _session(observed)

    night = await post_run_alert.review_night(session, RUN_DATE, manifest=None)

    assert "silent=unknown" in night.coverage.machine_line
    assert "written=" not in night.coverage.machine_line
    assert "observed=62" in night.coverage.machine_line
    assert night.coverage.escalates is False, "le repli ne rend JAMAIS 2"


def test_a_manifest_from_another_night_is_not_used(tmp_path: Path) -> None:
    path = tmp_path / "m.tsv"
    path.write_text(
        _line("meta", "run_date", "2026-08-17") + _line("expected", "scan", "red"),
        encoding="utf-8",
    )

    assert rm.load_run_manifest(path, run_date=RUN_DATE) is None


# --- stdout: two numbers side by side, every morning ------------------------


def _coverage(**kwargs: object) -> post_run_alert.CoverageReport:
    manifest = _manifest(**kwargs)  # type: ignore[arg-type]
    return post_run_alert.coverage_from_manifest(set(), manifest)


def test_the_machine_line_is_always_the_last_line_of_stdout() -> None:
    coverage = _coverage(expected=(("scan", "red"),), skipped=(("scan", "red", "killswitch"),))
    rendered = post_run_alert.render_stdout(None, RUN_DATE, coverage)

    lines = rendered.splitlines()
    assert lines[0] == "no failures for 2026-08-18"
    assert lines[-1].startswith("COVERAGE mode=")


def test_the_silent_line_sits_just_above_the_machine_line() -> None:
    coverage = _coverage(expected=(("scan", "red"),))
    rendered = post_run_alert.render_stdout("Dream run …:\n- scan", RUN_DATE, coverage)

    lines = rendered.splitlines()
    assert lines[-1].startswith("COVERAGE mode=")
    assert lines[-2].startswith("COVERAGE_SILENT ")


def test_the_coverage_block_sits_under_the_first_line_of_the_report() -> None:
    coverage = _coverage(expected=(("scan", "red"),), skipped=(("scan", "red", "killswitch"),))
    rendered = post_run_alert.render_stdout(
        "Dream run on 2026-08-18 had 1 non-OK phase(s):\n\n- connect [fail]: boom",
        RUN_DATE,
        coverage,
    )

    lines = [line for line in rendered.splitlines() if line]
    assert lines[0].startswith("Dream run on")
    assert lines[1] == "### Couverture dream_runs"
    assert any(line.startswith("- connect [fail]") for line in lines)


# --- Codes de sortie et CLI -------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "observed", "code"),
    [
        ({"expected": (("scan", "red"),)}, (("scan", "red"),), 0),
        ({"expected": (("scan", "red"),)}, (), 2),
        (
            {
                "expected": (("scan", "red"),),
                "skipped": (("scan", "red", "killswitch"),),
            },
            (),
            0,
        ),
        (
            {
                "expected": (("promote", "red"),),
                "skipped": (("promote", "red", "empty-pool-unrecorded"),),
            },
            (),
            2,
        ),
        ({"expected": (("scan", "red"),), "failed": (("scan", "red"),)}, (), 0),
    ],
)
async def test_exit_codes_follow_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kwargs: dict[str, object],
    observed: tuple[tuple[str, str], ...],
    code: int,
    tmp_path: Path,
) -> None:
    manifest = _manifest(**kwargs)  # type: ignore[arg-type]
    session = _session(_rows(observed))
    _wire_engine(monkeypatch, session)
    monkeypatch.setattr(post_run_alert, "load_run_manifest", lambda *a, **k: manifest)

    return_code = await post_run_alert._run(RUN_DATE, tmp_path / "m.tsv")

    assert return_code == code
    assert capsys.readouterr().out.splitlines()[-1].startswith("COVERAGE mode=")


def _wire_engine(monkeypatch: pytest.MonkeyPatch, session: AsyncMock) -> MagicMock:
    engine = MagicMock()
    engine.dispose = AsyncMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        post_run_alert,
        "Settings",
        MagicMock(return_value=SimpleNamespace(postgres_url="postgresql+asyncpg://unused")),
    )
    monkeypatch.setattr(post_run_alert, "create_async_engine", MagicMock(return_value=engine))
    monkeypatch.setattr(
        post_run_alert,
        "async_sessionmaker",
        MagicMock(return_value=MagicMock(return_value=context)),
    )
    return engine


@pytest.mark.asyncio
async def test_the_report_stays_read_only_with_a_manifest() -> None:
    """Pinned contract: no write, ever (test_dream_post_run_alert.py:125)."""
    manifest = _manifest(expected=_full_night())
    session = _session([])

    await post_run_alert.review_night(session, RUN_DATE, manifest=manifest)

    session.commit.assert_not_awaited()


def test_the_default_manifest_path_is_derived_from_the_repo() -> None:
    path = post_run_alert.default_manifest_path(RUN_DATE)

    assert path.name == "2026-08-18_manifest.tsv"
    assert path.parent.name == "dream"
    assert path.parent.parent.name == "logs"


def test_the_cli_accepts_an_explicit_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def _fake_run(
        run_date: dt.date,
        manifest_path: Path,
        phases_ok: int | None = None,
        phases_skipped: int = 0,
    ) -> int:
        seen["date"] = run_date
        seen["manifest"] = manifest_path
        seen["phases_ok"] = phases_ok
        seen["phases_skipped"] = phases_skipped
        return 0

    monkeypatch.setattr(post_run_alert.asyncio, "run", lambda coro: coro)
    monkeypatch.setattr(post_run_alert, "_run", _fake_run)

    assert post_run_alert.main(["--date", "2026-08-18", "--manifest", "/tmp/x.tsv"]) == 0
    assert seen["manifest"] == Path("/tmp/x.tsv")
    assert seen["date"] == RUN_DATE
    # Without --phases-ok (a manual replay), no counter: the RECONCILIATION line
    # is not printed and cannot lie.
    assert seen["phases_ok"] is None

    assert (
        post_run_alert.main(
            [
                "--date",
                "2026-08-18",
                "--manifest",
                "/tmp/x.tsv",
                "--phases-ok",
                "61",
                "--phases-skipped",
                "3",
            ]
        )
        == 0
    )
    assert seen["phases_ok"] == 61
    assert seen["phases_skipped"] == 3


# --- The fallback says WHAT IT CANNOT SAY, not only what it measured ---------
#
# Ticket `e30a1cec`, corrected on 2026-08-24. Its first half is FALSE and worth
# repeating: the main path does NOT degrade silently -- it escalates to the
# briefing and to `/metrics`, and stays fail-closed. What survives is one
# residue: `coverage_fallback` NEVER returns 2, so a night that loses its
# manifest stays green. That is deliberate, argued in the code, and pinned by a
# test -- an observation failure must not become a night failure. These tests do
# not touch it.
#
# What they close is the SILENCE, not the escalation. The reserve existed but
# read as a scope note ("coverage limited to promote/reorg"), which a reader at
# 7am can take for a clean night with a smaller perimeter. It now says the
# verdict is not a completeness statement, and why.


def test_the_fallback_reserve_says_the_verdict_is_not_a_completeness_statement() -> None:
    """Measured: 26 nights `mode=manifest` since 2026-08-2x, ZERO in fallback.

    The ticket targets the silence, not the frequency. A path that has never run
    is exactly the one whose wording nobody will check on the morning it does.
    """
    warning = post_run_alert.FALLBACK_WARNING.lower()

    assert "manifest" in warning, "la raison doit être nommée"
    assert any(word in warning for word in ("not reliable", "unreliable", "cannot")), (
        "la réserve doit dire que le verdict n'est PAS un constat de complétude"
    )
    assert "escalat" in warning, (
        "le non-déclenchement est délibéré : il doit être LISIBLE dans le rapport, "
        "pas seulement dans une docstring"
    )


def test_the_fallback_block_carries_no_count_to_be_read_as_clean() -> None:
    """It cannot print `0 mismatch`: without a manifest there is nothing to count.

    Pinned because the reserve alone is not enough — a reserve next to a row of
    zeroes is read as the zeroes.
    """
    coverage = post_run_alert.coverage_fallback(expected=23, observed=62, missing=0)
    block = "\n".join(coverage.block)

    assert post_run_alert.FALLBACK_WARNING in block
    assert "mismatch" not in block
    assert coverage.verdict is None
    assert coverage.escalates is False, "l'escalade du repli reste une DÉCISION, non tranchée ici"


def test_the_manifest_block_does_not_carry_the_fallback_reserve() -> None:
    """The counter-witness: a reserve printed every night would say nothing."""
    manifest = _manifest(expected=(("promote", "red"),))
    coverage = post_run_alert.coverage_from_manifest({("promote", "red")}, manifest)

    assert post_run_alert.FALLBACK_WARNING not in "\n".join(coverage.block)

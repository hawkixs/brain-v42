"""`post_run_alert` compare sur les paires DÉCLARÉES par la nuit.

Ticket `0a9c067e`. Le comparateur existait déjà et tirait ; son attendu venait du
drop-in systemd, qui n'a de clé que pour `promote` et `reorg`. La nuit du
2026-08-16 a donc annoncé 20 phases manquantes quand il en manquait 60.

Ce fichier tient trois contrats :

1. **La couverture passe de 2 phases à 6** — sans faux positif sur les skips
   (pré-flight, killswitch) NI faux négatif sur les écritures déclarées en échec.
2. **Le code de sortie 2** existe et ne sonne QUE sur un trou, une écriture
   déclarée en échec, ou une structure de manifeste douteuse. Jamais en repli.
3. **La ligne machine `COVERAGE` est la dernière ligne de stdout, TOUJOURS** —
   y compris les nuits vertes. C'est tout l'objet du ticket : mettre côte à côte,
   chaque matin, ce que la nuit dit avoir fait et ce qu'elle a écrit.

Le repli reste le chemin d'aujourd'hui : mêmes lignes de synthèse, même wording,
plus un avertissement explicite et une ligne machine aux NOMS DE CHAMPS
différents — parce que 23 paires attendues depuis le drop-in et 62 paires
écrites la même nuit ne sont pas comparables.
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
    """Couper la dépendance au drop-in systemd de la MACHINE.

    Même raison que dans `test_dream_post_run_alert.py` : une dépendance verte
    là où personne ne regarde et rouge là où le système tourne.
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
    # La QUATRIÈME lecture est celle de `fetch_mute_transitions` (marche 0 de
    # `55a21fb8`). Elle n'est consommée que par `review_and_render` ; les tests
    # qui appellent `review_night` directement en laissent une de côté, ce qui
    # est sans effet. Aucune assertion existante n'est touchée : c'est le
    # HARNAIS qui suit le chemin vivant, pas le contrat qui plie.
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


# --- Le fait du ticket : 60, pas 20 -----------------------------------------


@pytest.mark.asyncio
async def test_the_night_that_wrote_two_rows_reports_sixty_silent_phases() -> None:
    """Nuits des 2026-08-15 et 08-16 rejouées sur les attendus DÉCLARÉS.

    Le chemin d'aujourd'hui en rapporte 20 : `LOOP_PHASES` ne porte que
    `promote` et `reorg`, et les quatre core phases n'ont aucune clé dans
    `_KS_KEYS`, donc les élargir là-bas serait un no-op.
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
    """`written == 0` : le premier geste n'est pas d'aller voir les phases.

    Reproduit les 08-15 et 08-16 (2 lignes sur 63) — la régression de DSN. Sans
    ce message, l'opérateur ouvre 63 rapports de phase avant de penser au DSN.
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
    """Les 08-15 et 08-16 telles que la BASE les porte : 2 lignes sur 63.

    Mesuré en lecture seule sur la production — les deux nuits ont écrit
    `(extract, *)` et `(roadmap, *)`, les phases qui tournent EN PROCESSUS
    depuis dream.sh, et pas une ligne des 60 phases de projet. `written` vaut
    donc 2, pas 0 : la garde `not written` ne se déclenchait pas et l'opérateur
    de ces nuits-là recevait 61 lignes le renvoyant vers les rapports de phase —
    le premier geste que ce message existe pour corriger.
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
    """Le sens de marche inverse : la garde ne doit pas devenir un cri permanent.

    Une nuit où un projet a écrit ses six lignes et les autres rien est une
    panne de nuit, pas de connexion : le rail d'écriture a fonctionné.
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


# --- Les quatre wordings, parce que quatre premiers gestes -------------------


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
    """dream.sh sort déjà en 1 par `FAILED_PHASES` : rien à escalader ici."""
    manifest = _manifest(expected=(("connect", "brain-v42"),), failed=(("connect", "brain-v42"),))
    session = _session([])

    night = await post_run_alert.review_night(session, RUN_DATE, manifest=manifest)

    assert night.report is not None
    assert post_run_alert.MISSING_EXPECTED_MESSAGE in night.report
    assert night.coverage.escalates is False


@pytest.mark.asyncio
async def test_a_row_written_while_the_night_declared_failure_is_reported() -> None:
    """La nuit du 19→20, rejouée telle qu'elle s'est produite.

    `reorg`/`brain-v42` a été déclaré `failed` par dream.sh, mais son marquage
    `dream_runs` a crashé et la ligne est restée `done`. Le verdict lisait donc
    une couverture PLEINE en ayant sous les yeux un fichier d'entrée qui disait
    l'échec. Un rapport qui jette la déclaration de son propre fichier d'entrée
    est un faux-vert, pas une couverture.

    L'assertion porte sur `render_stdout`, pas sur le seul verdict : une nuit
    sans ligne en échec a `report is None`, donc un signal logé dans le corps du
    rapport n'atteindrait PERSONNE. Le bloc de couverture, lui, est imprimé
    toutes les nuits — c'est le seul endroit où ce signal existe vraiment.
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
    """Le compteur est TOUJOURS imprimé : une absence de ligne serait ambiguë."""
    manifest = _manifest(expected=(("scan", "red"),))
    session = _session(_rows((("scan", "red"),)))

    night = await post_run_alert.review_night(session, RUN_DATE, manifest=manifest)
    stdout = post_run_alert.render_stdout(night.report, RUN_DATE, night.coverage)

    assert "mismatch 0" in stdout
    assert post_run_alert.COVERAGE_MISMATCH_MESSAGE not in stdout
    assert "mismatch=0" in night.coverage.machine_line


@pytest.mark.asyncio
async def test_a_missing_declared_pair_is_not_counted_as_a_mismatch() -> None:
    """`declared` et `mismatch` ne doivent jamais compter la même paire."""
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
    """La ligne EXISTE — en synthétiser une seconde ferait un doublon."""
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
    """LE test anti-faux-positif de la couverture 2 → 6."""
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


# --- Structure douteuse : jamais de verdict vert -----------------------------


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


# --- Le repli : le chemin d'aujourd'hui, dit en toutes lettres ---------------


@pytest.mark.asyncio
async def test_without_a_manifest_the_synthesis_lines_are_the_ones_of_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contrat de non-régression porté sur `include_missing_expected_phases`.

    Pas sur les octets de stdout : le repli gagne explicitement un
    avertissement et une ligne machine. Promettre « byte-identique » tout en
    ajoutant des lignes serait un contrat que personne ne peut tenir.
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
    """23 paires attendues depuis le drop-in contre 62 écrites le 2026-08-18.

    `COVERAGE expected=23 written=62` reproduirait le défaut du ticket : deux
    nombres côte à côte que rien ne réconcilie.
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


# --- stdout : deux nombres côte à côte, chaque matin ------------------------


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
    """Contrat épinglé : aucune écriture, jamais (test_dream_post_run_alert.py:125)."""
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

    def _fake_run(run_date: dt.date, manifest_path: Path) -> int:
        seen["date"] = run_date
        seen["manifest"] = manifest_path
        return 0

    monkeypatch.setattr(post_run_alert.asyncio, "run", lambda coro: coro)
    monkeypatch.setattr(post_run_alert, "_run", _fake_run)

    assert post_run_alert.main(["--date", "2026-08-18", "--manifest", "/tmp/x.tsv"]) == 0
    assert seen["manifest"] == Path("/tmp/x.tsv")
    assert seen["date"] == RUN_DATE

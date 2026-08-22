"""Contrat du CLI de balayage : DRY par défaut, règle 4 h FERMÉE, rapport lisible.

Deux frontières de sûreté, pas une, et elles se composent : `--wet` décide si le
balayage ÉCRIT, `BRAIN_SESSION_INACTIVE_SWEEP_ENABLED` décide si la règle des 4 h
EXISTE. La seconde est neuve, et son défaut fermé n'est pas de la prudence de
forme : cette phase tourne WET toutes les nuits, en `uv run` depuis le dépôt.
Merger la règle sans drapeau l'armerait dès la nuit suivante, sans redémarrage
et sans fenêtre d'observation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.config import Settings
from brain_v42.models.brain_session import (
    AGENT_INACTIVE_AFTER,
    AUTO_STALE_ABANDONMENT_REASON,
    AUTO_STALE_AFTER,
    BrainSessionStatus,
    BrainSessionSweepCandidate,
    BrainSessionSweepResult,
)

NOW = datetime(2026, 8, 7, 6, 0, tzinfo=UTC)


def _candidate(index: int, outcome: BrainSessionStatus) -> BrainSessionSweepCandidate:
    inactive = outcome is BrainSessionStatus.CLOSED_INACTIVE
    return BrainSessionSweepCandidate(
        id=uuid4(),
        project_key=f"projet-{index}",
        client_key=f"codex-factory-{index}",
        last_heartbeat_at=NOW - timedelta(days=10 + index),
        last_observed_at=(NOW - timedelta(hours=5 + index)) if inactive else None,
        outcome=outcome,
    )


def _result(
    *,
    dry_run: bool,
    count: int = 2,
    inactive: int = 0,
    rule_armed: bool = False,
) -> BrainSessionSweepResult:
    candidates = [_candidate(index, BrainSessionStatus.ABANDONED) for index in range(count)]
    candidates += [
        _candidate(100 + index, BrainSessionStatus.CLOSED_INACTIVE) for index in range(inactive)
    ]
    return BrainSessionSweepResult(
        candidates=candidates,
        dry_run=dry_run,
        cutoff=NOW - AUTO_STALE_AFTER,
        inactive_cutoff=(NOW - AGENT_INACTIVE_AFTER) if rule_armed or inactive else None,
        abandoned_count=0 if dry_run else count,
        closed_inactive_count=0 if dry_run else inactive,
    )


def test_dry_is_the_default_mode() -> None:
    from brain_v42.maintenance.session_sweep import build_parser

    args = build_parser().parse_args([])

    assert args.wet is False


def test_threshold_default_comes_from_the_single_constant() -> None:
    from brain_v42.maintenance.session_sweep import build_parser

    args = build_parser().parse_args([])

    assert args.older_than_days == AUTO_STALE_AFTER.days == 7


def test_non_positive_threshold_is_refused() -> None:
    from brain_v42.maintenance.session_sweep import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--older-than-days", "0"])


def test_dry_report_says_would_and_never_says_did() -> None:
    from brain_v42.maintenance.session_sweep import render_report

    report = render_report(_result(dry_run=True))

    assert "DRY" in report
    assert "auraient reçu" in report
    assert "ont reçu" not in report
    assert "projet-0" in report and "projet-1" in report
    assert "2026-07-31" in report  # cutoff rendu, pas seulement le compte


def test_wet_report_states_what_was_written() -> None:
    from brain_v42.maintenance.session_sweep import render_report

    report = render_report(_result(dry_run=False))

    assert "WET" in report
    assert "2 sessions ont reçu" in report
    assert "auraient" not in report


def test_empty_sweep_is_reported_as_a_normal_night() -> None:
    from brain_v42.maintenance.session_sweep import render_report

    report = render_report(_result(dry_run=True, count=0))

    assert "aucune session à tarir" in report
    assert len(report.splitlines()) == 1, "aucune ligne de candidat"


class TestTheReportNeverMergesTheTwoOutcomes:
    """`abandoned` et `closed_inactive` sont deux faits, jamais un total."""

    def test_each_outcome_is_counted_and_named_separately(self) -> None:
        from brain_v42.maintenance.session_sweep import render_report

        report = render_report(_result(dry_run=False, count=1, inactive=2))

        assert "1 abandoned (7 j)" in report
        assert "2 closed_inactive (4 h)" in report
        # TÉMOIN : le total existe, mais jamais SEUL — « 3 sessions » sans la
        # ventilation se lirait « 3 abandons », et l'écart entre les deux règles
        # est précisément ce qu'on surveille.
        assert "3 sessions ont reçu" in report

    def test_every_line_names_the_outcome_it_received(self) -> None:
        from brain_v42.maintenance.session_sweep import render_report

        lines = render_report(_result(dry_run=False, count=1, inactive=1)).splitlines()[1:]

        assert [line.split()[0] for line in lines] == ["abandoned", "closed_inactive"]

    def test_a_never_observed_session_reads_never_not_a_date(self) -> None:
        """`NULL` doit se lire « jamais observée », jamais comme un blanc."""
        from brain_v42.maintenance.session_sweep import render_report

        report = render_report(_result(dry_run=False, count=1))

        assert "observed=never" in report

    def test_a_closed_rule_says_so_instead_of_reading_as_zero_findings(self) -> None:
        """Pas de plafond silencieux : « off » ≠ « aucune traçante inactive ».

        Sans cette ligne, une nuit à zéro fermeture se lirait « rien à fermer »
        alors que la règle n'a même pas été évaluée.
        """
        from brain_v42.maintenance.session_sweep import render_report

        assert "inactive_cutoff=off" in render_report(_result(dry_run=True, count=0))
        assert "inactive_cutoff=off" not in render_report(
            _result(dry_run=True, count=0, rule_armed=True)
        )


@pytest.mark.asyncio
async def test_record_dream_run_never_raises_when_the_database_is_down() -> None:
    from brain_v42.maintenance.session_sweep import record_dream_run

    def broken_factory():
        raise RuntimeError("base injoignable")

    await record_dream_run(
        broken_factory, "done", dry=True, duration_s=1.0, error=None
    )  # ne doit pas lever


async def _capture_sweep_call(
    monkeypatch: pytest.MonkeyPatch, args: object, *, rule_armed: bool = False
) -> dict[str, object]:
    """Exécuter `_run` en espionnant l'appel RÉEL à `sweep_open_sessions`.

    Retourne les kwargs effectivement reçus par la méthode (``older_than``,
    ``reason``, ``dry_run``, ``close_inactive_after``) plus le code retour —
    jamais une reconstruction indépendante de la f-string de l'implémentation.
    Partagé par tous les tests de frontière pour qu'ils espionnent identiquement
    le même chemin réel.
    """
    from brain_v42.maintenance.session_sweep import _run
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://b:b@localhost:5433/test")
    monkeypatch.setenv("BRAIN_SESSION_INACTIVE_SWEEP_ENABLED", "true" if rule_armed else "false")
    monkeypatch.setattr(
        "brain_v42.db.engine.get_session_factory", lambda: MagicMock(), raising=True
    )
    monkeypatch.setattr(
        "brain_v42.maintenance.session_sweep.record_dream_run", AsyncMock(), raising=True
    )

    captured: dict[str, object] = {}

    async def fake_sweep(
        self: PgBrainSessionRepo,
        *,
        older_than: timedelta,
        reason: str = AUTO_STALE_ABANDONMENT_REASON,
        close_inactive_after: timedelta | None = None,
        dry_run: bool = True,
        now: datetime | None = None,
    ) -> BrainSessionSweepResult:
        captured["older_than"] = older_than
        captured["reason"] = reason
        captured["dry_run"] = dry_run
        captured["close_inactive_after"] = close_inactive_after
        return BrainSessionSweepResult(
            candidates=[], dry_run=dry_run, cutoff=NOW, abandoned_count=0
        )

    monkeypatch.setattr(
        "brain_v42.repositories.pg_brain_session.PgBrainSessionRepo.sweep_open_sessions",
        fake_sweep,
        raising=True,
    )

    captured["rc"] = await _run(args)  # type: ignore[arg-type]
    return captured


@pytest.mark.asyncio
async def test_default_threshold_reason_matches_the_module_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Au seuil par défaut, le motif RÉELLEMENT transmis doit être la constante.

    Remplace l'ancien test décoratif qui comparait `AUTO_STALE_ABANDONMENT_REASON`
    à sa propre reconstruction (`f"auto_stale_{AUTO_STALE_AFTER.days}d"`) sans
    jamais toucher `session_sweep.py` — un typo dans le template de `_run`
    (ex. `f"auto_stale_{n}_days"`) l'aurait laissé passer. Celui-ci espionne
    l'appel réel à `sweep_open_sessions` : si `AUTO_STALE_AFTER` change un jour sans
    que `AUTO_STALE_ABANDONMENT_REASON` suive, le CLI émettrait `auto_stale_<N>d`
    alors que ce test attend encore la constante — et échouerait ici, à
    l'endroit qui compte.
    """
    from brain_v42.maintenance.session_sweep import build_parser

    args = build_parser().parse_args([])
    captured = await _capture_sweep_call(monkeypatch, args)

    assert captured["rc"] == 0
    assert captured["older_than"] == AUTO_STALE_AFTER
    assert captured["reason"] == AUTO_STALE_ABANDONMENT_REASON


@pytest.mark.asyncio
async def test_non_default_threshold_reason_reaches_the_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le motif écrit doit refléter le seuil RÉELLEMENT utilisé.

    Sans ça, `--older-than-days 30` écrirait `abandonment_reason='auto_stale_7d'`
    — un mensonge d'audit permanent, la trouvaille reportée de la Task 1. On
    espionne l'appel réel à `sweep_open_sessions`, pas notre propre f-string.
    """
    from brain_v42.maintenance.session_sweep import build_parser

    args = build_parser().parse_args(["--older-than-days", "30"])
    captured = await _capture_sweep_call(monkeypatch, args)

    assert captured["rc"] == 0
    assert captured["older_than"] == timedelta(days=30)
    assert captured["reason"] == "auto_stale_30d"
    assert captured["reason"] != AUTO_STALE_ABANDONMENT_REASON


@pytest.mark.asyncio
async def test_default_invocation_reaches_the_repository_in_dry_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`args.wet is False` ne prouve rien sur ce que reçoit le repository.

    Entre le drapeau et l'appel il y a une traduction — `dry = not args.wet` —
    et c'est elle, pas le défaut du parser, qui est l'unique frontière de sûreté
    de la phase : l'inverser fait abandonner pour de vrai des sessions sur une
    invocation sans `--wet`, et l'abandon est irréversible. Mesuré le 2026-08-07 :
    cette inversion survivait aux 6997 tests unitaires ET aux 256 d'intégration,
    parce que `_capture_sweep_call` capturait déjà `dry_run` sans que
    personne ne le relise. On espionne donc le kwarg réellement transmis.
    """
    from brain_v42.maintenance.session_sweep import build_parser

    args = build_parser().parse_args([])
    captured = await _capture_sweep_call(monkeypatch, args)

    assert captured["rc"] == 0
    assert captured["dry_run"] is True


@pytest.mark.asyncio
async def test_wet_flag_reaches_the_repository_as_a_real_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contrôle positif du test voisin.

    Sans lui, un `dry = True` codé en dur satisferait « DRY par défaut » tout en
    rendant `--wet` inopérant — le balayage ne ferait jamais rien et le soak
    paraîtrait propre indéfiniment. Les deux assertions se tuent par des
    mutations distinctes : `dry = False` tue celle du défaut, `dry = True` tue
    celle-ci.
    """
    from brain_v42.maintenance.session_sweep import build_parser

    args = build_parser().parse_args(["--wet"])
    captured = await _capture_sweep_call(monkeypatch, args)

    assert captured["rc"] == 0
    assert captured["dry_run"] is False


class TestTheInactivityRuleIsDeliveredClosed:
    """La seconde frontière de sûreté, et elle n'est pas celle de `--wet`."""

    def test_the_flag_default_is_false(self) -> None:
        assert Settings.model_fields["brain_session_inactive_sweep_enabled"].default is False

    @pytest.mark.asyncio
    async def test_a_closed_flag_sends_no_threshold_at_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`None`, jamais `timedelta(0)` : la règle n'EXISTE pas, elle n'est pas nulle.

        Un zéro rendrait toute traçante éligible à l'instant même. Les deux
        valeurs se ressemblent à la lecture et n'ont rien en commun à
        l'exécution — d'où l'assertion sur l'identité, pas sur la véracité.
        """
        from brain_v42.maintenance.session_sweep import build_parser

        args = build_parser().parse_args(["--wet"])
        captured = await _capture_sweep_call(monkeypatch, args, rule_armed=False)

        assert captured["rc"] == 0
        assert captured["close_inactive_after"] is None

    @pytest.mark.asyncio
    async def test_an_armed_flag_sends_the_single_constant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Contrôle positif : sans lui, un `None` codé en dur passerait le test voisin.

        Le seuil transmis est LU de `AGENT_INACTIVE_AFTER`, jamais recopié : deux
        exemplaires d'un même seuil, c'est le défaut de classe du learning
        8dc7e042, et celui-ci est déjà écrit dans le modèle et dans l'ADR.
        """
        from brain_v42.maintenance.session_sweep import build_parser

        args = build_parser().parse_args(["--wet"])
        captured = await _capture_sweep_call(monkeypatch, args, rule_armed=True)

        assert captured["rc"] == 0
        assert captured["close_inactive_after"] == AGENT_INACTIVE_AFTER
        assert AGENT_INACTIVE_AFTER == timedelta(hours=4)

    @pytest.mark.asyncio
    async def test_arming_the_rule_does_not_arm_writing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Les deux frontières se composent, elles ne se remplacent pas.

        Armer la règle sans `--wet` doit rester un DRY : sinon le geste
        d'observation deviendrait lui-même le geste d'écriture, et la fenêtre
        d'observation n'existerait pas.
        """
        from brain_v42.maintenance.session_sweep import build_parser

        captured = await _capture_sweep_call(
            monkeypatch, build_parser().parse_args([]), rule_armed=True
        )

        assert captured["dry_run"] is True
        assert captured["close_inactive_after"] == AGENT_INACTIVE_AFTER

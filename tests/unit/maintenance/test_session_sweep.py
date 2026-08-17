"""Contrat du CLI de balayage : DRY par défaut, seuil non dupliqué, rapport lisible."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.models.brain_session import (
    AUTO_STALE_ABANDONMENT_REASON,
    AUTO_STALE_AFTER,
    BrainSessionSweepCandidate,
    BrainSessionSweepResult,
)

NOW = datetime(2026, 8, 7, 6, 0, tzinfo=UTC)


def _result(*, dry_run: bool, count: int = 2) -> BrainSessionSweepResult:
    candidates = [
        BrainSessionSweepCandidate(
            id=uuid4(),
            project_key=f"projet-{index}",
            client_key=f"codex-factory-{index}",
            last_heartbeat_at=NOW - timedelta(days=10 + index),
        )
        for index in range(count)
    ]
    return BrainSessionSweepResult(
        candidates=candidates,
        dry_run=dry_run,
        cutoff=NOW - AUTO_STALE_AFTER,
        abandoned_count=0 if dry_run else count,
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


def test_dry_report_says_would_and_never_says_abandoned() -> None:
    from brain_v42.maintenance.session_sweep import render_report

    report = render_report(_result(dry_run=True))

    assert "DRY" in report
    assert "auraient été abandonnées" in report
    assert "ont été abandonnées" not in report
    assert "projet-0" in report and "projet-1" in report
    assert "2026-07-31" in report  # cutoff rendu, pas seulement le compte


def test_wet_report_states_what_was_written() -> None:
    from brain_v42.maintenance.session_sweep import render_report

    report = render_report(_result(dry_run=False))

    assert "WET" in report
    assert "2 sessions ont été abandonnées" in report
    assert "auraient" not in report


def test_empty_sweep_is_reported_as_a_normal_night() -> None:
    from brain_v42.maintenance.session_sweep import render_report

    report = render_report(_result(dry_run=True, count=0))

    assert "aucune session à abandonner" in report
    assert len(report.splitlines()) == 1, "aucune ligne de candidat"


@pytest.mark.asyncio
async def test_record_dream_run_never_raises_when_the_database_is_down() -> None:
    from brain_v42.maintenance.session_sweep import record_dream_run

    def broken_factory():
        raise RuntimeError("base injoignable")

    await record_dream_run(
        broken_factory, "done", dry=True, duration_s=1.0, error=None
    )  # ne doit pas lever


async def _capture_abandon_stale_call(
    monkeypatch: pytest.MonkeyPatch, args: object
) -> dict[str, object]:
    """Exécuter `_run` en espionnant l'appel RÉEL à `abandon_stale`.

    Retourne les kwargs effectivement reçus par la méthode (``older_than``,
    ``reason``, ``dry_run``) plus le code retour — jamais une reconstruction
    indépendante de la f-string de l'implémentation. Partagé par les deux
    tests de seuil (défaut et non-défaut) pour qu'ils espionnent identiquement
    le même chemin réel.
    """
    from brain_v42.maintenance.session_sweep import _run
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://b:b@localhost:5433/test")
    monkeypatch.setattr(
        "brain_v42.db.engine.get_session_factory", lambda: MagicMock(), raising=True
    )
    monkeypatch.setattr(
        "brain_v42.maintenance.session_sweep.record_dream_run", AsyncMock(), raising=True
    )

    captured: dict[str, object] = {}

    async def fake_abandon_stale(
        self: PgBrainSessionRepo,
        *,
        older_than: timedelta,
        reason: str = AUTO_STALE_ABANDONMENT_REASON,
        dry_run: bool = True,
        now: datetime | None = None,
    ) -> BrainSessionSweepResult:
        captured["older_than"] = older_than
        captured["reason"] = reason
        captured["dry_run"] = dry_run
        return BrainSessionSweepResult(
            candidates=[], dry_run=dry_run, cutoff=NOW, abandoned_count=0
        )

    monkeypatch.setattr(
        "brain_v42.repositories.pg_brain_session.PgBrainSessionRepo.abandon_stale",
        fake_abandon_stale,
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
    l'appel réel à `abandon_stale` : si `AUTO_STALE_AFTER` change un jour sans
    que `AUTO_STALE_ABANDONMENT_REASON` suive, le CLI émettrait `auto_stale_<N>d`
    alors que ce test attend encore la constante — et échouerait ici, à
    l'endroit qui compte.
    """
    from brain_v42.maintenance.session_sweep import build_parser

    args = build_parser().parse_args([])
    captured = await _capture_abandon_stale_call(monkeypatch, args)

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
    espionne l'appel réel à `abandon_stale`, pas notre propre f-string.
    """
    from brain_v42.maintenance.session_sweep import build_parser

    args = build_parser().parse_args(["--older-than-days", "30"])
    captured = await _capture_abandon_stale_call(monkeypatch, args)

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
    parce que `_capture_abandon_stale_call` capturait déjà `dry_run` sans que
    personne ne le relise. On espionne donc le kwarg réellement transmis.
    """
    from brain_v42.maintenance.session_sweep import build_parser

    args = build_parser().parse_args([])
    captured = await _capture_abandon_stale_call(monkeypatch, args)

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
    captured = await _capture_abandon_stale_call(monkeypatch, args)

    assert captured["rc"] == 0
    assert captured["dry_run"] is False

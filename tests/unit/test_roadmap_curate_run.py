"""Unit tests for scripts.roadmap_curate._run orchestration (mocked, no DB/LLM).

Nuit 2026-07-05 : SIGTERM à 20 m en plein batch 7/10 — l'apply terminal
n'a jamais tourné (24 proposals bloquées 'proposed', 0 appliquée) et
record_dream_run n'a pas écrit de row. Contrats testés ici :
- apply PAR BATCH (un SIGTERM ne perd que le batch en vol) ;
- budget nuit : plus aucun nouveau batch après NIGHT_BUDGET_S, fin propre
  (record_dream_run écrit, rc=0, la rotation resservira les projets) ;
- merges retenus par le juge : persistés 'proposed', JAMAIS auto-appliqués.
"""

from __future__ import annotations

import argparse
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from scripts import roadmap_curate as rc
from scripts.roadmap_curate import (
    BatchOutcome,
    CurationDraft,
    FeatureCard,
    PersistResult,
    ProjectBatch,
)

from brain_v42.dream_degradation import DEGRADED_PREFIX


def _mk_batch(key: str) -> ProjectBatch:
    return ProjectBatch(
        project_key=key,
        features=[
            FeatureCard(id=uuid4(), name=f"{key}-A", status="research", pinned=False),
            FeatureCard(id=uuid4(), name=f"{key}-B", status="research", pinned=False),
        ],
    )


def _archive_draft(batch: ProjectBatch) -> CurationDraft:
    return CurationDraft(op="archive", feature_id=batch.features[0].id, payload={}, rationale="r")


def _merge_draft(batch: ProjectBatch) -> CurationDraft:
    return CurationDraft(
        op="merge",
        feature_id=batch.features[0].id,
        payload={"into": str(batch.features[1].id)},
        rationale="r",
    )


def _args(**over: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "limit": 10,
        "wet": True,
        "apply_ids": None,
        "model": "test-model",
        "base_url": "https://mock.nvidia.local/v1",
        "budget_seconds": rc.NIGHT_BUDGET_S,
    }
    base.update(over)
    return argparse.Namespace(**base)


class _Clock:
    """Horloge injectable : rend les valeurs de la liste puis répète la dernière."""

    def __init__(self, values: list[float]):
        self._values = list(values)

    def __call__(self) -> float:
        if len(self._values) > 1:
            return self._values.pop(0)
        return self._values[0]


@pytest.fixture
def run_mocks(monkeypatch):
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://b:b@localhost:5433/test")
    monkeypatch.setattr(
        "brain_v42.db.engine.get_session_factory", lambda: MagicMock(), raising=True
    )
    mocks = {
        "record": AsyncMock(),
        "apply": AsyncMock(return_value=1),
        "judge": AsyncMock(return_value=set()),
    }
    monkeypatch.setattr(rc, "record_dream_run", mocks["record"])
    monkeypatch.setattr(rc, "apply_proposals", mocks["apply"])
    monkeypatch.setattr(rc, "judge_merges", mocks["judge"])
    return mocks


class TestFairShareWindow:
    @pytest.mark.asyncio
    async def test_curate_batch_receives_fair_share_window(self, run_mocks, monkeypatch):
        """Chaque batch reçoit une fenêtre LLM fair-share du budget restant —
        un gros projet ne peut plus manger la part des suivants (nuit
        2026-07-10 : red 383s → budget épuisé au 5e projet, 5 reportés)."""
        batches = [_mk_batch("p1"), _mk_batch("p2"), _mk_batch("p3")]
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=batches))

        windows: list[float] = []

        async def fake_curate(client, model, batch, **kw):
            windows.append(kw["llm_timeout_s"])
            return BatchOutcome(batch=batch, drafts=[_archive_draft(batch)], model_used=model)

        monkeypatch.setattr(rc, "curate_batch", fake_curate)
        inserted = iter([[101], [102], [103]])
        monkeypatch.setattr(
            rc,
            "persist_proposals",
            AsyncMock(side_effect=lambda sf, d: PersistResult(inserted=next(inserted))),
        )
        # t0=0 ; checks avant batch : 0, 400, 600 ; puis durée finale.
        clock = _Clock([0.0, 0.0, 400.0, 600.0, 700.0])

        rcode = await rc._run(
            _args(),
            api_key="k",
            model=rc.DEFAULT_WET_ROADMAP_MODEL,
            base_url="https://mock.nvidia.local/v1",
            clock=clock,
        )

        assert rcode == 0
        # b1 : (720-0)/3 = 240 → /2 = 120 ; b2 : (720-400)/2 = 160 → 80 ;
        # b3 : (720-600)/1 = 120 → 60 (= plancher MIN_LLM_WINDOW_S).
        assert windows == [120.0, 80.0, 60.0]


class TestBudgetGuard:
    @pytest.mark.asyncio
    async def test_budget_stops_new_batches_cleanly(self, run_mocks, monkeypatch, capsys):
        """Après NIGHT_BUDGET_S, plus AUCUN nouveau batch : fin propre,
        record_dream_run écrit (status done), rc=0 — pas un échec."""
        batches = [_mk_batch("p1"), _mk_batch("p2"), _mk_batch("p3")]
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=batches))

        async def fake_curate(client, model, batch, **kw):
            return BatchOutcome(batch=batch, drafts=[_archive_draft(batch)], model_used=model)

        monkeypatch.setattr(rc, "curate_batch", fake_curate)
        inserted = iter([[101], [102], [103]])
        monkeypatch.setattr(
            rc,
            "persist_proposals",
            AsyncMock(side_effect=lambda sf, d: PersistResult(inserted=next(inserted))),
        )
        # t0=0 ; checks avant batch : 0 (b1 passe), 400 (b2 passe), 800 (> 720
        # → stop) ; puis durée finale.
        clock = _Clock([0.0, 0.0, 400.0, 800.0, 810.0])

        rcode = await rc._run(
            _args(),
            api_key="k",
            model=rc.DEFAULT_WET_ROADMAP_MODEL,
            base_url="https://mock.nvidia.local/v1",
            clock=clock,
        )

        assert rcode == 0
        assert run_mocks["apply"].await_count == 2  # b1 et b2 seulement
        run_mocks["record"].assert_awaited_once()
        assert run_mocks["record"].await_args.kwargs.get("status", "done") == "done" or (
            "done" in run_mocks["record"].await_args.args
        )
        out = capsys.readouterr().out
        assert "budget" in out.lower()
        assert "1 projet" in out  # p3 non traité, annoncé — pas de drop silencieux

    @pytest.mark.asyncio
    async def test_apply_runs_per_batch_not_terminally(self, run_mocks, monkeypatch):
        """L'apply wet court APRÈS CHAQUE batch avec les ids de CE batch —
        plus d'apply terminal (SIGTERM 2026-07-05 : 24 proposals jamais
        appliquées)."""
        batches = [_mk_batch("p1"), _mk_batch("p2")]
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=batches))

        async def fake_curate(client, model, batch, **kw):
            return BatchOutcome(batch=batch, drafts=[_archive_draft(batch)], model_used=model)

        monkeypatch.setattr(rc, "curate_batch", fake_curate)
        inserted = iter([[11], [22]])
        monkeypatch.setattr(
            rc,
            "persist_proposals",
            AsyncMock(side_effect=lambda sf, d: PersistResult(inserted=next(inserted))),
        )

        rcode = await rc._run(
            _args(),
            api_key="k",
            model=rc.DEFAULT_WET_ROADMAP_MODEL,
            base_url="https://mock.nvidia.local/v1",
            clock=_Clock([0.0]),
        )

        assert rcode == 0
        ids_per_call = [c.args[1] for c in run_mocks["apply"].await_args_list]
        assert ids_per_call == [[11], [22]]


class TestJudgeGateInRun:
    @pytest.mark.asyncio
    async def test_missing_model_provenance_is_never_auto_applied(self, run_mocks, monkeypatch):
        batch = _mk_batch("p1")
        draft = _archive_draft(batch)
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=[batch]))

        async def fake_curate(client, model, b, **kw):
            return BatchOutcome(batch=b, drafts=[draft], model_used=None)

        monkeypatch.setattr(rc, "curate_batch", fake_curate)
        monkeypatch.setattr(
            rc, "persist_proposals", AsyncMock(return_value=PersistResult(inserted=[10]))
        )

        rcode = await rc._run(
            _args(wet=True),
            api_key="k",
            model=rc.DEFAULT_WET_ROADMAP_MODEL,
            base_url="https://mock.nvidia.local/v1",
            clock=_Clock([0.0]),
        )

        assert rcode == 0
        run_mocks["apply"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unreviewed_fallback_is_persisted_but_never_auto_applied(
        self, run_mocks, monkeypatch
    ):
        """Tout fallback hors allowlist reste review-only même si le run est wet."""
        batch = _mk_batch("p1")
        draft = _merge_draft(batch)
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=[batch]))

        async def fake_curate(client, model, b, **kw):
            return BatchOutcome(
                batch=b,
                drafts=[draft],
                model_used="unreviewed-fallback",
                fallback_used=True,
            )

        monkeypatch.setattr(rc, "curate_batch", fake_curate)
        persist = AsyncMock(return_value=PersistResult(inserted=[9]))
        monkeypatch.setattr(rc, "persist_proposals", persist)

        rcode = await rc._run(
            _args(wet=True),
            api_key="k",
            model=rc.DEFAULT_WET_ROADMAP_MODEL,
            base_url="https://mock.nvidia.local/v1",
            clock=_Clock([0.0]),
        )

        assert rcode == 0
        assert persist.await_args.args[1] == [draft]
        run_mocks["judge"].assert_not_awaited()
        run_mocks["apply"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_held_merges_persisted_but_never_applied(self, run_mocks, monkeypatch, capsys):
        """Merge retenu par le juge → persisté 'proposed' (review du matin),
        exclu des ids d'apply. Le status non-merge du même batch s'applique."""
        batch = _mk_batch("p1")
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=[batch]))
        merge = _merge_draft(batch)
        keep = CurationDraft(
            op="status",
            feature_id=batch.features[1].id,
            payload={"status": "deployed"},
            rationale="r",
        )

        async def fake_curate(client, model, b, **kw):
            return BatchOutcome(
                batch=b,
                drafts=[merge, keep],
                model_used=rc.DEFAULT_WET_ROADMAP_FALLBACK_MODEL,
                fallback_used=True,
            )

        monkeypatch.setattr(rc, "curate_batch", fake_curate)
        run_mocks["judge"].return_value = {0}  # l'unique merge est retenu
        persist_calls: list[list[CurationDraft]] = []
        results = iter([PersistResult(inserted=[7]), PersistResult(inserted=[8])])

        async def fake_persist(sf, drafts):
            persist_calls.append(list(drafts))
            return next(results) if drafts else PersistResult()

        monkeypatch.setattr(rc, "persist_proposals", fake_persist)

        rcode = await rc._run(
            _args(),
            api_key="k",
            model=rc.DEFAULT_WET_ROADMAP_MODEL,
            base_url="https://mock.nvidia.local/v1",
            clock=_Clock([0.0]),
        )

        assert rcode == 0
        # Le juge n'a vu QUE les merges.
        judged = run_mocks["judge"].await_args.args[3]
        assert judged == [merge]
        assert run_mocks["judge"].await_args.args[1] == rc.DEFAULT_WET_ROADMAP_FALLBACK_MODEL
        # Persist 1 : drafts applicables (sans le merge) ; persist 2 : retenus.
        assert persist_calls[0] == [keep]
        assert persist_calls[1] == [merge]
        # Apply : ids du persist applicable uniquement — jamais le retenu.
        ids_applied = [c.args[1] for c in run_mocks["apply"].await_args_list]
        assert ids_applied == [[7]]
        assert "retenu" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_dry_mode_skips_judge_and_apply(self, run_mocks, monkeypatch):
        batch = _mk_batch("p1")
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=[batch]))

        async def fake_curate(client, model, b, **kw):
            return BatchOutcome(batch=b, drafts=[_merge_draft(b)], model_used=model)

        monkeypatch.setattr(rc, "curate_batch", fake_curate)
        monkeypatch.setattr(
            rc, "persist_proposals", AsyncMock(return_value=PersistResult(inserted=[5]))
        )

        rcode = await rc._run(
            _args(wet=False),
            api_key="k",
            model=rc.DEFAULT_WET_ROADMAP_MODEL,
            base_url="https://mock.nvidia.local/v1",
            clock=_Clock([0.0]),
        )

        assert rcode == 0
        run_mocks["judge"].assert_not_awaited()
        run_mocks["apply"].assert_not_awaited()


class TestFallbackDegradationIsReported:
    """Une nuit entièrement servie par le secours doit se voir sans lire les logs bruts.

    2026-08-05 : dix nuits consécutives à 100 % de secours 8B, toutes `done`,
    toutes `8/8 phases OK`. Le run reste `done` — le passer `fail` referait
    l'erreur que 4480d3df vient de corriger — mais il cesse d'être muet.
    """

    @pytest.mark.asyncio
    async def test_full_fallback_run_prints_degradation_and_cause(
        self, run_mocks, monkeypatch, capsys
    ):
        batches = [_mk_batch("p1"), _mk_batch("p2")]
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=batches))

        async def fake_curate(client, model, b, **kw):
            return BatchOutcome(
                batch=b,
                drafts=[],
                model_used=rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
                fallback_used=True,
                primary_error=f"{rc.MODEL_GONE_MARKER} — HTTP 410 end of life",
            )

        monkeypatch.setattr(rc, "curate_batch", fake_curate)
        monkeypatch.setattr(rc, "persist_proposals", AsyncMock(return_value=PersistResult()))

        rcode = await rc._run(
            _args(wet=False),
            api_key="k",
            model=rc.DEFAULT_ROADMAP_MODEL,
            base_url="https://mock.nvidia.local/v1",
            clock=_Clock([0.0]),
        )

        out = capsys.readouterr().out
        assert rcode == 0
        assert "2/2" in out
        assert rc.MODEL_GONE_MARKER in out
        assert rc.DEFAULT_ROADMAP_MODEL in out

    @pytest.mark.asyncio
    async def test_nominal_run_stays_silent_about_fallback(self, run_mocks, monkeypatch, capsys):
        """Pas de bruit quand le primaire sert : l'alarme doit rester rare."""
        batches = [_mk_batch("p1")]
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=batches))

        async def fake_curate(client, model, b, **kw):
            return BatchOutcome(batch=b, drafts=[], model_used=model, fallback_used=False)

        monkeypatch.setattr(rc, "curate_batch", fake_curate)
        monkeypatch.setattr(rc, "persist_proposals", AsyncMock(return_value=PersistResult()))

        await rc._run(
            _args(wet=False),
            api_key="k",
            model=rc.DEFAULT_ROADMAP_MODEL,
            base_url="https://mock.nvidia.local/v1",
            clock=_Clock([0.0]),
        )

        assert "secours" not in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_dream_run_records_the_model_actually_used(self, run_mocks, monkeypatch):
        """dream_runs.model était NULL pour roadmap : la table qui sert le
        briefing ignorait quel modèle avait tourné."""
        batches = [_mk_batch("p1")]
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=batches))

        async def fake_curate(client, model, b, **kw):
            return BatchOutcome(
                batch=b,
                drafts=[],
                model_used=rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
                fallback_used=True,
                primary_error="primaire mort",
            )

        monkeypatch.setattr(rc, "curate_batch", fake_curate)
        monkeypatch.setattr(rc, "persist_proposals", AsyncMock(return_value=PersistResult()))

        await rc._run(
            _args(wet=False),
            api_key="k",
            model=rc.DEFAULT_ROADMAP_MODEL,
            base_url="https://mock.nvidia.local/v1",
            clock=_Clock([0.0]),
        )

        kwargs = run_mocks["record"].await_args.kwargs
        assert kwargs["status"] == "done"
        assert kwargs["model"] == rc.DEFAULT_ROADMAP_FALLBACK_MODEL


class TestBudgetSecondsArg:
    @pytest.fixture
    def capture_args(self, monkeypatch):
        seen: dict[str, Any] = {}

        async def fake_run(args, api_key, model, base_url, **kw):
            seen["args"] = args
            seen["model"] = model
            seen["fallback_model"] = kw.get("fallback_model")
            return 0

        monkeypatch.setattr(rc, "_run", fake_run)
        monkeypatch.setattr(rc, "load_env_file", lambda p: None)
        monkeypatch.setenv("BRAIN_NVIDIA_API_KEY", "k")
        return seen

    def test_default_is_night_budget(self, capture_args, monkeypatch):
        monkeypatch.setattr("sys.argv", ["roadmap_curate", "--limit", "3"])
        assert rc.main() == 0
        assert capture_args["args"].budget_seconds == rc.NIGHT_BUDGET_S

    def test_explicit_budget_seconds(self, capture_args, monkeypatch):
        monkeypatch.setattr("sys.argv", ["roadmap_curate", "--budget-seconds", "300"])
        assert rc.main() == 0
        assert capture_args["args"].budget_seconds == 300.0

    def test_default_model_is_the_live_canaryed_primary(self, capture_args, monkeypatch):
        """Troisième primaire en trois semaines : chacun est mort chez le fournisseur.

        qwen3-next-80b a atteint son EOL le 2026-07-27, remplacé par
        deepseek-v4-flash après canary du 2026-08-05 (3/3 valides, 16,6 s/batch).
        deepseek-v4-flash est mort à son tour le 2026-08-07, découvert le 08-16.

        Remplacé par mistral-nemotron sur DEUX mesures, parce que la première
        a failli faire choisir le mauvais candidat :

        - Vitesse et forme : 3/3 valides, 12-20 s/batch, soit 126-204 s sur les
          dix projets contre 720 s de budget. Le snapshot daté de la famille
          morte, deepseek-v4-flash-0731, est aussi 3/3 valides mais à 69,3 s —
          693 s, 96 % du budget, et QUATRE FOIS plus lent que l'alias qu'il
          remplace. Un pin daté n'hérite pas du profil de son alias.

        - Qualité du contenu, qui a renversé le classement. Le compte de
          propositions ne classe rien : sur trois runs des mêmes batches,
          mistral-nemotron a rendu 31 puis 21, gpt-oss-20b 29 puis 13, et le
          8B 28/30/29. Jugement en aveugle du contenu : mistral-nemotron
          48/100, gpt-oss-20b 35, llama-3.1-8b 10.

        Le secours 8B reste secours. Il est le plus RAPIDE et le PIRE sur le
        fond — 9 rationales vides, 2 merges vers une cible qu'il archive dans
        le même lot, et sept runs orchestrator fondus dans le plus ancien
        d'entre eux. Le promouvoir aurait aussi effondré la chaîne à un maillon
        (voir tests/unit/test_roadmap_model_chain.py).

        SECOURS REMPLACÉ le 2026-08-29 : le 8B a atteint sa fin de vie le
        2026-08-26 (410 mesuré par la sonde ET par les nuits des 27 et 28,
        toutes deux en fail). Remplaçant : openai/gpt-oss-20b, re-mesuré DANS
        SON RÉGIME EXACT après correction de l'instrument (fenêtres de nuit
        60 s, dix batches réels, caps secours FALLBACK_*) : 10/10 portés,
        12 propositions, 7,8 s/batch — et 35/100 en jugement aveugle contre
        10/100 pour le mort. Le profil secours n'est pas « réduit » : mêmes
        3 features, tokens DOUBLÉS (1024), ce qui évite la troncature de
        raisonnement qui coûtait 74,5 s/batch sous l'ancien instrument à
        512 tokens. Écartés au même régime : nano-30b (9,9 s/batch mais 5/10
        JSON valides) ; deepseek-v4-flash-0731 (69,3 s/batch le 08-16,
        famille morte deux fois en un mois, contenu jamais jugé).

        Les assertions comparent aux CONSTANTES : ce test prouve le ROUTAGE
        (le défaut atteint curate), pas l'identité du modèle — la forme de la
        chaîne vit dans test_roadmap_model_chain, l'historique du choix dans
        le commentaire des constantes.
        """
        monkeypatch.delenv("BRAIN_NVIDIA_ROADMAP_MODEL", raising=False)
        monkeypatch.delenv("BRAIN_NVIDIA_MODEL", raising=False)
        monkeypatch.setattr("sys.argv", ["roadmap_curate"])

        assert rc.main() == 0

        assert capture_args["model"] == rc.DEFAULT_ROADMAP_MODEL
        assert capture_args["fallback_model"] == rc.DEFAULT_ROADMAP_FALLBACK_MODEL

    def test_dry_primary_can_never_auto_apply(self, capture_args, monkeypatch):
        """Le primaire DRY doit rester hors allowlist : un modèle non canaryé
        pour le wet ne doit jamais devenir applicable par un simple swap."""
        assert rc.DEFAULT_ROADMAP_MODEL not in rc.AUTO_APPLY_MODELS
        assert rc.DEFAULT_ROADMAP_MODEL in rc.PROPOSER_ONLY_MODELS

    def test_legacy_global_model_does_not_override_roadmap_default(self, capture_args, monkeypatch):
        monkeypatch.setenv("BRAIN_NVIDIA_MODEL", "legacy-global")
        monkeypatch.delenv("BRAIN_NVIDIA_ROADMAP_MODEL", raising=False)
        monkeypatch.setattr("sys.argv", ["roadmap_curate"])

        assert rc.main() == 0

        assert capture_args["model"] == rc.DEFAULT_ROADMAP_MODEL

    def test_roadmap_model_env_overrides_default(self, capture_args, monkeypatch):
        monkeypatch.setenv("BRAIN_NVIDIA_ROADMAP_MODEL", "roadmap-reviewed")
        monkeypatch.setattr("sys.argv", ["roadmap_curate"])

        assert rc.main() == 0

        assert capture_args["model"] == "roadmap-reviewed"

    def test_a_fallback_equal_to_the_primary_warns_about_the_one_link_chain(
        self, capture_args, monkeypatch, capsys
    ):
        """curate_batch traite secours==primaire comme AUCUN secours, en silence.

        Le cas n'arrive que par override env (les constantes sont gardées
        distinctes par test_roadmap_model_chain) — et c'est la config que
        deploy/nvidia.env.example a portée en exemple pendant des semaines.
        Une chaîne à un maillon qui se croit à deux doit faire du bruit.
        """
        monkeypatch.setenv("BRAIN_NVIDIA_ROADMAP_FALLBACK_MODEL", rc.DEFAULT_ROADMAP_MODEL)
        monkeypatch.setattr("sys.argv", ["roadmap_curate"])

        assert rc.main() == 0

        assert "UN seul maillon" in capsys.readouterr().out

    def test_fallback_model_env_overrides_default(self, capture_args, monkeypatch):
        monkeypatch.setenv("BRAIN_NVIDIA_ROADMAP_FALLBACK_MODEL", "fallback-model")
        monkeypatch.setattr("sys.argv", ["roadmap_curate"])

        assert rc.main() == 0

        assert capture_args["fallback_model"] == "fallback-model"

    def test_cli_model_overrides_roadmap_env(self, capture_args, monkeypatch):
        monkeypatch.setenv("BRAIN_NVIDIA_ROADMAP_MODEL", "roadmap-fast")
        monkeypatch.setattr("sys.argv", ["roadmap_curate", "--model", "cli-model"])

        assert rc.main() == 0

        assert capture_args["model"] == "cli-model"

    def test_default_wet_model_is_reviewed_and_keeps_auto_apply(self, capture_args, monkeypatch):
        """PAIRE WET REMPLACÉE le 2026-08-29 : llama-3.3-70b est mort en 410.

        Fin de vie mesurée entre les nuits du 27 (extract done) et du 28
        (extract fail 410) — un maillon DORMANT côté roadmap, puisque la phase
        tourne en DRY. Le secours d'hier devient primaire et gpt-oss-120b
        prend le poste de secours : des maillons VIVANTS, pas une paire prête
        à armer. Mesuré le 2026-08-29 au régime WET réel — la voie non-gérée
        DIVISE la fenêtre par deux dès qu'un secours existe (30 s à dix
        projets ; la borne n'est PAS le read-timeout httpx de 180 s) : la
        paire telle qu'ordonnée ne porte pas (super-120b 1/10, 9 sauvés par
        le secours). Réarmer WET exige un canary sous 30 s d'abord — le
        commentaire des constantes porte la précondition complète.
        """
        monkeypatch.delenv("BRAIN_NVIDIA_ROADMAP_MODEL", raising=False)
        monkeypatch.delenv("BRAIN_NVIDIA_MODEL", raising=False)
        monkeypatch.setattr("sys.argv", ["roadmap_curate", "--wet"])

        assert rc.main() == 0

        assert capture_args["model"] == rc.DEFAULT_WET_ROADMAP_MODEL
        assert capture_args["fallback_model"] == rc.DEFAULT_WET_ROADMAP_FALLBACK_MODEL
        assert capture_args["args"].wet is True

    def test_explicit_fallback_model_forces_proposer_only(self, capture_args, monkeypatch, capsys):
        monkeypatch.setenv("BRAIN_NVIDIA_ROADMAP_MODEL", rc.DEFAULT_ROADMAP_FALLBACK_MODEL)
        monkeypatch.setattr("sys.argv", ["roadmap_curate", "--wet"])

        assert rc.main() == 0

        assert capture_args["args"].wet is False
        assert "proposer-only" in capsys.readouterr().out

    def test_unreviewed_model_forces_review_only(self, capture_args, monkeypatch, capsys):
        monkeypatch.setenv("BRAIN_NVIDIA_ROADMAP_MODEL", "unknown-large-model")
        monkeypatch.setattr("sys.argv", ["roadmap_curate", "--wet"])

        assert rc.main() == 0

        assert capture_args["args"].wet is False
        assert "review-only" in capsys.readouterr().out

    def test_reviewed_fallback_model_keeps_wet(self, capture_args, monkeypatch):
        monkeypatch.setenv("BRAIN_NVIDIA_ROADMAP_MODEL", rc.DEFAULT_WET_ROADMAP_FALLBACK_MODEL)
        monkeypatch.setattr("sys.argv", ["roadmap_curate", "--wet"])

        assert rc.main() == 0

        assert capture_args["args"].wet is True
        assert capture_args["fallback_model"] == rc.DEFAULT_WET_ROADMAP_MODEL


class TestDegradationNotice:
    """Le préfixe de dégradation est un CONTRAT entre deux processus.

    `scripts/roadmap_curate.py` l'écrit dans `dream_runs.error_message` ; le
    briefing (`DreamRunService`) le relit pour refuser de compter la nuit
    comme propre. Rien ne les tient d'accord à part ce préfixe, et il n'y a
    aucun backfill : une divergence rend les lignes passées muettes.
    """

    def test_the_degraded_prefix_literal_is_frozen(self):
        """Quatre nuits déjà en base en dépendent — 08-06, 08-08, 08-09, 08-10.

        Elles portent le préfixe ACCENTUÉ. Le désaccentuer ou le traduire
        n'orphelinerait pas seulement ces lignes : il les rendrait
        invisibles au lecteur sans qu'aucune écriture n'échoue.
        """
        assert DEGRADED_PREFIX == "DÉGRADÉ"

    def test_the_notice_is_built_from_the_shared_prefix(self, monkeypatch):
        """Prouve l'USAGE, pas l'import.

        Un `rc.DEGRADED_PREFIX is DEGRADED_PREFIX` resterait vrai avec le
        littéral réinliné dans la f-string : il prouverait qu'un import
        existe, jamais qu'il sert. Seule la substitution mord.
        """
        monkeypatch.setattr(rc, "DEGRADED_PREFIX", "ZZZTEST")

        notice = rc._degradation_notice("primaire-mort", 10, 10, ["410 Gone"])

        assert notice is not None
        assert notice.startswith("ZZZTEST")

    def test_a_nominal_run_says_nothing(self):
        """Le cas nominal est MUET : aucun batch servi par le secours."""
        assert rc._degradation_notice("primaire-vivant", 0, 10, []) is None

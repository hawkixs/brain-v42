#!/usr/bin/env python
"""Canary apparié : chaque modèle candidat sur les MÊMES batches réels.

Pourquoi ce script existe, et pourquoi une sonde de vivacité ne suffit pas :
le 2026-08-11, une sonde à 16 tokens a donné `minimaxai/minimax-m3` vivant en
2,4 s. Le canary du 2026-08-05, lui, l'avait déjà mesuré en TIMEOUT sur le vrai
prompt — le commentaire de scripts/roadmap_curate.py:60-67 en garde la trace.
Un modèle qui répond « ALIVE » à seize tokens peut mourir sur un prompt
consolidateur de plusieurs milliers.

Ce script ne PERSISTE RIEN. Il lit les batches par le vrai chemin
(`fetch_project_batches`) et les cure par le vrai point d'entrée de la nuit,
`curate_batch`. `persist_proposals` et `apply_proposals` ne sont jamais appelés.

Il a appelé `_curate_llm_attempt` jusqu'au 2026-08-17 — l'étage SOUS celui de la
nuit. Il lui manquait donc quatre contraintes : la fenêtre
`batch_llm_window` (60 s à dix batches, contre le read timeout httpx de 180 s),
le plafond de complétion, le compactage du batch, et le CIRCUIT qui écarte un
primaire défaillant de tous les batches suivants. C'est ce qui a fait basculer
la production sur `mistralai/mistral-nemotron` le 2026-08-16 : 3/3 batches
valides au canary, 10/10 sur le secours dès la première nuit.

Le verdict utile n'est donc pas « répond / ne répond pas », ni même la validité
JSON seule, mais ce que le CANDIDAT a porté lui-même — séparé de ce que le
secours a sauvé sous son nom. Le budget de nuit est fini : à dix projets, 720 s.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from brain_v42.scripts.domain_backfill import DEFAULT_BASE_URL, load_env_file
from brain_v42.scripts.roadmap_curate import (
    _API_KEY_VAR,
    _ENV_FILE,
    DEFAULT_ROADMAP_FALLBACK_MODEL,
    NIGHT_BUDGET_S,
    BatchOutcome,
    ProjectBatch,
    batch_llm_window,
    curate_batch,
    fetch_project_batches,
)

# `NIGHT_BUDGET_S` est IMPORTÉ, plus recopié. La valeur vivait ici en double
# (720.0), et deux sources de vérité pour un budget ne peuvent que diverger :
# le jour où la nuit changerait de budget, le canary continuerait de dériver
# ses fenêtres de l'ancien sans qu'une seule ligne ne change de couleur.

# Le DÉFAUT de --batches est le --limit que dream.sh passe à la nuit. À 3, le
# même budget se partageait en fenêtres de 120 s (croissant jusqu'à 200) là où
# dix batches les bornent à 60 s : le canary du 2026-08-29 a validé sous ce
# régime doux un secours à 74,5 s/batch qui aurait time-outé chaque tentative
# de production. Épinglé sur dream.sh par
# test_canary_roadmap_matches_night_regime.test_the_cli_default_batch_count_is_the_nights.
DEFAULT_CANARY_BATCHES = 10


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


@dataclass
class ModelMeasurement:
    """Ce qu'un candidat a porté LUI-MÊME, séparé de ce que le secours a sauvé."""

    model: str
    durations: list[float] = field(default_factory=list)
    carried_by_primary: int = 0
    rescued_by_fallback: int = 0
    failed: int = 0
    drafts_total: int = 0
    errors: list[str] = field(default_factory=list)
    # Index 1-based du batch qui a écarté le primaire pour tout le reste du run.
    circuit_opened_at: int | None = None
    outcomes: list[BatchOutcome] = field(default_factory=list)

    @property
    def batches(self) -> int:
        return len(self.durations)

    @property
    def mean_duration_s(self) -> float:
        return statistics.mean(self.durations) if self.durations else 0.0

    @property
    def verdict(self) -> str:
        """Le verdict porte sur le CANDIDAT, jamais sur ce que le secours a sauvé.

        C'est toute la correction : un run intégralement servi par le secours
        rendait « 10/10 valides » et faisait basculer la production sur un
        modèle mort.
        """
        if self.carried_by_primary == self.batches and self.batches:
            return "OK"
        return "PART" if self.carried_by_primary else "MORT"


async def measure_model(
    client: Any,
    model: str,
    batches: list[ProjectBatch],
    *,
    budget_s: float = NIGHT_BUDGET_S,
    fallback_model: str | None = None,
    curate: Any = curate_batch,
    clock: Any = time.monotonic,
    as_dry_primary: bool = False,
) -> ModelMeasurement:
    """Mesurer un candidat SOUS les contraintes de la nuit, pas sous les siennes.

    Passe par `curate_batch`, le point d'entrée que la nuit utilise, et non par
    `_curate_llm_attempt` en dessous : la fenêtre `batch_llm_window`, le plafond
    de complétion, le compactage du batch et le CIRCUIT viennent alors du même
    code que la production. `disabled_models` est un seul ensemble partagé par
    tous les batches — c'est lui qui reproduit « un batch perdu condamne les
    neuf suivants », le fait que le canary d'origine ne pouvait pas voir.

    `as_dry_primary` mesure un candidat DANS le régime qu'il aurait une fois
    adopté : `PROPOSER_ONLY_MODELS` étant dérivé de `DEFAULT_ROADMAP_MODEL`, un
    candidat y entre le jour de sa bascule et change de routage au même instant.
    Sans ce drapeau on mesurerait encore un régime que la production n'aura pas.
    """
    measurement = ModelMeasurement(model=model)
    circuit: set[str] = set()
    total = len(batches)
    # Demandé EXPLICITEMENT à curate_batch, jamais par mutation du global
    # PROPOSER_ONLY_MODELS : un monkeypatch qui fuite laisserait la production
    # routée autrement qu'elle ne l'était, et `check_container_image_pins`
    # interdit à bon droit d'écrire un attribut de module. None = routage
    # nominal, décidé par l'appartenance à l'ensemble, strictement inchangé.
    proposer_only = True if as_dry_primary else None
    t0 = clock()

    for index, batch in enumerate(batches, start=1):
        elapsed = clock() - t0
        window = batch_llm_window(budget_s, elapsed, total - index + 1)
        started = clock()
        try:
            outcome = await curate(
                client,
                model,
                batch,
                _sleep,
                llm_timeout_s=window,
                fallback_model=fallback_model,
                disabled_models=circuit,
                proposer_only=proposer_only,
            )
        except Exception as exc:  # noqa: BLE001 — on MESURE l'échec, on ne le masque pas
            measurement.durations.append(clock() - started)
            measurement.failed += 1
            measurement.errors.append(f"{type(exc).__name__}: {str(exc)[:90]}")
            continue

        measurement.durations.append(clock() - started)
        measurement.outcomes.append(outcome)
        if outcome.primary_error and measurement.circuit_opened_at is None:
            measurement.circuit_opened_at = index
        if outcome.primary_error:
            measurement.errors.append(outcome.primary_error[:90])

        if outcome.failed:
            measurement.failed += 1
            if outcome.error:
                measurement.errors.append(outcome.error[:90])
            continue
        measurement.drafts_total += len(outcome.drafts)
        if outcome.fallback_used:
            measurement.rescued_by_fallback += 1
        else:
            measurement.carried_by_primary += 1

    return measurement


def _proposals_payload(model: str, outcome: BatchOutcome) -> dict[str, Any]:
    """Rendre les propositions LISIBLES, sans prétendre les noter.

    Le triplet mesuré par ce script — validité JSON, s/batch, NOMBRE de
    propositions — ne dit rien de ce qui est proposé. Deux modèles peuvent
    rendre trente propositions chacun, l'un archivant des features vivantes et
    l'autre voyant les vrais doublons, et le tableau les classerait à égalité.

    Un UUID nu ne se juge pas : chaque proposition sort donc avec la feature
    qu'elle vise — nom, statut, épinglage — et `merge` nomme la feature dans
    laquelle elle absorberait sa cible. Sans ça, personne ne peut dire si un
    `archive` est un bon appel ou la destruction d'un engagement.

    Aucune notation ici, délibérément : un score rendu par l'étage qui produit
    les propositions n'aurait aucune valeur d'arbitrage.
    """
    by_id = {feature.id: feature for feature in outcome.batch.features}

    proposals: list[dict[str, Any]] = []
    for draft in outcome.drafts:
        target = by_id.get(draft.feature_id)
        payload = dict(draft.payload)
        if draft.op == "merge" and "into" in payload:
            winner = by_id.get(UUID(str(payload["into"])))
            payload["into_name"] = winner.name if winner else "(hors batch)"
        proposals.append(
            {
                "op": draft.op,
                "feature_id": str(draft.feature_id),
                "target": {
                    "name": target.name if target else "(hors batch)",
                    "status": target.status if target else None,
                    "pinned": target.pinned if target else None,
                },
                "payload": payload,
                "rationale": draft.rationale,
            }
        )

    return {
        "model": model,
        "project_key": outcome.batch.project_key,
        "features_in_batch": [
            {"name": f.name, "status": f.status, "pinned": f.pinned} for f in outcome.batch.features
        ],
        "proposals": proposals,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", required=True, help="candidats, séparés par des virgules")
    parser.add_argument(
        "--batches",
        type=int,
        default=DEFAULT_CANARY_BATCHES,
        help="nombre de batches réels (défaut : le --limit de la nuit, fenêtres identiques)",
    )
    # `--proposer-only` a été RETIRÉ. Il choisissait le parseur à la main, alors
    # que `curate_batch` — le point d'entrée de la nuit, désormais utilisé ici —
    # décide lui-même par appartenance à PROPOSER_ONLY_MODELS. Le garder aurait
    # permis de mesurer un régime de parsing que la nuit n'applique pas à ce
    # modèle, c'est-à-dire de reproduire la panne qu'on corrige.
    parser.add_argument(
        "--fallback-model",
        default=DEFAULT_ROADMAP_FALLBACK_MODEL,
        help=(
            "secours de la nuit, câblé pour reproduire son régime EXACT (fenêtre "
            "de moitié, circuit). Les batches qu'il sauve sont comptés à part et "
            "ne créditent jamais le candidat. `--fallback-model ''` le débranche."
        ),
    )
    parser.add_argument(
        "--as-dry-primary",
        action="store_true",
        help=(
            "mesurer le candidat DANS le régime qu'il aurait une fois primaire "
            "DRY (PROPOSER_ONLY_MODELS est dérivé de DEFAULT_ROADMAP_MODEL, donc "
            "l'adoption change le routage au moment même de la bascule)"
        ),
    )
    parser.add_argument(
        "--budget-seconds",
        type=float,
        default=NIGHT_BUDGET_S,
        help=f"budget nuit dont les fenêtres sont dérivées (défaut: {NIGHT_BUDGET_S:.0f}s)",
    )
    parser.add_argument(
        "--dump-proposals",
        metavar="PATH",
        help="écrire le CONTENU des propositions en JSON, pour un jugement de qualité",
    )
    args = parser.parse_args()

    load_env_file(_ENV_FILE)
    api_key = os.environ.get(_API_KEY_VAR)
    if not api_key:
        print(f"! {_API_KEY_VAR} absent de {_ENV_FILE}")
        return 2

    from brain_v42.db.engine import get_session_factory

    session_factory = get_session_factory()
    batches = await fetch_project_batches(session_factory, args.batches)
    if not batches:
        print("! aucun batch — rien à mesurer")
        return 2

    print(f"Batches réels : {len(batches)}")
    for batch in batches:
        print(f"  - {batch.project_key} : {len(batch.features)} features")
    print()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    client = httpx.AsyncClient(
        base_url=os.environ.get("BRAIN_NVIDIA_BASE_URL", DEFAULT_BASE_URL),
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0),
    )

    rows = []
    dumped: list[dict[str, Any]] = []
    try:
        for model in models:
            # Séquentiel et jamais concurrent : deux candidats en parallèle se
            # disputeraient la file d'attente du fournisseur et fausseraient
            # précisément la mesure recherchée.
            measurement = await measure_model(
                client,
                model,
                batches,
                budget_s=args.budget_seconds,
                fallback_model=args.fallback_model,
                as_dry_primary=args.as_dry_primary,
            )
            if args.dump_proposals:
                dumped.extend(
                    _proposals_payload(model, outcome)
                    for outcome in measurement.outcomes
                    if not outcome.failed
                )
            rows.append(measurement)
            print(
                f"[{measurement.verdict:4}] {model:42} "
                f"{measurement.carried_by_primary}/{measurement.batches} portés  "
                f"{measurement.mean_duration_s:6.1f} s/batch  "
                f"{measurement.drafts_total} propositions"
            )
            if measurement.rescued_by_fallback:
                # La ligne que le canary d'origine ne pouvait pas écrire, faute
                # de passer par l'étage où le repli existe.
                print(
                    f"         └─ {measurement.rescued_by_fallback} batch(es) "
                    f"sauvés par {args.fallback_model} — NON portés par le candidat"
                )
            if measurement.circuit_opened_at is not None:
                print(
                    f"         └─ circuit ouvert au batch {measurement.circuit_opened_at}: "
                    f"le primaire est écarté de tous les batches suivants"
                )
            for err in measurement.errors[:2]:
                print(f"         └─ {err}")
    finally:
        await client.aclose()

    if args.dump_proposals:
        Path(args.dump_proposals).write_text(
            json.dumps(dumped, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n-> contenu des propositions écrit dans {args.dump_proposals}")

    print()
    print("=" * 100)
    print(
        f"{'MODELE':42} {'car':>3} {'valide':>7} {'prop':>5} {'s/batch':>8} {'10 proj':>8}  verdict"
    )
    print("-" * 100)
    for measurement in sorted(rows, key=lambda m: (-m.carried_by_primary, m.mean_duration_s)):
        model = measurement.model
        valid = measurement.carried_by_primary
        total = measurement.batches
        mean = measurement.mean_duration_s
        drafts = measurement.drafts_total
        projected = mean * 10
        if measurement.circuit_opened_at is not None:
            # Priorité sur tout le reste : un circuit ouvert veut dire que la
            # nuit cesserait d'appeler ce modèle après ce batch. Le classer sur
            # la seule validité laisserait passer un candidat que la production
            # n'utiliserait plus.
            verdict = (
                f"ÉCARTÉ — circuit ouvert au batch {measurement.circuit_opened_at}, "
                f"{measurement.rescued_by_fallback} batch(es) servis par le secours"
            )
        elif valid < total:
            verdict = "ÉCARTÉ — pas 100 % porté par le candidat"
        elif projected > NIGHT_BUDGET_S:
            verdict = f"ÉCARTÉ — {projected:.0f} s > budget nuit {NIGHT_BUDGET_S:.0f} s"
        elif drafts == 0:
            # Valide et stérile : un modèle qui ne propose jamais rien passe
            # toutes les gardes de forme et ne fait rien du travail attendu.
            verdict = "SUSPECT — 0 proposition sur tous les batches"
        elif len(model) > 30:
            verdict = "retenable, mais migration 045 OBLIGATOIRE (>30 car.)"
        else:
            verdict = "retenable sans migration"
        print(
            f"{model:42} {len(model):>3} {valid}/{total:>5} {drafts:>5} "
            f"{mean:>8.1f} {projected:>7.0f}s  {verdict}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

#!/usr/bin/env python3
"""Sonder la vivacité des modèles NVIDIA configurés, hors de tout run.

Item (3) du ticket 911bb6f5, différé le 2026-08-05 avec une condition explicite —
« à rouvrir si un deuxième EOL passe ». Il est passé : `deepseek-v4-flash`, choisi
par canary ce jour-là, a atteint sa fin de vie deux jours plus tard, et la nuit du
2026-08-10 est repartie sur son secours 8B.

La machinerie de signalement construite alors FONCTIONNE — la ligne `DÉGRADÉ`
figure bien dans le rapport. Ce qui reste est la LATENCE : on apprend la mort d'un
modèle en lisant le rapport du lendemain, après une nuit dégradée sur dix projets.

Un 410 n'est pas transitoire : aucun retry ne le réparera jamais. Le savoir AVANT
la nuit permet de choisir un remplaçant sur mesure plutôt que sur la fiche du
fournisseur — et c'est exactement l'erreur que le canary du 08-05 avait déjà
évitée une fois.

Lecture seule, non câblé à aucun run, aucune persistance.

Usage :
    set -a; . ~/.config/brain-v42/nvidia.env; set +a
    uv run python -m scripts.probe_model_liveness

Sortie non nulle si au moins un modèle configuré est définitivement absent.
"""

from __future__ import annotations

import enum
import os
import sys
from dataclasses import dataclass

import httpx

BASE_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
API_KEY_VAR = "BRAIN_NVIDIA_API_KEY"
PROBE_MAX_TOKENS = 8
# 90 s, pas 30 : gpt-oss-120b — un maillon DORMANT, exactement ce que cette
# sonde surveille — répond en 75 s en queue froide (mesuré le 2026-08-29,
# puis 2,6 s à chaud). À 30 s il rendait OTHER chaque lundi, et OTHER sortait
# en 0 : l'unité restait verte sur le seul site qu'elle ne mesurait pas.
PROBE_TIMEOUT_SECONDS = 90.0

# 529 est présent délibérément : il manquait à RETRYABLE_STATUS et un seul 529
# renvoyait une nuit entière sur le secours (commit 0eda7e18). Le confondre avec
# un EOL ferait remplacer un modèle parfaitement vivant.
_BUSY_STATUSES = frozenset({429, 500, 502, 503, 504, 529})


class Verdict(enum.Enum):
    """Ce que la sonde peut conclure — et ce qu'elle refuse de conclure."""

    ALIVE = "alive"
    GONE = "gone"
    BUSY = "busy"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class ModelEntry:
    """Un modèle configuré, et le site qui l'utilise.

    Le site d'usage n'est pas décoratif : un verdict sans lui n'est pas
    actionnable, parce qu'il ne dit pas quelle constante remplacer.
    """

    model: str
    used_by: str


@dataclass(frozen=True, slots=True)
class ProbeResult:
    entry: ModelEntry
    status: int | None
    verdict: Verdict
    detail: str = ""


def configured_models() -> list[ModelEntry]:
    """Lire l'inventaire DEPUIS les modules qui s'en servent, jamais le retaper.

    Une liste recopiée ici dériverait de la configuration réelle, et la sonde
    rendrait un vert sur un modèle que plus personne n'appelle pendant que le
    vrai primaire meurt sans être vu. C'est la faute que ce dépôt corrige
    partout ailleurs : mesurer, ne pas recopier.
    """
    from scripts.domain_backfill import DEFAULT_MODEL as DEFAULT_EXTRACT_MODEL
    from scripts.roadmap_curate import (
        DEFAULT_ROADMAP_FALLBACK_MODEL,
        DEFAULT_ROADMAP_MODEL,
        DEFAULT_WET_ROADMAP_FALLBACK_MODEL,
        DEFAULT_WET_ROADMAP_MODEL,
    )
    from scripts.ticket_extract import DEFAULT_EXTRACT_FALLBACK_MODEL

    def resolved(env_var: str, default: str, site: str) -> ModelEntry:
        """La précédence des SITES (env > défaut) — l'unité charge nvidia.env.

        Sans elle, la sonde lirait la constante pendant que la nuit sert
        l'override : vert sur un modèle que plus personne n'appelle. Le site
        nomme la VARIABLE quand elle gagne — c'est elle qu'on remplace alors.
        """
        override = os.environ.get(env_var)
        if override:
            return ModelEntry(override, f"{site} — surchargé par {env_var}")
        return ModelEntry(default, site)

    return [
        resolved(
            "BRAIN_NVIDIA_ROADMAP_MODEL",
            DEFAULT_ROADMAP_MODEL,
            "roadmap_curate.DEFAULT_ROADMAP_MODEL (DRY primaire)",
        ),
        resolved(
            "BRAIN_NVIDIA_ROADMAP_FALLBACK_MODEL",
            DEFAULT_ROADMAP_FALLBACK_MODEL,
            "roadmap_curate.DEFAULT_ROADMAP_FALLBACK_MODEL (DRY secours)",
        ),
        resolved(
            "BRAIN_NVIDIA_ROADMAP_MODEL",
            DEFAULT_WET_ROADMAP_MODEL,
            "roadmap_curate.DEFAULT_WET_ROADMAP_MODEL (WET primaire)",
        ),
        resolved(
            "BRAIN_NVIDIA_ROADMAP_FALLBACK_MODEL",
            DEFAULT_WET_ROADMAP_FALLBACK_MODEL,
            "roadmap_curate.DEFAULT_WET_ROADMAP_FALLBACK_MODEL (WET secours)",
        ),
        resolved(
            "BRAIN_NVIDIA_MODEL",
            DEFAULT_EXTRACT_MODEL,
            "domain_backfill.DEFAULT_MODEL (extract + backfill)",
        ),
        # Maillon dormant : appelé seulement quand le primaire tombe. Égal au
        # primaire il est couvert par coïncidence ; divergent, il serait la seule
        # constante que ni la nuit ni la sonde ne voient mourir.
        resolved(
            "BRAIN_NVIDIA_FALLBACK_MODEL",
            DEFAULT_EXTRACT_FALLBACK_MODEL,
            "ticket_extract.DEFAULT_EXTRACT_FALLBACK_MODEL (extract, secours)",
        ),
    ]


def classify_status(status: int) -> Verdict:
    """410 est définitif, 5xx/429 sont transitoires, le reste n'est pas deviné.

    Ne jamais replier un statut inconnu sur ALIVE : un 401 mal lu ferait conclure
    « tous les modèles sont morts », et un repli optimiste ferait l'inverse.
    """
    if status == 200:
        return Verdict.ALIVE
    if status == 410:
        return Verdict.GONE
    if status in _BUSY_STATUSES:
        return Verdict.BUSY
    return Verdict.OTHER


def probe_models(
    entries: list[ModelEntry], *, client: httpx.Client, api_key: str
) -> list[ProbeResult]:
    """Une requête minimale par modèle. Aucune écriture, aucune persistance."""
    results: list[ProbeResult] = []
    for entry in entries:
        try:
            response = client.post(
                BASE_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": entry.model,
                    "messages": [{"role": "user", "content": "ok"}],
                    "max_tokens": PROBE_MAX_TOKENS,
                },
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            # Le type d'erreur, jamais son texte : une URL ou un en-tête peuvent
            # s'y retrouver, et ce résultat est fait pour être imprimé.
            results.append(ProbeResult(entry, None, Verdict.OTHER, type(exc).__name__))
            continue
        detail = ""
        if classify_status(response.status_code) is Verdict.GONE:
            detail = "fin de vie chez le fournisseur — aucun retry ne la réparera"
        results.append(
            ProbeResult(entry, response.status_code, classify_status(response.status_code), detail)
        )
    return results


def exit_code_for(results: list[ProbeResult]) -> int:
    """GONE → 1, OTHER → 3, sinon 0. « Je ne sais pas » n'est pas un vert.

    Mesuré le 2026-08-29 : le maillon WET dormant répondait au-delà du timeout
    de sonde et sortait en 0 — l'unité restait verte, chaque lundi, sur le
    seul site qu'elle n'avait pas mesuré. GONE domine : un modèle mort est
    plus urgent qu'un modèle illisible. BUSY reste 0 — transitoire, et le
    transformer en échec ferait remplacer un modèle vivant (commit 0eda7e18).
    """
    if any(result.verdict is Verdict.GONE for result in results):
        return 1
    if any(result.verdict is Verdict.OTHER for result in results):
        return 3
    return 0


def _print_result(result: ProbeResult) -> None:
    status = result.status if result.status is not None else "—"
    line = f"{result.verdict.value.upper():<6} {status:<5} {result.entry.model}"
    print(f"{line}\n         {result.entry.used_by}", flush=True)
    if result.detail:
        print(f"         {result.detail}", flush=True)


def main() -> int:
    api_key = os.environ.get(API_KEY_VAR)
    if not api_key:
        print(f"{API_KEY_VAR} absente — source ~/.config/brain-v42/nvidia.env", file=sys.stderr)
        return 2

    # Entrée par entrée, verdict imprimé AU FIL DE L'EAU : une exception sur
    # le site 5 ne doit pas emporter les quatre verdicts déjà acquis — c'est
    # le journal du lundi matin qui dit quelle constante remplacer.
    results: list[ProbeResult] = []
    with httpx.Client() as client:
        for entry in configured_models():
            result = probe_models([entry], client=client, api_key=api_key)[0]
            _print_result(result)
            results.append(result)

    gone = [r for r in results if r.verdict is Verdict.GONE]
    if gone:
        print(f"\n{len(gone)} modèle(s) configuré(s) définitivement absent(s).", file=sys.stderr)
    unknown = [r for r in results if r.verdict is Verdict.OTHER]
    if unknown:
        print(
            f"{len(unknown)} verdict(s) OTHER — illisible n'est pas vivant, re-mesurer.",
            file=sys.stderr,
        )
    return exit_code_for(results)


if __name__ == "__main__":
    raise SystemExit(main())

"""Shared parsing and configuration for the local Dream killswitch drop-in."""

from __future__ import annotations

from pathlib import Path

KILLSWITCHES_PATH = (
    Path.home() / ".config" / "systemd" / "user" / "brain-v42-dream.service.d" / "killswitches.conf"
)

# Env systemd → clé courte du payload.
_KS_KEYS = {
    "BRAIN_DREAM_PROMOTE_ENABLED": "promote",
    "BRAIN_DREAM_REORG_ENABLED": "reorg",
    "BRAIN_DREAM_REORG_DRY_RUN": "reorg_dry",
    "BRAIN_DREAM_EXTRACT_ENABLED": "extract",
    "BRAIN_DREAM_EXTRACT_DRY_RUN": "extract_dry",
    "BRAIN_DREAM_ROADMAP_ENABLED": "roadmap",
    "BRAIN_DREAM_ROADMAP_DRY_RUN": "roadmap_dry",
    "BRAIN_DREAM_SWEEP_ENABLED": "sweep",
    "BRAIN_DREAM_SWEEP_DRY_RUN": "sweep_dry",
}


# Clé À VALEUR DE LISTE, délibérément hors de `_KS_KEYS`. Ce dictionnaire rend un
# `dict[str, bool]` et coerce par `value.lower() == "true"` : une liste de projets
# y entrerait comme `False` et éteindrait une phase dans le briefing de session et
# dans `/metrics` sans toucher la nuit. Une seconde fonction, pas une clé de plus.
PROJECT_POOL_KEY = "BRAIN_DREAM_PROJECT_POOL"


def parse_killswitches(content: str) -> dict[str, bool]:
    """Parse a systemd drop-in (``Environment=KEY=value``) into flags."""
    flags: dict[str, bool] = {}
    for raw in content.splitlines():
        line = raw.strip()
        if not line.startswith("Environment="):
            continue
        for token in line.removeprefix("Environment=").split():
            key, sep, value = token.partition("=")
            if sep and key in _KS_KEYS:
                flags[_KS_KEYS[key]] = value.strip('"').lower() == "true"
    return flags


def parse_project_pool(content: str) -> list[str]:
    """Rend le pool de projets déclaré par le drop-in, dans l'ordre, sans doublon.

    Une clé absente rend une liste VIDE, jamais un défaut deviné. Ce parseur ne
    voit pas le positionnel de ``ExecStart=`` ; inventer ``brain-v42`` ici
    fabriquerait des attentes pour un projet que la nuit n'a peut-être pas servi,
    c'est-à-dire une alarme sortie de nulle part.

    Il accepte les DEUX transports, et ce n'est pas de la complaisance :

    - ``Environment=KEY=a,b,c`` — la forme retenue, sans blanc à protéger ;
    - ``Environment="KEY=a b"`` — la forme protégée, qui arrive entière.

    Et il rend plusieurs clés pour ``Environment=KEY=a b`` NON protégé, alors
    que systemd, lui, poserait la variable à ``a`` et jetterait ``b``. La
    divergence est volontaire. ``dream.sh`` ne peut pas voir ce piège : au
    moment où il démarre, ``b`` a déjà disparu de son environnement. Ce fichier
    est le seul endroit qui lise le texte d'origine, donc le seul qui puisse
    faire sonner quelque chose — une alarme bruyante sur ``b`` manquant vaut
    mieux qu'un accord silencieux avec une configuration cassée.
    """
    pool: list[str] = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line.startswith("Environment="):
            continue
        assignment = line.removeprefix("Environment=").strip()
        if len(assignment) >= 2 and assignment[0] == assignment[-1] and assignment[0] in "\"'":
            assignment = assignment[1:-1]
        key, sep, value = assignment.partition("=")
        if not sep or key != PROJECT_POOL_KEY:
            continue
        for chunk in value.replace(",", " ").split():
            entry = chunk.strip().strip("\"'")
            if entry and entry not in pool:
                pool.append(entry)
    return pool

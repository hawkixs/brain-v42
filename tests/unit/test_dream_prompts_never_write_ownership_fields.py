"""Un prompt de phase ne doit jamais ordonner une écriture que le serveur refuse.

Depuis l'armement du scope de capacité (2026-08-10), `brain_update` porte
`reject_update_ownership_fields=True` : tout `fields` contenant un champ de
propriété — `project_key` en tête — est refusé `ownership_field_forbidden`. Le
refus se fait PAR NOM, donc il tombe même quand la valeur est le projet du
bearer lui-même.

Or `phase_reorg.md` ordonnait
`brain_update(entity_type, entity_id, fields={tags: ..., project_key: ...})`.
Mesuré le 2026-08-17 en exécutant la couche d'autorisation :

    {tags}                        -> autorisé
    {tags, project_key} (même projet) -> REFUSÉ
    {project_key}                 -> REFUSÉ
    {freshness_status: archived}  -> autorisé

Autrement dit la Partie 1 de REORG ne pouvait écrire STRICTEMENT RIEN — sa
normalisation de tags comprise, puisque le prompt joignait toujours
`project_key` au même appel. Personne ne l'a vu parce que REORG tourne en DRY
et n'écrit jamais : la panne n'apparaîtrait qu'au passage en WET.

`test_dream_prompts_match_phase_allowlists.py` ne pouvait pas l'attraper : il
compare les NOMS d'outils, et `brain_update` est bien dans l'allowlist de reorg.
C'est l'ARGUMENT qui est refusé. Ce test ferme cet angle mort, et il dérive sa
liste interdite de `_OWNERSHIP_FIELDS` pour qu'un champ ajouté à la politique
serveur soit automatiquement couvert ici.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from brain_v42.services.dream_project_scope import _OWNERSHIP_FIELDS

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = REPO_ROOT / "scripts" / "dream"

# `fields={...}` tel qu'un prompt l'écrit, accolades non imbriquées.
_FIELDS_BLOCK = re.compile(r"fields\s*=\s*\{([^}]*)\}")


def _prompts() -> list[Path]:
    found = sorted(PROMPT_DIR.glob("phase_*.md"))
    assert found, f"aucun prompt de phase trouvé sous {PROMPT_DIR}"
    return found


@pytest.mark.parametrize("prompt_path", _prompts(), ids=lambda p: p.stem)
def test_no_phase_prompt_instructs_writing_an_ownership_field(prompt_path: Path) -> None:
    """Aucun `fields={...}` de prompt ne nomme un champ de propriété."""
    text = prompt_path.read_text(encoding="utf-8")

    offenders: list[tuple[int, str, str]] = []
    for match in _FIELDS_BLOCK.finditer(text):
        body = match.group(1)
        line_number = text.count("\n", 0, match.start()) + 1
        for field in sorted(_OWNERSHIP_FIELDS):
            # Frontières de mot : `project_keys` ne doit pas matcher `project_key`
            # par préfixe, et inversement.
            if re.search(rf"(?<![\w-]){re.escape(field)}(?![\w-])", body):
                offenders.append((line_number, field, body.strip()[:80]))

    assert not offenders, (
        f"{prompt_path.name} ordonne une écriture que le serveur refuse "
        f"`ownership_field_forbidden` (refus PAR NOM, même si la valeur est le "
        f"projet du bearer) — l'appel entier échoue, y compris les champs "
        f"légitimes du même `fields`: {offenders}"
    )


def test_the_forbidden_set_is_read_from_the_server_policy() -> None:
    """La liste interdite n'est pas recopiée ici : elle vient de la politique.

    Un champ de propriété ajouté côté serveur doit resserrer ce test tout seul.
    Sans cette dépendance, les deux listes divergeraient exactement comme le
    prompt et la politique ont divergé.
    """
    assert "project_key" in _OWNERSHIP_FIELDS
    assert len(_OWNERSHIP_FIELDS) >= 2

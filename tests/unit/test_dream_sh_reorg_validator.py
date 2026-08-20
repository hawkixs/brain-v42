"""Les journaux des validateurs REORG et PROMOTE doivent dire ce qui s'est passé.

Nuit du 19→20, mesurée : `dream.sh` a imprimé « FAIL reorg — validator flagged
integrity issues (dream_runs marked partial) » et la ligne `dream_runs` est
restée `done`. La parenthèse était FAUSSE, et elle l'était sur la seule foi de
`validator_rc != 0` — un code de retour non nul ne dit rien de ce que le
validateur a réussi à écrire avant de sortir. Cette nuit-là il n'avait rien
écrit : son marquage crashait sur une boucle asyncio fermée.

Le correctif du marquage (une seule boucle par validateur) est livré à part.
Il rend la phrase vraie AUJOURD'HUI — mais la laisser telle quelle garderait une
affirmation que rien ne vérifie, prête à re-mentir au prochain défaut du chemin
d'écriture. Le journal doit rapporter ce que `dream.sh` OBSERVE (le validateur a
rejeté le rapport), pas ce qu'il SUPPOSE (une ligne a été marquée ailleurs).

`connect` dit déjà la vérité — « validator rejected CONNECT report; see
validation detail » — et `test_dream_sh_connect_validator.py` interdit
explicitement chez lui la formule « dream_runs marked partial ». Ce fichier est
son miroir pour les deux autres validateurs, écrit dans la même forme.

CE FICHIER TOUCHE AU MOTEUR DE LA NUIT. `scripts/dream.sh` s'exécute depuis le
dépôt à chaque nuit, sans redémarrage : un changement y est actif dès le merge.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"

_BLOCKS = {
    "promote": ("# --- PROMOTE: post-phase validator", "# --- REORG: post-phase validator"),
    "reorg": ("# --- REORG: post-phase validator", '    case "$phase_rc" in'),
}


def _block(phase: str) -> str:
    content = DREAM_SH.read_text(encoding="utf-8")
    start_marker, end_marker = _BLOCKS[phase]
    start = content.index(start_marker)
    end = content.index(end_marker, start)
    return content[start:end]


@pytest.mark.parametrize("phase", sorted(_BLOCKS))
def test_the_validator_failure_log_claims_nothing_it_did_not_observe(phase: str) -> None:
    block = _block(phase)

    assert "dream_runs marked partial" not in block, (
        f"le journal de {phase} affirme un marquage qu'il n'a pas observé : il ne "
        f"dispose que de `validator_rc`, qui ne dit rien de ce que le validateur a "
        f"réussi à écrire avant de sortir. C'est le mensonge mesuré le 19→20."
    )
    assert f"validator rejected {phase.upper()} report; see validation detail" in block, (
        "la formule doit être celle de connect, au mot près — trois journaux qui "
        "disent la même chose de trois façons se relisent trois fois"
    )


@pytest.mark.parametrize("phase", sorted(_BLOCKS))
def test_the_validator_still_runs_and_propagates_its_failure(phase: str) -> None:
    """Le lot ne change QUE du texte : le câblage doit rester intact."""
    block = _block(phase)

    assert f"scripts.dream.{phase}_validate" in block
    assert "validator_rc=$?" in block
    assert "phase_rc=1" in block
    assert "--dream-run-id" in block, (
        "sans l'id, le validateur n'a aucune ligne à marquer et le journal "
        "n'aurait effectivement rien à rapporter"
    )


def test_connect_remains_the_reference_wording() -> None:
    """Le témoin : si connect changeait de formule, ce miroir serait faux."""
    content = DREAM_SH.read_text(encoding="utf-8")

    assert "validator rejected CONNECT report; see validation detail" in content


def test_dream_shell_remains_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(DREAM_SH)], check=True)

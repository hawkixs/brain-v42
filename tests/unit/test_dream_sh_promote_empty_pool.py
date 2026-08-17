"""Épingle le câblage du pool vide PROMOTE dans dream.sh (grep, sans exécution).

Trois propriétés, chacune bordée sur son propre bloc pour qu'une phase ajoutée
plus tard ne puisse pas affaiblir le test en glissant hors de portée :

  - le pool VIDE enregistre une row `dream_runs` ;
  - la RÉCUPÉRATION RATÉE du pool reste un échec et n'enregistre rien ;
  - le chemin nominal (pool non vide) est intact.
"""

from pathlib import Path

import pytest

_DREAM_SH = Path(__file__).parent.parent.parent / "scripts" / "dream.sh"
_RECORD_CMD = "scripts.dream._promote_helpers record-empty-pool"


def _content() -> str:
    return _DREAM_SH.read_text(encoding="utf-8")


def _between(content: str, start: str, end: str) -> str:
    """Tranche bornée des deux côtés ; échoue fort si un marqueur a bougé."""
    assert start in content, f"marqueur de début absent : {start!r}"
    tail = content.split(start, maxsplit=1)[1]
    assert end in tail, f"marqueur de fin absent après {start!r} : {end!r}"
    return tail.split(end, maxsplit=1)[0]


@pytest.fixture
def promote_block() -> str:
    return _between(_content(), "--- PROMOTE: killswitch", "--- REORG: killswitch")


def test_empty_pool_branch_records_a_dream_runs_row(promote_block: str) -> None:
    empty_branch = _between(
        promote_block, 'if [[ "$pool_size" -eq 0 ]]; then', "export PROMOTE_CANDIDATE_POOL_JSON"
    )

    assert _RECORD_CMD in empty_branch
    assert '--date "$TIMESTAMP"' in empty_branch


def test_failed_pool_fetch_stays_a_failure_and_records_nothing(promote_block: str) -> None:
    """Un pool vide n'est PAS une récupération ratée : la distinction doit tenir.

    Enregistrer une row `done` sur le chemin d'échec rendrait un crash de
    promote_prepare invisible — exactement l'incident des 2026-05-02/05-03.
    """
    fail_branch = _between(promote_block, "if (( prep_rc != 0 )); then", "pool_size=")

    assert "FAIL promote — candidate pool fetch failed" in fail_branch
    assert 'FAILED_PHASES+=("$PROJECT_KEY/promote")' in fail_branch
    assert _RECORD_CMD not in fail_branch


def test_nominal_pool_path_is_untouched(promote_block: str) -> None:
    """Le comportement des nuits à pool non vide ne change pas."""
    marker = "export PROMOTE_CANDIDATE_POOL_JSON"
    assert marker in promote_block
    nominal = promote_block.split(marker, maxsplit=1)[1]

    assert _RECORD_CMD not in nominal
    assert _content().count(_RECORD_CMD) == 1


def test_record_failure_cannot_abort_the_night(promote_block: str) -> None:
    """`set -e` est actif : l'appel doit être désarmé et son rc traduit en WARN.

    Une row manquante rallume l'alerte de synthèse — bruyant mais observable.
    Tuer la nuit entière pour un INSERT raté serait la panne, pas le remède.
    """
    empty_branch = _between(
        promote_block, 'if [[ "$pool_size" -eq 0 ]]; then', "export PROMOTE_CANDIDATE_POOL_JSON"
    )
    before, _, after = empty_branch.partition(_RECORD_CMD)

    assert "set +e" in before, "l'appel n'est pas désarmé face à set -e"
    assert "set -e" in after, "set -e n'est pas rétabli après l'appel"
    assert "WARN" in after, "un enregistrement raté doit rester visible"

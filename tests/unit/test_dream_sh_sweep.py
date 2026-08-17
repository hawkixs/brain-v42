"""Épingle le câblage de la phase SWEEP dans dream.sh (grep, sans exécution)."""

import re
from pathlib import Path

_DREAM_SH = Path(__file__).parent.parent.parent / "scripts" / "dream.sh"


def _content() -> str:
    return _DREAM_SH.read_text(encoding="utf-8")


def test_sweep_killswitch_defaults_closed_and_dry():
    content = _content()
    assert 'BRAIN_DREAM_SWEEP_ENABLED="${BRAIN_DREAM_SWEEP_ENABLED:-false}"' in content
    assert 'BRAIN_DREAM_SWEEP_DRY_RUN="${BRAIN_DREAM_SWEEP_DRY_RUN:-true}"' in content


def test_sweep_step_invokes_the_cli_module():
    content = _content()
    assert "brain_v42.maintenance.session_sweep" in content
    assert "SKIP sweep (killswitch" in content


def test_sweep_wet_flag_only_when_dry_run_false():
    content = _content()
    assert 'if [[ "$BRAIN_DREAM_SWEEP_DRY_RUN" != "true" ]]' in content


def test_sweep_step_has_timeout_and_own_log():
    content = _content()
    assert "timeout 5m uv run python -m brain_v42.maintenance.session_sweep" in content
    assert "_sweep.log" in content


def test_sweep_step_does_not_duplicate_the_threshold():
    """Le seuil vit dans brain_v42.models.brain_session.AUTO_STALE_AFTER, pas
    dans dream.sh. Une deuxième copie ici serait la bombe à retardement du
    learning 8dc7e042 : deux constantes qui se contredisent en silence le
    jour où l'une bouge.

    Le bloc est borné des deux côtés (marqueur `--- SWEEP` jusqu'au
    `FAIL_TOTAL=` qui suit) pour qu'une phase ajoutée plus tard après SWEEP
    ne puisse ni casser ni affaiblir ce test en glissant hors de la portée
    scannée.
    """
    content = _content()
    sweep_block = content.split("--- SWEEP", maxsplit=1)[1]
    sweep_block = sweep_block.split("FAIL_TOTAL=", maxsplit=1)[0]

    assert "--older-than-days" not in sweep_block

    # sweep_args ne doit jamais recevoir autre chose que --wet : un flag de
    # seuil sous n'importe quelle autre orthographe (ou tout autre argument)
    # doit faire échouer ce test.
    appended = re.findall(r"sweep_args\+=\(([^)]*)\)", sweep_block)
    assert appended == ["--wet"]

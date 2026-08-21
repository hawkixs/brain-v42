"""Le validateur REORG doit tourner AUSSI quand la phase a échoué.

CE FICHIER TOUCHE AU MOTEUR DE LA NUIT. `scripts/dream.sh` s'exécute depuis le
dépôt à chaque nuit, sans redémarrage : un changement y est actif dès le merge.

Le bloc était gardé par `[[ "$name" == "reorg" && "$phase_rc" == "0" ]]`. La
seconde condition retire le contrôle exactement du cas où il sert : une phase
REORG qui échoue ou qui expire a pu écrire AVANT de mourir, et ce sont ces
écritures-là — partielles, non relues par personne — qui ont le plus besoin
d'être confrontées au périmètre du projet. Une phase verte, elle, a au moins
émis son rapport et suivi son prompt jusqu'au bout.

Le piège en fermant ce trou est la double comptabilité : le `case "$phase_rc"`
qui suit classe la phase en FAILED ou en TIMED_OUT selon le code. Poser
`phase_rc=1` sur une phase déjà à 2 déplacerait un dépassement de budget dans le
seau des échecs durs, et la nuit rapporterait un incident qui n'a pas eu lieu à
la place de celui qui a eu lieu. Le verdict du validateur s'ajoute donc au
journal, jamais à la classification, quand la phase est déjà tombée.

Ces tests n'inspectent pas le TEXTE du script : ils découpent le bloc réel et
l'EXÉCUTENT sous bash avec des stubs pour `log` et `uv`, dans la forme établie
par test_dream_sh_exit_code.py. Un test qui cherche une chaîne prouve que le
texte existe ; celui-ci prouve que bash prend la bonne décision.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"

_BLOCK_START = "# --- REORG: post-phase validator"
_BLOCK_END = '    case "$phase_rc" in'


def _reorg_validator_block() -> str:
    content = DREAM_SH.read_text(encoding="utf-8")
    start = content.index(_BLOCK_START)
    end = content.index(_BLOCK_END, start)
    return content[start:end]


def _run_block(
    tmp_path: Path,
    *,
    phase_rc: int,
    validator_rc: int,
    name: str = "reorg",
) -> tuple[subprocess.CompletedProcess[str], str, int]:
    """Exécute le bloc réel; rend (process, argv `uv` capturés, phase_rc final)."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    uv_calls = tmp_path / "uv_calls.txt"
    uv_calls.write_text("", encoding="utf-8")
    # run_phase a déjà écrit ce fichier au moment où le bloc s'exécute — y
    # compris sur une phase qui échoue, la redirection le créant avant l'agent.
    (log_dir / f"2026-08-20_brain-v42_{name}.log").write_text("", encoding="utf-8")

    harness = "\n".join(
        [
            "set -euo pipefail",
            f"LOG_DIR={shlex.quote(str(log_dir))}",
            "TIMESTAMP=2026-08-20",
            "PROJECT_KEY=brain-v42",
            f"name={shlex.quote(name)}",
            f"phase_rc={phase_rc}",
            "DRY_RUN=false",
            "BRAIN_DREAM_REORG_DRY_RUN=false",
            # Posé par le bloc d'instantané, qui s'exécute plus haut dans la
            # même itération (test_dream_sh_reorg_tags_snapshot.py le couvre).
            f"REORG_TAGS_BEFORE={shlex.quote(str(log_dir / 'tags_before.json'))}",
            f"UV_CALLS={shlex.quote(str(uv_calls))}",
            f"VALIDATOR_RC={validator_rc}",
            'log() { printf "%s\\n" "$*"; }',
            # Deux appels distincts passent par ce stub : la récupération de
            # l'id `dream_runs` (`uv run python -c …`) et le validateur lui-même.
            # Seul le second porte un code de retour intéressant.
            "uv() {",
            '  printf "%s\\n" "$*" >> "$UV_CALLS"',
            '  case "$*" in',
            '    *reorg_validate*) return "$VALIDATOR_RC" ;;',
            '    *) printf "4242" ; return 0 ;;',
            "  esac",
            "}",
            "",
            _reorg_validator_block(),
            "",
            'printf "PHASE_RC=%s\\n" "$phase_rc"',
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    # Garde de harnais, pas une assertion de comportement : sous `set -u` une
    # variable oubliée sortirait en 1 et rendrait vert un test qui attend 1.
    assert proc.stderr == "", f"le harnais a bruité sur stderr: {proc.stderr!r}"
    final_rc = int(proc.stdout.rsplit("PHASE_RC=", 1)[1].strip())
    return proc, uv_calls.read_text(encoding="utf-8"), final_rc


@pytest.mark.parametrize("phase_rc", [0, 1, 2])
def test_the_validator_runs_whatever_the_phase_returned(tmp_path: Path, phase_rc: int) -> None:
    """Échec (1) et dépassement (2) sont précisément les cas à contrôler.

    Une phase morte en cours de route a pu franchir la frontière de projet avant
    de mourir. Le validateur est le dernier endroit qui puisse encore le dire :
    `brain_list` est le seul outil CRUD sans contrôle de scope propre, sa borne
    vivant dans le middleware seul.
    """
    _, uv_calls, _ = _run_block(tmp_path, phase_rc=phase_rc, validator_rc=0)

    assert "scripts.dream.reorg_validate" in uv_calls, (
        f"phase_rc={phase_rc} : le validateur n'a pas été invoqué. Appels vus :\n{uv_calls}"
    )
    assert "--project-key brain-v42" in uv_calls, (
        "le validateur a tourné sans périmètre — la garde de projet serait morte"
    )
    assert "--tags-before-json" in uv_calls, (
        "le validateur a tourné sans instantané : il n'aurait aucun « avant » à "
        "comparer, et la panne masquée de la Partie 1 redeviendrait invisible"
    )
    assert "--run-date" not in uv_calls, (
        "le drapeau du contrôle creux `updated_at >= run_date` est encore passé"
    )
    assert "reorg.events.jsonl" in uv_calls, (
        "le validateur a tourné sans le flux d'événements : le contrôle de symétrie "
        "rapport ↔ appels observés n'aurait rien à confronter"
    )


def test_a_rejected_report_fails_a_phase_that_was_green(tmp_path: Path) -> None:
    """Contre-épreuve : sur une phase verte, le verdict compte toujours."""
    proc, _, final_rc = _run_block(tmp_path, phase_rc=0, validator_rc=1)

    assert final_rc == 1, "un rapport rejeté doit encore faire rougir une phase verte"
    assert "validator rejected REORG report; see validation detail" in proc.stdout


@pytest.mark.parametrize(("phase_rc", "label"), [(1, "échec dur"), (2, "dépassement")])
def test_a_failed_phase_keeps_its_own_classification(
    tmp_path: Path, phase_rc: int, label: str
) -> None:
    """Le verdict du validateur s'ajoute au journal, pas à la classification.

    Le cas qui coûte est `phase_rc=2` : le `case` qui suit range un 2 dans
    TIMED_OUT_PHASES et un 1 dans FAILED_PHASES. Écraser le 2 par un 1 ferait
    rapporter à la nuit un échec dur à la place du dépassement de budget qui a
    réellement eu lieu — et l'opérateur chercherait la mauvaise panne.
    """
    proc, _, final_rc = _run_block(tmp_path, phase_rc=phase_rc, validator_rc=1)

    assert final_rc == phase_rc, (
        f"{label} : phase_rc est passé de {phase_rc} à {final_rc} — la phase serait "
        f"comptée deux fois, et dans le mauvais seau"
    )
    assert "validator rejected REORG report; see validation detail" in proc.stdout, (
        "le verdict doit rester LISIBLE même quand il ne change pas la classification : "
        "sans journal, un franchissement de frontière sur une phase déjà tombée serait muet"
    )


def test_the_block_ignores_every_other_phase(tmp_path: Path) -> None:
    """Témoin : le bloc ne doit pas se déclencher sur scan, synth ou promote."""
    _, uv_calls, final_rc = _run_block(tmp_path, phase_rc=1, validator_rc=1, name="scan")

    assert uv_calls.strip() == "", f"le bloc a tourné sur une phase scan : {uv_calls!r}"
    assert final_rc == 1


def test_dream_shell_remains_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(DREAM_SH)], check=True)

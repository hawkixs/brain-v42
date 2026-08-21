"""dream.sh doit MESURER le « avant » des tags, sinon la garde n'a rien à comparer.

CE FICHIER TOUCHE AU MOTEUR DE LA NUIT. `scripts/dream.sh` s'exécute depuis le
dépôt à chaque nuit, sans redémarrage : un changement y est actif dès le merge.

Le validateur REORG exige désormais un instantané des tags pris juste avant la
phase (`--tags-before-json`). C'est le seul « avant » qui soit observé : le
contrôle qu'il remplace, `updated_at >= run_date`, était creux parce que
`DecayFlusher` rafraîchit l'horodatage toutes les 300 s à travers un trigger sans
clause `WHEN` — et parce que les lectures de REORG lui-même alimentent le flusher.

L'instantané est pris APRÈS le killswitch (une phase coupée ne paie pas la
requête) et AVANT `run_phase_chain`, une seule fois pour les deux tentatives que
le budget de retry autorise : le « avant » de la nuit est celui d'avant la
PREMIÈRE écriture, pas d'avant la dernière.

Ces tests découpent le bloc réel et l'EXÉCUTENT sous bash avec des stubs, dans la
forme de test_dream_sh_exit_code.py.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"

_BLOCK_START = "# --- REORG: pre-phase tags snapshot"
_BLOCK_END = "# `set -e` is active, so we must guard the call"


def _snapshot_block() -> str:
    content = DREAM_SH.read_text(encoding="utf-8")
    start = content.index(_BLOCK_START)
    end = content.index(_BLOCK_END, start)
    return content[start:end]


def _run_block(
    tmp_path: Path,
    *,
    name: str = "reorg",
    snapshot_rc: int = 0,
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    """Rend (process, argv `uv` capturés, chemin de l'instantané tel que vu par bash)."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    uv_calls = tmp_path / "uv_calls.txt"
    uv_calls.write_text("", encoding="utf-8")

    harness = "\n".join(
        [
            "set -euo pipefail",
            f"LOG_DIR={shlex.quote(str(log_dir))}",
            "TIMESTAMP=2026-08-20",
            "PROJECT_KEY=brain-v42",
            f"name={shlex.quote(name)}",
            "REORG_TAGS_BEFORE=",
            f"UV_CALLS={shlex.quote(str(uv_calls))}",
            f"SNAPSHOT_RC={snapshot_rc}",
            'log() { printf "%s\\n" "$*"; }',
            "uv() {",
            '  printf "%s\\n" "$*" >> "$UV_CALLS"',
            "  printf '{\"seeded\": []}'",
            '  return "$SNAPSHOT_RC"',
            "}",
            "",
            _snapshot_block(),
            "",
            'printf "TAGS_BEFORE=%s\\n" "$REORG_TAGS_BEFORE"',
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    assert proc.stderr == "", f"le harnais a bruité sur stderr: {proc.stderr!r}"
    tags_before = proc.stdout.rsplit("TAGS_BEFORE=", 1)[1].strip()
    return proc, uv_calls.read_text(encoding="utf-8"), tags_before


def test_the_snapshot_is_taken_for_the_project_of_the_run(tmp_path: Path) -> None:
    """Un instantané pris sur le mauvais corpus serait pire que pas d'instantané.

    Il ferait comparer chaque entité mutée à un « avant » étranger : des tags
    qui n'ont pas bougé passeraient pour bougés, et l'inverse. La garde
    deviendrait un générateur de verdicts arbitraires que rien ne signalerait.
    """
    _, uv_calls, tags_before = _run_block(tmp_path)

    assert "scripts.dream.reorg_snapshot" in uv_calls, (
        f"aucun instantané pris avant la phase. Appels vus :\n{uv_calls}"
    )
    assert "--project-key brain-v42" in uv_calls
    assert tags_before.endswith("2026-08-20_brain-v42_reorg_tags_before.json"), (
        f"chemin d'instantané inattendu : {tags_before!r}"
    )
    assert Path(tags_before).read_text(encoding="utf-8") == '{"seeded": []}'


def test_a_failed_snapshot_is_announced_and_does_not_abort_the_night(
    tmp_path: Path,
) -> None:
    """Échouer en silence rendrait le refus du validateur incompréhensible.

    Le validateur refusera le rapport faute d'instantané lisible — c'est voulu et
    fail-closed. Mais l'opérateur doit pouvoir remonter du refus à sa cause en une
    ligne de journal, au lieu de soupçonner le rapport de l'agent.
    """
    proc, _, _ = _run_block(tmp_path, snapshot_rc=1)

    assert "pre-phase tags snapshot failed" in proc.stdout, (
        f"l'échec de l'instantané est muet. Journal :\n{proc.stdout}"
    )
    assert proc.returncode == 0, "l'échec de l'instantané ne doit pas tuer la nuit"


def test_the_block_ignores_every_other_phase(tmp_path: Path) -> None:
    """Témoin : scan, synth et promote ne paient pas la requête."""
    _, uv_calls, tags_before = _run_block(tmp_path, name="synth")

    assert uv_calls.strip() == "", f"un instantané a été pris pour synth : {uv_calls!r}"
    assert tags_before == ""


def test_dream_shell_remains_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(DREAM_SH)], check=True)

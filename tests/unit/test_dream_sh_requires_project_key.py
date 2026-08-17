"""`dream.sh` exige son projet. Aucun défaut, à aucun étage.

Le lot des écrivains a posé `--project-key` requis-sans-défaut sur les trois
CLI qui écrivent `dream_runs`. Cette garde s'arrêtait au binaire Python :
`dream.sh` portait `PROJECT_KEY="${1:-brain-v42}"`, donc un lancement nu
satisfaisait le flag requis avec `brain-v42` et étiquetait la nuit d'un autre
projet — exactement la classe de bug que la décision visait, une couche plus
haut que là où la garde avait été posée.

Le dépôt contenait les deux formes et il fallait choisir la référence :
`post_run_alert.py` (`default=DEFAULT_PROJECT_KEY`) contre
`promote_prepare.py` (`required=True`). Tranché par l'opérateur le
2026-08-09 : `required`. Le lot du pool a ensuite retiré le paramètre de
`post_run_alert` — il ne filtrait rien, et le projet vit maintenant dans le
corps groupé du rapport.

Aucune nuit ne change de comportement. L'unité systemd passe la clé
explicitement (`ExecStart=… scripts/dream.sh brain-v42`) et les six harnais de
test passent déjà `test-project`. Seul un `bash scripts/dream.sh` nu casse, et
c'est le but.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"


def _sandbox(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Copie exécutable de dream.sh, hors de la prod à tous les égards.

    `LOG_DIR` vaut `$SCRIPT_DIR/../logs/dream`, donc la copie journalise sous
    `tmp_path`. `XDG_RUNTIME_DIR` privé, sinon le script sortirait 0 en
    trouvant le flock de production pris — un test vert pour rien. `uv` et
    `claude` sont des no-op : aucun appel réseau, aucune écriture en base.
    """
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    dream_copy = scripts_dir / "dream.sh"
    dream_copy.write_text(DREAM_SH.read_text(encoding="utf-8"), encoding="utf-8")
    dream_copy.chmod(0o755)
    subprocess.run(
        ["cp", "-r", str(REPO_ROOT / "scripts" / "dream"), str(scripts_dir / "dream")],
        check=True,
    )

    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    for name in ("uv", "claude", "codex"):
        stub = mock_bin / name
        stub.write_text("#!/usr/bin/env bash\ncat >/dev/null 2>&1 || true\nexit 0\n")
        stub.chmod(0o755)

    env = {
        "HOME": str(tmp_path),
        "PATH": f"{mock_bin}:/usr/bin:/bin",
        "XDG_RUNTIME_DIR": str(tmp_path),
        "BRAIN_DREAM_AGENT_PROVIDER": "claude",
    }
    return dream_copy, env


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    dream_copy, env = _sandbox(tmp_path)
    return subprocess.run(
        [str(dream_copy), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=120,
    )


def test_a_bare_invocation_is_a_hard_failure(tmp_path: Path) -> None:
    """Sans projet, la nuit ne démarre pas — elle ne se rabat sur personne.

    Le mode d'échec que ce test interdit est silencieux par construction : une
    nuit mal étiquetée produit des lignes `dream_runs` parfaitement valides,
    et rien dans le corpus ne permet ensuite de savoir qu'elles mentent. Il n'y
    a pas de backfill possible.
    """
    proc = _run(tmp_path)

    assert proc.returncode == 2, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "project" in proc.stderr.lower()


def test_no_default_survives_in_the_source() -> None:
    """La forme, pas seulement l'effet — un défaut réintroduit ailleurs dans le
    script rendrait le test d'exécution vert sans rétablir la garde."""
    source = DREAM_SH.read_text(encoding="utf-8")

    assert "${1:-brain-v42}" not in source
    assert 'PROJECT_KEY="${1:-' not in source


def test_the_historic_aliases_are_still_normalized(tmp_path: Path) -> None:
    """`brain` et `brain_v42` restent convertis : la garde ajoute une exigence,
    elle ne retire pas la normalisation que le reste du dépôt attend."""
    proc = _run(tmp_path, "brain_v42")

    logs = sorted((tmp_path / "logs" / "dream").glob("*.log"))
    started = "\n".join(
        path.read_text(encoding="utf-8", errors="replace") for path in logs if "_" not in path.name
    )

    assert "project=brain-v42" in started, f"log={started!r} stderr={proc.stderr!r}"

"""`dream.sh` déclare ses attendus et ses skips AU SITE DE LA DÉCISION.

Ces tests n'inspectent pas seulement le texte : ils DÉCOUPENT les blocs réels de
`scripts/dream.sh` et les exécutent sous bash, comme `test_dream_sh_exit_code.py`.
Un test qui cherche une chaîne prouve que le texte existe ; celui-ci prouve que
bash écrit la bonne ligne, dans la bonne branche.

Deux propriétés structurent le fichier :

1. **L'écriture est INCRÉMENTALE.** Un manifeste vidé en fin de nuit n'existe
   que si la nuit se termine normalement — hypothèse qui saute précisément sur
   les nuits susceptibles d'avoir perdu des lignes (`TimeoutStartSec=36000`,
   OOM, redémarrage). L'absence du bloc de clôture devient alors le marqueur
   d'interruption, ce qui n'a de sens que si tout le reste a déjà été écrit.
2. **L'écriture est BEST-EFFORT INTÉGRALE.** Un `logs/` en lecture seule ne doit
   jamais tuer une nuit : la télémétrie qui échoue ne fait pas tomber la phase
   qu'elle observe.

Et une garde : chaque site qui pousse dans `SKIPPED_PHASES`, `FAILED_PHASES` ou
`TIMED_OUT_PHASES` doit avoir un `manifest_put` voisin, du bon `kind` et avec la
MÊME paire. C'est elle qui empêche le détecteur de rétrécir en silence — le
défaut exact que le ticket `0a9c067e` décrit.
"""

from __future__ import annotations

import fcntl
import re
import shlex
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from scripts.dream import run_manifest as rm

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"

# Ancres de découpe : du code réel, jamais des commentaires. Un remaniement qui
# les fait disparaître casse ces tests par ValueError, pas en les laissant verts.
_HEADER_ANCHOR = 'MANIFEST_FILE="$LOG_DIR/'
_HEADER_END_ANCHOR = "manifest_put meta started"
_TRUNCATE_ANCHOR = ': > "$MANIFEST_FILE"'
_LOCK_ANCHOR = 'LOCK_FILE="${XDG_RUNTIME_DIR:-/tmp}/brain-v42-dream.lock"'
_GLOBAL_PHASES_ANCHOR = "DREAM_GLOBAL_PHASES=(extract roadmap sweep)"
_LOOP_ANCHOR = 'for phase_spec in "${PHASES[@]}"; do'
_LOOP_END_ANCHOR = 'manifest_put expected "$name" "$PROJECT_KEY"'
_EMPTY_POOL_ANCHOR = "if (( record_rc == 0 )); then"
_EMPTY_POOL_END_ANCHOR = 'SKIPPED_PHASES+=("$PROJECT_KEY/promote")'

_GLOBAL_BLOCKS = {
    "extract": ("# --- EXTRACT:", 'if [[ "$BRAIN_DREAM_EXTRACT_ENABLED"'),
    "roadmap": ("# --- ROADMAP:", 'if [[ "$BRAIN_DREAM_ROADMAP_ENABLED"'),
    "sweep": ("# --- SWEEP:", 'if [[ "$BRAIN_DREAM_SWEEP_ENABLED"'),
}


def _source() -> str:
    return DREAM_SH.read_text(encoding="utf-8")


def _slice(start_anchor: str, end_anchor: str, *, from_index: int = 0) -> str:
    content = _source()
    start = content.index(start_anchor, from_index)
    end = content.index(end_anchor, start) + len(end_anchor)
    return content[start:end]


def _header_block() -> str:
    return _slice(_HEADER_ANCHOR, _HEADER_END_ANCHOR)


def _loop_expected_block() -> str:
    """Le début du corps de boucle, jusqu'à la déclaration de l'attendu incluse."""
    return _slice(_LOOP_ANCHOR, _LOOP_END_ANCHOR) + "\ndone"


def _empty_pool_block() -> str:
    """Les deux branches du verdict d'écriture + le push `SKIPPED_PHASES`."""
    content = _source()
    start = content.index(_EMPTY_POOL_ANCHOR)
    return _slice(_EMPTY_POOL_ANCHOR, _EMPTY_POOL_END_ANCHOR, from_index=start)


def _global_block(phase: str) -> str:
    start_anchor, end_anchor = _GLOBAL_BLOCKS[phase]
    content = _source()
    start = content.index(start_anchor)
    end = content.index(end_anchor, start)
    return content[start:end]


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )


def _stub_manifest_put(out: Path) -> str:
    """Le vrai `printf` à quatre champs — le parseur Python doit le relire."""
    return (
        "manifest_put() { "
        f'printf \'%s\\t%s\\t%s\\t%s\\n\' "$1" "${{2-}}" "${{3-}}" "${{4-}}" '
        f">> {shlex.quote(str(out))}; }}"
    )


def _bash_array(values: tuple[str, ...]) -> str:
    return " ".join(shlex.quote(value) for value in values)


# --- L'en-tête : trois nombres, écrits AVANT la première phase ---------------


def _run_header(
    tmp_path: Path,
    *,
    pool: tuple[str, ...] = ("red", "brain-v42", "red-lab"),
    phases: tuple[str, ...] = (
        "scan:fast:5:30",
        "clean:fast:5:25",
        "connect:fast:8:40",
        "synth:deep:15:50",
        "promote:deep:10:50",
        "reorg:deep:10:50",
    ),
    global_phases: tuple[str, ...] = ("extract", "roadmap", "sweep"),
    log_dir: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    directory = log_dir if log_dir is not None else tmp_path / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    harness = "\n".join(
        [
            "set -euo pipefail",
            f"LOG_DIR={shlex.quote(str(directory))}",
            "TIMESTAMP=2026-08-18",
            "POOL_SOURCE=BRAIN_DREAM_PROJECT_POOL",
            f"declare -a PROJECT_POOL=({_bash_array(pool)})",
            f"declare -a PHASES=({_bash_array(phases)})",
            f"declare -a DREAM_GLOBAL_PHASES=({_bash_array(global_phases)})",
            "",
            _header_block(),
            "",
            'echo "HEADER-SURVIVED"',
        ]
    )
    proc = _run(harness)
    return proc, directory / "2026-08-18_manifest.tsv"


def test_the_header_states_what_the_night_planned_before_running_it(tmp_path: Path) -> None:
    """`planned_phases` = |PHASES| × |POOL| + |globales|, calculé EN TÊTE.

    C'est le premier des trois nombres de l'auto-contrôle. Il est calculé avant
    la première phase, donc par un chemin de code distinct du compteur
    `TOTAL_PHASES` et de ce que la nuit atteindra réellement.
    """
    proc, manifest_path = _run_header(tmp_path)

    assert proc.returncode == 0, proc.stderr
    manifest = rm.parse_run_manifest(manifest_path.read_text(encoding="utf-8"))
    assert manifest.meta["planned_phases"] == "21"
    assert manifest.meta["run_date"] == "2026-08-18"
    assert manifest.meta["pool"] == "red,brain-v42,red-lab"
    assert manifest.meta["pool_source"] == "BRAIN_DREAM_PROJECT_POOL"
    assert "started" in manifest.meta
    assert manifest.warnings == ()


def test_the_header_names_the_three_global_phases_it_counts(tmp_path: Path) -> None:
    """Le +3 n'est pas une constante magique : il vient d'un tableau nommé."""
    proc, manifest_path = _run_header(tmp_path, pool=("red",), phases=("scan:fast:5:30",))

    assert proc.returncode == 0, proc.stderr
    manifest = rm.parse_run_manifest(manifest_path.read_text(encoding="utf-8"))
    assert manifest.meta["planned_phases"] == "4"


def test_the_header_truncates_so_a_same_day_rerun_never_doubles(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    stale = log_dir / "2026-08-18_manifest.tsv"
    stale.write_text("expected\tscan\tghost\t\n", encoding="utf-8")

    proc, manifest_path = _run_header(tmp_path, log_dir=log_dir)

    assert proc.returncode == 0, proc.stderr
    assert "ghost" not in manifest_path.read_text(encoding="utf-8")


def test_an_unwritable_log_dir_never_kills_the_night(tmp_path: Path) -> None:
    """Best-effort INTÉGRAL : la télémétrie qui échoue ne tue pas la nuit.

    `set -euo pipefail` est actif dès la ligne 2 du script : une redirection en
    échec non gardée ferait sortir bash au milieu de l'en-tête, donc avant la
    première phase.
    """
    log_dir = tmp_path / "readonly"
    log_dir.mkdir()
    log_dir.chmod(0o500)
    try:
        proc, manifest_path = _run_header(tmp_path, log_dir=log_dir)
    finally:
        log_dir.chmod(0o700)

    assert proc.returncode == 0, proc.stderr
    assert "HEADER-SURVIVED" in proc.stdout
    assert not manifest_path.exists()


# --- Le verrou : la troncature ne précède JAMAIS l'acquisition ---------------


def _lock_then_header_block() -> str:
    """Du verrou consultatif jusqu'à la fin de l'en-tête, dans l'ORDRE du script.

    L'assertion est le test : `dream.sh` existe en deux versions possibles, et
    une seule est sûre. Si la troncature vit AVANT le `flock`, la découpe est
    impossible et cette phrase dit pourquoi — la deuxième invocation d'une nuit
    (recouvrement cron, re-déclenchement manuel) traverserait `: > $MANIFEST_FILE`
    et les cinq lignes de méta AVANT de découvrir qu'elle n'a pas le droit de
    tourner, effaçant les déclarations de la nuit VIVANTE.
    """
    content = _source()
    lock = content.index(_LOCK_ANCHOR)
    truncation = content.index(_TRUNCATE_ANCHOR)
    assert truncation > lock, (
        "le manifeste est tronqué AVANT le verrou : une invocation concurrente "
        "— le cas même que le verrou existe pour absorber — viderait le "
        "manifeste de la nuit en cours puis sortirait en 0, rendant rouge une "
        "nuit saine et aveugle le détecteur sur toutes ses paires antérieures"
    )
    return _slice(_LOCK_ANCHOR, _HEADER_END_ANCHOR, from_index=lock)


@contextmanager
def _lock_taken(lock_path: Path) -> Iterator[None]:
    """Un AUTRE processus tient déjà le verrou — flock(2) est par ouverture."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        handle.close()


def _run_lock_then_header(
    tmp_path: Path, *, lock_held: bool
) -> tuple[subprocess.CompletedProcess[str], Path]:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(exist_ok=True)
    manifest_path = log_dir / "2026-08-18_manifest.tsv"
    # Ce que la nuit VIVANTE a déjà déclaré quand la seconde invocation arrive.
    manifest_path.write_text("expected\tscan\tred\t\n", encoding="utf-8")

    harness = "\n".join(
        [
            "set -euo pipefail",
            f"export XDG_RUNTIME_DIR={shlex.quote(str(runtime_dir))}",
            f"LOG_DIR={shlex.quote(str(log_dir))}",
            "TIMESTAMP=2026-08-18",
            "POOL_SOURCE=BRAIN_DREAM_PROJECT_POOL",
            "declare -a PROJECT_POOL=(red)",
            'declare -a PHASES=("scan:fast:5:30")',
            "declare -a DREAM_GLOBAL_PHASES=(extract roadmap sweep)",
            'log() { printf "%s\\n" "$*"; }',
            "",
            _lock_then_header_block(),
            "",
            'echo "HEADER-REACHED"',
        ]
    )
    if not lock_held:
        return _run(harness), manifest_path
    with _lock_taken(runtime_dir / "brain-v42-dream.lock"):
        return _run(harness), manifest_path


def test_a_locked_out_invocation_never_erases_the_running_nights_manifest(
    tmp_path: Path,
) -> None:
    """Le cas que le verrou existe pour absorber, joué jusqu'au fichier.

    Une nuit systemd tourne et a déjà déclaré ses premières paires ; l'opérateur
    relance `dream.sh` à la main. La seconde invocation doit sortir en 0 sans
    avoir touché une seule ligne — sinon la nuit vivante perd ses déclarations,
    finit `consistent=False`, escalade en rc 2 et pose une ligne `coverage`
    mensongère sur une nuit sans le moindre défaut.
    """
    proc, manifest_path = _run_lock_then_header(tmp_path, lock_held=True)

    assert proc.returncode == 0, proc.stderr
    assert "already running" in proc.stdout
    assert "HEADER-REACHED" not in proc.stdout
    assert manifest_path.read_text(encoding="utf-8") == "expected\tscan\tred\t\n"


def test_the_holder_of_the_lock_still_truncates_and_stamps_its_header(
    tmp_path: Path,
) -> None:
    """L'autre sens de marche : derrière le verrou, la troncature a bien lieu."""
    proc, manifest_path = _run_lock_then_header(tmp_path, lock_held=False)

    assert proc.returncode == 0, proc.stderr
    assert "HEADER-REACHED" in proc.stdout
    text = manifest_path.read_text(encoding="utf-8")
    assert "expected\tscan\tred" not in text, "la nuit précédente a bien été effacée"
    manifest = rm.parse_run_manifest(text)
    assert manifest.meta["run_date"] == "2026-08-18"
    assert manifest.meta["planned_phases"] == "4"


# --- La boucle : l'attendu est émis à l'ITÉRATION ----------------------------


def test_every_phase_of_every_project_declares_itself_expected(tmp_path: Path) -> None:
    """18 paires pour 3 projets × 6 phases, émises au même endroit que TOTAL_PHASES++.

    Émettre à l'itération est ce qui rend une SEPTIÈME phase couverte
    automatiquement : ajouter une entrée à `PHASES` étend l'attendu sans toucher
    au détecteur. Il n'y a plus de garde à maintenir, donc plus rien à oublier.
    """
    out = tmp_path / "manifest.tsv"
    phases = (
        "scan:fast:5:30",
        "clean:fast:5:25",
        "connect:fast:8:40",
        "synth:deep:15:50",
        "promote:deep:10:50",
        "reorg:deep:10:50",
    )
    harness = "\n".join(
        [
            "set -euo pipefail",
            f"declare -a PHASES=({_bash_array(phases)})",
            "TOTAL_PHASES=0",
            _stub_manifest_put(out),
            "for PROJECT_KEY in red brain-v42 red-lab; do",
            _loop_expected_block(),
            "done",
            'printf "TOTAL=%s\\n" "$TOTAL_PHASES"',
        ]
    )
    proc = _run(harness)

    assert proc.returncode == 0, proc.stderr
    assert "TOTAL=18" in proc.stdout
    manifest = rm.parse_run_manifest(out.read_text(encoding="utf-8"))
    assert len(manifest.expected) == 18
    assert ("synth", "brain-v42") in manifest.expected
    assert manifest.warnings == ()


def test_a_seventh_phase_extends_the_expected_set_without_touching_the_detector(
    tmp_path: Path,
) -> None:
    out = tmp_path / "manifest.tsv"
    harness = "\n".join(
        [
            "set -euo pipefail",
            'declare -a PHASES=("scan:fast:5:30" "brand-new:deep:9:9")',
            "TOTAL_PHASES=0",
            _stub_manifest_put(out),
            "PROJECT_KEY=red",
            _loop_expected_block(),
        ]
    )
    proc = _run(harness)

    assert proc.returncode == 0, proc.stderr
    manifest = rm.parse_run_manifest(out.read_text(encoding="utf-8"))
    assert ("brand-new", "red") in manifest.expected


@pytest.mark.parametrize("phase", sorted(_GLOBAL_BLOCKS))
def test_each_global_phase_declares_itself_expected_under_the_sentinel(
    tmp_path: Path, phase: str
) -> None:
    """Les trois globales portent `*` — la sentinelle traverse le manifeste."""
    out = tmp_path / "manifest.tsv"
    harness = "\n".join(
        [
            "set -euo pipefail",
            "TOTAL_PHASES=0",
            _stub_manifest_put(out),
            _global_block(phase),
            'printf "TOTAL=%s\\n" "$TOTAL_PHASES"',
        ]
    )
    proc = _run(harness)

    assert proc.returncode == 0, proc.stderr
    assert "TOTAL=1" in proc.stdout
    manifest = rm.parse_run_manifest(out.read_text(encoding="utf-8"))
    assert manifest.expected == frozenset({(phase, "*")})


# --- Finding 1 exécuté : la raison vit DANS chacune des deux branches --------


def _run_empty_pool(tmp_path: Path, record_rc: int) -> tuple[rm.RunManifest, list[str]]:
    out = tmp_path / "manifest.tsv"
    harness = "\n".join(
        [
            "set -euo pipefail",
            "PROJECT_KEY=red-lab",
            f"record_rc={record_rc}",
            "declare -a SKIPPED_PHASES=()",
            "log() { :; }",
            _stub_manifest_put(out),
            _empty_pool_block(),
            'printf "SKIPPED=%s\\n" "${SKIPPED_PHASES[*]}"',
        ]
    )
    proc = _run(harness)
    assert proc.returncode == 0, proc.stderr
    skipped = [
        line.partition("=")[2].split()
        for line in proc.stdout.splitlines()
        if line.startswith("SKIPPED=")
    ][0]
    return rm.parse_run_manifest(out.read_text(encoding="utf-8")), skipped


def test_a_recorded_empty_pool_row_is_declared_recorded(tmp_path: Path) -> None:
    manifest, skipped = _run_empty_pool(tmp_path, record_rc=0)

    assert manifest.skipped == {("promote", "red-lab"): "empty-pool-recorded"}
    assert skipped == ["red-lab/promote"]


def test_a_failed_empty_pool_write_is_declared_unrecorded(tmp_path: Path) -> None:
    """La branche `else` de dream.sh:880 — le WARN qui n'avait aucun lecteur.

    Le push `SKIPPED_PHASES+=` vit HORS du `if`, donc une ligne `manifest_put`
    placée après le `if` déclarerait « sautée, aucune ligne due » alors que
    dream.sh vient d'imprimer que l'écriture a ÉCHOUÉ. Ce test échoue si la
    déclaration sort des deux branches.
    """
    manifest, skipped = _run_empty_pool(tmp_path, record_rc=1)

    assert manifest.skipped == {("promote", "red-lab"): "empty-pool-unrecorded"}
    assert skipped == ["red-lab/promote"], (
        "les deux faits sont INDÉPENDANTS : la phase est sautée dans les deux cas"
    )


def test_the_two_empty_pool_reasons_are_the_ones_the_parser_knows() -> None:
    """Le vocabulaire du bash et celui du parseur ne peuvent pas dériver."""
    block = _empty_pool_block()

    assert rm.WRITE_RECORDED_SKIP_REASON in block
    assert rm.WRITE_FAILED_SKIP_REASON in block
    assert rm.WRITE_FAILED_SKIP_REASON not in rm.NO_ROW_SKIP_REASONS
    assert rm.WRITE_RECORDED_SKIP_REASON not in rm.NO_ROW_SKIP_REASONS


# --- La garde : aucun site de classement sans sa déclaration -----------------

_PUSH = re.compile(r'^\s*(SKIPPED|FAILED|TIMED_OUT)_PHASES\+=\(\s*"([^"]+)"\s*\)')
_PUT = re.compile(r"^\s*manifest_put\s+(\S+)\s+(\S+)\s+(\S+)")
_KIND_OF_ARRAY = {"SKIPPED": "skipped", "FAILED": "failed", "TIMED_OUT": "timeout"}
# Le voisinage est de HUIT lignes, pas de trois, et c'est mesuré : le site du
# pool vide pousse `SKIPPED_PHASES+=` APRÈS un `if/else` dont les deux branches
# portent la déclaration. La branche `then` est à cinq lignes du push.
_NEIGHBOURHOOD = 8


def _unquote(token: str) -> str:
    return token.strip().strip("\"'")


def _declared_pairs_near(lines: list[str], index: int) -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    low = max(0, index - _NEIGHBOURHOOD)
    high = min(len(lines), index + _NEIGHBOURHOOD + 1)
    for line in lines[low:high]:
        match = _PUT.match(line)
        if match:
            kind, phase, project = match.groups()
            found.add((kind, _unquote(phase), _unquote(project)))
    return found


def test_every_classification_site_declares_the_same_pair_to_the_manifest() -> None:
    """La garde qui empêche le détecteur de rétrécir en silence.

    Un site de classement ajouté demain sans sa déclaration casse ce test. Sans
    elle, l'attendu et le classement dériveraient exactement comme
    `LOOP_PHASES` a dérivé de la réalité de la nuit : sans un bruit.
    """
    lines = _source().splitlines()
    orphans: list[str] = []

    for index, line in enumerate(lines):
        match = _PUSH.match(line)
        if not match:
            continue
        array, pair = match.groups()
        project, _, phase = pair.partition("/")
        wanted = (_KIND_OF_ARRAY[array], _unquote(phase), _unquote(project))
        if wanted not in _declared_pairs_near(lines, index):
            orphans.append(f"line {index + 1}: {line.strip()} (attendu {wanted})")

    assert not orphans, "sites de classement sans déclaration voisine:\n" + "\n".join(orphans)


def test_the_guard_actually_sees_the_classification_sites() -> None:
    """Garde du harnais : un test vert sur zéro site ne prouverait rien."""
    sites = [line for line in _source().splitlines() if _PUSH.match(line)]

    assert len(sites) >= 10, f"seulement {len(sites)} sites de classement trouvés"


def test_the_closing_block_stamps_the_three_counters_and_the_end() -> None:
    """Le seul bloc non incrémental — et son absence EST le marqueur d'interruption."""
    content = _source()
    closing = content[content.index('log "=== Dream finished: $summary ==="') :]

    assert 'manifest_put meta total_phases "$TOTAL_PHASES"' in closing
    assert 'manifest_put meta ok_total "$OK_TOTAL"' in closing
    assert 'manifest_put meta fail_total "$FAIL_TOTAL"' in closing
    assert "manifest_put meta finished" in closing


def test_dream_shell_remains_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(DREAM_SH)], check=True)

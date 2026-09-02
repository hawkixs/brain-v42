"""`dream.sh` declares its expectations and its skips AT THE DECISION SITE.

These tests do not merely inspect the text: they EXTRACT the real blocks of
`scripts/dream.sh` and run them under bash, like `test_dream_sh_exit_code.py`. A
test that greps for a string proves the text exists; this one proves bash writes
the right line, in the right branch.

Two properties structure the file:

1. **Writing is INCREMENTAL.** A manifest flushed at the end of the night only
   exists if the night ends normally — an assumption that breaks precisely on the
   nights liable to have lost rows (`TimeoutStartSec=36000`, OOM, restart). The
   absence of the closing block then becomes the interruption marker, which only
   makes sense if everything else has already been written.
2. **Writing is BEST-EFFORT THROUGHOUT.** A read-only `logs/` must never kill a
   night: telemetry that fails does not bring down the phase it observes.

And one guard: every site that pushes into `SKIPPED_PHASES`, `FAILED_PHASES` or
`TIMED_OUT_PHASES` must have a neighbouring `manifest_put`, of the right `kind`
and with the SAME pair. That is what stops the detector shrinking in silence —
the exact defect ticket `0a9c067e` describes.
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

# Extraction anchors: real code, never comments. A rework that makes them
# disappear breaks these tests with a ValueError, not by leaving them green.
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
    """The start of the loop body, up to and including the expectation declaration."""
    return _slice(_LOOP_ANCHOR, _LOOP_END_ANCHOR) + "\ndone"


def _empty_pool_block() -> str:
    """Both branches of the write verdict + the `SKIPPED_PHASES` push."""
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
    """The real four-field `printf` — the Python parser must read it back."""
    return (
        "manifest_put() { "
        f'printf \'%s\\t%s\\t%s\\t%s\\n\' "$1" "${{2-}}" "${{3-}}" "${{4-}}" '
        f">> {shlex.quote(str(out))}; }}"
    )


def _bash_array(values: tuple[str, ...]) -> str:
    return " ".join(shlex.quote(value) for value in values)


# --- The header: three numbers, written BEFORE the first phase --------------


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
    """`planned_phases` = |PHASES| × |POOL| + |globals|, computed AT THE HEAD.

    It is the first of the self-check's three numbers. It is computed before the
    first phase, hence through a code path distinct from the `TOTAL_PHASES`
    counter and from what the night will actually reach.
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


@pytest.mark.parametrize(
    ("global_phases", "planned"),
    [
        (("extract", "roadmap", "sweep"), "4"),
        (("extract", "roadmap", "sweep", "quatrieme"), "5"),
        ((), "1"),
    ],
)
def test_the_header_counts_the_global_phases_it_is_GIVEN(
    tmp_path: Path, global_phases: tuple[str, ...], planned: str
) -> None:
    """The +3 is not a magic constant: it comes from a named array.

    The only observable distinguishing the array from a literal `3` is its
    VARIATION. The array therefore lives outside the extracted block, and the
    harness makes it move: with a hardcoded `+ 3`, the parameter table's last two
    rows fail. The array's real content is pinned by the next test, so this
    harness freedom does not become a fiction.
    """
    proc, manifest_path = _run_header(
        tmp_path, pool=("red",), phases=("scan:fast:5:30",), global_phases=global_phases
    )

    assert proc.returncode == 0, proc.stderr
    manifest = rm.parse_run_manifest(manifest_path.read_text(encoding="utf-8"))
    assert manifest.meta["planned_phases"] == planned


def test_the_script_really_declares_the_three_global_phases_the_blocks_implement() -> None:
    """The harness varies the array; here we pin what the script actually sets.

    Without this test, an emptied array in `dream.sh` would leave the
    parameterization above green: it never reads the real value.
    """
    content = _source()

    assert _GLOBAL_PHASES_ANCHOR in content
    assert content.index(_GLOBAL_PHASES_ANCHOR) < content.index(_HEADER_ANCHOR), (
        "le tableau doit rester défini AVANT l'en-tête qui le compte"
    )
    assert sorted(_GLOBAL_BLOCKS) == sorted(
        _GLOBAL_PHASES_ANCHOR.partition("(")[2].rstrip(")").split()
    ), "les phases comptées et les blocs réellement écrits ne peuvent pas dériver"


def test_the_header_truncates_so_a_same_day_rerun_never_doubles(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    stale = log_dir / "2026-08-18_manifest.tsv"
    stale.write_text("expected\tscan\tghost\t\n", encoding="utf-8")

    proc, manifest_path = _run_header(tmp_path, log_dir=log_dir)

    assert proc.returncode == 0, proc.stderr
    assert "ghost" not in manifest_path.read_text(encoding="utf-8")


def test_an_unwritable_log_dir_never_kills_the_night(tmp_path: Path) -> None:
    """Best-effort THROUGHOUT: telemetry that fails does not kill the night.

    `set -euo pipefail` is active from the script's line 2: an unguarded failing
    redirection would exit bash in the middle of the header, hence before the
    first phase.
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


# --- The lock: truncation NEVER precedes acquisition ------------------------


def _lock_then_header_block() -> str:
    """From the advisory lock to the end of the header, in the script's ORDER.

    The assertion is the test: `dream.sh` exists in two possible versions, and
    only one is safe. If the truncation lives BEFORE the `flock`, the extraction is
    impossible and this sentence says why — a night's second invocation (cron
    overlap, manual re-trigger) would go through `: > $MANIFEST_FILE` and the five
    meta lines BEFORE discovering it is not allowed to run, erasing the LIVE
    night's declarations.
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
    """ANOTHER process already holds the lock — flock(2) is per open file."""
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
    # What the LIVE night has already declared when the second invocation arrives.
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
    """The case the lock exists to absorb, played through to the file.

    A systemd night is running and has already declared its first pairs; the
    operator relaunches `dream.sh` by hand. The second invocation must exit 0
    without touching a single line — otherwise the live night loses its
    declarations, ends `consistent=False`, escalates to rc 2 and lays down a lying
    `coverage` row over a night with no defect whatsoever.
    """
    proc, manifest_path = _run_lock_then_header(tmp_path, lock_held=True)

    assert proc.returncode == 0, proc.stderr
    assert "already running" in proc.stdout
    assert "HEADER-REACHED" not in proc.stdout
    assert manifest_path.read_text(encoding="utf-8") == "expected\tscan\tred\t\n"


def test_the_holder_of_the_lock_still_truncates_and_stamps_its_header(
    tmp_path: Path,
) -> None:
    """The other direction: behind the lock, the truncation does happen."""
    proc, manifest_path = _run_lock_then_header(tmp_path, lock_held=False)

    assert proc.returncode == 0, proc.stderr
    assert "HEADER-REACHED" in proc.stdout
    text = manifest_path.read_text(encoding="utf-8")
    assert "expected\tscan\tred" not in text, "la nuit précédente a bien été effacée"
    manifest = rm.parse_run_manifest(text)
    assert manifest.meta["run_date"] == "2026-08-18"
    assert manifest.meta["planned_phases"] == "4"


# --- The loop: the expectation is emitted AT THE ITERATION ------------------


def test_every_phase_of_every_project_declares_itself_expected(tmp_path: Path) -> None:
    """18 pairs for 3 projects × 6 phases, emitted at the same place as TOTAL_PHASES++.

    Emitting at the iteration is what makes a SEVENTH phase covered automatically:
    adding an entry to `PHASES` extends the expectation without touching the
    detector. There is no guard left to maintain, hence nothing left to forget.
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
    """The three global phases carry `*` — the sentinel crosses the manifest."""
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


# --- Finding 1 executed: the reason lives IN each of the two branches -------


def _run_empty_pool(tmp_path: Path, record_rc: int) -> tuple[rm.RunManifest, list[str]]:
    out = tmp_path / "manifest.tsv"
    harness = "\n".join(
        [
            "set -euo pipefail",
            "PROJECT_KEY=red-lab",
            f"record_rc={record_rc}",
            "declare -a SKIPPED_PHASES=()",
            "SKIPPED_UNWRITTEN=0",
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
    """The `else` branch of dream.sh:880 — the WARN that had no reader.

    The `SKIPPED_PHASES+=` push lives OUTSIDE the `if`, so a `manifest_put` placed
    after the `if` would declare "skipped, no row due" while dream.sh has just
    printed that the write FAILED. This test fails if the declaration leaves either
    branch.
    """
    manifest, skipped = _run_empty_pool(tmp_path, record_rc=1)

    assert manifest.skipped == {("promote", "red-lab"): "empty-pool-unrecorded"}
    assert skipped == ["red-lab/promote"], (
        "les deux faits sont INDÉPENDANTS : la phase est sautée dans les deux cas"
    )


def test_the_two_empty_pool_reasons_are_the_ones_the_parser_knows() -> None:
    """The bash vocabulary and the parser's cannot diverge."""
    block = _empty_pool_block()

    assert rm.WRITE_RECORDED_SKIP_REASON in block
    assert rm.WRITE_FAILED_SKIP_REASON in block
    assert rm.WRITE_FAILED_SKIP_REASON not in rm.NO_ROW_SKIP_REASONS
    assert rm.WRITE_RECORDED_SKIP_REASON not in rm.NO_ROW_SKIP_REASONS


# --- The guard: no classification site without its declaration --------------

# The optional prefix is a `case` arm (`2)`, `*)`): without it the
# start-of-line anchor left out the TWO sites that classify every loop phase, i.e.
# 60 of the 63 slots of a ten-project night.
_PUSH = re.compile(r'^\s*(?:[^\s)]+\)\s*)?(SKIPPED|FAILED|TIMED_OUT)_PHASES\+=\(\s*"([^"]+)"\s*\)')
# The INDEPENDENT survey, with no start-of-line anchor: it is what measures what
# the guard does not see. `\b` is enough to exclude `CONTROLLED_TIMEOUT_PHASES`
# and `FALLBACK_PHASES`, which are not classes.
_ANY_PUSH = re.compile(r"\b(SKIPPED|FAILED|TIMED_OUT)_PHASES\+=\(")
_PUT = re.compile(r"^\s*manifest_put\s+(\S+)\s+(\S+)\s+(\S+)")
_KIND_OF_ARRAY = {"SKIPPED": "skipped", "FAILED": "failed", "TIMED_OUT": "timeout"}
# The neighbourhood is EIGHT lines, not three, and that is measured: the
# empty-pool site pushes `SKIPPED_PHASES+=` AFTER an `if/else` whose two branches
# both carry the declaration. The `then` branch is five lines from the push.
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


def _orphan_sites(lines: list[str]) -> list[str]:
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
    return orphans


def test_every_classification_site_declares_the_same_pair_to_the_manifest() -> None:
    """The guard that stops the detector shrinking in silence.

    A classification site added tomorrow without its declaration breaks this test.
    Without it, the expectation and the classification would diverge exactly as
    `LOOP_PHASES` diverged from the night's reality: without a sound.
    """
    orphans = _orphan_sites(_source().splitlines())

    assert not orphans, "sites de classement sans déclaration voisine:\n" + "\n".join(orphans)


def test_the_guard_actually_sees_the_classification_sites() -> None:
    """Harness guard: a test green over zero sites would prove nothing."""
    sites = [line for line in _source().splitlines() if _PUSH.match(line)]

    assert len(sites) >= 15, f"seulement {len(sites)} sites de classement trouvés"


def test_the_guard_sees_the_case_arms_too_not_just_the_flush_left_sites() -> None:
    """The numeric floor says NOTHING about the ones missing: this names them.

    The loop body's two `case` arms classify EVERY phase of every project in the
    pool — 60 of the 63 slots of a ten-project night. A start-of-line anchor left
    them out: their `manifest_put` could disappear without a single test moving,
    and a phase classified `failed` without a declaration falls back to `silent`,
    hence rc 2 and a red unit over a night whose failure was already reported.
    """
    lines = _source().splitlines()
    seen = {index for index, line in enumerate(lines) if _PUSH.match(line)}
    every = {index for index, line in enumerate(lines) if _ANY_PUSH.search(line)}

    unseen = sorted(every - seen)

    assert not unseen, "sites de classement invisibles pour la garde:\n" + "\n".join(
        f"line {index + 1}: {lines[index].strip()}" for index in unseen
    )


def test_the_guard_would_catch_a_case_arm_stripped_of_its_declaration() -> None:
    """Guard of the guard: it must BITE on the mutation it claims to see."""
    fabricated = [
        '    case "$phase_rc" in',
        "      0) ;;",
        '      2) TIMED_OUT_PHASES+=("$PROJECT_KEY/$name")',
        '         manifest_put timeout "$name" "$PROJECT_KEY" ;;',
        '      *) FAILED_PHASES+=("$PROJECT_KEY/$name") ;;',
        "    esac",
    ]

    orphans = _orphan_sites(fabricated)

    assert len(orphans) == 1
    assert "FAILED_PHASES" in orphans[0]
    assert not _orphan_sites(fabricated[:4]), "le bras déclaré, lui, reste accepté"


def test_the_closing_block_stamps_the_three_counters_and_the_end() -> None:
    """The only non-incremental block — and its absence IS the interruption marker."""
    content = _source()
    closing = content[content.index('log "=== Dream finished: $summary ==="') :]

    assert 'manifest_put meta total_phases "$TOTAL_PHASES"' in closing
    assert 'manifest_put meta ok_total "$OK_TOTAL"' in closing
    assert 'manifest_put meta fail_total "$FAIL_TOTAL"' in closing
    assert "manifest_put meta finished" in closing


def test_dream_shell_remains_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(DREAM_SH)], check=True)

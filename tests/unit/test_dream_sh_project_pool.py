"""The project pool — the loop EXECUTED, not merely the script's text.

Spec `2026-08-08-dream-project-pool-design.md` §3, §6, §9, §10.

These tests launch a real copy of `dream.sh` with stubs for `uv`, `claude` and
`codex`. No network call, no database write: we observe the log and the
`logs/dream/` tree, which are enough to prove what matters here — how many
projects were served, and whether their logs survived each other.

The targeted failure mode is **green and silent** in both directions:
- a pool shrinking to one project with no error (systemd transport, §6);
- logs truncating one another, leaving only the last project by morning (§3.2).
Neither produces a non-zero exit code. Only a measurement sees them.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"


def _sandbox(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """An executable copy of dream.sh, out of production in every respect.

    `LOG_DIR` is `$SCRIPT_DIR/../logs/dream`, so the copy logs under `tmp_path`.
    A private `XDG_RUNTIME_DIR`, otherwise the script would exit 0 on finding
    production's flock taken — a green for nothing.
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

    # A surgical `date`: it intercepts ONLY `+%j` and delegates everything else
    # to the real binary (timestamps, log file names). Without it, the pool's
    # daily rotation — `_rotation=$(( 10#$(date +%j) % size ))` — makes the pool's
    # ORDER depend on the day the suite runs.
    date_stub = mock_bin / "date"
    date_stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == "+%j" && -n "${BRAIN_DREAM_FAKE_DOY:-}" ]]; then\n'
        '  printf "%s\\n" "$BRAIN_DREAM_FAKE_DOY"\n'
        "  exit 0\n"
        "fi\n"
        'exec /bin/date "$@"\n'
    )
    date_stub.chmod(0o755)

    for name in ("claude", "codex"):
        stub = mock_bin / name
        stub.write_text("#!/usr/bin/env bash\ncat >/dev/null 2>&1 || true\nexit 0\n")
        stub.chmod(0o755)

    # `uv` fails SELECTIVELY on otel_split. That is not a contrivance: a
    # successful otel_split deletes the raw log (`rm -f "$raw_log"`) and lets the
    # real binary write report/otel, which the stub cannot do. Making it fail
    # takes the WARN branch — real code — which copies the raw log into the report
    # and touches the otel one. The three projected paths then exist on disk,
    # built by the script and not by the test.
    # The claude rail goes through scripts.dream.claude_runner since 2026-08-11.
    # The raw log is therefore no longer created by a dream.sh redirection but by
    # the runner itself: the stub must reproduce that observable contract,
    # otherwise otel_split's WARN branch has nothing to copy and the test fails on
    # an absence production does not produce.
    uv_stub = mock_bin / "uv"
    uv_stub.write_text(
        "#!/usr/bin/env bash\n"
        "cat >/dev/null 2>&1 || true\n"
        'case "$*" in\n'
        "  *otel_split*) exit 1 ;;\n"
        "  *claude_runner*)\n"
        '    _raw=""\n'
        "    while (($#)); do\n"
        "      if [[ $1 == --raw-log ]]; then _raw=$2; shift 2; else shift; fi\n"
        "    done\n"
        '    [[ -n "$_raw" ]] && printf "mock claude phase output\\n" >> "$_raw"\n'
        "    exit 0\n"
        "    ;;\n"
        "esac\n"
        "exit 0\n"
    )
    uv_stub.chmod(0o755)

    env = {
        "HOME": str(tmp_path),
        "PATH": f"{mock_bin}:/usr/bin:/bin",
        "XDG_RUNTIME_DIR": str(tmp_path),
        "BRAIN_DREAM_AGENT_PROVIDER": "claude",
        # The claude rail got its preflight on 2026-08-11, symmetric with
        # codex's. A night without an MCP token must not start: that is the
        # incident of 2026-07-03, six blind phases and "6/6 OK".
        "MCP_HTTP_TOKEN": "test-only-token",
    }
    return dream_copy, env


def _run(
    tmp_path: Path, *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    dream_copy, env = _sandbox(tmp_path)
    env.update(extra_env or {})
    return subprocess.run(
        [str(dream_copy), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=180,
    )


def _main_log(tmp_path: Path) -> str:
    """The night's single narrative — `$TIMESTAMP.log`, with no phase component."""
    logs = sorted((tmp_path / "logs" / "dream").glob("*.log"))
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace") for path in logs if "_" not in path.name
    )


# --- The pool forms, and it says where it came from ------------------------


def test_a_single_positional_still_serves_exactly_one_project(tmp_path: Path) -> None:
    """Regression on the property that makes this work safe.

    Steps 1 to 5 of §12 are deliverable "without a single night changing
    behaviour". A bare positional must therefore produce exactly the previous
    night: one project, served once.
    """
    _run(tmp_path, "brain-v42")
    log = _main_log(tmp_path)

    assert "Pool (1) from positional argument: brain-v42" in log
    assert log.count("--- Projet ") == 1


def test_a_comma_separated_pool_serves_every_project_once(tmp_path: Path) -> None:
    proc = _run(tmp_path, "alpha,beta,gamma")
    log = _main_log(tmp_path)

    assert log.count("--- Projet ") == 3, f"log={log!r} stderr={proc.stderr!r}"
    for project in ("alpha", "beta", "gamma"):
        assert f"--- Projet {project} ---" in log


def test_the_drop_in_variable_beats_the_positional(tmp_path: Path) -> None:
    """The precedence direction is a guard, not a preference.

    `ExecStart=` lives in the versioned template install.sh regenerates; the
    drop-in survives. If the positional won, widening the pool in the drop-in
    would change nothing and the night would stay at one project — green and mute.
    """
    # An even day -> zero rotation on a pool of 2, so the write order is
    # preserved. The order is what this test pins; the rotation has its own
    # witness.
    _run(
        tmp_path,
        "brain-v42",
        extra_env={"BRAIN_DREAM_PROJECT_POOL": "alpha,beta", "BRAIN_DREAM_FAKE_DOY": "222"},
    )
    log = _main_log(tmp_path)

    assert "Pool (2) from BRAIN_DREAM_PROJECT_POOL: alpha beta" in log
    assert "--- Projet brain-v42 ---" not in log


def test_the_pool_source_is_named_in_the_log(tmp_path: Path) -> None:
    """A one-project night must not be ambiguous by morning.

    Without the source, "pool of 1" does not distinguish "the drop-in says one
    project" from "systemd ate the variable and we fell back on the positional".
    """
    _run(tmp_path, "brain-v42", extra_env={"BRAIN_DREAM_PROJECT_POOL": "solo"})

    assert "from BRAIN_DREAM_PROJECT_POOL" in _main_log(tmp_path)


# --- Transport traps exit with 2, never shrink in silence ------------------


def test_a_space_separated_pool_is_a_hard_failure(tmp_path: Path) -> None:
    """§6, the named transport trap.

    `Environment=BRAIN_DREAM_PROJECT_POOL=a b` sets the variable to `a` and throws
    `b` away. But a quoted value (`Environment="…=a b"`) arrives WHOLE, with its
    space. Treating it as a single key would manufacture a `project_key` that
    canonicalize_project_key rejects deep inside a best-effort function which
    swallows its exception: the column would stay NULL without a sound.
    """
    proc = _run(tmp_path, "alpha", extra_env={"BRAIN_DREAM_PROJECT_POOL": "alpha beta"})

    assert proc.returncode == 2, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "comma-separated" in proc.stderr


def test_a_duplicate_key_is_a_hard_failure(tmp_path: Path) -> None:
    """Serving the same project twice is a typo, not a choice."""
    proc = _run(tmp_path, "alpha,beta,alpha")

    assert proc.returncode == 2
    assert "Duplicate" in proc.stderr


def test_an_empty_entry_is_a_hard_failure(tmp_path: Path) -> None:
    """`a,,b` is one comma too many, not an anonymous project."""
    proc = _run(tmp_path, "alpha,,beta")

    assert proc.returncode == 2
    assert "Empty project key" in proc.stderr


def test_a_slash_in_a_key_is_a_hard_failure(tmp_path: Path) -> None:
    """The key enters a log file name (§3.2)."""
    proc = _run(tmp_path, "alpha/beta")

    assert proc.returncode == 2
    assert "Slash" in proc.stderr


def test_surrounding_whitespace_is_trimmed_not_rejected(tmp_path: Path) -> None:
    """`a, b` is a natural, unambiguous human spelling."""
    _run(tmp_path, "alpha, beta", extra_env={"BRAIN_DREAM_FAKE_DOY": "222"})
    log = _main_log(tmp_path)

    assert "Pool (2) from positional argument: alpha beta" in log


# --- §3.2: one project's logs do not survive the next ----------------------


def test_each_project_keeps_its_own_phase_logs(tmp_path: Path) -> None:
    """codex_runner truncates: without projection, only the last project survives.

    The phase report is not merely a log — `PHASE_DEPS` RE-READS it to inject the
    previous phase's context (§3.3). An overwritten report makes `beta`'s CONNECT
    read `alpha`'s CLEAN report.
    """
    _run(tmp_path, "alpha,beta")
    names = {path.name for path in (tmp_path / "logs" / "dream").glob("*")}

    # The proof is not "each project has files": it is that the SAME phase
    # coexists for both. An unprojected template would produce a single
    # `…_scan.log`, and the test would still pass if we settled for counting files
    # per project.
    for phase in ("scan", "clean", "connect"):
        alpha = {n for n in names if n.endswith(f"_alpha_{phase}.log")}
        beta = {n for n in names if n.endswith(f"_beta_{phase}.log")}
        assert alpha, f"pas de rapport {phase} pour alpha: {sorted(names)}"
        assert beta, f"pas de rapport {phase} pour beta: {sorted(names)}"

    # And no phase report must stay WITHOUT a project: that would be the old
    # template, which the second project would overwrite.
    for phase in ("scan", "clean", "connect"):
        assert not [
            n for n in names if n.endswith(f"_{phase}.log") and "alpha" not in n and "beta" not in n
        ], f"gabarit non projeté survivant: {sorted(names)}"


def test_the_night_narrative_stays_a_single_file(tmp_path: Path) -> None:
    """§3.2: the main log is NOT projected.

    It is opened with `tee -a`, it is the night's single narrative and the target
    of §11's aggregated alert. Fragmenting it per project would contradict Q6.
    """
    _run(tmp_path, "alpha,beta")
    unprojected = [
        path.name for path in (tmp_path / "logs" / "dream").glob("*.log") if "_" not in path.name
    ]

    assert len(unprojected) == 1, f"le récit de la nuit s'est fragmenté: {unprojected}"


# --- §10: the retry allocation is a resource of the NIGHT -------------------


def test_the_retry_budget_is_a_night_allocation_not_a_per_phase_one() -> None:
    """The shape, because the effect requires a phase that really fails.

    +43 min eligible PER PROJECT is +344 min of ceiling at eight — the difference
    between 7.7 h and 13.4 h of configured worst case.
    """
    source = DREAM_SH.read_text(encoding="utf-8")

    assert 'BRAIN_DREAM_RETRY_BUDGET="${BRAIN_DREAM_RETRY_BUDGET:-2}"' in source
    assert "RETRY_BUDGET_LEFT=$(( RETRY_BUDGET_LEFT - 1 ))" in source
    assert "(( RETRY_BUDGET_LEFT > 0 ))" in source
    # An exhausted budget must not switch the signal off: the phase keeps its rc.
    assert "NO-RETRY" in source


def test_the_pool_order_rotates_so_the_same_project_is_not_always_last() -> None:
    """§10: without rotation, it is always the same project that is sacrificed.

    Same idiom as roadmap_curate.rotate_keys, in service since 2026-07-04.
    """
    source = DREAM_SH.read_text(encoding="utf-8")

    assert "10#$(date +%j)" in source, (
        "la rotation doit forcer la base 10 : `date +%j` rend 001-366 et bash "
        "lirait 008 comme un octal invalide — une nuit qui casse 2 jours sur 366"
    )


# --- §3.4: the exports do not survive the iteration ------------------------


def test_the_promote_exports_are_reset_per_project() -> None:
    """A project that skips PROMOTE must not inherit another's pool.

    The reset is at the HEAD of the iteration: the body's five `continue`s would
    skip a cleanup placed at the tail.
    """
    source = DREAM_SH.read_text(encoding="utf-8")
    body_start = source.index("run_project_phases() {")
    phase_loop = source.index('for phase_spec in "${PHASES[@]}"', body_start)
    preamble = source[body_start:phase_loop]

    assert "export PROMOTE_CANDIDATE_POOL_JSON='[]'" in preamble
    assert "export PROMOTE_RECENT_PROMOTIONS_JSON='[]'" in preamble


def test_the_pool_rotates_by_one_notch_per_day(tmp_path: Path) -> None:
    """The witness the rotation did not have, and whose absence cost dearly.

    `dream.sh` rotates the pool by one notch a day — without which the project at
    the tail is always the one sacrificed when the night exceeds its ceiling. That
    behaviour was proven by NO dedicated test: its only trace was the order
    asserted by two neighbouring tests, which SUFFERED it instead of checking it.
    Hence their red every other day.
    """
    _run(
        tmp_path,
        "brain-v42",
        extra_env={"BRAIN_DREAM_PROJECT_POOL": "alpha,beta,gamma", "BRAIN_DREAM_FAKE_DOY": "223"},
    )

    # 223 % 3 == 1: the pool starts on its second element.
    assert "Pool (3) from BRAIN_DREAM_PROJECT_POOL: beta gamma alpha" in _main_log(tmp_path)


def test_the_rotation_serves_every_project_whatever_the_day(tmp_path: Path) -> None:
    """The rotation changes the ORDER, never the SET.

    A badly written rotation — a shift that truncates instead of rotating — would
    lose one project per night without the announced count saying so.
    """
    _run(
        tmp_path,
        "brain-v42",
        extra_env={"BRAIN_DREAM_PROJECT_POOL": "alpha,beta,gamma", "BRAIN_DREAM_FAKE_DOY": "223"},
    )
    log = _main_log(tmp_path)

    for project in ("alpha", "beta", "gamma"):
        assert f"--- Projet {project} ---" in log, f"{project} n'a pas été servi"
    assert log.count("--- Projet ") == 3


def test_no_test_asserts_a_multi_project_pool_order_without_pinning_the_day() -> None:
    """The ANTI-RECURRENCE guard, and it is what the batch is worth.

    The defect was not in `dream.sh`: the daily rotation is correct and intended.
    It was in two tests that asserted a pool ORDER without fixing the day, hence
    green or red depending on the parity of `date +%j`. Measured on 2026-08-11
    (day 223, rotation 1 on a pool of 2): red. The day before (day 222, rotation
    0): green. A test green every other day guards nothing, and its colour says
    nothing about the code.
    """
    source = Path(__file__).read_text(encoding="utf-8")

    offenders: list[str] = []
    for block in re.split(r"\ndef ", source):
        name = block.split("(", 1)[0].strip()
        if not name.startswith("test_"):
            continue
        # "Pool (1)" is insensitive to rotation: n % 1 is always 0.
        if re.search(r'"Pool \((?!1\))\d+\) from [^"]*: \w+ \w+', block) and (
            "BRAIN_DREAM_FAKE_DOY" not in block
        ):
            offenders.append(name)

    assert offenders == [], (
        "ces tests asserent l'ordre d'un pool de plusieurs projets sans fixer le "
        f"jour : ils seront verts ou rouges selon la date d'exécution — {offenders}"
    )

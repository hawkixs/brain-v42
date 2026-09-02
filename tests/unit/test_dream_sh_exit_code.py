"""Discriminating exit code of scripts/dream.sh — shell blocks EXECUTED.

Context measured on 2026-08-07: the night reported "8/9 phases OK, 1 timed out
(extract), 2 skipped (promote sweep)" and `brain-v42-dream.service` was
nonetheless `failed (Result: exit-code)`. The only deviation was extract's
CONTROLLED timeout (rc=3 — a deadline the CLI imposes on itself after recording
its terminal dream_run), observed on 5 nights out of 6 between 2026-08-02 and
2026-08-07. A permanently red unit carries no information any more: the next REAL
failure will change nothing on screen.

These tests do not inspect the script's TEXT. They extract the two shell blocks
concerned from the real file and EXECUTE them under bash with prefabricated
arrays and stubs for `log` and `uv`. A test that greps for a string proves the
text exists; this one proves bash takes the right decision.

Two blocks, two links of the same chain:
  1. CLASSIFICATION (extract_rc → arrays),
  2. VERDICT (arrays → exit code + alert + summary).
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"

# Extraction anchors. These are pieces of real code, not comments: a rework that
# made them disappear breaks the test loudly instead of leaving it green over
# nothing.
_VERDICT_ANCHOR = "FAIL_TOTAL=$(("
_EXTRACT_ANCHOR = "if (( extract_rc == 0 )); then"
_EXTRACT_END_ANCHOR = "# --- ROADMAP"
_PHASE_CASE_ANCHOR = 'case "$phase_rc" in'
_PHASE_CASE_END_ANCHOR = "esac"


def _source() -> str:
    return DREAM_SH.read_text(encoding="utf-8")


def _verdict_block() -> str:
    """From the counter computation to the end of the file."""
    content = _source()
    start = content.index(_VERDICT_ANCHOR)
    return content[start:]


def _extract_classification_block() -> str:
    """The `extract_rc` → arrays cascade, external guardrail included."""
    content = _source()
    start = content.index(_EXTRACT_ANCHOR)
    end = content.index(_EXTRACT_END_ANCHOR, start)
    return content[start:end]


def _phase_classification_block() -> str:
    """The `case "$phase_rc"` of the SIX agent phases (scan…reorg).

    They share the exit gate with extract but never had a Python witness: the only
    witness was a bash script `testpaths=["tests"]` does not collect and no CI rail
    calls.
    """
    content = _source()
    start = content.index(_PHASE_CASE_ANCHOR)
    end = content.index(_PHASE_CASE_END_ANCHOR, start) + len(_PHASE_CASE_END_ANCHOR)
    return content[start:end]


def _bash_array(values: tuple[str, ...]) -> str:
    return " ".join(shlex.quote(v) for v in values)


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )


def _run_verdict(
    tmp_path: Path,
    *,
    failed: tuple[str, ...] = (),
    timed_out: tuple[str, ...] = (),
    controlled: tuple[str, ...] = (),
    skipped: tuple[str, ...] = (),
    fallbacks: tuple[str, ...] = (),
    total: int = 9,
    alert_rc: int = 0,
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the real verdict block; return (process, captured `uv` calls)."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    alert_calls = tmp_path / "alert_calls.txt"
    alert_calls.write_text("", encoding="utf-8")

    harness = "\n".join(
        [
            "set -euo pipefail",
            f"LOG_DIR={shlex.quote(str(log_dir))}",
            "TIMESTAMP=2026-08-07",
            # The verdict block now passes the night's manifest to the alert
            # (ticket 0a9c067e). A HARNESS stub: what it contains is verified by
            # test_dream_sh_run_manifest.py, and what is done with it by
            # test_dream_sh_coverage_verdict.py.
            'MANIFEST_FILE="$LOG_DIR/${TIMESTAMP}_manifest.tsv"',
            "PROJECT_KEY=brain-v42",
            f"TOTAL_PHASES={total}",
            f"declare -a FAILED_PHASES=({_bash_array(failed)})",
            f"declare -a TIMED_OUT_PHASES=({_bash_array(timed_out)})",
            f"declare -a CONTROLLED_TIMEOUT_PHASES=({_bash_array(controlled)})",
            f"declare -a SKIPPED_PHASES=({_bash_array(skipped)})",
            "SKIPPED_UNWRITTEN=0",
            f"declare -a FALLBACK_PHASES=({_bash_array(fallbacks)})",
            f"ALERT_CALLS={shlex.quote(str(alert_calls))}",
            'log() { printf "%s\\n" "$*"; }',
            # A HARNESS stub, not behaviour: the extracted blocks now
            # declare every decision to the night's manifest (ticket
            # 0a9c067e). Without it, `manifest_put` would be unresolvable and
            # `set -e` would exit bash with 127 — a false green witness for the
            # tests that expect 1. What the declaration writes is verified by
            # tests/unit/test_dream_sh_run_manifest.py.
            "manifest_put() { :; }",
            f"ALERT_RC={alert_rc}",
            'uv() { printf "%s\\n" "$*" >> "$ALERT_CALLS"; return "$ALERT_RC"; }',
            "",
            _verdict_block(),
        ]
    )
    proc = _run(harness)
    # Harness guard (not a behavioural assertion): under `set -u`, a forgotten
    # variable would exit bash with 1 and turn green a test expecting 1. We refuse
    # that false witness.
    assert proc.stderr == "", f"le harnais a bruité sur stderr: {proc.stderr!r}"
    return proc, alert_calls.read_text(encoding="utf-8")


def _run_extract_classification(extract_rc: int) -> dict[str, list[str]]:
    """Run extract's classification cascade for a given rc."""
    harness = "\n".join(
        [
            "set -euo pipefail",
            "TIMESTAMP=2026-08-07",
            f"extract_rc={extract_rc}",
            "declare -a FAILED_PHASES=()",
            "declare -a TIMED_OUT_PHASES=()",
            "declare -a CONTROLLED_TIMEOUT_PHASES=()",
            "log() { :; }",
            # A HARNESS stub, not behaviour: the extracted blocks now
            # declare every decision to the night's manifest (ticket
            # 0a9c067e). Without it, `manifest_put` would be unresolvable and
            # `set -e` would exit bash with 127 — a false green witness for the
            # tests that expect 1. What the declaration writes is verified by
            # tests/unit/test_dream_sh_run_manifest.py.
            "manifest_put() { :; }",
            # `if true; then` balances the extracted block's trailing `fi`, which
            # closed the EXTRACT killswitch left out of scope.
            "if true; then",
            _extract_classification_block(),
            'printf "FAILED=%s\\n" "${FAILED_PHASES[*]}"',
            'printf "TIMED_OUT=%s\\n" "${TIMED_OUT_PHASES[*]}"',
            'printf "CONTROLLED=%s\\n" "${CONTROLLED_TIMEOUT_PHASES[*]}"',
        ]
    )
    proc = _run(harness)
    return _parse_arrays(proc)


def _parse_arrays(proc: subprocess.CompletedProcess[str]) -> dict[str, list[str]]:
    assert proc.returncode == 0, f"harnais cassé (rc={proc.returncode}): {proc.stderr}"
    assert proc.stderr == "", f"le harnais a bruité sur stderr: {proc.stderr!r}"
    parsed: dict[str, list[str]] = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        parsed[key] = value.split()
    return parsed


def _run_phase_classification(phase_rc: int) -> dict[str, list[str]]:
    """Run an agent phase's real `case "$phase_rc"` for a given rc."""
    harness = "\n".join(
        [
            "set -euo pipefail",
            "name=synth",
            # A HARNESS stub, not behaviour: the extracted blocks now
            # declare every decision to the night's manifest (ticket
            # 0a9c067e). Without it, `manifest_put` would be unresolvable and
            # `set -e` would exit bash with 127 — a false green witness for the
            # tests that expect 1. What the declaration writes is verified by
            # tests/unit/test_dream_sh_run_manifest.py.
            "manifest_put() { :; }",
            "PROJECT_KEY=brain-v42",
            f"phase_rc={phase_rc}",
            "declare -a FAILED_PHASES=()",
            "declare -a TIMED_OUT_PHASES=()",
            "declare -a CONTROLLED_TIMEOUT_PHASES=()",
            _phase_classification_block(),
            'printf "FAILED=%s\\n" "${FAILED_PHASES[*]}"',
            'printf "TIMED_OUT=%s\\n" "${TIMED_OUT_PHASES[*]}"',
            'printf "CONTROLLED=%s\\n" "${CONTROLLED_TIMEOUT_PHASES[*]}"',
        ]
    )
    return _parse_arrays(_run(harness))


# --- B3: the reference night (controlled deadline alone) exits 0 -------------


def test_controlled_deadline_only_night_exits_zero(tmp_path: Path) -> None:
    """The 2026-08-07 night replayed: extract on a CONTROLLED deadline, nothing else.

    The systemd unit must go green again: the phase did what it could in the time
    it had imposed on itself, and recorded its terminal dream_run before handing
    back.
    """
    proc, _ = _run_verdict(
        tmp_path,
        timed_out=("extract",),
        controlled=("extract",),
        skipped=("promote", "sweep"),
        total=9,
    )
    assert proc.returncode == 0, (
        "une échéance contrôlée seule doit sortir en 0 — sinon l'unité est "
        f"rouge 5 nuits sur 6 et n'informe plus de rien. stdout={proc.stdout!r}"
    )


def test_controlled_deadline_only_night_still_alerts(tmp_path: Path) -> None:
    """B1: making the exit code less sensitive must switch NOTHING off.

    post_run_alert is called today only on anomaly. Decoupling the exit code
    without decoupling the alert would have removed it in the same gesture.
    """
    _, alert_calls = _run_verdict(
        tmp_path,
        timed_out=("extract",),
        controlled=("extract",),
        skipped=("promote", "sweep"),
        total=9,
    )
    assert "scripts.dream.post_run_alert" in alert_calls, (
        "l'alerte de fin de nuit doit rester envoyée sur une échéance "
        f"contrôlée. Appels uv capturés: {alert_calls!r}"
    )
    assert "--date 2026-08-07" in alert_calls
    # §11: `--project-key` was REMOVED from the call. It was decorative —
    # `fetch_failed_runs` did not even receive it — and with the pool rotation it
    # would have passed a different key every night, with no reader.
    assert "--project-key" not in alert_calls


def test_controlled_deadline_summary_hides_nothing(tmp_path: Path) -> None:
    """B4: we stop making it a unit failure; we do not hide it."""
    proc, _ = _run_verdict(
        tmp_path,
        timed_out=("extract",),
        controlled=("extract",),
        skipped=("promote", "sweep"),
        total=9,
    )
    assert "8/9 phases OK" in proc.stdout, proc.stdout
    assert "1 timed out (extract)" in proc.stdout, proc.stdout
    assert "2 skipped (promote sweep)" in proc.stdout, proc.stdout


# --- B2: what must stay red -------------------------------------------------


def test_hard_failure_exits_non_zero(tmp_path: Path) -> None:
    """A genuinely failed phase keeps a non-zero exit code."""
    proc, _ = _run_verdict(tmp_path, failed=("roadmap",), total=9)
    assert proc.returncode != 0, (
        f"un échec dur doit rester visible dans systemd. stdout={proc.stdout!r}"
    )


def test_outer_guard_timeout_exits_non_zero(tmp_path: Path) -> None:
    """EXTERNAL guardrail timeout: the phase's state is UNKNOWN.

    Not classified as a controlled deadline -> it counts as a failure. That is the
    meaning of the fail-closed guard: a timeout nature added tomorrow and forgotten
    here stays loud instead of going silent.
    """
    proc, _ = _run_verdict(tmp_path, timed_out=("synth",), total=9)
    assert proc.returncode != 0, (
        "un timeout de garde-fou externe laisse la phase dans un état inconnu "
        f"et doit sortir non nul. stdout={proc.stdout!r}"
    )


def test_mixed_night_exits_non_zero(tmp_path: Path) -> None:
    """B5: a hard failure AND a controlled deadline the same night -> non-zero exit.

    The mixed case is the one implementations miss: subtracting the controlled
    deadlines without clamping the counter at zero, or testing
    `timed_out == controlled` instead of the total, makes that night green.
    """
    proc, _ = _run_verdict(
        tmp_path,
        failed=("roadmap",),
        timed_out=("extract",),
        controlled=("extract",),
        total=9,
    )
    assert proc.returncode != 0, (
        "un échec dur ne doit jamais être masqué par une échéance contrôlée "
        f"concomitante. stdout={proc.stdout!r}"
    )


def test_mixed_night_still_alerts(tmp_path: Path) -> None:
    """The mixed case must alert too (no sensitivity regression)."""
    _, alert_calls = _run_verdict(
        tmp_path,
        failed=("roadmap",),
        timed_out=("extract",),
        controlled=("extract",),
        total=9,
    )
    assert "scripts.dream.post_run_alert" in alert_calls, alert_calls


def test_a_hard_failure_marked_controlled_by_mistake_still_exits_non_zero(
    tmp_path: Path,
) -> None:
    """The guard must be STRUCTURAL, not arithmetic.

    The original form subtracted `#CONTROLLED` from the total: it was fail-closed
    only as long as CONTROLLED ⊆ TIMED_OUT, an invariant no guard enforced. A phase
    wrongly written into FAILED **and** CONTROLLED — a pattern copied into the
    wrong branch, which is the normal error mode of a shell `case` — erased its own
    failure: the script exited 0 while printing "1 failed (synth)" itself.

    Here FAILED_PHASES is queried for itself. No write into
    CONTROLLED_TIMEOUT_PHASES can mask it any more.
    """
    proc, _ = _run_verdict(
        tmp_path,
        failed=("synth",),
        timed_out=(),
        controlled=("synth",),
        total=9,
    )
    assert "1 failed (synth)" in proc.stdout, (
        f"le scénario n'a pas été atteint (résumé attendu). stdout={proc.stdout!r}"
    )
    assert proc.returncode != 0, (
        "un échec dur ne doit jamais pouvoir être effacé par une inscription "
        f"erronée dans les échéances contrôlées. stdout={proc.stdout!r}"
    )


def test_failed_alert_on_a_controlled_night_still_exits_non_zero(tmp_path: Path) -> None:
    """B1 for DELIVERY, not only for invocation.

    Since a night with a controlled deadline exits 0 — the majority case — the log
    is the only witness left. If the reporter itself fails (database unavailable,
    `TooManyConnectionsError` measured on 2026-06-10), nothing is delivered any
    more AND the unit stays green: the night becomes entirely mute. A reporter
    failure is precisely the kind of breakage this work wants to make visible
    again.
    """
    proc, alert_calls = _run_verdict(
        tmp_path,
        timed_out=("extract",),
        controlled=("extract",),
        alert_rc=1,
        total=9,
    )
    assert "scripts.dream.post_run_alert" in alert_calls, (
        f"le scénario n'a pas été atteint (alerte non tentée): {alert_calls!r}"
    )
    assert proc.returncode != 0, (
        "une alerte non délivrée doit rougir l'unité : sinon la nuit ne laisse "
        f"aucune trace nulle part. stdout={proc.stdout!r}"
    )


def test_clean_night_exits_zero_and_reports_all_phases(tmp_path: Path) -> None:
    """A night with no anomaly: unchanged."""
    proc, _ = _run_verdict(tmp_path, total=9)
    assert proc.returncode == 0
    assert "9/9 phases OK" in proc.stdout, proc.stdout


def test_a_night_carried_by_the_fallback_is_never_reported_as_clean(
    tmp_path: Path,
) -> None:
    """A dead primary rail must not look like a perfect night.

    Measured from 2026-08-11 to 2026-08-17: the code-mode host being down failed
    each night's 60 codex phases, the agy fallback caught them all, and the night
    signed off "63/63 phases OK". Six nights, no warning light. `FAIL_TOTAL` counts
    PHASES — a fallen-back phase is a successful phase — while `dream_runs` records
    ATTEMPTS: 60 `fail` rows nobody read, the reporter being short-circuited by the
    clean case's `exit 0`.

    The fallback stays a success: the night does exit 0, we do not redden a unit
    for a fallback that did its job. But the summary NAMES it, and the reporter
    speaks on every night, not only the ones already red.
    """
    proc, alert_calls = _run_verdict(
        tmp_path,
        total=63,
        fallbacks=("brain-v42/scan", "brain-v42/clean"),
    )

    assert proc.returncode == 0, proc.stdout
    assert "63/63 phases OK" in proc.stdout, proc.stdout
    assert "2 repliées" in proc.stdout, (
        f"une nuit portée par le secours se lit comme une nuit parfaite: {proc.stdout!r}"
    )
    assert "brain-v42/scan" in proc.stdout, proc.stdout
    assert "scripts.dream.post_run_alert" in alert_calls, (
        "le rapporteur reste court-circuité quand aucune PHASE n'a échoué, "
        f"donc les tentatives échouées ne sont lues par personne: {alert_calls!r}"
    )


# --- Classification amont : extract_rc → tableaux ---------------------------


def test_extract_controlled_deadline_is_classified_controlled() -> None:
    """rc=3: a bounded deadline, terminal dream_run recorded."""
    arrays = _run_extract_classification(3)
    assert arrays["CONTROLLED"] == ["*/extract"], arrays
    assert arrays["TIMED_OUT"] == ["*/extract"], arrays
    assert arrays["FAILED"] == [], arrays


def test_extract_outer_guard_is_not_classified_controlled() -> None:
    """rc=124: `timeout 10m` killed the process, state unknown.

    It stays in TIMED_OUT (alert + summary) but must NEVER enter the controlled
    deadlines, otherwise the external guardrail goes silent.
    """
    arrays = _run_extract_classification(124)
    assert arrays["CONTROLLED"] == [], arrays
    assert arrays["TIMED_OUT"] == ["*/extract"], arrays


def test_extract_hard_failure_is_classified_failed() -> None:
    arrays = _run_extract_classification(1)
    assert arrays["FAILED"] == ["*/extract"], arrays
    assert arrays["CONTROLLED"] == [], arrays


def test_extract_deferral_is_not_an_anomaly() -> None:
    """rc=4: nothing was cut short, no anomaly to count."""
    arrays = _run_extract_classification(4)
    assert arrays["FAILED"] == [], arrays
    assert arrays["TIMED_OUT"] == [], arrays
    assert arrays["CONTROLLED"] == [], arrays


def test_extract_success_is_not_an_anomaly() -> None:
    arrays = _run_extract_classification(0)
    assert arrays["FAILED"] == [], arrays
    assert arrays["TIMED_OUT"] == [], arrays
    assert arrays["CONTROLLED"] == [], arrays


# --- Upstream classification: phase_rc of the SIX agent phases --------------
#
# These phases (scan, clean, connect, synth, promote, reorg) share the exit gate
# with extract but had no Python witness. The only witness,
# tests/integration/test_dream_sh_fail_propagation.sh, is collected by no rail:
# `testpaths=["tests"]` does not pick up .sh files and .github/workflows/ does not
# call it.


def test_agent_phase_timeout_is_never_a_controlled_deadline() -> None:
    """phase_rc=2: `timeout Nm` (or codex_runner) KILLED the agent in flight.

    Nothing was recorded, the working state is unknown — that is the 2026-04-09
    postmortem. Exempting those timeouts would make the unit green over exactly the
    failure it must signal. They stay in TIMED_OUT (alert + summary) and NEVER
    enter the controlled deadlines.
    """
    arrays = _run_phase_classification(2)
    assert arrays["CONTROLLED"] == [], arrays
    assert arrays["TIMED_OUT"] == ["brain-v42/synth"], arrays
    assert arrays["FAILED"] == [], arrays


def test_agent_phase_hard_failure_is_classified_failed() -> None:
    arrays = _run_phase_classification(1)
    assert arrays["FAILED"] == ["brain-v42/synth"], arrays
    assert arrays["CONTROLLED"] == [], arrays
    assert arrays["TIMED_OUT"] == [], arrays


def test_agent_phase_success_is_not_an_anomaly() -> None:
    arrays = _run_phase_classification(0)
    assert arrays["FAILED"] == [], arrays
    assert arrays["TIMED_OUT"] == [], arrays
    assert arrays["CONTROLLED"] == [], arrays


def test_dream_shell_remains_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(DREAM_SH)], check=True)


# --- End to end: the REAL script, from extract's rc to the exit code --------
#
# The two harnesses above each prove one link. This one proves they are WIRED
# together: the whole of dream.sh, with `claude` and `uv` mocked, in a temporary
# tree (LOG_DIR = $SCRIPT_DIR/../logs/dream, so no production log is touched) and
# its own XDG_RUNTIME_DIR so as not to share production's flock. Same pattern as
# tests/integration/test_dream_sh_fail_propagation.sh.


def _run_full_dream(
    tmp_path: Path, *, extract_rc: int, claude_rc: int = 0
) -> tuple[subprocess.CompletedProcess[str], str]:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    dream_copy = scripts_dir / "dream.sh"
    dream_copy.write_text(_source(), encoding="utf-8")
    dream_copy.chmod(0o755)
    subprocess.run(
        ["cp", "-r", str(REPO_ROOT / "scripts" / "dream"), str(scripts_dir / "dream")],
        check=True,
    )

    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    claude_mock = mock_bin / "claude"
    claude_mock.write_text(
        "#!/usr/bin/env bash\ncat >/dev/null 2>&1 || true\n"
        'echo "[mock claude] phase output"\nexit "${CLAUDE_MOCK_EXIT:-0}"\n',
        encoding="utf-8",
    )
    claude_mock.chmod(0o755)
    # `uv` is argument-sensitive: only ticket_extract returns the injected rc,
    # everything else (preflight, parsers, alert) is a silent no-op.
    uv_mock = mock_bin / "uv"
    uv_mock.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *scripts.ticket_extract* ]]; then\n'
        '  exit "${EXTRACT_MOCK_EXIT:-0}"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    uv_mock.chmod(0o755)

    proc = subprocess.run(
        [str(dream_copy), "test-project"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={
            "HOME": str(tmp_path),
            "PATH": f"{mock_bin}:/usr/bin:/bin",
            # A private lock: the script would exit 0 on finding production's
            # flock taken, which would turn this test green for nothing.
            "XDG_RUNTIME_DIR": str(tmp_path),
            "BRAIN_DREAM_AGENT_PROVIDER": "claude",
            # claude preflight delivered on 2026-08-11: no token, no night.
            "MCP_HTTP_TOKEN": "test-only-token",
            "BRAIN_DREAM_EXTRACT_ENABLED": "true",
            "CLAUDE_MOCK_EXIT": str(claude_rc),
            "EXTRACT_MOCK_EXIT": str(extract_rc),
        },
    )
    log = (tmp_path / "logs" / "dream").glob("*.log")
    main_log = "\n".join(
        p.read_text(encoding="utf-8", errors="replace") for p in sorted(log) if "_" not in p.name
    )
    return proc, main_log


def test_full_run_with_controlled_extract_deadline_exits_zero(tmp_path: Path) -> None:
    """The night of 2026-08-07, replayed against the whole script: 0."""
    proc, main_log = _run_full_dream(tmp_path, extract_rc=3)
    assert "TIMEOUT extract (controlled deadline" in main_log, (
        f"le scénario n'a pas été atteint. log={main_log!r} stdout={proc.stdout!r}"
    )
    assert proc.returncode == 0, (
        f"nuit à échéance contrôlée : l'unité doit être verte. log={main_log!r}"
    )


def test_full_run_with_outer_guard_extract_timeout_exits_non_zero(
    tmp_path: Path,
) -> None:
    """Même script, garde-fou externe (`timeout 10m`) : l'unité rougit."""
    proc, main_log = _run_full_dream(tmp_path, extract_rc=124)
    assert "TIMEOUT extract (outer guard" in main_log, (
        f"le scénario n'a pas été atteint. log={main_log!r} stdout={proc.stdout!r}"
    )
    assert proc.returncode != 0, f"état de phase inconnu : l'unité doit rougir. log={main_log!r}"

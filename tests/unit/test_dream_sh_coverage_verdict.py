"""Le verdict de couverture, du code retour de l'alerte jusqu'à journald.

Ticket `0a9c067e`, seconde moitié de son fil : « l'alerte n'est lue par
personne ». Ce n'était pas une figure de style. Deux faits mesurés :

- `scripts/dream.sh` redirigeait la sortie de `post_run_alert` vers le FICHIER
  daté seul (`>> "$LOG_DIR/$TIMESTAMP.log" 2>&1`), quand `log()` fait un `tee`
  vers stdout, donc vers journald. Le corps de l'alerte n'a jamais atteint
  `journalctl -u brain-v42-dream` ;
- aucun code de sortie ne suivait un trou de couverture : l'unité restait verte.

Ces tests découpent le bloc de verdict RÉEL et l'exécutent, avec un `uv` stub qui
rend un code et une sortie choisis. Ils prouvent que bash décide, journalise et
sort comme il faut — pas que le texte existe.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"

_VERDICT_ANCHOR = "FAIL_TOTAL=$(("

_COVERAGE_LINE = "COVERAGE mode=manifest expected=63 written=62 skipped=1 declared=0 writefail=0 silent=0 extra=0"
_ALERT_BODY = "\n".join(
    [
        "Dream run on 2026-08-18 had 1 non-OK phase(s):",
        "",
        "### Couverture dream_runs",
        "COVERAGE_SILENT silent=red/scan",
        _COVERAGE_LINE,
    ]
)


def _verdict_block() -> str:
    content = DREAM_SH.read_text(encoding="utf-8")
    return content[content.index(_VERDICT_ANCHOR) :]


def _run_verdict(
    tmp_path: Path,
    *,
    alert_rc: int,
    alert_out: str = _ALERT_BODY,
    strict: str | None = None,
    failed: tuple[str, ...] = (),
    record_rc: int = 0,
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    alert_calls = tmp_path / "alert_calls.txt"
    alert_calls.write_text("", encoding="utf-8")

    harness = "\n".join(
        [
            "set -euo pipefail",
            f"LOG_DIR={shlex.quote(str(log_dir))}",
            "TIMESTAMP=2026-08-18",
            'MANIFEST_FILE="$LOG_DIR/${TIMESTAMP}_manifest.tsv"',
            "PROJECT_KEY=brain-v42",
            "TOTAL_PHASES=63",
            f"declare -a FAILED_PHASES=({' '.join(shlex.quote(v) for v in failed)})",
            "declare -a TIMED_OUT_PHASES=()",
            "declare -a CONTROLLED_TIMEOUT_PHASES=()",
            "declare -a SKIPPED_PHASES=()",
            "SKIPPED_UNWRITTEN=0",
            "declare -a FALLBACK_PHASES=()",
            "manifest_put() { :; }",
            'log() { printf "%s\\n" "$*" | tee -a "$LOG_DIR/$TIMESTAMP.log"; }',
            f"ALERT_CALLS={shlex.quote(str(alert_calls))}",
            f"ALERT_RC={alert_rc}",
            f"RECORD_RC={record_rc}",
            f"ALERT_OUT={shlex.quote(alert_out)}",
            *([] if strict is None else [f"BRAIN_DREAM_COVERAGE_STRICT={shlex.quote(strict)}"]),
            # Un seul stub pour les deux CLI : le writer de la ligne `coverage`
            # n'imprime rien et rend son propre code, comme le vrai.
            "uv() {",
            '  printf "%s\\n" "$*" >> "$ALERT_CALLS"',
            '  case "$*" in',
            '    *record_coverage_gap*) return "$RECORD_RC" ;;',
            "  esac",
            '  printf "%s\\n" "$ALERT_OUT"',
            '  return "$ALERT_RC"',
            "}",
            "",
            _verdict_block(),
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    assert proc.stderr == "", f"le harnais a bruité sur stderr: {proc.stderr!r}"
    dated_log = (log_dir / "2026-08-18.log").read_text(encoding="utf-8")
    return proc, alert_calls.read_text(encoding="utf-8"), dated_log


def test_the_manifest_is_handed_to_the_alert(tmp_path: Path) -> None:
    _, alert_calls, _ = _run_verdict(tmp_path, alert_rc=0)

    assert "scripts.dream.post_run_alert" in alert_calls
    assert "--manifest" in alert_calls
    assert "2026-08-18_manifest.tsv" in alert_calls


def test_the_coverage_line_reaches_journald_on_a_green_night(tmp_path: Path) -> None:
    """La ligne machine passe par `log()`, donc par le `tee` vers stdout.

    Toutes les nuits, y compris vertes : c'est ce qui met les deux nombres du
    ticket côte à côte, sous le résumé « N/M phases OK » imprimé juste avant.
    """
    proc, _, dated_log = _run_verdict(tmp_path, alert_rc=0)

    assert proc.returncode == 0
    assert f"=== dream_runs {_COVERAGE_LINE} ===" in proc.stdout
    assert "=== Dream finished:" in proc.stdout
    assert _ALERT_BODY in dated_log, "la sortie COMPLÈTE reste dans le fichier daté"


def test_a_silent_gap_turns_the_unit_red(tmp_path: Path) -> None:
    proc, _, _ = _run_verdict(tmp_path, alert_rc=2)

    assert proc.returncode == 1
    assert "FAIL  dream_runs coverage" in proc.stdout


def test_a_broken_reporter_keeps_its_historic_wording(tmp_path: Path) -> None:
    """rc=1 n'est pas rc=2 : « l'outil est cassé » ≠ « la nuit a un trou »."""
    proc, _, _ = _run_verdict(tmp_path, alert_rc=1)

    assert proc.returncode == 1
    assert "WARN  post_run_alert failed (rc=1)" in proc.stdout
    assert "dream_runs coverage" not in proc.stdout


def test_the_escalation_can_be_disarmed_but_never_in_silence(tmp_path: Path) -> None:
    """Rollback opérateur : l'unité redevient verte, le verdict continue d'être dit."""
    proc, _, _ = _run_verdict(tmp_path, alert_rc=2, strict="false")

    assert proc.returncode == 0
    assert "FAIL  dream_runs coverage" in proc.stdout
    assert "BRAIN_DREAM_COVERAGE_STRICT=false" in proc.stdout


def test_the_disarm_switch_defaults_to_armed(tmp_path: Path) -> None:
    proc, _, _ = _run_verdict(tmp_path, alert_rc=2, strict="true")

    assert proc.returncode == 1


def test_disarming_coverage_never_hides_a_real_phase_failure(tmp_path: Path) -> None:
    """Le désarmement ne touche QUE la couverture, jamais la garde structurelle."""
    proc, _, _ = _run_verdict(tmp_path, alert_rc=2, strict="false", failed=("brain-v42/connect",))

    assert proc.returncode == 1


def test_an_alert_without_a_coverage_line_is_not_an_error(tmp_path: Path) -> None:
    """`grep` ne trouve rien : sous `set -e`, ça ne doit pas tuer la nuit."""
    proc, _, _ = _run_verdict(tmp_path, alert_rc=0, alert_out="no failures for 2026-08-18")

    assert proc.returncode == 0
    assert "=== dream_runs COVERAGE" not in proc.stdout


_ARGPARSE_USAGE_ERROR = "\n".join(
    [
        "usage: post_run_alert.py [-h] --date DATE [--manifest MANIFEST]",
        "python -m scripts.dream.post_run_alert: error: unrecognized arguments: --manifest",
    ]
)


def test_a_rc2_without_a_coverage_line_is_never_read_as_a_gap(tmp_path: Path) -> None:
    """Le 2 d'argparse n'est PAS le 2 du verdict — mesuré, pas supposé.

    `parser.error()` sort en 2 par un `SystemExit` que le `except Exception` de
    `main()` ne peut pas intercepter. Le déclencheur est nommé par la spec §8 :
    au rollback dur du lecteur, `dream.sh` continue de passer `--manifest` à un
    `post_run_alert` qui ne le connaît plus. Sans preuve positive, chaque matin
    imprimerait « des lignes attendues manquent sans explication » et poserait
    une ligne `dream_runs` `coverage` mensongère — pour un drapeau inconnu.
    `uv` et l'interpréteur ont le même 2 d'usage, donc renuméroter l'escalade ne
    fermerait pas la classe ; exiger la ligne machine, si.
    """
    proc, alert_calls, _ = _run_verdict(tmp_path, alert_rc=2, alert_out=_ARGPARSE_USAGE_ERROR)

    assert "FAIL  dream_runs coverage" not in proc.stdout
    assert "record_coverage_gap" not in alert_calls
    assert "WARN  post_run_alert failed (rc=2)" in proc.stdout
    assert proc.returncode == 1, "le rapporteur muet rougit quand même — règle 3"


def test_a_rc2_without_a_coverage_line_stays_red_even_disarmed(tmp_path: Path) -> None:
    """L'interrupteur désarme la COUVERTURE, jamais un rapporteur cassé."""
    proc, _, _ = _run_verdict(tmp_path, alert_rc=2, alert_out=_ARGPARSE_USAGE_ERROR, strict="false")

    assert proc.returncode == 1
    assert "BRAIN_DREAM_COVERAGE_STRICT=false" not in proc.stdout


def test_dream_shell_remains_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(DREAM_SH)], check=True)


# --- T2 : le verdict porté jusqu'au briefing --------------------------------


def test_a_silent_gap_writes_the_coverage_row(tmp_path: Path) -> None:
    _, alert_calls, _ = _run_verdict(tmp_path, alert_rc=2)

    assert "scripts.dream.record_coverage_gap" in alert_calls
    record_call = next(line for line in alert_calls.splitlines() if "record_coverage_gap" in line)
    assert "--date 2026-08-18" in record_call
    assert "COVERAGE mode=manifest" in record_call
    assert "COVERAGE_SILENT silent=red/scan" in record_call


def test_a_green_night_writes_no_coverage_row(tmp_path: Path) -> None:
    _, alert_calls, _ = _run_verdict(tmp_path, alert_rc=0)

    assert "record_coverage_gap" not in alert_calls


def test_a_failing_writer_never_short_circuits_the_exit_guard(tmp_path: Path) -> None:
    """FINDING 4 exécuté : sans `set +e`, bash sortirait AVANT la garde.

    `set -e` est restauré juste après l'appel à l'alerte et la garde de sortie
    vit une trentaine de lignes plus bas. Un writer rendant 1 — argparse rc=2,
    `Settings()` invalide, `ValidationError` hors du `try` — ferait sortir
    dream.sh sur-le-champ. Le WARN ne serait jamais imprimé, et surtout le
    désarmement ci-dessous ne serait jamais consulté.
    """
    proc, _, _ = _run_verdict(tmp_path, alert_rc=2, record_rc=1)

    assert proc.returncode == 1
    assert "WARN  coverage — ligne dream_runs 'coverage' NON enregistrée (rc=1)" in proc.stdout


def test_a_failing_writer_still_lets_the_disarm_switch_be_read(tmp_path: Path) -> None:
    """La preuve que l'exécution CONTINUE après un writer en échec.

    Sans `set +e`, bash serait mort à l'appel : ni le WARN, ni la ligne de
    désarmement, et un code de sortie 1 au lieu de 0. Les trois assertions
    tombent ensemble.
    """
    proc, _, _ = _run_verdict(tmp_path, alert_rc=2, record_rc=1, strict="false")

    assert proc.returncode == 0
    assert "WARN  coverage — ligne dream_runs 'coverage' NON enregistrée" in proc.stdout
    assert "BRAIN_DREAM_COVERAGE_STRICT=false" in proc.stdout


def test_the_coverage_row_is_written_even_when_the_escalation_is_disarmed(
    tmp_path: Path,
) -> None:
    """Désarmer ne veut pas dire éteindre : le verdict continue d'atteindre le briefing."""
    _, alert_calls, _ = _run_verdict(tmp_path, alert_rc=2, strict="false")

    assert "scripts.dream.record_coverage_gap" in alert_calls

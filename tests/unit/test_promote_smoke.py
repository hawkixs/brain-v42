"""Unit tests for the PROMOTE-phase report check in scripts/dream/_promote_smoke.sh.

The smoke script used to duplicate the marker/JSON-body pattern in a
hand-rolled regex, commented as coming from a symbol
(``scripts/dream/promote_validate.py:_REPORT_RE``) that had already been
deleted — the operator runs this smoke after every restart, so a drift
between smoke and validator is invisible until a real report with a
trailing-word marker line is rejected by one and accepted by the other.

These tests extract the exact Python snippet the smoke script runs (so a
future edit to that block is exercised, not a copy pasted into the test),
run it as a real subprocess against fixtures, and check its verdict against
``scripts.dream.promote_validate.parse_report`` called directly — the two
must always agree, because the snippet is required to call that function
rather than reimplement it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from scripts.dream.promote_validate import ValidationFailure, parse_report

_SMOKE_SCRIPT = Path(__file__).parents[2] / "scripts" / "dream" / "_promote_smoke.sh"
_REPO_ROOT = Path(__file__).parents[2]
_TRAILING_WORD_FIXTURE = (
    Path(__file__).parent / "data" / "promote_report_marker_line_trailing_word.log"
)


def _extract_check_snippet() -> str:
    """Pull the exact Python block the smoke script feeds to ``python3 -c``.

    Deliberately reads the live file rather than a copy: if the block is
    ever rewritten to duplicate the validator's pattern again, this
    extraction still finds *a* snippet, but the agreement tests below (which
    exercise it as a subprocess) will fail once its verdict diverges from
    ``parse_report``.
    """
    text = _SMOKE_SCRIPT.read_text()
    start_marker = 'if uv run python3 -c "\n'
    end_marker = '\n"; then'
    start = text.index(start_marker) + len(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


def test_promote_smoke_check_calls_the_validators_parse_report() -> None:
    """Locks the fix: no hand-rolled marker/JSON regex duplicating the
    validator, and no reference to the deleted ``_REPORT_RE`` symbol.
    """
    snippet = _extract_check_snippet()
    assert "from scripts.dream.promote_validate import" in snippet
    assert "parse_report" in snippet
    assert "_REPORT_RE" not in snippet
    assert "re.search" not in snippet


def _run_check(raw_text: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    log_path = tmp_path / "iter_1.log"
    log_path.write_text(raw_text)
    snippet = _extract_check_snippet().replace("$log", str(log_path))
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(_REPO_ROOT / "src")}
    return subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_promote_smoke_check_agrees_with_validator_on_trailing_word_fixture(
    tmp_path: Path,
) -> None:
    """The synthetic fixture (trailing word on the marker line, valid JSON
    body) must PASS both the smoke check and the validator, and the two
    must report the same target_type/dry_run.
    """
    raw = _TRAILING_WORD_FIXTURE.read_text()
    expected = parse_report(raw)  # must not raise

    result = _run_check(raw, tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"PASS: target_type={expected['target_type']}" in result.stdout
    assert f"dry_run={expected['dry_run']}" in result.stdout


def test_promote_smoke_check_agrees_with_validator_on_prose_prefixed_marker(
    tmp_path: Path,
) -> None:
    """The codex rail writes the model's last message verbatim, so a
    closing sentence can share the marker's line ('Voici le rapport.
    === PROMOTE REPORT ==='). Both smoke and validator must accept it and
    report the same target_type/dry_run.
    """
    raw = (
        "Voici le rapport. === PROMOTE REPORT ===\n"
        '{"target_type": "adr", "candidate_id": "abc", "dry_run": false}\n'
        "=== END ==="
    )
    expected = parse_report(raw)  # must not raise

    result = _run_check(raw, tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"PASS: target_type={expected['target_type']}" in result.stdout
    assert f"dry_run={expected['dry_run']}" in result.stdout


def test_promote_smoke_check_agrees_with_validator_on_malformed_json(
    tmp_path: Path,
) -> None:
    """A trailing-word marker line must not launder a malformed JSON body —
    both smoke and validator must refuse it.
    """
    raw = "=== PROMOTE REPORT === Bettina\n{not: valid, json}\n=== END ==="
    with pytest.raises(ValidationFailure, match="malformed JSON"):
        parse_report(raw)

    result = _run_check(raw, tmp_path)

    assert result.returncode == 1
    assert "FAIL: malformed JSON" in result.stdout


def test_promote_smoke_check_agrees_with_validator_on_missing_markers(
    tmp_path: Path,
) -> None:
    """No markers at all is the other refusal path — both must reject it."""
    raw = "the model rambled with no markers at all"
    with pytest.raises(ValidationFailure, match="missing PROMOTE REPORT markers"):
        parse_report(raw)

    result = _run_check(raw, tmp_path)

    assert result.returncode == 1
    assert "FAIL: missing PROMOTE REPORT markers" in result.stdout

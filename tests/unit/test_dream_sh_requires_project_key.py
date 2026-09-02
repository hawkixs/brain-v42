"""`dream.sh` requires its project. No default, at any level.

The writers batch made `--project-key` required-without-default on the three CLIs
that write `dream_runs`. That guard stopped at the Python binary: `dream.sh`
carried `PROJECT_KEY="${1:-brain-v42}"`, so a bare launch satisfied the required
flag with `brain-v42` and labelled the night with another project — exactly the
class of bug the decision targeted, one layer above where the guard had been
placed.

The repository contained both forms and the reference had to be chosen:
`post_run_alert.py` (`default=DEFAULT_PROJECT_KEY`) against `promote_prepare.py`
(`required=True`). Settled by the operator on 2026-08-09: `required`. The pool
batch then removed the parameter from `post_run_alert` — it filtered nothing, and
the project now lives in the report's grouped body.

No night changes behaviour. The systemd unit passes the key explicitly
(`ExecStart=… scripts/dream.sh brain-v42`) and the six test harnesses already pass
`test-project`. Only a bare `bash scripts/dream.sh` breaks, and that is the point.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"


def _sandbox(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """An executable copy of dream.sh, out of production in every respect.

    `LOG_DIR` is `$SCRIPT_DIR/../logs/dream`, so the copy logs under `tmp_path`. A
    private `XDG_RUNTIME_DIR`, otherwise the script would exit 0 on finding
    production's flock taken — a test green for nothing. `uv` and `claude` are
    no-ops: no network call, no database write.
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
    """Without a project, the night does not start — it falls back on nobody.

    The failure mode this test forbids is silent by construction: a mislabelled
    night produces perfectly valid `dream_runs` rows, and nothing in the corpus
    afterwards lets anyone know they lie. No backfill is possible.
    """
    proc = _run(tmp_path)

    assert proc.returncode == 2, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "project" in proc.stderr.lower()


def test_no_default_survives_in_the_source() -> None:
    """The shape, not only the effect — a default reintroduced elsewhere in the
    script would turn the execution test green without restoring the guard."""
    source = DREAM_SH.read_text(encoding="utf-8")

    assert "${1:-brain-v42}" not in source
    assert 'PROJECT_KEY="${1:-' not in source


def test_the_historic_aliases_are_still_normalized(tmp_path: Path) -> None:
    """`brain` and `brain_v42` are still converted: the guard adds a requirement,
    it does not remove the normalization the rest of the repository expects."""
    proc = _run(tmp_path, "brain_v42")

    logs = sorted((tmp_path / "logs" / "dream").glob("*.log"))
    started = "\n".join(
        path.read_text(encoding="utf-8", errors="replace") for path in logs if "_" not in path.name
    )

    assert "project=brain-v42" in started, f"log={started!r} stderr={proc.stderr!r}"

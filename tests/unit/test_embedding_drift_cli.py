"""`--json` must emit JSON on stdout, whatever happens.

The point of the flag is a report a runbook can branch on. structlog's default
logger prints to stdout, so an unguarded run interleaves `[info] …` lines with
the document and every consumer's parse fails -- the same stdout-pollution
failure this repository already knows from its MCP stdio servers. And an
unreachable database must still answer in JSON: a machine reading the switch
runbook's preflight has to see `verdict: unmeasurable`, not a bare stack trace
on stderr and an empty stdout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_embedding_model_drift.py"

#: A port nothing listens on: the connection is refused immediately, so the
#: test measures the failure path without waiting on a timeout. It goes through
#: POSTGRES_URL and not `--postgres-url`, because the flag is deliberately a
#: FALLBACK -- `settings_for_standalone_script` prefers the environment, and a
#: repository `.env` would otherwise point this run at the live database.
UNREACHABLE_DSN = "postgresql+asyncpg://brain@127.0.0.1:1/brain"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
        env={**os.environ, "POSTGRES_URL": UNREACHABLE_DSN},
    )


def test_json_mode_emits_a_parseable_document_when_the_database_is_unreachable() -> None:
    result = _run("--json")

    payload = json.loads(result.stdout)
    assert payload["verdict"] == "unmeasurable"
    assert payload["sampled"] == 0
    assert payload["problems"], "an unmeasurable run must say why"


def test_an_unmeasurable_run_exits_two_and_never_zero() -> None:
    """Fail closed: a preflight that cannot measure must not read as a pass."""
    result = _run("--json")

    assert result.returncode == 2


def test_stdout_carries_nothing_but_the_document() -> None:
    result = _run("--json")

    assert result.stdout.lstrip().startswith("{")
    assert "[info" not in result.stdout


def test_the_report_names_the_vector_tables_it_did_not_check() -> None:
    """A MATCH must never read as "the whole corpus is fine".

    Nine tables carry a vector; the sample covers the six whose text
    `embedding_text_from_row` can recompose. The other three hold 2239 of 8811
    embedded rows (measured 2026-09-21) — so after a switch the sampled six can
    be freshly written while a quarter of the corpus is still the old model's.
    The blind spot is stated on every run rather than left for the operator to
    infer.
    """
    result = _run("--json")
    payload = json.loads(result.stdout)

    assert {entry["table"] for entry in payload["unchecked"]} == {
        "indexed_plans",
        "indexed_plan_chunks",
        "gitlab_events",
    }


def test_features_is_checked_now_that_it_has_a_reindex_path() -> None:
    """The table that corrupts a decision rather than a search result is not a blind spot.

    `features` was unsampled for one honest reason: nothing could reindex it, so
    naming it as unchecked was the whole truth. `regen_embeddings.py` now rewrites
    it from `description`, which makes it reproducible from the row alone — and a
    check that keeps ignoring it cannot fail on it, which is precisely how a
    post-switch verification returns a false green on the 920 rows that drive
    `cluster_guard`'s semantic dedup at COSINE_LINK = 0.70.
    """
    result = _run("--json")
    payload = json.loads(result.stdout)

    assert "features" not in {entry["table"] for entry in payload["unchecked"]}

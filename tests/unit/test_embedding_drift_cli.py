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


def _load_script_module():
    """Import the CLI as a module so its table map can be asserted on."""
    import importlib.util
    import sys as _sys

    spec = importlib.util.spec_from_file_location("scripts_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    _sys.modules["scripts_under_test"] = module
    spec.loader.exec_module(module)
    return module


_load_script_module()

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


def test_every_vector_table_is_sampled_now() -> None:
    """The three-table blind spot is closed, and this pins that it stays closed.

    It used to be honest to name `indexed_plans`, `indexed_plan_chunks` and
    `gitlab_events` as unchecked: nothing could recompose their text, so the
    check could only announce them. They held 2239 of 8811 embedded rows, so a
    provider switch could leave a quarter of the corpus on the old model and
    still report MATCH — and `indexed_plan_chunks`, 1792 of those rows, is
    served by `brain_search`.

    They are sampled now because the write paths and the checker call the same
    composers. A table dropped from this map would go silent again, which is
    exactly how the hole opened.
    """
    from scripts_under_test import SAMPLED_TABLES  # noqa: PLC0415

    assert {table for table, _ in SAMPLED_TABLES.values()} == {
        "decisions",
        "learnings",
        "snippets",
        "runbooks",
        "adrs",
        "features",
        "indexed_plans",
        "indexed_plan_chunks",
        "gitlab_events",
    }


def test_the_report_names_what_it_refuses_to_judge() -> None:
    """A MATCH must never read as "the whole corpus is fine".

    Some rows cannot reproduce their own embedding input whatever the checker
    does: `gitlab_ingestor` embeds `text[:2000]` and stores `text[:500]`, and
    127 of that table's 239 rows sit at the storage ceiling. Guessing from a
    truncation would accuse a vector nobody touched, so those rows are refused
    — and refusing silently would be the old blind spot under a new name.
    """
    result = _run("--json")
    payload = json.loads(result.stdout)

    assert [entry["table"] for entry in payload["unverifiable"]] == ["gitlab_events"]
    assert payload["unverifiable"][0]["reason"]


def test_features_is_checked_now_that_it_has_a_reindex_path() -> None:
    """The table that corrupts a decision rather than a search result is not a blind spot.

    `features` was unsampled for one honest reason: nothing could reindex it, so
    naming it as unchecked was the whole truth. `regen_embeddings.py` now rewrites
    it from `description`, which makes it reproducible from the row alone — and a
    check that keeps ignoring it cannot fail on it, which is precisely how a
    post-switch verification returns a false green on the 920 rows that drive
    `cluster_guard`'s semantic dedup at COSINE_LINK = 0.70.
    """
    from scripts_under_test import SAMPLED_TABLES  # noqa: PLC0415

    assert "features" in {table for table, _ in SAMPLED_TABLES.values()}


def test_the_report_breaks_the_sample_down_by_type() -> None:
    """A global median cannot fail on a type it averages away.

    The sample is spread across the six covered types, so one type wholly
    written by another model is a minority of the rows: the median does not
    move and the check says MATCH. The breakdown is what an operator reads to
    see it. Measured against production on 2026-09-21, this is not theoretical
    -- `feature` sat at median 0.7937 with 8 of 8 below threshold while the
    global median was 0.9999.
    """
    result = _run("--json")
    payload = json.loads(result.stdout)

    assert "by_type" in payload, "the verdict must not be the only per-type signal"
    assert payload["by_type"] == [], "an unmeasurable run has no type to break down"

"""Migration 049 — the sweep series, the under-declared rail, two vocabulary words.

A grouping under criterion (c) of signed decision 9d22bc6a: three objects of one
family (nullable ADD COLUMN + CHECK widening) whose downgrades fail
INDEPENDENTLY — it is that independence, tested here, that makes the
multi-object head legitimate. M-C (checkpoint) does not join it: its delivery
product approval is still due (d04dc588). M-D stays isolated and takes the next
head.

The static template of 045: these tests read the migration file — they prove its
SHAPE (chain, idempotence, named refusals), not its execution; execution is
proven by the integration suite, which applies the whole chain against
brain_test on every run.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[3]
MIGRATION = ROOT / "alembic" / "versions" / "049_dream_run_series_and_freshness_vocabulary.py"
TABLES = ROOT / "src" / "brain_v42" / "db" / "tables.py"
PIN = ROOT / "src" / "brain_v42" / "maintenance" / "plan_index_repair_store.py"

_SIX_TABLES = ("adrs", "decisions", "indexed_plans", "learnings", "runbooks", "snippets")


def _text() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_migration_049_chains_from_048() -> None:
    text = _text()
    assert 'revision = "049"' in text
    assert 'down_revision = "048"' in text


def test_the_pin_moves_in_the_same_commit() -> None:
    """The corridor rule: migration and pin travel together, never apart."""
    assert '_REQUIRED_ALEMBIC_HEAD = "049"' in PIN.read_text(encoding="utf-8")


def test_both_columns_are_added_idempotently_and_nullable() -> None:
    """Replayable BY HAND (048 template): someone will replay these lines during
    the cutover. And nullable without a default — NULL means "written before
    049", never backfilled: a retroactive zero would lie about uncounted nights."""
    text = _text()
    assert "ADD COLUMN IF NOT EXISTS closed_inactive_count INTEGER" in text
    assert "ADD COLUMN IF NOT EXISTS thinking_tokens INTEGER" in text
    assert "DEFAULT" not in text.upper().replace("WITHOUT A DEFAULT", ""), (
        "aucun défaut : NULL est porteur de sens (pré-049 / non mesuré)"
    )


def test_the_vocabulary_gains_exactly_two_words_on_all_six_tables() -> None:
    text = _text()
    assert '_NEW_SOURCES = ("manual_update", "plan_reindex")' in text
    assert '_SOURCES_BEFORE = ("merge", "judgment", "score", "revive")' in text
    for table in _SIX_TABLES:
        assert f'"{table}"' in text, f"{table} absent des six tables du decay"


def test_tables_py_mirrors_the_extended_vocabulary() -> None:
    """The application metadata and the database must speak the SAME vocabulary —
    041 landed one day with nothing flagging it; never again."""
    tables = TABLES.read_text(encoding="utf-8")
    extended = (
        "freshness_source IS NULL OR freshness_source IN "
        "('merge', 'judgment', 'score', 'revive', 'manual_update', 'plan_reindex')"
    )
    assert tables.count(extended) == 6
    assert '"closed_inactive_count"' in tables
    assert '"thinking_tokens"' in tables


def test_the_downgrade_carries_three_independent_named_refusals() -> None:
    """It is the independence of the refusals that made the grouping legitimate
    (9d22bc6a, criterion (c)): three destructions, three NAMED opt-ins, three
    separate DO blocks — never a generic flag that gets copied without rereading."""
    text = _text()
    for opt_in in (
        "allow_sweep_series_downgrade",
        "allow_thinking_tokens_downgrade",
        "allow_freshness_vocabulary_downgrade",
    ):
        assert opt_in in text, f"opt-in absent : {opt_in}"
    assert len(re.findall(r"DO \$\$", text)) >= 3, "chaque destruction porte son propre bloc DO"
    assert "RAISE EXCEPTION" in text


def test_the_vocabulary_downgrade_nulls_before_restoring_the_old_check() -> None:
    """Re-applying 043's CHECK against rows carrying the new values would simply
    fail. The opt-in resets them to NULL — a MISSING provenance is visible, a
    silently erased one is believed (043's own words) — and only then restores
    the old vocabulary."""
    text = _text()
    assert "SET freshness_source = NULL" in text
    restore_at = text.rfind("_SOURCES_BEFORE")
    null_at = text.find("SET freshness_source = NULL")
    assert 0 < null_at < restore_at, "le NULL doit précéder la restauration du CHECK"


def test_the_thinking_column_is_fed_separately_never_summed() -> None:
    """The point of ticket 76e11c9f: adding thinking to output_tokens would make
    the rails incomparable in the other direction. A separate column, fed by the
    only rail that measures it, NULL everywhere else."""
    parser = (ROOT / "src" / "brain_v42" / "metrics" / "agy_dream_parser.py").read_text(
        encoding="utf-8"
    )
    shared = (ROOT / "src" / "brain_v42" / "metrics" / "dream_parser.py").read_text(
        encoding="utf-8"
    )
    assert 'if "thinking_tokens" in result_usage:' in parser, (
        "absent du flux = NULL (pas mesuré), jamais 0 (mesuré nul)"
    )
    assert "thinking_tokens: int | None = None" in shared
    assert "telemetry.thinking_tokens if telemetry else None" in shared
    assert "output_tokens + telemetry.thinking" not in shared
    assert "thinking_tokens + telemetry.output" not in shared

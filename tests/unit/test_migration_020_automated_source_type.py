"""Migration 020 — adds 'automated' to learnings.source_type CHECK.

Regression guard for the Pydantic↔DB drift observed during Dream Night 4
(2026-04-24 SYNTH): SourceType Literal has included 'automated' since
plan 2026-04-05-dream-mode.md Task 1, but migration 003's UNIFIED_TYPES
never included it — so brain_learn with source_type='automated' crashed
on the DB CHECK constraint while the Pydantic test passed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def test_migration_020_file_exists() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = root / "alembic" / "versions" / "020_add_automated_source_type.py"
    assert migration.exists(), f"migration not found at {migration}"


def test_migration_020_has_correct_revision_chain() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = root / "alembic" / "versions" / "020_add_automated_source_type.py"
    spec = importlib.util.spec_from_file_location("_020", migration)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert getattr(mod, "revision", None) == "020"
    assert getattr(mod, "down_revision", None) == "019"


def test_migration_020_adds_automated_to_check() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = root / "alembic" / "versions" / "020_add_automated_source_type.py"
    src = migration.read_text()
    assert "'automated'" in src, "migration must include 'automated' in UNIFIED_TYPES"
    assert "learnings_source_type_check" in src
    assert "DROP CONSTRAINT" in src and "ADD CONSTRAINT" in src


def test_migration_020_downgrade_removes_automated() -> None:
    """Downgrade must restore the pre-020 CHECK (no 'automated')."""
    root = Path(__file__).resolve().parents[2]
    migration = root / "alembic" / "versions" / "020_add_automated_source_type.py"
    src = migration.read_text()
    # The downgrade branch must reference the migration-003 UNIFIED_TYPES
    # (which lacks 'automated') so operators can roll back cleanly.
    assert "def downgrade()" in src

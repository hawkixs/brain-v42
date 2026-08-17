"""Migration 023 — merged_into self-FK ON DELETE SET NULL (5 entity tables).

Root cause (2026-06-22 audit): merged_into was a bare UUID with no FK, so
deleting a merge TARGET orphaned every inbound merged_into pointer (54 dangling
learnings observed). This migration nulls existing dangling pointers and adds a
self-referential FK ON DELETE SET NULL so future target-deletes self-heal.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "023_merged_into_on_delete_set_null.py"
)
_TABLES = ("decisions", "learnings", "snippets", "runbooks", "adrs")


def test_migration_023_file_exists() -> None:
    assert _MIGRATION.exists(), f"migration not found at {_MIGRATION}"


def test_migration_023_revision_chain() -> None:
    spec = importlib.util.spec_from_file_location("_023", _MIGRATION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert getattr(mod, "revision", None) == "023"
    assert getattr(mod, "down_revision", None) == "022"


def test_migration_023_adds_on_delete_set_null_fk_for_all_tables() -> None:
    # The migration is DRY (loops over a _TABLES tuple), so verify that tuple
    # covers all 5 entity tables and that the SQL builds a self-referential
    # ON DELETE SET NULL FK from it.
    spec = importlib.util.spec_from_file_location("_023fk", _MIGRATION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert set(mod._TABLES) == set(_TABLES), "migration must cover all 5 entity tables"

    src = _MIGRATION.read_text()
    assert "ON DELETE SET NULL" in src
    assert "_merged_into_fkey" in src
    assert "FOREIGN KEY (merged_into)" in src
    assert "REFERENCES" in src


def test_migration_023_nulls_dangling_before_adding_fk() -> None:
    src = _MIGRATION.read_text()
    # Existing dangling pointers must be nulled, else ADD CONSTRAINT fails.
    assert "SET merged_into = NULL" in src
    assert "NOT EXISTS" in src


def test_migration_023_downgrade_drops_fks() -> None:
    src = _MIGRATION.read_text()
    assert "def downgrade()" in src
    assert "DROP CONSTRAINT" in src

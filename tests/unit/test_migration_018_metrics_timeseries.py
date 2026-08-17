"""Smoke test: migration 018 creates metrics_timeseries with correct shape."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def test_migration_018_file_exists() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = root / "alembic" / "versions" / "018_metrics_timeseries.py"
    assert migration.exists(), f"migration not found at {migration}"


def test_migration_018_has_correct_revision_chain() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = root / "alembic" / "versions" / "018_metrics_timeseries.py"
    spec = importlib.util.spec_from_file_location("_018", migration)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert getattr(mod, "revision", None) == "018"
    assert getattr(mod, "down_revision", None) == "017"


def test_migration_018_creates_table_with_required_columns() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = root / "alembic" / "versions" / "018_metrics_timeseries.py"
    src = migration.read_text()
    assert "metrics_timeseries" in src
    assert "bucket_ts" in src
    assert "metric" in src
    assert "value" in src
    # composite PK
    assert "PrimaryKeyConstraint" in src or "primary_key=True" in src


def test_metrics_timeseries_registered_in_tables_module() -> None:
    from brain_v42.db import tables as t

    assert hasattr(t, "metrics_timeseries"), "metrics_timeseries Table not declared"
    assert "metrics_timeseries" in t.METADATA.tables

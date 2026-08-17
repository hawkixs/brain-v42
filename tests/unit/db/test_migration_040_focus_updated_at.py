"""Static contract for migration 040 focus_updated_at."""

from pathlib import Path

ROOT = Path(__file__).parents[3]
MIGRATION = ROOT / "alembic" / "versions" / "040_project_context_focus_updated_at.py"


def test_migration_040_appends_focus_updated_at_to_the_039_chain() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "040"' in source
    assert 'down_revision = "039"' in source
    assert "focus_updated_at" in source


def test_migration_040_adds_one_nullable_column_and_nothing_else() -> None:
    """No server_default, no raw SQL: the column carries only what code writes.

    `op.execute` is absent on purpose. Its two plausible uses here are both
    defects: a backfill, and a touch of the 039 trigger function.
    """
    source = MIGRATION.read_text(encoding="utf-8")

    assert "nullable=True" in source
    assert "server_default" not in source
    assert "op.execute" not in source
    assert source.count("op.add_column") == 1
    assert source.count("op.drop_column") == 1


def test_migration_040_does_not_backfill_from_updated_at() -> None:
    """NULL means "never measured", and that is the honest value.

    `project_contexts.updated_at` moves on any write to the row, counters
    included, so copying it into `focus_updated_at` would mint a number whose
    label does not match its truth-maker — the exact defect this column exists
    to remove. NULL self-heals on the first real focus write.
    """
    source = MIGRATION.read_text(encoding="utf-8")

    assert "updated_at" not in source.replace("focus_updated_at", "")


def test_migration_040_leaves_the_039_trigger_function_untouched() -> None:
    """039 pins `set_project_context_updated_at` by SHA256 of its source and by
    byte length. Rewriting that body here would make 039 undowngradable, so
    `focus_updated_at` is written by application code and never by the trigger.
    """
    source = MIGRATION.read_text(encoding="utf-8")

    assert "set_project_context_updated_at" not in source
    assert "CREATE OR REPLACE TRIGGER" not in source
    assert "CREATE FUNCTION" not in source

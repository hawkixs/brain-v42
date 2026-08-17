"""Static contract for migration 039 project-context timestamp CAS support."""

from pathlib import Path

ROOT = Path(__file__).parents[3]
MIGRATION = ROOT / "alembic" / "versions" / "039_project_context_timestamp_cas.py"


def test_migration_039_installs_the_opt_in_project_context_trigger() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "039"' in source
    assert 'down_revision = "038"' in source
    assert "set_project_context_updated_at" in source
    assert "brain_v42.allow_explicit_project_context_updated_at" in source
    assert "CREATE OR REPLACE TRIGGER trg_project_contexts_updated" in source


def test_migration_039_fails_closed_around_catalog_drift() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    required_fragments = (
        "LOCK TABLE public.project_contexts IN ACCESS EXCLUSIVE MODE",
        "historical function contract mismatch",
        "dedicated function contract mismatch",
        "project_contexts trigger contract mismatch",
        "historical trigger binding mismatch",
        "tgattr = ''::int2vector",
        "tgqual IS NULL",
        "tgparentid = 0",
        "tgconstraint = 0",
        "tgconstrrelid = 0",
        "tgconstrindid = 0",
        "NOT t.tgdeferrable",
        "NOT t.tginitdeferred",
        "t.tgoldtable IS NULL",
        "t.tgnewtable IS NULL",
        "t.tgenabled = 'O'",
        "NOT t.tgisinternal",
        "t.tgnargs = 0",
        "t.tgargs = ''::bytea",
        "83ca0f7a3230405dae8b4f4e692b4983869b58e4225b6e60bbf96db3f6ae9a59",
        "60c6154d6230d1d0e9244d8f20bc6d6b30e887e71263692e54363c96e22c0419",
    )
    for fragment in required_fragments:
        assert fragment in source


def test_migration_039_downgrade_requires_exact_opt_in() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "context.get_x_argument(as_dictionary=True)" in source
    assert "allow_project_context_trigger_downgrade" in source
    drop_line = next(
        line
        for line in source.splitlines()
        if "DROP FUNCTION public.set_project_context_updated_at()" in line
    )
    assert "CASCADE" not in drop_line

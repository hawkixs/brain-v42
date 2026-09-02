"""045 — `dream_runs.model` stops refusing the configured model names.

Ticket `bcb5e6d8`. The column has been a `varchar(30)` since
`alembic/versions/013_dream_runs.py`. Two of the five configured phase models do
not fit in it, and one of the two — `nvidia/nemotron-3-super-120b-a12b`, 33
chars — is the **already configured** WET fallback: it would lose its row on the
day of the WET cutover, which is precisely the day one wants to measure.

What is lost is not the `model` column: it is the whole `dream_runs` ROW.
`StringDataRightTruncation` surfaces inside a best-effort `except Exception`
that prints `! warning: could not record dream_run` and continues. A night that
really ran would leave no trace at all.

The canary of 2026-08-16 makes this precondition HARD rather than prudential:
the only two live candidates to replace the dead DRY primary are 34 and 37 chars.

SCOPE OF THIS FILE. The `tables.py` test below is DOCUMENTARY and must not be
sold as anything else: the real writer is a raw SQL INSERT, where the length
declared in the SQLAlchemy metadata plays no role at execution time. Widening
`tables.py` without applying the migration would give a green locally against a
production still on `varchar(30)`. Only the revision measured in the database is
authoritative.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[3]
MIGRATION = ROOT / "alembic" / "versions" / "045_dream_run_model_width.py"
DREAM_SH = ROOT / "scripts" / "dream.sh"

# The target width. 120 and not "the longest + margin": a round number survives
# the next model name without asking for another migration.
TARGET_WIDTH = 120
PREVIOUS_WIDTH = 30


def _load_migration_module() -> object:
    """Load the revision by path: `alembic/versions` is not a package."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("migration_045", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _declared_model_width() -> int:
    """Length declared for `dream_runs.model` in the metadata."""
    from brain_v42.db.tables import dream_runs

    length = dream_runs.c.model.type.length
    assert isinstance(length, int)
    return length


def _rail_models() -> list[str]:
    """RAIL models, read from `dream.sh` — never retyped.

    The ticket thread asks for this explicitly: `configured_models()` enumerates
    only the five phase models, no rail model (codex, agy, claude). A guard
    reading only the phase inventory would stay blind to a future long rail name
    — exactly the failure mode 045 closes.
    """
    source = DREAM_SH.read_text(encoding="utf-8")
    pattern = re.compile(
        r"^BRAIN_DREAM_(?:CODEX|AGY|CLAUDE)_(?:FAST|DEEP)_MODEL="
        r'"\$\{BRAIN_DREAM_\w+:-(?P<model>[^}"]+)\}"',
        re.MULTILINE,
    )
    models = [match.group("model") for match in pattern.finditer(source)]
    assert models, "aucun modèle de rail lu dans dream.sh — le motif a dérivé"
    return models


def _all_configured_models() -> list[str]:
    from scripts.probe_model_liveness import configured_models

    return [entry.model for entry in configured_models()] + _rail_models()


def test_migration_045_chains_from_044() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "045"' in source
    assert 'down_revision = "044"' in source


def test_migration_045_widens_the_model_column_and_can_narrow_it_back() -> None:
    """Read the module's real widths, not how they are spelled in the source.

    A textual assertion on `length=120` would break on a named constant — that
    is, on a better piece of writing than the one it imposes.
    """
    module = _load_migration_module()

    assert module._TARGET_WIDTH == TARGET_WIDTH
    assert module._PREVIOUS_WIDTH == PREVIOUS_WIDTH
    assert MIGRATION.read_text(encoding="utf-8").count("alter_column") == 2


def test_migration_045_touches_only_the_model_column() -> None:
    """A width migration adds nothing, drops nothing, fills nothing."""
    source = MIGRATION.read_text(encoding="utf-8")

    assert "op.add_column" not in source
    assert "op.drop_column" not in source
    assert "UPDATE" not in source.upper().replace("UPDATED", "")


def test_migration_045_downgrade_refuses_to_truncate_existing_rows() -> None:
    """Shrinking silently would erase the proof that the column exists to carry.

    Postgres already refuses a `varchar(30)` for a 34-char value — the migration
    must say so instead of letting a bare driver error surface.
    """
    source = MIGRATION.read_text(encoding="utf-8")

    assert "char_length(model)" in source
    assert "raise" in source


def test_migration_045_drops_and_recreates_the_view_that_blocks_the_alter() -> None:
    """Measured in production on 2026-08-16, not anticipated: the ALTER is refused.

        FeatureNotSupportedError: cannot alter type of a column used by a view
        DETAIL: rule _RETURN on view codex_dream_run_v1 depends on column "model"

    Postgres refuses to retype a column a view projects. The view must drop
    before, and come back after — in both directions of the migration.
    """
    source = MIGRATION.read_text(encoding="utf-8")
    module = _load_migration_module()

    # Both directions: upgrade and downgrade hit the same view.
    assert source.count("DROP VIEW IF EXISTS codex_dream_run_v1") == 2
    assert source.count("op.execute(_DREAM_RUN_VIEW_SQL)") == 2

    # A DROP VIEW takes its GRANTs with it: `codex_ro` must be re-served on both
    # sides, otherwise the view comes back with no reader and the failure is read
    # on the client side.
    assert module._GRANT_SQL == "GRANT SELECT ON codex_dream_run_v1 TO codex_ro"
    assert source.count("op.execute(_GRANT_SQL)") == 2


def test_migration_045_reuses_the_036_definition_instead_of_retyping_the_view() -> None:
    """Copying the SELECT would make 045 a second source of truth.

    The codex contract is guarded by `test_codex_contract_views_036.py`. A view
    recreated by hand would drift from it at the first forgotten column, and the
    drift would be visible only on the codex side, at read time.
    """
    source = MIGRATION.read_text(encoding="utf-8")

    assert "_CREATE_DREAM_RUN_VIEW" in source
    assert "CREATE OR REPLACE VIEW codex_dream_run_v1" not in source
    assert (
        _load_migration_module()
        ._DREAM_RUN_VIEW_SQL.strip()
        .startswith("CREATE OR REPLACE VIEW codex_dream_run_v1")
    )


def test_declared_width_holds_every_configured_model() -> None:
    """DOCUMENTARY guard: the inventory is read, never copied.

    A list retyped here would drift from the real configuration and would go
    green on models nobody calls any more (learning 93dc2ec2).
    """
    declared = _declared_model_width()
    for model in _all_configured_models():
        assert len(model) <= declared, (
            f"{model!r} ({len(model)} car.) ne tient pas dans varchar({declared})"
        )


def test_the_two_sqlite_mirrors_do_not_drift_from_tables_py() -> None:
    """Test mirrors are re-declared by hand: they can stay at 30.

    A mirror left narrow would pass a test against a constraint production no
    longer has — the false green in the other direction.
    """
    declared = _declared_model_width()
    mirrors = (
        ROOT / "tests" / "integration" / "test_session_start_briefing.py",
        ROOT / "tests" / "unit" / "services" / "test_dream_run_service.py",
    )
    for mirror in mirrors:
        assert f'Column("model", String({declared}))' in mirror.read_text(encoding="utf-8"), (
            f"{mirror.name} déclare une largeur de `model` différente de tables.py"
        )

"""RED contracts for the persistent session lifecycle v4 schema."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_session_v4_columns_and_artifact_ledger_are_declared() -> None:
    from brain_v42.db import tables

    assert {
        "last_heartbeat_at",
        "end_expected_focus_revision",
        "focus_outcome",
        "focus_at_end",
        "focus_revision_at_end",
    } <= set(tables.brain_sessions.c.keys())

    ledger = tables.brain_session_artifacts
    assert set(ledger.c.keys()) == {
        "knowledge_id",
        "session_id",
        "knowledge_type",
        "captured_at",
        # 048 : PAR QUELLE CLÉ cette ligne a été attribuée.
        "attribution_mode",
    }
    assert ledger.c.knowledge_id.primary_key is True
    session_fk = next(iter(ledger.c.session_id.foreign_keys))
    assert session_fk.column.table.name == "brain_sessions"
    assert session_fk.ondelete == "CASCADE"

    check_names = {
        constraint.name
        for constraint in ledger.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert "brain_session_artifacts_type_valid" in check_names
    assert "idx_brain_session_artifacts_session_captured" in {
        index.name for index in ledger.indexes
    }


def test_migration_037_extends_036_and_is_reversible() -> None:
    migration = PROJECT_ROOT / "alembic" / "versions" / "037_session_lifecycle_v4.py"

    assert migration.is_file()
    source = migration.read_text(encoding="utf-8")
    assert 'revision = "037"' in source
    assert 'down_revision = "036"' in source
    assert "CREATE TABLE brain_session_artifacts" in source
    assert "ADD COLUMN last_heartbeat_at" in source
    assert "ADD COLUMN focus_outcome" in source
    assert "COUNT(DISTINCT existing_session.id) > 1" in source
    assert "SELECT DISTINCT" in source
    assert "focus_revision_at_end = end_expected_focus_revision + 1" in source
    assert "focus_revision_at_end <> end_expected_focus_revision" in source
    assert "DROP TABLE IF EXISTS brain_session_artifacts" in source
    assert "DROP COLUMN IF EXISTS last_heartbeat_at" in source


def test_migration_037_downgrade_refuses_lossy_v4_state() -> None:
    migration = PROJECT_ROOT / "alembic" / "versions" / "037_session_lifecycle_v4.py"

    source = migration.read_text(encoding="utf-8")

    assert "cannot downgrade session lifecycle v4 with unsnapshotted artifacts" in source
    assert "cannot downgrade session lifecycle v4 with conflicted focus outcomes" in source
    assert source.index("cannot downgrade session lifecycle v4") < source.index(
        "DROP TABLE IF EXISTS brain_session_artifacts"
    )


def test_migration_048_upgrade_is_replayable_by_hand() -> None:
    """Une promesse d'idempotence tenue à moitié piège qui rejoue à la main.

    Alembic annule la révision entière sur échec, donc un `upgrade` interrompu
    ne laissait rien derrière lui — le défaut n'était pas là. Il était dans la
    PROMESSE : `ADD COLUMN IF NOT EXISTS` à côté d'un `ADD CONSTRAINT` qui
    n'existe pas en variante `IF NOT EXISTS`. Or l'ordre de bascule de ce lot
    demande explicitement d'appliquer la 048 et de la VÉRIFIER avant tout
    redémarrage : quelqu'un rejouera ces instructions à la main.

    On garde donc les trois objets sur la même promesse — colonne, contrainte,
    index — la contrainte par un DROP-IF-EXISTS préalable, gabarit de la 047.
    """
    import importlib.util
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[3] / "alembic" / "versions" / "048_attribution_mode.py"
    )
    spec = importlib.util.spec_from_file_location("_migration_048_probe", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert "IF EXISTS" in module._DROP_CHECK
    assert "IF NOT EXISTS" not in module._ADD_CHECK, (
        "Postgres n'a pas d'ADD CONSTRAINT IF NOT EXISTS — le DROP préalable EST le mécanisme"
    )
    assert module._DROP_CHECK != module._ADD_CHECK

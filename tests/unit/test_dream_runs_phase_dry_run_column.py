"""Verify dream_runs has the phase_dry_run column after migration 022."""


def test_dream_runs_has_phase_dry_run_column():
    from brain_v42.db.tables import dream_runs

    assert "phase_dry_run" in dream_runs.c
    col = dream_runs.c.phase_dry_run
    assert str(col.type) == "BOOLEAN"
    assert col.nullable is False
    assert col.server_default is not None

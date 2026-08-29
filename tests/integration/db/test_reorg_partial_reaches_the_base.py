"""Une ValidationFailure REORG écrit RÉELLEMENT `status='partial'` en base.

Ticket `38104499` : le 2026-08-20, G4 a détecté une vraie masked-failure, le
marquage a crashé (`RuntimeError: … attached to a different loop`), et le log a
imprimé « dream_runs marked partial » — trois couches, la troisième mentait.
Les correctifs (`536bc0a` : une seule boucle ; `36645bf` : le log n'affirme
plus un marquage non observé) sont sur main, gardés par un harnais UNITAIRE à
pool factice (`test_dream_validators_single_event_loop.py`).

Ce que ce harnais ne peut structurellement pas prouver, et que la table
mesurée crie — 0 `partial` REORG sur toute l'histoire contre 1 côté promote —
c'est l'ÉCRITURE : que le chemin réel (`main()` → `Settings` → engine réel →
`_amain` → `_mark_dream_run_partial`) dépose la ligne dans un vrai PostgreSQL.
Ce test-ci joue une ValidationFailure de bout en bout contre la base
d'intégration et lit la ligne — pas le log.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _asyncpg_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _run_sql(dsn: str, query: str, *args: object) -> list[tuple]:
    async def run() -> list[tuple]:
        import asyncpg

        connection = await asyncpg.connect(dsn)
        try:
            return [tuple(row) for row in await connection.fetch(query, *args)]
        finally:
            await connection.close()

    return asyncio.run(run())


def test_a_reorg_validation_failure_lands_as_partial_in_dream_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_migrations: None
) -> None:
    from tests.integration.conftest import INTEGRATION_DB_URL

    if not INTEGRATION_DB_URL:
        pytest.skip("BRAIN_V42_TEST_DB_URL is not set")
    monkeypatch.setenv("POSTGRES_URL", INTEGRATION_DB_URL)
    dsn = _asyncpg_dsn(INTEGRATION_DB_URL)

    project_key = f"integ-reorg-partial-{uuid.uuid4().hex[:8]}"
    [(dream_run_id,)] = _run_sql(
        dsn,
        "INSERT INTO dream_runs (run_date, phase, status, project_key) "
        "VALUES (current_date, 'reorg', 'done', $1) RETURNING id",
        project_key,
    )

    # Un rapport WET SANS marqueurs : `found_marker=False` lève AVANT toute
    # lecture d'entité — la ValidationFailure la plus courte qui existe, et
    # celle-là même que la ligne `partial` de promote porte depuis juin.
    report_log = tmp_path / "reorg.log"
    report_log.write_text("prose du run, aucun trailer machine\n", encoding="utf-8")
    events = tmp_path / "events.jsonl"
    events.write_text("", encoding="utf-8")
    tags_before = tmp_path / "tags_before.json"
    tags_before.write_text(json.dumps({}), encoding="utf-8")

    try:
        from scripts.dream.reorg_validate import main

        rc = main(
            [
                "--report-log",
                str(report_log),
                "--events-jsonl",
                str(events),
                "--tags-before-json",
                str(tags_before),
                "--project-key",
                project_key,
                "--dream-run-id",
                str(dream_run_id),
            ]
        )

        assert rc == 1
        [(status, error_message)] = _run_sql(
            dsn,
            "SELECT status, error_message FROM dream_runs WHERE id = $1",
            dream_run_id,
        )
        assert status == "partial", (
            "la ValidationFailure n'a pas atteint la base — c'est exactement le "
            "mensonge à trois couches du 2026-08-20"
        )
        assert "missing REORG REPORT markers" in str(error_message)
    finally:
        _run_sql(dsn, "DELETE FROM dream_runs WHERE id = $1", dream_run_id)

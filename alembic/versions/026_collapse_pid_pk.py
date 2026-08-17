"""Collapse process_metrics PRIMARY KEY to bare (agent_name) — post-cutover end-state.

Removes `pid` from the composite PK (agent_name, pid) introduced in migration
025 for the stdio↔HTTP coexistence window.  After full HTTP-fleet cutover,
there is exactly one writer per agent_name (the single HTTP server), so the
composite key is no longer needed.

pid column is RETAINED (NOT dropped) for back-compat: collector_db.py computes
active_processes = COUNT(DISTINCT pid), and the SELECT at line 125 reads it.

Upgrade steps:
  1. Deduplicate: keep only the row with the latest updated_at per agent_name.
     A ctid tie-break (keep the higher ctid) makes the choice deterministic when
     two rows share the exact same updated_at timestamp.
  2. Drop the composite PK constraint.
  3. Relax pid to NULLable (it is no longer a key; the flusher still writes it as
     a plain column for the active_processes = COUNT(DISTINCT pid) back-compat).
  4. Create the new single-column PK on agent_name.

Downgrade (lossy — acceptable; rows age out within 1 h via the flusher cleanup):
  1. Drop the single-column PK.
  2. Backfill any NULL pid to 0 and re-impose NOT NULL (the composite PK needs it).
  3. Restore the composite PK on (agent_name, pid).

DEPLOY ORDERING WARNING
-----------------------
Migration 026 (bare agent_name PK) and the flusher's ON CONFLICT (agent_name)
MUST ship together.  Mismatches in either direction cause a hard PostgreSQL error
at write time ("no unique constraint matching given keys for ON CONFLICT"):

  - Flusher with ON CONFLICT (agent_name) vs DB still at composite PK 025
    → INSERT will fail every 30 s per process.
  - Flusher with old ON CONFLICT (agent_name, pid) vs DB at bare PK 026
    → INSERT will also fail.

Sequence: run `alembic upgrade 026` first, then deploy the new flusher binary.
If a rollback is needed: revert the flusher first, then `alembic downgrade -1`.

Revision ID: 026
Revises: 025
Create Date: 2026-06-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Deduplicate: for each agent_name, delete all but the row with the
    #    latest updated_at.  When updated_at ties, keep the row with the
    #    greater ctid (physically later in the heap) for determinism.
    op.execute("""
        DELETE FROM process_metrics a
        USING process_metrics b
        WHERE a.agent_name = b.agent_name
          AND (
              a.updated_at < b.updated_at
              OR (a.updated_at = b.updated_at AND a.ctid < b.ctid)
          )
    """)

    # 2. Drop the composite PK (agent_name, pid) from migration 025.
    op.drop_constraint("process_metrics_pkey", "process_metrics", type_="primary")

    # 3. pid is no longer part of the PK. Dropping the PK does NOT drop the
    #    implicit NOT NULL that migration 025 imposed, so relax it explicitly.
    #    pid stays a plain column the flusher still writes (active_processes
    #    back-compat) — it is simply no longer a key.
    op.alter_column("process_metrics", "pid", existing_type=sa.Integer(), nullable=True)

    # 4. Create the new single-column PK on agent_name.
    #    The constraint name is passed explicitly — PostgreSQL would auto-derive
    #    "process_metrics_pkey" for a single-column PK too, but we pass it
    #    explicitly so downgrade() can DROP it by name without ambiguity and so
    #    the name is stable across environments.
    op.create_primary_key("process_metrics_pkey", "process_metrics", ["agent_name"])


def downgrade() -> None:
    # Lossy: the dedup that happened during upgrade cannot be undone — rows that
    # were removed are gone.  This is acceptable because process_metrics rows age
    # out within 1 h anyway via the flusher's cleanup query.

    # 1. Drop the single-column PK.
    op.drop_constraint("process_metrics_pkey", "process_metrics", type_="primary")

    # 2. The composite PK requires pid to be NOT NULL with no NULL values. The
    #    corrected flusher always writes pid, but backfill defensively (0) so a
    #    row written by any other path cannot break the PK recreation.
    op.execute("UPDATE process_metrics SET pid = 0 WHERE pid IS NULL")
    op.alter_column("process_metrics", "pid", existing_type=sa.Integer(), nullable=False)

    # 3. Restore the composite PK (agent_name, pid) from migration 025.
    op.create_primary_key("process_metrics_pkey", "process_metrics", ["agent_name", "pid"])

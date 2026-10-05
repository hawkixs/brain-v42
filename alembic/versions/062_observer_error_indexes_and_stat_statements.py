"""062 — observer error indexes, and pg_stat_statements in schema `monitoring`.

Two independent platform changes that share one revision.

1. Two PARTIAL indexes on ``delivery_confirmations``. The delivery observer counts
   the recent failures of every due job (``outcome = 'error'``, a few hundred rows
   out of about a hundred thousand) and no existing index leads with ``binding_id``
   or ``ticket_id``. A partial index is usable only when the query's own clauses
   imply its predicate, and CHECK constraints do not count: the binding predicate
   therefore names ``subject_kind = 'artifact_binding'`` and the query must say so too.

2. ``pg_stat_statements``, created in a dedicated schema. The extension is created
   in ``monitoring`` and NOT in ``public`` because its scripts grant SELECT on the
   view to PUBLIC, and recovery contract ACL v11 counts every PUBLIC grant on a
   relation of ``public`` as a mismatch. Nothing is left in ``public``: ACL v11, the
   schema fingerprint and the family guard are unchanged, and only the database-wide
   extension inventory moves.

Operational facts, measured rather than assumed:

* Locking. Alembic runs the revision in one transaction, so the indexes are plain
  ``CREATE INDEX`` (a SHARE lock, writers wait), not CONCURRENTLY, which would need an
  ``autocommit_block`` and could leave an INVALID index behind. The partial indexes
  cover about 520 rows of a 16 MB heap: well under a second. ``lock_timeout`` is set
  to 30 s for the revision so a stuck lock holder fails the migration atomically
  instead of queueing every writer behind it. Revisit above about 10M rows.
* Privileges. ``pg_stat_statements`` is not a trusted extension: CREATE EXTENSION
  needs a superuser. Under a least-privilege migration role, a superuser pre-creates
  it (``CREATE SCHEMA monitoring; CREATE EXTENSION pg_stat_statements WITH SCHEMA
  monitoring``) and ``IF NOT EXISTS`` makes this step a no-op; the downgrade then
  needs the extension owner. Reading the view needs ``pg_read_all_stats`` and USAGE
  on ``monitoring``.
* Preload. CREATE EXTENSION does not need ``shared_preload_libraries``; only reading
  the view does. On a server without the library the objects exist and the view
  raises SQLSTATE 55000 until it is preloaded.
* Downgrade drops the extension, then the schema with RESTRICT (the default): an
  operator's own object in ``monitoring`` makes the downgrade fail loudly instead of
  being dropped. Dropping the extension loses nothing durable, the statistics live in
  shared memory.
"""

from __future__ import annotations

from alembic import op

revision = "062"
down_revision = "061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '30s'")
    op.execute(
        "CREATE INDEX idx_delivery_confirmations_binding_errors "
        "ON delivery_confirmations (binding_id, collection_finished_at) "
        "WHERE outcome = 'error' AND subject_kind = 'artifact_binding'"
    )
    op.execute(
        "CREATE INDEX idx_delivery_confirmations_context_errors "
        "ON delivery_confirmations "
        "(ticket_id, contract_revision, attempt, context_set_digest, collection_finished_at) "
        "WHERE outcome = 'error' AND subject_kind = 'repository_context'"
    )
    op.execute("CREATE SCHEMA IF NOT EXISTS monitoring")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements WITH SCHEMA monitoring")
    op.execute("SET LOCAL lock_timeout TO DEFAULT")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS pg_stat_statements")
    op.execute("DROP SCHEMA IF EXISTS monitoring")
    op.execute("DROP INDEX IF EXISTS idx_delivery_confirmations_context_errors")
    op.execute("DROP INDEX IF EXISTS idx_delivery_confirmations_binding_errors")

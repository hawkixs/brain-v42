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
  monitoring``) and both steps then become no-ops. An extension that already lives in
  another schema (for instance ``public``, whose PUBLIC grants ACL v11 refuses) is
  RELOCATED with ``ALTER EXTENSION ... SET SCHEMA monitoring`` (it is relocatable;
  refusing would only block the cutover) and needs the extension owner. The schema step is guarded by a
  ``pg_namespace`` lookup rather than ``CREATE SCHEMA IF NOT EXISTS``, because the
  latter checks the CREATE privilege on the database BEFORE it checks existence and
  would still fail for a role without it; ``CREATE EXTENSION IF NOT EXISTS`` checks
  existence first. Reading the view needs USAGE on ``monitoring``;
  ``pg_read_all_stats`` is only needed to see other roles' query text.
* Preload. CREATE EXTENSION does not need ``shared_preload_libraries``; only reading
  the view does. On a server without the library the objects exist and the view
  raises SQLSTATE 55000 until it is preloaded.
* Downgrade drops the extension, then the schema with RESTRICT (the default): an
  operator's own object in ``monitoring`` makes the downgrade fail loudly instead of
  being dropped. Dropping the extension loses nothing durable, the statistics live in
  shared memory. It drops the extension and the schema even if a superuser pre-created
  them, and needs the extension owner: a rollback to the 061 code (downgrade 061)
  therefore needs a superuser. The downgrade also runs under a 30 s ``lock_timeout``
  and rolls back entirely if a concurrent transaction holds ``delivery_confirmations``.
* Delivery observer. Its inserts carry a 5 s lock_timeout; stop it while 062 holds
  its SHARE lock, or accept one lost pass (SQLSTATE 55P03).
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
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'monitoring') THEN "
        "CREATE SCHEMA monitoring; END IF; END $$"
    )
    # IF NOT EXISTS alone would silently leave an extension that already lives
    # elsewhere (typically `public`, with its PUBLIC grants, which ACL v11 refuses)
    # and `monitoring.pg_stat_statements` would not exist. The extension is
    # relocatable, so it is moved: refusing would block the cutover for nothing.
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements') THEN "
        "IF (SELECT extnamespace::regnamespace::text FROM pg_extension "
        "WHERE extname = 'pg_stat_statements') <> 'monitoring' THEN "
        "ALTER EXTENSION pg_stat_statements SET SCHEMA monitoring; END IF; "
        "ELSE CREATE EXTENSION pg_stat_statements WITH SCHEMA monitoring; END IF; END $$"
    )
    op.execute("SET LOCAL lock_timeout TO DEFAULT")


def downgrade() -> None:
    # DROP INDEX needs ACCESS EXCLUSIVE and env.py disables the statement timeout, so
    # the revision bounds its own wait; a timeout rolls the whole downgrade back.
    op.execute("SET LOCAL lock_timeout = '30s'")
    op.execute("DROP EXTENSION IF EXISTS pg_stat_statements")
    op.execute("DROP SCHEMA IF EXISTS monitoring")
    op.execute("DROP INDEX IF EXISTS idx_delivery_confirmations_context_errors")
    op.execute("DROP INDEX IF EXISTS idx_delivery_confirmations_binding_errors")
    op.execute("SET LOCAL lock_timeout TO DEFAULT")

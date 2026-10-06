"""063 — client credential registry, admin elevations and the schema compatibility ledger.

Three new tables, no change to an existing one.

* ``brain_client_credentials``: one row per issued bearer credential. Only the SHA-256
  digest of the token is stored, never the token. ``families`` is the set of capability
  families the credential may exercise; ``admin`` is deliberately NOT storable (the
  CHECK is the second wall behind the repository's refusal), so administrative power
  can only come from a time-boxed elevation. A ``transition`` credential is a rotation
  overlap and MUST carry an expiry. Rows are revoked, never deleted by the application.
  An AFTER row trigger sends ``pg_notify('brain_client_credentials', <row id>)`` so that
  a running server can reload its registry; the payload is the id only, never a digest.
* ``brain_admin_elevations``: a grant of administrative power to the connections of ONE
  operator session, at most four hours long. It follows the session's deletion, like
  ``brain_session_connections`` (061).
* ``brain_schema_compat``: the ledger of the oldest code head compatible with a schema
  head. This revision inserts no row: a later step's migrate command records it.

Locking: the foreign key takes a SHARE ROW EXCLUSIVE lock on ``brain_sessions``, so the
revision bounds its own wait with a 30 s ``lock_timeout`` (same device as 062) and a
stuck lock holder fails the migration atomically.
"""

from __future__ import annotations

from alembic import op

revision = "063"
down_revision = "062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '30s'")
    op.execute(
        """
        CREATE TABLE brain_client_credentials (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            client_id text NOT NULL,
            token_sha256 bytea NOT NULL,
            families text[] NOT NULL,
            issuers text[] NOT NULL DEFAULT '{}',
            transition boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now(),
            created_by text NOT NULL,
            expires_at timestamptz,
            revoked_at timestamptz,
            revoked_reason text,
            last_used_at timestamptz,
            CONSTRAINT brain_client_credentials_token_sha256_key UNIQUE (token_sha256),
            CONSTRAINT brain_client_credentials_client_id_format
                CHECK (client_id ~ '^[a-z0-9][a-z0-9.-]{0,63}$'),
            CONSTRAINT brain_client_credentials_token_sha256_length
                CHECK (octet_length(token_sha256) = 32),
            CONSTRAINT brain_client_credentials_families_valid
                CHECK (cardinality(families) >= 1
                       AND families <@ ARRAY['read','write','delivery','telemetry']::text[]),
            CONSTRAINT brain_client_credentials_created_by_nonblank
                CHECK (length(btrim(created_by)) > 0),
            CONSTRAINT brain_client_credentials_transition_expires
                CHECK (NOT transition OR expires_at IS NOT NULL),
            CONSTRAINT brain_client_credentials_revocation_pair
                CHECK ((revoked_at IS NULL) = (revoked_reason IS NULL))
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_brain_client_credentials_client_id "
        "ON brain_client_credentials (client_id)"
    )
    op.execute(
        """
        CREATE FUNCTION brain_client_credentials_notify() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('brain_client_credentials', COALESCE(NEW.id, OLD.id)::text);
            RETURN NULL;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER brain_client_credentials_notify
        AFTER INSERT OR UPDATE OR DELETE ON brain_client_credentials
        FOR EACH ROW EXECUTE FUNCTION brain_client_credentials_notify()
        """
    )
    op.execute(
        """
        CREATE TABLE brain_admin_elevations (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id uuid NOT NULL
                REFERENCES brain_sessions (id) ON DELETE CASCADE,
            connection_ids text[] NOT NULL,
            granted_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            granted_by text NOT NULL,
            reason text NOT NULL,
            revoked_at timestamptz,
            CONSTRAINT brain_admin_elevations_connection_ids_nonempty
                CHECK (cardinality(connection_ids) >= 1),
            CONSTRAINT brain_admin_elevations_window
                CHECK (expires_at > granted_at
                       AND expires_at <= granted_at + interval '4 hours'),
            CONSTRAINT brain_admin_elevations_reason_nonblank
                CHECK (length(btrim(reason)) > 0)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_brain_admin_elevations_session_id ON brain_admin_elevations (session_id)"
    )
    op.execute(
        """
        CREATE TABLE brain_schema_compat (
            schema_head text PRIMARY KEY,
            oldest_compatible_code_head text NOT NULL,
            recorded_at timestamptz NOT NULL DEFAULT now(),
            recorded_by_version text NOT NULL
        )
        """
    )
    op.execute("SET LOCAL lock_timeout TO DEFAULT")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '30s'")
    op.execute("DROP TABLE brain_schema_compat")
    op.execute("DROP TABLE brain_admin_elevations")
    op.execute("DROP TRIGGER brain_client_credentials_notify ON brain_client_credentials")
    op.execute("DROP FUNCTION brain_client_credentials_notify()")
    op.execute("DROP TABLE brain_client_credentials")
    op.execute("SET LOCAL lock_timeout TO DEFAULT")

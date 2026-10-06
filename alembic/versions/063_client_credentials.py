"""063 — client credential registry, admin elevations, the schema compatibility ledger and the audit outbox.

Four new tables, a nullable column on ``brain_session_connections`` and another on
``brain_sessions``.

* ``brain_client_credentials``: one row per issued bearer credential. Only the SHA-256
  digest of the token is stored, never the token. ``families`` is the set of capability
  families the credential may exercise; ``admin`` is deliberately NOT storable (the
  CHECK is the second wall behind the repository's refusal), so administrative power
  can only come from a time-boxed elevation. ``elevate`` and ``telemetry`` belong to no
  tool: a credential holding ``elevate`` holds nothing else (exclusivity CHECK), so the
  credential that asks for an elevation can never exercise one. A ``transition`` credential is a rotation
  overlap and MUST carry an expiry. Rows are revoked, never deleted by the application.
  AFTER row triggers send ``pg_notify('brain_client_credentials', <row id>)`` so that
  a running server can reload its registry; the payload is the id only, never a digest.
  An UPDATE that changes nothing but ``last_used_at`` does not notify: that stamp is
  routine bookkeeping and would otherwise reload every verifier cache.
* ``brain_admin_elevations``: a grant of administrative power to the connections of ONE
  operator session, at most four hours long. It follows the session's deletion, like
  ``brain_session_connections`` (061). The grant freezes ATTRIBUTED pairs: the
  ``connection_ids`` and the parallel ``connection_client_ids`` (same cardinality, no
  NULL), so a checker compares a call's (connection, credential) to what was frozen and
  never to the mutable connection table. Only the pairs of an ALLOWLISTED client are
  frozen: the other attributed connections are not elevated, and the grant records them
  (``excluded_client_ids``, sorted and distinct, and ``excluded_connection_count``) so
  the audit event can say what it left out. ``via`` says which gesture granted it, and
  the requester follows it in both directions (CHECKs): a ``hook`` grant names its
  requester, a ``cli`` grant names none. Elevations never exclude one another.
  ``expiry_audited_at`` marks the elevation whose natural expiry has been audited.
* ``brain_session_connections.client_id``: the credential that first presented the
  connection, NULL on every historical row. Written once by the recording call sites
  (a later step); this revision only adds the column and its format CHECK.
* ``brain_sessions.opener_client_id``: the attribution lock. The client that owns an
  OPERATOR session (every nature but ``agent``, fail-closed: NULL and ``operator`` are both
  locked), claimed by the first allowlisted client that
  attaches (the repository's ``claim_or_check_session_owner``, before it writes the
  connection row). An AFTER row trigger on ``brain_session_connections`` is the
  backstop: it refuses an attributed row on an operator session whose opener is NULL or
  another client, so a writer that forgets the check still cannot attach a foreign
  client. Rows with a NULL ``client_id`` (historical) and agent traces are not checked.
  The opener only ever moves from NULL to a value: a BEFORE UPDATE trigger on
  ``brain_sessions`` refuses to change or clear a set one. That is also why the connection
  trigger needs no row lock: a stale read sees NULL, which it refuses, or the final value.
  The trigger is AFTER, not BEFORE, so the column's format CHECK still answers first.
  The refusal message names no connection id.
* ``brain_schema_compat``: the ledger of the oldest code head compatible with a schema
  head. This revision inserts no row: a later step's migrate command records it.
* ``brain_credential_audit``: the outbox of the credential gestures (issue, revoke, elevate,
  unelevate, natural expiry). Why a table: the CLI runs through ``docker exec``, whose
  stdout never reaches the container's log stream that the operator's watcher follows, so
  a gesture inserts its row in its OWN transaction and the server's drainer emits it. The
  drain is at-least-once: ``emitted_at`` stays NULL until the event has been emitted, and
  a crash between the two re-emits, so a consumer dedupes on ``(event, elevation_id)``,
  which this table also makes unique per elevation. ``elevation_id`` has no foreign key
  on purpose: the audit row must outlive the elevation, which follows its session's
  deletion. ``payload`` is the event's body; it never holds a token, a digest or a
  connection id. An AFTER INSERT trigger sends ``pg_notify('brain_credential_audit',
  <row id>)`` to wake the drainer, the id only. A partial index serves the unemitted rows.

Locking: the foreign key takes a SHARE ROW EXCLUSIVE lock on ``brain_sessions``, and the
``ALTER TABLE`` statements an ACCESS EXCLUSIVE lock on ``brain_session_connections`` and on
``brain_sessions`` (metadata-only changes: the columns have no default, so no row is
rewritten; the CHECK on a column that is NULL everywhere validates instantly), so the revision bounds its own wait with a 30 s ``lock_timeout`` (same device as 062) and a
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
                       AND families <@ ARRAY['read','write','delivery','telemetry','elevate']::text[]),
            CONSTRAINT brain_client_credentials_elevate_exclusive
                CHECK (NOT ('elevate' = ANY (families)) OR cardinality(families) = 1),
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
        AFTER INSERT OR DELETE ON brain_client_credentials
        FOR EACH ROW EXECUTE FUNCTION brain_client_credentials_notify()
        """
    )
    op.execute(
        """
        CREATE TRIGGER brain_client_credentials_notify_update
        AFTER UPDATE ON brain_client_credentials
        FOR EACH ROW
        WHEN ((to_jsonb(OLD) - 'last_used_at') IS DISTINCT FROM (to_jsonb(NEW) - 'last_used_at'))
        EXECUTE FUNCTION brain_client_credentials_notify()
        """
    )
    op.execute(
        """
        CREATE TABLE brain_admin_elevations (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id uuid NOT NULL
                REFERENCES brain_sessions (id) ON DELETE CASCADE,
            connection_ids text[] NOT NULL,
            connection_client_ids text[] NOT NULL,
            granted_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            granted_by text NOT NULL,
            reason text NOT NULL,
            via text NOT NULL DEFAULT 'cli',
            requested_by_client_id text,
            excluded_client_ids text[] NOT NULL DEFAULT '{}',
            excluded_connection_count integer NOT NULL DEFAULT 0,
            revoked_at timestamptz,
            expiry_audited_at timestamptz,
            CONSTRAINT brain_admin_elevations_connection_ids_nonempty
                CHECK (cardinality(connection_ids) >= 1),
            CONSTRAINT brain_admin_elevations_connection_pairs
                CHECK (cardinality(connection_client_ids) = cardinality(connection_ids)
                       AND array_position(connection_client_ids, NULL) IS NULL),
            CONSTRAINT brain_admin_elevations_via_valid
                CHECK (via IN ('hook', 'cli')),
            CONSTRAINT brain_admin_elevations_hook_requester
                CHECK (via <> 'hook' OR requested_by_client_id IS NOT NULL),
            CONSTRAINT brain_admin_elevations_cli_no_requester
                CHECK (via <> 'cli' OR requested_by_client_id IS NULL),
            CONSTRAINT brain_admin_elevations_excluded_count
                CHECK (excluded_connection_count >= 0),
            CONSTRAINT brain_admin_elevations_reason_length
                CHECK (char_length(reason) BETWEEN 1 AND 200),
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
    op.execute(
        """
        CREATE TABLE brain_credential_audit (
            id bigserial PRIMARY KEY,
            event text NOT NULL,
            elevation_id uuid,
            payload jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            emitted_at timestamptz,
            CONSTRAINT brain_credential_audit_event_valid
                CHECK (event IN ('credentials.issued', 'credentials.revoked',
                                 'credentials.elevated', 'credentials.unelevated',
                                 'credentials.elevation_expired')),
            CONSTRAINT brain_credential_audit_elevation_pair
                CHECK ((event IN ('credentials.elevated', 'credentials.unelevated',
                                  'credentials.elevation_expired'))
                       = (elevation_id IS NOT NULL)),
            CONSTRAINT brain_credential_audit_payload_object
                CHECK (jsonb_typeof(payload) = 'object')
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_brain_credential_audit_event_elevation "
        "ON brain_credential_audit (event, elevation_id) WHERE elevation_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX idx_brain_credential_audit_unemitted "
        "ON brain_credential_audit (id) WHERE emitted_at IS NULL"
    )
    op.execute(
        """
        CREATE FUNCTION brain_credential_audit_notify() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('brain_credential_audit', NEW.id::text);
            RETURN NULL;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER brain_credential_audit_notify
        AFTER INSERT ON brain_credential_audit
        FOR EACH ROW EXECUTE FUNCTION brain_credential_audit_notify()
        """
    )
    op.execute(
        """
        ALTER TABLE brain_session_connections
            ADD COLUMN client_id text,
            ADD CONSTRAINT brain_session_connections_client_id_format
                CHECK (client_id IS NULL OR client_id ~ '^[a-z0-9][a-z0-9.-]{0,63}$')
        """
    )
    op.execute(
        """
        ALTER TABLE brain_sessions
            ADD COLUMN opener_client_id text,
            ADD CONSTRAINT brain_sessions_opener_client_id_format
                CHECK (opener_client_id IS NULL
                       OR opener_client_id ~ '^[a-z0-9][a-z0-9.-]{0,63}$')
        """
    )
    op.execute(
        """
        CREATE FUNCTION brain_session_connections_owner_check() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            owner_nature text;
            owner_opener text;
        BEGIN
            SELECT nature, opener_client_id INTO owner_nature, owner_opener
            FROM brain_sessions WHERE id = NEW.session_id;
            IF FOUND AND owner_nature IS DISTINCT FROM 'agent' AND owner_opener IS DISTINCT FROM NEW.client_id THEN
                RAISE EXCEPTION
                    'brain_session_connections: foreign client attach to an operator session refused'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER brain_session_connections_owner_check
        BEFORE INSERT OR UPDATE OF client_id ON brain_session_connections
        FOR EACH ROW WHEN (NEW.client_id IS NOT NULL)
        EXECUTE FUNCTION brain_session_connections_owner_check()
        """
    )
    op.execute(
        """
        CREATE FUNCTION brain_sessions_opener_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.opener_client_id IS NOT NULL
               AND NEW.opener_client_id IS DISTINCT FROM OLD.opener_client_id THEN
                RAISE EXCEPTION 'brain_sessions: the opener of a session never changes'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER brain_sessions_opener_immutable
        BEFORE UPDATE OF opener_client_id ON brain_sessions
        FOR EACH ROW EXECUTE FUNCTION brain_sessions_opener_immutable()
        """
    )
    op.execute("SET LOCAL lock_timeout TO DEFAULT")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '30s'")
    op.execute("DROP TRIGGER brain_session_connections_owner_check ON brain_session_connections")
    op.execute("DROP FUNCTION brain_session_connections_owner_check()")
    op.execute("DROP TRIGGER brain_sessions_opener_immutable ON brain_sessions")
    op.execute("DROP FUNCTION brain_sessions_opener_immutable()")
    op.execute(
        "ALTER TABLE brain_sessions "
        "DROP CONSTRAINT brain_sessions_opener_client_id_format, DROP COLUMN opener_client_id"
    )
    op.execute(
        "ALTER TABLE brain_session_connections "
        "DROP CONSTRAINT brain_session_connections_client_id_format, DROP COLUMN client_id"
    )
    op.execute("DROP TRIGGER brain_credential_audit_notify ON brain_credential_audit")
    op.execute("DROP FUNCTION brain_credential_audit_notify()")
    op.execute("DROP TABLE brain_credential_audit")
    op.execute("DROP TABLE brain_schema_compat")
    op.execute("DROP TABLE brain_admin_elevations")
    op.execute("DROP TRIGGER brain_client_credentials_notify_update ON brain_client_credentials")
    op.execute("DROP TRIGGER brain_client_credentials_notify ON brain_client_credentials")
    op.execute("DROP FUNCTION brain_client_credentials_notify()")
    op.execute("DROP TABLE brain_client_credentials")
    op.execute("SET LOCAL lock_timeout TO DEFAULT")

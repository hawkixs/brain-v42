"""Store issuer-declared delivery attestations as append-only ledger facts.

Revision ID: 054
Revises: 053

Ticket 04bc1f4a, from red-rail. Brain is the LEDGER: it STORES attestations and
validates their FORM (kind vocabulary shape, JSON payload, digest), and it NEVER
evaluates their kinds. red-rail is POLICY and computes DORA metrics by READING
these rows.

APPEND-ONLY BY CODE PATH, like `delivery_receipts`: there is deliberately NO
trigger. The repository exposes INSERT and SELECT only; no UPDATE or DELETE path
exists, and the unique idempotency key makes a replay land on the same row
instead of duplicating it. A row enters the table once and is never rewritten.

The table is additive: it references `delivery_workflows` and
`delivery_contract_revisions`, both created by 053, and reaches no pre-053 table
other than through those. `contract_revision` is NULLABLE on purpose — NULL
means the issuer attests about no particular revision — and the composite
foreign key is not enforced when it is NULL, which is exactly the intent.
"""

from __future__ import annotations

from alembic import context, op

revision = "054"
down_revision = "053"
branch_labels = None
depends_on = None

#: NAMED opt-in, 052's template — never a generic flag.
_OPT_IN = "allow_attestation_downgrade"


def upgrade() -> None:
    op.execute("""
    CREATE TABLE delivery_attestations (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      ticket_id UUID NOT NULL REFERENCES delivery_workflows(ticket_id) ON DELETE RESTRICT,
      contract_revision INTEGER,
      kind VARCHAR(64) NOT NULL,
      payload JSONB NOT NULL,
      digest VARCHAR(64) NOT NULL,
      issuer_project VARCHAR(50) NOT NULL,
      issuer_identity VARCHAR(200) NOT NULL,
      idempotency_key VARCHAR(200) NOT NULL,
      emitted_at TIMESTAMPTZ NOT NULL,
      recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT uq_delivery_attestation_idempotency UNIQUE (ticket_id, issuer_project, idempotency_key),
      CONSTRAINT delivery_attestations_kind_valid CHECK (kind ~ '^[a-z][a-z0-9_]{0,63}$'),
      CONSTRAINT delivery_attestations_payload_object CHECK (jsonb_typeof(payload) = 'object'),
      CONSTRAINT delivery_attestations_digest_valid CHECK (digest ~ '^[0-9a-f]{64}$'),
      FOREIGN KEY (ticket_id, contract_revision) REFERENCES delivery_contract_revisions(ticket_id, contract_revision) ON DELETE RESTRICT
    )""")
    op.execute(
        "CREATE INDEX ix_delivery_attestations_ticket_emitted ON delivery_attestations (ticket_id, emitted_at DESC, id DESC)"
    )
    # The second read scope: one issuer project across its tickets, which is how
    # red-rail computes a project's metrics without walking every ticket. Both
    # indexes put the scope column first and the sort keys next, with no `kind`
    # between them: the unfiltered newest-first read of a scope is then an ordered
    # index scan, and a kind filter is a cheap predicate on that same scan.
    op.execute(
        "CREATE INDEX ix_delivery_attestations_issuer_emitted ON delivery_attestations (issuer_project, emitted_at DESC, id DESC)"
    )


def downgrade() -> None:
    arguments = context.get_x_argument(as_dictionary=True)
    opted = arguments.get(_OPT_IN) == "yes"

    # The refusal NAMES the row count, because that is what disappears: an
    # attestation exists in no other column and cannot be recomputed from
    # anything — the issuer declared it once, on its own clock. Dropping the
    # table destroys the only copy.
    op.execute(
        f"""
        DO $$
        DECLARE
            attestations bigint;
        BEGIN
            SELECT count(*) INTO attestations FROM delivery_attestations;

            IF {"FALSE" if opted else "TRUE"} AND attestations > 0 THEN
                RAISE EXCEPTION
                    'cannot downgrade 054: delivery_attestations holds % row(s). '
                    'Each one is an issuer-declared fact that exists nowhere else and '
                    'cannot be recomputed. Export it, or rerun with '
                    '-x {_OPT_IN}=yes',
                    attestations;
            END IF;
        END;
        $$
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_delivery_attestations_issuer_emitted")
    op.execute("DROP INDEX IF EXISTS ix_delivery_attestations_ticket_emitted")
    op.execute("DROP TABLE IF EXISTS delivery_attestations")

"""Add exact credential project scopes without widening historical credentials.

NULL preserves unrestricted scope. The existing whole-row NOTIFY trigger already
observes scope updates. Scope changes use the credential audit outbox; its expanded
event vocabulary survives downgrade so historical audit rows remain readable.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "067"
down_revision = "066"
branch_labels = None
depends_on = None

_OPT_IN = "allow_credential_scope_downgrade"

# The round trip rejects commas inside elements; the CLI and repository also
# enforce distinctness and the 50-character limit without a schema-owned function.
_PROJECT_KEY = "[a-z0-9]+([:-][a-z0-9]+)*"
_ALLOWED_PROJECTS = (
    "allowed_projects IS NULL OR COALESCE("
    "cardinality(allowed_projects) >= 1 AND "
    "CASE WHEN array_ndims(allowed_projects) = 1 THEN "
    "array_position(allowed_projects, NULL) IS NULL "
    "AND cardinality(string_to_array(array_to_string(allowed_projects, ','), ',')) "
    "= cardinality(allowed_projects) "
    f"AND array_to_string(allowed_projects, ',') ~ '^({_PROJECT_KEY})(,{_PROJECT_KEY})*$' "
    "ELSE false END, false)"
)


def upgrade() -> None:
    op.add_column(
        "brain_client_credentials",
        sa.Column("allowed_projects", postgresql.ARRAY(sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        "brain_client_credentials_allowed_projects_valid",
        "brain_client_credentials",
        _ALLOWED_PROJECTS,
    )
    op.drop_constraint(
        "brain_credential_audit_event_valid", "brain_credential_audit", type_="check"
    )
    op.create_check_constraint(
        "brain_credential_audit_event_valid",
        "brain_credential_audit",
        "event IN ('credentials.issued', 'credentials.revoked', 'credentials.elevated', "
        "'credentials.unelevated', 'credentials.elevation_expired', 'credentials.scope_changed')",
    )


def downgrade() -> None:
    scoped = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM brain_client_credentials WHERE allowed_projects IS NOT NULL"
            )
        )
        .scalar_one()
    )
    opted_in = (context.get_x_argument(as_dictionary=True) or {}).get(_OPT_IN) == "yes"
    if scoped and not opted_in:
        raise RuntimeError(
            f"{scoped} credential(s) carry project scope; dropping it grants unrestricted "
            f"authority. Re-run with -x {_OPT_IN}=yes to accept that."
        )
    op.drop_constraint(
        "brain_client_credentials_allowed_projects_valid", "brain_client_credentials", type_="check"
    )
    op.drop_column("brain_client_credentials", "allowed_projects")
    # Audit events outlive their source state; never erase their vocabulary or rows.

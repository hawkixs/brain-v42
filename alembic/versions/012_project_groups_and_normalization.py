"""Project groups & key normalization.

Add project_group column to project_contexts, normalize all project_key
values from snake_case to kebab-case, remove orphan test keys, populate
groups, and add a CHECK constraint enforcing kebab format.

Revision ID: 012
Revises: 011
Create Date: 2026-03-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None

# ── Tables with project_key and their nullability ────────────────────────────
NULLABLE_TABLES = ["decisions", "learnings", "snippets", "search_log"]
NOT_NULL_TABLES = [
    "runbooks",
    "adrs",
    "project_contexts",
    "features",
    "indexed_plans",
    "gitlab_events",
]
ALL_TABLES = NULLABLE_TABLES + NOT_NULL_TABLES

# ── Key renames (snake_case → kebab-case) ────────────────────────────────────
RENAMES: dict[str, str] = {
    "red_ml": "red-ml",
    "red_data": "red-data",
    "red_story": "red-story",
    "red_daemon": "red-daemon",
    "red_llm": "red-llm",
    "red_tsdb": "red-tsdb",
    "brain_v42": "brain-v42",
    "auto_discord": "auto-discord",
    "poc_lyriks_v2": "poc-lyriks-v2",
    "datalake_v2": "datalake-v2",
    "mrc_rag": "mrc-rag",
    "hk_infofeed": "hk-infofeed",
    "hk_anime_list": "hk-anime-list",
    "red_writer": "red-writer",
    # orphan keys that may still be in snake_case form
    "e2e_test_cleanup": "e2e-test-cleanup",
    "e2e_test_project": "e2e-test-project",
    "test_deploy": "test-deploy",
    "test_pock": "test-pock",
}

# ── Orphan keys to delete (after rename, these are the kebab forms) ──────────
ORPHAN_KEYS = [
    "e2e-test-cleanup",
    "e2e-test-project",
    "test-deploy",
    "test-pock",
]

# ── Merge-duplicate keys (target already exists, drop the older row) ─────────
MERGE_KEYS = ["poc-lyriks-v2", "datalake-v2"]

# ── Group assignments ────────────────────────────────────────────────────────
GROUPS: dict[str, list[str]] = {
    "red": [
        "red",
        "red-api",
        "red-ml",
        "red-lab",
        "red-alerts",
        "red-orchestrator",
        "red-monitor",
        "red-llm",
        "red-tsdb",
        "red-daemon",
        "red-data",
        "red-story",
        "red-lab:architect",
        "red-lab:developer",
        "red-lab:developer-gemini",
        "red-lab:developer-opus",
        "red-lab:orchestrator",
        "red-lab:reviewer",
        "red-lab:sentinel",
        "red-lab:shared",
        "red-lab:overseer",
        "red-writer",
    ],
    "lyriks": [
        "lyriks",
        "lyriks-v3",
        "poc-lyriks-v2",
        "lyriks-backend-v2",
        "hackathon-lyriks",
    ],
    "watchk": ["watchk", "watchk-claude", "watchk-memory", "watchk-gitk"],
    "infra": ["brain-v42", "hawkixs-infra", "auto-discord"],
    "datalake": ["datalake-v1", "datalake-v2", "second-cerveau"],
}


def upgrade() -> None:
    # ── Step 1: ADD COLUMN project_group ─────────────────────────────────────
    op.add_column(
        "project_contexts",
        sa.Column("project_group", sa.String(50), nullable=True),
    )
    op.create_index("idx_project_contexts_group", "project_contexts", ["project_group"])

    # ── Step 2: RENAME KEYS (snake_case → kebab-case) ───────────────────────
    conn = op.get_bind()

    # 2a. Handle merge-duplicates FIRST: delete the OLD project_contexts row
    # (the snake_case one) before renaming, since the kebab target already exists.
    # Entities (decisions, learnings etc.) will be renamed to the target key below.
    for old_key, new_key in RENAMES.items():
        if new_key in MERGE_KEYS:
            # Delete the snake_case project_contexts row (target already exists)
            conn.execute(
                sa.text("DELETE FROM project_contexts WHERE project_key = :old"),
                {"old": old_key},
            )

    # 2b. Rename all entity tables (including project_contexts for non-merge keys)
    for old_key, new_key in RENAMES.items():
        for table in ALL_TABLES:
            # For merge keys, skip project_contexts (source row already deleted)
            if new_key in MERGE_KEYS and table == "project_contexts":
                continue
            conn.execute(
                sa.text(f"UPDATE {table} SET project_key = :new WHERE project_key = :old"),
                {"old": old_key, "new": new_key},
            )

    # 2c. Update related_projects arrays in project_contexts
    for old_key, new_key in RENAMES.items():
        conn.execute(
            sa.text(
                "UPDATE project_contexts "
                "SET related_projects = array_replace(related_projects, :old, :new) "
                "WHERE :old = ANY(related_projects)"
            ),
            {"old": old_key, "new": new_key},
        )

    # ── Step 3: DELETE ORPHANS ───────────────────────────────────────────────
    for key in ORPHAN_KEYS:
        # Nullable tables: set project_key = NULL
        for table in NULLABLE_TABLES:
            conn.execute(
                sa.text(f"UPDATE {table} SET project_key = NULL WHERE project_key = :key"),
                {"key": key},
            )
        # NOT NULL tables: delete rows
        for table in NOT_NULL_TABLES:
            conn.execute(
                sa.text(f"DELETE FROM {table} WHERE project_key = :key"),
                {"key": key},
            )

    # ── Step 4: POPULATE GROUPS ──────────────────────────────────────────────
    for group_name, keys in GROUPS.items():
        conn.execute(
            sa.text(
                "UPDATE project_contexts SET project_group = :grp WHERE project_key = ANY(:keys)"
            ),
            {"grp": group_name, "keys": keys},
        )

    # ── Step 5: ADD CHECK CONSTRAINT ─────────────────────────────────────────
    op.execute(
        "ALTER TABLE project_contexts "
        "ADD CONSTRAINT chk_project_key_format "
        "CHECK (project_key ~ '^[a-z0-9]+([:-][a-z0-9]+)*$')"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE project_contexts DROP CONSTRAINT IF EXISTS chk_project_key_format")
    op.drop_index("idx_project_contexts_group", table_name="project_contexts")
    op.drop_column("project_contexts", "project_group")
    # NOTE: key renames are NOT reversed in downgrade

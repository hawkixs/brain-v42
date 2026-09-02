# Curated Roadmap — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Curate the emerging roadmap (652 features, 500 in `research`): mechanical stock purge, nightly proposer-only LLM curator, briefing surfacing, write-back via a dedicated MCP tool, hardened dream metrics sidecar.

**Architecture:** Proven extract/backfill pattern — Python CLI (`scripts/roadmap_curate.py`, exact skeleton of `ticket_extract.py`), audit table `roadmap_curation_proposals` (mirrors 029), killswitched `dream.sh` step, human review via `--apply-ids`. The briefing replaces "In-flight" with a live "Roadmap" section; `brain_feature_update` (tool 42) closes the session→roadmap loop.

**Tech Stack:** Python 3.12+, SQLAlchemy 2.0 async (Core + `sa.text`), Alembic, FastMCP, httpx (NVIDIA API strict JSON without tools), pytest-asyncio, bash (dream.sh).

**Spec source:** `docs/superpowers/specs/2026-07-04-roadmap-curation-design.md`

## Global Constraints

- Brain `project_key` = `brain-v42` (hyphen) for every `brain_*` call.
- Coverage ≥ 60%; gates per task: `uv run pytest tests/unit`, `uv run ruff check src/ tests/ scripts/`, `uv run ruff format --check src/ tests/ scripts/`, `uv run mypy src/` — CI runs `ruff format --check`, `ruff check` alone is NOT enough.
- Conventional Commits, commit as soon as a unit is green (project rule).
- GitNexus: `gitnexus_impact({target, direction: "upstream"})` before modifying any existing symbol; `gitnexus_detect_changes()` before every commit.
- Never DELETE on `features` (the `feature_artifacts` FK ON DELETE CASCADE would erase the linking history). A merge archives the loser via `merged_into`.
- No CHECK may constrain `merged_into` (gotcha skill `postgres-check-vs-on-delete-set-null`).
- `Result.mappings()` is called on the **Result**, not on the session (mypy gotcha in scripts/); tests use `MagicMock(spec=AsyncSession)` to catch it.
- Nightly killswitches: env vars in dream.sh + systemd drop-in `killswitches.conf` — NEVER in the unit (incident 2026-06-30). No `Settings` field for these flags.
- `/metrics` contract is additive only: new keys OK, no rename/removal (consumed by red-monitor).
- httpx transport `str(exc)` is often empty → always `_exc_str` (learning 7144c5ae).
- Feature statuses (CHECK post-030): `planned, research, design, building, deployed, done, archived`.
- The nightly run applies whatever `WET_APPLYABLE_OPS` names, and since 2026-09-02 that is `archive`/`status` again — what this plan originally described. It was widened to all four ops on the evening of 2026-07-04 (aggressive regime) and NARROWED BACK on 2026-09-02 on measurement: 150 of the 181 proposals the wet ever applied were `merge` or `rename`, against 592 rejected by human review. Both decisions are dated in the constant's own comment, and the current one is pinned by `test_roadmap_curate.py::test_wet_applyable_ops_excludes_merge_and_rename`. `merge` and `rename` are still PROPOSED, and stay applicable BY REVIEW (`--apply-ids`, `brain_apply_curation_proposal` — both `allowed_ops=None`); only the unattended path is bounded. The listings further down show the original recipe and are historical. `tests/unit/test_roadmap_wet_scope_matches_its_doc.py` keeps this bullet and the rollout step honest, in BOTH directions.

---

### Task 1: Migration 030 + tables.py declarations

**Files:**
- Create: `alembic/versions/030_roadmap_curation.py`
- Modify: `src/brain_v42/db/tables.py` (features + new table + `__all__`)
- Test: `tests/unit/db/test_schema_roadmap_030.py`

**Interfaces:**
- Produces: `features.merged_into` column (UUID NULL, self-ref FK), `archived` status in the CHECK, `roadmap_curation_proposals` table importable via `from brain_v42.db.tables import roadmap_curation_proposals` (columns: `id` bigint PK, `op` varchar(10), `feature_id` UUID FK CASCADE, `payload` JSONB, `rationale` text, `status` varchar(10) default `'proposed'`, `created_at`, `applied_at`).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/db/test_schema_roadmap_030.py` (pattern `test_schema_indexes_027.py` — pure Python, no DB):

```python
"""Unit tests for migration 030 — roadmap curée (spec 2026-07-04).

Covers:
1. features.merged_into declared in tables.py (UUID nullable, FK self-ref).
2. roadmap_curation_proposals declared in METADATA with columns, checks, indexes.
3. Migration 030 wired (down_revision=029), adds 'archived' to the status CHECK,
   adds merged_into, creates the proposals table; downgrade is symmetric.
4. No CHECK constrains merged_into (postgres-check-vs-on-delete-set-null gotcha).

All tests are pure Python — no DB connection needed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent


class TestFeaturesMergedInto:
    def test_features_has_merged_into_column(self) -> None:
        from brain_v42.db.tables import features

        col = features.c["merged_into"]
        assert isinstance(col.type, UUID)
        assert col.nullable is True

    def test_merged_into_fk_targets_features_id(self) -> None:
        from brain_v42.db.tables import features

        fks = list(features.c["merged_into"].foreign_keys)
        assert len(fks) == 1
        assert fks[0].column.table.name == "features"
        assert fks[0].column.name == "id"
        # Never DELETE on features → no ON DELETE on this FK.
        assert fks[0].ondelete is None


class TestRoadmapCurationProposalsTable:
    def test_in_metadata_and_all(self) -> None:
        from brain_v42.db import tables as mod
        from brain_v42.db.tables import METADATA

        assert "roadmap_curation_proposals" in METADATA.tables
        assert "roadmap_curation_proposals" in mod.__all__

    def test_columns(self) -> None:
        from brain_v42.db.tables import roadmap_curation_proposals as t

        assert isinstance(t.c["id"].type, sa.BigInteger)
        assert t.c["id"].primary_key is True
        assert isinstance(t.c["op"].type, sa.String)
        assert t.c["op"].nullable is False
        assert isinstance(t.c["feature_id"].type, UUID)
        assert t.c["feature_id"].nullable is False
        assert isinstance(t.c["payload"].type, JSONB)
        assert t.c["payload"].nullable is False
        assert isinstance(t.c["rationale"].type, sa.Text)
        assert isinstance(t.c["status"].type, sa.String)
        assert t.c["status"].server_default is not None
        assert isinstance(t.c["created_at"].type, sa.DateTime)
        assert isinstance(t.c["applied_at"].type, sa.DateTime)
        assert t.c["applied_at"].nullable is True

    def test_feature_id_fk_cascade(self) -> None:
        from brain_v42.db.tables import roadmap_curation_proposals as t

        fks = list(t.c["feature_id"].foreign_keys)
        assert len(fks) == 1
        assert fks[0].column.table.name == "features"
        assert fks[0].ondelete == "CASCADE"

    def test_check_constraints(self) -> None:
        from brain_v42.db.tables import roadmap_curation_proposals as t

        names = {c.name for c in t.constraints if isinstance(c, sa.CheckConstraint)}
        assert "rcp_op_valid" in names
        assert "rcp_status_valid" in names

    def test_indexes(self) -> None:
        from brain_v42.db.tables import roadmap_curation_proposals as t

        index_names = {idx.name for idx in t.indexes}
        assert "idx_rcp_status" in index_names
        assert "idx_rcp_feature" in index_names


class TestMigration030Structure:
    @property
    def migration_path(self) -> Path:
        versions_dir = PROJECT_ROOT / "alembic" / "versions"
        candidates = list(versions_dir.glob("030_*.py"))
        assert len(candidates) == 1, f"Expected one 030_*.py, found {candidates}"
        return candidates[0]

    @property
    def content(self) -> str:
        return self.migration_path.read_text()

    def test_revision_chain(self) -> None:
        assert 'revision = "030"' in self.content
        assert 'down_revision = "029"' in self.content

    def test_has_upgrade_and_downgrade(self) -> None:
        tree = ast.parse(self.content)
        funcs = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        assert "upgrade" in funcs
        assert "downgrade" in funcs

    def test_upgrade_extends_status_check_with_archived(self) -> None:
        content = self.content
        assert "features_status_check" in content
        assert "'archived'" in content

    def test_upgrade_adds_merged_into_with_fk(self) -> None:
        content = self.content
        assert "merged_into" in content
        assert "REFERENCES features(id)" in content

    def test_no_check_clause_constrains_merged_into(self) -> None:
        """Gotcha postgres-check-vs-on-delete-set-null : merged_into jamais dans un CHECK.

        On inspecte le CONTENU des clauses CHECK (...) — pas le texte
        environnant, sinon les commentaires déclenchent des faux positifs.
        """
        import re

        for m in re.finditer(r"CHECK\s*\(([^)]*)\)", self.content, flags=re.IGNORECASE):
            assert "merged_into" not in m.group(1).lower(), m.group(0)

    def test_no_check_constraint_on_features_table_declaration(self) -> None:
        """tables.py : aucune CheckConstraint de features ne référence merged_into."""
        from brain_v42.db.tables import features

        for c in features.constraints:
            if isinstance(c, sa.CheckConstraint):
                assert "merged_into" not in str(c.sqltext)

    def test_upgrade_creates_proposals_table_with_indexes(self) -> None:
        content = self.content
        assert "CREATE TABLE roadmap_curation_proposals" in content
        assert "idx_rcp_status" in content
        assert "idx_rcp_feature" in content
        assert "rcp_op_valid" in content
        assert "rcp_status_valid" in content

    def test_downgrade_symmetric(self) -> None:
        content = self.content
        assert "DROP TABLE IF EXISTS roadmap_curation_proposals" in content
        assert "DROP COLUMN IF EXISTS merged_into" in content

    def test_alembic_chain_head_is_030(self) -> None:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config("alembic.ini")
        script = ScriptDirectory.from_config(cfg)
        rev = script.get_revision("030")
        assert rev is not None
        assert rev.down_revision == "029"
        assert script.get_current_head() == "030"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/db/test_schema_roadmap_030.py -v`
Expected: FAIL — `KeyError: 'merged_into'`, `ImportError: cannot import name 'roadmap_curation_proposals'`, "Expected one 030_*.py, found []".

- [ ] **Step 3: Write the migration**

Create `alembic/versions/030_roadmap_curation.py`:

```python
"""Roadmap curée — archived, merged_into, roadmap_curation_proposals.

Spec 2026-07-04 §1. Pattern 029 (proposer-only, review humaine → apply).
- features.status : + 'archived' dans le CHECK.
- features.merged_into : FK self-ref SANS ON DELETE (les features ne sont
  jamais DELETE — le FK feature_artifacts est ON DELETE CASCADE, supprimer
  effacerait l'historique de liage). Aucun CHECK ne contraint merged_into
  (gotcha postgres-check-vs-on-delete-set-null).

Revision ID: 030
Revises: 029
"""

from alembic import op

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. status CHECK: + 'archived' (constraint auto-named by 005).
    op.execute("ALTER TABLE features DROP CONSTRAINT features_status_check")
    op.execute(
        """
        ALTER TABLE features ADD CONSTRAINT features_status_check
        CHECK (status IN ('planned', 'research', 'design', 'building',
                          'deployed', 'done', 'archived'))
        """
    )

    # 2. merged_into — same pattern as decisions/learnings (007), with FK.
    op.execute("ALTER TABLE features ADD COLUMN merged_into UUID REFERENCES features(id)")

    # 3. Table proposals — miroir de ticket_extraction_proposals (029).
    op.execute(
        """
        CREATE TABLE roadmap_curation_proposals (
            id BIGSERIAL PRIMARY KEY,
            op VARCHAR(10) NOT NULL,
            feature_id UUID NOT NULL REFERENCES features(id) ON DELETE CASCADE,
            payload JSONB NOT NULL,
            rationale TEXT,
            status VARCHAR(10) NOT NULL DEFAULT 'proposed',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            applied_at TIMESTAMPTZ,

            CONSTRAINT rcp_op_valid CHECK (op IN ('merge', 'archive', 'status', 'rename')),
            CONSTRAINT rcp_status_valid CHECK (status IN ('proposed', 'applied', 'rejected'))
        )
        """
    )
    op.execute("CREATE INDEX idx_rcp_status ON roadmap_curation_proposals (status)")
    op.execute("CREATE INDEX idx_rcp_feature ON roadmap_curation_proposals (feature_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS roadmap_curation_proposals;")
    op.execute("ALTER TABLE features DROP COLUMN IF EXISTS merged_into;")
    # 'archived' rows would violate the restored CHECK → back to 'research'
    # (pre-curation ClusterGuard state). Info loss accepted on downgrade.
    op.execute("UPDATE features SET status = 'research' WHERE status = 'archived'")
    op.execute("ALTER TABLE features DROP CONSTRAINT features_status_check")
    op.execute(
        """
        ALTER TABLE features ADD CONSTRAINT features_status_check
        CHECK (status IN ('planned', 'research', 'design', 'building', 'deployed', 'done'))
        """
    )
```

- [ ] **Step 4: Update tables.py declarations**

In `src/brain_v42/db/tables.py`:

(a) In the `features = Table(...)` definition (≈line 432), add after the `pinned` column:

```python
    Column(
        "merged_into",
        UUID(as_uuid=True),
        sa.ForeignKey("features.id"),
        nullable=True,
    ),
```

(b) After the `ticket_extraction_proposals` block (≈line 930), add:

```python
# ─── roadmap_curation_proposals (curated roadmap — spec 2026-07-04 §1) ─────────
# Pattern 029: proposer-only audit table, human review → apply via
# scripts/roadmap_curate.py --apply-ids. No embedding, no decay.

roadmap_curation_proposals = Table(
    "roadmap_curation_proposals",
    METADATA,
    Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    Column("op", String(10), nullable=False),
    Column(
        "feature_id",
        UUID(as_uuid=True),
        sa.ForeignKey("features.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("payload", JSONB, nullable=False),
    Column("rationale", Text, nullable=True),
    Column("status", String(10), nullable=False, server_default=sa.text("'proposed'")),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column("applied_at", DateTime(timezone=True), nullable=True),
    sa.CheckConstraint(
        "op IN ('merge', 'archive', 'status', 'rename')",
        name="rcp_op_valid",
    ),
    sa.CheckConstraint(
        "status IN ('proposed', 'applied', 'rejected')",
        name="rcp_status_valid",
    ),
    Index("idx_rcp_status", "status"),
    Index("idx_rcp_feature", "feature_id"),
)
```

(c) Add `"roadmap_curation_proposals",` to `__all__` (after `"ticket_extraction_proposals",`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/db/test_schema_roadmap_030.py tests/unit/db/ -v`
Expected: PASS (including the existing 027 tests — the minimal table count is a `>=`).

- [ ] **Step 6: Gates + commit**

```bash
uv run pytest tests/unit -q && uv run ruff check src/ tests/ scripts/ && uv run ruff format --check src/ tests/ scripts/ && uv run mypy src/
git add alembic/versions/030_roadmap_curation.py src/brain_v42/db/tables.py tests/unit/db/test_schema_roadmap_030.py
git commit -m "feat(db): migration 030 — archived, merged_into, roadmap_curation_proposals"
```

---

### Task 2: Mechanical purge — `scripts/roadmap_purge.py`

**Files:**
- Create: `scripts/roadmap_purge.py`
- Test: `tests/unit/test_roadmap_purge.py`

**Interfaces:**
- Consumes: `PgProjectContextRepo(session_factory).get_keys_by_group("red") -> list[str]`; tables `features`/`feature_artifacts`.
- Produces: CLI `python -m scripts.roadmap_purge [--wet]`; pure functions `classify_feature(feature: dict, known_keys: set[str], now: datetime) -> str | None` (returns `"R1"|"R2"|"R3"|None`) and `build_report(classified: list[tuple[dict, str]]) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_roadmap_purge.py`:

```python
"""Unit tests for scripts.roadmap_purge — règles pures + apply mocké."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from scripts.roadmap_purge import (
    TERMINAL_STATUSES,
    apply_archive,
    build_report,
    classify_feature,
)
from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime(2026, 7, 4, tzinfo=UTC)
_KNOWN = {"brain-v42", "red-monitor", "red"}


def _feature(**kw) -> dict:
    defaults: dict = {
        "id": uuid4(),
        "project_key": "brain-v42",
        "name": "une feature",
        "status": "research",
        "pinned": False,
        "artifact_count": 3,
        "last_artifact_at": _NOW - timedelta(days=2),
    }
    defaults.update(kw)
    return defaults


class TestClassifyFeature:
    def test_pinned_never_touched(self):
        f = _feature(pinned=True, project_key="fantome", artifact_count=0)
        assert classify_feature(f, _KNOWN, _NOW) is None

    def test_r1_phantom_project_key(self):
        f = _feature(project_key="refondrre")
        assert classify_feature(f, _KNOWN, _NOW) == "R1"

    def test_r1_spares_key_in_red_group(self):
        # 'red' is in known_keys (via get_keys_by_group) → spared.
        f = _feature(project_key="red", artifact_count=5)
        assert classify_feature(f, _KNOWN, _NOW) is None

    def test_r2_zero_artifacts(self):
        f = _feature(artifact_count=0, last_artifact_at=None)
        assert classify_feature(f, _KNOWN, _NOW) == "R2"

    def test_r3_single_stale_artifact_non_terminal(self):
        f = _feature(artifact_count=1, last_artifact_at=_NOW - timedelta(days=61))
        assert classify_feature(f, _KNOWN, _NOW) == "R3"

    def test_r3_spares_terminal_status(self):
        for status in TERMINAL_STATUSES:
            f = _feature(
                status=status, artifact_count=1, last_artifact_at=_NOW - timedelta(days=61)
            )
            assert classify_feature(f, _KNOWN, _NOW) is None, status

    def test_r3_spares_recent_artifact(self):
        f = _feature(artifact_count=1, last_artifact_at=_NOW - timedelta(days=10))
        assert classify_feature(f, _KNOWN, _NOW) is None

    def test_r3_spares_multi_artifact(self):
        f = _feature(artifact_count=2, last_artifact_at=_NOW - timedelta(days=100))
        assert classify_feature(f, _KNOWN, _NOW) is None

    def test_alive_feature_untouched(self):
        assert classify_feature(_feature(), _KNOWN, _NOW) is None


class TestBuildReport:
    def test_report_groups_by_project_and_rule(self):
        rows = [
            (_feature(project_key="refondrre"), "R1"),
            (_feature(project_key="refondrre"), "R1"),
            (_feature(project_key="brain-v42", artifact_count=0), "R2"),
        ]
        report = build_report(rows)
        assert "refondrre" in report
        assert "R1: 2" in report
        assert "brain-v42" in report
        assert "R2: 1" in report
        assert "total à archiver: 3" in report


class TestApplyArchive:
    @pytest.mark.asyncio
    async def test_archives_ids_and_checks_postcondition(self):
        ids = [uuid4(), uuid4()]
        mock_session = MagicMock(spec=AsyncSession)
        update_result = MagicMock()
        count_result = MagicMock()
        count_result.scalar_one.return_value = len(ids)
        mock_session.execute = AsyncMock(side_effect=[update_result, count_result])
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=begin_cm)

        @asynccontextmanager
        async def factory():
            yield mock_session

        archived = await apply_archive(factory, ids)
        assert archived == len(ids)

    @pytest.mark.asyncio
    async def test_postcondition_mismatch_raises(self):
        ids = [uuid4()]
        mock_session = MagicMock(spec=AsyncSession)
        update_result = MagicMock()
        count_result = MagicMock()
        count_result.scalar_one.return_value = 0  # rien d'archivé → post-cond KO
        mock_session.execute = AsyncMock(side_effect=[update_result, count_result])
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=begin_cm)

        @asynccontextmanager
        async def factory():
            yield mock_session

        with pytest.raises(RuntimeError, match="post-condition"):
            await apply_archive(factory, ids)

    @pytest.mark.asyncio
    async def test_empty_ids_noop(self):
        factory = MagicMock()
        assert await apply_archive(factory, []) == 0
        factory.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_roadmap_purge.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.roadmap_purge'`.

- [ ] **Step 3: Write the implementation**

Create `scripts/roadmap_purge.py`:

```python
"""Purge mécanique du stock roadmap — one-shot, SQL pur, sans LLM (spec §2).

Règles (`pinned=true` : JAMAIS touchée) :
  R1. project_key absent de project_contexts ET hors groupe `red` → archived.
      (get_keys_by_group réutilisé — parité vue codex. Si `red` est une vraie
      clé legacy du groupe, la règle l'épargne et on tranche à la review.)
  R2. 0 artifact → archived.
  R3. 1 artifact, aucun artifact créé depuis 60 j (max(feature_artifacts.
      created_at), PAS status_updated_at), statut non terminal → archived.

Réversibilité : tout est `archived`, un UPDATE inverse suffit.

Usage:
    python -m scripts.roadmap_purge          # dry (défaut) — rapport seul
    python -m scripts.roadmap_purge --wet    # applique les UPDATE
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa

TERMINAL_STATUSES = ("deployed", "done", "archived")
STALE_DAYS = 60

_CANDIDATES_SQL = """
SELECT f.id,
       f.project_key,
       f.name,
       f.status,
       COALESCE(f.pinned, false) AS pinned,
       COUNT(fa.artifact_id) AS artifact_count,
       MAX(fa.created_at) AS last_artifact_at
FROM features f
LEFT JOIN feature_artifacts fa ON fa.feature_id = f.id
WHERE f.status != 'archived'
GROUP BY f.id, f.project_key, f.name, f.status, f.pinned
"""


def classify_feature(
    feature: dict[str, Any],
    known_keys: set[str],
    now: datetime,
) -> str | None:
    """Retourne la règle qui archive cette feature, ou None (vivante).

    Pure — testable sans DB. L'ordre R1 > R2 > R3 est contractuel :
    une feature fantôme à 0 artifact compte en R1.
    """
    if feature["pinned"]:
        return None
    if feature["project_key"] not in known_keys:
        return "R1"
    if feature["artifact_count"] == 0:
        return "R2"
    if (
        feature["artifact_count"] == 1
        and feature["status"] not in TERMINAL_STATUSES
        and feature["last_artifact_at"] is not None
        and now - feature["last_artifact_at"] >= timedelta(days=STALE_DAYS)
    ):
        return "R3"
    return None


def build_report(classified: list[tuple[dict[str, Any], str]]) -> str:
    """Rapport par projet : compte par règle + total."""
    per_project: dict[str, Counter[str]] = defaultdict(Counter)
    for feature, rule in classified:
        per_project[feature["project_key"]][rule] += 1
    lines = ["=== roadmap_purge — rapport par projet ==="]
    for pk in sorted(per_project):
        counts = per_project[pk]
        detail = ", ".join(f"{rule}: {counts[rule]}" for rule in sorted(counts))
        lines.append(f"- {pk}: {detail} (sous-total {sum(counts.values())})")
    lines.append(f"total à archiver: {len(classified)}")
    return "\n".join(lines)


async def fetch_known_keys(session_factory: Any) -> set[str]:
    """project_contexts keys ∪ get_keys_by_group('red') — parité vue codex."""
    from brain_v42.repositories.pg_project_context import PgProjectContextRepo  # noqa: PLC0415

    async with session_factory() as session:
        rows = (await session.execute(sa.text("SELECT project_key FROM project_contexts"))).all()
    keys = {r[0] for r in rows}
    repo = PgProjectContextRepo(session_factory)
    keys.update(await repo.get_keys_by_group("red"))
    return keys


async def fetch_candidates(session_factory: Any) -> list[dict[str, Any]]:
    async with session_factory() as session:
        rows = (await session.execute(sa.text(_CANDIDATES_SQL))).mappings().all()
    return [dict(r) for r in rows]


async def apply_archive(session_factory: Any, feature_ids: list[UUID]) -> int:
    """UPDATE → archived, transaction unique, post-condition positive (F-09)."""
    if not feature_ids:
        return 0
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                sa.text(
                    "UPDATE features SET status = 'archived', status_updated_at = NOW() "
                    "WHERE id = ANY(:ids)"
                ),
                {"ids": feature_ids},
            )
            archived = (
                await session.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM features "
                        "WHERE id = ANY(:ids) AND status = 'archived'"
                    ),
                    {"ids": feature_ids},
                )
            ).scalar_one()
            if archived != len(feature_ids):
                raise RuntimeError(
                    f"post-condition failed: {archived}/{len(feature_ids)} archived"
                )
    return int(archived)


async def _run(wet: bool) -> int:
    from brain_v42.db.engine import get_session_factory  # noqa: PLC0415

    sf = get_session_factory()
    now = datetime.now(tz=UTC)

    known_keys = await fetch_known_keys(sf)
    candidates = await fetch_candidates(sf)

    classified = [
        (f, rule) for f in candidates if (rule := classify_feature(f, known_keys, now))
    ]
    print(build_report(classified))

    alive = len(candidates) - len(classified)
    print(f"features vivantes restantes (hors pinned archivables): {alive}")

    if not wet:
        print("(dry — relancer avec --wet pour appliquer)")
        return 0

    archived = await apply_archive(sf, [f["id"] for f, _ in classified])
    print(f"wet: {archived} features archivées")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="roadmap_purge",
        description="Purge mécanique du stock roadmap (spec 2026-07-04 §2).",
    )
    parser.add_argument("--wet", action="store_true", help="applique les UPDATE (défaut: dry)")
    args = parser.parse_args()
    return asyncio.run(_run(wet=args.wet))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_roadmap_purge.py -v`
Expected: PASS (14 tests).

- [ ] **Step 5: Gates + commit**

```bash
uv run pytest tests/unit -q && uv run ruff check src/ tests/ scripts/ && uv run ruff format --check src/ tests/ scripts/ && uv run mypy src/
git add scripts/roadmap_purge.py tests/unit/test_roadmap_purge.py
git commit -m "feat(roadmap): purge mécanique du stock — scripts/roadmap_purge.py"
```

---

### Task 3: LLM curator — propose (`scripts/roadmap_curate.py`, part 1)

**Files:**
- Create: `scripts/roadmap_curate.py`
- Test: `tests/unit/test_roadmap_curate.py`

**Interfaces:**
- Consumes: `from scripts.domain_backfill import DEFAULT_BASE_URL, DEFAULT_MODEL, ResponseParseError, _exc_str, _post_chat, _strip_fences, load_env_file` (signatures identical to the usage in `ticket_extract.py`).
- Produces (used by Task 4): dataclasses `FeatureCard(id, name, status, pinned, artifacts: list[str])`, `ProjectBatch(project_key, features: list[FeatureCard])`, `CurationDraft(op: str, feature_id: UUID, payload: dict, rationale: str)`, `BatchOutcome(batch, drafts, failed, error)`; functions `render_batch`, `build_messages`, `format_digest`, `parse_and_validate(content, batch) -> list[CurationDraft]`, `fetch_project_batches(session_factory, limit) -> list[ProjectBatch]`, `curate_batch(client, model, batch, sleep) -> BatchOutcome`; constants `VALID_OPS`, `PROPOSABLE_STATUSES`, `WET_APPLYABLE_OPS`, `MAX_FEATURES_PER_PROJECT=30`, `MAX_ARTIFACTS_PER_FEATURE=10`, `MAX_PROPOSALS_PER_NIGHT=40`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_roadmap_curate.py`:

```python
"""Unit tests for scripts.roadmap_curate pure functions (no DB, no network)."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from scripts.roadmap_curate import (
    PROPOSABLE_STATUSES,
    VALID_OPS,
    WET_APPLYABLE_OPS,
    FeatureCard,
    ProjectBatch,
    ResponseParseError,
    build_messages,
    curate_batch,
    format_digest,
    parse_and_validate,
    render_batch,
)

_F1 = uuid4()
_F2 = uuid4()
_PINNED = uuid4()


def _batch() -> ProjectBatch:
    return ProjectBatch(
        project_key="brain-v42",
        features=[
            FeatureCard(
                id=_F1,
                name="Recherche hybride",
                status="research",
                pinned=False,
                artifacts=["2026-07-01 [decision] RRF retenu"],
            ),
            FeatureCard(
                id=_F2,
                name="recherche hybride v2",
                status="research",
                pinned=False,
                artifacts=[],
            ),
            FeatureCard(
                id=_PINNED,
                name="Feature épinglée",
                status="building",
                pinned=True,
                artifacts=["2026-06-30 [plan] Plan X (plan done)"],
            ),
        ],
    )


def _item(op: str, fid, payload: dict) -> str:
    import json

    return json.dumps(
        [{"op": op, "feature_id": str(fid), "payload": payload, "rationale": "r"}]
    )


class TestRenderAndBuild:
    def test_render_batch_contains_ids_names_statuses(self):
        text = render_batch(_batch())
        assert str(_F1) in text and str(_F2) in text
        assert "Recherche hybride" in text
        assert "research" in text
        assert "PINNED" in text  # marqueur sur la feature épinglée
        assert "RRF retenu" in text

    def test_build_messages_has_system_and_user(self):
        msgs = build_messages(_batch())
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "JSON" in msgs[0]["content"]

    def test_format_digest(self):
        from datetime import UTC, datetime

        d = format_digest("decision", "RRF retenu", datetime(2026, 7, 1, tzinfo=UTC), None)
        assert d == "2026-07-01 [decision] RRF retenu"

    def test_format_digest_plan_status(self):
        from datetime import UTC, datetime

        d = format_digest("plan", "Plan X", datetime(2026, 6, 30, tzinfo=UTC), "done")
        assert d == "2026-06-30 [plan] Plan X (plan done)"


class TestParseAndValidate:
    def test_all_four_ops_valid(self):
        assert set(VALID_OPS) == {"merge", "archive", "status", "rename"}
        assert parse_and_validate(_item("archive", _F1, {}), _batch())[0].op == "archive"
        assert (
            parse_and_validate(_item("merge", _F2, {"into": str(_F1)}), _batch())[0].payload[
                "into"
            ]
            == str(_F1)
        )
        assert (
            parse_and_validate(_item("status", _F1, {"status": "building"}), _batch())[0]
            .payload["status"]
            == "building"
        )
        assert (
            parse_and_validate(_item("rename", _F2, {"name": "Recherche hybride (fusion)"}),
                               _batch())[0].payload["name"]
            == "Recherche hybride (fusion)"
        )

    def test_empty_array_valid(self):
        assert parse_and_validate("[]", _batch()) == []

    def test_fences_stripped(self):
        assert parse_and_validate("```json\n[]\n```", _batch()) == []

    def test_invalid_json_raises(self):
        with pytest.raises(ResponseParseError):
            parse_and_validate("pas du json", _batch())

    def test_unknown_op_rejected(self):
        with pytest.raises(ResponseParseError, match="op"):
            parse_and_validate(_item("delete", _F1, {}), _batch())

    def test_feature_outside_batch_rejected(self):
        with pytest.raises(ResponseParseError, match="not in batch"):
            parse_and_validate(_item("archive", uuid4(), {}), _batch())

    def test_merge_target_outside_batch_rejected(self):
        with pytest.raises(ResponseParseError, match="not in batch"):
            parse_and_validate(_item("merge", _F1, {"into": str(uuid4())}), _batch())

    def test_merge_into_self_rejected(self):
        with pytest.raises(ResponseParseError, match="equals"):
            parse_and_validate(_item("merge", _F1, {"into": str(_F1)}), _batch())

    def test_pinned_only_status_allowed(self):
        with pytest.raises(ResponseParseError, match="pinned"):
            parse_and_validate(_item("archive", _PINNED, {}), _batch())
        # status sur pinned : OK
        drafts = parse_and_validate(_item("status", _PINNED, {"status": "deployed"}), _batch())
        assert drafts[0].feature_id == _PINNED

    def test_status_archived_rejected_use_archive_op(self):
        assert "archived" not in PROPOSABLE_STATUSES
        with pytest.raises(ResponseParseError, match="status"):
            parse_and_validate(_item("status", _F1, {"status": "archived"}), _batch())

    def test_rename_empty_rejected_and_truncated_200(self):
        with pytest.raises(ResponseParseError, match="name"):
            parse_and_validate(_item("rename", _F1, {"name": "  "}), _batch())
        drafts = parse_and_validate(_item("rename", _F1, {"name": "x" * 300}), _batch())
        assert len(drafts[0].payload["name"]) == 200

    def test_wet_applyable_ops_excludes_merge_rename(self):
        assert set(WET_APPLYABLE_OPS) == {"archive", "status"}


class TestCurateBatchErrorCapture:
    @pytest.mark.asyncio
    async def test_transport_error_names_exception_type(self) -> None:
        """str() vide des erreurs transport → outcome.error nommé (learning 7144c5ae)."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("", request=request)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(client, "test-model", _batch())

        assert outcome.failed
        assert outcome.error
        assert "ConnectError" in outcome.error
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_roadmap_curate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.roadmap_curate'`.

- [ ] **Step 3: Write the implementation (propose part)**

Create `scripts/roadmap_curate.py`:

```python
"""Roadmap curation — proposer-only dream step (spec 2026-07-04 §3).

Batch par projet : features vivantes (statut ∉ done/archived, non mergées)
+ digest des artifacts récents (titre, type, date — PAS les corps), envoyé
au LLM (NVIDIA API, JSON strict SANS tools — squelette exact de
ticket_extract). Quatre ops proposer-only : merge, archive, status, rename.

Garde-fous durs :
- pinned : seule l'op `status` est proposable ;
- done/archived : hors batch par construction (intouchables) ;
- merge intra-projet uniquement, `into` doit être dans le batch ;
- cap MAX_PROPOSALS_PER_NIGHT proposals/nuit (drop loggé, jamais silencieux).

La nightly n'applique JAMAIS merge/rename : --wet est restreint à
WET_APPLYABLE_OPS ; --apply-ids (review humaine) applique tout.

Usage:
    python -m scripts.roadmap_curate [--limit 10]        # propose (dry)
    python -m scripts.roadmap_curate --limit 10 --wet    # propose + apply archive/status
    python -m scripts.roadmap_curate --apply-ids "3,4"   # apply reviewé, sans LLM
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import sqlalchemy as sa

from scripts.domain_backfill import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ResponseParseError,
    _exc_str,
    _post_chat,
    _strip_fences,
    load_env_file,
)

_ENV_FILE = Path.home() / ".config" / "brain-v42" / "nvidia.env"
_API_KEY_VAR = "BRAIN_NVIDIA_API_KEY"

VALID_OPS = ("merge", "archive", "status", "rename")
# 'archived' excluded: the `archive` op exists for that.
PROPOSABLE_STATUSES = ("planned", "research", "design", "building", "deployed", "done")
# The nightly wet mode (rollout §4) applies only archive/status — merge and
# rename remain under review indefinitely.
WET_APPLYABLE_OPS = ("archive", "status")
MAX_FEATURES_PER_PROJECT = 30
MAX_ARTIFACTS_PER_FEATURE = 10
MAX_PROPOSALS_PER_NIGHT = 40

_SYSTEM_PROMPT = (
    "Tu es le cureur nocturne d'une roadmap de features auto-générées par "
    "clustering d'artifacts (decisions, learnings, snippets, runbooks, "
    "plans). Tu réponds UNIQUEMENT avec un tableau JSON valide "
    "(éventuellement vide []) — pas de prose, pas de markdown. Chaque "
    'élément: {"op": "merge"|"archive"|"status"|"rename", "feature_id": '
    '"<uuid du batch>", "payload": {...}, "rationale": "pourquoi"}. '
    'payload merge: {"into": "<uuid du batch>"} — fusionne feature_id '
    "(doublon) dans into (survivante). "
    "payload archive: {} — feature morte ou bruit sans valeur roadmap. "
    'payload status: {"status": "planned"|"research"|"design"|"building"|'
    '"deployed"|"done"} — aligne le statut sur la réalité des artifacts '
    "(un artifact « livré X »/« déployer X » → deployed ; un plan done → "
    "done). "
    'payload rename: {"name": "<titre clair, ≤200 chars>"} — retitre les '
    "noms de cluster illisibles. "
    "Règles dures : n'utilise QUE des feature_id présents dans le batch ; "
    "une feature marquée PINNED n'accepte QUE l'op status ; merge "
    "uniquement entre features du batch (même projet). Sois conservateur : "
    "ne propose que ce qui est évident depuis les artifacts."
)
_REPROMPT_INSTRUCTION = (
    "Ta réponse précédente n'était pas un tableau JSON valide selon le format "
    "demandé. Renvoie UNIQUEMENT le tableau JSON corrigé."
)


@dataclass
class FeatureCard:
    id: UUID
    name: str
    status: str
    pinned: bool
    artifacts: list[str] = field(default_factory=list)


@dataclass
class ProjectBatch:
    project_key: str
    features: list[FeatureCard]


@dataclass
class CurationDraft:
    op: str
    feature_id: UUID
    payload: dict[str, Any]
    rationale: str


@dataclass
class BatchOutcome:
    batch: ProjectBatch
    drafts: list[CurationDraft]
    failed: bool = False
    error: str | None = None


def format_digest(
    artifact_type: str,
    title: str,
    created_at: datetime,
    plan_status: str | None,
) -> str:
    """Digest une ligne — titre/type/date, jamais les corps complets."""
    base = f"{created_at.date().isoformat()} [{artifact_type}] {title}"
    if artifact_type == "plan" and plan_status:
        base += f" (plan {plan_status})"
    return base


def render_batch(batch: ProjectBatch) -> str:
    lines = [f"Projet: {batch.project_key} — {len(batch.features)} features vivantes"]
    for f in batch.features:
        pin = " [PINNED — seule l'op status est permise]" if f.pinned else ""
        lines.append(f"\n- feature_id: {f.id}\n  nom: {f.name}\n  statut: {f.status}{pin}")
        if f.artifacts:
            lines.append("  artifacts récents:")
            lines.extend(f"    - {a}" for a in f.artifacts)
        else:
            lines.append("  artifacts récents: (aucun)")
    return "\n".join(lines)


def build_messages(batch: ProjectBatch) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": render_batch(batch)},
    ]


def _parse_item_uuid(value: Any, i: int, fieldname: str) -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ResponseParseError(
            f"item {i}: {fieldname} is not a valid UUID: {value!r}"
        ) from exc


def parse_and_validate(content: str, batch: ProjectBatch) -> list[CurationDraft]:
    try:
        data = json.loads(_strip_fences(content))
    except json.JSONDecodeError as exc:
        raise ResponseParseError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ResponseParseError(f"expected a JSON array, got {type(data).__name__}")
    by_id = {f.id: f for f in batch.features}
    drafts: list[CurationDraft] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ResponseParseError(f"item {i}: expected object")
        op = item.get("op")
        if op not in VALID_OPS:
            raise ResponseParseError(f"item {i}: invalid op {op!r} (valid: {VALID_OPS})")
        fid = _parse_item_uuid(item.get("feature_id"), i, "feature_id")
        feature = by_id.get(fid)
        if feature is None:
            raise ResponseParseError(f"item {i}: feature_id {fid} not in batch")
        if feature.pinned and op != "status":
            raise ResponseParseError(
                f"item {i}: feature {fid} is pinned — only op 'status' allowed"
            )
        payload = item.get("payload")
        if payload is None and op == "archive":
            payload = {}
        if not isinstance(payload, dict):
            raise ResponseParseError(f"item {i}: payload must be an object")
        if op == "merge":
            into = _parse_item_uuid(payload.get("into"), i, "payload.into")
            if into not in by_id:
                raise ResponseParseError(f"item {i}: merge target {into} not in batch")
            if into == fid:
                raise ResponseParseError(f"item {i}: merge target equals feature_id")
            payload = {"into": str(into)}
        elif op == "status":
            new_status = payload.get("status")
            if new_status not in PROPOSABLE_STATUSES:
                raise ResponseParseError(
                    f"item {i}: invalid status {new_status!r} "
                    f"(valid: {PROPOSABLE_STATUSES}; use op 'archive' to archive)"
                )
            payload = {"status": new_status}
        elif op == "rename":
            name = payload.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ResponseParseError(
                    f"item {i}: rename payload must contain a non-empty 'name'"
                )
            payload = {"name": name.strip()[:200]}
        else:  # archive
            payload = {}
        drafts.append(
            CurationDraft(
                op=op,
                feature_id=fid,
                payload=payload,
                rationale=str(item.get("rationale", "")),
            )
        )
    return drafts


# ── I/O — DB + LLM ───────────────────────────────────────────────────────────

_KEYS_SQL = """
SELECT DISTINCT project_key FROM features
WHERE status NOT IN ('done', 'archived') AND merged_into IS NULL
ORDER BY project_key
LIMIT :lim
"""

_FEATURES_SQL = """
SELECT f.id, f.name, f.status, COALESCE(f.pinned, false) AS pinned
FROM features f
LEFT JOIN feature_artifacts fa ON fa.feature_id = f.id
WHERE f.project_key = :pk
  AND f.status NOT IN ('done', 'archived')
  AND f.merged_into IS NULL
GROUP BY f.id, f.name, f.status, f.pinned
ORDER BY MAX(fa.created_at) DESC NULLS LAST
LIMIT :cap
"""

_ARTIFACTS_SQL = """
SELECT fa.feature_id,
       fa.artifact_type,
       fa.created_at,
       COALESCE(d.title, l.topic, s.title, r.title, a.title, p.title, g.title, '?') AS title,
       p.status AS plan_status
FROM feature_artifacts fa
LEFT JOIN decisions d ON fa.artifact_type = 'decision' AND d.id = fa.artifact_id
LEFT JOIN learnings l ON fa.artifact_type = 'learning' AND l.id = fa.artifact_id
LEFT JOIN snippets s ON fa.artifact_type = 'snippet' AND s.id = fa.artifact_id
LEFT JOIN runbooks r ON fa.artifact_type = 'runbook' AND r.id = fa.artifact_id
LEFT JOIN adrs a ON fa.artifact_type = 'adr' AND a.id = fa.artifact_id
LEFT JOIN indexed_plans p ON fa.artifact_type = 'plan' AND p.id = fa.artifact_id
LEFT JOIN gitlab_events g ON fa.artifact_type = 'gitlab_event' AND g.id = fa.artifact_id
WHERE fa.feature_id = ANY(CAST(:fids AS uuid[]))
ORDER BY fa.feature_id, fa.created_at DESC
"""


async def fetch_project_batches(session_factory: Any, limit: int) -> list[ProjectBatch]:
    """Batchs par projet : features vivantes (cap 30) + digests (cap 10/feature)."""
    async with session_factory() as session:
        keys = [
            r[0]
            for r in (await session.execute(sa.text(_KEYS_SQL), {"lim": limit})).all()
        ]
        batches: list[ProjectBatch] = []
        for pk in keys:
            feat_rows = (
                (
                    await session.execute(
                        sa.text(_FEATURES_SQL),
                        {"pk": pk, "cap": MAX_FEATURES_PER_PROJECT},
                    )
                )
                .mappings()
                .all()
            )
            if not feat_rows:
                continue
            cards = {
                r["id"]: FeatureCard(
                    id=r["id"],
                    name=r["name"],
                    status=r["status"],
                    pinned=bool(r["pinned"]),
                )
                for r in feat_rows
            }
            art_rows = (
                (
                    await session.execute(
                        sa.text(_ARTIFACTS_SQL), {"fids": list(cards.keys())}
                    )
                )
                .mappings()
                .all()
            )
            for row in art_rows:
                card = cards[row["feature_id"]]
                if len(card.artifacts) >= MAX_ARTIFACTS_PER_FEATURE:
                    continue
                card.artifacts.append(
                    format_digest(
                        row["artifact_type"],
                        row["title"],
                        row["created_at"],
                        row["plan_status"],
                    )
                )
            batches.append(ProjectBatch(project_key=pk, features=list(cards.values())))
        return batches


async def curate_batch(
    client: httpx.AsyncClient,
    model: str,
    batch: ProjectBatch,
    sleep: Any = asyncio.sleep,
) -> BatchOutcome:
    """Un appel LLM par projet ; un re-prompt correctif sur parse error."""
    messages = build_messages(batch)
    try:
        content, _usage = await _post_chat(client, model, messages, sleep)
        try:
            drafts = parse_and_validate(content, batch)
        except ResponseParseError:
            corrective = [
                *messages,
                {"role": "assistant", "content": content},
                {"role": "user", "content": _REPROMPT_INSTRUCTION},
            ]
            content2, _usage2 = await _post_chat(client, model, corrective, sleep)
            try:
                drafts = parse_and_validate(content2, batch)
            except ResponseParseError as exc:
                return BatchOutcome(
                    batch=batch,
                    drafts=[],
                    failed=True,
                    error=f"unparseable after corrective re-prompt: {exc}",
                )
    except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as exc:
        return BatchOutcome(batch=batch, drafts=[], failed=True, error=_exc_str(exc))
    return BatchOutcome(batch=batch, drafts=drafts)
```

(The CLI `main`/`_run`, `persist_proposals`, `apply_proposals` and `record_dream_run` arrive in Task 4 — the module is importable from now on.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_roadmap_curate.py -v`
Expected: PASS (18 tests).

- [ ] **Step 5: Gates + commit**

```bash
uv run pytest tests/unit -q && uv run ruff check src/ tests/ scripts/ && uv run ruff format --check src/ tests/ scripts/ && uv run mypy src/
git add scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
git commit -m "feat(roadmap): cureur LLM — propose, parse_and_validate, garde-fous"
```

---

### Task 4: Curator — apply, post-conditions, CLI (`roadmap_curate.py`, part 2)

**Files:**
- Modify: `scripts/roadmap_curate.py` (append)
- Test: `tests/unit/test_roadmap_curate_apply.py`

**Interfaces:**
- Consumes: Task 3 (`CurationDraft`, `WET_APPLYABLE_OPS`, …); table `roadmap_curation_proposals` (Task 1).
- Produces: `persist_proposals(session_factory, drafts) -> list[int]`; `apply_proposals(session_factory, proposal_ids, allowed_ops=None) -> int`; `record_dream_run(session_factory, status, dry, duration_s, error) -> None` (phase=`roadmap`); `PostConditionError(RuntimeError)`; CLI `main() -> int` with `--limit/--wet/--apply-ids/--model/--base-url`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_roadmap_curate_apply.py`:

```python
"""Unit tests for scripts.roadmap_curate apply path (mocked session, no DB)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from scripts.roadmap_curate import apply_proposals, persist_proposals
from sqlalchemy.ext.asyncio import AsyncSession


def _proposal_row(
    proposal_id: int = 1,
    op: str = "archive",
    payload: dict | None = None,
    feature_id=None,
) -> dict:
    return {
        "id": proposal_id,
        "op": op,
        "feature_id": feature_id or uuid4(),
        "payload": payload if payload is not None else {},
        "rationale": "r",
        "status": "proposed",
    }


def _session_with(side_effects: list[Any]) -> tuple[Any, MagicMock]:
    """Fake session factory — spec=AsyncSession pour attraper session.mappings()."""
    mock_session = MagicMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(side_effect=side_effects)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)

    @asynccontextmanager
    async def factory():
        yield mock_session

    return factory, mock_session


def _mappings_all(rows: list[dict]) -> MagicMock:
    r = MagicMock()
    r.mappings.return_value.all.return_value = rows
    return r


def _mappings_one(row: dict) -> MagicMock:
    r = MagicMock()
    r.mappings.return_value.one.return_value = row
    return r


def _scalar_one(value: Any) -> MagicMock:
    r = MagicMock()
    r.scalar_one.return_value = value
    return r


class TestApplyArchive:
    @pytest.mark.asyncio
    async def test_archive_applies_and_checks_postcondition(self):
        row = _proposal_row(op="archive")
        factory, _ = _session_with(
            [
                _mappings_all([row]),          # SELECT proposals
                MagicMock(),                    # UPDATE features → archived
                _mappings_one({"status": "archived"}),  # post-condition re-read
                MagicMock(),                    # UPDATE proposal → applied
            ]
        )
        applied = await apply_proposals(factory, [1])
        assert applied == 1

    @pytest.mark.asyncio
    async def test_postcondition_failure_skips_proposal(self):
        row = _proposal_row(op="archive")
        factory, _ = _session_with(
            [
                _mappings_all([row]),
                MagicMock(),
                _mappings_one({"status": "research"}),  # état inattendu → rollback
            ]
        )
        applied = await apply_proposals(factory, [1])
        assert applied == 0


class TestApplyStatusRename:
    @pytest.mark.asyncio
    async def test_status_postcondition(self):
        row = _proposal_row(op="status", payload={"status": "deployed"})
        factory, _ = _session_with(
            [
                _mappings_all([row]),
                MagicMock(),
                _mappings_one({"status": "deployed"}),
                MagicMock(),
            ]
        )
        assert await apply_proposals(factory, [1]) == 1

    @pytest.mark.asyncio
    async def test_rename_postcondition(self):
        row = _proposal_row(op="rename", payload={"name": "Nouveau nom"})
        factory, _ = _session_with(
            [
                _mappings_all([row]),
                MagicMock(),
                _mappings_one({"name": "Nouveau nom"}),
                MagicMock(),
            ]
        )
        assert await apply_proposals(factory, [1]) == 1


class TestApplyMerge:
    @pytest.mark.asyncio
    async def test_merge_execute_sequence_and_postconditions(self):
        into = uuid4()
        row = _proposal_row(op="merge", payload={"into": str(into)})
        factory, session = _session_with(
            [
                _mappings_all([row]),   # SELECT proposals
                MagicMock(),            # UPDATE fa repoint
                MagicMock(),            # DELETE fa restants
                MagicMock(),            # UPDATE features loser
                _mappings_one({"merged_into": into, "status": "archived"}),
                _scalar_one(0),         # 0 artifacts restants sur le perdant
                MagicMock(),            # UPDATE proposal → applied
            ]
        )
        assert await apply_proposals(factory, [1]) == 1
        assert session.execute.call_count == 7

    @pytest.mark.asyncio
    async def test_merge_leftover_artifacts_fails_postcondition(self):
        into = uuid4()
        row = _proposal_row(op="merge", payload={"into": str(into)})
        factory, _ = _session_with(
            [
                _mappings_all([row]),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                _mappings_one({"merged_into": into, "status": "archived"}),
                _scalar_one(2),  # artifacts orphelins → post-condition KO
            ]
        )
        assert await apply_proposals(factory, [1]) == 0


class TestAllowedOps:
    @pytest.mark.asyncio
    async def test_wet_mode_skips_merge_and_rename(self):
        rows = [
            _proposal_row(proposal_id=1, op="merge", payload={"into": str(uuid4())}),
            _proposal_row(proposal_id=2, op="rename", payload={"name": "n"}),
            _proposal_row(proposal_id=3, op="archive"),
        ]
        factory, _ = _session_with(
            [
                _mappings_all(rows),
                # only archive (id 3) is applied:
                MagicMock(),
                _mappings_one({"status": "archived"}),
                MagicMock(),
            ]
        )
        applied = await apply_proposals(factory, [1, 2, 3], allowed_ops=("archive", "status"))
        assert applied == 1


class TestPersistProposals:
    @pytest.mark.asyncio
    async def test_empty_drafts_noop(self):
        factory = MagicMock()
        assert await persist_proposals(factory, []) == []
        factory.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_roadmap_curate_apply.py -v`
Expected: FAIL — `ImportError: cannot import name 'apply_proposals'`.

- [ ] **Step 3: Write the implementation**

Add to the end of `scripts/roadmap_curate.py`:

```python
class PostConditionError(RuntimeError):
    """L'état relu après l'op ne correspond pas à l'état attendu (pattern F-09)."""


async def persist_proposals(session_factory: Any, drafts: list[CurationDraft]) -> list[int]:
    """INSERT proposals status='proposed'. Returns inserted ids."""
    from brain_v42.db.tables import roadmap_curation_proposals  # noqa: PLC0415

    if not drafts:
        return []
    async with session_factory() as session:
        async with session.begin():
            ids: list[int] = []
            for draft in drafts:
                stmt = (
                    roadmap_curation_proposals.insert()
                    .values(
                        op=draft.op,
                        feature_id=draft.feature_id,
                        payload=draft.payload,
                        rationale=draft.rationale,
                        status="proposed",
                    )
                    .returning(roadmap_curation_proposals.c.id)
                )
                ids.append((await session.execute(stmt)).scalar_one())
            return ids


async def _apply_one(session: Any, row: Any) -> None:
    """Applique une proposal ; post-conditions positives DANS la transaction."""
    op, fid, payload = row["op"], row["feature_id"], row["payload"]
    if op == "merge":
        into = UUID(str(payload["into"]))
        # 1. Re-point the loser's artifacts (except duplicates already linked).
        await session.execute(
            sa.text(
                """
                UPDATE feature_artifacts fa SET feature_id = :into
                WHERE fa.feature_id = :loser
                  AND NOT EXISTS (
                      SELECT 1 FROM feature_artifacts dup
                      WHERE dup.feature_id = :into
                        AND dup.artifact_type = fa.artifact_type
                        AND dup.artifact_id = fa.artifact_id
                  )
                """
            ),
            {"into": into, "loser": fid},
        )
        # 2. Purge the remaining LINKS (duplicates) — never the features.
        await session.execute(
            sa.text("DELETE FROM feature_artifacts WHERE feature_id = :loser"),
            {"loser": fid},
        )
        # 3. Mark the loser.
        await session.execute(
            sa.text(
                "UPDATE features SET merged_into = :into, status = 'archived', "
                "status_updated_at = NOW() WHERE id = :loser"
            ),
            {"into": into, "loser": fid},
        )
        check = (
            (
                await session.execute(
                    sa.text("SELECT merged_into, status FROM features WHERE id = :loser"),
                    {"loser": fid},
                )
            )
            .mappings()
            .one()
        )
        if str(check["merged_into"]) != str(into) or check["status"] != "archived":
            raise PostConditionError(f"merge {fid}: unexpected state {dict(check)!r}")
        left = (
            await session.execute(
                sa.text("SELECT COUNT(*) FROM feature_artifacts WHERE feature_id = :loser"),
                {"loser": fid},
            )
        ).scalar_one()
        if left != 0:
            raise PostConditionError(f"merge {fid}: {left} artifacts still on loser")
    elif op == "archive":
        await session.execute(
            sa.text(
                "UPDATE features SET status = 'archived', status_updated_at = NOW() "
                "WHERE id = :fid"
            ),
            {"fid": fid},
        )
        check = (
            (
                await session.execute(
                    sa.text("SELECT status FROM features WHERE id = :fid"), {"fid": fid}
                )
            )
            .mappings()
            .one()
        )
        if check["status"] != "archived":
            raise PostConditionError(f"archive {fid}: status={check['status']!r}")
    elif op == "status":
        new_status = payload["status"]
        await session.execute(
            sa.text(
                "UPDATE features SET status = :s, status_updated_at = NOW() WHERE id = :fid"
            ),
            {"s": new_status, "fid": fid},
        )
        check = (
            (
                await session.execute(
                    sa.text("SELECT status FROM features WHERE id = :fid"), {"fid": fid}
                )
            )
            .mappings()
            .one()
        )
        if check["status"] != new_status:
            raise PostConditionError(f"status {fid}: status={check['status']!r}")
    elif op == "rename":
        new_name = payload["name"]
        await session.execute(
            sa.text("UPDATE features SET name = :n WHERE id = :fid"),
            {"n": new_name, "fid": fid},
        )
        check = (
            (
                await session.execute(
                    sa.text("SELECT name FROM features WHERE id = :fid"), {"fid": fid}
                )
            )
            .mappings()
            .one()
        )
        if check["name"] != new_name:
            raise PostConditionError(f"rename {fid}: name={check['name']!r}")
    else:
        raise PostConditionError(f"unknown op {op!r}")


async def apply_proposals(
    session_factory: Any,
    proposal_ids: list[int],
    allowed_ops: tuple[str, ...] | None = None,
) -> int:
    """Apply proposals reviewées — une transaction par proposal.

    allowed_ops : en wet nocturne, WET_APPLYABLE_OPS ; None (--apply-ids,
    review humaine) = toutes les ops. Une post-condition en échec rollback
    la proposal (elle reste 'proposed') et on continue.
    """
    from brain_v42.db.tables import roadmap_curation_proposals  # noqa: PLC0415

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    sa.select(roadmap_curation_proposals).where(
                        roadmap_curation_proposals.c.id.in_(proposal_ids),
                        roadmap_curation_proposals.c.status == "proposed",
                    )
                )
            )
            .mappings()
            .all()
        )

    applied = 0
    for row in rows:
        if allowed_ops is not None and row["op"] not in allowed_ops:
            print(f"~ proposal {row['id']} ({row['op']}) hors allowed_ops — laissée en review")
            continue
        try:
            async with session_factory() as session:
                async with session.begin():
                    await _apply_one(session, row)
                    await session.execute(
                        roadmap_curation_proposals.update()
                        .where(roadmap_curation_proposals.c.id == row["id"])
                        .values(status="applied", applied_at=sa.func.now())
                    )
            applied += 1
        except Exception as exc:
            print(f"! proposal {row['id']} ({row['op']}) failed: {_exc_str(exc)}")
    return applied


async def record_dream_run(
    session_factory: Any,
    status: str,
    dry: bool,
    duration_s: float,
    error: str | None,
) -> None:
    """INSERT dream_runs row for phase='roadmap'. Best-effort — never raises."""
    try:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    sa.text(
                        "INSERT INTO dream_runs "
                        "(run_date, phase, status, duration_s, error_message, phase_dry_run) "
                        "VALUES (:run_date, 'roadmap', :status, :duration_s, "
                        ":error_message, :phase_dry_run)"
                    ),
                    {
                        "run_date": date.today(),
                        "status": status,
                        "duration_s": duration_s,
                        "error_message": error,
                        "phase_dry_run": dry,
                    },
                )
    except Exception as exc:
        print(f"! warning: could not record dream_run: {exc}")


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"doit être >= 1 (reçu : {number})")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="roadmap_curate",
        description="Proposer-only roadmap curation (NVIDIA API).",
    )
    parser.add_argument(
        "--limit", type=_positive_int, default=10, help="max projets à traiter"
    )
    parser.add_argument(
        "--wet",
        action="store_true",
        help="propose puis applique les proposals archive/status de ce run",
    )
    parser.add_argument(
        "--apply-ids",
        default=None,
        help='apply des proposals reviewées (ex: "3,4") — incompatible avec --wet',
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"défaut: env BRAIN_NVIDIA_MODEL puis {DEFAULT_MODEL}",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"défaut: env BRAIN_NVIDIA_BASE_URL puis {DEFAULT_BASE_URL}",
    )
    args = parser.parse_args()

    if args.wet and args.apply_ids is not None:
        parser.error("--wet et --apply-ids sont incompatibles")

    load_env_file(_ENV_FILE)

    import os  # noqa: PLC0415

    api_key = os.environ.get(_API_KEY_VAR, "")
    if not api_key and args.apply_ids is None:
        print(
            f"{_API_KEY_VAR} manquant — renseigne-le dans {_ENV_FILE}.",
            file=sys.stderr,
        )
        return 2

    model = args.model or os.environ.get("BRAIN_NVIDIA_MODEL") or DEFAULT_MODEL
    base_url = args.base_url or os.environ.get("BRAIN_NVIDIA_BASE_URL") or DEFAULT_BASE_URL

    return asyncio.run(_run(args, api_key, model, base_url))


async def _run(args: Any, api_key: str, model: str, base_url: str) -> int:
    from pydantic import ValidationError  # noqa: PLC0415

    from brain_v42.config import Settings  # noqa: PLC0415
    from brain_v42.db.engine import get_session_factory  # noqa: PLC0415

    try:
        Settings()  # type: ignore[call-arg]  # validate config early
    except ValidationError as exc:
        print(f"Config invalide: {exc}", file=sys.stderr)
        return 2

    sf = get_session_factory()
    t0 = time.monotonic()
    any_failed = False
    error_msg: str | None = None

    # --apply-ids mode: no LLM, reviewed apply (all ops).
    if args.apply_ids is not None:
        try:
            ids = [int(x.strip()) for x in args.apply_ids.split(",") if x.strip()]
        except ValueError:
            print(
                "--apply-ids doit être une liste d'entiers séparés par des virgules",
                file=sys.stderr,
            )
            return 1
        applied = await apply_proposals(sf, ids, allowed_ops=None)
        duration = time.monotonic() - t0
        print(f"apply: {applied} appliqués")
        await record_dream_run(sf, "done", dry=False, duration_s=duration, error=None)
        return 0

    # Propose mode (dry ou wet).
    batches = await fetch_project_batches(sf, args.limit)
    if not batches:
        print("Aucune feature vivante — rien à curer.")
        await record_dream_run(
            sf, "done", dry=not args.wet, duration_s=time.monotonic() - t0, error=None
        )
        return 0

    http_client = httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0),
    )

    outcomes: list[BatchOutcome] = []
    try:
        for batch in batches:
            outcome = await curate_batch(http_client, model, batch)
            outcomes.append(outcome)
    finally:
        await http_client.aclose()

    all_drafts: list[CurationDraft] = []
    scanned = len(outcomes)
    skipped = 0
    failed = 0
    for outcome in outcomes:
        if outcome.failed:
            failed += 1
            any_failed = True
            error_msg = outcome.error
            print(f"! projet {outcome.batch.project_key} failed: {outcome.error}")
            continue
        if not outcome.drafts:
            skipped += 1
        all_drafts.extend(outcome.drafts)

    # Global cap — drop logged, never silent (spec §3).
    if len(all_drafts) > MAX_PROPOSALS_PER_NIGHT:
        dropped = len(all_drafts) - MAX_PROPOSALS_PER_NIGHT
        print(
            f"! cap {MAX_PROPOSALS_PER_NIGHT} proposals/nuit atteint — "
            f"{dropped} droppées (pas de troncature silencieuse)"
        )
        all_drafts = all_drafts[:MAX_PROPOSALS_PER_NIGHT]

    proposal_ids = await persist_proposals(sf, all_drafts)
    print(
        f"{scanned} projets scannés, {len(proposal_ids)} proposals, "
        f"{skipped} sans proposition, {failed} failed"
    )
    if proposal_ids:
        print(f"proposal ids: {proposal_ids}")

    # --wet: apply of the run, restricted to safe ops. NEVER merge/rename.
    if args.wet and proposal_ids:
        applied = await apply_proposals(sf, proposal_ids, allowed_ops=WET_APPLYABLE_OPS)
        print(f"wet: {applied} appliqués (ops {WET_APPLYABLE_OPS})")

    duration = time.monotonic() - t0
    status = "fail" if any_failed else "done"
    await record_dream_run(
        sf, status=status, dry=not args.wet, duration_s=duration, error=error_msg
    )
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_roadmap_curate_apply.py tests/unit/test_roadmap_curate.py -v`
Expected: PASS.

- [ ] **Step 5: Gates + commit**

```bash
uv run pytest tests/unit -q && uv run ruff check src/ tests/ scripts/ && uv run ruff format --check src/ tests/ scripts/ && uv run mypy src/
git add scripts/roadmap_curate.py tests/unit/test_roadmap_curate_apply.py
git commit -m "feat(roadmap): cureur — apply avec post-conditions positives + CLI"
```

---

### Task 5: Step dream.sh + killswitch ROADMAP (briefing)

**Files:**
- Modify: `scripts/dream.sh` (env defaults ≈line 39; block after EXTRACT ≈line 512)
- Modify: `src/brain_v42/services/dream_run_service.py` (KillswitchState + killswitch_state)
- Modify: `src/brain_v42/mcp/tools/session_tools.py` (`_section_killswitches`)
- Test: `tests/unit/test_dream_sh_roadmap.py`, `tests/unit/services/test_dream_run_service.py` (append), `tests/unit/mcp/test_session_tools.py` (append)

**Interfaces:**
- Consumes: `scripts.roadmap_curate` CLI (Task 4); existing `_clean_dry_streak(session, "roadmap")`.
- Produces: env vars `BRAIN_DREAM_ROADMAP_ENABLED` (default `false`) / `BRAIN_DREAM_ROADMAP_DRY_RUN` (default `true`); `KillswitchState.roadmap_enabled: bool = False`, `roadmap_dry: bool = True`, `roadmap_clean_dry_nights: int = 0`; `- ROADMAP: …` line in the briefing.

**⚠ Before modifying:** `gitnexus_impact({target: "killswitch_state", direction: "upstream"})` and `gitnexus_impact({target: "_section_killswitches", direction: "upstream"})` — report the blast radius.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_dream_sh_roadmap.py` (exact mirror of `test_dream_sh_extract.py`):

```python
"""Pin the ROADMAP killswitch wiring in dream.sh (grep-style, no execution)."""

from pathlib import Path

_DREAM_SH = Path(__file__).parent.parent.parent / "scripts" / "dream.sh"


def _content() -> str:
    return _DREAM_SH.read_text(encoding="utf-8")


def test_roadmap_killswitch_defaults_closed_and_dry():
    content = _content()
    assert 'BRAIN_DREAM_ROADMAP_ENABLED="${BRAIN_DREAM_ROADMAP_ENABLED:-false}"' in content
    assert 'BRAIN_DREAM_ROADMAP_DRY_RUN="${BRAIN_DREAM_ROADMAP_DRY_RUN:-true}"' in content


def test_roadmap_step_invokes_cli_module():
    content = _content()
    assert "scripts.roadmap_curate" in content
    assert "SKIP roadmap (killswitch" in content


def test_roadmap_wet_flag_only_when_dry_run_false():
    content = _content()
    assert 'if [[ "$BRAIN_DREAM_ROADMAP_DRY_RUN" != "true" ]]' in content


def test_roadmap_step_has_timeout_and_own_log():
    content = _content()
    assert "timeout 10m uv run python -m scripts.roadmap_curate" in content
    assert "_roadmap.log" in content
```

Add to `tests/unit/services/test_dream_run_service.py`, inside `class TestKillswitchState`:

```python
    @pytest.mark.asyncio
    async def test_roadmap_enabled_dry_with_streak(self, session_factory):
        for i in range(2):
            d = date.today() - timedelta(days=i)
            await _insert_run(
                session_factory, run_date=d, phase="roadmap", status="done", phase_dry_run=True
            )
        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state()
        assert state.roadmap_enabled is True
        assert state.roadmap_dry is True
        assert state.roadmap_clean_dry_nights == 2

    @pytest.mark.asyncio
    async def test_roadmap_disabled_when_phase_absent(self, session_factory):
        await _insert_run(session_factory, run_date=date.today(), phase="promote")
        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state()
        assert state.roadmap_enabled is False
        assert state.roadmap_dry is True
        assert state.roadmap_clean_dry_nights == 0
```

Add to `tests/unit/mcp/test_session_tools.py`, inside `class TestSectionKillswitches` (same kwargs as the existing extract tests — see `test_extract_enabled_dry_with_streak` line ≈175 for the `KillswitchState` construction template):

```python
    def test_roadmap_enabled_dry_with_streak(self):
        state = KillswitchState(
            last_run_date=date(2026, 7, 4),
            promote_enabled=False,
            promote_dry=False,
            reorg_enabled=False,
            reorg_dry=False,
            promote_clean_dry_nights=0,
            reorg_clean_dry_nights=0,
            roadmap_enabled=True,
            roadmap_dry=True,
            roadmap_clean_dry_nights=2,
        )
        out = _section_killswitches(state)
        assert "- ROADMAP: enabled (dry · 2 clean DRY nights)" in out

    def test_roadmap_disabled_by_default(self):
        state = KillswitchState(
            last_run_date=date(2026, 7, 4),
            promote_enabled=False,
            promote_dry=False,
            reorg_enabled=False,
            reorg_dry=False,
            promote_clean_dry_nights=0,
            reorg_clean_dry_nights=0,
        )
        out = _section_killswitches(state)
        assert "- ROADMAP: disabled" in out
```

(`date` import already present at the top of the file.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_dream_sh_roadmap.py tests/unit/services/test_dream_run_service.py tests/unit/mcp/test_session_tools.py -v`
Expected: FAIL — dream.sh without a roadmap block; `TypeError: unexpected keyword argument 'roadmap_enabled'`; `- ROADMAP:` missing.

- [ ] **Step 3: Implement — dream.sh**

In `scripts/dream.sh`, after the EXTRACT lines (≈39), add:

```bash
# ROADMAP killswitch — nightly roadmap curation (proposer-only, spec
# 2026-07-04). Ships CLOSED; once opened it starts in DRY (propose-only,
# human review via roadmap_curation_proposals) — same soak trajectory
# as EXTRACT. The nightly wet mode will apply ONLY archive/status (the CLI
# restricts via WET_APPLYABLE_OPS); merge/rename remain under review.
BRAIN_DREAM_ROADMAP_ENABLED="${BRAIN_DREAM_ROADMAP_ENABLED:-false}"
BRAIN_DREAM_ROADMAP_DRY_RUN="${BRAIN_DREAM_ROADMAP_DRY_RUN:-true}"
```

After the complete EXTRACT block (just before `FAIL_TOTAL=` ≈line 514), add:

```bash
# --- ROADMAP: nightly roadmap curation (proposer-only) --------------
# Not a claude -p phase: direct Python CLI (extract pattern). Inserts its
# own dream_runs row (phase='roadmap') for briefing visibility.
TOTAL_PHASES=$(( TOTAL_PHASES + 1 ))
if [[ "$BRAIN_DREAM_ROADMAP_ENABLED" != "true" ]]; then
  log "SKIP roadmap (killswitch BRAIN_DREAM_ROADMAP_ENABLED=$BRAIN_DREAM_ROADMAP_ENABLED)"
  SKIPPED_PHASES+=("roadmap")
else
  roadmap_args=(--limit 10)
  if [[ "$BRAIN_DREAM_ROADMAP_DRY_RUN" != "true" ]]; then
    roadmap_args+=(--wet)
  fi
  log "roadmap: roadmap_curate starting (dry_run=$BRAIN_DREAM_ROADMAP_DRY_RUN)"
  set +e
  timeout 10m uv run python -m scripts.roadmap_curate "${roadmap_args[@]}" \
    >> "$LOG_DIR/${TIMESTAMP}_roadmap.log" 2>&1
  roadmap_rc=$?
  set -e
  if (( roadmap_rc == 0 )); then
    log "DONE roadmap"
  else
    log "FAIL roadmap (rc=$roadmap_rc) — see ${TIMESTAMP}_roadmap.log"
    FAILED_PHASES+=("roadmap")
  fi
fi
```

- [ ] **Step 4: Implement — KillswitchState + section**

In `src/brain_v42/services/dream_run_service.py`:

(a) `KillswitchState` — add after `extract_clean_dry_nights`:

```python
    roadmap_enabled: bool = False
    roadmap_dry: bool = True
    roadmap_clean_dry_nights: int = 0
```

(b) `killswitch_state()` — after the extract block (≈line 93), add:

```python
            roadmap_enabled = "roadmap" in phases
            roadmap_dry = bool(phases["roadmap"]["phase_dry_run"]) if roadmap_enabled else True
            roadmap_streak = await self._clean_dry_streak(session, "roadmap")
```

and complete the return constructor:

```python
            extract_clean_dry_nights=extract_streak,
            roadmap_enabled=roadmap_enabled,
            roadmap_dry=roadmap_dry,
            roadmap_clean_dry_nights=roadmap_streak,
        )
```

In `src/brain_v42/mcp/tools/session_tools.py`, `_section_killswitches` — after the EXTRACT line:

```python
    lines.append(
        _row("ROADMAP", state.roadmap_enabled, state.roadmap_dry, state.roadmap_clean_dry_nights)
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_dream_sh_roadmap.py tests/unit/services/test_dream_run_service.py tests/unit/mcp/test_session_tools.py tests/unit/test_dream_sh_extract.py tests/unit/test_dream_sh_phase_timeouts.py -v`
Expected: PASS (the existing extract/timeouts pins stay green).

- [ ] **Step 6: Bash syntax check + gates + commit**

```bash
bash -n scripts/dream.sh
uv run pytest tests/unit -q && uv run ruff check src/ tests/ scripts/ && uv run ruff format --check src/ tests/ scripts/ && uv run mypy src/
git add scripts/dream.sh src/brain_v42/services/dream_run_service.py src/brain_v42/mcp/tools/session_tools.py tests/unit/test_dream_sh_roadmap.py tests/unit/services/test_dream_run_service.py tests/unit/mcp/test_session_tools.py
git commit -m "feat(dream): step roadmap killswitché + ligne ROADMAP au briefing"
```

---

### Task 6: Briefing — "Roadmap" section (replaces "In-flight")

**Files:**
- Modify: `src/brain_v42/services/feature_service.py` (+`RoadmapAliveFeature`, +`roadmap_alive`, −`in_flight`)
- Modify: `src/brain_v42/mcp/tools/session_tools.py` (`_section_roadmap` replaces `_section_in_flight`)
- Test: `tests/unit/mcp/test_session_tools.py` (modify), `tests/unit/services/test_feature_service_roadmap.py` (create)

**Interfaces:**
- Consumes: `merged_into` column (Task 1).
- Produces: `RoadmapAliveFeature(name: str, status: str, pinned: bool, artifact_count: int, last_artifact_at: datetime | None)` (frozen dataclass); `FeatureService.roadmap_alive(project_key: str, limit: int = 5) -> list[RoadmapAliveFeature]`; `_section_roadmap(items) -> str` with format `- <nom> [<statut>] — <N> artifacts, dernier il y a <X>j`.

**⚠ Before modifying:** `gitnexus_impact({target: "in_flight", direction: "upstream"})` — verify that `brain_session_start` is the only caller before removal; otherwise STOP and report.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/services/test_feature_service_roadmap.py`:

```python
"""Tests for FeatureService.roadmap_alive (mocked session — SQL is PG-only)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.services.feature_service import FeatureService, RoadmapAliveFeature


def _factory_with_rows(rows: list[dict]):
    mock_session = MagicMock(spec=AsyncSession)
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows
    mock_session.execute = AsyncMock(return_value=result)

    @asynccontextmanager
    async def factory():
        yield mock_session

    return factory, mock_session


class TestRoadmapAlive:
    @pytest.mark.asyncio
    async def test_maps_rows_to_dataclass(self):
        rows = [
            {
                "name": "Recherche hybride",
                "status": "building",
                "pinned": True,
                "artifact_count": 7,
                "last_artifact_at": datetime(2026, 7, 2, tzinfo=UTC),
            },
            {
                "name": "Cluster X",
                "status": "research",
                "pinned": False,
                "artifact_count": 0,
                "last_artifact_at": None,
            },
        ]
        factory, _ = _factory_with_rows(rows)
        svc = FeatureService(factory)
        items = await svc.roadmap_alive("brain-v42", limit=5)
        assert items == [
            RoadmapAliveFeature(
                name="Recherche hybride",
                status="building",
                pinned=True,
                artifact_count=7,
                last_artifact_at=datetime(2026, 7, 2, tzinfo=UTC),
            ),
            RoadmapAliveFeature(
                name="Cluster X",
                status="research",
                pinned=False,
                artifact_count=0,
                last_artifact_at=None,
            ),
        ]

    @pytest.mark.asyncio
    async def test_query_filters_alive_and_orders_pinned_first(self):
        factory, session = _factory_with_rows([])
        svc = FeatureService(factory)
        await svc.roadmap_alive("brain-v42")
        sql = str(session.execute.call_args[0][0])
        assert "NOT IN ('done', 'archived')" in sql
        assert "merged_into IS NULL" in sql
        assert "pinned" in sql
        assert "NULLS LAST" in sql

    @pytest.mark.asyncio
    async def test_in_flight_removed(self):
        assert not hasattr(FeatureService, "in_flight")
```

In `tests/unit/mcp/test_session_tools.py`:

(a) Replace the `_section_in_flight` import with `_section_roadmap` in the import block at the top.

(b) Replace the `TestSectionInFlight` class (≈line 215) with:

```python
class TestSectionRoadmap:
    def _item(self, **kw):
        from brain_v42.services.feature_service import RoadmapAliveFeature

        defaults = {
            "name": "Recherche hybride",
            "status": "building",
            "pinned": False,
            "artifact_count": 7,
            "last_artifact_at": datetime.now(UTC) - timedelta(days=2),
        }
        defaults.update(kw)
        return RoadmapAliveFeature(**defaults)

    def test_renders_spec_format(self):
        out = _section_roadmap([self._item()])
        assert out.startswith("### Roadmap (1)")
        assert "- Recherche hybride [building] — 7 artifacts, dernier il y a 2j" in out

    def test_zero_artifacts_renders_without_last(self):
        out = _section_roadmap([self._item(artifact_count=0, last_artifact_at=None)])
        assert "— 0 artifact" in out
        assert "dernier il y a" not in out

    def test_omits_when_empty(self):
        assert _section_roadmap([]) == ""

    def test_cap_5(self):
        items = [self._item(name=f"f{i}") for i in range(8)]
        out = _section_roadmap(items)
        assert "### Roadmap (5)" in out
        assert "f4" in out and "f5" not in out
```

(c) In `test_calls_all_services` (≈line 335): replace `mock_feature_svc.in_flight = AsyncMock(return_value=[])` with `mock_feature_svc.roadmap_alive = AsyncMock(return_value=[])` and the assertion `mock_feature_svc.in_flight.assert_called_once()` with `mock_feature_svc.roadmap_alive.assert_called_once()`. Sweep the rest of the file: every other occurrence of `in_flight` on a mock feature_svc (e.g. in `test_partial_failure_returns_degraded_briefing`, `test_killswitch_crash_renders_unavailable_not_no_activity`, `TestCrossProjectInTool`) moves to `roadmap_alive`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/services/test_feature_service_roadmap.py tests/unit/mcp/test_session_tools.py -v`
Expected: FAIL — `ImportError: cannot import name 'RoadmapAliveFeature'`, `ImportError: cannot import name '_section_roadmap'`.

- [ ] **Step 3: Implement — FeatureService**

In `src/brain_v42/services/feature_service.py`:

(a) Add to the imports: `from dataclasses import dataclass` and `import sqlalchemy as sa` (already present).

(b) Add at module level (before the class):

```python
_ROADMAP_ALIVE_SQL = """
SELECT f.name,
       f.status,
       COALESCE(f.pinned, false) AS pinned,
       COUNT(fa.artifact_id) AS artifact_count,
       MAX(fa.created_at) AS last_artifact_at
FROM features f
LEFT JOIN feature_artifacts fa ON fa.feature_id = f.id
WHERE f.project_key = :pk
  AND f.status NOT IN ('done', 'archived')
  AND f.merged_into IS NULL
GROUP BY f.id, f.name, f.status, f.pinned
ORDER BY COALESCE(f.pinned, false) DESC,
         MAX(fa.created_at) DESC NULLS LAST
LIMIT :lim
"""


@dataclass(frozen=True)
class RoadmapAliveFeature:
    """Ligne de la section briefing « Roadmap » (spec 2026-07-04 §5)."""

    name: str
    status: str
    pinned: bool
    artifact_count: int
    last_artifact_at: datetime | None
```

(c) REMOVE the `in_flight` method (impact verified beforehand) and add instead:

```python
    async def roadmap_alive(
        self,
        project_key: str,
        limit: int = 5,
    ) -> list[RoadmapAliveFeature]:
        """Features vivantes : statut ∉ {done, archived} ∧ non mergées.

        Pinned en tête, puis dernière activité artifact desc (NULLS LAST).
        Remplace in_flight (spec 2026-07-04 §5 — la section briefing
        « Roadmap » remplace « In-flight »).
        """
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        sa.text(_ROADMAP_ALIVE_SQL), {"pk": project_key, "lim": limit}
                    )
                )
                .mappings()
                .all()
            )
        return [
            RoadmapAliveFeature(
                name=r["name"],
                status=r["status"],
                pinned=bool(r["pinned"]),
                artifact_count=int(r["artifact_count"] or 0),
                last_artifact_at=r["last_artifact_at"],
            )
            for r in rows
        ]
```

(d) Update the module docstring (both queries: roadmap_alive + stale_pinned).

- [ ] **Step 4: Implement — session_tools**

In `src/brain_v42/mcp/tools/session_tools.py`:

(a) Replace `_section_in_flight` (≈line 124) with:

```python
def _section_roadmap(items: list[Any]) -> str:
    """### Roadmap — features vivantes (spec 2026-07-04 §5, remplace In-flight)."""
    if not items:
        return ""
    lines = [f"### Roadmap ({min(len(items), _CAP)})"]
    for f in items[:_CAP]:
        if f.last_artifact_at is not None:
            days = max(0, (datetime.now(UTC) - f.last_artifact_at).days)
            suffix = f"{f.artifact_count} artifacts, dernier il y a {days}j"
        else:
            suffix = f"{f.artifact_count} artifact"
        lines.append(f"- {f.name} [{f.status}] — {suffix}")
    return "\n".join(lines)
```

(b) In `_format_session_briefing`: rename the `in_flight` parameter to `roadmap_items` and replace `_section_in_flight(in_flight)` with `_section_roadmap(roadmap_items)` in the `sections` list (same position — before Stale-pinned).

(c) In `brain_session_start`: replace `feature_svc.in_flight(project_key=project_key, limit=5)` with `feature_svc.roadmap_alive(project_key=project_key, limit=5)` in the `asyncio.gather`, and rename the local variable `in_flight` to `roadmap_items` (unpacking + `roadmap_items = roadmap_items or []` + passing to `_format_session_briefing`).

(d) Update the tool docstring: "in-flight features" → "roadmap (features vivantes)".

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/services/test_feature_service_roadmap.py tests/unit/mcp/test_session_tools.py -v`
Expected: PASS. If old FeatureService tests break on `in_flight` (search with `grep -rn "in_flight" tests/`), migrate them to `roadmap_alive` or remove them if they only tested the removed method.

- [ ] **Step 6: Gates + commit**

```bash
uv run pytest tests/unit -q && uv run ruff check src/ tests/ scripts/ && uv run ruff format --check src/ tests/ scripts/ && uv run mypy src/
git add src/brain_v42/services/feature_service.py src/brain_v42/mcp/tools/session_tools.py tests/unit/services/test_feature_service_roadmap.py tests/unit/mcp/test_session_tools.py
git commit -m "feat(briefing): section Roadmap vivante — remplace In-flight"
```

---

### Task 7: Write-back — tool `brain_feature_update` (41→42)

**Files:**
- Modify: `src/brain_v42/services/feature_service.py` (+resolve_id_prefix, +resolve_feature, +update_status, +VALID_FEATURE_STATUSES)
- Modify: `src/brain_v42/mcp/tools/roadmap_tools.py` (+brain_feature_update, +param feature_svc)
- Modify: `src/brain_v42/mcp/server.py` (wire shared feature_svc + docstring 41→42)
- Test: `tests/unit/services/test_feature_service_resolve.py`, `tests/unit/mcp/tools/test_feature_update_tool.py`

**Interfaces:**
- Consumes: `parse_uuid`, `normalize_uuid_prefix`, `resolve_entity_id` from `brain_v42.mcp.tools.parsing`; `format_error`, `short_id` from `brain_v42.mcp.tools.formatters`; `canonicalize_project_key`.
- Produces: `VALID_FEATURE_STATUSES = ("planned", "research", "design", "building", "deployed", "done", "archived")`; `FeatureService.resolve_id_prefix(prefix_hex: str, *, limit: int = 6) -> list[UUID]`; `FeatureService.resolve_feature(project_key: str, feature: str) -> Feature | str` (str = error message); `FeatureService.update_status(feature_id: UUID, status: str) -> Feature | None`; MCP tool `brain_feature_update(feature: str, status: str, project_key: str) -> str`.

**⚠ Before modifying:** `gitnexus_impact({target: "register_roadmap_tools", direction: "upstream"})` (extended signature → adapt the call sites, including the existing tests `test_roadmap_tools.py`).

- [ ] **Step 1: Write the failing service tests**

Create `tests/unit/services/test_feature_service_resolve.py`:

```python
"""Tests for FeatureService.resolve_feature / update_status (mocked sessions)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.services.feature_service import (
    VALID_FEATURE_STATUSES,
    FeatureService,
)

_NOW = datetime(2026, 7, 4, tzinfo=UTC)


def _row(**kw) -> dict:
    defaults: dict = {
        "id": uuid4(),
        "project_key": "brain-v42",
        "name": "Recherche hybride",
        "description": "d",
        "status": "building",
        "status_updated_at": _NOW,
        "pinned": False,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(kw)
    return defaults


def _factory(side_effects: list) -> tuple:
    mock_session = MagicMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(side_effect=side_effects)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)

    @asynccontextmanager
    async def factory():
        yield mock_session

    return factory, mock_session


def _mappings_all(rows: list[dict]) -> MagicMock:
    r = MagicMock()
    r.mappings.return_value.all.return_value = rows
    return r


def _mappings_one_or_none(row: dict | None) -> MagicMock:
    r = MagicMock()
    r.mappings.return_value.one_or_none.return_value = row
    return r


def _scalars_all(values: list) -> MagicMock:
    r = MagicMock()
    r.scalars.return_value.all.return_value = values
    return r


class TestStatuses:
    def test_valid_statuses_include_archived(self):
        assert VALID_FEATURE_STATUSES == (
            "planned", "research", "design", "building", "deployed", "done", "archived",
        )


class TestResolveFeature:
    @pytest.mark.asyncio
    async def test_exact_name_hit(self):
        row = _row()
        factory, _ = _factory([_mappings_all([row])])
        svc = FeatureService(factory)
        resolved = await svc.resolve_feature("brain-v42", "Recherche hybride")
        assert not isinstance(resolved, str)
        assert resolved.id == row["id"]

    @pytest.mark.asyncio
    async def test_exact_name_ambiguous_lists_candidates(self):
        r1, r2 = _row(), _row(name="Recherche hybride")
        factory, _ = _factory([_mappings_all([r1, r2])])
        svc = FeatureService(factory)
        resolved = await svc.resolve_feature("brain-v42", "Recherche hybride")
        assert isinstance(resolved, str)
        assert str(r1["id"])[:8] in resolved and str(r2["id"])[:8] in resolved

    @pytest.mark.asyncio
    async def test_id_prefix_unique_hit(self):
        row = _row()
        prefix = str(row["id"]).replace("-", "")[:12]
        factory, _ = _factory(
            [
                _mappings_all([]),                 # exact name: miss
                _scalars_all([row["id"]]),         # resolve_id_prefix: 1 match
                _mappings_one_or_none(row),        # SELECT by id
            ]
        )
        svc = FeatureService(factory)
        resolved = await svc.resolve_feature("brain-v42", prefix)
        assert not isinstance(resolved, str)
        assert resolved.id == row["id"]

    @pytest.mark.asyncio
    async def test_id_prefix_wrong_project_rejected(self):
        row = _row(project_key="red-monitor")
        prefix = str(row["id"]).replace("-", "")[:12]
        factory, _ = _factory(
            [
                _mappings_all([]),
                _scalars_all([row["id"]]),
                _mappings_one_or_none(row),
            ]
        )
        svc = FeatureService(factory)
        resolved = await svc.resolve_feature("brain-v42", prefix)
        assert isinstance(resolved, str)
        assert "red-monitor" in resolved

    @pytest.mark.asyncio
    async def test_ilike_unique_hit(self):
        row = _row()
        factory, _ = _factory(
            [
                _mappings_all([]),      # exact: miss (pas hex → pas de branche id)
                _mappings_all([row]),   # ILIKE: 1 match
            ]
        )
        svc = FeatureService(factory)
        resolved = await svc.resolve_feature("brain-v42", "hybride")
        assert not isinstance(resolved, str)
        assert resolved.id == row["id"]

    @pytest.mark.asyncio
    async def test_ilike_ambiguous_lists_id_and_name(self):
        r1, r2 = _row(name="Recherche hybride"), _row(name="Recherche hybride v2")
        factory, _ = _factory([_mappings_all([]), _mappings_all([r1, r2])])
        svc = FeatureService(factory)
        resolved = await svc.resolve_feature("brain-v42", "hybride")
        assert isinstance(resolved, str)
        assert "Recherche hybride v2" in resolved
        assert str(r1["id"])[:8] in resolved

    @pytest.mark.asyncio
    async def test_no_match_explicit_error(self):
        factory, _ = _factory([_mappings_all([]), _mappings_all([])])
        svc = FeatureService(factory)
        resolved = await svc.resolve_feature("brain-v42", "inexistante")
        assert isinstance(resolved, str)
        assert "inexistante" in resolved and "brain-v42" in resolved


class TestUpdateStatus:
    @pytest.mark.asyncio
    async def test_update_pins_and_returns_feature(self):
        row = _row(status="deployed", pinned=True)
        result = MagicMock()
        result.mappings.return_value.one_or_none.return_value = row
        factory, session = _factory([result])
        svc = FeatureService(factory)
        updated = await svc.update_status(row["id"], "deployed")
        assert updated is not None
        assert updated.status == "deployed"
        assert updated.pinned is True
        # The UPDATE statement does carry status + pinned + status_updated_at.
        stmt = session.execute.call_args[0][0]
        compiled = str(stmt)
        assert "status" in compiled and "pinned" in compiled

    @pytest.mark.asyncio
    async def test_update_unknown_id_returns_none(self):
        result = MagicMock()
        result.mappings.return_value.one_or_none.return_value = None
        factory, _ = _factory([result])
        svc = FeatureService(factory)
        assert await svc.update_status(uuid4(), "done") is None
```

- [ ] **Step 2: Write the failing tool tests**

Create `tests/unit/mcp/tools/test_feature_update_tool.py`:

```python
"""Tests for brain_feature_update MCP tool (surface 42)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastmcp import FastMCP

from brain_v42.mcp.tools.roadmap_tools import register_roadmap_tools
from brain_v42.models.feature import Feature

_NOW = datetime(2026, 7, 4, tzinfo=UTC)


def _feature(**kw) -> Feature:
    defaults: dict = {
        "id": uuid4(),
        "project_key": "brain-v42",
        "name": "Recherche hybride",
        "description": "d",
        "status": "deployed",
        "status_updated_at": _NOW,
        "pinned": True,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(kw)
    return Feature(**defaults)


def _mcp_with_mocks(feature_svc=None):
    mcp = FastMCP("test")
    register_roadmap_tools(
        mcp, roadmap_svc=MagicMock(), feature_svc=feature_svc or MagicMock()
    )
    return mcp


class TestBrainFeatureUpdate:
    @pytest.mark.asyncio
    async def test_tool_registered(self):
        mcp = _mcp_with_mocks()
        tool = await mcp.get_tool("brain_feature_update")
        assert tool is not None

    @pytest.mark.asyncio
    async def test_invalid_status_rejected_without_service_call(self):
        svc = MagicMock()
        svc.resolve_feature = AsyncMock()
        mcp = _mcp_with_mocks(svc)
        tool = await mcp.get_tool("brain_feature_update")
        result = await tool.fn(feature="x", status="shipped", project_key="brain-v42")
        assert "Invalid status" in result
        svc.resolve_feature.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolution_error_passthrough(self):
        svc = MagicMock()
        svc.resolve_feature = AsyncMock(return_value="No feature matching 'x' in project 'brain-v42'")
        mcp = _mcp_with_mocks(svc)
        tool = await mcp.get_tool("brain_feature_update")
        result = await tool.fn(feature="x", status="done", project_key="brain-v42")
        assert "No feature matching" in result

    @pytest.mark.asyncio
    async def test_happy_path_updates_and_reports(self):
        feat = _feature()
        svc = MagicMock()
        svc.resolve_feature = AsyncMock(return_value=feat)
        svc.update_status = AsyncMock(return_value=feat)
        mcp = _mcp_with_mocks(svc)
        tool = await mcp.get_tool("brain_feature_update")
        result = await tool.fn(
            feature="Recherche hybride", status="deployed", project_key="brain-v42"
        )
        svc.update_status.assert_called_once_with(feat.id, "deployed")
        assert "Recherche hybride" in result
        assert "deployed" in result
        assert "pinned" in result

    @pytest.mark.asyncio
    async def test_archived_is_a_valid_status(self):
        feat = _feature(status="archived")
        svc = MagicMock()
        svc.resolve_feature = AsyncMock(return_value=feat)
        svc.update_status = AsyncMock(return_value=feat)
        mcp = _mcp_with_mocks(svc)
        tool = await mcp.get_tool("brain_feature_update")
        result = await tool.fn(feature="x", status="archived", project_key="brain-v42")
        assert "archived" in result
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/unit/services/test_feature_service_resolve.py tests/unit/mcp/tools/test_feature_update_tool.py -v`
Expected: FAIL — `ImportError: cannot import name 'VALID_FEATURE_STATUSES'`, `TypeError: register_roadmap_tools() got an unexpected keyword argument 'feature_svc'`.

- [ ] **Step 4: Implement — FeatureService resolve/update**

In `src/brain_v42/services/feature_service.py`:

(a) Additional imports:

```python
import uuid as uuid_mod

from brain_v42.mcp.tools.parsing import normalize_uuid_prefix, parse_uuid, resolve_entity_id
```

(b) Module constant:

```python
# Values from the features_status_check CHECK (migration 030) — 'archived' included:
# a session can manually archive a bogus feature.
VALID_FEATURE_STATUSES = (
    "planned", "research", "design", "building", "deployed", "done", "archived",
)
```

(c) Methods (inside the class):

```python
    async def resolve_id_prefix(
        self,
        prefix_hex: str,
        *,
        limit: int = 6,
    ) -> list[uuid_mod.UUID]:
        """Préfixe git-style → ids features (pattern PgBaseRepo.resolve_id_prefix)."""
        if not prefix_hex or not set(prefix_hex) <= set("0123456789abcdef"):
            return []
        t = self._t
        bare_id = sa.func.replace(sa.cast(t.c.id, sa.Text), "-", "")
        stmt = (
            sa.select(t.c.id)
            .where(bare_id.like(prefix_hex + "%"))
            .order_by(t.c.id)
            .limit(limit)
        )
        async with self._sf() as session:
            return list((await session.execute(stmt)).scalars().all())

    async def resolve_feature(self, project_key: str, feature: str) -> Feature | str:
        """Résout `feature` : nom exact → id (UUID/préfixe ≥8 hex) → ILIKE unique.

        Retourne la Feature ou un message d'erreur (pattern resolve_entity_id :
        str = erreur, isinstance-check au call site). Les candidats ambigus
        sont listés (id court + nom).
        """
        t = self._t
        async with self._sf() as session:
            exact = (
                (
                    await session.execute(
                        sa.select(t).where(
                            t.c.project_key == project_key, t.c.name == feature
                        )
                    )
                )
                .mappings()
                .all()
            )
        if len(exact) == 1:
            return Feature.model_validate(dict(exact[0]))
        if len(exact) > 1:
            listed = ", ".join(f"{str(r['id'])[:8]} « {r['name']} »" for r in exact[:6])
            return (
                f"Ambiguous feature name '{feature}' — matches: {listed}. "
                f"Use an id prefix."
            )

        # id branch: full UUID or git-style prefix (≥8 hex).
        if parse_uuid(feature) is not None or normalize_uuid_prefix(feature) is not None:
            resolved = await resolve_entity_id(feature, self.resolve_id_prefix, label="feature")
            if isinstance(resolved, str):
                return resolved
            async with self._sf() as session:
                row = (
                    (await session.execute(sa.select(t).where(t.c.id == resolved)))
                    .mappings()
                    .one_or_none()
                )
            if row is None:
                return f"No feature found for id {feature}"
            if row["project_key"] != project_key:
                return (
                    f"Feature {resolved} belongs to project '{row['project_key']}', "
                    f"not '{project_key}'"
                )
            return Feature.model_validate(dict(row))

        # Unique ILIKE scoped to the project.
        async with self._sf() as session:
            fuzzy = (
                (
                    await session.execute(
                        sa.select(t)
                        .where(
                            t.c.project_key == project_key,
                            t.c.name.ilike(f"%{feature}%"),
                        )
                        .order_by(t.c.name)
                        .limit(6)
                    )
                )
                .mappings()
                .all()
            )
        if not fuzzy:
            return f"No feature matching '{feature}' in project '{project_key}'"
        if len(fuzzy) > 1:
            listed = ", ".join(f"{str(r['id'])[:8]} « {r['name']} »" for r in fuzzy)
            return f"Ambiguous feature '{feature}' — matches: {listed}. Be more specific."
        return Feature.model_validate(dict(fuzzy[0]))

    async def update_status(
        self,
        feature_id: uuid_mod.UUID,
        status: str,
    ) -> Feature | None:
        """UPDATE status + status_updated_at=now + pinned=true (même contrat
        que le chemin update_feature_statuses de brain_update_project_focus)."""
        t = self._t
        stmt = (
            t.update()
            .where(t.c.id == feature_id)
            .values(status=status, status_updated_at=sa.func.now(), pinned=True)
            .returning(t)
        )
        async with self._sf() as session:
            async with session.begin():
                row = (await session.execute(stmt)).mappings().one_or_none()
        return Feature.model_validate(dict(row)) if row else None
```

Circular import note: `feature_service` imports from `brain_v42.mcp.tools.parsing`, which imports nothing from `services` — no cycle. If mypy/ruff flag a cycle anyway, move the parsing import into the method bodies with `# noqa: PLC0415`.

- [ ] **Step 5: Implement — tool + wiring**

In `src/brain_v42/mcp/tools/roadmap_tools.py` — **Targeted edits, do NOT rewrite the file** (the body of `brain_get_roadmap` stays strictly intact):

(a) Module docstring (line 1) →

```python
"""MCP tools: brain_get_roadmap + brain_feature_update (roadmap curée §6)."""
```

(b) Replace the existing import block (`from brain_v42.mcp.tools.formatters import format_roadmap` and the `if TYPE_CHECKING:`) with:

```python
from brain_v42.mcp.tools.formatters import format_error, format_roadmap, short_id
from brain_v42.models.project_key import canonicalize_project_key
from brain_v42.services.feature_service import VALID_FEATURE_STATUSES

if TYPE_CHECKING:
    from brain_v42.services.feature_service import FeatureService
    from brain_v42.services.roadmap_service import RoadmapService
```

(the `canonicalize_project_key` import already exists — do not duplicate it.)

(c) Extend the signature + docstring of the register function:

```python
def register_roadmap_tools(
    mcp: Any,
    roadmap_svc: RoadmapService,
    feature_svc: FeatureService,
) -> None:
    """Register brain_get_roadmap + brain_feature_update on mcp."""
```

(d) Add the new tool AFTER the `brain_get_roadmap` function (same indentation level, still inside `register_roadmap_tools`):

```python
    @mcp.tool(version="1.0")
    async def brain_feature_update(feature: str, status: str, project_key: str) -> str:
        """Update a roadmap feature's status (write-back livraison → roadmap).

        `feature` accepte : nom exact, préfixe d'id git-style (≥8 hex, tirets
        ignorés) ou fragment unique du nom (ILIKE). Ambiguïté → erreur listant
        les candidats (id + nom).

        `status` : planned | research | design | building | deployed | done |
        archived (une session peut archiver une fausse feature à la main).

        Side-effects : status_updated_at=now(), pinned=true — même contrat que
        brain_update_project_focus(feature_status=…), qui reste fonctionnel.

        Consigne : feature livrée → brain_feature_update(name, 'deployed'|'done').
        """
        project_key = canonicalize_project_key(project_key, strict=False)
        logger.debug(
            "mcp.brain_feature_update",
            project_key=project_key,
            feature=feature,
            status=status,
        )
        if status not in VALID_FEATURE_STATUSES:
            return format_error(
                f"Invalid status '{status}' (valid: {', '.join(VALID_FEATURE_STATUSES)})"
            )
        resolved = await feature_svc.resolve_feature(project_key, feature)
        if isinstance(resolved, str):
            return format_error(resolved)
        updated = await feature_svc.update_status(resolved.id, status)
        if updated is None:
            return format_error(f"Feature {resolved.id} disappeared during update")
        return (
            f"Feature « {updated.name} » [{short_id(str(updated.id))}] → "
            f"{updated.status} (pinned, project {updated.project_key})"
        )
```

In `src/brain_v42/mcp/server.py`:

(a) Docstring line 12: `41 brain_* tools` → `42 brain_* tools`.

(b) ≈lines 585-617 — build ONE shared instance and pass it to both registries:

```python
    from brain_v42.services.feature_service import FeatureService  # noqa: PLC0415

    feature_svc = FeatureService(_session_factory)
```

then `feature_svc=feature_svc` in `register_session_tools(...)` (instead of the inline `FeatureService(_session_factory)`) and:

```python
    register_roadmap_tools(
        mcp, roadmap_svc=services["roadmap_svc"], feature_svc=feature_svc
    )
```

(c) Adapt the existing test call sites: in `tests/unit/mcp/tools/test_roadmap_tools.py`, every `register_roadmap_tools(mcp, roadmap_svc=…)` additionally receives `feature_svc=MagicMock()`.

(d) If a tool-count test exists (`grep -rn "41" tests/unit/mcp/ tests/unit/test_*.py` — e.g. `test_toolsux_audit.py` or `test_server.py`), bump it to 42.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/unit/services/test_feature_service_resolve.py tests/unit/mcp/tools/test_feature_update_tool.py tests/unit/mcp/tools/test_roadmap_tools.py tests/unit/mcp/ -v`
Expected: PASS.

- [ ] **Step 7: Gates + commit**

```bash
uv run pytest tests/unit -q && uv run ruff check src/ tests/ scripts/ && uv run ruff format --check src/ tests/ scripts/ && uv run mypy src/
git add src/brain_v42/services/feature_service.py src/brain_v42/mcp/tools/roadmap_tools.py src/brain_v42/mcp/server.py tests/unit/services/test_feature_service_resolve.py tests/unit/mcp/tools/test_feature_update_tool.py tests/unit/mcp/tools/test_roadmap_tools.py
git commit -m "feat(mcp): brain_feature_update — write-back statut feature (tool 42)"
```

---

### Task 8: Dream metrics sidecar — dedup re-runs + per-phase dry_run

**Files:**
- Modify: `src/brain_v42/metrics/collector_dream.py` (`collect_dream_metrics` uniquement)
- Test: `tests/unit/metrics/test_dream_metrics.py` (modify rows + new tests)

**Interfaces:**
- Produces (additive `/metrics` contract, consumed by red-monitor): every `last_run.phases.<phase>` entry gains `"dry_run": bool`; no key renamed/removed. `last_run` computed on the LATEST row per phase (`DISTINCT ON (phase) … ORDER BY phase, id DESC`); `history[].status` computed on the set deduplicated by (run_date, phase); `history[].cost_usd`/`tokens` remain the sum of ALL rows (the money for a failed re-run really was spent).

**⚠ Before modifying:** `gitnexus_impact({target: "collect_dream_metrics", direction: "upstream"})`.

- [ ] **Step 1: Update existing test rows + write the failing tests**

In `tests/unit/metrics/test_dream_metrics.py`:

(a) The last-run SELECT goes from 13 to 14 columns (`phase_dry_run` at index 13). Add a 14th element (`False` by default) to EVERY existing row tuple in the file (the column header comment gains `, phase_dry_run`).

(b) Add the new tests:

```python
class TestDreamMetricsRoadmapSpec7:
    """Spec 2026-07-04 §7 — dédup re-runs + dry_run par phase (contrat additif)."""

    @pytest.mark.asyncio
    async def test_phase_dry_run_flag_exposed(self) -> None:
        rows = [
            ("extract", None, "done", 12.0, 0, 0, 0, 0, 0.0, 0, 0, date(2026, 7, 4), None, True),
            ("scan", "sonnet", "done", 42.0, 100, 10, 0, 0, 0.01, 2, 1, date(2026, 7, 4), None, False),
        ]
        collector = _make_collector_with_dream_data(rows)
        result = await collector.collect_dream_metrics()
        assert result["last_run"]["phases"]["extract"]["dry_run"] is True
        assert result["last_run"]["phases"]["scan"]["dry_run"] is False

    @pytest.mark.asyncio
    async def test_last_run_query_dedups_reruns(self) -> None:
        """La requête last-run prend la DERNIÈRE row par phase (id DESC)."""
        collector = _make_collector_with_dream_data([])
        await collector.collect_dream_metrics()
        # first execute = last-run query
        sql = str(collector._session_factory.return_value.execute.call_args_list[0][0][0])
        assert "DISTINCT ON (phase)" in sql
        assert "ORDER BY phase, id DESC" in sql

    @pytest.mark.asyncio
    async def test_history_status_dedups_but_costs_sum_all(self) -> None:
        rows = [
            ("scan", "sonnet", "done", 1.0, 0, 0, 0, 0, 0.0, 0, 0, date(2026, 7, 4), None, False),
        ]
        collector = _make_collector_with_dream_data(rows)
        await collector.collect_dream_metrics()
        sql = str(collector._session_factory.return_value.execute.call_args_list[1][0][0])
        assert "DISTINCT ON (run_date, phase)" in sql
        # the cost aggregates the full dream_runs (not the deduplicated subset)
        assert "SUM(dr.cost_usd)" in sql

    @pytest.mark.asyncio
    async def test_contract_additive_existing_keys_unchanged(self) -> None:
        rows = [
            ("scan", "sonnet", "done", 42.0, 3500, 250, 0, 0, 0.03, 6, 5, date(2026, 7, 4), None, False),
        ]
        collector = _make_collector_with_dream_data(rows)
        result = await collector.collect_dream_metrics()
        scan = result["last_run"]["phases"]["scan"]
        for key in ("status", "model", "duration_s", "cost_usd", "tokens",
                    "api_calls", "tool_calls", "error_message"):
            assert key in scan
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/metrics/test_dream_metrics.py -v`
Expected: FAIL — `KeyError: 'dry_run'`, `DISTINCT ON` assertions missing. (The existing updated tests still pass: index 11/12 hasn't moved.)

- [ ] **Step 3: Implement**

In `src/brain_v42/metrics/collector_dream.py`, `collect_dream_metrics`:

(a) Replace the last-run query (≈L44-55) with:

```python
                # Latest row PER PHASE of the latest run_date — a re-run the
                # same day (extract fail 06:13 then done 10:58) should no longer
                # make the night "partial" (spec 2026-07-04 §7).
                phases_rows = (
                    await session.execute(
                        text("""
                            SELECT DISTINCT ON (phase)
                                   phase, model, status, duration_s,
                                   input_tokens, output_tokens,
                                   cache_read_tokens, cache_creation_tokens,
                                   cost_usd, api_calls, tool_calls, run_date,
                                   error_message, phase_dry_run
                            FROM dream_runs
                            WHERE run_date = (SELECT MAX(run_date) FROM dream_runs)
                            ORDER BY phase, id DESC
                        """)
                    )
                ).all()
```

(b) In the `for row in phases_rows:` loop, add to the phase dict (after `"error_message": row[12],`):

```python
                        # CLI phases (extract, roadmap): model:null, cost:0
                        # are legitimate — do not "fix" them.
                        "dry_run": bool(row[13]),
```

(c) Replace the history query (≈L97-110) with:

```python
                # History: status on the deduplicated set (latest row per
                # (run_date, phase)), costs/tokens on ALL rows — a
                # failed re-run really did cost money.
                history_rows = (
                    await session.execute(
                        text("""
                            WITH latest AS (
                                SELECT DISTINCT ON (run_date, phase)
                                       run_date, status
                                FROM dream_runs
                                ORDER BY run_date, phase, id DESC
                            ),
                            day_status AS (
                                SELECT run_date,
                                       CASE WHEN COUNT(*) FILTER (WHERE status != 'done') = 0
                                            THEN 'success' ELSE 'partial' END AS status
                                FROM latest
                                GROUP BY run_date
                            )
                            SELECT dr.run_date,
                                   SUM(dr.cost_usd) AS cost,
                                   SUM(dr.input_tokens + dr.output_tokens) AS tokens,
                                   ds.status
                            FROM dream_runs dr
                            JOIN day_status ds ON ds.run_date = dr.run_date
                            GROUP BY dr.run_date, ds.status
                            ORDER BY dr.run_date DESC
                            LIMIT 10
                        """)
                    )
                ).all()
```

(d) Update the method docstring: mention the DISTINCT ON dedup + dry_run + additive contract. Nothing else changes (`phases_ok`/`phases_fail`/`status`/totals already compute on `phases_rows`, now deduplicated).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/metrics/ -v`
Expected: PASS (full file, including promote_outcome).

- [ ] **Step 5: Gates + commit**

```bash
uv run pytest tests/unit -q && uv run ruff check src/ tests/ scripts/ && uv run ruff format --check src/ tests/ scripts/ && uv run mypy src/
git add src/brain_v42/metrics/collector_dream.py tests/unit/metrics/test_dream_metrics.py
git commit -m "fix(metrics): sidecar dream — dédup re-runs par phase + flag dry_run"
```

---

### Task 9: Docs — CLAUDE.md, MCP_TOOLS.md

**Files:**
- Modify: `CLAUDE.md` (delivered-feature instruction + .env killswitches)
- Modify: `docs/MCP_TOOLS.md` (+brain_feature_update, count 41→42)

- [ ] **Step 1: CLAUDE.md — instruction + .env**

(a) In the "Second Cerveau (Mémoire Persistante)" section → "Règles d'utilisation", add:

```markdown
6. **FEATURE livrée** -> `brain_feature_update(feature, 'deployed'|'done', project_key)` — le write-back roadmap remplace le passage par `brain_update_project_focus(feature_status=…)` (qui reste fonctionnel)
```

(b) In the "Tools disponibles" table, add the line:

```markdown
| `brain_feature_update` | Mettre à jour le statut d'une feature roadmap (nom, préfixe d'id ou fragment unique) |
```

(c) In "Configuration" → the `.env` block, after the extract lines:

```bash
# Roadmap — curation nocturne (dream, proposer-only)
BRAIN_DREAM_ROADMAP_ENABLED=false
BRAIN_DREAM_ROADMAP_DRY_RUN=true
```

- [ ] **Step 2: MCP_TOOLS.md**

Read the structure of `docs/MCP_TOOLS.md`, update the tool count (41→42 everywhere it appears) and insert, next to `brain_get_roadmap`:

```markdown
### brain_feature_update

Write-back session → roadmap : met à jour le statut d'une feature (spec 2026-07-04 §6).

**Signature** : `brain_feature_update(feature: str, status: str, project_key: str) -> str`

- `feature` : nom exact → préfixe d'id git-style (≥8 hex, tirets ignorés) → fragment
  unique du nom (ILIKE). Ambiguïté → erreur listant les candidats (id + nom).
- `status` : `planned | research | design | building | deployed | done | archived`.
- Side-effects : `status_updated_at=now()`, `pinned=true`.
- L'ancien chemin `brain_update_project_focus(feature_status=…)` reste fonctionnel.

**Exemple** : `brain_feature_update("Recherche hybride", "deployed", "brain-v42")`
```

- [ ] **Step 3: Gates + commit**

```bash
uv run pytest tests/unit -q && uv run ruff check src/ tests/ scripts/ && uv run ruff format --check src/ tests/ scripts/
git add CLAUDE.md docs/MCP_TOOLS.md
git commit -m "docs(roadmap): consigne brain_feature_update + killswitches roadmap"
```

---

### Task 10: Deployment & rollout (ops — outside CI, to run on PC Serveur)

Operational checklist, in order. No code — each step is verified before the next.

- [ ] **Step 1: Migration** — `git pull` in the server checkout then `uv run alembic upgrade head`; verify: `SELECT conname FROM pg_constraint WHERE conname = 'features_status_check';` and `\d roadmap_curation_proposals` (psql port 5433).
- [ ] **Step 2: Restart the MCP** (new tool 42): `sudo systemctl restart brain-v42-mcp` (check the exact name: `systemctl list-units | grep brain`); confirm that `brain_feature_update` appears in the tool list of a fresh session.
- [ ] **Step 3: Restart the metrics sidecar** — MANDATORY, it is in no deploy loop (learning `f13144b3`): `sudo systemctl restart brain-metrics` then `curl -s http://127.0.0.1:9200/metrics | jq '.dream.last_run.phases'` → every phase carries `dry_run`; a night with a re-run shows `done`, not `partial` (criterion §12.5).
- [ ] **Step 4: Purge the stock** — `uv run python -m scripts.roadmap_purge` (dry) → read the per-project report; **decide the `red` case (117 features)**: if `red` comes out as R1 it's a ghost key (purge OK), if spared by the group it's a legacy key (decision to log via `brain_log_decision`); then `--wet`; verify criterion §12.1: `SELECT COUNT(*) FROM features WHERE status NOT IN ('done','archived') AND merged_into IS NULL;` ≤ 150.
- [ ] **Step 5: Open the ROADMAP killswitch (dry)** — systemd drop-in ONLY (incident 2026-06-30, never in the unit): edit `/etc/systemd/system/brain-v42-dream.service.d/killswitches.conf`, add `Environment="BRAIN_DREAM_ROADMAP_ENABLED=true"` and `Environment="BRAIN_DREAM_ROADMAP_DRY_RUN=true"`, then `sudo systemctl daemon-reload`. Verify the next morning: briefing → `ROADMAP: enabled (dry · …)` line, log `${TIMESTAMP}_roadmap.log` present.
- [ ] **Step 6: Rollout** — ≥2 clean dry nights → review the proposals in the morning: `SELECT id, op, feature_id, payload, rationale FROM roadmap_curation_proposals WHERE status='proposed' ORDER BY id;` → `uv run python -m scripts.roadmap_curate --apply-ids "…"` for the good ones, `UPDATE roadmap_curation_proposals SET status='rejected' WHERE id IN (…);` for the bad ones. When the acceptance rate is stable and high: consider `BRAIN_DREAM_ROADMAP_DRY_RUN=false` — and read what that arms before doing it: since 2026-09-02 `WET_APPLYABLE_OPS` is `archive` and `status`, so the flip lets the nightly apply those two without review, and leaves `merge` and `rename` under review. Measured on 2026-09-02: 185 proposals are pending, of which 45 would apply under this scope and 185 under the aggressive regime that ran until that day. Evidence against flipping today: on 2026-09-02 human review rejected 14 brain-v42 proposals out of 14, all title rewrites (decision `892c1491`); the standing decision is to stay DRY.
- [ ] **Step 7: Brain loop** — `brain_update_project_focus`: new state; first real `brain_feature_update` on a delivered feature (criterion §12.4).

---

## Spec → tasks coverage

| Spec § | Task |
|--------|------|
| §1 migration 030 | 1 |
| §2 mechanical purge | 2 (+10.4 real run) |
| §3 curator propose + guardrails | 3 |
| §3 apply + post-conditions | 4 |
| §4 dream step + killswitches + briefing line | 5 (+10.5-6 rollout) |
| §5 Roadmap briefing section | 6 |
| §6 brain_feature_update | 7 |
| §7 sidecar dedup + dry_run + restart | 8 (+10.3 restart) |
| §8 GitLab v1 successor (artifact content in the prompt) | 3 (digest + prompt status) — v2 out of scope |
| §9 non-goals | no ClusterGuard/front/gitlab_poll task ✓ |
| §12 success criteria | 10 (live checks) |
| §13 TDD breakdown | tasks 1-9 = blocks 1-9 |

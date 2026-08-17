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
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": feature_ids},
            )
            archived = (
                await session.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM features WHERE id = ANY(CAST(:ids AS uuid[])) AND status = 'archived'"
                    ),
                    {"ids": feature_ids},
                )
            ).scalar_one()
            if archived != len(feature_ids):
                raise RuntimeError(f"post-condition failed: {archived}/{len(feature_ids)} archived")
    return int(archived)


async def _run(wet: bool) -> int:
    from brain_v42.db.engine import get_session_factory  # noqa: PLC0415

    sf = get_session_factory()
    now = datetime.now(tz=UTC)

    known_keys = await fetch_known_keys(sf)
    candidates = await fetch_candidates(sf)

    classified = [(f, rule) for f in candidates if (rule := classify_feature(f, known_keys, now))]
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

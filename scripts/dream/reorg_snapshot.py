"""Snapshot the tags of a project's corpus, immediately before the REORG phase.

REORG Part 1 normalises metadata — in practice, ``tags``. Proving that a claimed
update actually landed therefore needs a *before* to compare against, and the
before has to be MEASURED: every derived substitute is hollow.

  - ``updated_at`` moves on its own. ``DecayFlusher`` issues bulk UPDATEs on
    ``learnings`` and ``decisions`` every 300 s, and the ``update_updated_at()``
    trigger from migration 001 carries no ``WHEN`` clause. Worse, the access rows
    that drive the flusher are produced by REORG's own ``brain_get`` reads — the
    phase manufactured the very evidence it was being judged on.
  - ``content_updated_at`` (migration 041) is trigger-written, but its triggers
    are declared ``BEFORE UPDATE OF topic, insight`` and
    ``BEFORE UPDATE OF title, description, reasoning, consequences``. ``tags`` is
    not in either list, so the column stays NULL on exactly the rows REORG mutates.

Written to disk beside the phase log so an out-of-band replay reads the same
before as the night did.

CLI:
    python -m scripts.dream.reorg_snapshot --project-key brain-v42 > before.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from brain_v42.config import Settings
from brain_v42.db.tables import decisions, learnings

#: The two tables REORG is allowed to mutate, per phase_reorg.md. Kept in sync
#: with ``reorg_validate._entity_row``, which searches the same pair.
_TABLES = (learnings, decisions)


async def snapshot_tags(
    session_factory: async_sessionmaker[AsyncSession],
    project_key: str,
) -> dict[str, list[str]]:
    """Return ``{entity_id: tags}`` for every learning and decision of a project.

    An entity with no tags is present with an empty list. ``[]`` and *absent*
    must stay distinct facts: conflating them would read an untagged entity —
    the most common shape in the corpus REORG exists to normalise — as one
    created during the phase.
    """
    taken: dict[str, list[str]] = {}
    async with session_factory() as session:
        for table in _TABLES:
            rows = (
                await session.execute(
                    sa.select(table.c.id, table.c.tags).where(table.c.project_key == project_key)
                )
            ).all()
            for entity_id, tags in rows:
                taken[str(entity_id)] = list(tags or [])
    return taken


def _build_factory(postgres_url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(postgres_url, pool_pre_ping=True)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _amain(project_key: str, session_factory: async_sessionmaker[AsyncSession]) -> int:
    taken = await snapshot_tags(session_factory, project_key)
    json.dump(taken, sys.stdout)
    sys.stdout.write("\n")
    print(
        f"REORG SNAPSHOT: {len(taken)} entities for project {project_key!r}",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-key",
        required=True,
        help="Project whose corpus to snapshot — required, deliberately without a default",
    )
    args = parser.parse_args(argv)

    session_factory = _build_factory(Settings().postgres_url)
    return asyncio.run(_amain(args.project_key, session_factory))


if __name__ == "__main__":
    sys.exit(main())

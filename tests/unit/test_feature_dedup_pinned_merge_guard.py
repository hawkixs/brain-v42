"""`merge_features` must refuse to absorb a PINNED feature.

SIBLING FILE: `test_feature_dedup_pinned_guard.py` already covers the same rule
on `find_candidates`. The two are not duplicates — they guard two different
moments, and that is the whole subject here.

Ticket `4a6fe67e` targeted "the dedup applies oldest-absorbs-newest with no guard
on `pinned`". That is no longer true to the letter: `find_candidates` carries that
guard from its sibling file. But it lives in the DISCOVERY path, not in the
MUTATION path, and `merge_features` — the only one that writes — did not carry it.

Two ways of absorbing a pinned feature therefore remained:

1. TOCTOU. `run_dedup_loop` collects ALL of a project's candidates, then merges
   them one by one, each in its own session and after a reranker round-trip. A
   human who pins a feature during that window sees their gesture ignored: the
   decision was taken on an earlier snapshot.
2. A direct call. `merge_features` is public and the module's docstring documents
   it as such. A caller that does not go through `find_candidates` inherits no
   guard.

The guard must therefore read `pinned` on the FOR UPDATE row — the only
authority — and not on the snapshot passed as an argument. Test 4 proves it: an
unpinned snapshot, a pinned authoritative row, the merge must be refused.

NEGATIVE WITNESS, in this file and nowhere else: `test_unpinned_source_still_merges`
and `test_pinned_target_still_absorbs`. Without them, an over-broad guard would
disable the dedup and the suite would stay green — we would have "protected" the
pinned features by breaking the functionality.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.services.feature_dedup_job import FeatureDedupJob


def _row(
    *,
    name: str,
    pinned: bool,
    feature_id: uuid.UUID | None = None,
    description: str = "desc",
) -> MagicMock:
    """A mocked features row.

    `pinned` is MANDATORY and has no default: a MagicMock's default attribute is
    truthy, so a row built without setting it would simulate a pinned feature
    without saying so. That is exactly the trap that would make this file green for
    the wrong reason.
    """
    row = MagicMock()
    row.id = feature_id or uuid.uuid4()
    row.name = name
    row.description = description
    row.embedding = [0.1] * 1536
    row.created_at = 1000.0
    row.status = "research"
    row.merged_into = None
    row.pinned = pinned
    return row


def _job() -> tuple[FeatureDedupJob, AsyncMock]:
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    embedding_svc = AsyncMock()
    embedding_svc.embed = AsyncMock(return_value=[0.5] * 1536)
    reranker = AsyncMock()
    reranker.rerank = AsyncMock(return_value=[0.9])

    job = FeatureDedupJob(
        session_factory=factory,
        reranker=reranker,
        embedding_svc=embedding_svc,
    )
    return job, session


def _wire_recheck(session: AsyncMock, rows: list[MagicMock]) -> None:
    """The first execution is the SELECT … FOR UPDATE; the later ones are DML."""
    recheck = MagicMock()
    recheck.fetchall.return_value = rows
    session.execute = AsyncMock(side_effect=[recheck] + [MagicMock() for _ in range(8)])


def _wrote_anything(session: AsyncMock) -> bool:
    """True as soon as a statement other than the SELECT FOR UPDATE has left."""
    return session.execute.await_count > 1


class TestPinnedSourceIsNeverAbsorbed:
    @pytest.mark.asyncio
    async def test_pinned_source_is_refused(self) -> None:
        """The ticket's case: the NEW one is pinned, the older would absorb it.

        `source` is the row that DISAPPEARS (status='archived', merged_into=target).
        Pinning is the gesture by which a human says "do not touch this": the merge
        must be refused, not executed.
        """
        job, session = _job()
        target = _row(name="Ancienne", pinned=False)
        source = _row(name="Épinglée par un humain", pinned=True)
        _wire_recheck(session, [target, source])

        result = await job.merge_features(session, target, source)

        assert result is False, (
            f"merge_features a rendu {result!r} sur une source ÉPINGLÉE — "
            "elle vient d'archiver un engagement explicite de l'opérateur"
        )
        assert not _wrote_anything(session), (
            "aucune écriture ne doit partir quand la source est épinglée ; "
            f"{session.execute.await_count - 1} instruction(s) ont été exécutées "
            "après le SELECT FOR UPDATE"
        )

    @pytest.mark.asyncio
    async def test_both_pinned_is_refused(self) -> None:
        """BOTH pinned: we block, we do not guess.

        A case surfaced explicitly rather than arbitrated in the code: nothing says
        which of the two human intentions should give way.
        """
        job, session = _job()
        target = _row(name="Ancienne épinglée", pinned=True)
        source = _row(name="Nouvelle épinglée", pinned=True)
        _wire_recheck(session, [target, source])

        result = await job.merge_features(session, target, source)

        assert result is False, (
            f"merge_features a rendu {result!r} alors que les DEUX sont épinglées"
        )
        assert not _wrote_anything(session)

    @pytest.mark.asyncio
    async def test_pinned_read_from_authoritative_row_not_snapshot(self) -> None:
        """TOCTOU: the snapshot says "not pinned", the database says "pinned".

        This is `run_dedup_loop`'s real window — candidates are collected in bulk,
        then merged one by one. A guard reading the argument would pass here, and
        the human's gesture would be lost.
        """
        job, session = _job()
        source_id = uuid.uuid4()
        target = _row(name="Ancienne", pinned=False)

        # Snapshot taken BEFORE the human pins.
        stale_snapshot = _row(name="Nouvelle", pinned=False, feature_id=source_id)
        # Authoritative row re-read FOR UPDATE, AFTER the pinning.
        authoritative = _row(name="Nouvelle", pinned=True, feature_id=source_id)

        _wire_recheck(session, [target, authoritative])

        result = await job.merge_features(session, target, stale_snapshot)

        assert result is False, (
            f"merge_features a rendu {result!r} : la garde a cru l'instantané "
            "plutôt que la ligne FOR UPDATE, donc elle ne ferme pas la fenêtre TOCTOU"
        )
        assert not _wrote_anything(session)


class TestDedupStillWorks:
    """Negative witness — without it, an over-broad guard would pass for a success."""

    @pytest.mark.asyncio
    async def test_unpinned_source_still_merges(self) -> None:
        """The nominal case must keep being deduplicated."""
        job, session = _job()
        target = _row(name="Ancienne", pinned=False)
        source = _row(name="Nouvelle", pinned=False)
        _wire_recheck(session, [target, source])

        result = await job.merge_features(session, target, source)

        assert result is True, (
            f"merge_features a rendu {result!r} sur une paire NON épinglée — "
            "le dedup a été désactivé, pas gardé"
        )
        assert _wrote_anything(session), (
            "une fusion nominale doit émettre du DML après le SELECT FOR UPDATE"
        )

    @pytest.mark.asyncio
    async def test_pinned_target_still_absorbs(self) -> None:
        """A pinned feature as TARGET stays allowed: it SURVIVES the merge.

        Forbidding this case would protect the pinning by preventing precisely what
        it asks for — that this feature stays.
        """
        job, session = _job()
        target = _row(name="Ancienne ÉPINGLÉE", pinned=True)
        source = _row(name="Nouvelle banale", pinned=False)
        _wire_recheck(session, [target, source])

        result = await job.merge_features(session, target, source)

        assert result is True, (
            f"merge_features a rendu {result!r} alors que seule la CIBLE est "
            "épinglée — la cible survit, il n'y a rien à protéger ici"
        )
        assert _wrote_anything(session)

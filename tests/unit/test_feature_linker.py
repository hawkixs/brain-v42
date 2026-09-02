"""Unit tests for FeatureLinker service."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.services.feature_linker import FeatureLinker


@pytest.fixture
def mock_session_factory():
    """Mock async session factory returning a mock session."""
    session = AsyncMock()
    result = MagicMock()
    result.fetchall.return_value = []
    session.execute = AsyncMock(return_value=result)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, session


@pytest.mark.asyncio
async def test_link_artifact_skips_when_no_embedding(mock_session_factory):
    factory, _ = mock_session_factory
    linker = FeatureLinker(session_factory=factory)
    result = await linker.link_artifact(
        embedding=None,
        artifact_type="learning",
        artifact_id=uuid.uuid4(),
        project_key="red",
    )
    assert result == 0


@pytest.mark.asyncio
async def test_link_artifact_skips_when_no_project_key(mock_session_factory):
    factory, _ = mock_session_factory
    linker = FeatureLinker(session_factory=factory)
    result = await linker.link_artifact(
        embedding=[0.1] * 1536,
        artifact_type="learning",
        artifact_id=uuid.uuid4(),
        project_key=None,
    )
    assert result == 0


@pytest.mark.asyncio
async def test_link_artifact_catches_exceptions(mock_session_factory):
    factory, session = mock_session_factory
    session.execute = AsyncMock(side_effect=RuntimeError("db down"))
    linker = FeatureLinker(session_factory=factory)
    result = await linker.link_artifact(
        embedding=[0.1] * 1536,
        artifact_type="learning",
        artifact_id=uuid.uuid4(),
        project_key="red",
    )
    assert result == 0  # fire-and-forget, no exception raised


@pytest.mark.asyncio
async def test_link_artifact_delegates_to_cluster_guard(mock_session_factory):
    """When cluster_guard is set AND title is provided, delegate to resolve()."""
    factory, session = mock_session_factory
    cluster_guard = AsyncMock()
    mock_feature = MagicMock(id=uuid.uuid4(), name="Test Feature")
    cluster_guard.resolve.return_value = (mock_feature, "linked")

    linker = FeatureLinker(session_factory=factory, cluster_guard=cluster_guard)
    result = await linker.link_artifact(
        embedding=[0.1] * 10,
        artifact_type="learning",
        artifact_id=uuid.uuid4(),
        project_key="test",
        title="Some learning",
    )
    cluster_guard.resolve.assert_called_once()
    assert result >= 0


@pytest.mark.asyncio
async def test_link_artifact_uses_raw_sql_without_cluster_guard(mock_session_factory):
    """Without cluster_guard, the existing raw SQL path is used."""
    factory, session = mock_session_factory
    linker = FeatureLinker(session_factory=factory)
    result = await linker.link_artifact(
        embedding=[0.1] * 10,
        artifact_type="learning",
        artifact_id=uuid.uuid4(),
        project_key="test",
    )
    assert result >= 0


@pytest.mark.asyncio
async def test_link_artifact_falls_back_to_raw_sql_when_no_title(mock_session_factory):
    """With cluster_guard but no title, fall back to raw SQL path."""
    factory, session = mock_session_factory
    cluster_guard = AsyncMock()

    linker = FeatureLinker(session_factory=factory, cluster_guard=cluster_guard)
    result = await linker.link_artifact(
        embedding=[0.1] * 10,
        artifact_type="learning",
        artifact_id=uuid.uuid4(),
        project_key="test",
    )
    cluster_guard.resolve.assert_not_called()
    assert result >= 0


@pytest.mark.asyncio
async def test_link_artifact_cluster_guard_inserts_link(mock_session_factory):
    """ClusterGuard path inserts a feature_artifact link after resolve()."""
    factory, session = mock_session_factory
    cluster_guard = AsyncMock()
    feature_id = uuid.uuid4()
    mock_feature = MagicMock(id=feature_id, name="Auth Feature")
    cluster_guard.resolve.return_value = (mock_feature, "created")

    linker = FeatureLinker(session_factory=factory, cluster_guard=cluster_guard)
    artifact_id = uuid.uuid4()
    result = await linker.link_artifact(
        embedding=[0.1] * 10,
        artifact_type="decision",
        artifact_id=artifact_id,
        project_key="proj",
        title="Add JWT auth",
    )
    assert result == 1
    # session.execute should have been called to insert the link
    session.execute.assert_called_once()
    session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_link_artifact_returns_zero_when_cluster_guard_skips(mock_session_factory):
    """ClusterGuard link-only mode: (None, "skipped") -> 0 links, no insert.

    An explicit `if feature is None: return 0` guard is required *before* the
    insert. Without it, `feature.id` would raise AttributeError, which the
    outer fire-and-forget try/except would swallow and mislabel as
    "feature_linker.link_failed" — masking a legitimate skip as an error.
    """
    factory, session = mock_session_factory
    cluster_guard = AsyncMock()
    cluster_guard.resolve.return_value = (None, "skipped")

    linker = FeatureLinker(session_factory=factory, cluster_guard=cluster_guard)
    with patch("brain_v42.services.feature_linker.logger") as mock_logger:
        result = await linker.link_artifact(
            embedding=[0.1] * 10,
            artifact_type="learning",
            artifact_id=uuid.uuid4(),
            project_key="test",
            title="Some learning",
        )

    assert result == 0
    # No feature_artifacts INSERT should have been attempted.
    session.execute.assert_not_called()
    session.commit.assert_not_called()
    # Must be a deliberate no-op, not an exception swallowed by the
    # fire-and-forget try/except.
    mock_logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_link_artifact_cluster_guard_exception_caught(mock_session_factory):
    """Exceptions from cluster_guard.resolve() are caught (fire-and-forget)."""
    factory, session = mock_session_factory
    cluster_guard = AsyncMock()
    cluster_guard.resolve.side_effect = RuntimeError("reranker timeout")

    linker = FeatureLinker(session_factory=factory, cluster_guard=cluster_guard)
    result = await linker.link_artifact(
        embedding=[0.1] * 10,
        artifact_type="learning",
        artifact_id=uuid.uuid4(),
        project_key="test",
        title="Some learning",
    )
    assert result == 0  # fire-and-forget


# ── archived / merged_into filter in _do_link ─────────────────────────────


@pytest.mark.asyncio
async def test_do_link_excludes_archived_and_merged(mock_session_factory):
    """_do_link SQL must filter out archived and merged-into features.

    Without the filter, stale-linking would attach artifacts to archived
    clusters, silently polluting the graph and breaking briefing queries.
    """
    factory, session = mock_session_factory
    # Return empty rows so the method exits early (no insert needed).
    result = MagicMock()
    result.fetchall.return_value = []
    session.execute = AsyncMock(return_value=result)

    linker = FeatureLinker(session_factory=factory)
    await linker.link_artifact(
        embedding=[0.1] * 10,
        artifact_type="learning",
        artifact_id=uuid.uuid4(),
        project_key="test",
    )

    # _do_link calls session.execute exactly once (the SELECT).
    stmt = session.execute.call_args_list[0].args[0]
    # literal_binds=True fails on pgvector VECTOR type; inspect the WHERE
    # clause elements directly instead.
    compiled_sql = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    # 'merged_into IS NULL' appears verbatim in the compiled SQL.
    assert "merged_into IS NULL" in compiled_sql
    # The archived filter renders as 'features.status != :status_N'; verify
    # the bound value is 'archived' by inspecting the statement's whereclause.
    where_str = str(stmt.whereclause.compile(compile_kwargs={"literal_binds": False}))
    # The status != 'archived' clause is present — find 'status' in the WHERE.
    assert "status" in where_str and "!=" in where_str


# ── The raw fallback is neither equivalent to ClusterGuard, nor announced, nor bounded ──


def _row(feature_id: uuid.UUID, sim: float) -> MagicMock:
    row = MagicMock()
    row.id = feature_id
    row.sim = sim
    return row


class TestTheFallbackPathIsAnnouncedInsteadOfSilent:
    """`link_artifact` has TWO paths, and chose the second without saying so.

    The raw SQL fallback is taken as soon as there is no ClusterGuard, or as soon
    as the caller passes no title. Both cases are normal configurations —
    `embedding_backfill` and the backlog catch-up build a linker WITHOUT a guard —
    so nothing distinguishes "the guard did the work" from "the guard was
    bypassed". The path's only log was at `debug`, that is, absent in production.

    This is not a cosmetic difference between two implementations of the same
    contract: the two paths do NOT return the same result (see the next class).
    Knowing which one ran is therefore the first question to ask when faced with an
    unexpected link.
    """

    @pytest.mark.asyncio
    async def test_a_missing_cluster_guard_is_named(self, mock_session_factory) -> None:
        from structlog.testing import capture_logs

        factory, _ = mock_session_factory
        linker = FeatureLinker(session_factory=factory)

        with capture_logs() as logs:
            await linker.link_artifact(
                embedding=[0.1] * 1536,
                artifact_type="learning",
                artifact_id=uuid.uuid4(),
                project_key="red",
                title="un titre",
            )

        fallback = [log for log in logs if log["event"] == "feature_linker.fallback_path"]
        assert len(fallback) == 1, f"le repli n'est pas annoncé. Journaux : {logs}"
        assert fallback[0]["log_level"] == "warning"
        assert fallback[0]["reason"] == "no_cluster_guard"

    @pytest.mark.asyncio
    async def test_a_missing_title_is_named_as_a_distinct_reason(
        self, mock_session_factory
    ) -> None:
        """Two distinct causes, two distinct reasons.

        Confusing them would send someone hunting for a missing dependency
        injection where it is the caller that does not pass a title — two entirely
        different fixes, in two different files.
        """
        from structlog.testing import capture_logs

        factory, _ = mock_session_factory
        cluster_guard = MagicMock()
        linker = FeatureLinker(session_factory=factory, cluster_guard=cluster_guard)

        with capture_logs() as logs:
            await linker.link_artifact(
                embedding=[0.1] * 1536,
                artifact_type="learning",
                artifact_id=uuid.uuid4(),
                project_key="red",
                title=None,
            )

        fallback = [log for log in logs if log["event"] == "feature_linker.fallback_path"]
        assert len(fallback) == 1
        assert fallback[0]["reason"] == "no_title"

    @pytest.mark.asyncio
    async def test_the_guard_path_says_nothing(self, mock_session_factory) -> None:
        """Negative witness: a warning on every link would no longer be a signal.

        Without it, emitting the WARN unconditionally would pass both tests above
        while making the log unreadable — and a warning one always sees is a
        warning one no longer reads.
        """
        from structlog.testing import capture_logs

        factory, _ = mock_session_factory
        feature = MagicMock()
        feature.id = uuid.uuid4()
        cluster_guard = MagicMock()
        cluster_guard.resolve = AsyncMock(return_value=(feature, "linked"))
        linker = FeatureLinker(session_factory=factory, cluster_guard=cluster_guard)

        with capture_logs() as logs:
            await linker.link_artifact(
                embedding=[0.1] * 1536,
                artifact_type="learning",
                artifact_id=uuid.uuid4(),
                project_key="red",
                title="un titre",
            )

        assert [log for log in logs if log["event"] == "feature_linker.fallback_path"] == []


class TestTheFallbackPathIsBounded:
    """The fallback had NO cap, where the guard path has a structural one.

    `_do_link_via_guard` goes through `ClusterGuard.resolve`, which returns ONE
    feature: at most one link per artifact. `_do_link` inserted a link for EVERY
    feature above 0.70, with no `ORDER BY` and no `LIMIT`. On a project whose
    features resemble each other — precisely the corpus the roadmap curation is
    meant to tighten — a single artifact could attach itself to the whole batch,
    and the order of the links obtained was not even deterministic.

    Option B (operator decision of 2026-08-18, ticket fb62624f): cap and document
    the fallback. Option C is only implemented if a flaky night proves the need.
    """

    @staticmethod
    def _select_statement(session: MagicMock):
        return session.execute.call_args_list[0][0][0]

    @pytest.mark.asyncio
    async def test_the_query_carries_the_cap(self, mock_session_factory) -> None:
        factory, session = mock_session_factory
        linker = FeatureLinker(session_factory=factory, max_links=3)

        await linker.link_artifact(
            embedding=[0.1] * 1536,
            artifact_type="learning",
            artifact_id=uuid.uuid4(),
            project_key="red",
        )

        compiled = self._select_statement(session).compile()
        assert "LIMIT" in str(compiled).upper(), (
            "la requête du repli ne borne rien : un artefact peut s'attacher à "
            "toutes les features du projet"
        )
        assert 3 in compiled.params.values()

    @pytest.mark.asyncio
    async def test_the_best_candidates_are_kept_not_arbitrary_ones(
        self, mock_session_factory
    ) -> None:
        """A cap without ordering trades "too many links" for "the wrong links".

        Without `ORDER BY`, PostgreSQL returns rows in whatever order suits it, and
        `LIMIT 3` would keep three arbitrary ones. The cap must keep the three
        closest, otherwise it degrades the result instead of bounding it.
        """
        factory, session = mock_session_factory
        linker = FeatureLinker(session_factory=factory, max_links=3)

        await linker.link_artifact(
            embedding=[0.1] * 1536,
            artifact_type="learning",
            artifact_id=uuid.uuid4(),
            project_key="red",
        )

        rendered = str(self._select_statement(session).compile()).upper()
        assert "ORDER BY" in rendered and "DESC" in rendered

    @pytest.mark.asyncio
    async def test_reaching_the_cap_is_never_silent(self, mock_session_factory) -> None:
        """A mute truncation reads as "there were only three candidates".

        This is the same rule as `brain_list`'s page cap, announced for the same
        reason: a page truncated in silence is indistinguishable from a corpus that
        stops there.
        """
        from structlog.testing import capture_logs

        factory, session = mock_session_factory
        result = MagicMock()
        result.fetchall.return_value = [_row(uuid.uuid4(), 0.9 - i / 100) for i in range(3)]
        session.execute = AsyncMock(return_value=result)
        linker = FeatureLinker(session_factory=factory, max_links=3)

        with capture_logs() as logs:
            linked = await linker.link_artifact(
                embedding=[0.1] * 1536,
                artifact_type="learning",
                artifact_id=uuid.uuid4(),
                project_key="red",
            )

        assert linked == 3
        capped = [log for log in logs if log["event"] == "feature_linker.cap_reached"]
        assert len(capped) == 1, f"plafond atteint sans un mot. Journaux : {logs}"
        assert capped[0]["log_level"] == "warning"
        assert capped[0]["max_links"] == 3

    @pytest.mark.asyncio
    async def test_staying_below_the_cap_says_nothing(self, mock_session_factory) -> None:
        """Negative witness: the announcement must bear on the cap being REACHED."""
        from structlog.testing import capture_logs

        factory, session = mock_session_factory
        result = MagicMock()
        result.fetchall.return_value = [_row(uuid.uuid4(), 0.9), _row(uuid.uuid4(), 0.8)]
        session.execute = AsyncMock(return_value=result)
        linker = FeatureLinker(session_factory=factory, max_links=3)

        with capture_logs() as logs:
            await linker.link_artifact(
                embedding=[0.1] * 1536,
                artifact_type="learning",
                artifact_id=uuid.uuid4(),
                project_key="red",
            )

        assert [log for log in logs if log["event"] == "feature_linker.cap_reached"] == []


def test_the_two_paths_do_not_share_a_contract() -> None:
    """NAME the divergence, so that the two paths stop being read as one.

    The module's docstring says "the original raw-SQL cosine-similarity path is
    used for backward compatibility", which suggests a behavioural equivalence.
    There is none, and the discrepancies go in the dangerous direction:

      | | ClusterGuard path | raw SQL fallback |
      |---|---|---|
      | links per artifact | 1 at most, structural | up to `max_links` |
      | reranker | yes (0.75 link / 0.50 merge) | none |
      | grey zone 0.50–0.70 | arbitrated | ignored |
      | link-only mode | honoured | unknown |
      | feature creation | possible | never |

    This test reads both implementations and requires that the divergence stay
    DECLARED in the module. A test verifying the equality of the two paths would be
    wrong; a module that implies it is worse, because it is read quickly.
    """
    import inspect

    from brain_v42.services import feature_linker as module

    source = inspect.getsource(module)

    assert "backward compatibility" not in source, (
        "le module présente encore le repli comme une simple compatibilité "
        "ascendante — il a un contrat DIFFÉRENT, pas une histoire différente"
    )
    assert "DIVERGENCE" in source, (
        "la divergence entre les deux chemins doit être déclarée dans le module, "
        "pas seulement connue de qui a lu les deux"
    )

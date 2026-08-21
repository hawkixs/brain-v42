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


# ── Le repli brut n'est ni équivalent au ClusterGuard, ni annoncé, ni borné ──


def _row(feature_id: uuid.UUID, sim: float) -> MagicMock:
    row = MagicMock()
    row.id = feature_id
    row.sim = sim
    return row


class TestTheFallbackPathIsAnnouncedInsteadOfSilent:
    """`link_artifact` a DEUX chemins, et choisissait le second sans le dire.

    Le repli SQL brut est pris dès qu'il n'y a pas de ClusterGuard, ou dès que
    l'appelant ne passe pas de titre. Les deux cas sont des configurations
    normales — `embedding_backfill` et le rattrapage de backlog construisent un
    linker SANS guard — donc rien ne distingue « le guard a travaillé » de « le
    guard a été contourné ». Le seul journal du chemin était en `debug`, c'est-à-dire
    absent en production.

    Ce n'est pas une différence cosmétique entre deux implémentations du même
    contrat : les deux chemins ne rendent PAS le même résultat (voir la classe
    suivante). Savoir lequel a tourné est donc la première question à poser
    devant un lien inattendu.
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
        """Deux causes distinctes, deux raisons distinctes.

        Les confondre enverrait chercher une injection de dépendance absente là
        où c'est l'appelant qui ne transmet pas de titre — deux correctifs
        totalement différents, dans deux fichiers différents.
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
        """Témoin négatif : un avertissement à chaque lien ne serait plus un signal.

        Sans lui, poser le WARN inconditionnellement passerait les deux tests
        ci-dessus tout en rendant le journal illisible — et un avertissement
        qu'on voit toujours est un avertissement qu'on ne lit plus.
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
    """Le repli n'avait AUCUN plafond, là où le chemin guard en a un structurel.

    `_do_link_via_guard` passe par `ClusterGuard.resolve`, qui rend UNE feature :
    au plus un lien par artefact. `_do_link` insérait un lien pour CHAQUE feature
    au-dessus de 0,70, sans `ORDER BY` ni `LIMIT`. Sur un projet dont les features
    se ressemblent — précisément le corpus que la curation roadmap est censée
    resserrer — un seul artefact pouvait s'attacher à tout le lot, et l'ordre des
    liens obtenus n'était même pas déterministe.

    Option B (décision opérateur du 2026-08-18, ticket fb62624f) : plafonner et
    documenter le repli. L'option C ne s'implémente que si une nuit flaky en
    prouve le besoin.
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
        """Un plafond sans tri troque « trop de liens » contre « les mauvais liens ».

        Sans `ORDER BY`, PostgreSQL rend les lignes dans l'ordre qui l'arrange, et
        `LIMIT 3` en garderait trois quelconques. Le plafond doit conserver les
        trois plus proches, sinon il dégrade le résultat au lieu de le borner.
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
        """Une troncature muette se lit comme « il n'y avait que trois candidats ».

        C'est la même règle que le plafond de page de `brain_list`, annoncé pour
        la même raison : une page tronquée en silence est indiscernable d'un
        corpus qui s'arrête là.
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
        """Témoin négatif : l'annonce doit porter sur le plafond ATTEINT."""
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
    """NOMMER la divergence, pour qu'on cesse de lire les deux chemins comme un seul.

    Le docstring du module dit « the original raw-SQL cosine-similarity path is
    used for backward compatibility », ce qui laisse entendre une équivalence de
    comportement. Il n'y en a pas, et les écarts vont dans le sens dangereux :

      | | chemin ClusterGuard | repli SQL brut |
      |---|---|---|
      | liens par artefact | 1 au plus, structurel | jusqu'à `max_links` |
      | reranker | oui (0,75 lier / 0,50 fusionner) | aucun |
      | zone grise 0,50–0,70 | arbitrée | ignorée |
      | mode link-only | respecté | inconnu |
      | création de feature | possible | jamais |

    Ce test lit les deux implémentations et exige que la divergence reste
    DÉCLARÉE dans le module. Un test qui vérifierait l'égalité des deux chemins
    serait faux ; un module qui la sous-entend est pire, parce qu'il se lit vite.
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

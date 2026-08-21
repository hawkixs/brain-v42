"""L'injection du middleware doit ATTEINDRE le SQL de brain_list, bout en bout.

Les deux moitiés sont déjà prouvées, chacune de son côté :

- `tests/unit/mcp/test_dream_project_authorization.py` prouve que le middleware
  INJECTE `project_key` dans les arguments de `brain_list` et refuse un
  `project_key` divergent ;
- `tests/integration/test_project_scoped_crud.py` prouve que les repos honorent un
  `project_key` qu'on leur PASSE explicitement.

Aucune ne prouve la jonction. Un remaniement de `brain_list` qui cesserait
d'honorer l'argument injecté — un `project_key` oublié dans l'appel au service,
un `canonicalize_project_key` qui l'écraserait — laisserait les DEUX suites
vertes, et la nuit REORG repaginerait le corpus entier en silence. C'est le point
le plus exposé du dispositif : `brain_list` est le SEUL outil CRUD qui n'appelle
jamais `get_dream_project_scope()` lui-même, donc sa borne vit entièrement dans le
middleware et n'a aucune redondance en aval.

Le témoin négatif est dans le test, pas à côté : la même page demandée SANS
l'autorisation doit rendre le corpus des deux projets. Sans lui, un `brain_list`
qui ne rendrait jamais rien passerait pour parfaitement borné.

Contre `brain_test`, jamais `brain` — les clés portent le préfixe `integ-` que la
purge de fin de session reconnaît.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.mcp.dream_project_authorization import (
    DreamProjectAudit,
    authorize_dream_project_request,
)
from brain_v42.models.learning import LearningCreate
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.services.learning_service import LearningService

pytestmark = pytest.mark.integration


class MockMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **_kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = fn
            return fn

        return decorator


class _UnusedResolver:
    """`brain_list` n'a pas de référence à résoudre — l'appeler serait un défaut."""

    async def references_belong_to_project(self, *_args: Any) -> bool:
        raise AssertionError("brain_list carries no reference to resolve")


def _other_services() -> dict[str, Any]:
    """Les quatre services que `brain_list` ne touche pas sur le chemin learning."""
    services: dict[str, Any] = {}
    for name in ("decision_svc", "snippet_svc", "runbook_svc", "adr_svc"):
        svc = MagicMock()
        svc.list_all = AsyncMock(return_value=[])
        svc.list_snippets = AsyncMock(return_value=[])
        svc.list_by_project = AsyncMock(return_value=[])
        services[name] = svc
    return services


@pytest.fixture
def brain_list(session_factory: async_sessionmaker[AsyncSession]) -> Any:
    """`brain_list` réel, câblé sur un LearningService réel et la vraie base."""
    from brain_v42.mcp.tools.crud_tools import register_crud_tools

    mcp = MockMCP()
    register_crud_tools(
        mcp,
        learning_svc=LearningService(PgLearningRepo(session_factory=session_factory)),
        session_factory=session_factory,
        **_other_services(),
    )
    return mcp.registered["brain_list"]


@pytest_asyncio.fixture
async def two_projects(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[str, str, str, str]:
    """Un learning dans le projet du run, un dans un projet voisin."""
    service = LearningService(PgLearningRepo(session_factory=session_factory))
    suffix = uuid.uuid4().hex[:10]
    mine_key = f"integ-scope-mine-{suffix}"
    theirs_key = f"integ-scope-theirs-{suffix}"
    mine_topic = f"scope-mine-{suffix}"
    theirs_topic = f"scope-theirs-{suffix}"

    await service.create(
        LearningCreate(
            topic=mine_topic,
            insight="corpus of the run project",
            project_key=mine_key,
            source_type="experience",
            confidence="high",
        )
    )
    await service.create(
        LearningCreate(
            topic=theirs_topic,
            insight="corpus of a neighbouring project",
            project_key=theirs_key,
            source_type="experience",
            confidence="high",
        )
    )
    return mine_key, mine_topic, theirs_key, theirs_topic


async def _authorized_arguments(project_key: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await authorize_dream_project_request(
        tool_name="brain_list",
        arguments=arguments,
        project_key=project_key,
        resolver=_UnusedResolver(),
        audit=DreamProjectAudit(principal="dream-codex-reorg", phase="reorg"),
    )
    return dict(result.arguments)


@pytest.mark.asyncio
async def test_an_injected_perimeter_actually_bounds_the_page(
    brain_list: Any, two_projects: tuple[str, str, str, str]
) -> None:
    """LE test de jonction : le middleware injecte, et le SQL en tient compte.

    La page demandée est celle de REORG Partie 1 — `summary_only`, sans
    `project_key`, parce que la phase n'en fournit jamais un elle-même.
    """
    mine_key, mine_topic, _, theirs_topic = two_projects
    page = {"entity_type": "learning", "limit": 100, "offset": 0, "summary_only": True}

    arguments = await _authorized_arguments(mine_key, page)
    assert arguments["project_key"] == mine_key, (
        "le middleware n'a rien injecté — le reste du test ne mesurerait plus rien"
    )

    output = await brain_list(**arguments)

    assert mine_topic in output, "le corpus du projet du run doit rester listable"
    assert theirs_topic not in output, (
        "un learning d'un autre projet est sorti d'une page BORNÉE par le "
        "middleware : l'injection n'atteint pas le SQL, et REORG repaginerait le "
        "corpus entier en silence"
    )


@pytest.mark.asyncio
async def test_the_same_page_unbounded_returns_both_projects(
    brain_list: Any, two_projects: tuple[str, str, str, str]
) -> None:
    """Témoin négatif, sans lequel le test ci-dessus ne prouve rien.

    Un `brain_list` cassé qui ne rendrait jamais rien satisferait l'assertion
    « le voisin n'apparaît pas ». Ce test-ci exige que le voisin SOIT visible
    quand rien ne borne la page — c'est ce qui rend son absence significative.
    """
    _, mine_topic, _, theirs_topic = two_projects

    output = await brain_list(entity_type="learning", limit=100, offset=0, summary_only=True)

    assert mine_topic in output
    assert theirs_topic in output, (
        "sans périmètre, les deux projets doivent apparaître; sinon l'isolement "
        "mesuré plus haut viendrait d'autre chose que du périmètre"
    )


@pytest.mark.asyncio
async def test_a_forged_perimeter_never_reaches_the_page(
    brain_list: Any, two_projects: tuple[str, str, str, str]
) -> None:
    """Le refus est en AMONT du SQL : l'appel n'a jamais lieu.

    Complète la jonction par l'autre bout — l'injection borne, et une tentative
    de la contredire ne descend pas jusqu'à la base.
    """
    from fastmcp.exceptions import AuthorizationError

    mine_key, _, theirs_key, _ = two_projects
    page = {"entity_type": "learning", "limit": 100, "project_key": theirs_key}

    with pytest.raises(AuthorizationError):
        await _authorized_arguments(mine_key, page)

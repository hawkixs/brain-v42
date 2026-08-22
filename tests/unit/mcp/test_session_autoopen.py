"""Auto-ouverture de session — synchrone, fail-open, mémoïsée par connexion.

Forme signée `ae0d0475` / ADR §0ter. Quatre propriétés, chacune avec son test :
écriture AVANT l'outil ; échec JAMAIS propagé ; une seule ouverture par appel
client malgré le double tir de `on_call_tool` en profil `compact` ; et rien du
tout en stdio, où il n'existe aucun identifiant de connexion (§0ter.2).
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from brain_v42.config import Settings
from brain_v42.mcp.provenance_middleware import ProvenanceMiddleware
from brain_v42.mcp.session_autoopen import (
    AutoOpenIdentity,
    SessionAutoOpener,
    get_session_autoopener,
    reset_session_autoopener,
)
from brain_v42.provenance import (
    UNEXPANDED_ACTOR,
    set_current_actor,
    set_current_transport,
)

_CONNECTION = "3f2b1a0c9d8e7f6a5b4c3d2e1f0a9b8c"
_OTHER_CONNECTION = "aaaa1111bbbb2222cccc3333dddd4444"
#: Ce que `normalize_agent` rend du `${PWD}` de ce dépôt : le basename.
_ACTOR = "brain_v42"
#: DSN jetable — `Settings` l'exige, rien ne s'y connecte dans ces tests.
_DSN = "postgresql+asyncpg://brain:brain@127.0.0.1:5433/brain_test"


def _context(tool_name: str = "brain_get") -> MagicMock:
    context = MagicMock()
    context.message.name = tool_name
    return context


def _headers(
    *,
    agent: str | None = "/home/hawixs/hawkixs_infra/git_repo/brain_v42",
    connection: str | None = _CONNECTION,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if agent is not None:
        headers["x-brain-agent"] = agent
    if connection is not None:
        headers["mcp-session-id"] = connection
    return headers


class _RecordingOpener:
    """Ouvreur de test : enregistre chaque identité, rend un UUID neuf."""

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.seen: list[AutoOpenIdentity] = []
        self._raises = raises

    async def __call__(self, identity: AutoOpenIdentity) -> UUID | None:
        self.seen.append(identity)
        if self._raises is not None:
            raise self._raises
        return uuid4()


@pytest.fixture(autouse=True)
def _isolate_autoopener() -> Iterator[None]:
    reset_session_autoopener()
    set_current_actor(_ACTOR)
    set_current_transport(_CONNECTION)
    yield
    reset_session_autoopener()
    set_current_transport(None)


class TestClosedByDefault:
    def test_flag_default_is_false(self) -> None:
        """Le drapeau est livré FERMÉ — exigence R3, pas une préférence."""
        assert Settings.model_fields["brain_session_auto_open_enabled"].default is False

    def test_getter_returns_none_when_flag_is_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Drapeau fermé ⇒ aucun ouvreur, donc le middleware ne fait rien.

        ``get_settings`` est ``lru_cache(maxsize=1)`` : sans neutraliser ce
        cache, ce test resterait VERT quel que soit le défaut du champ — il
        lirait les settings d'un appel antérieur. MESURÉ : la première
        rédaction de ce test survivait au retournement du défaut à ``True``,
        donc ne prouvait rien. La substitution ci-dessous et le sens INVERSE
        sont ce qui le rend mordant.
        """
        monkeypatch.setattr(
            "brain_v42.mcp.session_autoopen.get_settings",
            lambda: Settings(postgres_url=_DSN, brain_session_auto_open_enabled=False),
        )
        assert get_session_autoopener() is None

    def test_getter_builds_an_opener_when_flag_is_armed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sens inverse — sans lui, le test ci-dessus passerait sur du code mort."""
        monkeypatch.setattr(
            "brain_v42.mcp.session_autoopen.get_settings",
            lambda: Settings(postgres_url=_DSN, brain_session_auto_open_enabled=True),
        )
        assert isinstance(get_session_autoopener(), SessionAutoOpener)


class TestIdentityResolution:
    async def test_writes_agent_nature_and_connection_identity(self) -> None:
        opener = _RecordingOpener()
        assert await SessionAutoOpener(opener).ensure_open() is not None
        assert len(opener.seen) == 1
        identity = opener.seen[0]
        # `nature` est la SEULE colonne de la 046 au contrat public MCP ; les
        # quatre autres voyagent ici, dans l'identité interne.
        assert identity.nature == "agent"
        assert identity.connection_id == _CONNECTION
        assert identity.started_by_actor == _ACTOR
        # basename `brain_v42` -> clé canonique `brain-v42`.
        assert identity.project_key == "brain-v42"
        # `intent` NULL veut dire « pas mesuré », jamais « vide ».
        assert identity.intent is None

    async def test_stdio_opens_nothing(self) -> None:
        """§0ter.2 : PAS DE SESSION AUTOMATIQUE du tout en stdio."""
        set_current_transport(None)
        opener = _RecordingOpener()
        auto = SessionAutoOpener(opener)
        assert await auto.ensure_open() is None
        assert opener.seen == []
        assert auto.skipped["no_connection"] == 1

    async def test_unexpanded_actor_opens_nothing(self) -> None:
        """Sans acteur normalisable, aucun projet honnête : on n'invente pas."""
        set_current_actor(UNEXPANDED_ACTOR)
        opener = _RecordingOpener()
        auto = SessionAutoOpener(opener)
        assert await auto.ensure_open() is None
        assert opener.seen == []
        assert auto.skipped["no_actor"] == 1

    async def test_non_canonical_actor_opens_nothing(self) -> None:
        """Un acteur qui n'est pas une clé de projet valide ne devient pas un projet."""
        set_current_actor("Not A Project Key")
        opener = _RecordingOpener()
        auto = SessionAutoOpener(opener)
        assert await auto.ensure_open() is None
        assert opener.seen == []
        assert auto.skipped["no_project"] == 1


class TestIdempotence:
    async def test_memoized_per_connection(self) -> None:
        opener = _RecordingOpener()
        auto = SessionAutoOpener(opener)
        first = await auto.ensure_open()
        second = await auto.ensure_open()
        assert first is not None
        assert second == first
        assert len(opener.seen) == 1
        assert auto.memoized == 1

    async def test_distinct_connections_open_distinct_sessions(self) -> None:
        opener = _RecordingOpener()
        auto = SessionAutoOpener(opener)
        first = await auto.ensure_open()
        set_current_transport(_OTHER_CONNECTION)
        second = await auto.ensure_open()
        assert first != second
        assert [identity.connection_id for identity in opener.seen] == [
            _CONNECTION,
            _OTHER_CONNECTION,
        ]

    async def test_double_dispatch_opens_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Profil `compact` : `on_call_tool` tire DEUX fois par appel client.

        C'est `is_outermost_call()` qui rend l'auto-ouverture idempotente — la
        garde de profondeur, pas la mémo. Le témoin est donc un compteur
        d'APPELS à `ensure_open`, pas l'ouvreur : la mémo masquerait un second
        tir au lieu de prouver qu'il n'a pas eu lieu.
        """
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda **_kw: _headers(),
        )
        calls: list[str] = []

        class _Counting(SessionAutoOpener):
            async def ensure_open(self) -> UUID | None:
                calls.append("ensure_open")
                return None

        auto = _Counting(_RecordingOpener())
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_session_autoopener",
            lambda: auto,
        )
        middleware = ProvenanceMiddleware()

        async def inner(_ctx: object) -> str:
            return "inner"

        async def outer(ctx: object) -> str:
            # La passerelle `brain_call_tool` ré-entre dans la chaîne.
            return await middleware.on_call_tool(ctx, inner)

        assert await middleware.on_call_tool(_context(), outer) == "inner"
        assert calls == ["ensure_open"]


class TestSynchronousBeforeTheTool:
    async def test_session_exists_before_the_tool_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Le feu-et-oubli n'attribue rien : l'ouverture précède l'outil.

        Témoin d'ORDRE dans le journal partagé — si l'ouverture était
        asynchrone, `call_next` s'exécuterait avant elle.
        """
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda **_kw: _headers(),
        )
        order: list[str] = []

        async def opener(_identity: AutoOpenIdentity) -> UUID:
            order.append("open")
            return uuid4()

        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_session_autoopener",
            lambda: SessionAutoOpener(opener),
        )

        async def call_next(_ctx: object) -> str:
            order.append("tool")
            return "ok"

        assert await ProvenanceMiddleware().on_call_tool(_context(), call_next) == "ok"
        assert order == ["open", "tool"]


class TestFailOpen:
    async def test_open_failure_never_breaks_the_tool_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-open : l'appel passe quand même. Avec TÉMOIN NÉGATIF.

        Le témoin est indispensable : un test qui n'observe que « l'appel a
        réussi » resterait vert si l'auto-ouverture n'était jamais tentée. On
        prouve donc les DEUX sens dans le même test — l'ouvreur a bien été
        appelé et a bien levé, et l'appel d'outil a quand même rendu sa valeur.
        """
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda **_kw: _headers(),
        )
        boom = _RecordingOpener(raises=RuntimeError("database is down"))
        auto = SessionAutoOpener(boom)
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_session_autoopener",
            lambda: auto,
        )

        async def call_next(_ctx: object) -> str:
            return "ok"

        result = await ProvenanceMiddleware().on_call_tool(_context(), call_next)

        assert result == "ok"  # l'appel passe
        assert len(boom.seen) == 1  # TÉMOIN NÉGATIF : l'ouverture a bien été TENTÉE
        assert auto.failed == 1  # et elle a bien ÉCHOUÉ

    async def test_failure_is_not_memoized(self) -> None:
        """Un échec ne pose pas de mémo : sinon la connexion perdrait sa session à vie."""
        boom = _RecordingOpener(raises=RuntimeError("transient"))
        auto = SessionAutoOpener(boom)
        await auto.ensure_open()
        await auto.ensure_open()
        assert len(boom.seen) == 2
        assert auto.memoized == 0

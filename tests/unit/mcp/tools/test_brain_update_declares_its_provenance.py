"""Steps 1 and 2 of `55a21fb8`: `brain_update` stops writing a silent transition.

`brain_update` writes `freshness_status` **without ever naming the column** — it
arrives through the Pydantic update model. No `grep` on the column name can see
it, and that is how it escaped a first survey. The 043 trigger then resets
`freshness_source` to `NULL`: the transition is dated, but orphaned.

Measured in production on 2026-08-22, over the real twelve-day window: **3 silent
transitions out of 44**, all on `learnings`, all towards `archived`, 68 ms apart,
in a project whose night carried a WET `reorg` run. `brain_update` is the ONLY
write the server allowlist grants REORG.

The value used is `judgment`: 043's `CHECK` already allows it and **no code was
writing it** — reserved, unused, and it describes exactly what REORG does. No
migration, hence nothing in the signed corridor. Exercised for the FIRST time on
2026-08-22 against `brain_test`, transaction rolled back: accepted, with the date
stamped; and a value outside the vocabulary refused by the same constraint.

**What step 1 deliberately did NOT do**: it dried up only the KNOWN source. A
human write stayed silent — hence still SEEN by step 0's counter. Drying
everything at once would have removed the signal along with the noise, and the
next unsurveyed source would have slipped through with nothing moving.

**STEP 2 (2026-09-02) takes the ruling 049 left open**, and it can because the
fact step 1 lacked now exists: `SYSTEM_ACTOR_NAMES` surveys the machine actors,
per call site, dated 2026-08-29. A write from a HUMAN actor outside the dream
declares `manual_update`; a write from a SURVEYED machine actor still declares
nothing. The counter therefore does not go quiet — it gets SHARPER: a mute
transition stops being ambiguous between "a human" and "a source nobody
surveyed", and comes to mean only the second.

Measured read-only on production the same day, before the change: 44 dated
transitions, **4 mute** (3 learnings, 1 snippet), `score` the only source
actually written on the five knowledge tables. `judgment` has never appeared —
REORG runs DRY.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.mcp.dream_project_authorization import (
    DreamProjectAudit,
    DreamProjectScope,
    bind_dream_project_scope,
)
from brain_v42.provenance import SYSTEM_ACTOR_NAMES, get_current_actor, set_current_actor
from tests.unit.mcp._tool_error_adapter import capture_tool_errors

PROJECT_KEY = "reorg-owned"

#: The closed vocabulary of 043's `CHECK`. `judgment` was already in it.
FRESHNESS_SOURCES = ("merge", "judgment", "score", "revive")


class MockMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **_kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = capture_tool_errors(fn)
            return fn

        return decorator


class UnusedResolver:
    async def references_belong_to_project(self, *_args: Any) -> bool:
        raise AssertionError("point-of-use scoping must not rerun middleware resolution")


@contextmanager
def bind_actor(actor: str) -> Iterator[None]:
    """Set the request actor the way ``ProvenanceMiddleware`` does, then restore it.

    The tests must not hand the provenance to the code under test; they set the
    contextvar the middleware sets and let the tool resolve what it wants from it.
    """
    previous = get_current_actor()
    set_current_actor(actor)
    try:
        yield
    finally:
        set_current_actor(previous)


def _scope() -> DreamProjectScope:
    return DreamProjectScope(
        project_key=PROJECT_KEY,
        resolver=UnusedResolver(),
        audit=DreamProjectAudit(principal="dream-codex-reorg", phase="reorg"),
        tool_name="brain_update",
    )


def _registered_tools() -> tuple[dict[str, Any], dict[str, Any]]:
    from brain_v42.mcp.tools.crud_tools import register_crud_tools

    mcp = MockMCP()
    services: dict[str, Any] = {}
    for name in ("decision_svc", "learning_svc", "snippet_svc", "runbook_svc", "adr_svc"):
        svc = MagicMock()
        svc.update = AsyncMock(return_value=None)
        svc.get_by_id = AsyncMock(return_value=None)
        svc.resolve_id_prefix = AsyncMock(return_value=[])
        services[name] = svc
    session = AsyncMock()
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    optional = (
        {"access_logger": MagicMock()}
        if "access_logger" in inspect.signature(register_crud_tools).parameters
        else {}
    )
    register_crud_tools(
        mcp, **services, session_factory=MagicMock(return_value=context), **optional
    )
    return mcp.registered, services


def _sent_model(services: dict[str, Any], entity: str = "learning") -> Any:
    call = services[f"{entity}_svc"].update.await_args
    assert call is not None, "le service doit avoir été appelé"
    return call.args[1]


@pytest.mark.asyncio
async def test_a_scoped_freshness_write_declares_judgment() -> None:
    """The positive witness: REORG declares, so the transition stops being silent."""
    tools, services = _registered_tools()

    with bind_dream_project_scope(_scope()):
        await tools["brain_update"]("learning", str(uuid4()), {"freshness_status": "archived"})

    assert _sent_model(services).freshness_source == "judgment"


@pytest.mark.asyncio
async def test_an_unrelated_scoped_write_declares_nothing() -> None:
    """A FALSE provenance is worse than a missing one — the trigger says so itself.

    Stamping every scoped write would describe a transition that did not happen.
    The mark follows `freshness_status` only.
    """
    tools, services = _registered_tools()

    with bind_dream_project_scope(_scope()):
        await tools["brain_update"]("learning", str(uuid4()), {"topic": "renommé"})

    assert _sent_model(services).freshness_source is None


@pytest.mark.asyncio
async def test_a_human_write_declares_manual_update() -> None:
    """Step 2 — and it REPLACES the step-1 pin that lived here.

    That pin asserted `freshness_source is None` for a human write, and it was
    right for step 1: drying every source at once would have removed the signal
    with the noise while the machine-actor census did not exist yet. It does now
    (`SYSTEM_ACTOR_NAMES`, surveyed per call site on 2026-08-29), which is the fact
    that makes this step possible and did not exist before.

    049 pre-admitted the word for exactly this moment — "``manual_update``
    (reserved, unused […] the ruling on stamping human writes stays open and will
    find the word already admitted)".
    """
    tools, services = _registered_tools()

    with bind_actor("red-lab"):
        await tools["brain_update"]("learning", str(uuid4()), {"freshness_status": "archived"})

    assert _sent_model(services).freshness_source == "manual_update"


@pytest.mark.asyncio
async def test_an_unrelated_human_write_declares_nothing() -> None:
    """Same bound as the scoped branch: the mark follows `freshness_status` alone.

    Stamping a write that does not touch the status would describe a transition
    that never happened — the false provenance 043 warns about.
    """
    tools, services = _registered_tools()

    with bind_actor("red-lab"):
        await tools["brain_update"]("learning", str(uuid4()), {"topic": "renamed"})

    assert _sent_model(services).freshness_source is None


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", sorted(SYSTEM_ACTOR_NAMES)[:2] + ["dream-codex-reorg", "unknown"])
async def test_a_surveyed_machine_write_stays_mute_and_therefore_visible(actor: str) -> None:
    """THE witness that keeps step 0's counter meaningful.

    `manual_update` says *manual*. Stamping it for a surveyed bot would be a FALSE
    provenance — believed, where a missing one is seen — and 043 names that as the
    worse of the two. A machine write outside the dream therefore stays mute, so it
    keeps being counted by `fetch_mute_transitions` and keeps pointing at a source
    nobody has surveyed.

    The classifier is `is_human_actor`, the SAME one the Q1 unarchival guard
    already trusts for a heavier decision. A second, weaker rule here would be the
    inconsistency.
    """
    tools, services = _registered_tools()

    with bind_actor(actor):
        await tools["brain_update"]("learning", str(uuid4()), {"freshness_status": "archived"})

    assert _sent_model(services).freshness_source is None


@pytest.mark.asyncio
@pytest.mark.parametrize("entity", ["decision", "learning", "snippet", "runbook", "adr"])
async def test_every_mutable_type_declares_the_human_write(entity: str) -> None:
    """The five types the generic tool can write, not only `learning`."""
    tools, services = _registered_tools()

    with bind_actor("red-lab"):
        await tools["brain_update"](entity, str(uuid4()), {"freshness_status": "archived"})

    assert _sent_model(services, entity).freshness_source == "manual_update"


def test_the_human_value_belongs_to_the_049_vocabulary() -> None:
    """049 widened the CHECK; a word outside it would fail at the constraint."""
    from brain_v42.mcp.tools import crud_tools

    assert crud_tools.HUMAN_FRESHNESS_SOURCE == "manual_update"

    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "049_dream_run_series_and_freshness_vocabulary.py"
    ).read_text(encoding="utf-8")
    assert f'"{crud_tools.HUMAN_FRESHNESS_SOURCE}"' in migration


@pytest.mark.asyncio
@pytest.mark.parametrize("forged", ["judgment", "score", "revive", "merge"])
async def test_a_caller_may_never_forge_its_own_provenance(forged: str) -> None:
    """The provenance is set by the SERVER or not at all.

    A caller able to write it could sign a transition in someone else's name: "a
    false provenance, which is believed, instead of a missing provenance, which is
    seen" — 043's own words.
    """
    tools, services = _registered_tools()

    with bind_dream_project_scope(_scope()):
        result = await tools["brain_update"](
            "learning",
            str(uuid4()),
            {"freshness_status": "archived", "freshness_source": forged},
        )

    assert "freshness_source" in result
    services["learning_svc"].update.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("entity", ["decision", "learning", "snippet", "runbook", "adr"])
async def test_every_mutable_type_declares(entity: str) -> None:
    """The five types `brain_update` can write, not only `learning`.

    The three measured transitions were on `learnings`; nothing guarantees the
    next one will be.
    """
    tools, services = _registered_tools()

    with bind_dream_project_scope(_scope()):
        await tools["brain_update"](entity, str(uuid4()), {"freshness_status": "archived"})

    assert _sent_model(services, entity).freshness_source == "judgment"


def test_the_stamped_value_belongs_to_the_043_vocabulary() -> None:
    """Without this, step 1 would fail on a constraint, and the whole night with it.

    Also verified against the real database: `judgment` accepted, `reorg` refused.
    """
    from brain_v42.mcp.tools import crud_tools

    assert crud_tools.DREAM_FRESHNESS_SOURCE in FRESHNESS_SOURCES

    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "043_freshness_status_clock.py"
    ).read_text(encoding="utf-8")
    declared = set(re.findall(r"_SOURCES[^)]*?\)", migration, flags=re.S))
    assert declared, "la migration 043 doit déclarer son vocabulaire"
    assert crud_tools.DREAM_FRESHNESS_SOURCE in " ".join(declared), (
        "la valeur estampillée doit venir du vocabulaire de la 043, pas d'un littéral parallèle"
    )

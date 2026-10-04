"""A slot-writing integration module must run on the private head database.

Slot, slot-history and slot-anchor rows can never be deleted (no delete path, RESTRICT keys),
and migration 060's downgrade refuses while any exists. A `tests/integration/db` module that
writes slots through the shared `session_factory` therefore breaks every downgrading migration
test that runs after it, and the failure points at the wrong module. It happened twice (the 060
module, then the sidecar module); both were caught only by a full `tests/integration/db` run.

This guard reads the sources, statically: a module that references slots must override
`session_factory` at module level with a private-head fixture (or alias that of a module that
does), or use `private_head_engine` directly, or be declared below with the reason it is safe.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_TESTS = REPO_ROOT / "tests" / "integration" / "db"

# `slot_id` stands for bind and relay: both need a slot to act on.
_SLOT_REFERENCE = re.compile(
    r"focus_slot|FocusSlot|SlotAnchor|brain_slot|slot_project|session_slots|slot_id|\.relay\("
)
_SHARED_FIXTURES = frozenset({"engine", "session_factory"})

#: Modules that reference slots but never write them, by dotted name, with the reason.
DECLARED_NOT_WRITING: Mapping[str, str] = {}


def _dotted(path: Path) -> str:
    return ".".join(path.relative_to(REPO_ROOT).with_suffix("").parts)


def _is_compliant(
    name: str, sources: Mapping[str, str], seen: frozenset[str] = frozenset()
) -> bool:
    if name in seen or name not in sources:
        return False
    tree = ast.parse(sources[name])
    aliases: dict[str, str] = {}
    params: set[str] = set()
    for node in ast.walk(tree):
        # Only what pytest injects: a helper's `engine` parameter may well be a private one.
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
            node.name.startswith("test")
            or any("fixture" in ast.unparse(d) for d in node.decorator_list)
        ):
            params |= {arg.arg for arg in node.args.args + node.args.kwonlyargs}
    # The shared engine taken by ANY test or fixture of the module disqualifies it,
    # whatever its `session_factory` is: naming the private head is not using it.
    if "engine" in params:
        return False
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            aliases.update({a.asname or a.name: f"{node.module}.{a.name}" for a in node.names})
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
            node.name == "session_factory"
        ):
            return "private_head_engine" in {a.arg for a in node.args.args}
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "session_factory" for t in node.targets
        ):
            value = node.value
            return (
                isinstance(value, ast.Attribute)
                and value.attr == "session_factory"
                and isinstance(value.value, ast.Name)
                and _is_compliant(aliases.get(value.value.id, ""), sources, seen | {name})
            )
    return "private_head_engine" in params and not params & _SHARED_FIXTURES


def slot_module_problems(
    sources: Mapping[str, str], declared: Mapping[str, str] = DECLARED_NOT_WRITING
) -> list[str]:
    """Every way a slot-referencing module, or a declaration, is wrong; empty when none."""
    problems = [
        f"{name} references focus slots but neither overrides `session_factory` with a "
        "private-head fixture nor uses `private_head_engine`: its slot rows would land on the "
        "shared head and make every later downgrading migration test refuse. Rebind the "
        "fixtures from tests.integration.db.test_delivery_focus_slots, or declare the module "
        "in DECLARED_NOT_WRITING with the reason it never writes."
        for name, source in sorted(sources.items())
        if _SLOT_REFERENCE.search(source)
        and name not in declared
        and not _is_compliant(name, sources)
    ]
    problems += [
        f"{name} is declared in DECLARED_NOT_WRITING but no such module references slots"
        for name in sorted(declared)
        if name not in sources or not _SLOT_REFERENCE.search(sources[name])
    ]
    return problems


def _tree_sources() -> dict[str, str]:
    return {_dotted(p): p.read_text(encoding="utf-8") for p in sorted(DB_TESTS.glob("test_*.py"))}


# The sidecar module as it was committed before its fix: no `session_factory` of its own, so the
# parent conftest's shared-head fixture served it. Header lines only; the body is irrelevant.
_SIDECAR_ORIGINAL = """
import pytest

from brain_v42.repositories.pg_focus_slot import session_slots_block
from tests.integration.db.test_delivery_focus_slot_bound_end import bound
from tests.integration.db.test_delivery_focus_slots import slot_project  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_the_live_block_conforms(session_factory, slot_project):
    await bound(session_factory, slot_project)
"""

_PRIVATE_FIXTURE = """
import pytest_asyncio

@pytest_asyncio.fixture
async def session_factory(private_head_engine):
    return private_head_engine
"""

_ALIASED = """
from tests.integration.db import test_slots_base as _slots

session_factory = _slots.session_factory
slot_project = _slots.slot_project

async def test_it(session_factory, slot_project):
    ...
"""

_SHARED_FIXTURE = """
import pytest_asyncio

@pytest_asyncio.fixture
async def session_factory(engine):
    return engine

async def test_it(session_factory):
    await service(session_factory).open(SlotAnchor)
"""

_BASE = "tests.integration.db.test_slots_base"
_SIDECAR = "tests.integration.db.test_sidecar"


def test_the_tree_has_no_slot_module_on_the_shared_head() -> None:
    assert slot_module_problems(_tree_sources()) == []


def test_the_guard_is_not_blind() -> None:
    """Non-vacuity: a scan that finds no slot module would pass forever."""
    sources = _tree_sources()
    referencing = [n for n, s in sources.items() if _SLOT_REFERENCE.search(s)]
    assert len(referencing) >= 8
    assert all(_is_compliant(name, sources) for name in referencing)


def test_the_sidecars_original_form_is_refused() -> None:
    problems = slot_module_problems(
        {_BASE: _PRIVATE_FIXTURE + "focus_slots = 1", _SIDECAR: _SIDECAR_ORIGINAL}
    )
    assert len(problems) == 1 and problems[0].startswith(_SIDECAR)


def test_a_module_writing_slots_on_a_shared_fixture_is_refused() -> None:
    problems = slot_module_problems({_SIDECAR: _SHARED_FIXTURE})
    assert len(problems) == 1 and "private-head fixture" in problems[0]


def test_a_private_fixture_an_alias_of_it_and_a_direct_engine_pass() -> None:
    direct = "async def test_it(private_head_engine):\n    focus_slots = 1\n"
    assert (
        slot_module_problems(
            {
                _BASE: _PRIVATE_FIXTURE + "focus_slots = 1",
                _SIDECAR: _ALIASED,
                "tests.integration.db.test_direct": direct,
            }
        )
        == []
    )


def test_an_alias_of_a_module_that_is_not_private_is_refused() -> None:
    """The alias is only as good as its target; a cycle or a missing target never passes."""
    problems = slot_module_problems(
        {_BASE: _SHARED_FIXTURE, _SIDECAR: _ALIASED, "tests.integration.db.test_orphan": _ALIASED}
    )
    assert [p.split()[0] for p in problems] == [
        "tests.integration.db.test_orphan",
        _SIDECAR,
        _BASE,
    ]


def test_a_declared_module_is_exempt_and_a_stale_declaration_is_refused() -> None:
    assert slot_module_problems({_SIDECAR: _SIDECAR_ORIGINAL}, {_SIDECAR: "reads only"}) == []
    stale = slot_module_problems({_SIDECAR: "x = 1"}, {_SIDECAR: "reads only"})
    assert len(stale) == 1 and "declared in DECLARED_NOT_WRITING" in stale[0]
    assert len(slot_module_problems({}, {_SIDECAR: "reads only"})) == 1


def test_modules_that_do_not_mention_slots_are_not_asked_anything() -> None:
    assert (
        slot_module_problems({"tests.integration.db.test_other": "async def test_it(engine): ..."})
        == []
    )


_PRIVATE_NAME_SHARED_BODY = """
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

@pytest_asyncio.fixture
async def session_factory(private_head_engine, engine):
    return async_sessionmaker(engine)

focus_slots = 1
"""

_ALIASED_PLUS_SHARED_ENGINE = (
    _ALIASED
    + """
async def test_also_on_the_shared_head(engine):
    focus_slots = 1
"""
)


def test_a_private_override_that_also_takes_the_shared_engine_is_refused() -> None:
    """Independent review of #287: naming the private head is not using it."""
    problems = slot_module_problems({_SIDECAR: _PRIVATE_NAME_SHARED_BODY})
    assert len(problems) == 1 and problems[0].startswith(_SIDECAR)


def test_an_alias_does_not_excuse_a_test_on_the_shared_engine() -> None:
    problems = slot_module_problems(
        {_BASE: _PRIVATE_FIXTURE + "focus_slots = 1", _SIDECAR: _ALIASED_PLUS_SHARED_ENGINE}
    )
    assert len(problems) == 1 and problems[0].startswith(_SIDECAR)

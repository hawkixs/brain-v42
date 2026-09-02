"""A validator that fails must MARK the row, not die while marking it.

The night of the 19→20: `reorg` failed, `dream.sh` printed "dream_runs marked
partial", and the `dream_runs` row stayed `done`. Both facts are true at the same
time because the marking itself crashed.

THE SHAPE, exactly. `main()` builds the engine OUTSIDE any loop, then uses it in
TWO successive `asyncio.run()` calls:

    session_factory = _build_factory(...)      # engine created outside a loop
    try:
        asyncio.run(validate(..., session_factory, ...))     # loop 1, then CLOSED
    except ValidationFailure as exc:
        asyncio.run(_mark_dream_run_partial(session_factory, ...))   # loop 2

`asyncio.run` closes its loop on exit. The connections the pool opened during
loop 1 stay attached to that dead loop; `pool_pre_ping=True` touches them at the
first checkout of loop 2, and asyncpg raises
`RuntimeError: Event loop is closed`. The marking therefore never happens — and
it raises AFTER the `except`, so the exception propagates out of `main()`: the
failure `print` does not run either.

This path only triggers on the failure path, that is, exactly when it is needed.
A green night never meets it — which is why it survived.

`connect_validate.py` is the healthy model: a single `asyncio.run`.

WHY THE EXISTING TESTS DID NOT SEE IT: `test_main_logs_positive_wet_
validation_evidence` replaces the factory with a `MagicMock()`, which lets itself
be used from any loop and has no pool. It tests the GREEN path with an object
that cannot reproduce the defect. The harness below reproduces the loop affinity,
which is the production fact.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
from typing import Any
from unittest.mock import MagicMock

import pytest

_WET_REORG_TRAILER = (
    "Prose.\n\n"
    "=== REORG REPORT ===\n"
    '{"dry_run": false, "updated": [], "archived": []}\n'
    "=== END ===\n"
)
_WET_PROMOTE_TRAILER = (
    'Prose.\n\n=== PROMOTE REPORT ===\n{"promoted": [], "skipped": []}\n=== END ===\n'
)


class _DeadLoopError(RuntimeError):
    """What asyncpg raises when a connection from a closed loop is touched again."""


class _Begin:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Session:
    def __init__(self, pool: _LoopAffinePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> _Session:
        # The pool checkout: it is HERE that `pool_pre_ping` touches the connection.
        self._pool.checkout()
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def begin(self) -> _Begin:
        return _Begin()

    async def execute(self, statement: Any) -> MagicMock:
        self._pool.statements.append(statement)
        return MagicMock()

    async def commit(self) -> None:
        return None


class _LoopAffinePool:
    """A session factory that binds to the loop of the FIRST checkout.

    This is the behaviour of an asyncpg pool under `pool_pre_ping=True`, reduced
    to what matters here. A `MagicMock` cannot reproduce it: it has no pool, hence
    no affinity.
    """

    def __init__(self) -> None:
        self.bound_loop: asyncio.AbstractEventLoop | None = None
        self.statements: list[Any] = []
        self.checkouts = 0

    def checkout(self) -> None:
        loop = asyncio.get_running_loop()
        self.checkouts += 1
        if self.bound_loop is None:
            self.bound_loop = loop
        elif loop is not self.bound_loop:
            raise _DeadLoopError("Event loop is closed")

    def __call__(self) -> _Session:
        return _Session(self)


def _module(name: str) -> Any:
    from scripts.dream import connect_validate, promote_validate, reorg_validate

    return {
        "reorg": reorg_validate,
        "promote": promote_validate,
        "connect": connect_validate,
    }[name]


def _argv(name: str, tmp_path: pathlib.Path) -> list[str]:
    if name == "reorg":
        log = tmp_path / "reorg.log"
        log.write_text(_WET_REORG_TRAILER)
        tags_before = tmp_path / "tags_before.json"
        tags_before.write_text("{}")
        events = tmp_path / "reorg.events.jsonl"
        events.write_text("")
        return [
            "--report-log",
            str(log),
            "--dream-run-id",
            "4242",
            "--project-key",
            "brain-v42",
            "--tags-before-json",
            str(tags_before),
            "--events-jsonl",
            str(events),
        ]
    log = tmp_path / "promote.log"
    log.write_text(_WET_PROMOTE_TRAILER)
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps([]))
    return [
        "--report-log",
        str(log),
        "--candidates-json",
        str(candidates),
        "--dream-run-id",
        "4242",
        "--project-key",
        "brain-v42",
    ]


def _install(name: str, monkeypatch: pytest.MonkeyPatch, pool: _LoopAffinePool) -> Any:
    """Wire the module onto the affine factory, with a production `validate`.

    "Production" means: it TOUCHES the database — hence binds loop 1 — then fails.
    That is the 19→20 night's sequence. A `validate` that failed without touching
    the database would reproduce nothing: the pool would stay pristine and loop 2
    would work.
    """
    module = _module(name)
    monkeypatch.setattr(
        module, "Settings", lambda: MagicMock(postgres_url="postgresql+asyncpg://unused")
    )
    monkeypatch.setattr(module, "_build_factory", lambda _url: pool)

    async def _validate_that_touched_the_db(*_args: object, **_kwargs: object) -> None:
        async with pool() as session:
            await session.execute("SELECT 1")
        raise module.ValidationFailure("integrity violation")

    monkeypatch.setattr(module, "validate", _validate_that_touched_the_db)
    return module


@pytest.mark.parametrize("name", ["reorg", "promote"])
def test_a_failing_validator_actually_marks_the_row_partial(
    name: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The production fact: the marking must SUCCEED, not raise."""
    module = _install(name, monkeypatch, pool := _LoopAffinePool())

    rc = module.main(_argv(name, tmp_path))

    assert rc == 1, "un validateur qui échoue rend 1, il ne remonte pas son exception"
    assert pool.checkouts == 2, "validate PUIS le marquage doivent avoir servi le pool"
    assert pool.statements, (
        "aucun UPDATE n'a atteint la base : la ligne dream_runs reste `done` alors "
        "que la nuit a déclaré l'échec — c'est le faux-vert du 19→20"
    )
    compiled = str(pool.statements[-1]).lower()
    assert "update" in compiled and "dream_runs" in compiled
    assert "VALIDATION FAILED" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["reorg", "promote"])
def test_the_pool_is_never_used_from_a_second_event_loop(
    name: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The structural property, stated directly rather than deduced.

    The harness RAISES if a second checkout comes from another loop; this test
    therefore fails on `_DeadLoopError` as long as `main()` opens two loops.
    """
    module = _install(name, monkeypatch, pool := _LoopAffinePool())

    module.main(_argv(name, tmp_path))

    assert pool.bound_loop is not None
    assert pool.checkouts == 2


@pytest.mark.parametrize("name", ["reorg", "promote", "connect"])
def test_a_validator_opens_exactly_one_event_loop(name: str) -> None:
    """Textual pinning: one `asyncio.run` per module, `connect` included.

    `connect_validate` is the healthy model and serves as a witness — if this test
    went green for a bad reason (the pattern no longer matches), it would redden
    here.
    """
    module = _module(name)
    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    runs = re.findall(r"asyncio\.run\(", source)

    assert len(runs) == 1, (
        f"{name}_validate ouvre {len(runs)} boucles. Deux `asyncio.run` sur une "
        f"fabrique partagée laissent les connexions de la première boucle dans le "
        f"pool ; `pool_pre_ping` les touche depuis la seconde et asyncpg lève."
    )

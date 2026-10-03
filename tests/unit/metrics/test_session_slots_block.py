import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from brain_v42.db.focus_slots import AnchorState
from brain_v42.repositories.pg_focus_slot import (
    assemble_session_slots_block,
    session_slots_block,
    session_slots_statements,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

CONTRACT = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "session_slots_block.json"
NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
FORBIDDEN = ("access_log", "access_log_daily", "last_accessed_at", "access_count")


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _conforms(value: Any, shape: Any, path: str = "$") -> None:
    if isinstance(shape, dict):
        assert isinstance(value, dict) and set(value) == set(shape), path
        for key, inner in shape.items():
            _conforms(value[key], inner, f"{path}.{key}")
    elif isinstance(shape, list):
        assert isinstance(value, list) and value, f"{path}: exercise at least one element"
        for index, item in enumerate(value):
            _conforms(item, shape[0], f"{path}[{index}]")
    else:
        assert _kind(value) in shape.split("|"), f"{path}: {_kind(value)} not in {shape}"


def _block() -> dict[str, Any]:
    bound_slot, orphan, bound_session = uuid4(), uuid4(), uuid4()
    return assemble_session_slots_block(
        bases=[
            {"project_key": "brain-v42", "focus_revision": 9, "focus_updated_at": NOW, "chars": 812}
        ],
        slots=[
            {
                "id": bound_slot,
                "project_key": "brain-v42",
                "title": "relay",
                "revision": 2,
                "opened_at": NOW,
                "body_updated_at": NOW,
                "bound_session_id": bound_session,
                "last_bound_ended_at": None,
            },
            {
                "id": orphan,
                "project_key": "brain-v42",
                "title": "old",
                "revision": 0,
                "opened_at": NOW - timedelta(days=9),
                "body_updated_at": NOW - timedelta(days=9),
                "bound_session_id": None,
                "last_bound_ended_at": None,
            },
        ],
        states={
            bound_slot: [AnchorState(kind="lot", ref="lot:0.6.4", completing_row_id=None)],
            orphan: [AnchorState(kind="ticket", ref="ticket:x", completing_row_id=uuid4())],
        },
        sessions=[
            {
                "id": bound_session,
                "project_key": "brain-v42",
                "slot_id": bound_slot,
                "started_at": NOW,
                "last_heartbeat_at": NOW,
                "last_observed_at": None,
            },
            {
                "id": uuid4(),
                "project_key": "brain-v42",
                "slot_id": None,
                "started_at": NOW - timedelta(days=2),
                "last_heartbeat_at": NOW - timedelta(days=2),
                "last_observed_at": NOW,
            },
        ],
        traces=[{"project_key": "brain-v42", "open_traces": 3}],
        now=NOW,
    )


def test_the_assembled_block_equals_the_published_shape() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    shape = {key: value for key, value in contract["shape"].items() if key != "generated_at"}
    _conforms(_block(), shape)


def test_the_block_derives_holder_staleness_and_pending() -> None:
    project = _block()["projects"][0]
    bound, orphan = project["slots"]
    assert (bound["is_stale"], bound["receipt_pending"]) == (False, False)
    assert (orphan["bound_session_id"], orphan["is_stale"], orphan["receipt_pending"]) == (
        None,
        True,
        True,
    )
    assert [s["is_stale"] for s in project["sessions"]] == [False, True]
    assert project["agent_traces_open"] == 3


def test_the_contract_carries_no_body_summary_or_client_key() -> None:
    text = CONTRACT.read_text(encoding="utf-8")
    for name in ('"body"', '"summary"', '"client_key"', '"next_focus"'):
        assert name not in text


class _RecordingSession:
    """Fake AsyncSession: records every executed statement, answers per call position."""

    def __init__(self, replies: list[list[dict[str, Any]]]) -> None:
        self.statements: list[Any] = []
        self._replies = iter(replies)

    async def execute(self, statement: Any) -> Any:  # noqa: ANN401
        self.statements.append(statement)
        rows = next(self._replies, [])
        return SimpleNamespace(mappings=lambda: rows)


async def test_collection_executes_only_selects_and_no_access_counter_s13() -> None:
    # No body, summary or client_key leaves the block either: the key-set pinned by
    # test_the_assembled_block_equals_the_published_shape rejects any extra key.
    slot = {
        "id": uuid4(),
        "project_key": "brain-v42",
        "title": "relay",
        "revision": 1,
        "opened_at": NOW,
        "body_updated_at": NOW,
        "bound_session_id": None,
        "last_bound_ended_at": None,
    }
    # session_slots_statements() order: bases, slots, sessions, traces; then the anchors.
    names = list(session_slots_statements())
    session = _RecordingSession([[slot] if name == "slots" else [] for name in names])

    block = await session_slots_block(cast("AsyncSession", session), now=NOW)

    assert block["projects"][0]["project"] == "brain-v42"
    for statement in session.statements:
        assert isinstance(statement, sa.Select), type(statement).__name__
        sql = str(statement.compile(dialect=postgresql.dialect()))
        for name in FORBIDDEN:
            assert name not in sql, name
    assert len(session.statements) == len(names) + 1  # the anchor-state query ran too

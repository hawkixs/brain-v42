"""Programme progress: the caller's identity and every input, checked before a write.

Contract `docs/contracts/programmes_v1.md` §6. Who writes is the project the
credential layer resolved for this request (`provenance`): a principal verified
from the bearer, and the project its accepted actor maps to. Nothing in a tool
argument names the writer. Without both, a write is refused
(`caller_unidentified`): under the shared-token or stdio transports there is no
verified project, so programmes are written over the credentialed HTTP transport
only. Reads need no identity: the same data is published unauthenticated on the
sidecar behind the WireGuard boundary.

The project comes from the actor the credential's issuer patterns accepted; that
acceptance is the authority, as for delivery attestations (`delivery_tools`). A
credential holding `@project` may therefore write as any existing project it
declares, and one holding a prefix pattern (`red-lab*`) as every existing project
the pattern matches. `credentials_http` calls the same lookup "attribution" because
it never refuses a request on it; here it decides who may write, by design.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from brain_v42.models.programme import (
    GATE_STATES,
    LINE_MAX,
    PROGRAMME_STATES,
    TITLE_MAX,
    ProgrammeError,
    gate_key,
    line,
    programme_key,
)
from brain_v42.models.programme import (
    progress as checked_progress,
)
from brain_v42.models.programme import (
    weight as checked_weight,
)
from brain_v42.models.project_key import canonicalize_project_key
from brain_v42.provenance import get_current_actor_project, get_current_principal


class ProgrammeRepository(Protocol):
    """`repositories.pg_programme.PgProgrammeRepo`; `caller` is always the service's."""

    async def list_programmes(
        self, *, include_done: bool = False, key: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def upsert_programme(
        self,
        *,
        caller: str,
        key: str,
        title: str | None = None,
        state: str | None = None,
        position: int | None = None,
    ) -> dict[str, Any]: ...

    async def upsert_gate(
        self,
        *,
        caller: str,
        programme: str,
        gate: str,
        title: str | None = None,
        owner: str | None = None,
        depends_on: Sequence[str] | None = None,
        weight: int | None = None,
        position: int | None = None,
        state: str | None = None,
    ) -> dict[str, Any]: ...

    async def report_gate(
        self,
        *,
        caller: str,
        programme: str,
        gate: str,
        state: str | None = None,
        progress: Mapping[str, Any] | None = None,
        clear_progress: bool = False,
        note: str | None = None,
        clear_note: bool = False,
    ) -> dict[str, Any]: ...

    async def request(
        self, *, caller: str, programme: str, gate: str, ask: str | None
    ) -> dict[str, Any]: ...


def current_caller() -> str:
    """The verified caller's project, or a refusal: never a declared name."""
    principal = get_current_principal()
    project = get_current_actor_project()
    if principal is None or not project:
        raise ProgrammeError(
            "caller_unidentified",
            "programme writes need a credential that resolves to a project "
            "(HTTP transport with a client credential)",
        )
    return project


def _state(value: str | None, allowed: Sequence[str]) -> str | None:
    if value is not None and value not in allowed:
        raise ProgrammeError("invalid_state", f"state must be one of {', '.join(allowed)}")
    return value


def _position(value: int | None) -> int | None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ProgrammeError("invalid_position", "position must be a non-negative integer")
    return value


def _owner(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        canonical = canonicalize_project_key(value)
    except ValueError as refused:
        raise ProgrammeError("invalid_owner", "owner must be a canonical project key") from refused
    if canonical != value or len(value) > 50:
        raise ProgrammeError("invalid_owner", "owner must be a canonical project key")
    return value


class ProgrammeService:
    def __init__(self, repo: ProgrammeRepository) -> None:
        self.repo = repo

    async def list_programmes(
        self, *, include_done: bool = False, key: str | None = None
    ) -> list[dict[str, Any]]:
        return await self.repo.list_programmes(
            include_done=include_done, key=None if key is None else programme_key(key)
        )

    async def upsert_programme(
        self,
        *,
        key: str,
        title: str | None = None,
        state: str | None = None,
        position: int | None = None,
    ) -> dict[str, Any]:
        # Every input is checked before the caller is read: a refusal names the input.
        checked_key = programme_key(key)
        checked_title = None if title is None else line(title, field="title", limit=TITLE_MAX)
        checked_state = _state(state, PROGRAMME_STATES)
        checked_position = _position(position)
        return await self.repo.upsert_programme(
            caller=current_caller(),
            key=checked_key,
            title=checked_title,
            state=checked_state,
            position=checked_position,
        )

    async def upsert_gate(
        self,
        *,
        programme: str,
        gate: str,
        title: str | None = None,
        owner: str | None = None,
        depends_on: Sequence[str] | None = None,
        weight: int | None = None,
        position: int | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        checked_programme = programme_key(programme)
        checked_gate = gate_key(gate)
        checked_title = None if title is None else line(title, field="title", limit=TITLE_MAX)
        checked_owner = _owner(owner)
        checked_depends = None if depends_on is None else [gate_key(key) for key in depends_on]
        checked_weight_value = None if weight is None else checked_weight(weight)
        checked_position = _position(position)
        checked_state = _state(state, GATE_STATES)
        return await self.repo.upsert_gate(
            caller=current_caller(),
            programme=checked_programme,
            gate=checked_gate,
            title=checked_title,
            owner=checked_owner,
            depends_on=checked_depends,
            weight=checked_weight_value,
            position=checked_position,
            state=checked_state,
        )

    async def report_gate(
        self,
        *,
        programme: str,
        gate: str,
        state: str | None = None,
        progress: Mapping[str, Any] | None = None,
        clear_progress: bool = False,
        note: str | None = None,
        clear_note: bool = False,
    ) -> dict[str, Any]:
        if (progress is not None and clear_progress) or (note is not None and clear_note):
            raise ProgrammeError(
                "conflicting_arguments", "set a value or clear it, not both in one call"
            )
        if (
            state is None
            and progress is None
            and note is None
            and not (clear_progress or clear_note)
        ):
            raise ProgrammeError("nothing_to_report", "give a state, a progress or a note")
        checked_programme = programme_key(programme)
        checked_gate = gate_key(gate)
        checked_state = _state(state, GATE_STATES)
        checked_progress_value = (
            None
            if progress is None
            else checked_progress(progress.get("done"), progress.get("total"), progress.get("unit"))
        )
        checked_note = None if note is None else line(note, field="note", limit=LINE_MAX)
        return await self.repo.report_gate(
            caller=current_caller(),
            programme=checked_programme,
            gate=checked_gate,
            state=checked_state,
            progress=checked_progress_value,
            clear_progress=clear_progress,
            note=checked_note,
            clear_note=clear_note,
        )

    async def request(self, *, programme: str, gate: str, ask: str | None) -> dict[str, Any]:
        checked_programme = programme_key(programme)
        checked_gate = gate_key(gate)
        checked_ask = None if ask is None else line(ask, field="ask", limit=LINE_MAX)
        return await self.repo.request(
            caller=current_caller(), programme=checked_programme, gate=checked_gate, ask=checked_ask
        )

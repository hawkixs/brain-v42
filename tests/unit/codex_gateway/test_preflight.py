"""The four clauses of `/ready`, provable without root.

WHY THIS EXISTS AT ALL — measured on 2026-09-03, not supposed.

`scripts/rotate_codex_gateway_credentials.py::_preflight` calls
`privileged_installer.preflight(config.shrik_env)` BEFORE
`_refuse_a_broken_gateway_contract(...)`. On this host
`/usr/local/sbin/brain-shrik-env-control` does not exist, so the CLI exits on
`{"error":"non-interactive Shrik privilege is unavailable"}` before a single SQL
probe. The contract proof has therefore been unreachable for 33 days, while the
four clauses were in fact true in production the whole time. A proof that can
only run inside a rotation is a proof nobody runs.

THE ONE RULE THIS MODULE OBEYS

It owns NO list of views, barriers or triggers. Every name is read out of
`_CODEX_CONTRACT_READY` — the same string `/ready` executes. The rotation CLI
keeps its copy (it deliberately imports nothing from `brain_v42`, since it
rewrites the `.env` files `Settings` reads); this module is inside the package
and has no such excuse. A second list would drift, and the drift would only show
after a cutover — which is exactly the failure b3331691 already paid for.
"""

from __future__ import annotations

from typing import Any

import pytest

from brain_v42.codex_gateway.composition import _CODEX_CONTRACT_READY
from brain_v42.codex_gateway.preflight import (
    ContractShapeError,
    contract_names,
    inspect_contract,
)

# ── the names come from the authority, never from here ───────────────────────


def test_the_names_are_read_out_of_the_readiness_authority() -> None:
    """10 views, 7 barriers, 2 triggers — and each one present in the SQL itself.

    The counts are asserted because the parser returning an empty tuple would
    otherwise make every clause vacuously green.
    """
    names = contract_names()
    authority = str(_CODEX_CONTRACT_READY)

    assert len(names.views) == 10
    assert len(names.barrier_views) == 7
    assert len(names.triggers) == 2
    for name in (*names.views, *names.barrier_views, *names.triggers):
        assert f"'{name}'" in authority, f"{name} was invented, not read"


def test_the_barrier_views_are_a_subset_of_the_declared_views() -> None:
    """A barrier name absent from the view list would mean the parser crossed arrays."""
    names = contract_names()
    assert set(names.barrier_views) < set(names.views)


def test_a_changed_authority_shape_is_refused_rather_than_guessed() -> None:
    """Fail-closed on the ONE thing a regex parser cannot survive.

    If somebody rewrites the readiness SQL without `ARRAY[...]::text[]`, the
    parser must stop. Returning what it managed to find would silently shrink the
    contract to the clauses it still recognised — a preflight that goes green
    because it forgot what to check.
    """
    with pytest.raises(ContractShapeError):
        contract_names("SELECT true")


# ── the verdict ──────────────────────────────────────────────────────────────


class _Row:
    def __init__(self, missing_views: list[str], unbarriered: list[str], inactive: list[str]):
        self.missing_views = missing_views
        self.unbarriered = unbarriered
        self.inactive_triggers = inactive


class _Result:
    def __init__(self, row: _Row) -> None:
        self._row = row

    def one(self) -> _Row:
        return self._row


class _Connection:
    """The narrowest fake that can lie the way a real database can."""

    def __init__(self, *, ready: bool, row: _Row) -> None:
        self._ready = ready
        self._row = row

    async def scalar(self, _statement: Any, *_args: Any, **_kwargs: Any) -> bool:
        return self._ready

    async def execute(self, _statement: Any, *_args: Any, **_kwargs: Any) -> _Result:
        return _Result(self._row)


@pytest.mark.asyncio
async def test_four_true_clauses_read_ready_and_exit_zero() -> None:
    report = await inspect_contract(_Connection(ready=True, row=_Row([], [], [])))

    assert report.ready is True
    assert set(report.clauses) == {
        "views",
        "security_barrier",
        "trg_feature_artifact_live_target",
        "trg_ticket_participants_immutable",
    }
    assert all(report.clauses.values())
    assert report.exit_code == 0


@pytest.mark.asyncio
async def test_a_view_that_lost_its_barrier_is_named_not_merely_counted() -> None:
    """The exact move a column rotation forces — DROP+CREATE without the option.

    "security_barrier: false" would send an operator to read seven view
    definitions. The name is the whole value of the decomposition.
    """
    report = await inspect_contract(_Connection(ready=False, row=_Row([], ["codex_ticket_v1"], [])))

    assert report.ready is False
    assert report.clauses["security_barrier"] is False
    assert report.clauses["views"] is True, "an unrelated clause must stay green"
    assert report.missing["security_barrier"] == ["codex_ticket_v1"]
    assert "codex_ticket_v1" in report.as_json()
    assert report.exit_code == 1


@pytest.mark.asyncio
async def test_an_inactive_trigger_is_named_by_its_own_clause() -> None:
    report = await inspect_contract(
        _Connection(ready=False, row=_Row([], [], ["trg_ticket_participants_immutable"]))
    )

    assert report.clauses["trg_ticket_participants_immutable"] is False
    assert report.clauses["trg_feature_artifact_live_target"] is True
    assert report.exit_code == 1


@pytest.mark.asyncio
async def test_a_refusal_the_decomposition_cannot_explain_is_declared() -> None:
    """The branch that keeps the authority authoritative.

    `/ready` is the string that decides; the decomposition only explains it. If
    the authority refuses and the three diagnostic arrays come back empty, the
    honest output is "not ready, and I cannot tell you why" — never a green
    report. Reporting the four clauses true here would contradict the very
    verdict the gateway will produce.
    """
    report = await inspect_contract(_Connection(ready=False, row=_Row([], [], [])))

    assert report.ready is False
    assert report.unexplained is True
    assert report.exit_code == 1


@pytest.mark.asyncio
async def test_a_ready_authority_with_a_dissenting_clause_is_also_unexplained() -> None:
    """The mirror case, and the one that would hide a real regression.

    If the authority says true while a clause name comes back missing, something
    diverged between the two queries. Trusting the boolean and dropping the name
    is how a decomposition becomes decorative.
    """
    report = await inspect_contract(
        _Connection(ready=True, row=_Row(["codex_dream_run_v1"], [], []))
    )

    assert report.unexplained is True
    assert report.exit_code == 1

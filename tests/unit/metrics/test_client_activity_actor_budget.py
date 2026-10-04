"""One actor cannot fill the activity registry by rotating connections.

Measured on production, 2026-10-04 (ticket `9595d6c8`): red-rail opens one MCP
HTTP connection per call -- 1193 in 24 h, median lifetime 0 s, peaks of 135 new
connections in one 10-minute window -- against a capacity of 64 and a TTL of
600 s. Every connection became a transport row carrying calls, so a burst
evicted the bearing rows of everyone else.

The fix is a per-actor transport budget: beyond ``MAX_TRANSPORT_ROWS_PER_ACTOR``
live transport rows, a new connection of that actor folds into the actor's
residual row, which keeps its calls.
"""

from __future__ import annotations

from datetime import UTC, datetime

from brain_v42.metrics.client_activity import (
    ACTIVITY_TTL_SECONDS,
    MAX_TRANSPORT_ROWS_PER_ACTOR,
    ClientActivityRegistry,
)
from brain_v42.metrics.client_observation import ClientObservation

SECRET = b"x" * 32
SESSION = "0b8f2c4e-5d1a-4c3b-9e7f-1a2b3c4d5e6f"


class _Clock:
    def __init__(self) -> None:
        self._value = 0.0

    def __call__(self) -> float:
        return self._value

    def advance(self, seconds: float) -> None:
        self._value += seconds


def _registry(clock: _Clock) -> ClientActivityRegistry:
    return ClientActivityRegistry(
        secret=SECRET,
        clock=clock,
        wall_clock=lambda: datetime(2026, 10, 4, 12, 0, tzinfo=UTC),
    )


def _connections(actor: str, count: int, *, first: int = 0) -> tuple[ClientObservation, ...]:
    """`count` distinct connections of one actor, one call each."""
    return tuple(
        ClientObservation(actor=actor, session_id=None, calls=1, transport=f"{index:032x}")
        for index in range(first, first + count)
    )


def _rows(registry: ClientActivityRegistry, actor: str) -> list[dict[str, object]]:
    clients = registry.snapshot()["clients"]
    assert isinstance(clients, list)
    return [row for row in clients if row["actor"] == actor and row["brain_calls"] is not None]


def _calls(rows: list[dict[str, object]]) -> int:
    return sum(int(str(row["brain_calls"])) for row in rows)


class TestAFloodOfConnectionsOfOneActorIsBounded:
    def test_135_single_call_connections_evict_nothing_and_lose_no_call(self) -> None:
        """The measured peak: 135 new connections of one actor in one TTL window."""
        registry = _registry(_Clock())
        registry.record_observations(_connections("brain-v42", 3, first=1000))
        registry.record_observations(_connections("codex", 2, first=2000))
        registry.record_observations(_connections("auto-discord", 4, first=3000))

        for start in range(0, 135, 27):
            registry.record_observations(_connections("red-rail", 27, first=start))

        counters = registry.eviction_counters()
        assert counters["evictions_bearing_total"] == 0
        assert counters["evictions_total"] == {"ttl": 0, "capacity": 0}

        flood = _rows(registry, "red-rail")
        assert _calls(flood) == 135, "every call of the flooding actor stays counted"
        kinds = [row["kind"] for row in flood]
        assert kinds.count("transport") == MAX_TRANSPORT_ROWS_PER_ACTOR
        assert kinds.count("unattributed") == 1
        residual = next(row for row in flood if row["kind"] == "unattributed")
        assert residual["brain_calls"] == 135 - MAX_TRANSPORT_ROWS_PER_ACTOR

        assert _calls(_rows(registry, "brain-v42")) == 3
        assert _calls(_rows(registry, "codex")) == 2
        assert _calls(_rows(registry, "auto-discord")) == 4

    def test_a_flood_arriving_first_does_not_starve_the_actors_after_it(self) -> None:
        registry = _registry(_Clock())
        registry.record_observations(_connections("red-rail", 135))
        registry.record_observations(_connections("brain-v42", 4, first=1000))

        assert registry.eviction_counters()["evictions_bearing_total"] == 0
        assert len(_rows(registry, "brain-v42")) == 4

    def test_every_folded_observation_is_counted(self) -> None:
        registry = _registry(_Clock())
        registry.record_observations(_connections("red-rail", 135))

        assert (
            registry.eviction_counters()["folded_observations_total"]
            == 135 - MAX_TRANSPORT_ROWS_PER_ACTOR
        )


class TestTheTransportTierStillSeparatesRealEngines:
    def test_four_connections_of_one_actor_stay_four_rows(self) -> None:
        """The case the transport tier exists for: four engines in one directory."""
        registry = _registry(_Clock())
        registry.record_observations(_connections("brain-v42", 4))

        rows = _rows(registry, "brain-v42")
        assert [row["kind"] for row in rows] == ["transport"] * 4
        assert registry.eviction_counters()["folded_observations_total"] == 0

    def test_a_connection_within_the_budget_keeps_accumulating_after_the_budget_fills(
        self,
    ) -> None:
        registry = _registry(_Clock())
        registry.record_observations(_connections("red-rail", MAX_TRANSPORT_ROWS_PER_ACTOR + 3))

        registry.record_observations(_connections("red-rail", 1, first=0))

        transports = [row for row in _rows(registry, "red-rail") if row["kind"] == "transport"]
        assert len(transports) == MAX_TRANSPORT_ROWS_PER_ACTOR
        assert sorted(int(str(row["brain_calls"])) for row in transports) == [1] * (
            MAX_TRANSPORT_ROWS_PER_ACTOR - 1
        ) + [2]

    def test_the_budget_is_per_actor_not_shared(self) -> None:
        registry = _registry(_Clock())
        registry.record_observations(_connections("red-rail", 20))
        registry.record_observations(
            _connections("brain-v42", MAX_TRANSPORT_ROWS_PER_ACTOR, first=1000)
        )

        rows = _rows(registry, "brain-v42")
        assert [row["kind"] for row in rows] == ["transport"] * MAX_TRANSPORT_ROWS_PER_ACTOR

    def test_a_declared_session_still_joins_past_the_budget(self) -> None:
        """The budget bounds transport rows only; the join tier is untouched."""
        registry = _registry(_Clock())
        registry.record_observations(_connections("red-rail", MAX_TRANSPORT_ROWS_PER_ACTOR + 2))

        registry.record_observations(
            (ClientObservation(actor="red-rail", session_id=SESSION, calls=5, transport="f" * 32),)
        )

        sessions = [row for row in _rows(registry, "red-rail") if row["kind"] == "session"]
        assert len(sessions) == 1
        assert sessions[0]["brain_calls"] == 5


class TestAnExpiredConnectionFreesItsBudget:
    def test_after_the_ttl_a_new_connection_gets_its_own_row_again(self) -> None:
        clock = _Clock()
        registry = _registry(clock)
        registry.record_observations(_connections("red-rail", MAX_TRANSPORT_ROWS_PER_ACTOR + 2))

        clock.advance(ACTIVITY_TTL_SECONDS + 1)
        registry.record_observations(_connections("red-rail", 1, first=900))

        rows = _rows(registry, "red-rail")
        assert [row["kind"] for row in rows] == ["transport"]
        assert registry.eviction_counters()["folded_observations_total"] == 2


class TestRelabellingAConnectionDoesNotBypassTheBudget:
    """A connection's actor comes from a header read on every request.

    Independent review of PR #286 (codex, ha judge 20261004T174000-7b41b7ce): an
    existing transport row skipped the budget check while its actor was
    overwritten, so a client rotating two labels over reused connections moved
    rows from one budget to the other without bound.
    """

    def test_a_connection_relabelled_to_an_actor_at_its_budget_folds(self) -> None:
        registry = _registry(_Clock())
        registry.record_observations(_connections("actor-a", MAX_TRANSPORT_ROWS_PER_ACTOR))
        registry.record_observations(
            _connections("actor-b", MAX_TRANSPORT_ROWS_PER_ACTOR, first=100)
        )
        relabelled = tuple(
            ClientObservation(actor="actor-a", session_id=None, calls=1, transport=obs.transport)
            for obs in _connections("actor-b", MAX_TRANSPORT_ROWS_PER_ACTOR, first=100)
        )
        registry.record_observations(relabelled)

        rows_a = _rows(registry, "actor-a")
        assert [row["kind"] for row in rows_a].count("transport") == MAX_TRANSPORT_ROWS_PER_ACTOR
        assert (
            _calls(rows_a) + _calls(_rows(registry, "actor-b")) == 3 * MAX_TRANSPORT_ROWS_PER_ACTOR
        )

    def test_rotating_two_labels_over_new_connections_evicts_nothing(self) -> None:
        registry = _registry(_Clock())
        registry.record_observations(_connections("brain-v42", 4, first=10_000))
        for index in range(135):
            transport = f"{index:032x}"
            for actor in ("actor-b", "actor-a"):
                registry.record_observations(
                    (ClientObservation(actor=actor, session_id=None, calls=1, transport=transport),)
                )

        counters = registry.eviction_counters()
        assert counters["evictions_bearing_total"] == 0
        for actor in ("actor-a", "actor-b"):
            kinds = [row["kind"] for row in _rows(registry, actor)]
            assert kinds.count("transport") <= MAX_TRANSPORT_ROWS_PER_ACTOR
        assert _calls(_rows(registry, "actor-a")) + _calls(_rows(registry, "actor-b")) == 270
        assert _calls(_rows(registry, "brain-v42")) == 4

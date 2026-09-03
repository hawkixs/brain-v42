"""What the activity registry THROWS AWAY is counted, by cause.

Ticket `863ff2ca`, point 3, and this module is the instrument the arbitration of
2026-09-03 asked for BEFORE either fix. The brief that led to it measured the
registry at **9 rows out of 64** with **zero unattributed residues**, and found
that the pressure source the ticket named -- a cron minting one identity per
minute -- was `inactive`. Both candidate fixes protect an object of which the
live registry holds no example.

So the honest first move is not a fix, it is an instrument: `_trim_brain` cut a
sorted list and counted nothing, which made "how many measurements did we lose"
unanswerable in either direction. A zero from a source that counts nothing is
indistinguishable from a real zero -- the lesson `d5e4bd73` already wrote for the
receiver rejections, applied to the other end of the same registry.

REOPENING CONDITION for the ticket, which these counters exist to evaluate:
occupancy above 48 of 64 sustained over 24 h, OR a single observed eviction of a
bearing residue. Until one of them fires, neither (a) purge-on-DELETE nor (b)
reversing `e5cda111` has a measured problem to solve.

Two causes and no more, because two is what the code can attribute: `ttl`, from
`_prune_brain`, and `capacity`, from `_trim_brain`. "Transport rotation" is why
rows APPEAR, never why they are dropped, and a counter that claimed to separate
it would be inventing an attribution the code cannot make.
"""

from __future__ import annotations

from datetime import UTC, datetime

from brain_v42.metrics.client_activity import (
    ACTIVITY_TTL_SECONDS,
    MAX_ACTIVE_CONVERSATIONS,
    ClientActivityRegistry,
)
from brain_v42.metrics.client_observation import ClientObservation

SECRET = b"x" * 32


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
        wall_clock=lambda: datetime(2026, 9, 3, 7, 0, tzinfo=UTC),
    )


def _transports(count: int, *, first: int = 0, calls: int = 1) -> tuple[ClientObservation, ...]:
    """`count` distinct transports, each carrying `calls` measured calls."""
    return tuple(
        ClientObservation(
            actor="brain-v42",
            session_id=None,
            calls=calls,
            transport=f"{index:032x}",
        )
        for index in range(first, first + count)
    )


class TestTheCountersExistBeforeAnythingIsEvicted:
    """A structure absent until it fires cannot be read as "nothing happened"."""

    def test_a_fresh_registry_reports_zeroed_counters(self) -> None:
        counters = _registry(_Clock()).eviction_counters()
        assert counters["evictions_total"] == {"ttl": 0, "capacity": 0}
        assert counters["evictions_bearing_total"] == 0

    def test_a_fresh_registry_reports_its_occupancy_and_capacity(self) -> None:
        counters = _registry(_Clock()).eviction_counters()
        assert counters["occupancy"] == 0
        assert counters["capacity"] == MAX_ACTIVE_CONVERSATIONS


class TestEvictionsAreCountedByCause:
    def test_the_capacity_cut_is_counted_as_capacity(self) -> None:
        registry = _registry(_Clock())
        registry.record_observations(_transports(MAX_ACTIVE_CONVERSATIONS))
        assert registry.eviction_counters()["evictions_total"]["capacity"] == 0

        registry.record_observations(_transports(8, first=MAX_ACTIVE_CONVERSATIONS))
        counters = registry.eviction_counters()
        assert counters["evictions_total"]["capacity"] == 8
        assert counters["evictions_total"]["ttl"] == 0
        assert counters["occupancy"] == MAX_ACTIVE_CONVERSATIONS

    def test_an_expiry_is_counted_as_ttl_and_never_as_capacity(self) -> None:
        """Expiry is the design working, not a loss -- it must not read as one."""
        clock = _Clock()
        registry = _registry(clock)
        registry.record_observations(_transports(3))

        clock.advance(ACTIVITY_TTL_SECONDS + 1)
        registry.record_observations(_transports(1, first=900))

        counters = registry.eviction_counters()
        assert counters["evictions_total"]["ttl"] == 3
        assert counters["evictions_total"]["capacity"] == 0
        assert counters["occupancy"] == 1


class TestABearingResidueIsCountedSeparately:
    """The number the ticket is actually about: measurement thrown away."""

    def test_a_capacity_eviction_of_rows_carrying_calls_is_bearing(self) -> None:
        registry = _registry(_Clock())
        registry.record_observations(_transports(MAX_ACTIVE_CONVERSATIONS, calls=7))
        registry.record_observations(_transports(4, first=MAX_ACTIVE_CONVERSATIONS, calls=1))

        counters = registry.eviction_counters()
        assert counters["evictions_total"]["capacity"] == 4
        assert counters["evictions_bearing_total"] == 4

    def test_a_ttl_expiry_is_NOT_counted_as_bearing(self) -> None:
        """A row silent for the whole TTL is stale by design, not a measurement lost.

        Conflating the two would make the reopening condition fire on healthy
        traffic, which is the fastest way to make an alarm unread.
        """
        clock = _Clock()
        registry = _registry(clock)
        registry.record_observations(_transports(5, calls=9))

        clock.advance(ACTIVITY_TTL_SECONDS + 1)
        registry.record_observations(_transports(1, first=900))

        counters = registry.eviction_counters()
        assert counters["evictions_total"]["ttl"] == 5
        assert counters["evictions_bearing_total"] == 0


class TestOccupancyIsTheReopeningConditionsOtherHalf:
    def test_occupancy_tracks_the_live_row_count(self) -> None:
        registry = _registry(_Clock())
        registry.record_observations(_transports(12))
        assert registry.eviction_counters()["occupancy"] == 12

    def test_occupancy_never_exceeds_the_capacity_it_is_read_against(self) -> None:
        registry = _registry(_Clock())
        registry.record_observations(_transports(MAX_ACTIVE_CONVERSATIONS))
        registry.record_observations(_transports(20, first=500))
        counters = registry.eviction_counters()
        assert counters["occupancy"] == counters["capacity"] == MAX_ACTIVE_CONVERSATIONS


class TestTheCountersAreMonotonic:
    def test_a_second_pressure_wave_adds_instead_of_replacing(self) -> None:
        registry = _registry(_Clock())
        registry.record_observations(_transports(MAX_ACTIVE_CONVERSATIONS))
        registry.record_observations(_transports(3, first=100))
        registry.record_observations(_transports(3, first=200))
        assert registry.eviction_counters()["evictions_total"]["capacity"] == 6


class TestTheCountersReachTheOnlyExposureSite:
    """A counter nobody can read is a comment with extra steps.

    Asserted NARROWLY, on purpose. Driving `_handle_metrics` end to end would
    mean rebuilding that handler's entire collector contract in this module --
    five unrelated keys and two awaited collaborators before reaching our one
    line -- and the test would then break on every unrelated key added to it.
    What is pinned here is the wiring that can actually go wrong: the server
    reports the counters OF THE REGISTRY IT WAS GIVEN, under the documented name.
    The end-to-end path is verified by reading live `/metrics` after a restart,
    and that reading is in the report rather than faked here.
    """

    @staticmethod
    def _server(registry: ClientActivityRegistry) -> object:
        from unittest.mock import MagicMock

        from brain_v42.metrics.server import MetricsServer

        return MetricsServer(MagicMock(), MagicMock(), host="127.0.0.1", codex_registry=registry)

    def test_the_server_reports_the_counters_of_the_registry_it_was_given(self) -> None:
        registry = ClientActivityRegistry(secret=SECRET, clock=_Clock())
        registry.record_observations(_transports(MAX_ACTIVE_CONVERSATIONS + 5))
        server = self._server(registry)

        served = server._codex_registry.eviction_counters()  # type: ignore[attr-defined]

        assert served["evictions_total"]["capacity"] == 5
        assert served is not registry.eviction_counters(), "the reader must return a copy"
        assert served == registry.eviction_counters()

    def test_the_counter_payload_carries_exactly_the_documented_keys(self) -> None:
        """red-monitor reads this shape; an extra key is a contract change."""
        counters = ClientActivityRegistry(secret=SECRET).eviction_counters()
        assert set(counters) == {
            "evictions_total",
            "evictions_bearing_total",
            "occupancy",
            "capacity",
        }

    def test_the_totals_are_a_copy_and_not_the_live_dict(self) -> None:
        """A caller mutating the returned dict must not silence the instrument."""
        registry = ClientActivityRegistry(secret=SECRET, clock=_Clock())
        counters = registry.eviction_counters()
        counters["evictions_total"]["capacity"] = 999  # type: ignore[index]
        assert registry.eviction_counters()["evictions_total"]["capacity"] == 0

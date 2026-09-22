"""In-process closed catalogue that measures only verified fact targets."""

from __future__ import annotations

import asyncio
import inspect
import time
import traceback
from collections.abc import Awaitable, Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, TypeVar, cast
from uuid import uuid4

import structlog

from brain_v42.facts.canonical import ValueTooLargeError
from brain_v42.facts.model import (
    FactTarget,
    Identity,
    IdentityUnreadableError,
    Measured,
    Measurement,
    Unreadable,
    validate_fact_name,
    with_source_kind,
)
from brain_v42.facts.probe import (
    FactDescriptor,
    Probe,
    SourceFactory,
    SourceSession,
    check_value_schema,
)

_NEGATIVE_CACHE_SECONDS: Final = 30
_MAX_BATCH_NAMES: Final = 32
_logger = structlog.get_logger(__name__)
_Result = TypeVar("_Result")
_BucketKey = TypeVar("_BucketKey", bound=Hashable)


class DuplicateFactError(ValueError):
    """Raised when two probes attempt to own the same catalogue name."""


class RegistryFrozenError(RuntimeError):
    """Raised when composition tries to mutate the published catalogue."""


class RegistryClosedError(RuntimeError):
    """Raised when a shutdown registry is asked to start another measurement."""


class UnknownFactError(KeyError):
    """Raised when a caller requests a name absent from the closed catalogue."""


class UnverifiableTargetError(ValueError):
    """Raised when composition lacks an independent source identity for a target."""


@dataclass(frozen=True, slots=True)
class RefreshBudget:
    """Bound forced refreshes so one caller cannot exhaust a shared target."""

    per_fact_per_minute: int = 10
    per_target_per_minute: int = 30

    def __post_init__(self) -> None:
        """Fail early because a zero-sized bucket makes every refresh impossible."""
        if (
            type(self.per_fact_per_minute) is not int
            or self.per_fact_per_minute < 1
            or type(self.per_target_per_minute) is not int
            or self.per_target_per_minute < 1
        ):
            raise ValueError("refresh budget limits must be positive integers")


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_mono: float


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    measurement: Measurement
    measured_mono: float


def _utcnow() -> datetime:
    """Provide a UTC publication instant while all expiry maths stays monotonic."""
    return datetime.now(UTC)


def _whole_seconds(value: timedelta, *, field: str) -> int:
    """Keep descriptor bounds representable by the immutable public result types."""
    seconds = value.total_seconds()
    if seconds < 0 or not seconds.is_integer():
        raise ValueError(f"{field} must be a non-negative whole number of seconds")
    return int(seconds)


class FactRegistry:
    """Own probe tasks, cache outcomes, and reject values from an unexpected source."""

    def __init__(
        self,
        *,
        sources: Mapping[FactTarget, SourceFactory],
        expected: Mapping[FactTarget, Identity],
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = _utcnow,
        concurrency: int = 4,
        queue_timeout: timedelta = timedelta(seconds=2),
        refresh_budget: RefreshBudget = RefreshBudget(),  # noqa: B008
    ) -> None:
        """Keep mutable scheduling state private so registry ownership is unambiguous."""
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError("concurrency must be a positive integer")
        self._queue_timeout_seconds = _whole_seconds(queue_timeout, field="queue_timeout")
        if self._queue_timeout_seconds < 1:
            raise ValueError("queue_timeout must be at least one second")
        self._sources = dict(sources)
        self._expected = dict(expected)
        self._monotonic = monotonic
        self._wall = wall
        self._refresh_budget = refresh_budget
        self._semaphore = asyncio.Semaphore(concurrency)
        self._concurrency = concurrency
        self._probes: dict[str, Probe] = {}
        self._descriptors: dict[str, FactDescriptor] = {}
        self._cache: dict[str, _CacheEntry] = {}
        self._inflight: dict[str, asyncio.Task[Measurement]] = {}
        self._fact_buckets: dict[str, _Bucket] = {}
        self._target_buckets: dict[FactTarget, _Bucket] = {}
        self._frozen = False
        self._closed = False
        self._refusals: dict[str, str] = {}
        self._disabled: dict[str, str] = {}
        # This seam preserves asyncio's normal timeout behaviour in production while
        # allowing deterministic capacity tests without sleeping a real clock.
        self._wait_for: Any = asyncio.wait_for

    def register(self, probe: Probe) -> None:
        """Admit only one fully declared probe whose source can be verified independently."""
        if self._frozen:
            raise RegistryFrozenError("fact registry is frozen")
        name = validate_fact_name(probe.name)
        if name in self._probes:
            raise DuplicateFactError(name)
        if probe.target not in self._sources or probe.target not in self._expected:
            raise UnverifiableTargetError(
                f"target {probe.target.value!r} is not independently verifiable"
            )
        if type(probe.definition_version) is not int or probe.definition_version < 1:
            raise ValueError("definition_version must be a positive integer")
        ttl_seconds = _whole_seconds(probe.ttl, field="ttl")
        timeout_seconds = _whole_seconds(probe.timeout, field="timeout")
        if timeout_seconds < 1:
            raise ValueError("timeout must be at least one second")
        if probe.briefing and timeout_seconds > 3:
            raise ValueError("briefing probes must have a timeout of at most 3 seconds")
        if not isinstance(probe.policies, Mapping) or not isinstance(probe.value_schema, Mapping):
            raise ValueError("policies and value_schema must be mappings")
        for value in probe.policies.values():
            if type(value) is not int:
                raise ValueError("policy values must be integers")
        for schema in probe.value_schema.values():
            if schema not in {"int", "bool", "string", "null|int"}:
                raise ValueError("value_schema has an unsupported value type")

        self._probes[name] = probe
        self._descriptors[name] = FactDescriptor(
            name=name,
            definition_version=probe.definition_version,
            target=probe.target,
            ttl_seconds=ttl_seconds,
            timeout_seconds=timeout_seconds,
            queue_timeout_seconds=self._queue_timeout_seconds,
            deadline_seconds=self._queue_timeout_seconds + timeout_seconds,
            briefing=probe.briefing,
            policies=probe.policies,
            value_schema=probe.value_schema,
        )

    def note_refusal(self, name: str, reason: str) -> None:
        """Record a probe the composition root could not register, for the briefing.

        An empty catalogue must never be silent: the reader learns which fact
        is missing and why, instead of inferring it from an absent line.
        """
        if self._frozen:
            raise RegistryFrozenError("fact registry is frozen")
        self._refusals[validate_fact_name(name)] = reason

    def refusals(self) -> dict[str, str]:
        """The probes refused at registration, by name, with the reason."""
        return dict(self._refusals)

    def disable(self, name: str, reason: str) -> None:
        """Narrow a registered fact after freeze without installing a reader.

        ``freeze`` exists so runtime callers cannot install arbitrary readers.
        Disabling installs nothing and only narrows what the registry will
        answer, so it cannot become an escalation path.
        """
        self.describe(name)
        self._disabled[name] = reason

    def disabled(self) -> dict[str, str]:
        """The registered facts disabled after composition, by name and reason."""
        return dict(self._disabled)

    def freeze(self) -> None:
        """Close composition so runtime callers cannot install arbitrary readers."""
        self._frozen = True

    def names(self) -> tuple[str, ...]:
        """Return names in registration order for stable operator-facing output."""
        return tuple(self._probes)

    def describe(self, name: str) -> FactDescriptor:
        """Return immutable declared metadata without starting a probe."""
        try:
            return self._descriptors[name]
        except KeyError as exc:
            raise UnknownFactError(name) from exc

    def expected_identity(self, target: FactTarget) -> Identity | None:
        """Return independently configured target identity without opening its source."""
        return self._expected.get(target)

    def cached(self, name: str) -> Measurement | None:
        """Return the stored observation without changing cache age or starting a probe."""
        self.describe(name)
        entry = self._cache.get(name)
        return None if entry is None else entry.measurement

    def briefing_names(self) -> tuple[str, ...]:
        """Return only cheap, explicitly declared briefing facts in catalogue order."""
        return tuple(name for name, descriptor in self._descriptors.items() if descriptor.briefing)

    def cached_age_seconds(self, name: str) -> float | None:
        """Monotonic age of the cached reading of a fact, or None when nothing is cached.

        A renderer that wants to say "mesuré il y a 3 min" asks here rather
        than subtracting `measured_at` from a wall clock: the wall clock may
        step, the monotonic one does not.
        """
        self.describe(name)
        entry = self._cache.get(name)
        if entry is None:
            return None
        return max(0.0, self._monotonic() - entry.measured_mono)

    async def measure(self, name: str, *, max_age: timedelta | None = None) -> Measurement:
        """Serve a valid immutable cache entry or join/start exactly one producer task."""
        if self._closed:
            raise RegistryClosedError("fact registry is closed")
        descriptor = self.describe(name)
        disabled_reason = self._disabled.get(name)
        if disabled_reason is not None:
            return self._unreadable(
                descriptor,
                disabled_reason,
                where=name,
                duration_ms=0,
            )
        max_age_seconds = self._max_age_seconds(max_age)
        cached = self._cache.get(name)
        now = self._monotonic()
        if cached is not None and self._cache_is_fresh(cached, descriptor, max_age_seconds, now):
            return with_source_kind(cached.measurement, "cache")

        task = self._inflight.get(name)
        if task is None:
            forced = max_age_seconds is not None and max_age_seconds < descriptor.ttl_seconds
            if forced and not self._charge_refresh(name, descriptor.target, now):
                return self._unreadable(descriptor, "refresh_budget", duration_ms=0)
            task = asyncio.create_task(self._run(name, forced=forced), name=f"fact:{name}")
            self._inflight[name] = task

            def cleanup(done: asyncio.Task[Measurement], *, fact: str = name) -> None:
                self._cleanup_inflight(fact, done)

            task.add_done_callback(cleanup)
        return await asyncio.shield(task)

    async def measure_many(
        self,
        names: Sequence[str],
        *,
        max_age: timedelta | None = None,
        budget: timedelta | None = None,
    ) -> dict[str, Measurement]:
        """Bound batch admission so a briefing cannot create an unbounded queue of producers."""
        if self._closed:
            raise RegistryClosedError("fact registry is closed")
        listed = tuple(names)
        if len(listed) > _MAX_BATCH_NAMES or len(set(listed)) != len(listed):
            raise ValueError("names must contain at most 32 distinct facts")
        if budget is not None and budget.total_seconds() < 0:
            raise ValueError("budget must be non-negative")
        # Validate all names and age before scheduling even one task.
        for name in listed:
            try:
                self.describe(name)
            except UnknownFactError as exc:
                raise ValueError(f"unregistered fact {name!r}") from exc
        self._max_age_seconds(max_age)

        started_mono = self._monotonic()
        budget_seconds = None if budget is None else budget.total_seconds()
        pending = list(listed)
        active: dict[asyncio.Task[Measurement], str] = {}
        results: dict[str, Measurement] = {}

        try:
            while pending or active:
                while pending and len(active) < self._concurrency:
                    if (
                        budget_seconds is not None
                        and self._monotonic() - started_mono >= budget_seconds
                    ):
                        break
                    name = pending.pop(0)
                    task = asyncio.create_task(self.measure(name, max_age=max_age))
                    active[task] = name
                if not active:
                    break
                done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    name = active.pop(task)
                    results[name] = await task
        except BaseException:
            # The batch's own reader tasks must not outlive a cancelled caller;
            # the registry-owned runs they were shielding survive untouched.
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            raise

        for name in pending:
            results[name] = self._unreadable(
                self._descriptors[name], "briefing_budget", duration_ms=0
            )
        return {name: results[name] for name in listed}

    async def aclose(self) -> None:
        """Finish cancellation locally before a source factory can be disposed by its owner."""
        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._inflight.values())
        # Let tasks created by readers enter their own cancellation boundary.  A
        # task cancelled before its first instruction cannot turn cancellation
        # into the required Unreadable result itself.
        if tasks:
            await asyncio.sleep(0)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _max_age_seconds(self, max_age: timedelta | None) -> float | None:
        """Separate a cache bypass from a normal TTL read before any producer is touched."""
        if max_age is None:
            return None
        seconds = max_age.total_seconds()
        if seconds < 0:
            raise ValueError("max_age must be non-negative")
        return seconds

    def _cache_is_fresh(
        self,
        entry: _CacheEntry,
        descriptor: FactDescriptor,
        max_age_seconds: float | None,
        now: float,
    ) -> bool:
        """Use only monotonic elapsed time so published wall-clock changes cannot prolong trust."""
        if max_age_seconds == 0:
            return False
        ttl = descriptor.ttl_seconds
        if isinstance(entry.measurement, Unreadable):
            ttl = min(ttl, _NEGATIVE_CACHE_SECONDS)
        bound = ttl if max_age_seconds is None else min(ttl, max_age_seconds)
        return now - entry.measured_mono <= bound

    def _charge_refresh(self, name: str, target: FactTarget, now: float) -> bool:
        """Spend both independent buckets atomically only for a newly forced probe run."""
        fact_bucket = self._refill_bucket(
            self._fact_buckets, name, self._refresh_budget.per_fact_per_minute, now
        )
        target_bucket = self._refill_bucket(
            self._target_buckets, target, self._refresh_budget.per_target_per_minute, now
        )
        if fact_bucket.tokens < 1 or target_bucket.tokens < 1:
            return False
        fact_bucket.tokens -= 1
        target_bucket.tokens -= 1
        return True

    def _refund_refresh(self, name: str, target: FactTarget) -> None:
        """Give back the tokens of a forced run the registry itself refused."""
        fact_bucket = self._fact_buckets.get(name)
        if fact_bucket is not None:
            limit = float(self._refresh_budget.per_fact_per_minute)
            fact_bucket.tokens = min(limit, fact_bucket.tokens + 1)
        target_bucket = self._target_buckets.get(target)
        if target_bucket is not None:
            limit = float(self._refresh_budget.per_target_per_minute)
            target_bucket.tokens = min(limit, target_bucket.tokens + 1)

    @staticmethod
    def _refill_bucket(
        buckets: dict[_BucketKey, _Bucket], key: _BucketKey, limit: int, now: float
    ) -> _Bucket:
        """Refill continuously so a full minute is not an accidental fixed window."""
        bucket = buckets.get(key)
        if bucket is None:
            bucket = _Bucket(float(limit), now)
            buckets[key] = bucket
        else:
            elapsed = max(0.0, now - bucket.updated_mono)
            bucket.tokens = min(float(limit), bucket.tokens + elapsed * limit / 60)
            bucket.updated_mono = now
        return bucket

    async def _run(self, name: str, *, forced: bool) -> Measurement:
        """Run one producer under capacity and turn every untrusted outcome into data.

        Nothing but a `BaseException` that is not an `Exception` leaves this
        task: a reader must receive a `Measurement`, never a third state.
        """
        descriptor = self._descriptors[name]
        probe = self._probes[name]
        result: Measurement
        cache_it = True
        try:
            try:
                await self._await_timeout(self._semaphore.acquire(), self._queue_timeout_seconds)
            except TimeoutError:
                # A refusal of the registry, not a verdict on the target: not
                # cached, and the forced refresh it never spent is refunded.
                result = self._unreadable(descriptor, "capacity_timeout", duration_ms=0)
                self._record_failure(result)
                cache_it = False
                if forced:
                    self._refund_refresh(name, descriptor.target)
            else:
                # The duration is the probe's, measured once the slot is held;
                # the wait for capacity is not a property of the target.
                started_mono = self._monotonic()
                try:
                    result = await self._run_with_slot(probe, descriptor, started_mono)
                except Exception as exc:
                    result = self._unreadable(
                        descriptor,
                        "probe_error",
                        where=self._exception_where(exc, probe),
                        duration_ms=self._duration_ms(started_mono),
                    )
                    self._record_failure(result)
                finally:
                    self._semaphore.release()
        except asyncio.CancelledError:
            result = self._unreadable(descriptor, "timeout", duration_ms=0)
            self._record_failure(result)
        if cache_it:
            self._cache[name] = _CacheEntry(result, self._monotonic())
        return result

    async def _run_with_slot(
        self, probe: Probe, descriptor: FactDescriptor, started_mono: float
    ) -> Measurement:
        """Keep opening, measuring, and identity comparison in one hard timeout budget."""
        try:
            try:
                value, identity = await self._await_timeout(
                    self._open_measure_and_identify(self._sources[descriptor.target], probe),
                    descriptor.timeout_seconds,
                )
            except TimeoutError:
                result = self._unreadable(
                    descriptor, "timeout", duration_ms=self._duration_ms(started_mono)
                )
                self._record_failure(result)
                return result
        except asyncio.CancelledError:
            raise
        except _IdentityFailure:
            result = self._unreadable(
                descriptor,
                "identity_unreadable",
                duration_ms=self._duration_ms(started_mono),
            )
            self._record_failure(result)
            return result
        except _SourceOpenFailure as exc:
            # The probe never ran: the diagnostic must not say it did.
            cause = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
            result = self._unreadable(
                descriptor,
                "probe_error",
                where=f"{type(cause).__name__} in source_open"[:120],
                duration_ms=self._duration_ms(started_mono),
            )
            self._record_failure(result)
            return result
        except Exception as exc:
            result = self._unreadable(
                descriptor,
                "probe_error",
                where=self._exception_where(exc, probe),
                duration_ms=self._duration_ms(started_mono),
            )
            self._record_failure(result)
            return result

        if identity != self._expected[descriptor.target]:
            result = self._unreadable(
                descriptor, "target_mismatch", duration_ms=self._duration_ms(started_mono)
            )
            self._record_failure(result, identity=identity)
            return result
        try:
            check_value_schema(value, descriptor.value_schema)
        except ValueError:
            result = self._unreadable(
                descriptor,
                "value_not_canonical",
                where="ValueError in check_value_schema",
                duration_ms=self._duration_ms(started_mono),
            )
            self._record_failure(result)
            return result
        try:
            return Measured.from_value(
                fact=descriptor.name,
                definition_version=descriptor.definition_version,
                target=descriptor.target,
                source=identity,
                value=value,
                observation_id=uuid4(),
                measured_at=self._wall(),
                duration_ms=self._duration_ms(started_mono),
                ttl_seconds=descriptor.ttl_seconds,
            )
        except ValueTooLargeError:
            result = self._unreadable(
                descriptor, "value_too_large", duration_ms=self._duration_ms(started_mono)
            )
            self._record_failure(result)
            return result
        except ValueError:
            result = self._unreadable(
                descriptor,
                "value_not_canonical",
                where="ValueError in Measured.from_value",
                duration_ms=self._duration_ms(started_mono),
            )
            self._record_failure(result)
            return result

    async def _measure_and_identify(
        self, probe: Probe, source: SourceSession
    ) -> tuple[Mapping[str, object], Identity]:
        """Run identity after the value so both remain inside the source's transaction."""
        try:
            value = await probe.measure(source)
        except IdentityUnreadableError as exc:
            raise _IdentityFailure(exc) from exc
        except Exception as exc:
            raise _ProbeFailure(exc) from exc
        try:
            identity = await source.identity()
        except Exception as exc:
            raise _IdentityFailure(exc) from exc
        return value, identity

    async def _open_measure_and_identify(
        self, factory: SourceFactory, probe: Probe
    ) -> tuple[Mapping[str, object], Identity]:
        """Include source opening and cleanup in the same deadline as the probe itself."""
        context = factory()
        try:
            source = await context.__aenter__()
        except Exception as exc:
            raise _SourceOpenFailure(exc) from exc
        try:
            outcome = await self._measure_and_identify(probe, source)
        except BaseException:
            await context.__aexit__(*_exc_info())
            raise
        if await context.__aexit__(None, None, None):  # pragma: no cover - never swallows
            pass
        return outcome

    async def _await_timeout(self, awaitable: Awaitable[_Result], seconds: float) -> _Result:
        """Centralise the timeout seam while asyncio supplies cancellation-safe producers."""
        return cast(_Result, await self._wait_for(awaitable, seconds))

    def _unreadable(
        self,
        descriptor: FactDescriptor,
        error_code: str,
        *,
        where: str | None = None,
        duration_ms: int,
    ) -> Unreadable:
        """Build a fresh failure observation because a refusal is still an observed outcome."""
        return Unreadable(
            fact=descriptor.name,
            definition_version=descriptor.definition_version,
            target=descriptor.target,
            error_code=error_code,
            where=where,
            observation_id=uuid4(),
            measured_at=self._wall(),
            duration_ms=duration_ms,
            ttl_seconds=descriptor.ttl_seconds,
            source_kind="probe",
        )

    def _duration_ms(self, started_mono: float) -> int:
        """Derive a non-negative duration without ever consulting the publication clock."""
        return max(0, int((self._monotonic() - started_mono) * 1000))

    def _record_failure(self, result: Unreadable, *, identity: Identity | None = None) -> None:
        """Log one redacted warning from the producer, never once for each waiting reader."""
        fields: dict[str, object] = {
            "fact": result.fact,
            "error_code": result.error_code,
            "where": result.where,
        }
        if identity is not None:
            fields.update(identity.as_dict())
        _logger.warning("fact_measurement_failed", **fields)

    @staticmethod
    def _exception_where(exc: Exception, probe: Probe) -> str:
        """Name a stable probe frame without accidentally logging an exception message."""
        if isinstance(exc, _ProbeFailure):
            exc = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
        if isinstance(exc, _IdentityFailure):
            return "identity unreadable"
        owner = type(probe).__name__
        function = f"{owner}.measure"
        module = inspect.getmodule(probe.measure)
        module_file = getattr(module, "__file__", None)
        if module_file is not None:
            # walk_tb reads code objects only: no linecache, no file IO on the
            # event loop for every failed run.
            frames = [frame for frame, _ in traceback.walk_tb(exc.__traceback__)]
            for frame in reversed(frames):
                if frame.f_code.co_filename == module_file:
                    function = f"{owner}.{frame.f_code.co_name}"
                    break
        return f"{type(exc).__name__} in {function}"[:120]

    def _cleanup_inflight(self, name: str, task: asyncio.Task[Measurement]) -> None:
        """Do not let an old task callback delete a newer task for the same fact."""
        if self._inflight.get(name) is task:
            self._inflight.pop(name, None)


def _exc_info() -> tuple[type[BaseException] | None, BaseException | None, Any]:
    """The current exception triple, for a manual ``__aexit__`` during unwinding."""
    import sys

    return sys.exc_info()


class _SourceOpenFailure(Exception):
    """The source could not be opened: the probe never ran, and the diagnostic says so."""


class _ProbeFailure(Exception):
    """Keep probe failures distinct from identity failures without exposing their messages."""


class _IdentityFailure(Exception):
    """Keep identity failures fail-closed without treating them as a measured value failure."""

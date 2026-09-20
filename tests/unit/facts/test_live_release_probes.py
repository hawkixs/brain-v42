"""Contracts for facts measured from the immutable release running this process."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from brain_v42.facts.model import FactTarget, Measured, ReleaseIdentity, Unreadable
from brain_v42.facts.probe import check_value_schema
from brain_v42.facts.probes.alembic_head_shipped import AlembicHeadShippedProbe
from brain_v42.facts.probes.live_release_sha import LiveReleaseShaProbe
from brain_v42.facts.registry import FactRegistry
from brain_v42.facts.sources import ReleaseSourceFactory, ReleaseSourceSession

_SHA = "b4f7194d84c153dad11ee249ec9eca8951056651"
_VERSION = "0.6.0"


class Clock:
    """Keep cache-age assertions deterministic without waiting for a release TTL."""

    def __init__(self) -> None:
        self.mono = 0.0

    def monotonic(self) -> float:
        return self.mono

    def now(self) -> datetime:
        return datetime(2026, 9, 20, tzinfo=UTC)


def _release_path(tmp_path: Path) -> Path:
    package_file = tmp_path / "releases" / _SHA / "brain_v42" / "__init__.py"
    package_file.parent.mkdir(parents=True)
    package_file.touch()
    return package_file


def _registry(
    tmp_path: Path, *, expected_sha: str = _SHA, development: bool = False
) -> FactRegistry:
    package_file = tmp_path / "checkout" / "brain_v42" / "__init__.py"
    if not development:
        package_file = _release_path(tmp_path)
    else:
        package_file.parent.mkdir(parents=True)
        package_file.touch()
    clock = Clock()
    result = FactRegistry(
        sources={
            FactTarget.LIVE_RELEASE: ReleaseSourceFactory(
                package_file=package_file, version=lambda: _VERSION
            )
        },
        expected={FactTarget.LIVE_RELEASE: ReleaseIdentity(expected_sha, _VERSION)},
        monotonic=clock.monotonic,
        wall=clock.now,
    )
    result.register(LiveReleaseShaProbe())
    result.register(AlembicHeadShippedProbe(head=lambda: "054"))
    return result


async def test_live_release_sha_probe_publishes_the_measured_release_identity(
    tmp_path: Path,
) -> None:
    probe = LiveReleaseShaProbe()
    value = await probe.measure(ReleaseSourceSession(_release_path(tmp_path), lambda: _VERSION))

    assert value == {"release_sha": _SHA, "package_version": _VERSION}
    check_value_schema(value, probe.value_schema)
    assert (
        probe.name,
        probe.definition_version,
        probe.target,
        probe.ttl,
        probe.timeout,
        probe.briefing,
        probe.policies,
        probe.value_schema,
    ) == (
        "live_release_sha",
        1,
        FactTarget.LIVE_RELEASE,
        timedelta(days=3650),
        timedelta(seconds=1),
        True,
        {},
        {"release_sha": "string", "package_version": "string"},
    )


async def test_alembic_head_shipped_probe_uses_its_injected_head_reader() -> None:
    probe = AlembicHeadShippedProbe(head=lambda: "054")

    assert await probe.measure(object()) == {"revision": "054"}  # type: ignore[arg-type]
    assert (
        probe.name,
        probe.definition_version,
        probe.target,
        probe.ttl,
        probe.timeout,
        probe.briefing,
        probe.policies,
        probe.value_schema,
    ) == (
        "alembic_head_shipped",
        1,
        FactTarget.LIVE_RELEASE,
        timedelta(days=3650),
        timedelta(seconds=1),
        True,
        {},
        {"revision": "string"},
    )


async def test_alembic_head_shipped_probe_propagates_a_strict_reader_failure() -> None:
    def broken_head() -> str:
        raise ValueError("revision chain has zero heads")

    with pytest.raises(ValueError, match="zero heads"):
        await AlembicHeadShippedProbe(head=broken_head).measure(object())  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["live_release_sha", "alembic_head_shipped"])
async def test_live_release_probes_measure_once_then_serve_the_process_lifetime_cache(
    tmp_path: Path, name: str
) -> None:
    registry = _registry(tmp_path)

    first = await registry.measure(name)
    second = await registry.measure(name)

    assert isinstance(first, Measured)
    assert first.source_kind == "probe"
    assert isinstance(second, Measured)
    assert second.source_kind == "cache"
    assert second.observation_id == first.observation_id


@pytest.mark.parametrize("name", ["live_release_sha", "alembic_head_shipped"])
async def test_live_release_probes_refuse_another_declared_release_sha(
    tmp_path: Path, name: str
) -> None:
    result = await _registry(tmp_path, expected_sha="a" * 40).measure(name)

    assert isinstance(result, Unreadable)
    assert result.error_code == "target_mismatch"


@pytest.mark.parametrize("name", ["live_release_sha", "alembic_head_shipped"])
async def test_live_release_probes_refuse_a_development_package_path(
    tmp_path: Path, name: str
) -> None:
    result = await _registry(tmp_path, development=True).measure(name)

    assert isinstance(result, Unreadable)
    assert result.error_code == "identity_unreadable"


@pytest.mark.parametrize("name", ["live_release_sha", "alembic_head_shipped"])
async def test_live_release_probes_refuse_the_same_sha_with_another_declared_version(
    tmp_path: Path, name: str
) -> None:
    """The identity is the pair: a comparison on the SHA alone would let a
    re-installed package of another version pass as the declared release."""
    package_file = _release_path(tmp_path)
    clock = Clock()
    registry = FactRegistry(
        sources={
            FactTarget.LIVE_RELEASE: ReleaseSourceFactory(
                package_file=package_file, version=lambda: _VERSION
            )
        },
        expected={FactTarget.LIVE_RELEASE: ReleaseIdentity(_SHA, _VERSION + ".post1")},
        monotonic=clock.monotonic,
        wall=clock.now,
    )
    registry.register(LiveReleaseShaProbe())
    registry.register(AlembicHeadShippedProbe(head=lambda: "054"))

    result = await registry.measure(name)

    assert isinstance(result, Unreadable)
    assert result.error_code == "target_mismatch"

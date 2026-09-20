"""The composition root of the catalogue: build it once, freeze it, hand it over.

Nothing at runtime adds a fact. This module is where every probe is
registered, where the operator's declared identities meet the registry, and
where a target the registry cannot verify is refused — out loud, in the
journal, and without taking the service down: a Brain without production
facts is degraded and says so; a Brain measuring an unverified database would
be lying. Absent or refused, the declaration leaves the catalogue empty of
production facts (spec §5.3 rule 4).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from brain_v42.facts.model import (
    FactTarget,
    HostIdentity,
    Identity,
    ReleaseIdentity,
    SourceIdentity,
)
from brain_v42.facts.probe import Probe
from brain_v42.facts.probes.alembic_head_shipped import AlembicHeadShippedProbe
from brain_v42.facts.probes.dream_killswitches_declared import DreamKillswitchesDeclaredProbe
from brain_v42.facts.probes.dream_last_night import DreamLastNightProbe
from brain_v42.facts.probes.graph_projection_lag import GraphProjectionLagProbe
from brain_v42.facts.probes.live_release_sha import LiveReleaseShaProbe
from brain_v42.facts.registry import FactRegistry, UnverifiableTargetError
from brain_v42.facts.sources import HostSourceFactory, PostgresSourceFactory, ReleaseSourceFactory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)


def _catalogue() -> tuple[Probe, ...]:
    """The closed catalogue, in the order the briefing renders it."""
    # Revision 6 order: graph_projection_lag, alembic_head, live_release_sha,
    # alembic_head_shipped, dream_killswitches_declared, dream_last_night.
    return (
        GraphProjectionLagProbe(),
        # Task T9 inserts AlembicHeadProbe here.
        LiveReleaseShaProbe(),
        AlembicHeadShippedProbe(),
        DreamKillswitchesDeclaredProbe(),
        DreamLastNightProbe(),
    )


def build_fact_registry(
    declared_production_identity: object,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    declared_live_release_identity: object = None,
    declared_host_identity: object = None,
    host_root: Path | None = None,
) -> FactRegistry:
    """Build and freeze the registry from what the operator declared.

    `declared_production_identity` is the mapping `Settings.facts_production_identity()`
    returns — `None` when undeclared. The deep validation happens here, in
    the model: a declaration the model refuses is logged and the production
    target stays unverifiable, so every production probe is refused at
    registration and the catalogue closes without them.
    """
    expected: dict[FactTarget, Identity] = {}
    _declare_identity(
        expected,
        FactTarget.PRODUCTION,
        declared_production_identity,
        SourceIdentity,
        "BRAIN_FACTS_PRODUCTION_IDENTITY",
    )
    _declare_identity(
        expected,
        FactTarget.LIVE_RELEASE,
        declared_live_release_identity,
        ReleaseIdentity,
        "BRAIN_FACTS_LIVE_RELEASE_IDENTITY",
    )
    _declare_identity(
        expected,
        FactTarget.HOST,
        declared_host_identity,
        HostIdentity,
        "BRAIN_FACTS_HOST_IDENTITY",
    )

    registry = FactRegistry(
        sources={
            FactTarget.PRODUCTION: PostgresSourceFactory(session_factory),
            FactTarget.LIVE_RELEASE: ReleaseSourceFactory(),
            FactTarget.HOST: HostSourceFactory(
                root=host_root
                if host_root is not None
                else Path.home() / ".config" / "systemd" / "user"
            ),
        },
        expected=expected,
    )
    for probe in _catalogue():
        try:
            registry.register(probe)
        except UnverifiableTargetError as exc:
            logger.warning("facts.probe_not_registered", fact=probe.name, error=str(exc))
            registry.note_refusal(probe.name, "unverifiable_target")
    registry.freeze()
    logger.info("facts.registry_frozen", facts=list(registry.names()))
    return registry


def _declare_identity(
    expected: dict[FactTarget, Identity],
    target: FactTarget,
    declared: object,
    identity_type: type[SourceIdentity] | type[ReleaseIdentity] | type[HostIdentity],
    variable: str,
) -> None:
    """Accept only one complete operator declaration, otherwise make refusal observable."""
    event = f"facts.{target.value}_identity"
    if declared is None:
        logger.warning(
            f"{event}_undeclared", hint=f"set {variable} to register {target.value} facts"
        )
    elif not isinstance(declared, Mapping):
        logger.warning(f"{event}_refused", error="not a mapping")
    else:
        try:
            expected[target] = identity_type.from_mapping(declared)
        except ValueError as exc:
            logger.warning(f"{event}_refused", error=str(exc))

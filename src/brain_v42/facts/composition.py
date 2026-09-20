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
from typing import TYPE_CHECKING

import structlog

from brain_v42.facts.model import FactTarget, SourceIdentity
from brain_v42.facts.probe import Probe
from brain_v42.facts.probes.graph_projection_lag import GraphProjectionLagProbe
from brain_v42.facts.registry import FactRegistry, UnverifiableTargetError
from brain_v42.facts.sources import PostgresSourceFactory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)


def _catalogue() -> tuple[Probe, ...]:
    """The closed catalogue, in the order the briefing renders it."""
    return (GraphProjectionLagProbe(),)


def build_fact_registry(
    declared_production_identity: object,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> FactRegistry:
    """Build and freeze the registry from what the operator declared.

    `declared_production_identity` is the mapping `Settings.facts_production_identity()`
    returns — `None` when undeclared. The deep validation happens here, in
    the model: a declaration the model refuses is logged and the production
    target stays unverifiable, so every production probe is refused at
    registration and the catalogue closes without them.
    """
    expected: dict[FactTarget, SourceIdentity] = {}
    if declared_production_identity is None:
        logger.warning(
            "facts.production_identity_undeclared",
            hint="set BRAIN_FACTS_PRODUCTION_IDENTITY to register production facts",
        )
    elif not isinstance(declared_production_identity, Mapping):
        logger.warning("facts.production_identity_refused", error="not a mapping")
    else:
        try:
            expected[FactTarget.PRODUCTION] = SourceIdentity.from_mapping(
                declared_production_identity
            )
        except ValueError as exc:
            logger.warning("facts.production_identity_refused", error=str(exc))

    registry = FactRegistry(
        sources={FactTarget.PRODUCTION: PostgresSourceFactory(session_factory)},
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

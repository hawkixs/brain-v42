"""Register immutable fact definitions when the server starts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from brain_v42.facts.canonical import definition_digest
from brain_v42.repositories.pg_fact_definitions import DefinitionOutcome, register_definition

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from brain_v42.facts.registry import FactRegistry

logger = structlog.get_logger(__name__)


async def register_fact_definitions(
    registry: FactRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    """Persist each frozen descriptor and disable only definitions proven to have drifted.

    Returns whether the pass completed: disabling a drifted fact still counts as
    completed (it is a decision, not a failure); only an exception during the pass
    itself (e.g. an unreachable database) returns `False`. The server ignores this
    flag today (spec ADR 27 lot C, T1.1); the nightly `claim_verify` CLI fails closed
    on it instead of proceeding on a startup that never finished.
    """
    drifted: list[tuple[str, str, str]] = []
    try:
        for name in registry.names():
            descriptor = registry.describe(name)
            computed_digest = definition_digest(descriptor)
            async with session_factory() as session, session.begin():
                outcome, stored_digest = await register_definition(
                    session, descriptor, digest=computed_digest
                )
            if outcome is DefinitionOutcome.INSERTED:
                logger.info(
                    "facts.definition_registered",
                    fact=name,
                    definition_version=descriptor.definition_version,
                    digest=computed_digest,
                )
            elif outcome is DefinitionOutcome.DRIFTED:
                drifted.append((name, str(stored_digest), computed_digest))
    except Exception:
        # An unreachable database already makes the probes unreadable. Failing
        # startup here would convert that degraded read path into an outage.
        logger.error("facts.definition_registration_failed", exc_info=True)
        return False

    for name, stored_digest, computed_digest in drifted:
        descriptor = registry.describe(name)
        registry.disable(name, "definition_drift")
        logger.error(
            "facts.definition_drift",
            fact=name,
            definition_version=descriptor.definition_version,
            stored_digest=stored_digest,
            computed_digest=computed_digest,
        )
    return True

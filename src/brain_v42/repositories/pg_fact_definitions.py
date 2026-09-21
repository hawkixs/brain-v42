"""Append-only persistence of the fact catalogue seen at server startup."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import knowledge_fact_definitions


class FactDescriptor(Protocol):
    """The immutable descriptor shape persisted by the repository boundary."""

    @property
    def name(self) -> str: ...

    @property
    def definition_version(self) -> int: ...

    @property
    def target(self) -> Any: ...

    @property
    def ttl_seconds(self) -> int: ...

    @property
    def timeout_seconds(self) -> int: ...

    @property
    def policies(self) -> Any: ...

    @property
    def value_schema(self) -> Any: ...


class DefinitionOutcome(StrEnum):
    """Describe whether startup first saw, confirmed, or detected drift in a definition."""

    INSERTED = "inserted"
    MATCHED = "matched"
    DRIFTED = "drifted"


async def register_definition(
    session: AsyncSession, descriptor: FactDescriptor, *, digest: str
) -> tuple[DefinitionOutcome, str | None]:
    """Insert an immutable definition, or compare its first-seen digest without updating it.

    The digest is an INPUT, computed by the caller in the `facts` layer. Computing
    it here would make `repositories` depend on `facts`, and the only way to hide
    that from `scripts/check_module_layering.py` is a runtime import the static
    check cannot see — which bypasses the gate instead of satisfying it. Taking the
    value as a parameter keeps the dependency genuinely one-way.

    Returns the outcome and, on drift only, the digest already stored. Returning it
    spares the caller a second SELECT on the conflict path without smuggling the
    value through `session.info`.
    """
    inserted_digest = (
        await session.execute(
            insert(knowledge_fact_definitions)
            .values(
                fact_name=descriptor.name,
                definition_version=descriptor.definition_version,
                target=descriptor.target.value,
                ttl_seconds=descriptor.ttl_seconds,
                timeout_seconds=descriptor.timeout_seconds,
                policies=dict(descriptor.policies),
                value_schema=dict(descriptor.value_schema),
                digest=digest,
            )
            .on_conflict_do_nothing(
                index_elements=(
                    knowledge_fact_definitions.c.fact_name,
                    knowledge_fact_definitions.c.definition_version,
                )
            )
            .returning(knowledge_fact_definitions.c.digest)
        )
    ).scalar_one_or_none()
    if inserted_digest is not None:
        return DefinitionOutcome.INSERTED, None

    stored_digest = (
        await session.execute(
            select(knowledge_fact_definitions.c.digest).where(
                knowledge_fact_definitions.c.fact_name == descriptor.name,
                knowledge_fact_definitions.c.definition_version == descriptor.definition_version,
            )
        )
    ).scalar_one()
    if stored_digest == digest:
        return DefinitionOutcome.MATCHED, None
    return DefinitionOutcome.DRIFTED, str(stored_digest)

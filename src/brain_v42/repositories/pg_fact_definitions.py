"""Append-only persistence of the fact catalogue seen at server startup."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from importlib import import_module
from typing import Any, Protocol, cast

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
    session: AsyncSession, descriptor: FactDescriptor
) -> DefinitionOutcome:
    """Insert an immutable definition or compare its first-seen digest without updating it."""
    digest = _definition_digest(descriptor)
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
        return DefinitionOutcome.INSERTED

    stored_digest = (
        await session.execute(
            select(knowledge_fact_definitions.c.digest).where(
                knowledge_fact_definitions.c.fact_name == descriptor.name,
                knowledge_fact_definitions.c.definition_version == descriptor.definition_version,
            )
        )
    ).scalar_one()
    if stored_digest == digest:
        return DefinitionOutcome.MATCHED
    # Startup emits the drift event after the transaction closes. Retain the
    # value already read here so that observability does not add a second SELECT
    # to the immutable conflict path.
    session.info["brain_v42.fact_definition.stored_digest"] = stored_digest
    return DefinitionOutcome.DRIFTED


def _definition_digest(descriptor: FactDescriptor) -> str:
    """Use the facts-owned recipe only at runtime to keep repository layering one-way."""
    codec = cast(Any, import_module("brain_v42.facts.canonical"))
    digest = cast(Callable[[FactDescriptor], str], codec.definition_digest)
    return digest(descriptor)

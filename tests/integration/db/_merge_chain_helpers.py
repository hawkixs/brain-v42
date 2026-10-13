"""Seed historical residue without bypassing registry or outbox writes."""

from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine


async def legacy_chain(engine: AsyncEngine, table: sa.Table, ids: tuple[UUID, UUID, UUID]) -> None:
    """Disable only the new guard so head databases can reproduce pre-068 chains."""
    child, middle, survivor = ids
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                f"ALTER TABLE public.{table.name} DISABLE TRIGGER {table.name}_merge_chain_guard"
            )
        )
        for source, target in ((middle, survivor), (child, middle)):
            await connection.execute(
                table.update().where(table.c.id == source).values(merged_into=target)
            )
        await connection.execute(
            sa.text(
                f"ALTER TABLE public.{table.name} ENABLE TRIGGER {table.name}_merge_chain_guard"
            )
        )

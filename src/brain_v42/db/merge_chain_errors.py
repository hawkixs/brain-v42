"""Translate the database guard without leaking foreign-project row identifiers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sqlalchemy as sa


class MergeChainRefusedError(ValueError):
    """Let writers report a merge refusal independently of the database driver."""


@asynccontextmanager
async def merge_chain_errors(
    error_type: type[Exception] = MergeChainRefusedError,
) -> AsyncIterator[None]:
    """Wrap writes and transaction exits because deferred guards can fail at commit.

    SQLSTATE 23514 is shared by CHECK constraints, so only the guard's stable
    message prefix identifies this refusal. Never forward its two row ids.
    SQLAlchemy's asyncpg adapter prefixes the message with the driver class;
    inspect that first message line without matching a prefix in row content.
    """
    try:
        yield
    except sa.exc.DBAPIError as error:
        message = str(error.orig).partition("\n")[0]
        # The adapted driver message starts with "<class 'asyncpg...'>: ".
        if message.startswith("<class 'asyncpg."):
            _, separator, message = message.partition("'>: ")
            if not separator:
                raise
        if getattr(error.orig, "sqlstate", None) == "23514" and message.startswith(
            "merge_chain_refused:"
        ):
            raise error_type("merge_chain_refused") from error
        raise

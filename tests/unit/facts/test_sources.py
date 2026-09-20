"""The PostgreSQL source refuses what it cannot vouch for, without a database."""

from __future__ import annotations

import pytest

from brain_v42.facts.sources import PostgresSourceSession


class _Rows:
    def __init__(self, row: dict[str, object]) -> None:
        self._row = row

    def mappings(self):  # type: ignore[no-untyped-def]
        return self

    def one(self) -> dict[str, object]:
        return self._row


class _Session:
    def __init__(self, row: dict[str, object], *, in_transaction: bool = True) -> None:
        self._row = row
        self._in_transaction = in_transaction

    def in_transaction(self) -> bool:
        return self._in_transaction

    async def execute(self, statement, *args, **kwargs):  # type: ignore[no-untyped-def]
        return _Rows(self._row)


_ROW: dict[str, object] = {
    "system_identifier": "7612696091383607335",
    "database": "brain",
    "server_addr": "172.31.0.4/32",
    "server_port": 5432,
}


@pytest.mark.asyncio
async def test_a_unix_socket_connection_has_no_address_and_is_refused() -> None:
    """NULL inet_server_addr(): the declaration may not use a socket, so the read cannot vouch."""
    with pytest.raises(ValueError, match="server address"):
        await PostgresSourceSession(_Session({**_ROW, "server_addr": None})).identity()  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_identity_outside_the_source_transaction_is_refused() -> None:
    """A probe that ended the transaction would let identity run in a fresh one: not one snapshot."""
    with pytest.raises(ValueError, match="transaction"):
        await PostgresSourceSession(_Session(_ROW, in_transaction=False)).identity()  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_identity_inside_the_transaction_is_the_four_measured_fields() -> None:
    identity = await PostgresSourceSession(_Session(_ROW)).identity()  # type: ignore[arg-type]
    assert identity.as_dict() == {
        "system_identifier": "7612696091383607335",
        "database": "brain",
        "server_addr": "172.31.0.4",
        "server_port": 5432,
    }

"""The v24 mint must only ever touch a disposable database, however the DSN is spelled."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "mint_recovery_contract_v24.py"


@pytest.fixture(scope="module")
def mint():
    spec = importlib.util.spec_from_file_location("mint_recovery_contract_v24", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:p@h:5433/brain",
        "postgresql://u:p@h:5433/brain_test",
        # The path a naive rsplit("/") misreads: asyncpg connects to `brain`.
        "postgresql://u:p@h:5433/brain?application_name=x/y",
        # Percent-encoding spells `brain` without the letters.
        "postgresql://u:p@h:5433/%62rain",
        "postgresql://u:p@h:5433/brain_v24_x?dbname=brain",
        "postgresql://u:p@h:5433/brain_v24_x?host=/tmp",
        "postgresql://u:p@h:5433/brain_v24_x/extra",
        "postgresql://u:p@h:5433/",
        "postgresql://u:p@h:5433/other",
    ],
)
def test_the_dsn_guard_refuses_what_is_not_a_disposable_database(mint, dsn: str) -> None:
    with pytest.raises(SystemExit):
        mint.assert_disposable_dsn(dsn)


def test_the_dsn_guard_accepts_a_disposable_database(mint) -> None:
    mint.assert_disposable_dsn("postgresql://u:p@h:5433/brain_v24_base_0123456789")


def test_the_connection_itself_is_verified_before_any_use(
    mint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the connection lands on `brain` whatever the DSN said, it is closed unused."""
    connection = AsyncMock()
    connection.fetchval.return_value = "brain"
    monkeypatch.setattr(mint.asyncpg, "connect", AsyncMock(return_value=connection))

    with pytest.raises(SystemExit):
        asyncio.run(mint.connect("postgresql://u:p@h:5433/brain_v24_x"))

    connection.close.assert_awaited_once()
    connection.fetch.assert_not_called()


def test_no_row_is_exempt_from_the_066_delta(mint) -> None:
    """Review of #376: v21's exemption skipped any new fingerprint of a named index.

    v24 measures each variant on the form it describes, so nothing is exempt: a
    divergence on any row outside the 066 footprint stops the mint.
    """
    assert mint.TWIN_DIVERGENCE_BY_DESIGN == set()
    with pytest.raises(SystemExit):
        mint.split_delta(
            "expected_table_indexes",
            [("dream_promotions", "idx_dream_promotions_source_materialized", "0" * 32)],
            [("dream_promotions", "idx_dream_promotions_source_materialized", "1" * 32)],
        )


def test_every_written_asset_is_replayed_from_disk(mint) -> None:
    """Review of #376: the written bytes, not the text in memory, pass a full replay."""
    source = SCRIPT.read_text()
    main = source[source.index("async def main(") : source.index("async def replay_written(")]
    assert main.index("dst.write_text(") < main.index("await replay_written(dsn, dst")
    assert "asset.read_text(" in source[source.index("async def replay_written(") :]


def _replay(mint, monkeypatch: pytest.MonkeyPatch, asset: Path, observed: str) -> None:
    """Replay a written asset whose receipt fails extension_versions with `observed`."""
    receipt = {
        "checks": [
            {
                "id": "extension_versions",
                "status": "fail",
                "expected": "pg_stat_statements 1.10, plpgsql 1.0, vector 0.8.2",
                "observed": observed,
            }
        ]
    }
    connection = MagicMock()
    connection.transaction.return_value.__aenter__ = AsyncMock(return_value=None)
    connection.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
    connection.fetchval = AsyncMock(return_value=json.dumps(receipt))
    connection.close = AsyncMock()
    monkeypatch.setattr(mint, "connect", AsyncMock(return_value=connection))
    asset.write_text("SELECT 1", encoding="utf-8")
    asyncio.run(mint.replay_written("postgresql://u:p@h:5433/brain_v24_x", asset, restored=False))


@pytest.mark.parametrize("version", ["0.8.4", "0.8.5"])
def test_a_proven_vector_build_passes_the_live_replay(
    mint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version: str
) -> None:
    asset = tmp_path / "brain-v42-v24.sql"
    _replay(mint, monkeypatch, asset, f"pg_stat_statements 1.10, plpgsql 1.0, vector {version}")
    assert asset.exists()


@pytest.mark.parametrize(
    "observed",
    [
        # Review round 2 of #376: an extra extension is outside the 066 footprint.
        "pg_stat_statements 1.10, pgcrypto 1.3, plpgsql 1.0, vector 0.8.5",
        # A vector build nobody proved harmless.
        "pg_stat_statements 1.10, plpgsql 1.0, vector 0.9.0",
        "plpgsql 1.0, vector 0.8.5",
    ],
)
def test_any_other_extension_inventory_fails_the_live_replay(
    mint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, observed: str
) -> None:
    asset = tmp_path / "brain-v42-v24.sql"
    with pytest.raises(SystemExit):
        _replay(mint, monkeypatch, asset, observed)
    assert not asset.exists()


@pytest.mark.parametrize(
    "name,key",
    [
        ("expected_table_columns", ("programmes",)),
        ("expected_table_constraints", ("knowledge_extractions", "surprise_check")),
        ("expected_table_indexes", ("knowledge_evaluations", "surprise_idx")),
        ("expected_sequences", ("knowledge_extractions_id_seq_extra",)),
    ],
)
def test_rows_outside_the_declared_footprint_stop_the_mint(mint, name, key):
    with pytest.raises(SystemExit):
        mint.split_delta(name, [(*key, "unexpected")], [])


def test_acl_delta_restricts_history_and_mutable_tables(mint):
    grants = mint.lifecycle_grants()
    for table in mint.HISTORY:
        assert grants[table] == ["SELECT", "INSERT"]
    for table in mint.MUTABLE:
        assert grants[table] == ["SELECT", "INSERT", "UPDATE"]
    live = mint.patch_runtime_acl((mint.RECOVERY / "brain-v42-v23.sql").read_text())
    assert "runtime_lifecycle_grants" in live
    assert "a.grantee = 0" in live
    restored = (mint.RECOVERY / "brain-v42-v23-pgrestore.sql").read_text()
    assert mint.patch_runtime_acl(restored) == restored


def test_sequence_delta_is_typed_and_owned(mint):
    rows = mint.declared_sequences()
    assert len(rows) == 4
    for row in rows:
        assert row[1] in mint.HISTORY
        assert row[2:] == ("id", "bigint", 1, 1, 9223372036854775807, 1, False)
        assert mint.allowed_addition("expected_sequences", (row[0],))
    assert "FALSE" in "\n".join(mint.render("expected_sequences", rows[0]))


def test_bind_requires_acl_assets_before_writing_current(mint, monkeypatch, tmp_path):
    monkeypatch.setattr(mint, "RECOVERY", tmp_path)
    monkeypatch.setattr(mint, "ROOT", tmp_path)
    (tmp_path / "current.json").write_text("unchanged")
    for suffix in (".sql", "-pgrestore.sql", ".json"):
        (tmp_path / f"brain-v42-v24{suffix}").write_text("{}")
    with pytest.raises(FileNotFoundError):
        mint.bind()
    assert (tmp_path / "current.json").read_text() == "unchanged"

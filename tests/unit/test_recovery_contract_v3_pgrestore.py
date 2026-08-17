"""Round-trip-safe attestation variant for the immutable head-037 contract."""

from __future__ import annotations

import hashlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
RECOVERY_DIR = PROJECT_ROOT / "ops" / "recovery"
IMMUTABLE_SQL = RECOVERY_DIR / "brain-v42-v3.sql"
PGRESTORE_SQL = RECOVERY_DIR / "brain-v42-v3-pgrestore.sql"


def test_v3_original_attestation_remains_immutable() -> None:
    assert hashlib.sha256(IMMUTABLE_SQL.read_bytes()).hexdigest() == (
        "2160b75148ffeaa3af1a8f0115319c7b9d32e406093e08d5c2283ca0b43cb8f3"
    )


def test_pgrestore_variant_is_bounded_and_canonicalizes_only_redundant_casts() -> None:
    sql = PGRESTORE_SQL.read_bytes()

    assert sql != IMMUTABLE_SQL.read_bytes()
    assert hashlib.sha256(sql).hexdigest() == (
        "d46bcdbbc1e560bb7859ddfff9883572fd4f6462cc38732520dd880d3155fd6a"
    )
    assert len(sql) <= 64 * 1024
    assert sql.startswith(b"WITH ")
    assert sql.endswith(b";\n")
    assert sql.count(b";") == 1
    assert b"observed_session_constraints" in sql
    assert b"observed_artifact_constraints" in sql
    assert b"'::character varying::text', '::character varying'" in sql
    assert b"']::text[]', ']'" in sql

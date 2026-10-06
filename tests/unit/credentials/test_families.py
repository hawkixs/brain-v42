"""Keep credential permissions aligned with the database's independent check."""

import re
from pathlib import Path

from brain_v42.credentials.families import ELEVATION_FAMILY, STORABLE_FAMILIES


def test_storable_families_match_migration_check() -> None:
    migration = Path(__file__).resolve().parents[3] / "alembic/versions/063_client_credentials.py"
    source = migration.read_text(encoding="utf-8")
    check = re.search(
        r"CONSTRAINT brain_client_credentials_families_valid\s+CHECK\s*\(.*?ARRAY\[(.*?)\]",
        source,
        re.DOTALL,
    )
    assert check is not None
    assert frozenset(re.findall(r"'([^']+)'", check.group(1))) == STORABLE_FAMILIES


def test_admin_is_only_an_elevation_family() -> None:
    assert ELEVATION_FAMILY == "admin"
    assert ELEVATION_FAMILY not in STORABLE_FAMILIES

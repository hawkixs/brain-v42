"""The restore image needs the same PostgreSQL client major as the database."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_production_installs_pinned_pgdg_postgresql_16_client() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    production = dockerfile.split("FROM deps AS production\n", 1)[1]
    assert re.search(r"ARG POSTGRES_CLIENT_VERSION=16\.\d+-\d+\.pgdg13\+\d+", production)
    assert '"postgresql-client-16=${POSTGRES_CLIENT_VERSION}"' in production
    assert "apt-archive.postgresql.org/pub/repos/apt trixie-pgdg-archive main" in production
    assert "signed-by=/usr/share/keyrings/postgresql-pgdg.gpg" in production
    assert "B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8" in production
    assert "gpg --show-keys --with-colons" in production
    assert "gpg --dearmor" in production
    assert "Pin: version ${POSTGRES_CLIENT_VERSION}" in production
    assert "Pin-Priority: 1001" in production
    assert "--allow-unauthenticated" not in production
    assert production.index("postgresql-client-16=${POSTGRES_CLIENT_VERSION}") < production.index(
        "USER appuser"
    )
    stages = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert stages[-1].endswith(" AS production")
    assert "pgvector/pgvector:0.8.4-pg16@sha256:" in (ROOT / "docker-compose.yml").read_text(
        encoding="utf-8"
    )

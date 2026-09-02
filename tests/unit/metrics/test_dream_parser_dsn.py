"""The parsers do not invent database credentials: they resolve them.

`_insert_dream_run` carried `postgresql+asyncpg://brain:brain@localhost:5433/brain`
as the default of `os.environ.get("POSTGRES_URL", ...)`. The `brain-v42-dream`
unit does not export that variable — measured: `systemctl --user show
brain-v42-dream.service -p Environment` contains none — so that literal WAS NOT a
development default, it was the production DSN, purely by coincidence, for as
long as the password was `brain`.

Rotating that password therefore cut the only writer that sets the real project
key in `dream_runs`, and cut it on the side nobody watches:
`asyncpg.exceptions.InvalidPasswordError` surfaces in a caller that logs
`WARN … (non-fatal)` then continues. Measured on 2026-08-16: 122 rows on 08-14,
**2** on 08-15 and **2** on 08-16 — the only survivors being `extract` and
`roadmap`, which write through the application path and therefore read `.env`.
Both nights nonetheless announced "61/63 phases OK".

These tests forbid the literal from returning: resolution goes through the same
configuration as the rest of the application, and a total absence of
configuration raises instead of manufacturing a credential.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from brain_v42.db.dsn import resolve_postgres_dsn
from brain_v42.metrics import dream_parser

_ROTATED = "postgresql+asyncpg://brain:s3cret-rotated@localhost:5433/brain"


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cut off the repository's `.env`: otherwise the test reads real production."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("POSTGRES_URL", raising=False)


def test_environment_dsn_wins_and_loses_the_asyncpg_driver_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POSTGRES_URL", _ROTATED)

    assert resolve_postgres_dsn() == "postgresql://brain:s3cret-rotated@localhost:5433/brain"


def test_dot_env_supplies_the_dsn_when_the_environment_is_silent(tmp_path: Path) -> None:
    """The production shape: `WorkingDirectory` is the repository, `.env` lives there.

    That is exactly what the old default short-circuited.
    """
    (tmp_path / ".env").write_text(f"POSTGRES_URL={_ROTATED}\n", encoding="utf-8")

    assert resolve_postgres_dsn() == "postgresql://brain:s3cret-rotated@localhost:5433/brain"


def test_no_credential_is_invented_when_nothing_configures_one() -> None:
    """Neither environment nor `.env`: raise, never guess.

    A default here is not merely wrong — it is INDISTINGUISHABLE from a correct
    configuration for as long as the guessed password stays valid.
    """
    with pytest.raises(RuntimeError) as excinfo:
        resolve_postgres_dsn()

    assert "POSTGRES_URL" in str(excinfo.value)


def test_the_dead_default_is_gone_from_the_parser_source() -> None:
    """Textual guardrail: the literal must return in neither of the two sites.

    The behavioural test above would not see a default reintroduced in ANOTHER
    `os.environ.get` call of the same file.
    """
    for module_path in (
        Path(dream_parser.__file__),
        Path(__file__).parents[3] / "scripts" / "dream" / "dream_preflight.py",
    ):
        assert "brain:brain@" not in module_path.read_text(encoding="utf-8"), (
            f"{module_path.name} réintroduit un identifiant de base en dur"
        )


@pytest.mark.asyncio
async def test_insert_dream_run_connects_with_the_resolved_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The INSERT site goes through the resolver, not `os.environ` directly."""
    monkeypatch.setenv("POSTGRES_URL", _ROTATED)
    connection = AsyncMock()

    with patch("brain_v42.metrics.dream_parser.asyncpg") as asyncpg_module:
        asyncpg_module.connect = AsyncMock(return_value=connection)
        await dream_parser._insert_dream_run(
            phase="scan",
            model="gpt-5.6-sol",
            run_date="2000-01-01",
            status="done",
            duration_s=1.0,
            telemetry=None,
            error_message=None,
            project_key="brain-v42",
            phase_dry_run=False,
        )

    asyncpg_module.connect.assert_awaited_once_with(
        "postgresql://brain:s3cret-rotated@localhost:5433/brain"
    )
    assert os.environ["POSTGRES_URL"] == _ROTATED

"""Les parsers n'inventent pas d'identifiants de base : ils les résolvent.

`_insert_dream_run` portait `postgresql+asyncpg://brain:brain@localhost:5433/brain`
en défaut de `os.environ.get("POSTGRES_URL", ...)`. L'unité `brain-v42-dream`
n'exporte pas cette variable — mesuré : `systemctl --user show
brain-v42-dream.service -p Environment` n'en contient aucune — donc ce littéral
N'ÉTAIT PAS un défaut de développement, c'était le DSN de production, juste par
coïncidence, tant que le mot de passe valait `brain`.

La rotation de ce mot de passe a donc coupé le seul écrivain qui pose la vraie
clé de projet dans `dream_runs`, et l'a coupé du côté où personne ne regarde :
`asyncpg.exceptions.InvalidPasswordError` remonte dans un appelant qui
journalise `WARN … (non-fatal)` puis continue. Mesuré le 2026-08-16 : 122 lignes
le 08-14, **2** le 08-15 et **2** le 08-16 — les seules survivantes étant
`extract` et `roadmap`, qui écrivent par le chemin applicatif et lisent donc
`.env`. Les deux nuits ont malgré tout annoncé « 61/63 phases OK ».

Ces tests interdisent le retour du littéral : la résolution passe par la même
configuration que le reste de l'application, et l'absence totale de
configuration lève au lieu de fabriquer un identifiant.
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
    """Couper le `.env` du dépôt : sinon le test lit la vraie production."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("POSTGRES_URL", raising=False)


def test_environment_dsn_wins_and_loses_the_asyncpg_driver_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POSTGRES_URL", _ROTATED)

    assert resolve_postgres_dsn() == "postgresql://brain:s3cret-rotated@localhost:5433/brain"


def test_dot_env_supplies_the_dsn_when_the_environment_is_silent(tmp_path: Path) -> None:
    """La forme de production : `WorkingDirectory` est le dépôt, `.env` y vit.

    C'est exactement ce que l'ancien défaut court-circuitait.
    """
    (tmp_path / ".env").write_text(f"POSTGRES_URL={_ROTATED}\n", encoding="utf-8")

    assert resolve_postgres_dsn() == "postgresql://brain:s3cret-rotated@localhost:5433/brain"


def test_no_credential_is_invented_when_nothing_configures_one() -> None:
    """Ni environnement ni `.env` : lever, jamais deviner.

    Un défaut ici ne se contente pas d'être faux — il est INDISCERNABLE d'une
    configuration correcte tant que le mot de passe deviné reste valide.
    """
    with pytest.raises(RuntimeError) as excinfo:
        resolve_postgres_dsn()

    assert "POSTGRES_URL" in str(excinfo.value)


def test_the_dead_default_is_gone_from_the_parser_source() -> None:
    """Garde-fou textuel : le littéral ne doit revenir dans aucun des deux sites.

    Le test de comportement ci-dessus ne verrait pas un défaut réintroduit dans
    un AUTRE appel `os.environ.get` du même fichier.
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
    """Le site d'INSERT emprunte le résolveur, pas `os.environ` en direct."""
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

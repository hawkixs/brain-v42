"""Postgres DSN for the CLI entry points that talk to asyncpg directly.

The dream rail's command-line tools do not open the application's SQLAlchemy
engine: they call `asyncpg.connect(dsn)`. Each therefore carried its own
`os.environ.get("POSTGRES_URL", "…brain:brain…")` — that is, its own hardcoded
credential.

That defect was not a development defect: `brain-v42-dream.service` exports no
`POSTGRES_URL`, so the literal WAS the production DSN for as long as the
password was `brain`. Rotating it cut the `dream_runs` writer without breaking
anything visible, because the caller logs `WARN … (non-fatal)`.

One function, two properties: it reads configuration the way the rest of the
application does (environment variable, then the working directory's `.env` —
which under systemd really is the repository root), and it RAISES when nothing
configures it. A guessed credential is worse than a wrong one: it is
indistinguishable from a correct configuration for as long as the guess holds.
"""

from __future__ import annotations

from pydantic import ValidationError

from brain_v42.config import Settings

__all__ = ["resolve_postgres_dsn"]


def resolve_postgres_dsn() -> str:
    """Return the Postgres DSN in the format asyncpg expects.

    The `postgresql+asyncpg://` scheme is the application's; asyncpg wants
    `postgresql://`. The conversion lives here so callers never have to remember
    which way the translation goes.

    Raises:
        RuntimeError: if no configuration supplies `POSTGRES_URL`, or if the
            supplied value does not satisfy the `Settings` contract.
    """
    try:
        settings = Settings()  # type: ignore[call-arg]  # postgres_url vient de l'env/.env
    except ValidationError as exc:
        raise RuntimeError(
            "POSTGRES_URL n'est pas configuré : ni dans l'environnement, ni dans "
            "le .env du répertoire de travail. Aucun identifiant par défaut n'est "
            f"fabriqué ici, volontairement. Détail : {exc}"
        ) from exc

    return settings.postgres_url.replace("+asyncpg", "")

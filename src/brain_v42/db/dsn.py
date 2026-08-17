"""DSN Postgres pour les points d'entrée CLI qui parlent à asyncpg en direct.

Les outils en ligne de commande du rail dream n'ouvrent pas le moteur SQLAlchemy
de l'application : ils appellent `asyncpg.connect(dsn)`. Ils avaient donc chacun
leur propre `os.environ.get("POSTGRES_URL", "…brain:brain…")`, c'est-à-dire leur
propre identifiant en dur.

Ce défaut n'était pas un défaut de développement : `brain-v42-dream.service`
n'exporte aucun `POSTGRES_URL`, donc le littéral ÉTAIT le DSN de production tant
que le mot de passe valait `brain`. Sa rotation a coupé l'écrivain de
`dream_runs` sans rien casser de visible, parce que l'appelant journalise
`WARN … (non-fatal)`.

Une seule fonction, deux propriétés : elle lit la configuration comme le reste
de l'application (variable d'environnement, puis `.env` du répertoire de
travail — qui est bien la racine du dépôt sous systemd), et elle LÈVE quand rien
ne la configure. Un identifiant deviné est pire que faux : il est indiscernable
d'une configuration correcte tant que la devinette reste valide.
"""

from __future__ import annotations

from pydantic import ValidationError

from brain_v42.config import Settings

__all__ = ["resolve_postgres_dsn"]


def resolve_postgres_dsn() -> str:
    """Retourne le DSN Postgres au format attendu par asyncpg.

    Le schéma `postgresql+asyncpg://` est celui de l'application ; asyncpg veut
    `postgresql://`. La conversion vit ici pour que les appelants n'aient pas à
    se souvenir du sens de la traduction.

    Raises:
        RuntimeError: si aucune configuration ne fournit `POSTGRES_URL`, ou si
            la valeur fournie ne satisfait pas le contrat de `Settings`.
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

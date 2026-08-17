"""045 — `dream_runs.model` cesse de refuser les modèles qu'on lui configure.

Ticket `bcb5e6d8`. La colonne naît `varchar(30)` en 013. Les noms de modèles,
eux, ont grandi : le fournisseur préfixe désormais l'éditeur et suffixe la date
du snapshot. Mesuré le 2026-08-16 sur l'inventaire réel :

    nvidia/nemotron-3-super-120b-a12b       33 car.  secours WET, DÉJÀ CONFIGURÉ
    deepseek-ai/deepseek-v4-flash-0731      34 car.  candidat vivant au primaire
    nvidia/nemotron-3.5-lightning-30b-a3b   37 car.  candidat écarté au canary

Ce qui se perd n'est pas la colonne `model`, c'est la LIGNE entière :
`StringDataRightTruncation` remonte dans un `except Exception` best-effort qui
imprime `! warning: could not record dream_run` et continue. Une nuit qui a
réellement tourné n'aurait aucune trace — le chemin d'échec qui efface la preuve
du succès.

Le défaut n'a pas encore frappé parce que ROADMAP tourne en DRY, dont la chaîne
est le primaire mort puis `meta/llama-3.1-8b-instruct` (26 car.). Il est armé
pour le jour de la bascule WET.

120 et non « la plus longue + marge » : un nombre rond absorbe le prochain nom
sans redemander une migration. Aucun backfill, aucune donnée touchée — élargir
un `varchar` est une réécriture de catalogue, pas de lignes.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "045"
down_revision = "044"
branch_labels = None
depends_on = None

_TARGET_WIDTH = 120
_PREVIOUS_WIDTH = 30


def _dream_run_view_sql() -> str:
    """Relire la définition de la vue DANS la 036, jamais la retaper ici.

    Mesuré en production : `ALTER TABLE dream_runs ALTER COLUMN model` est
    refusé par Postgres tant que `codex_dream_run_v1` projette cette colonne
    (`cannot alter type of a column used by a view or rule`). La vue doit donc
    tomber et revenir — et « revenir » veut dire IDENTIQUE. Recopier son SELECT
    ferait de cette révision une seconde source de vérité pour un contrat que
    `test_codex_contract_views_036.py` garde ailleurs : la dérive n'apparaîtrait
    qu'à la lecture, côté codex.
    """
    source = Path(__file__).with_name("036_codex_contract_views.py")
    spec = importlib.util.spec_from_file_location("_migration_036_views", source)
    if spec is None or spec.loader is None:  # pragma: no cover — chemin figé
        raise RuntimeError(f"036 illisible depuis la 045 : {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module._CREATE_DREAM_RUN_VIEW)


_DREAM_RUN_VIEW_SQL = _dream_run_view_sql()

# Reposer le droit après recréation : un DROP VIEW emporte ses GRANT avec lui,
# et `codex_ro` perdrait sa lecture en silence — la vue existerait, vide de
# permissions, ce qui se lit comme une panne côté client et non comme un oubli
# de migration ici. La 036 pose exactement cette ligne (l.461).
_GRANT_SQL = "GRANT SELECT ON codex_dream_run_v1 TO codex_ro"


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS codex_dream_run_v1")
    op.alter_column(
        "dream_runs",
        "model",
        existing_type=sa.String(length=_PREVIOUS_WIDTH),
        type_=sa.String(length=_TARGET_WIDTH),
        existing_nullable=True,
    )
    op.execute(_DREAM_RUN_VIEW_SQL)
    op.execute(_GRANT_SQL)


def downgrade() -> None:
    """Fail-closed : rétrécir ne doit jamais tronquer une mesure déjà écrite.

    Postgres refuserait de lui-même, mais avec une erreur de driver nue. La dire
    ici nomme la cause et la ligne fautive, au lieu de laisser l'opérateur
    deviner devant un `value too long for type character varying(30)`.
    """
    offenders = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT model FROM dream_runs "
                "WHERE char_length(model) > :width ORDER BY char_length(model) DESC LIMIT 5"
            ),
            {"width": _PREVIOUS_WIDTH},
        )
        .scalars()
        .all()
    )
    if offenders:
        raise RuntimeError(
            f"downgrade 045 refusé : {len(offenders)} modèle(s) dépassent "
            f"{_PREVIOUS_WIDTH} car. et seraient tronqués — {', '.join(offenders)}. "
            "Réécrire ou supprimer ces lignes avant de rétrécir la colonne."
        )

    op.execute("DROP VIEW IF EXISTS codex_dream_run_v1")
    op.alter_column(
        "dream_runs",
        "model",
        existing_type=sa.String(length=_TARGET_WIDTH),
        type_=sa.String(length=_PREVIOUS_WIDTH),
        existing_nullable=True,
    )
    op.execute(_DREAM_RUN_VIEW_SQL)
    op.execute(_GRANT_SQL)

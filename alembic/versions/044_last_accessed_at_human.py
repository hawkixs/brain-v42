"""044 — la récence humaine, le terme que la 041 avait laissé contaminé.

Spec `docs/superpowers/specs/2026-08-08-dream-v2-design.md` §5.2.

La 041 a donné `access_count_human` aux six tables suivies par le decay. Elle
répare `freq_factor`, de poids **0,2**. Elle laisse intact `access_factor`, de
poids **0,3** — le plus lourd après l'âge — qui lit `last_accessed_at`, sans
variante humaine.

Mesuré sur `learnings` au moment de la spec : **1 779 entités** ont une
`last_accessed_at` non nulle avec `access_count_human = 0`. Leur terme de
récence est donc piloté par des lectures MACHINE seules. Substituer le seul
compteur répare 0,2 des 0,5 de poids pilotés par la lecture ; le reste continue
de faire vivre ce que la machine relit — c'est-à-dire de faire vivre ce que le
dream vient de lire, ce qui est exactement la boucle qu'on veut couper.

L'AGRÉGAT SAIT DÉJÀ. `repositories/pg_access_log.py` groupe par
`access_log.actor` et teste `is_human_actor` pour remplir `count_human` ; le
`max_accessed` existe aussi, mais il est replié sur TOUS les acteurs. Un
`max_accessed_human` est une ligne dans une boucle qui existe. Ce qui manquait,
c'est la colonne où le ranger — la voici, sur les six mêmes tables.

Nullable et sans backfill, comme la 041 et la 043 : `NULL` veut dire « aucune
lecture humaine enregistrée depuis que la colonne existe », jamais « jamais
lu par un humain ». Un backfill depuis `last_accessed_at` recopierait
précisément le signal contaminé qu'on sépare.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "044"
down_revision = "043"
branch_labels = None
depends_on = None

# Mêmes six tables que la 041 et la 043 — l'ensemble suivi par le decay.
_DECAY_TABLES = (
    "learnings",
    "decisions",
    "snippets",
    "runbooks",
    "adrs",
    "indexed_plans",
)


def upgrade() -> None:
    for table in _DECAY_TABLES:
        op.add_column(
            table,
            sa.Column("last_accessed_at_human", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    for table in _DECAY_TABLES:
        op.drop_column(table, "last_accessed_at_human")

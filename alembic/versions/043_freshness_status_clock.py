"""043 — dater le STATUT de fraîcheur, et dire d'où la transition vient.

Spec `docs/superpowers/specs/2026-08-08-dream-v2-design.md` §4.3 et §6.2.
Préalable DUR de la purge, pas une préférence d'ordonnancement.

Le dépôt a déjà un critère de suppression — `decay_tools.brain_decay_status`,
affiché à SCAN toutes les nuits — et il est faux sur ses deux termes :

- `access_count = 0` est le compteur TOTAL. Un artefact relu par le seul dream
  sort du critère et devient indéfiniment non-purgeable.
- `updated_at < cutoff` REDÉMARRE à chaque écriture du flusher de compteurs,
  parce que `trg_<table>_updated` est présent sur les six tables. Il n'existe
  donc aujourd'hui AUCUNE horloge honnête pour mesurer un séjour en archive.

AUCUN BACKFILL. `NULL` veut dire « jamais mesuré », jamais « archivé depuis
toujours ». La distinction décide qui serait supprimé : dater rétroactivement à
`now()` ferait croire que tout le corpus vient de changer de statut, et dater à
`updated_at` recopierait précisément l'horloge fausse qu'on remplace.

MÉCANISME 041, PAS 040. La 040 écrit `focus_updated_at` en code applicatif
parce que le focus n'a QU'UN écrivain. `freshness_status` en a quatre, dont le
jugement REORG qui passe par le tool générique `brain_update` — lequel ne sait
rien du decay. Stamper en applicatif obligerait à le faire dans `brain_update`
lui-même, pour une colonne que 99 % de ses appels ne touchent pas. C'est donc un
trigger conditionnel, et aucun des quatre écrivains n'a à s'en souvenir. C'est
le point : l'un des quatre est un prompt.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "043"
down_revision = "042"
branch_labels = None
depends_on = None

# Les six tables suivies par le decay — même ensemble que
# `decay_tools._DECAY_ENTITY_TABLES` et que `_COUNTER_TABLES` de la 041.
_DECAY_TABLES = (
    "learnings",
    "decisions",
    "snippets",
    "runbooks",
    "adrs",
    "indexed_plans",
)

# Vocabulaire fermé des transitions, §4.3. `merge` = CLEAN a fusionné,
# `judgment` = REORG a tranché, `score` = le decay a calculé, `revive` = un
# accès humain a ramené l'entité.
_SOURCES = ("merge", "judgment", "score", "revive")

_CREATE_FUNCTION = """
CREATE FUNCTION public.stamp_freshness_status()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    NEW.freshness_status_updated_at := CURRENT_TIMESTAMP;
    -- Une source décrit UNE transition. La laisser survivre à la suivante
    -- ferait décrire la transition précédente avec la date de la nouvelle :
    -- une provenance fausse, qui se croit, au lieu d'une provenance absente,
    -- qui se voit. Un écrivain qui ne redéclare pas la sienne la perd.
    --
    -- Angle mort assumé, et il vaut mieux ici qu'ailleurs : deux transitions
    -- consécutives DE MÊME SOURCE ne sont pas distinguables d'une source non
    -- redéclarée, donc la seconde retombe à NULL. PostgreSQL n'expose pas la
    -- liste SET d'un UPDATE en plpgsql. La dégradation va vers « non mesuré »,
    -- jamais vers « mesuré faux ».
    IF NEW.freshness_source IS NOT DISTINCT FROM OLD.freshness_source THEN
        NEW.freshness_source := NULL;
    END IF;
    RETURN NEW;
END;
$function$;
"""


def upgrade() -> None:
    for table in _DECAY_TABLES:
        op.add_column(
            table,
            sa.Column("freshness_status_updated_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.add_column(table, sa.Column("freshness_source", sa.String(16), nullable=True))
        # NULL passe la CHECK par construction en SQL, et c'est voulu : la
        # colonne est « non mesuré » par défaut sur tout le corpus existant.
        allowed = ", ".join(f"'{source}'" for source in _SOURCES)
        op.create_check_constraint(
            f"ck_{table}_freshness_source",
            table,
            f"freshness_source IS NULL OR freshness_source IN ({allowed})",
        )

    op.execute(_CREATE_FUNCTION)

    for table in _DECAY_TABLES:
        # `OF freshness_status` restreint le déclenchement, le `WHEN` le
        # restreint encore : réécrire le MÊME statut ne rajeunit pas l'horloge.
        # Sans ce prédicat, un traitement idempotent qui repose `archived`
        # chaque nuit remettrait le séjour à zéro tous les jours, et rien ne
        # deviendrait jamais purgeable — silencieusement.
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_freshness_stamped
            BEFORE UPDATE OF freshness_status ON public.{table}
            FOR EACH ROW
            WHEN (OLD.freshness_status IS DISTINCT FROM NEW.freshness_status)
            EXECUTE FUNCTION public.stamp_freshness_status();
            """
        )


def downgrade() -> None:
    for table in _DECAY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_freshness_stamped ON public.{table};")
    op.execute("DROP FUNCTION IF EXISTS public.stamp_freshness_status();")
    for table in _DECAY_TABLES:
        op.drop_constraint(f"ck_{table}_freshness_source", table, type_="check")
        op.drop_column(table, "freshness_source")
        op.drop_column(table, "freshness_status_updated_at")

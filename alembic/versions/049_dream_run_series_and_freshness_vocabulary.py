"""La série temporelle du sweep, le rail qui se sous-déclarait, et deux mots de vocabulaire.

Revision ID: 049
Revises: 048

Trois objets d'une même famille — ADD COLUMN nullable + élargissement de CHECK —
regroupés sous le critère (c) de la décision signée 9d22bc6a : leurs downgrades
peuvent échouer INDÉPENDAMMENT, et chacun porte son propre refus nommé. M-C
(table checkpoint) n'entre PAS ici : son approbation produit de livraison est
encore due (ticket d04dc588). M-D reste ISOLÉE et prend la tête suivante.

1. ``dream_runs.closed_inactive_count`` (ticket 24ca3b73, C9 du couloir) : le
   compteur distinct existait au rapport et au modèle, PAS en base — une nuit
   qui fermerait 200 traçantes pour inactivité ne laissait aucune série
   temporelle. Abandonner et fermer-pour-inactivité sont deux événements de
   sens opposé ; les confondre était le mode de panne nommé par le ticket.

2. ``dream_runs.thinking_tokens`` (ticket 76e11c9f) : le rail agy générait
   962 thinking pour 1554 output sur le run mesuré du 2026-08-11 — ~38 % des
   tokens n'étaient comptés NULLE PART, alors que l'ordre des rails
   codex→agy→claude a été tranché sur un coût comparé. La colonne rend la
   mesure honnête ; les rails qui ne distinguent pas le thinking laissent NULL.

3. Vocabulaire ``freshness_source`` : ``plan_reindex`` (l'upsert de plan pose
   ``freshness_status='fresh'`` sur un fichier réédité — désarchivage légitime
   mais INVISIBLE tant qu'il ne se déclare pas, ticket 55a21fb8) et
   ``manual_update`` (réservé, inutilisé — gabarit exact de ``judgment``, qui
   a vécu réservé dans le CHECK de la 043 jusqu'à ce que la marche 1 le
   consomme ; l'arbitrage d'estampiller l'écriture humaine reste ouvert et
   trouvera le mot déjà admis).

NULLABLE ET SANS DÉFAUT, doctrine des 040-041-042-046-048 : ``NULL`` veut dire
« écrit avant la 049 » (ou : rail/phase qui ne mesure pas cette dimension).
Aucun backfill — un zéro rétroactif mentirait sur des nuits jamais comptées.
"""

from __future__ import annotations

from alembic import context, op

revision = "049"
down_revision = "048"
branch_labels = None
depends_on = None

#: Opt-ins NOMMÉS, un par destruction — jamais un drapeau générique (gabarit
#: 039/046/048) : trois refus indépendants, parce que c'est l'indépendance des
#: downgrades qui a rendu ce regroupement légitime (9d22bc6a, critère (c)).
_SERIES_OPT_IN = "allow_sweep_series_downgrade"
_THINKING_OPT_IN = "allow_thinking_tokens_downgrade"
_VOCABULARY_OPT_IN = "allow_freshness_vocabulary_downgrade"

_SOURCES_BEFORE = ("merge", "judgment", "score", "revive")
_SOURCES_AFTER = (*_SOURCES_BEFORE, "manual_update", "plan_reindex")
_NEW_SOURCES = ("manual_update", "plan_reindex")

#: Les six tables suivies par le decay — mêmes six que la 043, qui a posé ces
#: CHECK. L'ordre est celui des noms de contrainte, pour des messages stables.
_DECAY_TABLES = ("adrs", "decisions", "indexed_plans", "learnings", "runbooks", "snippets")


def _check_sql(table: str, sources: tuple[str, ...]) -> str:
    values = ", ".join(f"'{source}'" for source in sources)
    return (
        f"ALTER TABLE {table} ADD CONSTRAINT ck_{table}_freshness_source "
        f"CHECK (freshness_source IS NULL OR freshness_source IN ({values}))"
    )


def upgrade() -> None:
    # Rejouable POUR DE VRAI (gabarit 048) : quelqu'un rejouera ces lignes à la
    # main pendant la bascule, la promesse d'idempotence doit tenir partout.
    op.execute("ALTER TABLE dream_runs ADD COLUMN IF NOT EXISTS closed_inactive_count INTEGER")
    op.execute("ALTER TABLE dream_runs ADD COLUMN IF NOT EXISTS thinking_tokens INTEGER")
    for table in _DECAY_TABLES:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS ck_{table}_freshness_source")
        op.execute(_check_sql(table, _SOURCES_AFTER))


def downgrade() -> None:
    arguments = context.get_x_argument(as_dictionary=True)

    # 1. La série du sweep : la détruire efface la seule trace nuit-par-nuit
    #    des fermetures pour inactivité — les sessions closes restent, mais le
    #    « combien cette nuit-là » ne se reconstruit pas après coup.
    series_opted = arguments.get(_SERIES_OPT_IN) == "yes"
    op.execute(
        f"""
        DO $$
        DECLARE
            nights bigint;
            dates text;
        BEGIN
            SELECT count(*), coalesce(string_agg(DISTINCT run_date::text, ', '), '')
              INTO nights, dates
            FROM dream_runs
            WHERE closed_inactive_count IS NOT NULL;

            IF {"FALSE" if series_opted else "TRUE"} AND nights > 0 THEN
                RAISE EXCEPTION
                    'cannot downgrade 049: % dream_runs row(s) carry the closed_inactive '
                    'series (dates: %). Dropping the column erases the only per-night '
                    'record of inactivity closures. Export it, or rerun with '
                    '-x {_SERIES_OPT_IN}=yes',
                    nights, dates;
            END IF;
        END;
        $$
        """
    )

    # 2. Les thinking_tokens : les détruire ramène le rail agy à la
    #    sous-déclaration de ~38 % que la colonne existait pour fermer.
    thinking_opted = arguments.get(_THINKING_OPT_IN) == "yes"
    op.execute(
        f"""
        DO $$
        DECLARE
            measured bigint;
            dates text;
        BEGIN
            SELECT count(*), coalesce(string_agg(DISTINCT run_date::text, ', '), '')
              INTO measured, dates
            FROM dream_runs
            WHERE thinking_tokens IS NOT NULL;

            IF {"FALSE" if thinking_opted else "TRUE"} AND measured > 0 THEN
                RAISE EXCEPTION
                    'cannot downgrade 049: % dream_runs row(s) carry measured '
                    'thinking_tokens (dates: %). Dropping the column returns the agy '
                    'rail to its ~38%% under-declaration — the cost comparison that '
                    'ordered the provider chain becomes false again. Export it, or '
                    'rerun with -x {_THINKING_OPT_IN}=yes',
                    measured, dates;
            END IF;
        END;
        $$
        """
    )

    # 3. Le vocabulaire : restaurer le CHECK de la 043 est IMPOSSIBLE tant que
    #    des lignes portent les valeurs nouvelles. L'opt-in les remet à NULL —
    #    une provenance ABSENTE se voit, une provenance effacée en silence se
    #    croit (les mots de la 043) — puis seulement re-pose l'ancien CHECK.
    vocabulary_opted = arguments.get(_VOCABULARY_OPT_IN) == "yes"
    new_values = ", ".join(f"'{source}'" for source in _NEW_SOURCES)
    for table in _DECAY_TABLES:
        op.execute(
            f"""
            DO $$
            DECLARE
                carriers bigint;
                ids text;
            BEGIN
                SELECT count(*), coalesce(string_agg(id::text, ', '), '')
                  INTO carriers, ids
                FROM {table}
                WHERE freshness_source IN ({new_values});

                IF carriers > 0 THEN
                    IF {"FALSE" if vocabulary_opted else "TRUE"} THEN
                        RAISE EXCEPTION
                            'cannot downgrade 049: % row(s) of {table} declare a '
                            'provenance the 043 vocabulary cannot hold (%). Restoring '
                            'the old CHECK would fail outright; opting in NULLs these '
                            'provenances — a visible absence — before restoring it. '
                            'Record them elsewhere, or rerun with -x {_VOCABULARY_OPT_IN}=yes',
                            carriers, ids;
                    END IF;
                    UPDATE {table} SET freshness_source = NULL
                     WHERE freshness_source IN ({new_values});
                END IF;
            END;
            $$
            """
        )
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS ck_{table}_freshness_source")
        op.execute(_check_sql(table, _SOURCES_BEFORE))

    op.execute("ALTER TABLE dream_runs DROP COLUMN IF EXISTS thinking_tokens")
    op.execute("ALTER TABLE dream_runs DROP COLUMN IF EXISTS closed_inactive_count")

"""Dire PAR QUELLE CLÉ un artefact a été attribué, et rendre la devinette défaisable.

Revision ID: 048
Revises: 047

L'absorption dérivée gagne un second étage : quand la traçante de la connexion
courante ne porte rien — parce que ce transport est mort, ce qui arrive ~26 fois
par jour, tué par l'idle timeout de 900 s — la session de l'utilisateur peut
reprendre le ledger d'une AUTRE traçante du projet, à condition d'être la seule
session non-`agent` qui couvrait l'instant de création.

Le premier étage est une PREUVE : même connexion, appariement exact. Le second
est une DÉDUCTION : personne d'autre ne pouvait l'avoir produit. Les deux
écrivent la même colonne `session_id`, et sans cette révision rien ne les
distingue ensuite — ni pour un audit, ni pour un humain qui voudrait défaire une
mauvaise attribution. **Un taux ne se défait pas, une liste si.**

NULLABLE ET SANS DÉFAUT, doctrine des 040-041-042-046 : `NULL` veut dire « écrit
avant la 048 ». Aucun backfill, et c'est un refus motivé — poser `'explicit'`
partout mentirait sur les lignes que `derive_capture` avait déjà déposées, qui
n'ont jamais été une capture explicite de qui que ce soit.

L'INDEX EST PARTIEL sur le seul mode déduit : défaire une devinette doit être une
requête, pas un scan. Les trois autres modes ne se cherchent pas en masse.
"""

from __future__ import annotations

from alembic import context, op

revision = "048"
down_revision = "047"
branch_labels = None
depends_on = None

#: L'opt-in qui lève le refus de downgrade. NOMMÉ, jamais générique — gabarit
#: 039 puis 046 : un drapeau générique se recopie d'une migration à l'autre sans
#: qu'on relise ce qu'il autorise.
_DOWNGRADE_OPT_IN = "allow_attribution_mode_downgrade"

#: Miroir exact du CHECK posé plus bas ET de `tables.py`. Les quatre modes :
#: `explicit` (un humain a nommé l'UUID), `derived_deposit` (le serveur a déposé
#: dans une traçante), `derived_connection` (étage exact) et `derived_window`
#: (étage déduit par exclusivité temporelle).
_MODES = ("explicit", "derived_deposit", "derived_connection", "derived_window")

_CHECK = (
    "ALTER TABLE brain_session_artifacts "
    "ADD CONSTRAINT brain_session_artifacts_attribution_mode_valid "
    "CHECK (attribution_mode IS NULL OR attribution_mode IN ("
    + ", ".join(f"'{mode}'" for mode in _MODES)
    + "))"
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE brain_session_artifacts ADD COLUMN IF NOT EXISTS attribution_mode VARCHAR(24)"
    )
    op.execute(_CHECK)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_session_artifacts_derived_window "
        "ON brain_session_artifacts (session_id) "
        "WHERE attribution_mode = 'derived_window'"
    )


def downgrade() -> None:
    # Ce downgrade ne détruit AUCUNE ligne de ledger : les attributions restent,
    # les artefacts gardent leur session. Il détruit la seule chose qui
    # distingue une PREUVE d'une DÉDUCTION — et c'est précisément ce qu'il faut
    # nommer, parce que la perte est invisible dans les données restantes.
    #
    # Compter ET NOMMER, gabarit 047 : un message qui dit « N lignes » sans dire
    # lesquelles laisse l'opérateur sans geste possible.
    arguments = context.get_x_argument(as_dictionary=True)
    opted_in = arguments.get(_DOWNGRADE_OPT_IN) == "yes"

    op.execute(
        f"""
        DO $$
        DECLARE
            guessed bigint;
            names text;
            targets text;
        BEGIN
            SELECT count(*),
                   coalesce(string_agg(DISTINCT knowledge_id::text, ', '), ''),
                   coalesce(string_agg(DISTINCT session_id::text, ', '), '')
              INTO guessed, names, targets
            FROM brain_session_artifacts
            WHERE attribution_mode = 'derived_window';

            IF {"FALSE" if opted_in else "TRUE"} AND guessed > 0 THEN
                RAISE EXCEPTION
                    'cannot downgrade 048: % attribution(s) were DEDUCED by temporal '
                    'exclusivity, not proven by a connection. Dropping the column makes '
                    'them indistinguishable from a human explicit capture, and undoing a '
                    'wrong one becomes impossible. Artifacts: %. Target sessions: %. '
                    'Record them elsewhere, or rerun with -x {_DOWNGRADE_OPT_IN}=yes',
                    guessed, names, targets;
            END IF;
        END;
        $$
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_brain_session_artifacts_derived_window")
    op.execute(
        "ALTER TABLE brain_session_artifacts "
        "DROP CONSTRAINT IF EXISTS brain_session_artifacts_attribution_mode_valid"
    )
    op.execute("ALTER TABLE brain_session_artifacts DROP COLUMN IF EXISTS attribution_mode")

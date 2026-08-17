"""Distinguer le métabolisme du dream de l'activité humaine.

Revision ID: 041
Revises: 040

`updated_at` confond deux questions : qui a touché la ligne, et si c'est le
contenu qui a changé. Une écriture de compteur — `decay_flusher` incrémentant
`access_count` après une simple lecture — la rajeunit exactement comme une
réécriture humaine. Le cache anti-rejugement de PROMOTE compare un verdict à
cette date : il meurt donc à chaque lecture, et le même learning a été réévalué
23 nuits d'affilée pour le même verdict.

Trois colonnes, aucun backfill. `content_updated_at` NULL et
`access_count_human` 0 se lisent « jamais mesuré » et se réparent d'eux-mêmes
au premier vrai signal.

`content_updated_at` est écrite par TRIGGER, à l'inverse de `focus_updated_at`
(révision 040) qui l'est par code applicatif. La divergence est délibérée :
la clause WHEN ... IS DISTINCT FROM donne la sémantique de VALEUR que la 040
recherchait — recopier le même texte ne rajeunit rien — et le contenu des
entités a de nombreux écrivains (brain_learn, brain_update, REORG, merges de
CLEAN, scripts de backfill) là où le focus n'en a qu'un. Une invariante tenue
par convention sur N écrivains est oubliée par le N+1 ; c'est déjà arrivé ici
(voir repositories/pg_learning.py). Enfin `content_updated_at` PILOTE une
garde, quand `focus_updated_at` informe un humain : le niveau de garantie
exigé n'est pas le même.

La fonction `public.update_updated_at()` n'est PAS modifiée : la révision 039
l'épingle par SHA256 et par longueur, et la rendre conditionnelle rendrait 039
non-downgradable.
"""

import sqlalchemy as sa
from alembic import op

revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None

# Tables suivies par le decay : toutes reçoivent le compteur humain, car
# decay_flusher._ENTITY_TABLES les met à jour uniformément.
_COUNTER_TABLES = (
    "learnings",
    "decisions",
    "snippets",
    "runbooks",
    "adrs",
    "indexed_plans",
)

# Tables de connaissance : colonnes de contenu par table. `indexed_plans` est
# absent — ni candidat à la promotion, ni dans le signal préflight.
_CONTENT_COLUMNS = {
    "learnings": ("topic", "insight"),
    "decisions": ("title", "description", "reasoning", "consequences"),
    "snippets": ("title", "code"),
    "runbooks": ("title", "description", "trigger", "steps"),
    "adrs": ("title", "context", "decision", "consequences"),
}

_CREATE_FUNCTION = """
CREATE FUNCTION public.stamp_content_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    NEW.content_updated_at := CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$function$;
"""


def upgrade() -> None:
    op.add_column(
        "access_log",
        sa.Column(
            "actor",
            sa.String(64),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
    )

    for table in _COUNTER_TABLES:
        op.add_column(
            table,
            sa.Column(
                "access_count_human",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )

    for table in _CONTENT_COLUMNS:
        op.add_column(
            table,
            sa.Column("content_updated_at", sa.DateTime(timezone=True), nullable=True),
        )

    op.execute(_CREATE_FUNCTION)

    for table, columns in _CONTENT_COLUMNS.items():
        column_list = ", ".join(columns)
        predicate = " OR ".join(f"OLD.{c} IS DISTINCT FROM NEW.{c}" for c in columns)
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_content_updated
            BEFORE UPDATE OF {column_list} ON public.{table}
            FOR EACH ROW
            WHEN ({predicate})
            EXECUTE FUNCTION public.stamp_content_updated_at();
            """
        )


def downgrade() -> None:
    for table in _CONTENT_COLUMNS:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_content_updated ON public.{table};")
    op.execute("DROP FUNCTION IF EXISTS public.stamp_content_updated_at();")
    for table in _CONTENT_COLUMNS:
        op.drop_column(table, "content_updated_at")
    for table in _COUNTER_TABLES:
        op.drop_column(table, "access_count_human")
    op.drop_column("access_log", "actor")

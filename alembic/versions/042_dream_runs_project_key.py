"""Donner à dream_runs la dimension que le dream croyait couvrir.

Revision ID: 042
Revises: 041

Le dream est mono-projet depuis toujours — `dream.sh:70` porte
`PROJECT_KEY="${1:-brain-v42}"`, aucune boucle, une seule unité systemd — et
`dream_runs` n'a AUCUNE colonne de projet. Le schéma lui-même ne peut donc pas
exprimer la dimension qu'on croyait couverte : huit phases sur quatre mois, 124
nuits, toutes indistinguables. C'est la colonne qui manque avant que la boucle
puisse s'ouvrir.

NULLABLE, ET C'EST UNE CONSÉQUENCE MESURÉE, PAS DE LA PRUDENCE. Corrigé le
2026-08-09 : il y a SIX sites d'INSERT, pas cinq, et AUCUN ne fait remonter son
échec. Trois l'avalent dans leur propre fonction, c'est écrit dans leurs
docstrings — `scripts/ticket_extract.py` et `scripts/roadmap_curate.py`
(« Best-effort — never raises »), `src/brain_v42/maintenance/session_sweep.py`
(« la trace ne doit jamais tuer la phase »). Deux lèvent en Python mais sont
avalés par l'orchestrateur : le site partagé des deux parsers
(`dream.sh`, `WARN … (non-fatal)`) et `scripts/dream/_promote_helpers.py`
(`WARN promote — empty-pool dream_runs row NOT recorded`). Le sixième,
`scripts/dream/cross_project_resonance.py`, est mort — aucun appelant, donc
jamais exécuté, donc jamais avalé non plus. Une colonne NOT NULL transformerait
une erreur de schéma en avertissement imprimé sur tous ceux qui tournent : la
nuit perdrait sa trace en silence, soit exactement le défaut qu'on cherche à
supprimer.

AUCUN DÉFAUT NON PLUS. Un `server_default='brain-v42'` étiquetterait chaque nuit
de chaque projet avec la clé d'un seul, le jour où la boucle s'ouvre — la classe
de bug que cette colonne existe pour rendre visible.

AUCUN BACKFILL : les 864 lignes d'avant restent NULL, et NULL se lit « écrit
avant la 042 ». C'est la doctrine des révisions 040 et 041, et elle rend
l'aller-retour sûr — un lecteur non migré et un écrivain migré coexistent, ce
qui permet d'appliquer la migration en production AVANT le merge qui introduit
ses lecteurs.

LA SENTINELLE `'*'` désigne une phase globale. Corrigé le 2026-08-09, en livrant
les écrivains : elle est posée par QUATRE d'entre eux, pas trois — `extract`,
`roadmap`, `sweep`, plus `cross_project_resonance`, qui est mort (aucun appelant,
aucune ligne) et la reçoit pour ne pas être le seul incohérent le jour où
quelqu'un le rebranche.

La première écriture ajoutait que ces trois-là étaient « et ce n'est pas un
hasard » exactement les trois best-effort ci-dessus. C'était vrai par accident et
faux en substance : les SIX sites d'INSERT perdent leur trace en silence — trois
l'avalent dans leur fonction, trois sont avalés par l'orchestrateur. L'argument
du nullable porte donc sur six, pas sur trois, et il en sort renforcé. Une
élégance qui repose sur un décompte est une élégance à revérifier.

L'INDEX S'AJOUTE, il ne remplace pas. `idx_dream_runs_date(run_date DESC)` sert
les dix lectures mesurées aujourd'hui, dont aucune ne filtre par projet ; le
retirer casserait leur plan pour un gain nul.

Downgrade : `drop_column` simple, comme la 041, sans garde fail-closed. La perte
est réelle et assumée — un `dream_runs` sans clé de projet est exactement l'état
d'avant, qui a tenu 124 nuits, et la donnée se reconstruit en une nuit de
télémétrie. Une garde rendrait le retour plus dur que l'aller, ce qui est le
mauvais sens pour de la télémétrie.
"""

import sqlalchemy as sa
from alembic import op

revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None

_INDEX = "idx_dream_runs_date_project"


def upgrade() -> None:
    op.add_column(
        "dream_runs",
        sa.Column("project_key", sa.String(64), nullable=True),
    )
    op.create_index(
        _INDEX,
        "dream_runs",
        [sa.text("run_date DESC"), "project_key"],
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="dream_runs")
    op.drop_column("dream_runs", "project_key")

"""Source de vérité unique pour la fenêtre de séjour de ``process_metrics``.

La table porte une ligne par ``agent_name`` (c'est sa clé primaire), rafraîchie à chaque
flush et supprimée quand elle cesse d'être rafraîchie. Deux questions en découlent, et
elles doivent recevoir la MÊME réponse :

* jusqu'à quand garde-t-on une ligne ? (la purge, dans ``flusher`` et ``runtime``)
* jusqu'à quand la montre-t-on ? (la lecture, dans ``collector_db``)

Le 2026-08-10 elles divergeaient d'un facteur 60 — purge à 1 h, lecture à 60 s — et deux
appelants réels sur cinq étaient donc invisibles du panneau alors que leurs lignes
existaient en base. Le correctif n'est pas d'élargir un littéral mais de supprimer les
littéraux : trois constantes qui doivent s'accorder sans que rien ne les relie finissent
toujours par diverger.

La purge frappe l'INACTIVITÉ, pas l'ancienneté : une ligne rafraîchie reste indéfiniment.
La fenêtre ci-dessous borne donc le SILENCE d'un agent, pas la durée de vie de sa ligne.
"""

from __future__ import annotations

PROCESS_METRICS_RETENTION_SECONDS = 3600
"""Silence toléré avant qu'un agent quitte la table et le panneau (1 heure)."""

PROCESS_METRICS_FRESH_SQL = (
    f"updated_at > NOW() - INTERVAL '{PROCESS_METRICS_RETENTION_SECONDS} seconds'"
)
"""Prédicat de LECTURE : les agents encore montrés par le panneau."""

PROCESS_METRICS_STALE_SQL = (
    f"updated_at < NOW() - INTERVAL '{PROCESS_METRICS_RETENTION_SECONDS} seconds'"
)
"""Prédicat de PURGE : strict complément du précédent, par construction."""

PROCESS_METRICS_LIVE_SECONDS = 60
"""Silence au-delà duquel un PROCESS cesse d'être compté comme vivant.

Deux fenêtres, deux questions, et les confondre est exactement le défaut d'origine.
« Quels agents montre-t-on ? » se répond sur la rétention — un agent qui s'est tu il y a
dix minutes a bien travaillé et doit rester au panneau. « Combien de process tournent ? »
ne se répond que sur un silence court : le flush est périodique, donc un process vivant
réécrit forcément sa ligne. Mesuré le 2026-08-10 : le pid 1082528 était dans la fenêtre
d'une heure et absent de ``ps`` — sur la seule rétention, il aurait été « actif ».
"""

PROCESS_METRICS_IS_LIVE_SQL = (
    f"(updated_at > NOW() - INTERVAL '{PROCESS_METRICS_LIVE_SECONDS} seconds')"
)
"""Vivacité calculée par PostgreSQL.

Volontairement pas en Python : ``updated_at`` vient de la base, le sidecar a sa propre
horloge, et un écart entre les deux dériverait en production sans jamais échouer
bruyamment. Une seule horloge décide.
"""

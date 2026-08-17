# Pagination fiable de `brain_ticket_list`

## Contexte

`brain_ticket_list(project_key="brain-v42")` annonce 19 tickets à traiter, mais `_format_groups` n'en rend que 10. La constante `_LIST_CAP` coupe aussi les catégories « À confirmer » et « En attente » sans notice. Le contrat MCP n'expose aucun paramètre pour atteindre les lignes suivantes. En amont, `PgTicketRepo.list_grouped` trie uniquement par date de création croissante, ce qui maintient les plus vieux tickets sur la première page.

Le ticket fe1c8c33 approuve cette correction et interdit toute mutation directe des tickets frères. L'inventaire opérationnel reste donc une preuve en lecture seule destinée à l'orchestrateur.

## Contrat retenu

`brain_ticket_list` accepte deux paramètres compatibles avec l'appel existant :

```python
async def brain_ticket_list(
    project_key: str,
    limit: int = 10,
    offset: int = 0,
) -> str:
```

Le serveur borne `limit` entre 1 et 100 et ramène un `offset` négatif à 0. `limit` et `offset` s'appliquent séparément à chacune des trois catégories. Les en-têtes conservent le nombre total de tickets de la catégorie.

Chaque catégorie paginée indique le nombre exact de tickets omis sur la page. Quand une page suivante existe, la notice donne l'appel complet avec le prochain `offset`. Un appel répété avec ces offsets permet d'atteindre chaque ticket.

## Ordre

Le dépôt ordonne chaque catégorie par :

1. `updated_at DESC` ;
2. `created_at DESC` ;
3. `id ASC` pour stabiliser les égalités.

Ce tri remonte les tickets nouveaux ou récemment actifs, au lieu de réserver la première page aux plus vieux. La description MCP documente cet ordre et demande de parcourir les pages signalées pour examiner le backlog complet, notamment ses échéances.

## Compatibilité et limites

- L'appel historique avec le seul `project_key` rend toujours au plus 10 lignes par catégorie.
- Les catégories et leurs libellés restent « À traiter », « À confirmer » et « En attente de l'autre côté ».
- Aucun schéma de base, statut ou ticket n'est modifié.
- La correction n'interprète pas les dates libres présentes dans les titres ou les corps. La visibilité repose sur le tri d'activité, la notice exacte et l'accès à toutes les pages.
- `brain_session_start` conserve son aperçu compact. Il bénéficie du nouvel ordre via le dépôt et continue de renvoyer vers `brain_ticket_list` pour la pagination complète.

## Preuves

Les tests MCP couvrent la notice par défaut, l'accès à une page ultérieure, le comptage avant/après, la conservation des catégories et la description du contrat. Un test du dépôt compile les trois requêtes et vérifie l'ordre déterministe. La suite ciblée est exécutée en RED puis en GREEN, suivie de la suite unitaire, de Ruff, du formatage et de mypy.

L'inventaire des parents partiels, des tickets sortants et des tickets sans enfant utilise uniquement les outils Brain de lecture. Il consigne l'état observé et une décision proposée pour l'orchestrateur, sans réponse, transition, résolution ni fermeture.

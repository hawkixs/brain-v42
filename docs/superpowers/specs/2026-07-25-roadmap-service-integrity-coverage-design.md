# RoadmapService — validation des statuts et couverture des erreurs

**Date** : 2026-07-25

**Statut** : approuvé par le ticket Brain `5619c851`

## Contexte

Le ticket classe `roadmap_service.py` parmi les quatre modules sous 70 % et impose
l'ordre `project_group_ticket_service → roadmap_service → pg_ticket → thresholds`.
Le premier module est traité dans `9f41b01`. Le lot concurrent `2f797d6` touche
uniquement les tests de `ProjectGroupTicketService` et reste hors périmètre.

La mesure fraîche du test unitaire existant donne 55,97 % des instructions et
40,74 % des branches pour `roadmap_service.py`. Les pivots et les chemins heureux
de `update_feature_statuses` sont déjà couverts. Les préconditions et erreurs de
`update_project_focus` ne le sont pas dans la suite unitaire.

`update_feature_statuses` accepte actuellement toute chaîne comme statut. La
base protège déjà la colonne avec la contrainte canonique
`features_status_check`. La validation du service ajoute une défense en
profondeur : elle rejette tout le lot avant session ou SQL et fournit une
`ProjectFocusValidationError` déterministe, plutôt qu'une erreur PostgreSQL.

## Décision

Le service rejettera tout lot contenant un statut absent de
`VALID_FEATURE_STATUSES` avant d'ouvrir une session. L'erreur utilisera
`ProjectFocusValidationError`, déjà exposée par ce module pour les mutations de
roadmap invalides. Un lot mixte valide/invalide échouera entièrement avant SQL.

Le lot ajoutera des tests unitaires ciblés pour :

- le rejet précoce des statuts invalides dans `update_feature_statuses` ;
- les préconditions de focus et de révision ;
- le conflit entre mise à jour de statut et dépinglage ;
- les résolutions de feature manquante, ambiguë ou fusionnée ;
- la construction de `ProjectFocusConflictError` ;
- le chemin vide de `_lock_requested_features`.

Les tests de pivot existants restent la preuve de ce critère. Les tests
PostgreSQL existants continuent de prouver l'atomicité et la concurrence ; ce lot
ne les duplique pas et ne touche aucune base live.

## Limites

- Aucun changement d'API, de schéma, de transaction ou de requête roadmap.
- Aucun changement dans `test_project_group_ticket_service.py`.
- Aucun merge, push, déploiement ou mutation PostgreSQL live.
- Le seuil de sortie est au moins 70 % des instructions du module, sans baisse de
  la couverture globale.

## Alternatives écartées

1. **Ajouter seulement des tests** : la couverture progresserait, mais le writer
   resterait permissif et déléguerait le rejet à PostgreSQL au lieu de fournir
   une erreur applicative déterministe.
2. **Dupliquer les tests PostgreSQL en unitaires** : cela brouillerait la frontière
   entre validation rapide et preuve d'intégration.
3. **Refondre les deux writers de roadmap** : cette consolidation dépasse le
   ticket de couverture et augmenterait le rayon de changement.

## Vérification

- RED fonctionnel : un lot contenant `in_progress` atteint actuellement SQL au
  lieu de lever `ProjectFocusValidationError`.
- GREEN ciblé : tests du module et seuil de couverture ≥70 %.
- Validation dépôt : suite unitaire, Ruff, format, mypy, `git diff --check` et
  `gitnexus_detect_changes()`.

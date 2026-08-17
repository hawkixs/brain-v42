# Monolithe modulaire à DAG prouvé — contrat de couches

**Date** : 2026-07-30
**Statut** : proposé
**Contexte** : discussion théorie/archi — « le projet devient gros, faut-il passer en
microservices pour donner une session par service à des agents moins chargés ? »

## Problème

La crainte exprimée est réelle et documentée : le contexte est le goulot des agents de
code. La recherche sur le *context rot* montre une dégradation de précision sur les
18 modèles frontier testés dès que l'input grandit, avec un contexte effectif très
inférieur à la limite annoncée.

Le remède envisagé — découper `brain_v42` en microservices pour obtenir une session
d'agent par service — repose sur une implication fausse :

> frontière de déploiement ⇒ frontière de contexte

Ce sont deux axes indépendants. L'isolation de contexte s'obtient par subagent, scoping
et retrieval, à coût nul. Le microservice résout un problème d'organisation (déploiement
indépendant, équipes indépendantes, scaling indépendant) au prix de contrats versionnés,
de migrations distribuées et de la perte des changements atomiques.

La mesure du dépôt le confirme, et aggrave le diagnostic.

## Constat mesuré

Graphe des dépendances entre modules de premier niveau de `src/brain_v42`, construit par
AST sous Python 3.12 :

```
_root, models              feuilles
db          -> _root
repositories-> db, models
services    -> db, models, repositories, mcp*, metrics*
automation  -> _root, db, services
metrics     -> _root, db, services, automation
mcp         -> _root, db, models, repositories, services, metrics
```

Une composante fortement connexe : `automation ↔ mcp ↔ metrics ↔ services`, soit
27k des 39k lignes du paquet.

Les arêtes marquées `*` la refermaient et provenaient de **trois sites d'import** :

| Site | Cible | Symboles |
|---|---|---|
| `services/brain_service.py` | `mcp.dream_project_authorization` | `DreamProjectAuthorizationError`, `get_dream_project_scope` |
| `services/dream_run_service.py` | `metrics.collector_nightly` | `KILLSWITCHES_PATH`, `parse_killswitches` |
| `services/feature_service.py` | `mcp.tools.parsing` | `normalize_uuid_prefix`, `parse_uuid`, `resolve_entity_id` |

Les trois avaient la même nature : une primitive de bas niveau (politique d'autorisation,
parsing de configuration, parsing d'UUID) garée dans un module de haut niveau. Aucune
n'est un couplage architectural intentionnel.

Le retrait de ces trois arêtes rend le graphe entièrement acyclique — vérifié par test.

**Conséquence pour la question initiale** : découper aujourd'hui aurait transformé ce
cycle en quatre services s'appelant en boucle par HTTP, soit un *distributed monolith*.
Un cycle est un défaut de conception ; aucune topologie de déploiement ne le répare.

## Décision

Rester en monolithe modulaire et rendre l'acyclicité **prouvée en CI** plutôt
qu'espérée. La propriété visée n'est pas « être découpé » mais « rester découpable » :
tant que le graphe est un DAG, l'extraction d'un service reste mécanique. C'est une
option préservée à coût nul, pas une architecture choisie d'avance.

L'incertitude est assumée : la crainte est anticipée, pas vécue. Sous incertitude, on ne
choisit pas l'architecture, on garde le choix disponible.

## Conception

### Composant 1 — `scripts/check_module_layering.py`

Suit la convention des préflights existants (`check_mcp_http_port.py`,
`check_container_image_pins.py`) : erreur dédiée, fonctions `validate_*` pures,
`main() -> int` renvoyant `2`, aucune dépendance nouvelle (stdlib `ast`).

- `build_module_graph(package_root)` — résout `brain_v42.X` vers un sous-paquet réel ou
  vers `_root` si `X` est un module fichier ; gère les imports relatifs de tout niveau ;
  ignore self-imports et tiers.
- `find_cycles(graph)` — Tarjan à pile explicite, renvoie les SCC de taille > 1.
- `validate_module_layering(package_root)` — échoue à la moindre SCC de taille supérieure
  à un : aucune baseline ni exception n'est admise.

### Composant 2 — `tests/unit/test_module_layering.py`

Les tests sur paquets synthétiques (`tmp_path`) couvrent résolution paquet/module/relative,
fail-closed sur source illisible, détection de cycle et code retour `2`. Le test contre le
paquet réel exige directement un graphe sans SCC. Le checker ne prétend pas calculer un
ensemble minimal d'arêtes : il vérifie seulement l'invariant utile, le DAG.

### Ce qui n'est pas fait ici

La phase GREEN déplace les primitives vers les couches inférieures, avec des shims qui
préservent les identités publiques. Les symboles partagés (`parse_uuid`,
`resolve_entity_id`) exigent une analyse d'impact GitNexus préalable.

## Suite proposée

1. **GREEN réalisé** — les killswitches et UUID sont à la racine, le scope Dream est sous
   `services`, et les anciens chemins sont des shims d'identité.
2. Le contrat est « zéro cycle » : baseline, heuristique et `xfail` ont été retirés après
   la preuve du DAG.
3. `check_module_layering.py` est câblé dans `test:unit`, avant pytest.
4. Hors périmètre de ce contrat mais dans le même diagnostic : `CLAUDE.md` scopés par
   module, et découpe des quatre fichiers de plus de 1000 lignes (`db/tables.py` 1510,
   `repositories/pg_graph_ledger.py` 1488, `services/brain_graph_projection.py` 1400,
   `services/graph_service.py` 947) — la vraie friction agent d'aujourd'hui.

## Critères de sortie vers un vrai service

Extraire un module seulement si l'un devient vrai :

- contrainte runtime divergente (motif réel de l'extraction d'`embedding`) ;
- frontière de sécurité (motif réel de `codex_gateway`) ;
- cadence ou criticité de déploiement réellement divergente ;
- besoin de scaling indépendant démontré.

« L'agent a trop de contexte » n'est pas un critère. Un changement qui touche
systématiquement trois modules ou plus signale de mauvaises frontières, pas un besoin de
services.

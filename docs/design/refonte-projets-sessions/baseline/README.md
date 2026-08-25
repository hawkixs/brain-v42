# Baseline Phase 0 — refonte PROJETS + SESSIONS

**Contenu n° 1 de la Phase 0.** Mesure, zéro mutation.

## Rejouer

```bash
python3 docs/design/refonte-projets-sessions/baseline/snapshot.py
```

Écrit `snapshot-<horodatage>.json` dans ce répertoire. `--stdout` pour ne rien écrire.

**Ne jamais recopier un snapshot : le rejouer.** C'est la raison d'être de ce
répertoire. Le mode de panne documenté de ce chantier est de citer une mesure morte —
les « 479 artefacts » étaient justes le 2026-08-08 et périmés dix jours plus tard ; les
« 10/59 contextes à focus NULL » étaient justes et ne prouvaient pas ce qu'on leur
faisait dire.

## Lecture seule, garantie par le moteur

`snapshot.py` encadre la requête par `BEGIN READ ONLY` / `COMMIT`. Ce n'est pas une
intention, c'est Postgres qui l'impose. **Prouvé dans les deux sens le 2026-08-19** :

```
BEGIN READ ONLY; UPDATE brain_sessions … WHERE false;
  → ERROR: cannot execute UPDATE in a read-only transaction
BEGIN;           UPDATE brain_sessions … WHERE false;
  → passe (0 ligne)
```

Un seul statement, donc une seule transaction, donc un instantané **cohérent** : des
mesures réparties sur plusieurs transactions pourraient se contredire sans que personne
ne le voie.

## Chaque mesure porte son caveat

Le JSON ne contient pas que des nombres. Chaque mesure porte `proves` et
`does_not_prove`. Un nombre sans son caveat est un piège — c'est exactement ce qui a
produit les deux erreurs citées plus haut. **Lire `does_not_prove` avant de citer
`value`.**

## Première mesure — 2026-08-19, head `045`

| Mesure | Valeur | Ce qu'elle dit |
|---|---|---|
| Capture 30 j | **18,2 %** (77/424 fermées) | Recalcule le « 18 % » de B3. Ventilation neuve : `ended` 23,5 %, `abandoned` 3,5 % |
| Attribution 30 j | **30,4 %** (319/1050) | **En BAISSE** — 34 % au 2026-08-16. B3 se dégrade, elle ne stagne pas |
| `client_key` | **465 distinctes / 469 sessions** | B9 chiffrée : ratio de réutilisation 1,01. Une clé par session, ou presque |
| Focus NULL | **10/59, toutes « jamais écrit »** | `focus_revision = 0` ET jamais datées. **Zéro effacement**, confirme la prémisse de Q13 |
| Ambiguïté | **23 open sur 28** dans un projet à ≥2 | Plafond, par projet et non par couple (projet, acteur) |
| Masse colon | **537** sur six clés | `red-shrik:agent` 314 pèse toujours plus que `red-shrik` 246 |
| Index sessions | **3, aucun sur un acteur** | Voir la conclusion ci-dessous |
| `access_log` | **0 ligne** | Régime NORMAL — c'est un tampon purgé à chaque flush, pas un instrument |

## Conclusion sur la décision d'index de M-A — ÉCRITE, et la question a changé

Critère de sortie du PLAN §2 : « la conclusion — index nécessaire ou non — est
**écrite** ». La voici, avec sa mesure.

`EXPLAIN (ANALYZE, BUFFERS)` du 2026-08-19, cardinalité 469 lignes / 28 `open` :

| Forme | Plan | Temps | Buffers |
|---|---|---|---|
| A · squelette D5 + sous-requête corrélée | Bitmap Index Scan sur `idx_brain_sessions_project_status_started` | 0,157 ms | **36** |
| B · égalité sur colonne NON indexée | **Seq Scan**, 468 lignes rejetées | 0,255 ms | **63** |
| C · balayage complet (borne haute) | Seq Scan, 469 lignes | 0,085 ms | 63 |

**Trois lectures, dans cet ordre d'importance.**

1. **La question posée est devenue caduque, et c'est le cadrage du 2026-08-19 qui l'a
   rendue telle.** Elle demandait s'il faut indexer `started_by_actor`, parce que
   l'émetteur D5 devait filtrer dessus à chaque appel outermost. Sous la clé
   `(projet, connexion)` (ADR §0bis.2), **l'émetteur ne filtre plus sur l'acteur** : il
   fait un lookup par connexion. `started_by_actor` devient informatif — affichage dans
   `list`, triage des fantômes — et sort du chemin chaud. **Il n'a pas besoin d'index.**

2. **Une question d'index NEUVE la remplace, et celle-là est sérieuse.** La mesure B le
   montre : une égalité sur une colonne non couverte force un **Seq Scan de toute la
   table**, 63 buffers contre 2 pour le scan d'index de A — un facteur 30 dès
   aujourd'hui, sur le chemin le plus chaud qui soit, un appel outermost de tool. **La
   colonne de connexion doit être indexée.**

3. **La forme naturelle est un index UNIQUE**, et c'est plus qu'une optimisation. La
   propriété « exactement un par construction » que le cadrage invoque serait alors
   **imposée par la base**, pas seulement affirmée par le design. Sans elle, une régression
   d'insertion pourrait créer deux sessions sur une même connexion et le modèle mentirait
   en silence.

**Détail qui appuie (1) :** dans le plan A, la sous-requête corrélée de comptage — celle
qui implémente « exactement un » — consomme **27 des 36 buffers**, soit 75 %, et tourne
une fois par ligne candidate (`loops=3`). Sous la clé de connexion, cette sous-requête
**disparaît entièrement**. Le cadrage n'a pas seulement déplacé la question d'index : il a
supprimé les trois quarts du coût du statement.

**Réserve honnête :** à 469 lignes tout est sous la milliseconde et rien ne se sentirait
aujourd'hui. Cette conclusion porte sur la **forme** du plan — index scan contre balayage
complet — pas sur une douleur mesurée. Elle vaut parce que le parc croît et que le
chemin est chaud, pas parce que quelque chose est lent maintenant.

**Conséquence R1.5 :** ajouter un index à `brain_sessions` casse `expected_session_indexes`
(liste **FERMÉE**, `brain-v42-v4.sql:404-412`, contrôlée `:665` et `:687`) **et**
`SESSION_INDEX_DEFINITION_MD5`. Cet index voyage donc dans M-A avec la régénération des
**deux** assets v4.

## Fichiers

| Fichier | Rôle |
|---|---|
| `queries.sql` | Les 12 mesures, un seul statement, chacune avec `proves` / `does_not_prove` |
| `explain.sql` | Le coût du statement D5 — trois formes comparées |
| `snapshot.py` | Rejoue et date. Lecture seule imposée par la transaction |
| `snapshot-*.json` | Instantanés datés. **Historique, pas source de vérité** — rejouer |

# Le système PROJETS, de bout en bout

Ce document existe parce qu'il n'existait pas. Le système projets de brain-v42 est
réparti sur quatre briques — un format, trois tables, une convention de nommage et un
pipeline — dont aucune ne renvoyait aux autres. On pouvait lire chacune sans jamais
apprendre que la clé est immuable, que la hiérarchie est une illusion typographique, ou
que le prédicat qui l'implémente vit en douze exemplaires.

**Comment lire ce document.** Il est organisé par une grille : **chaque champ du modèle
est soit un FAIT que le serveur observe ou dérive, soit un JUGEMENT qu'un humain
déclare — jamais les deux.** Cette grille n'est pas décorative : elle dit qui a le droit
d'écrire quoi, et c'est elle qui rend les erreurs de conception visibles (§7).

**Ce que ce document n'est pas.** Il ne décrit pas la cible d'une refonte. Il décrit
l'état vérifié du dépôt et de la base. Les nombres qu'il cite sont **datés et
périssables** ; la commande pour les rejouer est donnée plutôt que le nombre seul.

---

## 1. Brique 1 — Le format de la clé

Source de vérité côté code : `src/brain_v42/models/project_key.py`.

- **Regex canonique** : `^[a-z0-9]+([:-][a-z0-9]+)*$`. Kebab-case, avec `:` accepté
  comme séparateur au même titre que `-`.
- **Deux alias auto-canonicalisés**, matchés exactement, sensibles à la casse :
  `brain` et `brain_v42` deviennent `brain-v42`.
- **Asymétrie écriture / lecture, délibérée.** `canonicalize_project_key(value,
  strict=True)` — le défaut, donc le chemin d'écriture — lève `ValueError` avec une
  suggestion sur toute clé non conforme. `strict=False` — le chemin de lecture —
  laisse passer tel quel : une lecture avec une mauvaise clé rend simplement zéro
  résultat, au lieu de faire échouer une requête inoffensive.
- `None` traverse inchangé : c'est de la connaissance globale, non scopée.
- `ProjectKeyCanonicalMixin` applique la règle à tout modèle Pydantic qui déclare un
  `project_key`.

**La propriété qui compte, et qu'il ne faut jamais laisser régresser** : *une mauvaise
clé est impossible à persister*. Elle a été acquise après un incident de drift où des
artefacts ont été écrits sous `brain_v42` (underscore) au lieu de `brain-v42`.

---

## 2. Brique 2 — Trois surfaces en base, souvent confondues

| Table | Née en | Rôle | Ce qu'on croit à tort |
|---|---|---|---|
| `projects` | 033 | **Registre.** PK `project_key`, `registry_status` ∈ {claimed, unclaimed, archived}, `source` ∈ {context, reference, manual} | Que ce soit l'objet opérationnel. Il ne l'est pas |
| `project_aliases` | 033 | **Table d'alias.** `alias_key` → `project_key`, FK CASCADE. Des triggers appliquent la règle aux écritures | Qu'elle soit consultée par le code applicatif — la canonicalisation, elle, vit dans le code |
| `project_contexts` | **001** | **L'objet opérationnel réel.** `current_focus`, `focus_revision`, `focus_updated_at`, `related_projects`, `project_group`, roadmap, compteurs | Qu'elle soit née avec le registre. Elle le précède de trente-deux migrations |

**Le registre suit le contexte, pas l'inverse.** Créer un `project_context` inscrit une
entrée `claimed` ; une simple référence depuis un autre projet crée `unclaimed` ;
supprimer le contexte repasse la ligne en `unclaimed/reference`. La ligne de registre
n'est donc jamais une décision en soi — c'est une conséquence observée.

**Depuis la 033, `project_contexts.project_key` est IMMUABLE.** Un trigger lève
`project_contexts.project_key is immutable` sur tout UPDATE de cette colonne. Renommer
un projet exige une migration explicite. Ce n'est pas un oubli d'ergonomie : c'est ce
qui rend le drift de clé irréversible-par-accident.

**Les tables de connaissance portent la clé SANS clé étrangère.** `decisions`,
`learnings` et `snippets` l'ont nullable ; `runbooks`, `adrs` et `indexed_plans` la
veulent NOT NULL. La cohérence ne repose donc pas sur le moteur mais sur la frontière
Pydantic plus les triggers de la 033. C'est un choix, et il a un prix : rien en base
n'empêche une clé de connaissance de désigner un projet qui n'existe pas.

**Le CHECK de `projects` et la regex du code sont identiques** — vérifié le
2026-08-19, caractère pour caractère : `^[a-z0-9]+([:-][a-z0-9]+)*$` des deux côtés
(`projects_key_format_valid` en base, `_KEBAB` dans le code). Voir §8 pour ce qui,
aujourd'hui, ne garantit *pas* qu'ils le restent.

---

## 3. Brique 3 — La hiérarchie est PLATE

`red-shrik:agent` n'est pas un enfant de `red-shrik`. **Aucun lien parent/enfant
n'existe en base.** Le deux-points est une convention de nommage, rien de plus : la
regex l'accepte comme un séparateur ordinaire, au même titre que le tiret.

Partout où le code compare des projets, c'est en **égalité stricte**. Le pipeline
nocturne filtre `project_key = :pk` ; il n'existe aucun filtre par préfixe. Conséquence
directe et souvent surprenante : *une nuit de `red-lab` ne voit jamais
`red-lab:architect`*, même si les deux tournent.

L'exception à cette égalité stricte est le **périmètre de groupe** — et c'est là que le
prédicat de sous-partition vit.

---

## 4. Le recensement du prédicat colon

**Ce recensement a été faux trois fois, chaque fois par un angle mort différent.** Il
est reproduit ici avec sa méthode, pour que la prochaine personne puisse le refaire au
lieu de le croire.

- Une première version affirmait « une seule exception dans tout le code ».
- Une deuxième corrigeait en « trois exemplaires `src/` et deux vues ».
- Une troisième, en cherchant les copies Python par le motif `":" in `, a manqué celle
  qui s'écrit `":" not in` — le grep de correction avait lui aussi son angle mort.

**Compte vérifié le 2026-08-19 : cinq exemplaires dans `src/`, sept vues en base,
trois formulations distinctes du même prédicat.**

### Trois formulations

| # | Forme | Où |
|---|---|---|
| 1 | **SQL**, `base_key NOT LIKE '%:%' AND candidate LIKE base \|\| ':%'` | `db/project_group_scope.py:24` · `services/project_group_ticket_service.py:134` · `services/proposal_service.py:380` |
| 2 | **Python**, `":" not in base_key and project_key.startswith(f"{base_key}:")` | `services/project_group_ticket_service.py:163-166` |
| 3 | **SQL**, `split_part(project_key, ':', 1)` | `repositories/pg_project_context.py:202-204` |

Trois observations qui expliquent pourquoi le compte a résisté :

- **La n° 2 vit dans la MÊME méthode que sa jumelle SQL** (`_lock_participants_scope`).
  Le prédicat y est écrit deux fois, dans deux langages, à trente lignes d'écart.
- **La n° 3 est invisible à un grep sur `not_like("%:%")`** : elle n'emploie ni `LIKE`
  ni `%:%`.
- **`proposal_service.py` recopie le SQL alors qu'il importe déjà
  `project_group_scope`** — le helper partagé existe et n'est pas utilisé là.

### Sept vues en base

Mesuré :

```sql
SELECT table_name FROM information_schema.views
WHERE table_schema = 'public' AND view_definition LIKE '%split_part%'
ORDER BY 1;
```

`codex_brain_entity_v1`, `codex_feature_artifact_v1`, `codex_feature_v1`,
`codex_roadmap_curation_proposal_v1`, `codex_ticket_extraction_proposal_v1`,
`codex_ticket_message_v1`, `codex_ticket_v1`.

Toutes issues de la migration **036**, et nées de **deux corps de CTE recopiés** :
`_RED_KEYS_CTE` (six vues) et `_BRAIN_RED_KEYS_CTE` (une). La migration 024 n'est pas
un second objet vivant : la 036 remplace sa vue par `CREATE OR REPLACE`.

**Total : douze objets encodent la même sémantique**, cinq en Python/SQLAlchemy et sept
en SQL figé dans une migration. Aucun ne référence les autres.

---

## 5. Brique 4 — Les chiffres, et comment les rejouer

**Ne recopiez aucun nombre de cette section.** Rejouez-la :

```bash
python3 docs/design/refonte-projets-sessions/baseline/snapshot.py
```

Mesure du **2026-08-19** (head Alembic `045`), donnée comme ordre de grandeur et non
comme référence : 59 `project_contexts` ; environ 4 560 artefacts de connaissance ;
**537 artefacts sous une clé colon**, répartis sur six clés. La plus lourde,
`red-shrik:agent` (314), pèse **plus que son parent** `red-shrik` (246) — ce qui n'a
aucune conséquence structurelle, puisque le lien parent/enfant n'existe pas (§3), et
toute la conséquence pratique : ce sont deux corpus qui ne se voient pas.

Une mesure de 2026-08-08 citait « 479 artefacts colon » ; elle était juste à sa date et
a été recopiée pendant dix jours après avoir cessé de l'être. C'est le mode de panne
que cette section cherche à rendre impossible.

---

## 6. Les trois surfaces vues par un appelant

| Surface | Ce qu'elle expose | Canonicalisation |
|---|---|---|
| Outils MCP `brain_*` | `project_key` en argument | **Stricte** en écriture, **tolérante** en lecture |
| Vues `codex_*` | Lecture seule, périmètre de groupe | Figée dans le SQL de la 036 |
| Pipeline nocturne | Périmètre par projet | Égalité stricte, aucun préfixe |

---

## 7. La grille FAIT / JUGEMENT appliquée

C'est la grille annoncée en tête. Elle dit qui écrit quoi.

| Champ | Nature | Qui écrit |
|---|---|---|
| `projects.registry_status`, `source` | **FAIT** | Le serveur, en conséquence d'un contexte créé ou référencé |
| `project_aliases.*` | **FAIT** | Triggers de la 033 |
| `project_contexts.focus_revision` | **FAIT** | Trigger (032) — un compteur, jamais une opinion |
| `project_contexts.focus_updated_at` | **FAIT** | Code applicatif (`db/focus_stamp`), sous `IS DISTINCT FROM` : réécrire le même focus ne le rajeunit pas. `NULL` = jamais mesuré |
| `decisions_count`, `learnings_count`, … | **FAIT** | Compteurs dérivés |
| `project_contexts.current_focus` | **JUGEMENT** | L'humain. **Seul canal de jugement libre du système** |
| `project_contexts.blockers` | **JUGEMENT** | L'humain |
| `description`, `code_style`, `git_workflow`, `test_strategy` | **JUGEMENT** | L'humain |
| Roadmap (`features`) | **JUGEMENT** | L'humain, assisté de propositions |

**Ce que la grille rend visible.** `current_focus` est le seul endroit du système où
s'écrit du jugement non dérivable. Tout le reste est recalculable. La règle qui en
découle — et qui est facile à violer sans le voir — est que **le focus ne doit contenir
que ce qui n'est pas déjà mesurable ailleurs** : y recopier un compte d'artefacts ou un
statut de migration, c'est transformer le seul canal de jugement en cache périmé.

**Une seule ligne du tableau est ambiguë**, et il vaut mieux le dire : `current_phase`
est déclaré par l'humain mais décrit un état que le système pourrait souvent dériver.
C'est un champ à surveiller — il vieillit mal.

---

## 8. Dérives connues et non gardées

- **La regex de la clé existe en dix-sept endroits de l'arbre, répartis sur seize
  fichiers** (mesuré le 2026-08-19, hors ce document), et `project_key.py` se déclare
  pourtant « seule source de vérité ». **Aucun test ne relie `_KEBAB` au CHECK SQL** :
  le test de la 033 épingle la source de la migration contre un littéral réécrit dans le
  test, sans jamais importer `_KEBAB`. Élargir la regex Python laisserait donc passer,
  côté Pydantic, des clés que la base refuserait — et rien ne rougirait avant l'INSERT.
  La ventilation est plus instructive que le nombre : deux migrations (012, 033), trois
  modules `src/`, deux tests, quatre documents, et **cinq assets d'attestation de
  récupération** (`ops/recovery/`, versions v2 à v4 et leurs variantes `pgrestore`).
  Toucher à la regex ne casse donc pas seulement la cohérence code/base : cela casse
  aussi la preuve de restauration.
- **Le prédicat colon vit en douze exemplaires** (§4), dont deux dans la même méthode.
  Un changement de sémantique de sous-partition doit être appliqué douze fois.
- **Les tables de connaissance n'ont pas de FK vers le registre** (§2). Une clé de
  connaissance peut désigner un projet inexistant sans que la base s'y oppose.
- **`project_contexts.current_focus` peut être effacé par omission.** La branche
  `ON CONFLICT` de l'upsert réécrit le focus à `NULL` quand l'argument est omis. Ce
  canal existe dans le code ; il n'a **jamais été observé en train de mordre** — les
  10 contextes à focus `NULL` mesurés le 2026-08-19 sont **tous** à `focus_revision = 0`
  et jamais datés, donc « jamais écrit », pas « effacé ».

---

*Vérifications de ce document, 2026-08-19, en lecture seule : `models/project_key.py` ;
`db/project_group_scope.py` ; `services/project_group_ticket_service.py` ;
`services/proposal_service.py` ; `repositories/pg_project_context.py` ;
`alembic/versions/001_initial.py`, `033_graph_relation_ledger.py`,
`036_codex_contract_views.py` ; `tests/unit/db/test_schema_data_foundation_033.py` ;
et la base de production pour les vues, les contraintes et les cardinalités. Aucune
écriture, aucun commit hors ce fichier.*

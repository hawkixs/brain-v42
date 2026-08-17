# Dream v2 — périmètre global, curseur de reprise, décroissance réunifiée

**Date** : 2026-08-08
**Statut** : design — **passé en revue adversariale le 2026-08-08 à 03:24 UTC ; chiffres rejoués,
corrections consignées en §11.** Une décision d'opérateur est en attente et bloque l'étape 6a :
§4.3, propriété 6.
**Supersède le périmètre** de
`2026-08-08-dream-project-pool-design.md` (ci-après « la v1 »), hérite de son inventaire de
couplages, et rend exécutables six décisions prises par l'opérateur après l'avoir lue.
**Périmètre** : `brain_v42` seul — `scripts/dream.sh`, les phases, `dream_runs`, le calcul de
décroissance et ses deux notions de fraîcheur, l'unité systemd et son timer.
**Worktree** : `.claude/worktrees/dream-pool`, branche `feat/dream-project-pool`.
**Migration mesurée en production au moment de l'écriture** : `041`

```
docker exec brain_v42_postgres psql -U brain -d brain -Atc "select version_num from alembic_version;"
→ 041
```

**Aucun code d'implémentation n'accompagne cette spec.** `scripts/dream.sh` est exécuté depuis le
working tree de la **racine**, tous les jours. Tout ce que ce lot ajoute vit sous `docs/`.

---

## 0. Comment lire les chiffres de cette spec

Trois qualités de chiffre, jamais mélangées :

- **Mesuré** — une commande a été lancée aujourd'hui, elle est citée à côté du résultat.
- **Hérité de la v1** — mesuré le 2026-08-08 par la spec précédente et re-cité, pas re-mesuré.
  Signalé par « v1 §N ».
- **Arithmétique** — une déduction depuis des constantes lues dans le code. Ce n'est pas une
  mesure d'exécution et c'est dit à chaque fois.

**La réserve de méthode de la §4 et de la §5 est levée.** La première écriture publiait des
multiplicateurs issus d'une **réplique SQL** de `src/brain_v42/services/decay.py`, sans avoir
exécuté le Python. La revue adversariale a rejoué le calcul en important le **vrai**
`DecayCalculator`, en lecture seule, sur les 2 343 learnings ni archivés ni fusionnés exportés
par `COPY … TO STDOUT` :

```
PYTHONPATH=<worktree>/src .venv/bin/python  →  from brain_v42.services.decay import DecayCalculator
```

La réplique et le code réel **concordent** : moyenne 0,5130 / 0,4493, minimum 0,2228, `stale`
1 094 / 1 453, `archived` 0 / 0, `< 0,25` = 49, `< 0,30` = 236. Seul le p10 du compteur humain
diffère de 0,0001 (0,2905 contre 0,2906 publié), écart de méthode d'interpolation. Les chiffres
de §4 et §5 sont donc **mesurés au sens fort**. La consigne « un seul calcul dans le dépôt »
(§4.3, propriété 1) reste entière : elle vise l'implémentation, pas la mesure.

**Une revue adversariale a rejoué toute la spec le 2026-08-08 à 03:24 UTC.** Ce qui a changé est
consigné en §11, y compris un chiffre qui était faux.

---

## 1. Ce que la v1 apporte, et ce qu'elle perd

La v1 concevait un **pool de huit projets en dur**. L'opérateur a tranché autrement après
lecture : **tout le brain est éligible** (D1). Le supersédage doit être explicite, parce que la
v1 reste un document utile et que la moitié de son contenu ne bouge pas.

### 1.1 Ce qui tombe

| v1 | ce qui tombe | pourquoi |
|---|---|---|
| §1 — le pool des huit clés | **tombe entièrement** | D1 : plus de liste en dur |
| §2 — « l'exclusion des sous-projets et son coût » (479 artefacts) | **tombe comme dette** | les six clés à deux niveaux sont éligibles comme les autres ; il n'y a plus d'exclusion à payer |
| §4.2 — la simulation « 8 × agent + 1 × global » (80 min, p90 126,6, max 286,3) | **tombe comme dimensionnement** | on ne multiplie plus par un cardinal fixe : on remplit une fenêtre (§7). La **méthode** — décomposer nuit par nuit, prendre le p90, jamais multiplier des moyennes — reste et est reprise |
| §4.3 — le plafond configuré « 8 × 53 min + 35 » (459/803 min) | **tombe** | à 55 projets ce calcul donne **2 950 min** (55 × 53 + 35 ; `PHASES`, `dream.sh:106-113` → 5+5+8+15+10+10) — la première écriture disait 3 050, faux de 100 min. Le plafond cesse d'être une borne utile et devient une fenêtre (§7) |
| §6 — `BRAIN_DREAM_PROJECT_POOL` comme **liste d'inclusion** | **tombe** | il n'y a plus d'inclusion à déclarer. Les deux pièges de transport que §6 a mesurés **restent valables** et resservent tels quels pour toute liste future (exclusion, canari d'ouverture) |
| §12 étape 6 — « ouvrir le pool une clé à la fois » | **change de forme** | on n'ouvre plus des clés, on ouvre une **fenêtre** ; l'ouverture progressive se fait en largeur de fenêtre (§7, §8) |
| §16 — « le pool couvre 62,4 % du corpus » | **tombe** | la couverture visée est 100 %, étalée sur plusieurs nuits |

### 1.2 Ce qui reste, et ne se rediscute pas

Tout ceci est mesuré par la v1, re-cité, non re-mesuré sauf mention contraire.

- **Le verrou est global** — `scripts/dream.sh:351`,
  `LOCK_FILE="${XDG_RUNTIME_DIR:-/tmp}/brain-v42-dream.lock"` + `flock -n 9`. Plusieurs unités
  systemd sont structurellement impossibles : les suivantes sortent vertes sans rien faire.
  **Une seule unité, une seule boucle.** À 55 projets cet argument ne s'affaiblit pas, il se
  renforce.
- **Sept chemins de journal sur douze gagnent une composante de projet**, cinq n'en gagnent pas
  (v1 §3.2). La consigne uniforme « projeter chaque chemin » produit du code faux dans les deux
  sens. Inchangé — sauf que la troncature de `codex_runner.py:419,432-433` frappe désormais
  jusqu'à 25 projets par nuit au lieu de 8.
- **La boucle est projet-majeur**, jamais phase-majeur (v1 §3.3) : `PHASE_DEPS`
  (`dream.sh:116-123`) réinjecte le rapport de la phase précédente **en relisant un fichier**
  (`:205-226`).
- **Deux exports survivent aux itérations** et doivent être remis à `[]` en tête de chaque
  itération de projet (v1 §3.4) : `PROMOTE_CANDIDATE_POOL_JSON`,
  `PROMOTE_RECENT_PROMOTIONS_JSON` (`dream.sh:504-510`, relus `:196-197`).
- **Quatre lignes de prompt portent `brain-v42` en dur** et c'est le seul défaut irréversible
  du lot (v1 §3.5) : `phase_synth.md:24,58`, `phase_promote.md:4`, `phase_connect.md:43`. Plus
  `promote_validate.py:67-179` qui ne vérifie **pas** `project_key`. À 55 projets, SYNTH
  écrirait des insights étiquetés `brain-v42` pour tout le monde. **Ces lignes tombent avant la
  boucle, pas avec elle.**
- **`extract`, `roadmap` et `sweep` sont globales** et ne reçoivent aucun `--project-key`
  (v1 §7). Elles sortent de la boucle et tournent une fois. `roadmap` garde sa propre rotation
  (`roadmap_curate.py:437-441,474-487`), mesurée à 30 projets, qui **n'est pas** le domaine de
  la boucle et ne doit jamais être lue comme une couverture.
- **`sweep` est irréversible et reste hors périmètre** (v1 §8) : livrée fermée et dry, jamais
  armée dans le même lot qu'un changement de topologie. Re-mesuré aujourd'hui : `sweep`
  **n'apparaît toujours pas** dans `dream_runs` —
  `SELECT phase, count(*) … GROUP BY phase` rend huit phases, sans elle.
- **La migration 042** (`dream_runs.project_key VARCHAR(64) NULL`, sans backfill, sentinelle
  `'*'` pour les phases globales, index `(run_date DESC, project_key)`) **précède la boucle**
  (v1 §5, §12). Re-vérifié aujourd'hui : `\d dream_runs` ne montre que `dream_runs_pkey(id)` et
  `idx_dream_runs_date(run_date DESC)`, `phase` est `character varying(10)`, et
  `dream_promotions.dream_run_id` la référence en `ON DELETE SET NULL`.
- **Isolation complète des échecs entre projets, aucun quorum, une seule alerte agrégée**
  (v1 §9, §11). Les compteurs deviennent des paires `projet/phase`. Les cinq `continue` de la
  boucle de phases (`:447,455,469,501,522`) sont à requalifier.
- **Le scope serveur existe, est testé, et est éteint** (v1 §14.1). Voir §1.3 : c'est le fait qui
  change de statut.

### 1.3 Le fait de la v1 qui change de statut : le scope éteint

La v1 §4.1 mesurait que dans cinq des six prompts de phase, `{{PROJECT_KEY}}` n'apparaît qu'en
prose, et que `brain_decay_status()` (`decay_tools.py:56`), `brain_consolidation_candidates`,
`brain_backfill_links_batch`, `brain_list_orphans_for_classification` et `brain_get_clusters`
portent une `DreamProjectToolPolicy()` **vide**. Le middleware
(`services/dream_project_scope.py`, `mcp/dream_capabilities.py:224-261`) est inerte :
`dream.sh:20` lit `BRAIN_DREAM_CAPABILITY_ENFORCEMENT="${…-false}"` et la variable est absente
du `.env` comme des trois drop-ins.

La v1 en faisait une **question ouverte**. À périmètre global elle ne peut plus l'être :

- à 8 projets, boucler sans scope coûtait 80 min pour le résultat de 15 — un gaspillage ;
- à ~25 projets par nuit, c'est **25 fois le même travail global**, et la boucle ne produit
  aucune couverture supplémentaire. Elle ne fabrique que des lignes `dream_runs` et de la
  consommation de jetons.

**Conséquence nommée, pas tranchée** : la boucle à fenêtre pleine n'a de sens qu'après le
remplissage des cinq politiques vides. Elle se **livre** avant (§8), fermée à un projet, parce
que toute l'infrastructure — curseur, journaux, compteurs, alerte — est indépendante du scope.
Elle ne s'**ouvre** pas avant. C'est l'étape 7 de §8.

Et une partie de cette spec n'attend pas le scope du tout : §4 et §5 travaillent **par entité**,
pas par run. Elles produisent un effet dès la première nuit, sur un seul projet.

---

## 2. Le curseur : structure, persistance, reprise, apparition, disparition, famine

### 2.1 Ce qu'il doit résoudre, mesuré

Domaine d'itération et masse, mesurés aujourd'hui :

```sql
WITH a AS (SELECT project_key FROM learnings UNION ALL SELECT project_key FROM decisions
           UNION ALL SELECT project_key FROM snippets UNION ALL SELECT project_key FROM runbooks
           UNION ALL SELECT project_key FROM adrs)
SELECT coalesce(project_key,'(null)'), count(*) FROM a GROUP BY 1 ORDER BY 2 DESC;
```

| fait | valeur mesurée |
|---|---|
| `project_contexts` | **55** (voir §2.5 : c'était 54 il y a trente minutes) |
| artefacts (5 tables de connaissance) | **3 803** |
| artefacts sans `project_key` | **0** — voir l'encadré ci-dessous |
| clés d'artefact **sans** `project_context` | **9** (`openclaw` 7, `red-backup` 4, `red-cli` 2, `red-feed`, `lyriks-backend-v2`, `red-dataset`, `red-life`, `hawkixs-infra`, `hk-anime-list` — 19 artefacts) |
| `project_contexts` **sans aucun artefact** | **9** (`red-daemon`, `red-llm`, `red-e2e-target`, `red-lab:developer-gemini`, `red-tsdb`, `red-api`, `red-lab:developer-opus`, `red-alerts`, `red-lab-factory`) |
| projets où le dream a déjà synthétisé | **1** — la même requête sur `learnings`, `snippets` et `decisions` rend **une seule ligne, `learnings / brain-v42 : 87`**. Aucun snippet ne porte le tag, alors que `phase_synth.md:30` en fait produire |

> **Le « 26 artefacts sans `project_key` » n'existe pas.** La première écriture de cette spec
> l'affichait dans la colonne « valeur mesurée » et le répétait en §10. Il vient de la v1 §1
> (« 26 de ces artefacts portent `project_key IS NULL` ») et n'a pas été rejoué. Rejoué :
>
> ```sql
> SELECT count(*) FILTER (WHERE project_key IS NULL) FROM learnings;  -- et les 4 autres
> → learnings 0 · decisions 0 · snippets 0 · runbooks 0 · adrs 0 · indexed_plans 0
> ```
>
> Zéro, sur les cinq tables de connaissance **et** sur `indexed_plans`. La seule table du schéma
> qui porte encore des `project_key` nuls est `brain_entities` (**13** sur 4 970) et
> `search_log` (275 sur 1 351) — ni l'une ni l'autre n'est un artefact de connaissance. C'est
> exactement le défaut que la §0 prétend interdire : un chiffre hérité, republié sous l'étiquette
> « mesuré ». Le domaine d'itération n'en est pas affecté ; la leçon, si.

**Le corpus bouge pendant qu'on le mesure.** La masse totale est passée de **3 803 à 3 804** et
celle de `brain-v42` de **658 à 659** entre le début et la fin de cette session (~40 min). Les
deux valeurs apparaissent dans cette spec selon l'instant de la mesure ; c'est l'écart réel, pas
une coquille, et il vaut la même leçon que §2.5.

**Le domaine d'itération et le domaine des artefacts ne coïncident pas.** Neuf contextes ne
portent rien ; neuf clés portent des artefacts sans contexte. Un curseur qui boucle sur les
`project_contexts` ne verra jamais les 19 artefacts des neuf clés orphelines ; un curseur qui
boucle sur les clés d'artefact ne verra jamais les neuf contextes vides. **Ce choix se déclare.**

**Tranché** : le curseur itère sur `project_contexts`. Raison : c'est le domaine que les phases
savent adresser (`--project-key`, `brain_get_project`, la roadmap, le focus), et une clé sans
contexte n'a pas de focus à mettre à jour ni de roadmap à curer. Les 19 artefacts orphelins
deviennent un **signal de rattachement** — la même famille que le chantier
`2026-07-27-orphan-project-key-bind-design.md` — et non un cas de boucle. À nommer dans le
rapport nocturne, pas à traiter dans la boucle.

Durées, re-mesurées sur les 30 dernières nuits (méthode de la v1 §4.2, nuit par nuit) :

```sql
WITH n AS (SELECT run_date,
  sum(duration_s) FILTER (WHERE phase IN ('scan','clean','connect','synth','promote','reorg')) agent_s,
  sum(duration_s) FILTER (WHERE phase IN ('extract','roadmap','sweep')) global_s
 FROM dream_runs WHERE run_date >= (SELECT max(run_date) FROM dream_runs) - 29 GROUP BY 1)
SELECT avg(agent_s), percentile_cont(0.9) WITHIN GROUP (ORDER BY agent_s), max(agent_s), … FROM n;
```

| sous-total | moyenne | p90 | max |
|---|---|---|---|
| phases **agent** (les six de la boucle) | **9,28 min** | **14,73 min** | **30,50 min** |
| phases **globales** (extract, roadmap, sweep) | 5,77 min | — | **42,26 min** |

(Le max global de 42,26 min dépasse le plafond configuré de 35 min des trois `timeout` : il
inclut des re-runs manuels, comme la v1 l'avait déjà noté pour sa propre série.)

### 2.2 La structure : une date par projet, pas un pointeur global

**Tranché : le curseur est un état par projet — une date d'essai — et non un index dans une
liste.**

L'argument est structurel. La liste des projets est **re-triée chaque nuit** par la priorité
humaine (§3), qui bouge à chaque lecture. Un pointeur « on s'est arrêté au 25ᵉ » désigne un
projet différent d'une nuit à l'autre sans que rien n'ait été consolidé entre-temps : il ne
garantit **aucune** couverture. Une date par projet est stable sous insertion, sous suppression
et sous re-tri — c'est la seule forme dont la propriété « tout finit couvert » se démontre.

Forme minimale proposée :

```
dream_project_cursor(
  project_key      TEXT PRIMARY KEY,   -- pas de FK, cf. §2.6
  last_attempt_date DATE NOT NULL,     -- avancé à l'ADMISSION, pas au succès
  last_success_date DATE,              -- NULL = jamais réussi
  attempts          INT  NOT NULL,
  consecutive_failures INT NOT NULL
)
```

`last_attempt_date` porte la couverture. `last_success_date` porte la santé, et c'est lui qui
répond à « ce projet a-t-il jamais été consolidé ? » — la question dont §6 fait un préalable de
purge.

### 2.3 Où il persiste : sa propre table, ni `project_contexts`, ni `dream_runs`

**Pas dans `project_contexts`.** Mesuré :

```
docker exec brain_v42_postgres psql -U brain -d brain -c "\d project_contexts"
→ trg_project_contexts_updated BEFORE UPDATE ON project_contexts
     FOR EACH ROW EXECUTE FUNCTION set_project_context_updated_at()
```

et le corps de la fonction, lu dans `pg_proc`, réécrit `NEW.updated_at := CURRENT_TIMESTAMP`
sur **toute** UPDATE, sauf sous le réglage explicite
`brain_v42.allow_explicit_project_context_updated_at`. Une écriture de curseur par nuit et par
projet ferait donc paraître **tous** les projets « mis à jour cette nuit », et détruirait le
signal d'inactivité dont la purge a besoin (§6). C'est exactement l'erreur que la 040 a corrigée
pour le focus, à recommettre à l'échelle du projet. S'y ajoute que `project_contexts` porte
**sept** triggers et un compare-and-swap sur `focus_revision` : y brancher un écrivain nocturne
crée une surface de concurrence avec `brain_update_project_focus` pour un gain nul.

**Pas dérivé de `dream_runs.project_key`** — et c'est la tentation à écarter explicitement,
parce que la 042 rendrait la dérivation gratuite (« le prochain projet = celui dont le
`max(run_date)` est le plus ancien »). Elle échoue sur un cas précis : le curseur n'avancerait
que si une ligne est écrite. Or la v1 §5 avait mesuré que trois des cinq sites d'INSERT dans
`dream_runs` avalent leur exception en best-effort (`ticket_extract.py:752`,
`roadmap_curate.py:1146`, `maintenance/session_sweep.py:94`, sur le modèle
`except Exception: print(f"! warning: …")`). **Remesuré le 2026-08-09, c'est pire, et l'argument en
sort renforcé : les CINQ perdent leur trace en silence** (§14.2). Le cinquième, `dream_parser` —
celui qui porte justement toute la télémétrie par projet — lève bien en Python, mais son
orchestrateur l'attrape : `dream.sh:338`, `WARN dream_parser failed for $name (non-fatal)`. Le seul
écrivain dont la dérivation aurait eu besoin est donc lui aussi silencieux à l'échec. Un projet
dont les phases meurent avant d'écrire
quoi que ce soit garderait un curseur `NULL`, donc **la tête de file, toutes les nuits, pour
toujours** — il consommerait le début de chaque fenêtre et bloquerait la rotation. C'est la
famine, et elle serait invisible.

**Règle qui en découle, non négociable : le curseur avance à l'ADMISSION, jamais au succès.**
`last_attempt_date` est écrit avant la première phase du projet, dans sa propre transaction.
Si cette écriture échoue, le projet **n'est pas admis** — fail-closed, sinon on rouvre le trou
qu'on vient de fermer.

`dream_runs.project_key` (042) reste utile et reste livré : c'est la **télémétrie** (quelle
phase a tourné, pour quel projet, avec quel statut). Le curseur est l'**ordonnancement**. Les
confondre, c'est faire dépendre la couverture d'une table dont trois écrivains sont
best-effort.

### 2.4 La reprise

À l'ouverture de la fenêtre, une seule requête, sans état caché :

```
ORDER BY  last_attempt_date ASC NULLS FIRST,   -- 1. la couverture
          priorite_humaine   DESC,             -- 2. l'ordre dans la bande (§3)
          masse_artefacts    DESC,             -- 3. départage stable
          project_key        ASC               -- 4. déterminisme total
```

`run_date` et `last_attempt_date` sont des **dates**, pas des instants : tous les projets servis
la même nuit se retrouvent dans **la même bande d'égalité**. À ~25 projets par nuit, la bande
fait 25 éléments — la priorité humaine y ordonne réellement quelque chose, ce qui ne serait pas
le cas avec un horodatage à la seconde. **C'est une propriété du type de colonne, à ne pas
« améliorer » en `TIMESTAMPTZ` sans détruire l'effet.**

La couverture se lit alors comme une arithmétique, pas comme une promesse : chaque nuit sert les
N plus anciens ; un projet servi passe en queue ; aucun projet ne peut être dépassé deux fois par
le même autre.

**Cette propriété a été simulée, pas seulement affirmée.** La question que la revue devait
trancher : *un projet à priorité humaine nulle est-il jamais atteint, ou la tête de liste est-elle
réordonnée devant lui pour toujours ?* Sur les 55 projets réels — dont **44 ont une priorité
strictement nulle** et 9 une masse nulle — la simulation de l'ordre ci-dessus donne :

- **aucun projet n'est jamais servi**, sur 60 nuits, dans les cinq configurations testées (§2.5) ;
- le pire projet selon la clé de tri (`red-alerts` : priorité 0, masse 0, dernier alphabétique de
  sa bande) est servi à la **nuit 7** dans la configuration la plus restrictive (fenêtre 14,
  plafond 7) ;
- l'intervalle de revisite en régime établi est **borné** : 3 nuits à fenêtre 25, 4 nuits à
  fenêtre 14, jamais davantage.

La raison est structurelle et vaut plus que la simulation : la clé primaire est une **date**, et
un projet servi reçoit la date du jour, donc la plus récente de toutes. Il ne peut pas redevenir
prioritaire sur un projet non servi. Le tri est un FIFO strict sur les dates, et la priorité
n'ordonne qu'**à l'intérieur** d'une classe d'équivalence. **Le cliquet de D3 ne revient pas par
la porte du tri.**

### 2.5 Apparition d'un projet — cas observé pendant l'écriture de cette spec

Au début de cette session, `SELECT count(*) FROM project_contexts` rendait **54**. Trente
minutes plus tard, la même requête rend **55**, et
`SELECT project_key, created_at FROM project_contexts ORDER BY created_at DESC LIMIT 3` rend
`perso | 2026-08-08 03:11:29+00`, deux minutes avant la mesure. **Le domaine bouge sous le
curseur, ce n'est pas une hypothèse.**

Comportement : un projet neuf n'a pas de ligne de curseur, `NULLS FIRST` le met en tête, il est
servi la nuit suivante. C'est le bon comportement par défaut — un projet qui vient d'être créé
est celui dont on parle.

**Le risque est le lot, pas l'unité.** Rythme de création mesuré :

```sql
SELECT date_trunc('month',created_at)::date, count(*) FROM project_contexts GROUP BY 1 ORDER BY 1;
→ jan 6 · fév 8 · mars 25 · avr 2 · mai 1 · juin 3 · juil 7 · août 3 (8 jours)
```

**Mars 2026 : 25 contextes en un mois.** Un mois comme celui-là remplit une nuit entière de
projets jamais servis et repousse d'un tour la rotation de tous les autres. Un lot de 25
créations le même jour la remplit **exactement**.

**Tranché** : plafonner la part de la fenêtre allouée aux projets jamais servis. Ce n'est pas une
constante physique ; elle se re-mesure après un trimestre. Le plafond est un **plancher de
rotation**, pas une exclusion : la moitié basse de la fenêtre reste ouverte aux nouveaux la nuit
suivante.

**Le chiffrage de la première écriture était faux sur trois points, et la revue les a mesurés.**
Elle proposait « la moitié de la fenêtre, soit ~12 unités sur ~25 à la moyenne », présenté comme
« le plus petit plafond qui absorbe un mois-de-mars en deux nuits ».

1. **Deux nuits, non.** 25 créations à 12 par nuit font **trois** nuits (12 + 12 + 1). Le plus
   petit plafond qui tienne en deux nuits est **13**.
2. **~25, non plus.** §7.3 refuse explicitement de planifier sur la moyenne et retient le p90,
   soit **~14 projets par nuit**. La moitié de la fenêtre sur laquelle la spec planifie vaut donc
   **7**, pas 12 — et 25 créations mettent alors **quatre** nuits.
3. **Le plafond mord surtout à la mise en service, et §7.3 l'ignorait.** La nuit de bascule,
   **les 55 projets sont « jamais servis »**. Le plafond s'applique donc à tout le monde, et il
   n'existe aucun projet déjà servi pour remplir le reste de la fenêtre : la moitié basse tourne
   à vide.

Simulation de l'ordre de §2.4 sur les **55 projets réels**, avec leurs priorités et leurs masses
mesurées (compteur humain de §3.1) :

| fenêtre | plafond neufs | admis nuits 1-5 | couverture complète | revisite max en régime |
|---|---|---|---|---|
| 25 | aucun | 25 · 25 · 25 · 25 · 25 | **nuit 3** | 3 nuits |
| 25 | 12 | **12** · 24 · 25 · 25 · 25 | **nuit 5** | 3 nuits |
| 14 | aucun | 14 · 14 · 14 · 14 · 14 | **nuit 4** | 4 nuits |
| 14 | **7** (la moitié) | **7** · 14 · 14 · 14 · 14 | **nuit 8** | 4 nuits |
| 14 | 12 | 12 · 14 · 14 · 14 · 14 | **nuit 5** | 4 nuits |

**Le plafond double le délai de première couverture** (8 nuits au lieu de 4 au p90) et perd la
moitié de la première nuit. Les « 2,5 / 4,0 / 8,2 nuits » de §7.3 sont calculés **sans** lui ;
avec lui, lire la colonne « couverture complète » ci-dessus.

**Tranché après mesure** : le plafond ne s'applique **qu'à partir du moment où il existe des
projets déjà servis**, c'est-à-dire jamais pendant la première rotation. Formellement, il
plafonne la part des neufs **relativement aux candidats disponibles**, pas dans l'absolu :
`min(plafond, fenêtre − nb_de_projets_déjà_servis_éligibles)` ne s'applique que si le second
terme est positif. À l'état d'équilibre il vaut 7 sur une fenêtre de 14 ; à la bascule il
disparaît. C'est ce qui rend les deux paragraphes compatibles au lieu de contradictoires.

### 2.6 Disparition d'un projet

Un `project_context` supprimé (§6) laisse une ligne de curseur orpheline.

**Tranché : pas de clé étrangère entre `dream_project_cursor.project_key` et
`project_contexts`.** Les deux comportements disponibles sont mauvais :

- `ON DELETE RESTRICT` ferait du curseur un **veto sur la purge** — la ligne d'ordonnancement
  empêcherait de supprimer le projet, ce qui n'a aucun sens et se découvrirait au pire moment ;
- `ON DELETE CASCADE` effacerait silencieusement la **preuve qu'on a visité ce projet**, qui est
  précisément le préalable dont §6 fait le ticket d'entrée de la purge. Purger un projet
  effacerait la trace autorisant à le purger.

La sélection nocturne fait donc un `JOIN` sur `project_contexts` : une ligne orpheline est
**inerte**, jamais sélectionnée, et jamais perdue. Le rapport de nuit compte les lignes de
curseur sans contexte — c'est le seul endroit où l'on saura qu'un projet a disparu.

Comparaison mesurée pour montrer que ce n'est pas théorique :
`brain_sessions.project_key → project_contexts` est déclarée **`ON DELETE RESTRICT`**, et
`SELECT count(DISTINCT project_key), count(*) FROM brain_sessions` rend **17 projets, 363
sessions**. Dix-sept projets sur 55 sont déjà indélébiles à cause d'une FK écrite pour une autre
raison. Ajouter une seconde FK au même endroit sans y penser, c'est refaire cette contrainte à
l'aveugle.

### 2.7 La famine — les quatre mécanismes, et celui qui reste ouvert

1. **Avancer à l'admission** (§2.3). Un projet qui plante à la première phase a quand même
   consommé son tour. Sans ça, un projet cassé mange la tête de fenêtre toutes les nuits.
2. **Bande d'égalité à la journée** (§2.4). La priorité humaine ne peut réordonner qu'à
   l'intérieur d'une nuit, jamais sauter une bande. Un projet très lu ne peut pas passer deux
   fois avant un projet jamais lu.
3. **Plafond des jamais-servis** (§2.5). Un lot de créations ne suspend pas la rotation.
4. **Le projet lent ne mange pas le suivant** : l'admission se décide sur le **temps restant**
   dans la fenêtre (§7), pas sur un compteur de projets. Un projet qui déborde repousse la
   frontière d'admission, il ne vole pas un tour à un autre — celui qui n'est pas admis garde sa
   date de curseur ancienne et repasse **en tête** la nuit suivante.

**Reste ouvert, mais moins grand qu'annoncé** : un projet qui déborde *systématiquement* (par
exemple un corpus qui fait timer SYNTH toutes les nuits) consomme chaque nuit une part importante
de la fenêtre, tout en avançant son curseur. Il ne bloque personne — mais il réduit N pour tout le
monde, à répétition.

La première écriture disait « une part démesurée » et « il n'existe aucune donnée pour calibrer un
plafond par projet ». **La revue a mesuré que le plafond par projet existe déjà, en dur.**
`PHASES` (`dream.sh:106-113`) donne à chaque phase son propre `timeout Nm` (`:260`) :
`5+5+8+15+10+10 = 53 min`, et le retry (`:539`, jamais sur `promote`) ajoute au pire `43 min`.

```
plafond structurel d'un projet   = 53 min sans retry, 96 min avec
part maximale de la boucle (205) = 25,9 % sans retry, 46,8 % avec
```

Un projet ne peut donc pas dépasser **47 %** de la fenêtre, quoi qu'il arrive, et il en laisse
toujours assez pour au moins un autre. Ce qui manque n'est pas un plafond, c'est la
**distribution empirique** : aucune nuit à plus d'un projet n'a jamais tourné (v1 §9). La spec
refuse donc de resserrer ce plafond au doigt mouillé et fixe la sortie : `consecutive_failures` et
l'écart `last_attempt_date − last_success_date` sont **persistés dès le premier lot** pour que la
question soit tranchable sur mesure après quelques nuits. Poser un seuil plus fin aujourd'hui
serait exactement l'erreur que la v1 §9 a refusée pour le quorum d'échec.

### 2.8 Les projets vides

Neuf `project_contexts` ne portent aucun artefact (mesuré §2.1). Les servir coûte un budget
d'agent complet pour produire zéro consolidation.

**Proposé — et c'est une inférence, pas une décision de l'opérateur** : un projet à masse nulle
est **sauté**, avec `last_attempt_date` quand même avancé et un motif `skipped_empty` journalisé.
L'argument est celui de D3 retourné : le cliquet que D3 refuse suppose qu'une consolidation
*aurait produit quelque chose* qu'on aurait ensuite trouvé, lu, et remonté. Une masse nulle ne
peut pas amorcer ce cycle — sauter un projet vide ne referme aucune boucle. Un artefact créé
demain rend le projet non vide et il est servi au tour suivant.

Si l'opérateur refuse ce saut, le coût est chiffré : neuf projets × un budget d'agent chacun,
tous les ~3 tours de rotation.

---

## 3. La priorité humaine : ordonner sans filtrer

### 3.1 Pourquoi un filtre serait un cliquet — chiffré

L'opérateur a tranché (D3) et l'argument est mesurable. Lectures humaines sur **tout** le corpus,
toutes tables confondues :

```sql
WITH a AS (SELECT access_count ac, access_count_human ach FROM learnings
           UNION ALL … decisions, snippets, runbooks, adrs)
SELECT count(*), count(*) FILTER (WHERE ac=0), count(*) FILTER (WHERE ac>0 AND ach=0),
       count(*) FILTER (WHERE ach>0) FROM a;
```

| | artefacts | part |
|---|---|---|
| total | **3 803** | 100 % |
| **jamais lus par personne** | **1 180** | 31,0 % |
| lus par la machine seule | **2 522** | 66,3 % |
| lus au moins une fois par un « humain » | **101** | **2,66 %** |

Un filtre sur le compteur humain retiendrait **2,66 %** du corpus. Les 1 180 artefacts que
personne n'a jamais ouverts ne seraient jamais consolidés, donc jamais remontés en recherche,
donc jamais lus. La boucle se refermerait sur 97 % du brain. **D3 est confirmé par la mesure,
pas seulement par le raisonnement.**

Au niveau projet, c'est encore plus net — 11 projets sur 55 portent la moindre lecture humaine :

| projet | lectures humaines | artefacts lus | masse |
|---|---|---|---|
| red | 36 | 22 | 872 |
| red-shrik | 29 | 16 | 222 |
| red-monitor | 22 | 14 | 77 |
| brain-v42 | 21 | 19 | 659 |
| auto-discord | 11 | 9 | 113 |
| datalake-v1 | 9 | 8 | 74 |
| red-gift | 9 | 5 | 73 |
| red-arena | 4 | 4 | 25 |
| red-orchestrator | 2 | 1 | 10 |
| red-quant | 2 | 2 | 24 |
| red-lab | 1 | 1 | 161 |

Filtrer, c'est passer de 55 projets à 11.

### 3.2 Comment elle ordonne

La priorité humaine est la **deuxième** clé de tri (§2.4), à l'intérieur de la bande d'égalité
du curseur. Elle ne décide **jamais** de l'éligibilité.

Définition proposée de la clé, par projet :
`somme(access_count_human)` sur les cinq tables, **et** `count(*) FILTER (WHERE ach>0)` en
départage. Deux signaux plutôt qu'un parce qu'ils ne disent pas la même chose : `red` a 36
lectures sur 22 artefacts (large), `red-shrik` en a 29 sur 16 (concentré). Le second est plus
robuste à un unique artefact très relu.

**Ce qu'elle n'est pas** : une pondération de la masse. `red-lab` (161 artefacts, 1 lecture)
passe après `red-arena` (25 artefacts, 4 lectures). C'est voulu : la clé mesure l'attention, pas
le volume. Le volume est la troisième clé, et il ne sert qu'à départager.

### 3.3 Ce qu'elle fait pendant la période de chauffe

**Le compteur a deux jours et aucun backfill.** `access_count_human` est arrivé avec la 041,
appliquée le 2026-08-06 (CLAUDE.md, bascule mesurée) ; nous sommes le 2026-08-08. Distribution
complète, mesurée :

```sql
WITH a AS (SELECT access_count_human ach FROM learnings UNION ALL … )
SELECT ach, count(*) FROM a GROUP BY 1 ORDER BY 1;
```

| `access_count_human` | 0 | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|---|
| artefacts | **3 702** | 72 | 18 | 7 | 3 | 1 |

**Trois faits à assumer pendant la chauffe :**

1. **La clé est quasi constante.** 97,3 % des artefacts sont à zéro, et 44 projets sur 55 ont une
   somme nulle. Le tri dégénère donc en `curseur → masse → clé`. Il ne devient pas **faux**, il
   devient **aveugle**. Et c'est exactement pourquoi la priorité doit être une clé de tri : un
   tri aveugle couvre quand même tout le corpus ; un **filtre** aveugle ne consoliderait rien
   pendant deux jours, puis 2,66 % du corpus.
2. **Le compteur est monotone et sans décroissance** : il ne peut que monter. Un projet ne peut
   donc que **gagner** des rangs à mesure que la chauffe avance ; aucun projet ne perd sa place
   pour une raison qui n'est pas une lecture réelle d'un autre. La chauffe converge, elle
   n'oscille pas.
3. **Il ne faut pas attendre.** Rien dans la conception ne dépend d'un compteur chaud ; le
   curseur porte seul la garantie de couverture. Attendre la chauffe pour livrer serait attendre
   un signal que seule la livraison produit — les lectures humaines passent par les mêmes outils.

### 3.4 Le nom du compteur ment, et c'est mesurable dans le code

`src/brain_v42/provenance.py:26-27` :

```python
# Préfixes des acteurs système qui se déclarent. Un acteur absent de cette
# liste et non sentinelle est traité comme humain.
_SYSTEM_ACTOR_PREFIXES = ("dream-codex-",)
```

`access_count_human` compte donc **toute lecture qui ne vient pas du dream nocturne**, y compris
celle d'un agent automatique tiers. Le classement de §3.1 doit se lire avec ça : `red-shrik`
(29 « lectures humaines », 2ᵉ rang) porte un corpus d'alertes de supervision auto-générées
(échantillon mesuré : `vps cpu_total warning streak 1h`, `pc-serveur load_avg1 streak 1/h`, …).
Qu'un agent `red-shrik` relise ses propres alertes est plausible et compterait comme humain.

**Non mesurable aujourd'hui** : `SELECT count(*) FROM access_log` rend **0**. C'est normal —
`PgAccessLogRepo.aggregate_in_session` supprime les lignes agrégées et `purge_old(30)` finit le
travail — mais la conséquence est réelle : `access_log.actor`, la colonne que la 041 a ajoutée
pour rendre l'identité visible, **ne conserve aucun historique**. On ne peut pas vérifier
a posteriori qui a lu quoi. Le compteur est un agrégat sans journal.

À faire dans le même lot, et ce n'est pas cosmétique : **renommer la notion dans les rapports**
(« lectures hors-dream », pas « lectures humaines »), ou étendre `_SYSTEM_ACTOR_PREFIXES` aux
agents connus. Tant que ce n'est pas fait, tout tableau qui affiche « humain » ment de la même
manière que `dream_runs` mentait sur le projet.

---

## 4. Le decay : réunir les deux notions de fraîcheur

### 4.1 L'état mesuré, qui n'est pas tout à fait celui du brief

Le brief posait : deux notions qui ne se parlent pas, `freshness_status` écrit par un jugement
LLM, un seul écrivain côté code qui remet à `fresh`. La lecture du code et la mesure du corpus
donnent une image **plus précise et plus gênante**.

**Il y a quatre écrivains de `freshness_status`, pas un :**

| écrivain | fichier:ligne | ce qu'il écrit |
|---|---|---|
| **le score lui-même** | `services/decay_flusher.py:204-215` | `new_status = self._decay_calculator.freshness_status(multiplier)`, écrit en UPDATE quand il diffère |
| la **fusion** | `services/consolidation.py:171` | `merged_into=target, freshness_status="archived"` sur la source |
| le **jugement** REORG | `scripts/dream/phase_reorg.md:82` via `brain_update` | `"archived"` seulement (`:117` interdit `fresh` et `stale`) |
| le **revive** | `mcp/tools/decay_tools.py:141` | `"fresh"` |

**Donc le lien score → statut existe déjà.** Il est simplement **affamé deux fois**, et c'est ça
le vrai défaut de conception.

**Famine n°1 — il ne se déclenche que sur une lecture.** `DecayFlusher._flush` ne recalcule que
les entités remontées par `aggregate_in_session`, c'est-à-dire celles qui ont une ligne dans
`access_log`, c'est-à-dire celles **qu'on vient de lire**. Les **1 180 artefacts que personne n'a
jamais lus** (§3.1) n'ont jamais eu leur multiplicateur calculé une seule fois. Le cliquet de D3
existe donc aussi au niveau de l'entité : *ce que personne ne lit n'est même pas évalué*.

**Famine n°2 — au moment où il calcule, la réponse est déjà décidée.** Arithmétique sur les
constantes de `decay.py` (ce n'est pas une mesure d'exécution) : le flusher passe
`last_accessed_at = stats["max_accessed"]`, c'est-à-dire un instant vieux de quelques minutes.
`access_factor ≈ 1`. Le plancher du multiplicateur est donc :

```
m ≥ w_access·1 + w_valid·0,7
learning / decision / snippet / runbook / plan : 0,30 + 0,14 = 0,44
adr                                            : 0,20 + 0,35 = 0,55
```

et `archive_threshold = 0,2`. **Le flusher ne peut mathématiquement jamais écrire `archived`.**
Il peut écrire `stale` (plancher 0,44 < seuil 0,5), et c'est ce qu'on observe :

```sql
SELECT project_key, created_at::date, updated_at::date, access_count, access_count_human,
       merged_into IS NOT NULL FROM snippets WHERE freshness_status='stale';
→ 3 lignes, access_count=1, access_count_human=0, merged_into NULL
```

Trois snippets `stale`, non fusionnés, avec exactement une lecture machine. `phase_reorg.md:117`
interdit à REORG d'écrire `stale` : **ce sont des écritures du flusher**, la signature du seul
chemin score → statut qui fonctionne. Il fonctionne, et il ne peut produire qu'un seul des trois
états.

### 4.2 Le « fait troublant » est expliqué, et il se réduit à une seule ligne

Le brief signalait 329 archivages inexpliqués (`red-shrik:agent` 205, `red` 124, …) alors que
REORG ne tourne que sur `brain-v42`. Mesure d'aujourd'hui — 359 learnings archivés, et la
question est mal posée :

```sql
SELECT merged_into IS NOT NULL, freshness_status, count(*) FROM learnings GROUP BY 1,2;
→ merged=t / archived : 348      merged=f / archived : 11
```

**348 des 359 archivages sont des pierres tombales de fusion**, écrites par
`consolidation.py:171` — pas un jugement, un effet de bord mécanique du merge. Le compte par
projet du brief additionnait les tombes et les jugements.

Les **11 vrais jugements** :

```sql
SELECT project_key, count(*) FROM learnings
WHERE freshness_status='archived' AND merged_into IS NULL GROUP BY 1;
→ brain-v42 : 10      red-shrik:agent : 1
```

Dix sur `brain-v42` — le projet où REORG tourne. **Un seul ailleurs**, et il est datable :

```sql
SELECT id, created_at::date, updated_at, access_count, left(topic,60) FROM learnings
WHERE freshness_status='archived' AND merged_into IS NULL AND project_key='red-shrik:agent';
→ a68af001… | 2026-04-21 | 2026-06-30 04:20:57+00 | 4 | test_phase2_red-shrik
```

et la nuit correspondante, dans `dream_runs` :

```sql
SELECT phase, status, created_at, duration_s FROM dream_runs WHERE run_date='2026-06-30';
→ … promote 04:15:40 · reorg 04:21:47 (366 s)
```

L'archivage tombe **à 04:20:57, à l'intérieur de la fenêtre REORG de cette nuit-là**
(≈04:15:41 → 04:21:47). Le sujet est `test_phase2_red-shrik` — une pollution de test, exactement
la famille que la liste blanche de `phase_reorg.md:64` autorise à archiver. Et `brain_update` ne
porte **aucune garde de projet** tant que le scope est éteint (§1.3).

**Explication la plus simple, compatible avec toutes les mesures : le REORG nocturne de
`brain-v42` a archivé une entité d'un autre projet, une fois, le 2026-06-30.** Aucun lancement
manuel de `dream.sh <projet>` n'est nécessaire pour expliquer quoi que ce soit.

**Ce qui reste non prouvé, et qui est dit** : `updated_at` est réécrit par **toute** UPDATE de la
ligne — `trg_learnings_updated` existe (`SELECT tgname FROM pg_trigger WHERE
tgrelid='learnings'::regclass`), et le flusher écrit `access_count` sans changer le statut. La
coïncidence de fenêtre est un **faisceau**, pas une trace. `dream_runs` n'a pas de colonne de
projet (la 042 n'est pas appliquée) et `access_log` est vide : **il n'existe aucune trace
d'audit capable de trancher**, et il n'y en aura pas pour le passé.

### 4.3 Ce qui entre dans la nuit : le lien, pas le calcul

**Tranché, et cette phrase est la contrainte principale de la §4 : le calcul reste à la volée.**
`brain_service.py:336-347` calcule le multiplicateur à chaque recherche, avec un
`last_accessed_at` frais à la milliseconde. Le persister la nuit le rendrait périmé de 24 h.
Ce serait une **régression**, pas une optimisation.

Ce qui entre dans la boucle, c'est **le lien** : le score produit les candidats, REORG juge.

**Interface, précisément.** Un producteur de candidats **en lecture seule**, appelé par REORG
au début de sa Partie 2 :

```
brain_decay_candidates(project_key, limit=20, entity_types=[...])
→ [ { id, type, topic, multiplier,
      age_factor, access_factor, freq_factor, validation_factor,   ← les quatre termes, séparés
      access_count, access_count_human, age_days, last_access_days,
      freshness_status } … ]   trié par multiplier ASC
```

Sept propriétés obligatoires :

1. **Un seul calcul dans le dépôt.** Le tool appelle `DecayCalculator.compute_multiplier`, il ne
   ré-implémente pas la formule en SQL. La réplique SQL de cette spec (§0) existe pour mesurer,
   pas pour être copiée : deux formules qui dérivent, c'est le défaut qu'on est en train de
   corriger, pas une méthode.
2. **Les quatre termes sont rendus séparément.** Un juge à qui on donne `0,2231` ne peut rien en
   faire. `age_factor=0,03 · access_factor=0,03 · freq=0,00 · valid=0,70` se lit : *vieux, jamais
   relu, jamais validé*.
3. **Rang, pas seuil.** Voir §5.4 : `archive_threshold=0,2` ne sélectionne **rien** sur ce corpus
   (minimum mesuré : 0,2228). Le producteur rend les N plus bas, où N est borné par le cap
   existant de REORG (20, `phase_reorg.md:85`).
4. **Le juge reste, et le verdict reste réversible.** Le score **propose**, REORG **décide**, et
   la décision est `freshness_status='archived'` — annulable par le revive
   (`decay_tools.py:141`). Rien d'irréversible n'entre dans ce chemin ; l'irréversible est en §6.
5. **La garde de compteur de REORG doit changer, mais pas pour la raison publiée d'abord.**
   `phase_reorg.md:119` : « **NEVER archive** an entity with `access_count > 5` » — le compteur
   **total** ; la même règle est répétée à l'étape (b) de la Partie 2 (`access:N` où `N > 5`).
   Mesuré : 508 learnings sur 2 742 (18,5 %) ont `access_count ≥ 10`, et **zéro** a
   `access_count_human ≥ 10`. La première écriture en concluait : « livrer le producteur sans ce
   changement, c'est brancher un nominateur sur un veto qui l'annule ». **La revue a mesuré
   l'intersection, et elle est vide** :

   | rang dans le classement du producteur | candidats à `access_count > 5` |
   |---|---|
   | 20 premiers (le `limit` spécifié) | **0** |
   | 100 premiers | **0** |
   | 500 premiers | 23 (compteur humain) · 0 (compteur total) |

   Le veto ne mord qu'à partir du **rang 264** avec le compteur humain, et jamais avant le rang
   1 000 avec le compteur total. La raison est structurelle : un `access_count` élevé s'accompagne
   d'un `last_accessed_at` récent, donc d'un `access_factor` proche de 1, donc d'un multiplicateur
   **haut** — l'ensemble vetoté et l'ensemble nominé sont aux deux bouts du même classement. La
   garde reste à changer (elle protège la pollution auto-générée, et c'est le sens de la 041),
   mais **elle ne bloque pas le producteur** et ce n'est pas un préalable de livraison.

6. **La vraie porte, c'est la liste blanche — et elle ferme à 100 %.** C'est le défaut de
   conception que la première écriture a manqué. La Partie 2 de `phase_reorg.md:64-71` n'autorise
   REORG à archiver **que** les entités dont le sujet matche l'un de six motifs :
   `^test_`, `^verify_.*_test$`, `^infra_status`, `^status_infra_`, `^cpu_metrics_`,
   `^.*_events_\d+h$` — et l'étape (a) impose de « vérifier le match regex » avant toute chose.
   Mesuré, en appliquant les six motifs au classement du producteur :

   | population | matche la liste blanche |
   |---|---|
   | 20 plus bas multiplicateurs | **0** |
   | 100 plus bas | **0** |
   | 500 plus bas | **0** |
   | corpus entier non archivé (2 343 learnings) | **7** |

   **Zéro sur cinq cents.** Le bas de classement de §5.4 (`Neo4j Persistence Fix`,
   `Grafana metrics fix`, les neuf `Lyriks Design Conventions …`) ne matche aucun motif. Livré tel
   quel, `brain_decay_candidates` remet chaque nuit à REORG vingt entités que son propre prompt lui
   interdit d'archiver : **le lien est un no-op, pas un lien.**

   **Tranché : le lot ne se livre pas sans trancher le mandat de la Partie 2.** Trois options, et
   il faut en nommer une :

   - **(a) Ne rien changer à la liste blanche.** Le producteur devient un instrument de
     *rapport* : REORG le lit, le journalise dans le rapport nocturne, n'agit pas. Honnête, sans
     risque, et c'est déjà mieux que rien — mais ce n'est plus « le score propose, REORG décide ».
   - **(b) Élargir la liste blanche.** C'est changer le mandat : la Partie 2 passe de « archiver
     une pollution connue par son motif » à « archiver ce que le score désigne et que le juge
     trouve périmé ». Le dernier garde-fou devient alors le jugement d'un LLM sur du texte, et le
     verdict alimente, 180 jours plus tard, le chemin **irréversible** de §6.6. **C'est
     exactement la purge automatique qui ne dit pas son nom** que §6.1 refuse. Si c'est cette
     option, elle se livre avec son propre killswitch et son propre soak, jamais en même temps que
     le producteur.
   - **(c) Un troisième verdict.** Le producteur ne nomme pas des candidats à l'archivage mais des
     candidats à la **relecture humaine** : une sortie de rapport, pas une mutation. Cela n'exige
     aucune modification de la liste blanche et laisse §6.6 intact.

   - **(d) Refuser le couplage, et réparer la portée de REORG séparément.** Option absente de la
     première écriture, ouverte par la mesure de §12.1 : la Partie 2 n'a jamais atteint ses
     propres cibles, indépendamment de tout producteur. Le défaut n'est pas la largeur de la
     liste, c'est la fenêtre de 500 lignes triée par `created_at`. On répare le scan, la liste ne
     bouge pas d'un caractère, et le producteur cesse d'être le sujet.

   **Recommandation de la revue : (a) pour l'étape 6 de §8, et (c) comme forme du rapport.**
   L'option (b) est une décision d'opérateur, séparée, et elle n'a pas été prise.

   **Décision d'opérateur du 2026-08-08 (§12.3) : (d), avec (c) comme forme du rapport.** Elle
   supersède la recommandation ci-dessus sans la contredire : (a) proposait de rendre le
   producteur inoffensif en le débranchant de l'action, (d) constate que le branchement n'aurait
   de toute façon rien pu produire — les deux populations ne se recoupent pas (§12.2). La
   différence pratique est que (a) laissait REORG cassé et (d) le répare. **(b) reste non prise**,
   et le devient encore moins : élargir ne réconcilierait pas deux ensembles disjoints par
   construction.

7. **Le producteur est lui-même un cliquet, et rien ne le fait tourner.** Les vingt plus bas ont
   tous `access_count=0` et `access_count_human=0` (mesuré) : leur multiplicateur ne fait que
   décroître, de façon monotone, et leur ordre relatif ne change jamais. Un candidat refusé
   revient donc **le lendemain, à la même place, indéfiniment**, et masque à jamais le rang 21.
   Le vingtième multiplicateur vaut 0,2279 et le centième 0,2664 — l'écart est si mince qu'aucun
   vieillissement raisonnable ne réordonne la tête de file. Le dépôt a déjà la réponse à ce
   problème exact : `roadmap_curate.rotate_keys` (`:474-487`), écrit le 2026-07-04 parce que
   « `ORDER BY` + `LIMIT` scannait les 10 premiers projets alphabétiques chaque nuit et jamais les
   16 autres ». **Le producteur doit porter le même décalage déterministe**, ou mémoriser les
   refus. Sans l'un des deux, `limit=20` est une fenêtre gelée sur 2 343 entités.

**Le retour manquant, dans l'autre sens.** Aujourd'hui le statut persisté ne pèse jamais sur le
score : un `archived` est simplement exclu de la recherche
(`brain_service.py:280,290-299`, `include_archived=False` par défaut). Ça suffit pour la
recherche. Ça ne suffit **pas** pour §6, et voici pourquoi c'est un bloquant :

**il n'existe aucune date d'archivage sur la ligne de l'entité.** `freshness_status` est une
colonne sans horodatage propre. `updated_at` bouge à chaque écriture de compteur par le flusher —
`trg_learnings_updated` est un `BEFORE UPDATE` **sans clause `WHEN`** (`pg_get_triggerdef`, mesuré).
Or le seul critère de suppression existant s'appuie dessus (`decay_tools.py:83-95`,
`updated_at < now() - 180j`). **La purge court donc sur une horloge que n'importe quelle lecture
machine remet à zéro.**

**Correction de la revue : « il n'existe AUCUNE horloge honnête » était trop fort.** Il en existe
une, imparfaite, et elle est déjà en production :

```
CREATE TRIGGER learnings_brain_entity_registry_trigger
  AFTER INSERT OR DELETE OR UPDATE OF topic, project_key, freshness_status, merged_into, metadata
  ON public.learnings FOR EACH ROW EXECUTE FUNCTION sync_brain_entity_registry()
```

La fonction écrit `brain_entities.updated_at = NOW()` (`prosrc`, lignes 82 et 230). **`access_count`
et `last_accessed_at` ne figurent pas dans la liste de colonnes** : le flusher ne déclenche pas ce
trigger. `brain_entities.updated_at` est donc une horloge **immunisée contre exactement la
contamination** que ce paragraphe dénonce. Vérifié sur les 98 learnings en attente de §6.2 : les 98
sont appariés dans le registre, `min(brain_entities.updated_at)` vaut **2026-03-13**, la même date
que `min(learnings.updated_at)`, et **0** dépasse 180 jours. Le registre porte aussi
`lifecycle ∈ {active, archived, deleted}` — 2 343 / 399 / 3 pour les learnings, et
399 = 359 archivés + 40 fusionnés-mais-frais.

Elle reste imparfaite, et c'est pourquoi la colonne dédiée reste tranchée : la Partie 1 de REORG
**normalise `tags` et `project_key`**, et `project_key` est dans la liste du trigger — un
rangement de métadonnées rajeunirait donc l'horloge d'archivage. Mais l'écart entre « aucune
horloge » et « une horloge à quatre écrivains parasites au lieu de tous » change le calendrier :
la §6.6 peut commencer à mesurer un séjour **aujourd'hui**, approximativement, au lieu d'attendre
plusieurs mois après la livraison de la colonne.

**Tranché : une colonne `freshness_status_updated_at` (et, tant qu'à faire, `freshness_source ∈
{merge, judgment, score, revive}`) sur les six tables suivies par le decay.** Quand `updated_at`
ne peut pas répondre, on date la chose elle-même, sans backfill, `NULL` = « jamais mesuré ».

**Le mécanisme est celui de la 041, pas celui de la 040, et il faut le dire.** La première
écriture parlait de « la doctrine 040/041 » sans choisir. Les deux migrations ont fait des choix
opposés pour une raison que CLAUDE.md nomme : la 040 écrit `focus_updated_at` **en code
applicatif** parce que le focus n'a **qu'un** écrivain ; la 041 écrit `content_updated_at` par
**trigger conditionnel `WHEN … IS DISTINCT FROM`** parce que le contenu en a beaucoup.
`freshness_status` en a **quatre** (§4.1), dont un — le jugement REORG — passe par le tool
générique `brain_update`, qui ne sait rien du decay. Stamper en code applicatif obligerait à le
faire dans `brain_update` lui-même, pour une colonne que 99 % de ses appels ne touchent pas.
**C'est donc la 041 qu'on copie** : un `BEFORE UPDATE OF freshness_status … WHEN (old.freshness_status
IS DISTINCT FROM new.freshness_status)`, du même gabarit que `trg_learnings_content_updated`, qui
existe déjà et qu'on peut lire. Aucun des quatre écrivains n'a alors à s'en souvenir — et c'est le
point, puisque l'un des quatre est un prompt.

C'est le point de jonction des deux notions, et c'est **le préalable dur de la purge**.

---

## 5. Le recalibrage de la formule, et d'où viennent les valeurs

### 5.1 Le signal contaminé — re-mesuré

```sql
SELECT count(*), sum(access_count), sum(access_count_human), max(access_count_human),
       count(*) FILTER (WHERE access_count>=10), count(*) FILTER (WHERE access_count_human>=10)
FROM learnings;
```

| table | n | Σ total | Σ humain | part humaine | max hum | saturés (total) | saturés (humain) |
|---|---|---|---|---|---|---|---|
| learnings | 2 742 | 19 049 | 79 | **0,41 %** | **5** | 508 (18,5 %) | **0** |
| decisions | 843 | 7 994 | 41 | 0,51 % | 3 | 182 | 0 |
| snippets | 100 | 388 | 6 | 1,55 % | 3 | 3 | 0 |
| runbooks | 93 | 965 | 13 | 1,35 % | 2 | 50 | 0 |
| adrs | 25 | 224 | 7 | 3,13 % | 3 | 19 | **1** |
| indexed_plans | 197 | 508 | 11 | 2,17 % | 3 | 25 | 0 |

(Colonne « saturés » = au-dessus du `freq_baseline` **du profil de ce type** : 10 pour
learning/decision, 20 snippet, 5 runbook/plan, 3 adr.)

Le brief donnait max humain = 4 pour les learnings ; la re-mesure d'aujourd'hui rend **5**. Le
compteur bouge pendant qu'on écrit — raison de plus pour ne pas recopier un chiffre.

`brain_service.py:336` passe `access_count = getattr(entity, "access_count", 0)` — le **total**.

### 5.2 Le trou que le brief ne nomme pas : le terme de récence pèse plus lourd et n'a pas de variante humaine

Substituer le compteur ne corrige que `w_freq`. Or `access_count` et `last_accessed_at` sont
**deux** entrées, de poids **0,2** et **0,3** :

| terme | poids (learning) | source | corrigeable par la 041 ? |
|---|---|---|---|
| `age_factor` | 0,3 | `created_at` | neutre |
| `access_factor` | **0,3** | **`last_accessed_at`** | **non — la colonne humaine n'existe pas** |
| `freq_factor` | 0,2 | `access_count` | oui — `access_count_human` |
| `validation_factor` | 0,2 | `validated_at` / `decided_at` | neutre |

`\d learnings` le confirme : il y a `access_count_human`, il n'y a **pas** de
`last_accessed_at_human`. Et la contamination est massive :

```sql
SELECT count(*) FILTER (WHERE last_accessed_at IS NOT NULL AND access_count_human=0),
       count(*) FILTER (WHERE last_accessed_at IS NOT NULL AND access_count_human>0),
       count(*) FILTER (WHERE last_accessed_at IS NULL) FROM learnings;
→ 1 779   |   51   |   912
```

**1 779 learnings ont leur terme de récence — le plus lourd avec l'âge — piloté par des lectures
machine seules.** Corriger `freq` seul répare 0,2 des 0,5 de poids pilotés par la lecture. Le
reste continue de faire vivre ce que la machine relit.

**Bonne nouvelle mesurée : l'agrégat sait déjà tout ce qu'il faut.**
`repositories/pg_access_log.py:63-94` groupe déjà par `access_log.actor`, teste
`is_human_actor(row["actor"])` pour remplir `count_human`, et calcule `max_accessed` — mais il
**replie** ce max sur tous les acteurs. Un `max_accessed_human` est une ligne dans une boucle qui
existe. **Ce qui manque, c'est la colonne où le ranger**, sur les six mêmes tables que la 041.

### 5.3 Les valeurs proposées, et leur origine

**Principe, à écrire dans le code** : une `freq_baseline` se dérive de **la distribution du
compteur qu'on branche**, jamais du ratio entre les deux compteurs. Le ratio (0,41 %) donnerait
`10 / 240` — un non-sens.

Distribution mesurée du compteur humain sur tout le corpus (§3.3) : `0→3 702, 1→72, 2→18, 3→7,
4→3, 5→1`. Le maximum global est **5**, et **11 artefacts sur 3 803** atteignent 3.

| profil | `freq_baseline` actuel | proposé | origine de la valeur |
|---|---|---|---|
| learning | 10 | **3** | max humain mesuré 5 ; `≥3` = 11 artefacts corpus-entier (0,29 %). Une baseline à 3 fait travailler le terme sur toute son amplitude sans saturer au premier accès |
| decision | 10 | **3** | max humain mesuré 3 |
| snippet | 20 | **2** | max humain 3, six lectures humaines sur 100 snippets |
| runbook | 5 | **2** | max humain 2 — à 3 le terme ne pourrait jamais être plein |
| plan | 5 | **2** | max humain 3 |
| adr | 3 | **3 (inchangé)** | c'est le seul type dont le compteur humain sature déjà (1 ADR) : la valeur est déjà calibrée pour ce compteur |

Ce ne sont pas des constantes physiques. Elles se re-mesurent — et le dépôt a l'endroit pour le
dire : `src/brain_v42/thresholds.py` porte déjà `calibrated=False` / `last_calibrated=None` /
`corpus_dependency` sur des seuils du même genre (`consolidation_similarity`, ligne 105-118).
Ces six baselines y entrent, avec `corpus_dependency="distribution de access_count_human"` et la
date de mesure.

**Ce que la revue a mesuré, et qui recadre toute cette sous-section : sur le corpus
d'aujourd'hui, changer les baselines ne fait presque rien.** Le vrai `DecayCalculator` exécuté sur
les 2 343 learnings, compteur humain, `freq_baseline` 10 puis 3 :

| | baseline 10 | baseline **3** |
|---|---|---|
| multiplicateur **modifié** | — | **51 learnings sur 2 343 (2,18 %)** |
| multiplicateur inchangé | — | **2 292** |
| moyenne | 0,4493 | 0,4507 |
| minimum | 0,2228 | **0,2228** (identique) |
| p10 | 0,2905 | **0,2905** (identique) |
| sous 0,5 → `stale` | 1 453 | **1 453** (identique) |
| sous 0,25 · sous 0,30 | 49 · 263 | **49 · 263** (identiques) |

La raison est arithmétique et tient en une ligne : `frequency_factor = min(ac / baseline, 1)`, et
**3 702 artefacts sur 3 804 ont `access_count_human = 0`**. Zéro divisé par n'importe quelle
baseline vaut zéro. La baseline ne touche que les **51 learnings** qui ont au moins une lecture
humaine — les mêmes 51 que §5.2 — et elle les pousse vers le **haut** (delta maximal +0,1400).

Deux conséquences à écrire noir sur blanc :

- **Tout l'effet mesuré de §5.4 (0,5130 → 0,4493, +359 `stale`) vient de la SUBSTITUTION du
  compteur, pas des baselines.** Ce sont deux changements de nature différente empaquetés dans une
  même sous-section, et un seul des deux a un effet mesurable aujourd'hui.
- **Les baselines ne changent rien au classement des candidats** (§4.3) : le bas de classement est
  intégralement à `access_count_human = 0`, donc à `freq_factor = 0`, donc invariant. Elles sont un
  changement **protecteur** — elles font remonter les 51 artefacts qu'un humain a ouverts — et il
  faut les vendre comme ça, pas comme un recalibrage du signal d'archivage.

Elles restent justifiées : quand le compteur chauffera, une baseline de 10 sur un maximum observé
de 5 rendrait le terme structurellement incapable d'être plein. Mais **elles ne sont pas urgentes,
et elles ne débloquent rien**. Si l'étape 6 de §8 doit être découpée pour réduire son risque, c'est
ici que la ligne de coupe passe.

### 5.4 Ce que le recalibrage ne fait PAS : créer des candidats à l'archivage

Mesure centrale de cette spec. Multiplicateur **au repos** (avec le vrai `last_accessed_at`, pas
celui d'un accès en cours), sur les 2 343 learnings ni archivés ni fusionnés. Publiée d'abord
depuis une réplique SQL, **re-calculée depuis par le vrai `DecayCalculator` importé du dépôt**
(§0) : les deux concordent, seul le p10 humain diffère de 0,0001. Les valeurs ci-dessous sont
celles du code réel.

| | compteur **total** | compteur **humain** |
|---|---|---|
| moyenne | **0,5130** | **0,4493** |
| minimum | **0,2228** | 0,2228 |
| p10 | 0,2998 | 0,2905 |
| sous 0,5 → `stale` | **1 094** | **1 453** |
| sous **0,2** → `archived` | **0** | **0** |

**Le minimum du corpus (0,2228) est au-dessus du seuil d'archivage (0,2).** Substituer le
compteur fait basculer 359 learnings de plus en `stale` et **n'en archive toujours aucun**.

Arithmétique de la raison, sur les constantes de `decay.py` : une entité non validée reçoit
`w_valid · 0,7 = 0,14` **gratuitement**, et le corpus le plus vieux date du 2026-01-05 (215 j),
soit `age_factor = 2^(-215/90) ≈ 0,19` → `0,3 · 0,19 ≈ 0,057`. Le plancher réel du corpus est
donc autour de 0,22. Le seuil à 0,2 a été choisi pour un corpus qui n'existe pas encore.

**Tranché : le producteur de candidats classe, il ne seuille pas** (§4.3, propriété 3). Deux
raisons : la population d'un seuil n'est pas prévisible (à 0,25 → 49 learnings, à 0,30 → 236 —
mesuré), alors que la population d'un rang est exactement le cap de REORG. Et un rang ne se
périme pas quand le corpus vieillit.

`stale_threshold` et `archive_threshold` **restent** — pour l'étiquette `_freshness` de
`brain_service.py:347` et pour `format_decay_status`. Ils cessent d'être une porte.

Et l'ordre produit est déjà bon, avant même la chauffe du compteur. Bas de classement mesuré
(vrai `DecayCalculator`, compteur humain) :

```
0,2228  datalake-v2     Neo4j Persistence Fix                       ac=0 ach=0  2026-01-05
0,2228  datalake-v2     Service Dependency Injection Pattern        ac=0 ach=0  2026-01-05
0,2228  second-cerveau  Second Cerveau cleanup                      ac=0 ach=0  2026-01-05
0,2228  second-cerveau  Grafana metrics fix                         ac=0 ach=0  2026-01-05
0,2233  poc-lyriks-v2   neo4j-driver v5 breaking changes            ac=0 ach=0  2026-01-06
0,2241  lyriks          Lyriks Design Conventions - Grid System      ac=0 ach=0  2026-01-07
…
20ᵉ = 0,2279 · 100ᵉ = 0,2664 · 500ᵉ = 0,3132
```

Les quatre projets du bas de classement sont ceux dont le dernier artefact date de 149 à 215
jours (§6.2). **Le score sait déjà quoi proposer ; il n'a personne à qui le dire.** C'est la
phrase qui résumait la §4 — et la revue l'a corrigée sur deux points, tous deux en §4.3 :
il **a** quelqu'un à qui le dire (le lien score → statut existe déjà via le flusher), et le juge
à qui on veut le dire n'a **pas le droit** d'archiver ce qu'on lui propose (propriété 6 : zéro
des 500 plus bas ne matche la liste blanche de REORG). Le classement est bon ; le destinataire
reste à choisir.

### 5.5 Le seul changement de cette spec qui touche un chemin interactif

Modifier les `DecayProfile` change `effective_score` (`brain_service.py:344`) pour **toutes** les
recherches, immédiatement, y compris celles d'un humain en session. Il n'y a **pas** de dry-run
pour la recherche, et `decay_floor=0.3` (`config.py:192`) amortit sans annuler.

**Tranché** : la substitution du compteur et les nouvelles baselines arrivent **derrière un
réglage**, avec les valeurs d'aujourd'hui par défaut, et une mesure avant/après sur un jeu de
requêtes fixe. Ce n'est pas un killswitch d'irréversibilité (rien n'est écrit), c'est un
killswitch de **régression de pertinence** — la seule chose de ce lot qu'un humain sentirait le
jour même.

---

## 6. La purge — section dédiée, parce qu'elle est irréversible

### 6.1 Posture, avant tout chiffre

Même posture que `sweep` (v1 §8), sans négociation :

- livrée **killswitch fermé** : `BRAIN_DREAM_PURGE_ENABLED=false`, `BRAIN_DREAM_PURGE_DRY_RUN=true`,
  **absents du drop-in** ;
- **dry-run d'abord**, avec un manifeste complet de ce qui serait supprimé ;
- **la mesure de ce qu'elle toucherait AVANT d'armer**, publiée, pas déduite ;
- **jamais armée dans le même lot** qu'un changement de topologie, ni que l'ouverture de `sweep`.

### 6.2 Ce qu'elle toucherait aujourd'hui : zéro

Le dépôt a **déjà** un critère de suppression, et personne ne l'a remarqué.
`mcp/tools/decay_tools.py:83-95`, dans `brain_decay_status()` — donc **déjà affiché à l'agent
SCAN toutes les nuits** :

```python
# Deletion candidates: archived 180+ days with access_count=0
table.c.freshness_status == "archived", table.c.access_count == 0, table.c.updated_at < cutoff
```

Rejoué tel quel :

```sql
SELECT count(*) FILTER (WHERE freshness_status='archived' AND access_count=0
                          AND updated_at < now()-interval '180 days') FROM learnings; -- et les 4 autres
```

| table | candidats **aujourd'hui** | archivés à `access_count=0` en attente |
|---|---|---|
| learnings | **0** | 98 |
| decisions | **0** | 2 |
| snippets | **0** | 0 |
| runbooks | **0** | 1 |
| adrs | **0** | 0 |

**Zéro, partout.** Et la date où ça change est calculable :

```sql
SELECT min(updated_at)::date, (min(updated_at)+interval '180 days')::date, count(*)
FROM learnings WHERE freshness_status='archived' AND access_count=0;
→ 2026-03-13 | 2026-09-09 | 98
```

**Premier candidat : le 2026-09-09**, dans 32 jours, puis 98 learnings qui suivent. Une purge
armée aujourd'hui ne ferait rien ; armée sans surveillance, elle mordrait le 9 septembre.

**Ce que ces 98 learnings sont vraiment — la mesure que la première écriture n'a pas faite, et qui
retourne le paragraphe :**

```sql
SELECT count(*), count(*) FILTER (WHERE merged_into IS NOT NULL)
FROM learnings WHERE freshness_status='archived' AND access_count=0;
→ 98 | 97
```

**97 des 98 sont des pierres tombales de fusion.** Le critère existant, laissé tel quel, ne
supprimerait donc pas « 98 connaissances périmées » : il supprimerait **l'historique de 97 fusions
faites par CLEAN**, plus une seule entité réelle —

```sql
SELECT project_key, created_at::date, left(topic,40) FROM learnings
WHERE freshness_status='archived' AND access_count=0 AND merged_into IS NULL;
→ brain-v42 | 2026-07-26 | Dream night failure: 2026-07-26
```

C'est exactement le danger que §6.5 nomme (« toute règle doit exclure `merged_into IS NOT NULL` »)
appliqué au seul rayon d'explosion daté de cette spec. **Avec l'exclusion des tombes, le
2026-09-09 ne concerne plus 98 entités mais 1.** Deux ordres de grandeur. Le paragraphe
« armée sans surveillance, elle mordrait le 9 septembre » reste vrai, mais ce qu'elle mordrait
n'est pas ce qu'on croyait : la trace du travail de consolidation, pas son résultat périmé.

**Ce critère existant est faux sur ses deux termes**, et §4.3/§5.2 expliquent pourquoi :

- `access_count = 0` — le compteur **total**. Un artefact relu par le seul dream sort du critère
  et devient indéfiniment non-purgeable. C'est le mécanisme de §5.1 appliqué à la suppression.
- `updated_at < cutoff` — l'horloge de 180 jours **redémarre à chaque écriture de compteur du
  flusher** (`trg_learnings_updated` est présent). Sans `freshness_status_updated_at` (§4.3), il
  n'existe **aucune** horloge honnête. **La purge est bloquée sur cette colonne**, ce n'est pas
  une préférence d'ordonnancement.

### 6.3 Le risque nommé par l'opérateur, re-mesuré

> « Sur 46 projets que le dream n'a jamais lus, un premier passage WET effacerait ce qu'on n'a
> jamais regardé. »

Re-mesuré, c'est **pire que 46** : le dream n'a jamais synthétisé ailleurs que sur `brain-v42`
(§2.1, `dream:generated` → 87 learnings, un seul projet). Ce sont **54 projets sur 55**, et
**3 145 artefacts sur 3 803 (82,7 %)** qui n'ont jamais vu une phase de consolidation.

Et pour la définition la plus probable d'« inactif » — aucun artefact créé depuis 90 jours :

```sql
WITH a AS (… les cinq tables …), last AS (SELECT project_key, max(created_at) lc FROM a GROUP BY 1)
SELECT count(DISTINCT a.project_key), count(*), count(*) FILTER (WHERE a.access_count_human>0),
       count(*) FILTER (WHERE a.access_count>0)
FROM a JOIN last l USING (project_key) WHERE l.lc < now() - interval '90 days';
```

| | valeur |
|---|---|
| **clés d'artefact** sans artefact neuf depuis 90 j | **31** (le domaine ici est celui des clés portant des artefacts, 54 valeurs non nulles, pas les 55 `project_contexts`) |
| artefacts concernés | **567** |
| dont lus au moins une fois par un humain | **3** |
| dont lus au moins une fois par quiconque | 268 |

> **Ce rayon d'explosion bouge à vue d'œil.** Rejoué vingt minutes plus tard, le même SQL rend
> **32 clés / 580 artefacts / 3 humains / 271 quiconque** : la clé `perso` (13 artefacts créés du
> 2026-02-17 au 2026-03-20) est entrée dans l'ensemble entre les deux mesures. Un chiffre qui varie
> de 2,3 % en vingt minutes n'est pas une base pour armer une suppression irréversible — il est une
> raison de plus de faire produire ce manifeste **par la purge elle-même, en dry-run, à l'instant
> où elle tournerait**, jamais par un document.

**Un passage WET sur « projet inactif 90 j » supprimerait 567 artefacts dont 3 ont été regardés
par un humain.** Ce n'est pas un argument pour purger : c'est l'argument inverse. Ces 567
artefacts n'ont jamais été consolidés, donc jamais indexés par CONNECT, donc jamais remontés par
la recherche — **ils n'ont jamais eu leur chance d'être lus.** Les supprimer, c'est conclure d'un
silence qu'on a soi-même produit.

Contre-exemple mesuré, à garder sous les yeux : `red-orchestrator`, dernier artefact il y a
**120 jours**, porte quand même **2 lectures humaines** en deux jours de compteur (§3.1).
L'inactivité d'écriture ne dit rien de l'inactivité de lecture.

### 6.4 Tranché : le curseur est le ticket d'entrée de la purge

**Un projet n'est purgeable que si le dream l'a consolidé.** Formellement :
`dream_project_cursor.last_success_date IS NOT NULL`, et depuis assez de nuits pour que la
consolidation ait pu produire quelque chose.

C'est ce qui relie D4 et D5 : « on ne purge que ce qu'on a regardé, et le curseur est la preuve
qu'on l'a regardé ». Aujourd'hui, cette règle rend **54 projets sur 55 non purgeables**, et c'est
la bonne réponse. Elle se relâche toute seule, à mesure que le curseur tourne — sans qu'on ait
rien à ré-armer.

### 6.5 Deux purges, deux rayons d'explosion — à ne jamais confondre

**Purge d'entité.** La primitive existe déjà : `mcp/tools/crud_tools.py:280`,
`brain_delete(entity_type, entity_id)`. Aucun outil destructif nouveau n'est requis. Trois
couplages mesurés :

```sql
SELECT tc.table_name, kcu.column_name, ccu.table_name, rc.delete_rule …
WHERE ccu.table_name IN ('learnings','decisions','snippets','runbooks','adrs','project_contexts');
```

- `dream_promotions` porte **quatre** FK `SET NULL` vers les tables de connaissance, pas une :
  `source_learning_id → learnings`, `target_runbook_id → runbooks`, `target_adr_id → adrs`, plus
  `dream_run_id → dream_runs` (105 lignes dans `dream_promotions`, mesuré). Supprimer un learning
  **vide silencieusement l'audit de promotion** — et supprimer un runbook ou un ADR **cible** le
  vide aussi, par l'autre bout. Le même risque exact que la v1 §12 refusait pour `dream_run_id`.
- `merged_into` est en `SET NULL` sur les cinq tables : supprimer une cible de fusion **délie ses
  tombes**, qui redeviennent des entités archivées sans explication.
- **Les tombes sont la première cible d'un critère naïf.** 348 des 359 learnings archivés sont
  des pierres tombales de fusion (§4.2). Une purge sur `freshness_status='archived'` détruit
  d'abord **l'historique des fusions**, c'est-à-dire la trace de tout ce que CLEAN a consolidé.
  Toute règle doit exclure `merged_into IS NOT NULL`, ou dire pourquoi elle ne le fait pas.
- **Le graphe** : `services/graph_helpers.py:210 graph_delete_entity` existe ; une purge qui
  l'oublie laisse des nœuds Neo4j orphelins. Non mesuré ici (§10).

**Purge de projet.** Rayon différent, et **deux** gardes structurelles déjà présentes — la
première écriture n'en nommait qu'une. Inventaire complet des FK entrantes, mesuré :

```sql
SELECT tc.table_name, kcu.column_name, ccu.table_name, rc.delete_rule … 
WHERE ccu.table_name IN ('projects','project_contexts');
→ brain_sessions.project_key  → project_contexts : RESTRICT
→ brain_entities.project_key  → projects         : RESTRICT
→ project_aliases.project_key → projects         : CASCADE
```

- `brain_sessions → project_contexts` en **`RESTRICT`**, avec **363 sessions sur 17 projets**.
  **Dix-sept `project_contexts` sur 55 ne peuvent pas être supprimés du tout**, et PostgreSQL le
  refusera fail-closed sans qu'on ait rien à écrire.
- **`brain_entities → projects` en `RESTRICT`, qui n'a rien à voir et bloque tout.** `projects` et
  `project_contexts` sont deux tables distinctes. Mesuré : **74 lignes dans `projects`**, et
  **74 clés distinctes référencées par `brain_entities`** — chaque projet porte au minimum son
  propre nœud de registre (`entity_type='project'`, 74 lignes), qui référence sa propre clé. **Les
  74 lignes de `projects` sont donc toutes indélébiles en l'état**, et 59 d'entre elles portent en
  plus des entités de connaissance. Une « purge de projet » doit dire laquelle des deux tables elle
  vise ; si c'est `projects`, elle est structurellement impossible sans démonter le registre —
  lequel est lui-même protégé par `entity_relations` en `RESTRICT`.

Les 9 contextes vides (§2.1) sont les seuls dont la suppression ne détruit rien d'autre
qu'eux-mêmes — s'ils n'ont pas de session, et à condition de ne viser que `project_contexts`.

### 6.6 L'escalier, et où il est coupé aujourd'hui

```
proposer → archiver (réversible) → séjour minimal en archive → purger (irréversible)
   ↑ §4.3           ↑ REORG, cap 20            ↑ BLOQUÉ                ↑ fermé, dry
```

Le troisième barreau **n'existe pas sur la ligne de l'entité** : il n'y a pas de date d'archivage
dédiée (§4.3), donc pas de séjour mesurable de façon fiable, donc pas de purge défendable. **Le lot
« purge » ne peut pas démarrer avant que `freshness_status_updated_at` ait été livrée et ait
accumulé du temps réel.** Il y a là une attente incompressible de plusieurs mois entre la colonne et
le premier WET — à dire à l'opérateur maintenant, pas au moment d'armer.

**Nuance ajoutée par la revue** : `brain_entities.updated_at` (§4.3) donne dès aujourd'hui une
approximation du séjour, immunisée contre le flusher mais pas contre la normalisation de métadonnées
de REORG Partie 1. Ça ne raccourcit pas le soak — un chiffre approximatif ne justifie pas une
suppression — mais ça permet de **surveiller le barreau pendant qu'il se construit**, au lieu
d'être aveugle jusqu'à la première mesure de la colonne dédiée.

Et le manifeste de dry-run ne doit pas finir dans `logs/dream/` : la v1 §16 y a mesuré
**1 865 fichiers pour 121 Mo sans aucune rotation**. Un manifeste de suppression est une pièce
d'audit ; il va en base ou dans un chemin dédié.

---

## 7. La fenêtre 05:00-09:00 : timer, plafond, et le piège du template régénéré

### 7.1 L'état mesuré

| ce qui est en vigueur | valeur | où elle vit |
|---|---|---|
| déclenchement | `OnCalendar=*-*-* 06:00:00` | `~/.config/systemd/user/brain-v42-dream.timer:7` **et** `deploy/systemd/brain-v42-dream.timer:7` |
| dispersion | `RandomizedDelaySec=120` | idem `:13` |
| rattrapage | `Persistent=true` | idem `:10` |
| plafond dur | `TimeoutStartSec=10800` (**3 h → tue à 09:00**) | `~/.config/systemd/user/brain-v42-dream.service:38` **et** `deploy/systemd/brain-v42-dream.service.tmpl:41` |
| cible d'exécution | `dream.sh brain-v42` | `…service:28`, `…tmpl:31` |

`systemctl --user show` n'était pas disponible dans cet environnement (pas de bus DBus) ; les
valeurs ci-dessus sont lues **dans les fichiers vivants** sous `~/.config/systemd/user/`, ce qui
est la même source mais sans les surcharges de drop-in. Aucun drop-in ne touche `TimeoutStartSec`
(`grep -rn TimeoutStartSec` sur les trois `.conf` → rien).

**Correction de comptage.** La première écriture publiait « `killswitches.conf` 8 » depuis
`grep -c 'Environment='`. Ce motif n'est pas ancré et compte la ligne 3 du fichier, qui est un
**commentaire** (`# Environment= line there (incident 2026-06-30 …)`). Le compte réel de directives
actives est **7** (`grep -c '^Environment='`), et c'est aussi ce que rendrait le garde-fou de
§7.2, dont l'awk saute les lignes commençant par `#` ou `;`. `nvidia.conf` 0, `token.conf` 0.

**Passer à 05:00-09:00 demande de bouger les deux**, et ce sont deux fichiers différents.

### 7.2 Le piège, vérifié en lisant le garde-fou

`deploy/systemd/install.sh` régénère l'unité depuis le template. Son garde-fou est
`warn_wiped_env` / `count_environment_directives` (`install.sh:277-345`), lu intégralement : il
compte les lignes **`Environment=`** et rien d'autre.

**Conséquence : un `TimeoutStartSec` relevé à la main dans
`~/.config/systemd/user/brain-v42-dream.service` est réécrit à 10800 par la prochaine
réinstallation, sans un mot.** La nuit suivante serait tuée à 08:00 (3 h après 05:00), au milieu
de la fenêtre, en laissant les projets non servis **sans aucune ligne dans `dream_runs`** — un
lecteur `DISTINCT ON (phase)` ne verrait rien d'anormal. C'est le jumeau exact de l'incident du
2026-06-30 cité en tête de `killswitches.conf` (PROMOTE+REORG éteints deux nuits par une
régénération).

Le timer subit la même mécanique : `brain-v42-dream.timer` figure dans la liste des unités
copiées par `install.sh` (`:44`).

**Tranché : les deux valeurs bougent dans `deploy/systemd/`, jamais dans le vivant.** Et le test
d'intégration `tests/integration/test_dream_systemd_install.sh` (v1 §13 : `:180` épingle
`ExecStart=… dream.sh brain-v42`, `:284,286` le pré-vol) est mis à jour **dans le même commit**.

### 7.3 Le budget de la fenêtre, et qui l'applique

240 minutes. Répartition proposée, chiffres de §2.1 :

| poste | budget | origine |
|---|---|---|
| phases globales (extract 10 + roadmap 20 + sweep 5) | **35 min réservées** | plafond **configuré** des trois `timeout` (v1 §4.3, `dream.sh:664,706,735`) — c'est une borne dure, pas une moyenne |
| boucle projets | **205 min** | le reste |
| budget d'admission par projet | **15 min** | p90 mesuré du sous-total agent d'une nuit : **14,73 min**, arrondi au-dessus |

**Projets servis par nuit** : `205 / 14,73 = 13,9` au p90 ; `205 / 9,28 = 22,1` à la moyenne ;
`205 / 30,50 = 6,7` au pire sous-total agent mesuré. Couverture de 55 projets : **2,5 nuits à la
moyenne, 4,0 nuits au p90, 8,2 nuits au pire cas mesuré**.

> **Ces trois chiffres ignorent le plafond des jamais-servis de §2.5**, et le plafond mord
> précisément à la mise en service, quand les 55 projets sont neufs. Simulé sur les 55 projets
> réels : à fenêtre 14 avec un plafond fixe à la moitié (7), la couverture complète tombe à la
> **nuit 8**, pas 4, et la première nuit n'admet que 7 projets pour une fenêtre de 14. §2.5 a été
> corrigée en conséquence : le plafond ne s'applique que s'il existe des projets déjà servis pour
> remplir le reste. Avec cette correction, et seulement avec elle, les chiffres ci-dessus tiennent.

L'opérateur avait calculé ~25 unités-projet (D4) en partant de la moyenne 9,29 et sans réserver
les globales. **L'arithmétique est juste ; elle décrit la moyenne.** La spec planifie sur le p90,
soit ~14, parce qu'un plan de couverture qui tient une nuit sur deux n'est pas un plan. Les deux
chiffres se valent, ils ne répondent pas à la même question.

**Tranché : la fenêtre est appliquée par la boucle, pas par systemd.** La boucle porte une
échéance et n'admet un projet que si `restant ≥ budget d'admission`. `TimeoutStartSec=14400`
(09:00 pile depuis 05:00) devient le **filet**, plus le mécanisme. Raison mesurée : v1 §4.3 —
systemd tue **au milieu d'un projet**, et les projets suivants n'écrivent aucune ligne ; l'échec
est alors invisible. Une boucle qui décide elle-même de ne pas admettre un projet **le laisse en
tête du curseur**, ce qui est un état correct et lisible.

**L'échéance est relative au démarrage du processus, PAS une heure murale.** La première écriture
proposait « une échéance absolue (par exemple 08:55) ». La revue a rapproché cette phrase de la
ligne « rattrapage » du tableau de §7.1, deux paragraphes plus haut :

```
~/.config/systemd/user/brain-v42-dream.timer:10   Persistent=true
```

`Persistent=true` fait rejouer une occurrence manquée **au démarrage suivant, à n'importe quelle
heure**. Un PC rallumé à 13:00 déclenche le dream de 05:00 à 13:00. Une échéance murale à 08:55
rend alors `restant` **négatif** : la boucle n'admet **aucun** projet, n'avance **aucun** curseur,
et sort verte sans une ligne dans `dream_runs` — le jumeau exact du mode d'échec silencieux que ce
même paragraphe dit vouloir éviter. Répété, c'est une couverture nulle qui ne se signale jamais.

**Tranché** : `échéance = début_du_processus + 235 min`, bornée par `min(échéance, 08:55)` **et**
par un plancher qui garantit au moins un projet. Un rattrapage tardif sert alors une fenêtre pleine
hors de la plage nominale — ce qui est le comportement voulu de `Persistent=true` — au lieu de ne
rien servir. Si l'opérateur préfère qu'un rattrapage tardif ne tourne pas du tout, la façon de
l'écrire est `Persistent=false`, pas une échéance murale qui échoue en silence.

**Le retry entre dans le même budget.** v1 §10 : le retry vaut +43 min par projet
(`dream.sh:539`, échec dur seulement, jamais sur timeout, jamais sur `promote`). À 14 projets
c'est +602 min de plafond, soit deux fois et demie la fenêtre. La recommandation v1 tient et se
durcit : **allocation de retries pour la nuit entière**, décomptée du temps restant comme
n'importe quelle phase.

### 7.4 Ce que la fenêtre neuve ne heurte pas

Voisinage des timers, lu dans `deploy/systemd/` :

| unité | déclenchement | dispersion | plafond | fin au pire |
|---|---|---|---|---|
| `brain-v42-embedding-backfill` | `*-*-* 04:30:00` | 120 s | `TimeoutStartSec=900` | ~04:47 |
| `brain-v42-graph-recon` | `Sun *-*-* 04:00:00` | 300 s | `TimeoutStartSec=1800` | ~04:35 |

**Aucun des deux ne déborde sur 05:00**, y compris le dimanche où les deux tournent.
`RandomizedDelaySec=120` du dream reste utile et coûte 2 min sur 240 — bruit.

---

## 8. Ordre de livraison

Le principe de la v1 §12 est repris et il est le seul garde-fou qui compte : **chaque étape est
livrable sans qu'une seule nuit change de comportement**, jusqu'à celle qui le change exprès.

| # | ce qui atterrit | régime après merge |
|---|---|---|
| 1 | ~~Les **quatre lignes de prompt en dur** + le contrôle de `project_key` manquant dans `promote_validate`~~ (v1 §3.5, §12 étape 1) — **LIVRÉ**, `ca13c1e9` + `89b5926d`, branche `fix/dream-lot1-prompt-scope` (§13) | inchangé (un seul projet), **prouvé** et non promis |
| **R** | **La portée de REORG Partie 2** : la liste blanche sort de la prose et entre en code (fait, inerte, `b917c0b7`), le filtre se câble dans `brain_list`, la Partie 2 interroge au lieu de paginer (§12.3, §12.4) | **change exprès** : REORG archive ses 7 cibles la nuit suivante, réversible |
| 2 | **042** appliquée en production, `alembic current` **prouvé**, puis les 5 écrivains et les 10 lecteurs de `dream_runs` | inchangé, colonne remplie |
| 3 | **Sortie structurelle des trois phases globales**, sentinelle `'*'`, ancre de test textuelle | inchangé (elles tournaient déjà une fois) |
| 4 | Chemins de journal projetés (7/12), réinit des exports, compteurs `projet/phase`, alerte agrégée groupée | inchangé à un projet |
| 5 | **`freshness_status_updated_at` + `freshness_source`** sur les six tables, écrites par les quatre écrivains | inchangé ; l'horloge commence à tourner |
| 6a | **Le producteur de candidats** (lecture seule), **avec son décalage déterministe** (§4.3 propriété 7), rendu **en rapport de relecture humaine** — découplé de REORG, plus conditionné à rien (§4.3 propriété 6, option **d** + forme **c**) | un rapport de plus, aucune mutation |
| 6b | **La substitution du compteur** + `last_accessed_at_human`, **derrière un réglage** (§5.5) | seul changement à effet immédiat sur un humain |
| 6c | Les six `freq_baseline` + la garde REORG passée au compteur humain | **inerte aujourd'hui** (§5.3 : 51 entités sur 2 343) ; à livrer quand le compteur aura chauffé |
| 7 | **Le curseur** + la fenêtre 05:00-09:00 + l'admission par temps restant, **largeur de fenêtre = 1 projet** | identique à aujourd'hui, à l'octet près |
| 8 | **Le scope serveur** : les cinq `DreamProjectToolPolicy()` vides remplies, `BRAIN_DREAM_CAPABILITY_ENFORCEMENT` armé | changement de régime **par projet**, mesurable |
| 9 | **Élargissement de la fenêtre**, quelques unités à la fois, en mesurant chaque nuit | couverture progressive, réversible en réduisant un nombre |
| 10 | **La purge**, fermée et dry, après plusieurs mois de séjour d'archive mesuré par l'étape 5 | rien ne se supprime |

Quatre raisons d'ordre qui ne sont pas des préférences :

- **5 avant 6, et très avant 10.** L'horodatage du statut est le seul moyen de mesurer un séjour ;
  livré tard, il repousse la purge d'autant. C'est l'étape la plus petite et la plus bloquante.
- **6 avant 7.** La §4/§5 travaille **par entité** et produit un effet dès la première nuit sur
  `brain-v42` seul. Elle ne dépend ni du curseur, ni du scope, ni de la fenêtre. La livrer
  d'abord, c'est encaisser la valeur avant la partie chère.
- **6 est découpée en trois, et l'ordre interne compte.** La revue a mesuré que 6c est inerte
  aujourd'hui (§5.3) et que 6a ne changeait rien tant que le mandat de la Partie 2 de REORG
  n'était pas tranché (§4.3, propriété 6). Les empaqueter ensemble, c'était livrer un effet
  immédiat sur la recherche (6b) sous le même réglage qu'un no-op (6c) et qu'une question ouverte
  (6a). **Le découpage tient, mais pour une raison de moins** : depuis §12.3, 6a n'est plus une
  question ouverte — c'est un rapport découplé. Reste que 6b est le seul des trois qu'un humain
  sentirait le jour même, et c'est ça qui justifie qu'il ait son propre réglage.
- **R n'a pas de place dans l'ordre, et c'est un fait, pas une commodité.** Elle ne dépend ni de
  la 042, ni de l'horloge d'archivage, ni du curseur, ni du scope : elle répare un scan qui était
  déjà cassé à un seul projet. Elle porte une lettre plutôt qu'un numéro parce que renuméroter
  2-10 casserait les renvois « étape 5 », « étape 6 », « étape 8 » disséminés dans tout le
  document — un risque de documentation sans contrepartie.
- **8 avant 9.** §1.3 : ouvrir la fenêtre avant le scope, c'est refaire 25 fois par nuit le même
  travail global. La boucle se livre (7), elle ne s'ouvre pas (9).

**Cette décision d'opérateur est prise** (§12.3) : option **(d)**, forme **(c)**. Le lot 6a est
donc spécifiable, et il est retombé au rang de rapport ordinaire — c'est le lot **R** qui porte
désormais l'effet sur REORG. La première écriture disait « le lot 6a n'est spécifiable qu'après » ;
c'était vrai, et ce n'est plus une attente.

**Le seul arbitrage encore ouvert sur cette table est l'armement de R.** Le code est vert et
inerte ; l'armer est deux gestes (câbler `brain_list`, réécrire la Partie 2) et le premier merge
sur `main` fait archiver sept entités la nuit suivante. C'est réversible — `freshness_status` se
remet à `fresh` — mais c'est un changement de comportement réel, et §12.4 rappelle que la racine
est un working tree déployé.

**Contrainte hors dépôt, reprise de la v1 §12** : `dream.sh` tourne depuis le working tree de la
**racine**, donc merger sur `main` c'est déployer. La 042 s'applique en production **avant** le
merge qui introduit ses lecteurs. Et le numéro de révision se **mesure** à chaque fois.

---

## 9. Ce qui se livre killswitch fermé

- **`BRAIN_DREAM_PURGE_ENABLED=false`, `BRAIN_DREAM_PURGE_DRY_RUN=true`**, absentes du drop-in,
  défauts dans `dream.sh` sur l'idiome `BRAIN_DREAM_SWEEP_*` (`:58-59`). Rien n'est supprimé
  avant une décision explicite adossée à un manifeste mesuré.
- **La largeur de fenêtre par défaut vaut 1.** Le curseur tourne, sert un projet, et la nuit est
  identique à aujourd'hui. C'est la propriété qui rend l'étape 7 sûre.
- **`sweep` ne bouge pas** : `ENABLED=false`, `DRY_RUN=true`, absente du drop-in. Re-vérifié : elle
  n'a toujours **aucune ligne** dans `dream_runs`. Son soak est indépendant.
- **Aucun killswitch existant ne change de valeur.** Ni PROMOTE, ni REORG, ni EXTRACT, ni
  ROADMAP. Le drop-in vivant porte **7** directives `Environment=` actives (§7.1 : la première
  écriture disait 8, en comptant un commentaire) et ce lot n'en modifie aucune.
- **`BRAIN_DREAM_CAPABILITY_ENFORCEMENT` reste absente** jusqu'à l'étape 8, et son armement est
  un lot à lui.
- **Le recalibrage du decay ships derrière un réglage**, valeurs d'aujourd'hui par défaut (§5.5).
  C'est le seul élément qui n'a pas d'irréversibilité mais a un effet immédiat sur un humain.
- **Ce qui n'a PAS besoin d'être fermé, et pourquoi** : le producteur de candidats (§4.3) est un
  `SELECT`. `freshness_status_updated_at` est une colonne sans backfill — `NULL` veut dire
  « jamais mesuré », doctrine 040/041. Ni l'un ni l'autre ne peut rien casser en étant ouvert,
  et les fermer retarderait l'horloge dont la purge dépend.
- **Le lot R n'a pas de killswitch, et n'en aura pas — parce qu'il n'en existe pas d'utile.**
  REORG est **WET** aujourd'hui ; son killswitch protège de la phase entière, pas d'un changement
  de portée à l'intérieur. Ajouter un drapeau propre à R donnerait l'illusion d'une garde alors
  que la vraie garde est ailleurs : **REORG ne fait que ce que son prompt lui dit**. Tant que
  `phase_reorg.md` pagine, la capacité de `pollution_patterns.py` est inerte, et c'est
  *vérifiable* — `git grep` sur les appelants, pas un booléen à croire. L'interrupteur de R est
  donc le prompt lui-même, et l'armer est un merge, pas un flip.
- **La 042 n'est pas un killswitch** : nullable et sans backfill précisément pour qu'un lecteur
  non migré et un écrivain migré coexistent.

---

## 10. Limites assumées

- **Le scope serveur est éteint, donc la boucle multiplie le travail sans multiplier la
  couverture** (§1.3). Ouverte avant l'étape 8, elle ferait 25 fois par nuit ce qu'elle fait une
  fois aujourd'hui. C'est la limite dominante de tout le chantier.
- **Aucune nuit à plus d'un projet n'a jamais tourné.** Tous les dimensionnements de §7 sont des
  simulations sur des nuits réelles à un projet. Inchangé depuis la v1.
- **Le taux de nuit rouge devient une non-information.** v1 §9 mesure 6,7 % (30 nuits) et 16,5 %
  (121 nuits) de nuits avec au moins une ligne agent non-`done`. Sous la même hypothèse
  d'indépendance — fausse dans le sens conservateur — 14 projets par nuit donnent
  `1 − (1−p)^14` = **62 %** au taux récent et **92 %** au taux historique. Une unité rouge deux
  nuits sur trois ne dit plus rien. La conclusion de la v1 §9 tient et se durcit : **le signal
  utile est le contenu du rapport groupé par projet, jamais la couleur de l'unité.**
- **Le compteur « humain » compte les lectures hors-dream, pas les lectures humaines** (§3.4),
  et `access_log` ne garde aucun historique pour le vérifier.
- **`freq_baseline` et les seuils ne sont pas calibrés** au sens de `thresholds.py`
  (`calibrated=False`). Les six valeurs de §5.3 viennent d'une distribution de **deux jours** —
  et, mesuré, elles ne changent le multiplicateur que de **51 entités sur 2 343** aujourd'hui.
- **Le lien score → REORG n'a pas de destinataire** — et depuis §12.3, il n'en aura pas : le
  couplage est refusé, pas ajourné. Zéro des 500 plus bas multiplicateurs matche la liste blanche,
  et §12.2 en donne la raison de fond — les deux mécanismes visent des populations opposées par
  construction. Ce n'est donc plus « la limite dominante de la §4 » en attente d'un arbitrage :
  c'est une **non-limite**, la §4 ne débouchant simplement pas sur REORG. Ce qui reste ouvert est
  plus étroit et clairement nommé : **à qui le classement s'adresse**, si ce n'est pas REORG
  (§12.6). Le scope éteint reste, lui, la limite dominante de la §2.
- **Le producteur de candidats n'a pas de mécanique de rotation spécifiée** (§4.3, propriété 7) :
  sans elle, `limit=20` est une fenêtre gelée sur 2 343 entités.
- **Le seuil d'archivage de `decay.py` est inutilisable sur ce corpus** (minimum 0,2228 > 0,2) et
  la spec le contourne par un rang plutôt que de le retoucher. Un corpus plus vieux le rendra
  utilisable ; personne ne s'en apercevra automatiquement.
- **Le producteur de candidats coûtera un calcul par entité et par projet**, non mesuré : la
  formule est pure et sans I/O (`decay.py`), mais le chargement des lignes ne l'est pas.
- **Les projets vides sont sautés sur une inférence** (§2.8), pas sur une décision de
  l'opérateur.
- **Le plafond des jamais-servis vient d'un seul mois** (mars 2026, 25 créations). Une seule
  observation — et sa valeur a été corrigée deux fois par la revue (§2.5).
- **Aucun plafond par projet plus fin que celui qui existe déjà** (§2.7) : le plafond structurel
  est de 53 min sans retry / 96 min avec, soit 47 % de la boucle ; c'est la **distribution
  empirique** qui manque, pas le plafond.
- **La rotation des journaux reste absente** (1 865 fichiers / 121 Mo, v1 §16) et la fenêtre
  élargie en avance l'échéance.
- **`dream_runs` n'a pas de colonne de projet aujourd'hui**, donc toute l'histoire d'avant la 042
  est définitivement inattribuable. La v1 le disait pour la télémétrie ; §4.2 montre que ça vaut
  aussi pour l'audit d'archivage.

### Ce que je n'ai pas pu mesurer

1. **Le débit de l'abonnement qui authentifie Codex.** v1 §17.1 l'avait déjà nommé comme le
   risque non chiffré le plus sérieux ; à 14-25 projets par nuit il grandit d'autant. ~11,6 M
   jetons/nuit à 8 projets était déjà une extrapolation.
2. **La durée réelle d'un run agent scopé sur un projet non-`brain-v42`.** Jamais exécuté. Le
   budget d'admission de 15 min (§7.3) vient de nuits **non scopées** sur **un** projet ; il est
   la meilleure estimation disponible et il sera faux le jour de l'étape 8.
3. **Le comportement réel du chemin `DecayFlusher` en exécution.** Les conclusions de §4.1 sont
   arithmétiques (constantes de `decay.py`) et corroborées par trois snippets `stale`, pas par
   une exécution instrumentée. La revue a en revanche exécuté le vrai `DecayCalculator` sur le
   corpus (§0) : c'est la **formule** qui est désormais mesurée, pas le **chemin** qui l'appelle.
   Corroboration supplémentaire du plancher à 0,44 : `freshness_status='stale'` n'existe **que**
   sur les snippets (3 lignes) et sur aucune autre table — mesuré sur les six.
4. **L'identité des acteurs.** `access_log` : 0 ligne. Ni le mélange humain/machine, ni le nombre
   d'acteurs distincts, ni la validité du préfixe `dream-codex-` comme seule marque système.
5. **Le coût Neo4j** d'une purge ou d'une passe de candidats, et le nombre de nœuds orphelins que
   le graphe porte déjà.
6. **L'archivage du 2026-06-30 sur `red-shrik:agent`** : la coïncidence de fenêtre est un
   faisceau (§4.2), pas une trace. Aucune donnée d'audit ne peut trancher, et il n'y en aura
   jamais pour le passé.
7. **Les consommateurs hors dépôt du payload `/metrics`** (red-monitor) face aux lecteurs
   `DISTINCT ON (phase)` : non inspectés, comme dans la v1.
8. **Le comportement de `record-empty-pool`**, qui n'avait toujours écrit **aucune** ligne au
   moment de la v1 §10. Non re-vérifié aujourd'hui.
9. **La distribution des durées par projet**, qui n'existe pas : `dream_runs` n'a pas de colonne
   de projet. Toute la §7 raisonne sur la durée d'une **nuit**, pas d'un projet.

### Hors périmètre

- **La sémantique de préfixe** (`red-lab` voyant `red-lab:architect`). D1 rend les six clés à
  deux niveaux éligibles comme projets à part entière ; **agréger** un parent et ses enfants
  reste un chantier sans aucune mécanique dans le code (v1 §2 : `promote_prepare.py` filtre par
  égalité exacte).
- **L'ouverture de `sweep` en WET** — décision indépendante, jamais dans le même lot.
- **Le rattachement des 19 artefacts à clé sans `project_context`** : famille
  `2026-07-27-orphan-project-key-bind-design.md`. (Les « 26 artefacts sans `project_key` » de la
  première écriture n'existent pas — voir l'encadré de §2.1.)
- **`scripts/dream/cross_project_resonance.py`** — aucun site d'appel (v1 §18).
- **Le projet dans `X-Brain-Agent`** — bloqué par `_MAX_AGENTS = 32` (v1 §14.2).
- **La rotation des journaux**, préexistante.
- **Le panneau red-monitor** et tout affichage hors dépôt.

---

## 11. Revue adversariale du 2026-08-08 (03:24 UTC)

Chaque chiffre de la première écriture a été rejoué contre la production, à l'exception de ceux
explicitement marqués « v1 §N ». Les commandes sont celles citées dans le corps du document.

### 11.1 Ce qui a tenu, à l'identique

`alembic current` = **041**. 55 `project_contexts` · 9 clés orphelines / 19 artefacts · 9 contextes
vides · rythme de création mensuel (6 · 8 · 25 · 2 · 1 · 3 · 7 · 3). Durées agent
**9,28 / 14,73 / 30,50** et globales **5,77 / 42,26**, sur exactement 30 nuits. Huit phases dans
`dream_runs`, toujours pas de `sweep`. 363 sessions sur 17 projets, FK `RESTRICT`. 1 180 jamais
lus · 2 522 machine seule. Le tableau complet de §5.1, ligne à ligne. 1 779 / 51 / 912. Toute la
§4.2 : 348 tombes, 11 jugements, 10 sur `brain-v42`, 1 sur `red-shrik:agent` à
`2026-06-30 04:20:57`, à l'intérieur de la fenêtre REORG `04:15:41 → 04:21:47` — et le sujet
`test_phase2_red-shrik` matche bien `^test_` de la liste blanche. Toute la §5.4, re-calculée par le
**vrai** `DecayCalculator`. Toute la §6.2 : 0 candidat partout, 98 / 2 / 0 / 1 / 0 en attente,
échéance 2026-09-09. 105 lignes dans `dream_promotions`. Toutes les valeurs systemd et **tous** les
numéros de ligne cités (`dream.sh:20, 58-59, 106-113, 116-123, 196-197, 260, 351, 439, 447, 455,
469, 501, 504-510, 522, 539, 664, 706, 735` ; les quatre lignes de prompt en dur ; les cinq
`DreamProjectToolPolicy()` vides ; `install.sh` qui ne compte que `Environment=`). Les
probabilités de §10 (62 % / 92 %).

**Et la question centrale de la revue est tranchée dans le sens de la spec** : la priorité humaine
ne redevient jamais un filtre. Simulé sur les 55 projets réels — 44 à priorité nulle — aucun projet
n'est jamais affamé, dans aucune des cinq configurations testées (§2.4).

### 11.2 Ce qui a changé

| § | ce qui était écrit | ce qui est mesuré |
|---|---|---|
| **2.1, 10** | « 26 artefacts sans `project_key` », en colonne « valeur mesurée » | **0**, sur les cinq tables et sur `indexed_plans`. Chiffre hérité de la v1, jamais rejoué |
| **4.3 (5)** | le veto `access_count > 5` « annule le nominateur » | intersection **vide** au rang 20 et au rang 100 ; le veto ne mord qu'au rang 264 |
| **4.3 (6)** | rien | **la liste blanche de `phase_reorg.md:64-71` est la vraie porte, et elle ferme à 100 %** : 0 des 500 plus bas multiplicateurs matche l'un des six motifs. Le lien est un no-op tant que le mandat de la Partie 2 n'est pas tranché |
| **4.3 (7)** | rien | le producteur est lui-même un cliquet : ordre figé, refus non mémorisé, `limit=20` gelé sur 2 343 entités |
| **4.3, 6.6** | « il n'existe **aucune** horloge honnête » | `brain_entities.updated_at`, via un trigger qui **exclut** `access_count`, en est une — imparfaite mais immunisée contre le flusher |
| **4.3** | « la doctrine 040/041 » | il faut choisir : c'est **041** (trigger conditionnel), parce que `freshness_status` a quatre écrivains dont un prompt |
| **5.3** | six baselines présentées comme le cœur du recalibrage | **inertes** : 51 entités sur 2 343 changent, `stale`/min/p10 identiques. Tout l'effet vient de la substitution du compteur |
| **6.2** | « 98 learnings qui suivent » le 2026-09-09 | **97 des 98 sont des pierres tombales de fusion** ; hors tombes, le rayon d'explosion vaut **1** |
| **6.3** | 31 clés / 567 artefacts | **32 / 580** vingt minutes plus tard — le rayon bouge à vue d'œil |
| **6.5** | une garde structurelle (`brain_sessions`), trois couplages | **deux** gardes (`brain_entities → projects` RESTRICT bloque les 74 lignes de `projects`), **quatre** FK `SET NULL` depuis `dream_promotions` |
| **7.3** | échéance **absolue** « par exemple 08:55 » | incompatible avec `Persistent=true`, mesuré au même endroit : un rattrapage tardif admet **zéro** projet, en silence |
| **7.3 / 2.5** | couverture 2,5 / 4,0 / 8,2 nuits | calculée **sans** le plafond des jamais-servis ; avec un plafond fixe, 8 nuits au p90. §2.5 corrigée pour rendre les deux compatibles |
| **2.5** | « ~12 sur ~25 », « absorbe mars en deux nuits » | 12 absorbe 25 en **trois** nuits ; et la fenêtre sur laquelle §7.3 planifie vaut **14**, dont la moitié est **7** |
| **2.7** | « aucune donnée pour calibrer un plafond par projet » | le plafond existe : **53 min** sans retry, **96 min** avec, soit 47 % de la boucle. C'est la distribution qui manque |
| **1.1** | « à 55 projets, 3 050 min » | **2 950** (55 × 53 + 35) |
| **7.1, 9** | « `killswitches.conf` 8 `Environment=` » | **7** ; le motif non ancré comptait un commentaire |
| **5.4** | p10 humain 0,2906 | **0,2905** (code réel contre réplique SQL) |
| **0** | réserve : « le Python n'a pas été exécuté » | **levée** — le vrai `DecayCalculator` a été exécuté, et il concorde |

### 11.3 Ce que la revue n'a pas pu faire

- **Exécuter une nuit.** Aucune boucle multi-projets, aucun curseur, aucun producteur de candidats
  n'existe. Tout ce qui touche au comportement nocturne reste une simulation sur des durées
  mesurées de nuits **à un projet**.
- **Vérifier ce qu'un juge LLM ferait** d'une liste de candidats classée par le score. La
  propriété 6 de §4.3 mesure ce que le **prompt** interdit, pas ce que le modèle déciderait si on
  le lui permettait.
- **Trancher l'archivage du 2026-06-30.** `access_log` reste à 0 ligne ; le faisceau reste un
  faisceau.

---

## 12. Passe de mesure du 2026-08-08 (11:00 UTC) — le destinataire est tranché

La §5.4 se terminait sur « Le classement est bon ; le destinataire reste à choisir ». Cette passe
choisit, et la mesure qui la fonde déplace le problème : **la propriété 6 de §4.3 était vraie mais
trop étroite.**

### 12.1 Ce que la propriété 6 ne disait pas

La revue de 03:24 mesurait « 0 des 500 plus bas multiplicateurs matche l'un des six motifs ». Exact,
re-vérifié. Mais la question posée à l'envers donne un fait plus lourd — mesuré sur les 3 184
entités ni archivées ni fusionnées (2 345 learnings + 839 decisions), regex appliquées telles
quelles et vrai `DecayCalculator` :

| | mesure |
|---|---|
| cibles vivantes de la liste blanche, **tout le corpus** | **7** |
| leurs rangs par `created_at DESC` | **587, 920, 921, 923, 925, 926, 927** |
| fenêtre que `phase_reorg.md` Partie 1 pagine | **500** premiers, sur 2 345 learnings |
| runs `reorg` en `done` depuis le 2026-04-06 | **112** |
| learnings archivés **hors** tombe de fusion, tout le corpus | **1** |

**REORG Partie 2 n'a jamais pu atteindre ses propres cibles.** Ce n'est pas le producteur de
candidats qui bute sur la liste blanche : la phase, seule et depuis le premier jour, pagine les 500
learnings les plus récemment créés, et ses sept cibles ont été créées le 2026-04-17. L'écart se
creuse mécaniquement à chaque nouveau learning.

L'unique archivage hors tombe du corpus est très probablement celui du 2026-06-30 déjà instruit en
§4.2 et §11.1 — `test_phase2_red-shrik`, dans la fenêtre REORG `04:15:41 → 04:21:47`. Autrement dit
la Partie 2 a mordu **une fois en 112 nuits**, et le faisceau de §11.3 gagne ici un chiffre : ce
n'est pas « on ne sait pas si REORG l'a fait », c'est « si REORG l'a fait, c'est sa seule prise ».

### 12.2 Les deux mécanismes sélectionnent des populations opposées

C'est la raison de fond, et elle interdit le remède évident. Rangs des sept cibles **par decay**,
sur 3 184 : **948, 1318, 1320, 1321, 1359, 1401, 2115**, scores 0,35 à 0,57. Elles sont au milieu
et en haut du classement, pas en bas — parce que la pollution visée est de la sortie machine
**récente** (`red-shrik:agent`, dernier artefact il y a 36 jours), et que le decay note la
récence.

Le bas du classement, lui, n'est pas de la pollution : c'est le corpus complet de projets finis.
`second-cerveau` (215 j), `lyriks` (213 j), `datalake-v2` (167 j), `poc-lyriks-v2` (165 j).

Élargir la liste blanche ne « réparerait » donc pas le producteur — les deux ensembles ne se
recoupent pas par construction, et aucun élargissement raisonnable ne les fera se recouper. Ça
changerait ce que REORG **est**, exactement comme la §5.4 le craignait.

### 12.3 Tranché : réparer la PORTÉE, pas la largeur

Décision opérateur du 2026-08-08. La liste blanche garde ses six motifs. Ce qui change est le
**scope du scan** : une requête exacte sur les motifs, au lieu d'une fenêtre de 500 lignes triée
par une colonne sans rapport avec eux.

Conséquences pour cette spec :

- Le couplage « producteur de candidats → REORG Partie 2 » est **refusé**, pas ajourné. Le
  classement par decay et la liste blanche ne visent pas la même population ; les brancher l'un sur
  l'autre serait un contresens quel que soit le réglage.
- Le destinataire du classement reste donc à écrire, mais il n'est plus *bloquant* : REORG redevient
  opérant sans lui. Les deux chantiers se séparent proprement.
- Le bas du classement est un signal de **projet**, pas d'entité — 9 entités pour `lyriks`, 3 pour
  `second-cerveau`. Piste ouverte, non tranchée ici : une décision humaine par projet fini plutôt
  que 500 verdicts LLM par entité. Elle rejoint le ticket d'entrée de la purge de §6.4.

### 12.4 Livré inerte — `b917c0b7`, branche `fix/reorg-scope-pollution-candidates`

`src/brain_v42/services/pollution_patterns.py` sort les six motifs de la prose et les épingle par
test. La clause SQL est **dérivée du même tuple**, donc la dérive entre le matcher Python et la
requête est impossible par construction.

**Correction de la première écriture de cette section.** Elle affirmait « pas de test adossé à la
base : `BRAIN_V42_TEST_DB_URL` n'est posée nulle part et le ticket `71576155` enregistre que ces
tests ne tournent jamais en CI ». Les deux moitiés étaient fausses. Mesuré depuis : les **deux**
rails posent la variable sur le job **unitaire** — `.gitlab-ci.yml:131` et
`.github/workflows/continuous-integration.yml:98` — plus un `alembic upgrade head`, parce que
`tests/unit/` n'a pas de fixture qui crée le schéma. C'est le correctif du ticket `71576155`, qui
est donc **validé** : `test_promote_prepare.py` rend 23/23 avec la variable posée, et le
commentaire de la CI porte la mesure du trou bouché — 7 249/54 sans, 7 294/9 avec, soit 45 tests
qui n'avaient jamais tourné. Le ticket peut être clos.

Le test manquant est donc écrit (`test_pollution_clause_pg.py`, `a3bd7ab9`). Il couvre ce que la
dérivation depuis un tuple unique ne couvre pas : **les deux moteurs de regex doivent s'accorder**.
Python compile avec `re.IGNORECASE`, PostgreSQL reçoit la même chaîne via `~*` dans son ARE ; que
`\d`, `.*`, `^` et `$` veuillent dire la même chose des deux côtés est une question empirique sur
PostgreSQL, pas un choix de conception. Ce n'est **pas** le patron de tables miroir SQLite (snippet
brain `4b86f802`) : `~*` est un opérateur PostgreSQL que SQLite ne parserait pas, donc un miroir ne
testerait rien ici.

Les tests mordent, vérifié par mutation : `~*` → `~` fait tomber 3 tests, retirer le `$` de
`_events_\d+h$` en fait tomber 2 ici et 2 dans les tests purs.

La mesure empirique contre le corpus réel reste vraie et garde sa valeur propre — un test porte sur
un jeu construit, elle porte sur la production : sur 2 744 learnings et 844 decisions, SQL et Python
sélectionnent des ensembles identiques (54 et 0).

Chaque ancre porte son jumeau négatif, selon la règle de méthode du focus : retirer le `^` de
`^test_` fait matcher `my_test_helper` ; retirer le `$` de `_events_\d+h$` fait matcher
`count_events_24hours`.

**Rien n'appelle ce module et `phase_reorg.md` n'est pas touché.** Le comportement nocturne est
donc inchangé, et c'est *vérifiable* plutôt que promis par un drapeau : ici le prompt **est**
l'interrupteur, puisque REORG ne fait que ce que son prompt lui dit. L'armement sera un geste
séparé — câbler le filtre dans `brain_list`, puis réécrire la Partie 2 pour qu'elle interroge au
lieu de paginer. Le jour où ça atteint `main`, REORG archive ses sept cibles la nuit suivante ;
c'est réversible, mais la racine est un working tree déployé.

Gates, codes de sortie relevés directement et pas derrière un `tail` : `ruff check` 0,
`ruff format --check` 0, `mypy` 0. Suite unitaire **dans la configuration de la CI**, variable de
base posée sur `brain_test` — jamais sur `brain`, que le garde-fou de
`tests/integration/conftest.py` rejette nommément : **7 345 passés, 9 skippés**. Les 9 skips
correspondent exactement au chiffre relevé par le commentaire de la CI, ce qui vaut recoupement.
Sans la variable, la même branche rend **7 294 passés / 60 skippés** : les 51 tests de ce lot
tournent, mais les 4 nouveaux tests PostgreSQL rejoignent les 56 qui skippaient déjà. C'est le
régime que décrivait la première écriture — une exécution locale, pas ce que fait la CI.

### 12.5 Un fait neuf qui touche la §6.5

**41 pierres tombales de fusion sur 395 (10 %) portent encore `freshness_status='fresh'`** — 40
learnings, 1 decision, contre 348 + 4 correctement archivées. Elles sont masquées de `brain_list`
par `pg_base.py:494`, qui ajoute `merged_into IS NULL` quand `include_archived=False` — une clause
**indépendante** de leur fraîcheur.

C'est exactement le piège que la §6.5 nomme (« toute règle doit exclure `merged_into IS NOT NULL`,
ou dire pourquoi elle ne le fait pas ») : une règle qui filtre sur `freshness_status` seul les
compte comme du savoir vivant, alors que l'outil, lui, ne les montre jamais. Incohérence de CLEAN,
pas de la v2 ; tracée ici, pas traitée.

### 12.6 Ce que cette passe n'a pas fait

- **Aucune nuit exécutée**, toujours. Le chiffre « 7 cibles » est un état du corpus au 2026-08-08
  11:00, pas un débit nocturne.
- **Le câblage n'existe pas.** `brain_list` n'a pas de filtre sur les motifs ; tant qu'il n'existe
  pas, « la portée est réparée » décrit une capacité, pas un comportement.
- **Le destinataire du classement par decay reste à écrire**, et avec lui la question de projet de
  §12.3.
- **L'archivage du 2026-06-30 n'est toujours pas tranché** — `access_log` reste à 0 ligne. Cette
  passe l'a seulement encadré par un majorant : au plus une prise en 112 nuits.

---

## 13. Lot 1 — livré le 2026-08-08

Branche `fix/dream-lot1-prompt-scope`, deux commits, non fusionnée. Le lot est écrit ici **après**
sa livraison, ce qui est l'ordre inverse du bon : les deux décisions de conception ci-dessous ont
été prises dans la conversation puis codées, sans passer par ce document. Elles y sont maintenant,
et c'est la spec qui fait foi.

### 13.1 Les quatre lignes de prompt — `ca13c1e9`

Trois des quatre nommaient `brain-v42` en position de **périmètre** : `phase_promote.md:4`,
`phase_synth.md:24` et `:58`. Rendues pour un autre projet, elles disent à l'agent d'écrire dans
brain-v42 — SYNTH l'ordonnait même en garde-fou (« Always use `project_key="brain-v42"` »).

**La quatrième n'en est pas une.** `phase_connect.md:43` liste `brain-v42` dans un exemple de
taxonomie de domaine (« memory — knowledge graph, brain-v42, embeddings, … »). La templatiser
réécrirait la taxonomie par projet au lieu de nommer un sujet. Elle reste, sur liste blanche
**nommée** dans le test, avec sa raison — et un test vérifie que c'est bien cette ligne-là, pas une
autre qui aurait dérivé dessous. Le compte « quatre » de §1.1 était donc exact mais mélangeait deux
natures.

**Décision : préserver le rendu à l'octet plutôt que d'améliorer la formulation.** « v1 is
brain-v42 only » devient « v1 is `{{PROJECT_KEY}}` only » et non une tournure plus propre, parce
que reformuler change le prompt d'un LLM et qu'aucune reformulation n'est prouvablement neutre.
C'est ce qui rend l'inertie du lot **vérifiable** : les six prompts rendus pour
`PROJECT_KEY=brain-v42` sont identiques avant et après (`diff -r` sur les six sorties, exit 0). Le
nettoyage de cette tournure est un autre lot, et il devra s'assumer comme un changement de prompt.

### 13.2 Le contrôle de périmètre du validateur — `89b5926d`

`promote_validate.py` ne regardait pas `project_key`. À 55 projets, PROMOTE peut produire un ADR ou
un runbook dans le mauvais projet sans que rien en aval ne le voie.

**Décision d'opérateur : échec dur, pas enregistrement.** Une promotion mal étiquetée est une
violation d'intégrité référentielle au même titre que les autres de ce fichier — elle lève
`ValidationFailure`, et `main()` marque `dream_runs` en `partial` par le handler qui existe déjà.
L'alternative — consigner sans échouer — produisait un signal que personne ne lit, ce que la §11.3
reproche déjà au faisceau du 2026-06-30.

**`--project-key` est requis, sans valeur par défaut.** Un défaut `brain-v42` validerait
silencieusement chaque projet contre le mauvais périmètre le jour où la boucle s'ouvre : très
exactement la classe de bug que cet argument existe pour attraper. `dream.sh:574` passe
`$PROJECT_KEY`.

**Inerte aujourd'hui**, comme le reste du lot : à un projet, le learning source et l'entité produite
portent la même clé, donc le contrôle passe trivialement. Il ne mord qu'à l'ouverture de la boucle.

### 13.3 Ce qui rend ces tests non décoratifs

Les deux moitiés portent des jumeaux négatifs, selon la règle de méthode : un test qui n'asserte
qu'une présence ne peut pas témoigner du retrait d'un filtre. Retirer le `^` de `^test_` fait
matcher `my_test_helper` ; un contrôle de périmètre qui refuserait **tout** aurait l'air correct
sans son jumeau positif « même forme, projet correct, passe ».

Et les contrôles ont été éprouvés par **mutation** : neutraliser la comparaison ADR fait tomber son
test de rejet, neutraliser celle du runbook fait tomber le sien. Sans cette étape on ne sait pas si
un test couvre ou se contente d'exécuter la ligne (snippet brain `4b86f802`).

### 13.4 Ce que ce lot ne fait pas

- **Il n'ouvre aucune boucle.** Les phases restent invoquées une fois, sur `brain-v42`.
- **Il ne touche pas `phase_connect.md`**, par décision et non par oubli (§13.1).
- **Il ne vérifie pas la clé du learning *source***, seulement celle de l'entité produite. Mesuré
  plutôt que supposé : c'est sans conséquence, parce que le producteur est **déjà** scopé —
  `fetch_candidates(session_factory, project_key, limit)` (`promote_prepare.py:84`), alimenté par
  `dream.sh:462` avec le même `$PROJECT_KEY` que celui qui va maintenant au validateur
  (`dream.sh:574`). La phase promote lit donc et écrit sous une seule et même clé, de bout en bout.
  Ce qui reste non couvert est étroit : un candidat déjà mal étiqueté **en base** passerait les deux
  gardes, puisqu'elles s'accordent sur la clé sans jamais la mettre en doute.

---

## 14. Lot 2 — la 042, rendue exécutable

Écrit **avant** le code, contrairement au lot 1. La forme de la colonne était déjà arrêtée (§1.2)
et son rôle aussi (§2.3 : télémétrie, jamais ordonnancement). Ce qui manquait pour exécuter, c'est
l'inventaire réel des sites à toucher et la manœuvre hors bande. Tout ce qui suit est mesuré le
2026-08-08, pas recopié.

### 14.1 L'état de départ, remesuré

`dream_runs` porte **16 colonnes**, aucune de projet. Deux index seulement :
`dream_runs_pkey(id)` et `idx_dream_runs_date(run_date DESC)`. `alembic_version` = **041** en
production comme sur `brain_test`, et le dernier fichier de `alembic/versions/` est
`041_corpus_provenance.py` : **la 042 n'existe pas encore**, ni en base ni sur disque.

Huit phases présentes — `connect` 136, `clean` 136, `scan` 136, `synth` 135, `reorg` 116,
`promote` 112, `roadmap` 52, `extract` 41 — et toujours **pas de `sweep`**, ce qui reconfirme §1.2.

La 042 ajoute `project_key VARCHAR(64) NULL`, sans backfill, plus l'index `(run_date DESC,
project_key)`. `NULL` veut dire « ligne écrite avant la 042 », pour toujours ; `'*'` est la
sentinelle des phases globales.

### 14.2 L'inventaire, et la distinction que « 5 écrivains / 10 lecteurs » ne fait pas

> **Ce tableau est faux d'une ligne, et §15 fait foi.** La passe du 2026-08-09 a mesuré **six**
> `INSERT`, pas cinq : il manque `scripts/dream/_promote_helpers.py:125`, que cette section range
> par ailleurs du côté des **lecteurs**. Il est vivant et a écrit les lignes `promote` des deux
> dernières nuits. La liste des `SELECT` est également sous-comptée (§15.6). **Ne pas prendre ce
> tableau pour la liste de travail** — c'est §15.3 qui la porte.

Le compte de §8 est exact mais il ne parle que des `INSERT`. Mesuré, il y a **neuf** sites
d'écriture, de deux natures :

**Cinq `INSERT` — ceux qui doivent porter `project_key` :**

| site | phase écrite | vivant ? | échec | clé à poser |
|---|---|---|---|---|
| `metrics/dream_parser.py:181` | paramétrée | **oui — le seul qui écrit les six phases par projet** | lève, mais `dream.sh:338` l'avale en `WARN … (non-fatal)` | la vraie clé |
| `scripts/ticket_extract.py:752` | `extract` | oui, 41 lignes | avale — « Best-effort — never raises » | `'*'` |
| `scripts/roadmap_curate.py:1146` | `roadmap` | oui, 52 lignes | avale — même docstring | `'*'` |
| `maintenance/session_sweep.py:94` | `sweep` | livré mais **désarmé, 0 ligne** | avale — « la trace ne doit jamais tuer la phase » | `'*'` |
| `dream/cross_project_resonance.py:163` | `RESONANCE` | **mort** — 0 ligne, aucun appelant | lève | `'*'`, sans le câbler |

**Deux corrections à la première écriture de ce tableau, mesurées le 2026-08-09.**

D'abord, `cross_project_resonance` **n'est pas un écrivain vivant**. Sa constante vaut
`PHASE = "RESONANCE"` et `dream_runs` n'a jamais porté une ligne de ce nom ; un `grep` sur tout le
dépôt ne trouve aucun appelant — ni `dream.sh`, ni une unité, ni un autre script. Seuls ses propres
tests et une entrée de `thresholds.py` le mentionnent. La première écriture le rangeait parmi les
écrivains « qui lèvent », ce qui lui donnait une importance qu'il n'a pas.

Ensuite, et c'est le point qui compte : **le partage « trois avalent, deux lèvent » était faux.**
`dream_parser` lève bien en Python, mais `dream.sh:338` attrape et journalise
`WARN dream_parser failed for $name (non-fatal)`. Donc **les cinq perdent leur trace en silence** ;
ce qui diffère est l'endroit où l'exception est avalée — dans la fonction ou dans l'orchestrateur —
pas le fait qu'elle le soit.

**Tranché sur `cross_project_resonance` (2026-08-09) : il reçoit `'*'` sans être câblé.** Trois
lignes, et ça évite qu'il soit le seul écrivain incohérent le jour où quelqu'un le rebranche. Le
câbler ou le supprimer sont deux autres décisions, hors de ce lot.

**Ce que le lot pèse réellement**, une fois ce tableau lu : **un** écrivain qui compte aujourd'hui,
**deux** sentinelles vivantes, **une** sentinelle dormante, **un** mort qu'on met en cohérence.
« Cinq écrivains » surestime le travail.

**Quatre `UPDATE` — ceux qui ne doivent PAS y toucher**, nommés ici pour que personne ne les
« complète » par symétrie : `cross_project_resonance.py:181`, `reorg_validate.py:271`,
`promote_validate.py:192`, `connect_validate.py:110`. Ils visent `WHERE id = :id` et ne posent que
`status`, `duration_s` et `error_message`. La ligne existe déjà, sa clé aussi ; la réécrire serait
une occasion de la contredire.

**Dix `SELECT`, sur sept fichiers** : `collector_dream.py` (:90, :161, :175), `collector_nightly.py`
(:84), `_promote_helpers.py` (:91), `post_run_alert.py` (:115), `dream.sh` (:612),
`dream_preflight.py` (:107), `connect_validate.py` (:100). Aucun ne filtre par projet aujourd'hui —
c'est ce qui rend la colonne inerte tant qu'ils ne sont pas touchés, et donc sûre à livrer avant eux.

### 14.3 Pourquoi `NULL` n'est pas de la prudence, mais une conséquence mesurée

Le tableau ci-dessus donne la raison, et elle est plus forte que « on évite un backfill » :
**les cinq `INSERT` perdent leur trace en silence**. Trois l'avalent dans leur propre fonction,
c'est écrit dans leurs docstrings ; le quatrième — `dream_parser`, celui qui porte toute la
télémétrie par projet — lève, mais son orchestrateur l'attrape (`dream.sh:338`,
`WARN … (non-fatal)`). Une colonne `NOT NULL` transformerait donc une erreur de schéma en
avertissement imprimé **partout**, pas sur trois sites — la nuit perdrait sa trace sans un bruit,
ce qui est le défaut que cette spec cherche à supprimer, pas à déplacer.

> La première écriture disait « trois des cinq », et en tirait un alignement séduisant : les trois
> best-effort auraient été exactement les trois phases globales. C'était vrai par accident et faux
> en substance — l'argument est **plus fort** sans lui, parce qu'il porte sur les cinq. Une
> élégance qui repose sur un décompte est une élégance à revérifier.

**Qui écrit quoi**, sans chercher de symétrie : `dream_parser` reçoit la vraie clé, parce qu'il est
invoqué une fois par phase et par projet (`dream.sh:334`). Les quatre autres écrivent la sentinelle
`'*'` — `extract`, `roadmap` et `sweep` parce que ce sont les phases globales de §1.2 et qu'une
phase globale n'a pas de projet à nommer ; `RESONANCE` parce qu'il est mort et qu'on le met en
cohérence plutôt que de le laisser seul incohérent.

### 14.4 La manœuvre hors bande

Contrainte reprise de §8 et de la v1 §12 : `dream.sh` tourne depuis le working tree de la
**racine**, donc merger sur `main` c'est déployer. La 042 s'applique en production **avant** le
merge qui introduit ses lecteurs. Séquence :

1. Écrire `042_dream_runs_project_key.py` sur une branche, dans un worktree — qui ne déploie pas.
2. L'appliquer d'abord sur `brain_test`, la seule base où un aller-retour est sans conséquence.
   Vérifier l'`upgrade` **et** le `downgrade` sur cette base.
3. L'appliquer en production **depuis le worktree**, pas depuis la racine.
4. **Prouver** la révision, ne pas la supposer :
   `docker exec brain_v42_postgres psql -U brain -d brain -Atc "select version_num from alembic_version;"`
   doit rendre `042`. Le CLAUDE.md porte l'avertissement qui justifie cette étape : cette ligne a
   affirmé « la production reste à 037 » pendant **trois jours** après une bascule.
5. Seulement ensuite, merger les cinq écrivains.
6. Les dix lecteurs viennent après, dans leur propre lot — ils n'ont aucune raison de partager le
   destin de la migration.

Entre 4 et 5, la production tourne avec une colonne que personne n'écrit : c'est l'état voulu, et
c'est ce que §9 appelle « la 042 n'est pas un killswitch ».

> **Cette séquence est incomplète — voir §14.7.** Elle a été écrite avant que la migration existe,
> et il lui manque l'étape des six épingles de tête, découverte en la codant. §14.7 porte la
> version corrigée, qui fait foi.

### 14.5 Le downgrade

Précédent mesuré : la 041 se contente de `op.drop_column`, sans garde fail-closed — celle-ci est
réservée aux migrations dont le retour perdrait un état qu'on ne peut pas reconstruire (037 pour
les captures de session). La 042 est de la télémétrie sans backfill : son downgrade est un
`drop_column` plus le retrait de l'index, et il perd les clés écrites depuis l'upgrade.

**Tranché le 2026-08-08 : comme la 041, un `drop_column` simple, sans garde fail-closed.** La perte
est réelle et assumée — un `dream_runs` sans clé de projet est exactement l'état d'avant, qui a tenu
135 nuits. Une garde n'aurait de sens que si la donnée perdue était irremplaçable ; ici elle est
reconstructible par une nuit de télémétrie, et §2.3 interdit déjà que quoi que ce soit d'important
en dépende. Refuser le downgrade rendrait au contraire le retour arrière plus dur que l'aller,
ce qui est le mauvais sens pour une migration de télémétrie.

### 14.6 Ce que ce lot ne fait pas

- **Il n'ouvre aucune boucle** et ne crée aucun curseur. §2.3 interdit explicitement de dériver
  l'ordonnancement de cette colonne, et le tableau de §14.2 en donne la preuve chiffrée : trois
  écrivains sur cinq peuvent ne rien écrire sans que personne ne le sache.
- **Il ne remplit pas le passé.** Les 864 lignes existantes resteront `NULL`. Toute mesure
  rétrospective par projet est donc impossible, définitivement — c'est le prix déjà payé, pas un
  prix nouveau.
- **Il ne touche pas les dix lecteurs.** Tant qu'ils ne filtrent pas, la colonne est de la donnée
  morte, et c'est précisément ce qui rend l'étape 5 sûre.

### 14.7 L'étape que §14.4 avait oubliée : les six épingles de tête

Découvert en écrivant la migration, pas en la concevant : **le dépôt refuse une bascule de tête non
documentée, par six tests distincts.** La manœuvre de §14.4 est donc incomplète : il lui manque une
étape, et c'est la plus importante, parce que c'est celle qui empêche la panne que le CLAUDE.md
raconte.

**Et l'inventaire ne se prend pas en un seul passage — c'est le fait de méthode de cette section.**
Poser `042` sur disque fait tomber **cinq** tests (mesuré : 5 échecs sur 7 299). Réparer ces cinq
en fait apparaître un **sixième**, qui ne pouvait pas mordre avant : il n'épingle pas la tête, il
épingle une *phrase de `SCHEMA.md`*, et cette phrase n'avait pas encore bougé. Un inventaire fondé
sur « ce qui casse quand j'ajoute la migration » est donc structurellement incomplet. La seule
mesure fiable est itérative : réparer, relancer la suite entière, recommencer jusqu'au vert.

| Épingle | Ce qu'elle exige |
|---|---|
| `test_alembic_cli_fail_closed.py:143` | le rendu **hors ligne** de toutes les migrations compte `Running upgrade` : `41` → `42` |
| `test_documentation_contract.py:1755` | `docs/SCHEMA.md` : « 41 révisions (001 → 041) », une ligne `\| 042 \|` dans le tableau, et le compte de tables au head |
| `test_documentation_contract.py:1778` | `migrations 001–042 defined` dans `ARCHITECTURE`, et `migration 042` dans `README`, `CLAUDE.md` et `MCP_TOOLS` |
| `test_plan_index_repair_head_pin.py:45` | une **revue explicite** avant tout bump de `_REQUIRED_ALEMBIC_HEAD` |
| `test_recovery_contract.py:271` | `script.get_heads() == ["041"]` — marqueur « ce contrat a été relu à cette tête » |
| `test_recovery_contract_v4.py:436` | `« La cible du dépôt est 041. »` dans `SCHEMA.md` — **doublon**, et le seul qui ne mord qu'au second passage |

**Pourquoi six et pas une.** Chacune garde une chose différente : que les migrations se rendent
hors ligne sans secret, que le schéma documenté existe, que quatre pages d'entrée soient d'accord,
que la réparation d'index ait été rejugée, que le contrat de recovery ait été relu. Une seule
épingle globale aurait été plus simple et aurait laissé passer cinq des six questions.

**La sixième est mal placée, et il faut le dire.** Elle vit dans un test nommé
`test_full_runbook_has_one_039_operator_order_and_no_live_claim` — un test sur l'ordre opérateur de
la 039 — et y assère au passage une phrase de `SCHEMA.md` que `test_documentation_contract.py:1764`
assère déjà. Un doublon caché dans un test qui parle d'autre chose est invisible à qui inventorie
les gardes, et c'est exactement ce qui m'a fait écrire « cinq ». Ce n'est pas un défaut de la
garde — elle fait son travail — mais de son emplacement.

**Et la docstring de la deuxième dit exactement ce que cette spec passe son temps à répéter :**

> « Until 2026-08-04 these docs asserted a production head of `037` while the database had been on
> `039` for three days. No page can prove a live head, so the docs now name the repository target
> and send the reader to measure the rest. »

C'est la distinction à ne jamais perdre : **le dépôt possède la tête du dépôt, il ne possède pas la
tête déployée.** Les documents nomment une cible ; `psql` seul dit l'état. Le nom du test contient
`041`, donc bumper impose de le renommer — le dépôt rend son propre marqueur impossible à oublier.

**La revue exigée par l'épingle `plan_index_repair`, faite et consignée ici.** Son message demande
de vérifier ce que la nouvelle tête change sur les trois tables que la réparation écrit —
`indexed_plans`, `indexed_plan_chunks`, `project_contexts` — en cherchant des triggers, des
contraintes ou des colonnes `NOT NULL` sans défaut. Mesuré : la 042 ne touche **que** `dream_runs`,
n'ajoute **aucun** trigger, **aucune** contrainte, et sa seule colonne est nullable sans défaut. Les
trois tables de la réparation sont intactes. **Le bump est sûr**, et c'est la seule des cinq
épingles qui demandait un jugement plutôt qu'un décompte.

**Un chiffre qui ne bouge pas, et il faut le dire** : un schéma neuf contient **32 tables `public`**
au head 041 comme au head 042 — mesuré sur `brain` et sur `brain_test`. La 042 ajoute une colonne,
pas une table, donc la phrase de `SCHEMA.md` ne change que de numéro.

**La manœuvre corrigée.** L'étape manquante s'intercale avant tout contact avec la production,
parce qu'elle est du code et de la documentation, pas une opération :

1. Écrire la migration en worktree.
2. **Mettre à jour les six épingles et les cinq documents, dans le même commit que la
   migration.** Une branche qui porte `042` sans ses épingles est rouge par conception.
3. Éprouver `upgrade` **et** `downgrade` sur `brain_test`.
4. Appliquer en production, depuis le worktree.
5. **Prouver** la révision par `psql`.
6. Merger les écrivains ; les lecteurs dans leur propre lot.

### 14.8 La septième garde, et le prix de l'ordre choisi

**La septième ne se découvre qu'en visant la production.** `alembic/env.py:70` refuse la base
`brain` sans `BRAIN_ALEMBIC_ALLOW_PROD=1|true|yes`. Les six autres sont statiques et tombent en
suite de tests ; celle-ci est à l'exécution, donc **aucun inventaire de tests ne pouvait la
contenir** — elle s'ajoute au fait de méthode ci-dessus, sous une autre forme. C'est le learning
`a567d2a8` (« alembic env.py a silencieusement migré la prod ») transformé en fail-closed.

**Et l'ordre « migration avant merge » a un prix, mesuré.** Entre l'étape 5 et l'étape 6, le code
déployé épingle encore `041` pendant que la base dit `042`. Or
`plan_index_repair_store.py:230` compare les deux et lève `RepairSafetyError("alembic_head_mismatch")`
sur divergence. **La réparation d'index est donc désarmée tant que la branche n'est pas fusionnée.**

Ce n'est pas une casse, c'est la garde qui fait son travail — et la fenêtre est bornée, connue, et
se referme d'elle-même au merge. Deux raisons de l'accepter plutôt que d'inverser l'ordre :
`grep` sur `dream.sh` et `scripts/dream/` ne trouve **aucun** appel à ce chemin, donc rien de
nocturne n'en dépend ; et inverser l'ordre — merger d'abord — mettrait en production du code qui
écrit une colonne absente, ce qui casse pour de bon au lieu de se refuser.

**À dire à l'opérateur au moment d'appliquer, pas après** : pendant cette fenêtre, une réparation
d'index refusera de démarrer. Si elle devient nécessaire avant le merge, le merge est le remède,
pas le contournement de la garde.

---

## 15. Lot 3 — les écrivains. Passe d'inventaire du 2026-08-09

La 042 est appliquée et fusionnée (`ecf7cf84`, `f94d078a`), la production et `brain_test` sont
mesurées à `042`, et **872 lignes portent `project_key IS NULL`** — aucun écrivain ne la pose. Ce
lot est celui qui la remplit. Il est spécifié ici **avant** son code, et la passe de mesure qui suit
a précédé la première ligne écrite.

### 15.1 Le tableau de §14.2 a une ligne de trop peu, et elle est vivante

**Il y a SIX sites d'`INSERT` dans `dream_runs`, pas cinq.** Le sixième est
`scripts/dream/_promote_helpers.py:125`, fonction `_record_empty_pool`, un
`sa.insert(dream_runs).values(...)` — la seule écriture du lot qui ne soit pas du SQL textuel.
`git grep -nE 'sa\.insert\(dream_runs|INSERT INTO dream_runs' -- src/ scripts/` en rend six ; aucun
`COPY`, aucun `executemany`, aucun `ON CONFLICT`.

**L'omission n'est pas un oubli de ligne : elle est active.** §14.2 range `_promote_helpers.py`
du côté des **lecteurs** (« `_promote_helpers.py` (:91) » dans la liste des dix `SELECT`). Le
fichier était donc lu, classé, et classé du mauvais côté. Un inventaire qui nomme un fichier ne
prouve pas qu'il l'a compris.

**Et ce sixième écrivain n'est pas dormant — il écrit les nuits en cours.** Mesuré :

```
select phase,status,count(*),min(run_date),max(run_date) from dream_runs
where error_message like 'empty candidate pool%' group by 1,2;
→ promote|done|2|2026-08-08|2026-08-09
```

Les deux dernières lignes `promote` de la base (ids 906 et 914) portent `model IS NULL` et
`EMPTY_POOL_MESSAGE` : elles viennent de lui, pas de `dream_parser`. La cause est connue et
légitime — depuis le filtre de maturité de la 041, le pool de candidats est régulièrement vide, et
`e8b59c3c` a précisément rendu ce vide observable au lieu d'inaudible. **`dream_parser` n'a plus
écrit une seule ligne `promote` depuis le 2026-08-06.** L'affirmation de §14.2 — « le seul qui écrit
les six phases par projet » — est donc fausse pour la phase `promote`, et fausse depuis avant
l'écriture de §14.2.

**Conséquence sur la sémantique, et c'est le point qui rend ce lot non négociable.** Livrer les cinq
écrivains du tableau laisserait `promote` à `NULL` une nuit sur deux, alors que les sept autres
phases porteraient une clé. Or `NULL` a un sens arrêté : « ligne écrite avant la 042 ». Un `NULL`
d'après-042 le détruirait — et il serait invisible, parce que la ligne existe et vaut `done`.

`promote` est une phase **par projet**. Le sixième écrivain reçoit donc la **vraie clé**, pas la
sentinelle. Il lui faut un `--project-key` requis sur sa sous-commande `record-empty-pool` et son
câblage depuis `scripts/dream.sh:490`.

### 15.2 La spec cite les bons numéros pour le mauvais rail

§14.2 et §14.3 s'appuient sur `dream.sh:334` (invocation) et `dream.sh:338` (le `WARN … non-fatal`
qui avale l'échec). **Ces deux lignes sont la branche Claude, qui est le repli explicite de
l'opérateur et ne tourne pas en production.** `scripts/dream.sh:19` :

```bash
BRAIN_DREAM_AGENT_PROVIDER="${BRAIN_DREAM_AGENT_PROVIDER:-codex}"
```

Le rail vivant est `dream.sh:326-331`, qui invoque `brain_v42.metrics.codex_dream_parser` — ce que
confirment les lignes récentes de `dream_runs`, toutes en `model='gpt-5.6-sol'`. Le raisonnement de
§14.3 (« il lève, mais son orchestrateur l'attrape ») reste **exact**, parce que les deux branches
avalent de la même façon ; seuls les numéros désignent le chemin mort.

**Un `INSERT`, deux points d'entrée.** `codex_dream_parser.py:12` importe `_insert_dream_run` de
`dream_parser`, mais possède son **propre** `_build_arg_parser` (`:117-129`) et son propre `main()`.
Les deux consomment le **même** tableau `parser_args` construit une seule fois à `dream.sh:315-317`.
Il en découle une contrainte que l'inventaire ne pouvait pas exprimer tant qu'il comptait des
fichiers :

- ajouter `--project-key` au seul `dream_parser` **répare le rail mort et casse le rail vivant** :
  `codex_dream_parser` reçoit un argument inconnu, `argparse` sort en `2`, `set -euo pipefail`
  propage, et `dream.sh:331` journalise `WARN codex_dream_parser failed for $name (non-fatal)`.
  **La nuit perd ses six lignes par projet, en silence, et la suite de tests reste verte** —
  `codex_dream_parser._build_arg_parser` et `main()` n'ont, mesuré, **aucun test**.
- ajouter le paramètre requis à `_insert_dream_run` sans toucher `codex_dream_parser.py:159`
  produit exactement la même panne, un étage plus bas et sous forme de `TypeError`.

C'est le piège « un paramètre requis casse l'appelant qu'on n'a pas touché », et il tombe sur le
seul chemin qui tourne.

### 15.3 Le partage corrigé : six écrivains, deux clés, trois avalements

| # | site | clé | qui avale l'échec |
|---|---|---|---|
| 1 | `metrics/dream_parser.py:181` (`_insert_dream_run`) | **vraie clé** | rien en Python ; `dream.sh:331`/`:338` selon le rail |
| 2 | `scripts/dream/_promote_helpers.py:125` | **vraie clé** | attrapé dans `main()`, `rc=1` ; `dream.sh:499` le rabaisse en `WARN` |
| 3 | `scripts/ticket_extract.py:752` | `'*'` | sa propre fonction — « Best-effort — never raises » |
| 4 | `scripts/roadmap_curate.py:1146` | `'*'` | idem |
| 5 | `maintenance/session_sweep.py:94` | `'*'` | idem — « la trace ne doit jamais tuer la phase » |
| 6 | `scripts/dream/cross_project_resonance.py:163` | `'*'` | rien en Python ; mort, aucun appelant |

**§14.3 tient et se renforce.** Elle disait « les cinq perdent leur trace en silence » ; ils sont
six, et **aucun ne fait remonter son échec**. Un `NOT NULL` transformerait donc une erreur de
schéma en avertissement imprimé partout. La colonne reste nullable, et ce n'est toujours pas de la
prudence.

> **Correction du 2026-08-09, après revue adverse.** La première écriture de ce paragraphe disait
> « trois avalés dans leur fonction, **trois** avalés par l'orchestrateur ». C'est faux, et faux
> d'une manière que le paragraphe suivant contredisait déjà : la répartition est **3 + 2 + 1**.
> Trois avalent dans leur propre fonction ; **deux** sont avalés par l'orchestrateur — le site
> **partagé** des deux parsers (`WARN … non-fatal`) et `_promote_helpers`
> (`WARN promote — empty-pool dream_runs row NOT recorded`) ; le sixième,
> `cross_project_resonance`, est mort, donc jamais exécuté, donc jamais avalé non plus. Compter
> les deux parsers comme deux sites d'avalement, c'était les recompter comme deux écrivains après
> avoir établi en §15.2 qu'ils n'en font qu'un. **Deuxième fois qu'une élégance repose sur un
> décompte dans cette spec** — la leçon de §14.3 s'appliquait à §15.3 et je ne l'ai pas vue.

Corollaire à écrire noir sur blanc : **quatre écrivains poseront `'*'`, pas trois.** Le docstring
versionné de `042_dream_runs_project_key.py` dit « trois », et `CLAUDE.md` comme
`docs/SCHEMA.md:1032` portent encore « trois des cinq écrivains ». Aucun test n'épingle ces phrases
— leur fausseté serait donc silencieuse et durable, dans les fichiers mêmes qu'on lit pour
comprendre la sémantique de la colonne. Ce lot les corrige.

### 15.4 Ce qui dicte la FORME du code, et qui n'est pas une préférence

Cinq gardes mesurées, dont aucune n'apparaît dans l'inventaire de §14. Elles ne laissent qu'une
seule forme viable, et un lot écrit naïvement les casse mécaniquement.

**(a) La sentinelle ne passe jamais par le validateur central.**
`canonicalize_project_key('*')` lève : `_KEBAB = ^[a-z0-9]+([:-][a-z0-9]+)*$`
(`src/brain_v42/models/project_key.py:23`). Le réflexe « valider la clé avant écriture », légitime
partout ailleurs dans le dépôt, ferait lever les quatre sentinelles — et sur trois d'entre elles
l'exception est avalée par conception, donc **la colonne resterait `NULL` en silence, chaque nuit,
sur les phases globales**.

> **Corrigé en écrivant le code.** Cette clause disait « `'*'` s'écrit en **littéral SQL**, dans la
> requête ». C'était sur-spécifié : ce qui compte est que la valeur soit **non validée**, **hors
> ligne de commande** (b) et **hors signature** (c). Un littéral donne les trois, mais il oblige à
> recopier `'*'` dans quatre chaînes SQL indépendantes — et une coquille dans l'une des quatre
> serait silencieuse, puisque ces écrivains avalent leur échec. La forme livrée est un **paramètre
> lié nommé** dont la valeur vient d'**une seule constante partagée**,
> `brain_v42.dream_run_project_key.GLOBAL_PHASE_PROJECT_KEY`. Elle satisfait les trois exigences et
> supprime la classe de coquille que le littéral rouvrait. Un test épingle que les quatre importent
> bien la même constante, et non une copie.

**(b) Les phases globales ne gagnent aucun flag CLI.** Deux épingles l'interdisent :
`tests/unit/test_dream_sh_sweep.py:57` assène `appended == ["--wet"]` sur les arguments ajoutés au
bloc SWEEP, avec un commentaire qui dit explicitement qu'un flag de plus **doit** faire échouer le
test ; `tests/unit/test_dream_sh_extract.py:33` épingle un littéral exact. Ces deux gardes ne sont
pas des obstacles à contourner : elles prouvent que la sentinelle appartient au code Python de la
phase, pas à la ligne de commande. La forme qu'elles imposent est exactement celle que §14.3 avait
déduite pour d'autres raisons.

**(c) Les signatures de `record_dream_run` ne changent pas.**
`tests/unit/maintenance/test_session_sweep.py:100` appelle la fonction en **positionnel**, et une
vingtaine de sites de test patchent ces writers en `AsyncMock()` sans jamais asserter leurs
arguments. Ajouter un paramètre — même avec défaut — casse un appel réel et ne réveille aucun des
tests aveugles : le pire ratio. La sentinelle entrant en littéral SQL, la signature n'a aucune
raison de bouger. **Le corollaire est que ces quatre sites n'ont, aujourd'hui, aucun témoin de test
qui lise le SQL émis.** Le lot doit en créer un par écrivain — c'est là que se trouve le rouge.

**(d) `project_key` s'insère en `$14`, `phase_dry_run` reste dernier.**
`tests/unit/metrics/test_dream_parser_phase_dry_run.py:98-101` lit le SQL compilé et épingle
`"$14" in sql` **et** `bind_args[-1] is True`. Placer `project_key` en fin de `VALUES` rendrait ce
pin rouge pour une raison sans rapport avec ce qu'il garde — et la tentation immédiate serait de
« corriger le test », ce que le TDD du projet interdit. En l'insérant **avant** `phase_dry_run`,
l'intention du pin (« `phase_dry_run` est lié en dernier, et c'est un vrai booléen ») survit intacte
et se laisse renforcer d'une assertion sur `$15`. Le docstring de ce test, qui dit « `$14` =
`phase_dry_run` », devient faux et se corrige dans le même geste.

**(e) Aucune constante partagée sous `scripts/`.** Le layering mesuré autorise `scripts → src`
(pratiqué une vingtaine de fois) et **interdit** `src → scripts` — non pas par la garde, qui ne voit
que `brain_v42` (`scripts/check_module_layering.py:43`), mais par le `Dockerfile`, qui ne copie
jamais `scripts/`. Un import `src → scripts` serait **vert en local, vert en CI, et casserait
l'image de production à l'import**. Et un nouveau module à la racine du paquet doit n'importer
**rien** d'un sous-paquet : le graphe mesure `_root: []` alors que huit sous-paquets ciblent `_root`
— une seule arête sortante referme huit cycles et fait sortir la garde en `rc=2`, **avant** pytest.
Une constante de sentinelle est donc soit un littéral répété, soit un module racine sans aucun
import.

### 15.5 Le défaut qui vit une couche au-dessus — tranché le 2026-08-09

La décision « `--project-key` requis sans défaut » ne portait, telle qu'écrite, que sur le binaire
Python. `scripts/dream.sh:70` porte déjà :

```bash
PROJECT_KEY="${1:-brain-v42}"
```

Un `bash scripts/dream.sh` nu satisfait donc le flag requis avec `brain-v42` et étiquette toute la
nuit d'un autre projet — **exactement la classe de bug que la décision vise**, une couche plus haut
que là où la garde avait été posée. Le précédent existe déjà en production :
`scripts/dream/post_run_alert.py:157` porte `default=DEFAULT_PROJECT_KEY`, quand
`scripts/dream/promote_prepare.py:131` porte `required=True`. Le dépôt contient les deux formes ; il
faut choisir laquelle est la référence.

**Tranché par l'opérateur : le défaut est retiré, dans un commit séparé.** `dream.sh` exigera son
positionnel. Aucune nuit ne change de comportement — le systemd passe `brain-v42` explicitement
(`ExecStart=… scripts/dream.sh brain-v42`) et les six harnais de test passent déjà `test-project`.
Le commit est distinct de celui des écrivains, donc rétractable seul.

### 15.6 Ce que ce lot ne fait pas, et un inventaire de lecteurs à refaire

- **Il ne touche pas les lecteurs**, et §14.6 reste vraie sur le fond : aucun ne change de résultat
  cette nuit. Mais sa raison est fausse. Un lecteur a **déjà** changé de SQL au merge du lot 2 :
  `sa.select(t)` dans `DreamRunService.last_failure` émet `dream_runs.project_key` depuis que
  `tables.py:1253` déclare la colonne. La donnée est morte parce que personne ne la lit **dans un
  prédicat**, pas parce que personne ne la sélectionne.
- **L'inventaire des lecteurs de §14.2 est sous-compté sur deux axes**, et le lot lecteurs devra
  repartir d'une mesure, pas de cette liste. Manquent trois fichiers entiers :
  `src/brain_v42/services/dream_run_service.py` (**six** `SELECT`, et c'est le briefing de
  `brain_session_start`), `src/brain_v42/services/brain_graph_projection.py:232` (l'allowlist de
  colonnes projetée en nœuds Neo4j), et la vue persistante `codex_dream_run_v1` de la migration 036.
  Mesuré : **au moins dix-huit `SELECT` sur dix sites**, contre « dix sur sept ».
- **Il ne touche aucune des sept gardes de §14.7/§14.8.** Elles sont déjà à `042` et vertes ; les
  « réparer » serait un faux positif sur une bascule déjà consommée. §14.7 décrit l'état *avant*
  bump et est périmée en tant que consigne.
- **Il ne câble pas `cross_project_resonance`.** Il lui donne `'*'` pour qu'il ne soit pas le seul
  incohérent le jour où quelqu'un le rebranche. Le câbler ou le supprimer restent deux décisions
  séparées, non prises.
- **Il n'ouvre aucune boucle.** §2.3 interdit de dériver l'ordonnancement de cette colonne, et le
  tableau de §15.3 renforce l'interdit : **six** écrivains peuvent ne rien écrire sans que personne
  ne le sache.

### 15.7 Ce que cette passe n'a pas pu faire

- **Aucune nuit n'a été observée avec la colonne remplie.** Tout ce qui précède est de la lecture de
  code, de la mesure en base sur des lignes écrites *avant* le lot, et une exécution de la suite de
  tests. La preuve qu'un écrivain pose bien sa clé viendra de la nuit du lendemain du merge, pas
  d'ici.
- **Le compte de lignes bouge chaque nuit** : §14.1 dit 864, la mesure du 2026-08-09 dit 872. Aucun
  test de ce lot ne doit épingler un décompte de lignes — il serait rouge le lendemain matin, à
  06:00, sans que personne n'ait rien changé.

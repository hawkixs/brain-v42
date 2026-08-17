# Pool de projets du dream nocturne — sortir du mono-projet

**Date** : 2026-08-08
**Statut** : design ; six questions tranchées, deux points laissés ouverts et nommés ;
**passé en revue adversariale le 2026-08-08 — chiffres rejoués, corrections en §19**
**Périmètre** : `brain_v42` seul — `scripts/dream.sh`, les phases, `dream_runs` et ses lecteurs
**Worktree** : `.claude/worktrees/dream-pool`, branche `feat/dream-project-pool`, base `b55b7590`
**Migration mesurée en production au moment de l'écriture** : `041`
(`docker exec brain_v42_postgres psql -U brain -d brain -Atc "select version_num from alembic_version;"`)
**Aucun code d'implémentation n'accompagne cette spec.** `scripts/dream.sh` est exécuté
depuis le working tree de la **racine** à 06:00 ; une boucle à moitié écrite tournerait
cette nuit sur huit projets.

---

## 1. Ce qui est décidé en amont

L'opérateur a fixé le pool. Ce n'est pas une question ouverte de cette spec :

```
brain-v42   red   auto-discord   red-lab   red-shrik   red-writer   red-watcher   red-monitor
```

Huit **clés parentes**, exactement. Masse mesurée (`learnings ∪ decisions ∪ snippets ∪
runbooks ∪ adrs`, `GROUP BY project_key`) :

| clé | artefacts |
|---|---|
| red | 859 |
| brain-v42 | 658 |
| red-writer | 252 |
| red-shrik | 222 |
| red-lab | 161 |
| auto-discord | 113 |
| red-monitor | 77 |
| red-watcher | 31 |
| **pool** | **2 373** |

Le corpus complet fait **3 803** artefacts, répartis sur 54 `project_contexts`. **26 de ces
artefacts** portent `project_key IS NULL` ; aucun `project_context` n'a de clé nulle (mesuré :
`SELECT count(*) FROM project_contexts WHERE project_key IS NULL` → 0). Le pool couvre
**62,4 %** du corpus (2 373 / 3 803).

`red` est plus gros que `brain-v42`. C'est le fait qui a motivé le pool, et §4 montre
pourquoi il ne change **rien** au coût d'une nuit tant que le scope serveur reste éteint.

---

## 2. L'exclusion des sous-projets, et son coût

Les six clés à deux niveaux sont **volontairement hors du premier lot**. Coût mesuré :

| clé exclue | artefacts |
|---|---|
| red-shrik:agent | **280** |
| red-lab:architect | 113 |
| red-lab:orchestrator | 64 |
| red-lab:reviewer | 15 |
| red-lab:sentinel | 5 |
| red-lab:developer | 2 |
| **total exclu** | **479** |

**`red-shrik:agent` (280) est plus gros que `red-shrik` lui-même (222).** Le sous-projet
exclu pèse plus que le parent inclus. C'est la forme la plus nette de la dette : le pool
ne consolidera pas la moitié lourde de `red-shrik`.

479 artefacts = **20,2 % de la masse du pool**, hors consolidation pour une durée non
décidée.

Il n'existe **aucun** filtre par préfixe dans le pipeline. `scripts/dream/promote_prepare.py`
filtre par égalité exacte :

```sql
AND l.project_key = :pk
```

Donc `red-lab` ne verra jamais `red-lab:architect`, et aucune fermeture de cette dette ne
se fera « toute seule » : il faudra soit ajouter les six clés au pool — six runs de plus,
+6 × 53 min de plafond configuré (§10) — soit introduire une sémantique de préfixe qui
n'existe nulle part dans le code aujourd'hui. Les deux sont des chantiers, pas des
réglages.

**Dette assumée, pas oubli.** Elle doit être portée par un ticket au moment du merge, pas
par cette phrase.

---

## 3. Ce qui casse avant même les six questions

Cinq couplages ne sont pas des arbitrages : ils rendent la boucle **fausse** si on ne les
traite pas. Ils précèdent toute discussion.

**3.1 — Le verrou est global.** `scripts/dream.sh:351` :

```bash
LOCK_FILE="${XDG_RUNTIME_DIR:-/tmp}/brain-v42-dream.lock"
```

`flock -n 9` sans composante de projet. La topologie « huit unités systemd » est donc
**structurellement impossible** : les sept invocations suivantes sortiraient en `exit 0`
avec « dream cycle already running, skipping ». Une nuit verte où sept projets sur huit
n'ont rien fait. C'est le pire mode de panne du lot — silencieux et vert. **Le pool est
une boucle dans un run, pas huit runs.**

**3.2 — Tous les chemins de journal sont datés, aucun n'est projeté.**
`scripts/dream.sh:74` : `TIMESTAMP=$(date +%Y-%m-%d)`. Douze gabarits de chemin en
dépendent, et `scripts/dream/codex_runner.py:419` fait `report_log.write_text("")` tandis
que `:432-433` ouvrent `events` et `stderr` en `"w"` — **troncature**, pas append. Au
matin, seuls les journaux du dernier projet survivent.

**Mais la projection ne s'applique pas aux douze.** Le tri est décidé par §7 et §11, pas
par ce paragraphe. Sur les douze gabarits recensés (`grep -n 'LOG_DIR/' scripts/dream.sh`) :

| gabarit | projeté ? | pourquoi |
|---|---|---|
| `${TIMESTAMP}_${name}.log` (rapport), `.raw.log`, `.otel.log`, `.events.jsonl`, `.stderr.log`, `.err.log` | **oui** | tronqués par `codex_runner`, et le rapport nourrit `PHASE_DEPS` (§3.3) |
| `${TIMESTAMP}_promote_candidates.json` | **oui** | ce n'est pas un journal mais une **entrée de validateur** (`promote_validate --candidates-json`) ; sans projection, aucune reprise post-mortem par projet n'est possible |
| `${TIMESTAMP}_promote.log` (rapport de synthèse « pool vide », `:481`) | **oui** | même gabarit que le rapport de phase, écrit en `>` |
| `${TIMESTAMP}_extract.log`, `_roadmap.log`, `_sweep.log` | **non** | phases globales, une seule exécution par nuit (§7) — les projeter fabriquerait sept fichiers vides |
| `$TIMESTAMP.log` (journal principal) | **non** | ouvert en `tee -a` / `>>`, c'est le récit unique de la nuit et la cible de redirection de l'alerte agrégée unique (§11). Le fragmenter en huit contredirait Q6. |

Sept gabarits gagnent une composante de projet, cinq n'en gagnent pas. Une consigne
uniforme « chaque chemin » produirait du code faux dans les deux sens.

**3.3 — L'ordre de boucle est une décision de correction.** `scripts/dream.sh:205-226`
injecte le rapport de la phase précédente **en relisant un fichier** :
`dep_log="$LOG_DIR/${TIMESTAMP}_${dep}.log"`, table `PHASE_DEPS` à `:116-123`. En ordre
phase-majeur (tous les `scan`, puis tous les `clean`…), CONNECT de `red` recevrait le
rapport CLEAN de `red-writer`. **La boucle doit être projet-majeur** : un projet, ses six
phases, puis le suivant.

**3.4 — Deux variables exportées survivent aux itérations.** `scripts/dream.sh:504-510`
exporte `PROMOTE_CANDIDATE_POOL_JSON` et `PROMOTE_RECENT_PROMOTIONS_JSON`, relues sans
réinitialisation à `:196-197`. Si le projet N saute PROMOTE (killswitch, pool vide, échec
de `promote_prepare`), la valeur du projet N−1 reste chargée. **Remise à `[]` en tête de
chaque itération de projet**, sinon un projet promeut sur le pool d'un autre.

**3.5 — Deux prompts portent `brain-v42` en dur, et c'est le seul défaut irréversible.**

```
scripts/dream/phase_synth.md:24   project_key="brain-v42",
scripts/dream/phase_synth.md:58   - Always use `project_key="brain-v42"` … for insights.
scripts/dream/phase_promote.md:4  Project scope: {{PROJECT_KEY}} (v1 is brain-v42 only — …)
scripts/dream/phase_connect.md:43 memory — knowledge graph, brain-v42, embeddings, …
```

SYNTH tourné pour `red` créerait des insights et des snippets étiquetés `brain-v42`. Aux
plafonds du prompt (`phase_synth.md:55-56`) et à huit projets : **jusqu'à 24 insights et
24 snippets par nuit, mal attribués**. Aucune migration ne répare ça — c'est du contenu
écrit dans le mauvais projet. **Ces quatre lignes tombent avant la boucle, pas avec elle.**

Et `scripts/dream/promote_validate.py:67-179` ne vérifie **pas** `project_key` : il
contrôle `target_type`, l'identité du candidat, `adrs.status == 'accepted'` et le compte de
lignes. La promesse de `phase_promote.md:122` (« `project_key` … MUST equal
`{{PROJECT_KEY}}` ») est de la prose seule. À huit projets, une ADR matérialisée sur le
mauvais projet passe le gate.

---

## 4. Le mur d'horloge — méthode d'abord, chiffres ensuite

### 4.1 L'hypothèse qui pilote tout, et qui est mesurée

Dans **cinq** des six prompts de phase, `{{PROJECT_KEY}}` n'apparaît que dans l'en-tête
`## Mode`, en prose. Aucun appel d'outil ne le reçoit, et les signatures serveur ne
l'exposent pas : `brain_decay_status()` (`decay_tools.py:56`) n'a aucun paramètre,
`brain_consolidation_candidates`, `brain_backfill_links_batch`,
`brain_list_orphans_for_classification` et `brain_get_clusters` non plus.

**Conséquence : SCAN, CLEAN, CONNECT, SYNTH et REORG exécutés huit fois font huit fois le
même travail sur le même corpus global.** Le coût d'un run est piloté par des plafonds
d'appels constants, pas par la taille du projet. Que `red` (859) soit plus gros que
`brain-v42` (658) ne change rien.

C'est ce qui autorise la méthode ci-dessous. **Cette hypothèse tombe le jour où le scope
serveur est armé** (§14) — il faudra alors tout re-mesurer.

### 4.2 Borne (a) — simulation sur les nuits réelles, pas sur des moyennes

Méthode : décomposer chacune des 30 dernières nuits en un sous-total « phases agent »
(scan, clean, connect, synth, promote, reorg) et un sous-total « phases globales »
(extract, roadmap, sweep), puis évaluer `8 × agent + global` **nuit par nuit**, et enfin
prendre la moyenne, le p90 et le max de cette série. Multiplier des moyennes aurait perdu
la queue de distribution, qui est précisément ce qui menace la fenêtre de 06:00.

Requête réellement exécutée :

```sql
WITH n AS (
  SELECT run_date,
         sum(duration_s) FILTER (WHERE phase IN ('scan','clean','connect','synth','promote','reorg')) AS agent_s,
         sum(duration_s) FILTER (WHERE phase IN ('extract','roadmap','sweep'))                        AS global_s
  FROM dream_runs
  WHERE run_date >= (SELECT max(run_date) FROM dream_runs) - 29
  GROUP BY run_date)
SELECT ... avg / percentile_cont(0.9) / max ... FROM n;
```

| topologie | moyenne | p90 | max |
|---|---|---|---|
| aujourd'hui (1 projet) | **15,05 min** | 21,22 | **72,76 min** |
| 8 × agent + 1 × global | **80,0 min** | **126,6 min** | **286,3 min** (4,8 h) |
| 8 × tout (naïf) | 120,4 min | — | **582,1 min** (9,7 h) |

Décomposition par phase, secondes consommées par nuit sur ces 30 nuits (re-runs inclus) :

| phase | s/nuit | nature |
|---|---|---|
| roadmap | 259,9 | **globale** |
| synth | 210,3 | dans la boucle |
| reorg | 187,6 | dans la boucle |
| extract | 86,1 | **globale** |
| clean | 62,6 | dans la boucle |
| scan | 42,8 | dans la boucle |
| connect | 28,0 | dans la boucle |
| promote | 25,8 | dans la boucle |
| | sous-total agent **557,1 s** · global **346,0 s** | total 903,1 s = 15,05 min |

**Ce que la borne (a) suppose, et qu'elle ne mesure pas.** Le `×8` porte sur des **runs**, pas
sur des artefacts : aucune extrapolation linéaire sur la masse d'un projet n'est faite ni
justifiable ici, et c'est §4.1 qui l'interdit. Reste un biais dont le **signe est inconnu** :
avec le scope éteint, les runs 2 à 8 travaillent sur un corpus que le run 1 vient de muter.
SYNTH le gonfle (ce qui pousse vers le haut), tandis que CLEAN, CONNECT et REORG épuisent
leur propre file de travail au premier passage (ce qui pousse vers le bas — un agent qui ne
trouve plus rien à faire rend la main plus vite). Ni l'un ni l'autre n'est mesuré : aucune
nuit à deux projets n'a jamais tourné. Biais secondaire, mesuré et négligeable : le pre-flight
a coupé les phases profondes 2 nuits sur 121 (dont une dans la fenêtre de 30), et il ne le
fera plus une fois huit SYNTH en marche (§16).

### 4.3 Borne (b) — le plafond configuré, qui est ce que systemd tue

`PHASES` (`scripts/dream.sh:107-114`) : `scan:5 clean:5 connect:8 synth:15 promote:10
reorg:10` = **53 min**. Le retry (`:539`) est unique, sur échec dur seulement, et exclut
`promote` : **+43 min** éligibles → **96 min par projet**.
Globales : `timeout 10m` extract (`:664`), `timeout 20m` roadmap (`:706`), `timeout 5m`
sweep (`:735`) = **35 min**.

| scénario | plafond | vs `TimeoutStartSec=10800` (180 min) |
|---|---|---|
| aujourd'hui | 131 min | 49 min de marge |
| **2 projets** | **227 min** | **déjà dépassé** |
| 8 projets, sans retry | 459 min (7,7 h) | ×2,6 |
| 8 projets, avec retry | 803 min (13,4 h) | ×4,5 |

**Le plafond systemd actuel ne tient pas deux projets.** L'unité est `Type=oneshot`, le timer
`OnCalendar=*-*-* 06:00:00` avec `Persistent=true` (lus dans les fichiers vivants). Un
déclenchement dont l'unité est encore active ne démarre pas de seconde instance : il est
perdu. Précision honnête, contre la version antérieure de ce paragraphe : **ce scénario n'est
pas atteignable avec ces chiffres** — le pire cas configuré est 803 min (13,4 h), et
`TimeoutStartSec` tue de toute façon bien avant. Le risque réel n'est pas le débordement de
24 h, c'est **systemd qui tue la nuit au milieu d'un projet**, laissant les projets suivants
sans aucune ligne dans `dream_runs` — invisible pour un lecteur `DISTINCT ON (phase)` qui
verra les phases des projets déjà traités.

### 4.4 Tokens et coût

Mesurés sur les mêmes 30 nuits, avec un piège qu'il faut nommer : `cache_creation_tokens`
est `NULL` sur **167 lignes sur 272**. La somme naïve
`sum(input+output+cache_read+cache_creation)` propage le `NULL` et **perd 61 % des
lignes** — elle donne 190 239 tokens/nuit. Avec `COALESCE(...,0)` :

| mesure | valeur |
|---|---|
| tokens/nuit, moyenne | **1 455 688** |
| tokens/nuit, max | **3 850 807** |
| `cost_usd`/nuit, moyenne | **0,6425 $** |
| `cost_usd`/nuit, max | 4,6846 $ |

À huit projets : ~11,6 M tokens/nuit en moyenne, ~30,8 M sur la pire nuit mesurée. Le
coût dollar est nul depuis la bascule vers l'authentification par abonnement — **le
plafond de débit de cet abonnement est le vrai risque, et il n'est pas mesurable depuis
ici** (§17).

---

## 5. Q1 — Faut-il une migration 042 ?

### Décision : oui. `dream_runs.project_key VARCHAR(64) NULL`, sans backfill, avec un index `(run_date DESC, project_key)`. Les phases globales y écrivent le sentinelle `'*'`.

**Il n'existe pas d'alternative sans DDL.** Mesuré :

```
phase | character varying(10) | not null
```

Une valeur composite du type `synth@red-writer` fait 16 caractères et **ne rentre pas**.
Et il n'existe aucun index utilisable pour un filtre par projet : `dream_runs_pkey(id)` et
`idx_dream_runs_date(run_date DESC)`, rien d'autre.

**Sans colonne, dix lecteurs mentent, et trois d'entre eux écrivent.** Ce n'est pas un
problème d'affichage :

| lecteur | fichier | ce qui se passe à huit |
|---|---|---|
| `_dream_run_id` | `scripts/dream/_promote_helpers.py:86-101` | `ORDER BY id DESC LIMIT 1` → renvoie le **dernier projet** ; ce `dream_run_id` sert ensuite à marquer `partial` (`promote_validate.py:181-196`) et à **backfiller `dream_promotions.dream_run_id`** (`:122-127`) |
| `_mark_latest_connect_partial` | `scripts/dream/connect_validate.py:91-115` | idem sur CONNECT |
| `REORG_RUN_ID` | `scripts/dream.sh:601-622` | idem sur REORG |
| payload `/dream` | `metrics/collector_dream.py:81-95` | `DISTINCT ON (phase)` → **une** ligne par phase : un `synth` en échec sur `red` est **effacé** par un `synth` réussi sur `red-writer` |
| historique 10 j | `metrics/collector_dream.py:155-182` | même effacement ; les `SUM(cost_usd)` restent justes, le `day_status` ment |
| `last_failure` | `services/dream_run_service.py:218-237` | le briefing de `brain_session_start(project_key="brain-v42")` affichera l'échec de `red-watcher` |
| `killswitch_state` / `_clean_dry_streak` | `services/dream_run_service.py:76-188` | voir plus bas |
| `fetch_failed_runs` | `scripts/dream/post_run_alert.py:90-124` | filtre `run_date` seul (voir Q6) |
| `last_failure` sidecar | `metrics/collector_nightly.py:78-86` | second exemplaire du même défaut |
| `expected_dream_phases` | `metrics/collector_dream.py:27-48` | voir Q2 |

Les trois premiers **écrivent une attribution fausse et durable** dans l'audit des
promotions. C'est ce qui rend l'ordre de livraison non négociable (§12).

### Nullable, jamais `NOT NULL`

Cinq sites d'INSERT écrivent dans `dream_runs`, et **trois le font en SQL textuel avalé
best-effort** :

```
src/brain_v42/metrics/dream_parser.py:181       INSERT INTO dream_runs (…14 colonnes…)   ← asyncpg brut, partagé avec codex_dream_parser.py:159
scripts/ticket_extract.py:752                   INSERT INTO dream_runs …   except Exception: print(f"! warning: …")
scripts/roadmap_curate.py:1146                  INSERT INTO dream_runs …
src/brain_v42/maintenance/session_sweep.py:94   INSERT INTO dream_runs …
scripts/dream/_promote_helpers.py:125           sa.insert(dream_runs).values(…)
```

(un sixième, `scripts/dream/cross_project_resonance.py:163`, n'est câblé nulle part.)

Une colonne `NOT NULL` sans défaut y produirait une **perte de télémétrie invisible** :
l'INSERT échoue, l'exception est imprimée en warning, la nuit reste verte et la ligne
n'existe pas. La colonne est nullable, et c'est le code qui la remplit.

### Le backfill, confronté à la doctrine du dépôt

La doctrine est explicite : *aucun backfill, `NULL` veut dire « jamais mesuré »* (040 pour
`focus_updated_at`, 041 pour ses trois colonnes). Il faut la confronter, pas l'appliquer
par réflexe.

**L'argument POUR le backfill à `'brain-v42'` est solide.** Les 856 lignes ont été
produites par un process dont l'argument de projet est épinglé en dur à trois endroits :
`ExecStart=… dream.sh brain-v42` dans l'unité **vivante** (`~/.config/systemd/user/
brain-v42-dream.service:28`, lu en production), dans le template versionné
(`deploy/systemd/brain-v42-dream.service.tmpl:31`), et gardé par un test d'intégration
(`tests/integration/test_dream_systemd_install.sh:180`). Ce n'est pas une inférence sur un
événement non observé — c'est une constante de configuration.

**L'argument CONTRE tient quand même, et il gagne.** Deux raisons :

1. `dream.sh` accepte un argument positionnel (`:70`). Une invocation manuelle avec une
   autre clé est possible et **indistinguable a posteriori**. Le backfill affirmerait 856
   fois quelque chose qu'on ne peut pas vérifier ligne à ligne. La doctrine existe
   exactement pour ce cas.
2. **Le coût du `NULL` a été mesuré, et il est nul.** L'objection sérieuse était :
   « `_clean_dry_streak` compte les nuits DRY propres, et c'est l'argument opérationnel
   pour basculer un killswitch en WET ; filtrer par projet remettrait tous les compteurs à
   zéro ». Mesure des cinq streaks :

   | phase | `reset_date` | nuits DRY propres |
   |---|---|---|
   | roadmap | 2026-07-14 | **24** |
   | promote | 2026-08-06 | 0 |
   | reorg | 2026-08-07 | 0 |
   | extract | 2026-08-07 | 0 |
   | sweep | — (aucune ligne) | — |

   **Le seul compteur qui vaut quelque chose aujourd'hui appartient à `roadmap`** — une
   phase globale, qui reste hors de la boucle (Q3) et garde donc un écrivain unique par
   nuit. `promote` et `reorg`, les deux seules phases de la boucle à streak, valent déjà
   0. Le `NULL` ne détruit aucune histoire utile.

`NULL` = « avant le pool ». Les lecteurs doivent l'afficher comme un troisième état, pas
le faire disparaître d'un `WHERE project_key = :pk`.

### Le sentinelle `'*'` pour les phases globales

Pour extract, roadmap et sweep, le projet n'est pas « non mesuré » : il est **non
applicable**. Ce sont deux choses différentes et les confondre relancerait la même
ambiguïté que le backfill. Ces trois phases écrivent `'*'`. `dream_runs` n'a aucune clé
étrangère vers `project_contexts`, donc rien n'interdit une valeur hors domaine ; c'est une
colonne de télémétrie, pas une référence.

### Ce qu'on perd

- Les 856 lignes historiques sortent de toute vue par projet. Les comparaisons
  « avant/après pool » demanderont un `IS NULL` explicite, jamais un `= 'brain-v42'`.
- Un index de plus sur une table qui prend ~7 lignes/nuit aujourd'hui et en prendra 51 —
  coût négligeable, mais c'est une migration de plus dans une chaîne qui en compte déjà 41.
- Le sentinelle `'*'` est une valeur qu'aucune validation de clé de projet n'accepterait.
  Tout code qui réutiliserait cette colonne comme une clé de projet valide se tromperait.
  À écrire dans le commentaire de colonne, pas seulement ici.

---

## 6. Q2 — Faut-il un gating des killswitches par projet ?

### Décision : non. Les six killswitches restent globaux. Le gating par projet est **l'appartenance au pool**, exprimée par une seule variable dans le même drop-in.

```
Environment=BRAIN_DREAM_PROJECT_POOL=brain-v42
```

Défaut dans `dream.sh` (idiome identique à `BRAIN_DREAM_SWEEP_*` à `:58-59`) : `brain-v42`
seul.

### Deux pièges de transport, à traiter avec la variable elle-même

**« Ajouter un mot à une ligne » ne marche pas.** `Environment=` de systemd découpe sur les
blancs NON protégés et traite chaque morceau comme une affectation distincte :
`Environment=BRAIN_DREAM_PROJECT_POOL=brain-v42 red` met la variable à `brain-v42` et jette
`red` (morceau sans `=`). **Le pool rétrécirait à un projet, en silence, sans erreur au
démarrage** — exactement le mode de panne verte que §3.1 refuse. Deux sorties, à trancher à
l'implémentation, jamais à laisser implicite :

- séparateur **virgule** (`…=brain-v42,red,red-lab`) — aucune protection nécessaire ;
- ou blancs **explicitement protégés** (`Environment="BRAIN_DREAM_PROJECT_POOL=brain-v42 red"`).

Un test doit épingler le format retenu contre le drop-in réel, sinon la première ouverture de
pool sera une nuit verte à un projet.

**Le parseur partagé ne sait pas lire cette variable.** `parse_killswitches`
(`src/brain_v42/dream_killswitches.py:26-37`) rend un `dict[str, bool]` : il ignore toute clé
absente de `_KS_KEYS` et coerce la valeur par `value.strip('"').lower() == "true"`. Une clé à
valeur de liste n'y entre pas. Or §6 exige plus bas que `expected_dream_phases` lise le pool —
donc ce lot **touche quand même le parseur partagé** (nouvelle clé dans `_KS_KEYS` ou, plus
propre, une seconde fonction qui rend les valeurs brutes). L'argument 2 ci-dessous facture ce
coût à la matrice rejetée ; il faut reconnaître qu'il n'est pas nul du côté retenu non plus.
Il reste très inférieur : **une** clé de liste contre **48** lignes booléennes.

Trois raisons mesurées :

1. **Trois des six killswitches n'ont rien à gater par projet.** EXTRACT, ROADMAP et SWEEP
   pilotent des phases **globales** qui sortent de la boucle (Q3) : « armer EXTRACT pour
   `red` seulement » n'a pas de sens, la file de tickets est globale
   (`scripts/ticket_extract.py:471-486`, aucun filtre de projet). Il ne reste donc que
   PROMOTE et REORG, soit **deux** familles, pas six.
2. **8 × 6 = 48 lignes `Environment=` seraient à maintenir à la main** dans un fichier qui
   porte déjà quatre paragraphes de commentaires d'incident. Le format
   `Environment=KEY=value` n'a aucune dimension de projet, et `_KS_KEYS`
   (`src/brain_v42/dream_killswitches.py:12-22`) est un dictionnaire plat partagé par
   **trois** lecteurs applicatifs : `dream_run_service._read_killswitch_flags` (briefing de
   session), `collector_nightly.collect_nightly_ops` (payload `/metrics`) et
   `collector_dream.expected_dream_phases`. Une matrice obligerait à toucher les trois, ou
   le briefing et `/metrics` mentiraient sur l'état armé.
3. Le drop-in vivant, lu ce jour, montre le régime réel : `PROMOTE=true`, `REORG=true`,
   `REORG_DRY_RUN=false`, `EXTRACT=true`, `EXTRACT_DRY_RUN=false`, `ROADMAP=true`,
   `ROADMAP_DRY_RUN=true`, `SWEEP_*` **absentes** (donc `false`/`true` par défaut). Ce sont
   des décisions de cadence, prises phase par phase après soak — pas des décisions de
   projet.

### La contrainte non négociable qui vient avec

`expected_dream_phases()` (`metrics/collector_dream.py:27-48`) transforme « phase armée »
en « alarme si absente de `dream_runs` ». C'est le mécanisme anti-crash-silencieux du
2026-05-02, et il est consommé par `post_run_alert.include_missing_expected_phases:69-87`
et `collector_dream:138-150`.

**À huit projets, il se désarme tout seul** : si un seul projet saute `promote`, la phase
reste « observée » globalement grâce aux sept autres, et l'alarme ne sonne plus.

Donc l'attendu doit devenir un **produit cartésien** `{phase} × {projet du pool}` pour les
phases de la boucle, et rester un singleton pour les globales. Cette transformation lit la
nouvelle variable de pool. **Elle atterrit avec la boucle, jamais après.**

### Ce qu'on perd

- Impossible de dire « REORG en WET sur `brain-v42` mais en DRY sur `red` ». Le seul levier
  est binaire : le projet est dans le pool, ou il n'y est pas. Un projet qu'on voudrait
  scanner sans le réorganiser n'a aucune expression.
- Le premier soak d'un projet neuf se fait donc **au régime WET déjà en vigueur** pour
  REORG et EXTRACT. C'est la vraie raison pour laquelle l'ouverture du pool se fait une clé
  à la fois (§12), et pas d'un bloc.

---

## 7. Q3 — Où sortir `sweep` de la boucle ?

### Décision : les **trois** phases globales sortent de la boucle, pas seulement `sweep`. Elles tournent une fois, après la boucle de projets, et écrivent `project_key='*'`.

La question posée ne nommait que `sweep`. La mesure dit qu'elles sont trois, et que les
deux autres coûtent plus cher.

**`sweep` — globale par construction.** `src/brain_v42/maintenance/session_sweep.py:39-57` :
le parser expose `--wet` et `--older-than-days`, **rien d'autre**.
`PgBrainSessionRepo.abandon_stale` filtre sur `status == 'open'` et le heartbeat, sans
projet. À huit exécutions : la première abandonne, les sept suivantes ne trouvent rien et
écrivent sept lignes `done` supplémentaires — qui gonfleraient `_clean_dry_streak` de sept
« nuits » fictives par nuit réelle.

**`extract` — globale par construction.** `scripts/ticket_extract.py:471-486` :
`WHERE extraction_status = 'pending' ORDER BY closed_at ASC LIMIT :limit`, aucun filtre de
projet. La première exécution vide la file jusqu'à `--limit 20`, les sept suivantes tournent
à vide **en consommant quand même leur `--run-budget-seconds 540`**.

**`roadmap` — déjà multi-projets, avec sa propre rotation.**
`scripts/roadmap_curate.py:437-441` sélectionne `DISTINCT project_key FROM features
WHERE status NOT IN ('done','archived') AND merged_into IS NULL`, et
`rotate_keys(keys, limit, day_ordinal)` (`:474-487`) fait une fenêtre glissante de `--limit`
**projets** par nuit. `--limit 10` (`dream.sh:698`) veut donc dire **10 projets**, pas 10
features.

Le domaine de rotation n'est **pas** les 54 `project_contexts` : c'est cette requête, mesurée
à **30 projets** au moment de l'écriture (dont six clés à deux niveaux et plusieurs projets
hors pool). La couverture complète se fait donc en ⌈30/10⌉ = **3 nuits**, pas 6. Ce domaine
est dynamique — il rétrécit quand des features passent `done`/`archived` — donc il se
remesure, il ne se recopie pas. Comme
`day_ordinal = date.today().toordinal()` est identique pour les huit invocations, la **même
fenêtre serait curée huit fois**, avec huit fois le budget API. C'est la phase la plus
coûteuse de la nuit (259,9 s/nuit mesurés) et celle qui a le moins besoin de la boucle.

**Garantie structurelle, pas convention.** Les trois appels doivent être **hors du corps**
de la boucle projet, et un test doit l'épingler textuellement — l'idiome existe déjà
(`tests/unit/test_dream_sh_agent_provider.py:74` assure `'--project-key "$PROJECT_KEY"' in
content`). Une convention se perd au premier refactor ; une ancre textuelle échoue
bruyamment.

### Ce qu'on perd

- La portée de `sweep` reste **globale**, donc différente du pool. Elle continuera
  d'abandonner des sessions de projets hors pool. C'est voulu (§8), mais cela crée deux
  ensembles qu'il ne faut jamais confondre : « le pool » et « ce que `sweep` touche ». À
  écrire dans le CLAUDE.md au moment du rollout.
- `roadmap` conserve sa propre rotation sur **30** projets (mesuré), indépendante du pool de
  8 et ne le contenant pas. Deux notions de « projets traités cette nuit » coexistent dans le
  même run. Personne ne doit lire la fenêtre roadmap comme une couverture du pool.

---

## 8. `sweep` — la seule phase irréversible

Elle mérite sa section, pour deux propriétés qu'aucune autre phase ne partage.

**C'est la seule phase irréversible.** Elle fait passer des sessions en `abandoned`.
`brain_session_resume` exige `status='open'` : l'abandon est terminal, et il n'existe pas
d'`unabandon` (décision explicite de la spec du 2026-08-07 : ne pas construire une
annulation pour un problème non observé). Les cinq autres phases de la nuit proposent, ou
écrivent des artefacts qu'un REORG peut reprendre. `sweep` ferme.

**C'est la seule phase qui agirait sur des projets où le dream n'a jamais rien
consolidé.** Mesure des sessions ouvertes et des fantômes à 7 jours :

| project_key | ouvertes | fantômes 7 j | dans le pool ? |
|---|---|---|---|
| red-lab | 2 | **2** | oui |
| red-watcher | 1 | **1** | oui |
| claude-dev-pc | 1 | **1** | **non** |
| red-codex | 1 | **1** | **non** |
| red-story | 1 | **1** | **non** |
| red-viewer | 1 | **1** | **non** |
| brain-v42 | 1 | 0 | oui |
| red-monitor | 1 | 0 | oui |
| red-arena, datalake-v1 | 1 + 1 | 0 | non |

**7 fantômes éligibles : 3 dans le pool, 4 hors pool. Zéro pour `brain-v42`.**

C'est l'argument décisif de §7 pris à l'envers : si `sweep` devenait per-project et bouclait
sur le pool, **4 fantômes sur 7 (57 %) deviendraient inatteignables**. La phase qui a le
moins besoin du pool est aussi celle que le pool abîmerait le plus.

Trois conséquences pour la conception :

1. `sweep` reste hors boucle, portée globale, `project_key='*'` dans `dream_runs`. Aucune
   variante « pool » n'est proposée, même en option.
2. Elle reste **fermée et dry** (`BRAIN_DREAM_SWEEP_*` absentes du drop-in, donc
   `false`/`true`). Elle n'a **jamais écrit une seule ligne** dans `dream_runs` : la table
   compte 856 lignes sur 8 phases, et `sweep` n'y figure pas. Son soak DRY est encore
   devant elle, et le pool ne doit surtout pas le devancer.
3. L'ouverture de `sweep` et l'ouverture du pool sont **deux décisions indépendantes**, à
   ne jamais livrer dans le même lot. Une phase irréversible ne s'arme pas la même nuit
   qu'un changement de topologie.

---

## 9. Q4 — Isolation des échecs : le projet 3 échoue, les projets 4 à 8 tournent-ils ?

### Décision : oui, isolation complète. Aucun quorum, aucun seuil. Les compteurs cessent d'être des tableaux plats de **noms de phase** et deviennent des paires `projet/phase`. La porte de sortie garde ses trois conditions, mot pour mot.

Le code réel de la porte, `scripts/dream.sh:801-811`, retravaillé par `b3c3e0fd` et
`f47427d0` :

```bash
if (( ${#FAILED_PHASES[@]} > 0 )) \
  || (( ${#TIMED_OUT_PHASES[@]} > ${#CONTROLLED_TIMEOUT_PHASES[@]} )) \
  || (( alert_rc != 0 )); then
  exit 1
fi
```

Son commentaire dit pourquoi elle est **structurelle et pas arithmétique** : la forme
d'origine soustrayait `${#CONTROLLED_TIMEOUT_PHASES[@]}` du total, et une phase inscrite par
erreur dans `FAILED` **et** `CONTROLLED` effaçait son propre échec — le script sortait en 0
après avoir imprimé « 1 failed (synth) ».

**Ce que huit projets en font.** `TOTAL_PHASES` passe de 9 à **51** (8 × 6 + extract +
roadmap + sweep). Le résumé de `:766-776` imprimerait `3 failed (synth synth synth)` :
**trois projets indiscernables**. Les tableaux sont des multi-ensembles de noms sans
étiquette.

Correctif : `FAILED_PHASES+=("$project/$name")`, idem pour `TIMED_OUT_PHASES`,
`CONTROLLED_TIMEOUT_PHASES` et `SKIPPED_PHASES`. Le résumé redevient lisible sans changer
une seule condition de la garde.

**Pourquoi aucun seuil.** Un quorum « 7/8 projets OK = vert » serait exactement le défaut
que `2026-08-07` a corrigé, retourné : la garde d'origine rougissait toutes les nuits et
était devenue muette ; un seuil rendrait la garde verte trop souvent et la rendrait muette
à l'autre bout. Tant qu'aucune nuit à huit projets n'a été observée, il n'y a **aucune
mesure** pour calibrer un seuil. On garde la forme fail-closed.

**Mais le taux de nuit rouge, lui, se mesure — et il faut le porter ici.** Fréquence
d'apparition d'au moins une ligne agent non-`done` dans `dream_runs` :

| fenêtre | nuits rouges (côté agent) | taux |
|---|---|---|
| 121 nuits (tout l'historique) | 20 | **16,5 %** |
| 30 dernières nuits | 2 | **6,7 %** |

Sous hypothèse d'indépendance entre projets — hypothèse fausse dans le sens conservateur, une
panne Codex frappant les huit à la fois — une nuit à huit projets rougit avec probabilité
`1 − (1 − p)⁸`, soit **42 % au taux récent et 77 % au taux historique**, contre 6,7 % et
16,5 % aujourd'hui.

Ce chiffre ne renverse pas la décision : la sortie fail-closed reste la bonne forme, et le
2026-08-07 a montré qu'un compteur arithmétique masque des échecs réels. Il change ce qu'on
promet à l'exploitation. **Une unité rouge une nuit sur deux redevient un état sans
information** — le même mur que `b3c3e0fd` a démoli, atteint par l'autre côté. Deux
conséquences à assumer explicitement plutôt qu'à découvrir :

1. Le signal utile n'est plus le code de sortie de l'unité mais le **contenu** du rapport
   agrégé par projet (§11). C'est lui qu'il faut rendre lisible en priorité, pas la couleur.
2. Le seuil ne se rediscute qu'**après** avoir mesuré une série de nuits à pool ouvert. C'est
   la vraie raison d'ouvrir une clé à la fois (§12 étape 6) : chaque ajout donne un point de
   mesure sur ce taux, avant qu'il ne soit trop tard pour reculer.

**Prix assumé, et il est réel** : un échec de `synth` sur `red-watcher` (31 artefacts)
rougit l'unité exactement autant que sept échecs sur `red` (859). Il n'existe aucun état
« majoritairement OK » exprimable. Si l'exploitation montre que cet état manque, il faudra
le mesurer d'abord, pas le postuler.

**Mécanique shell à ne pas rater.** `set -euo pipefail` est actif (`:2`), et cinq `continue`
(`:447, 455, 469, 501, 522` — relevés par `grep -n '^\s*continue\s*$'`) appartiennent à la
boucle de **phases**. Une boucle projet
englobante les transformerait en `continue` de la mauvaise boucle : le projet passerait à
l'itération suivante **de projet** au lieu de la phase suivante. Cinq sites à requalifier,
et c'est le genre de bug qui rend une nuit verte en n'ayant rien fait.

Filet existant, mesuré : `tests/unit/test_dream_sh_exit_code.py` (21 tests) **découpe et
exécute des blocs shell réels** via des ancres textuelles (`:35-39` : `FAIL_TOTAL=$((`,
`if (( extract_rc == 0 )); then`, `# --- ROADMAP`, `case "$phase_rc" in`, `esac`). Ce sont
des `content.index()` : un remaniement qui déplace ces blocs échoue **bruyamment**
(`ValueError`), pas en silence. C'est le filet de cette question.

---

## 10. Q5 — Budgets : par projet ou partagés sur la nuit ?

### Décision : **par projet pour les six phases agent** (elles n'ont pas le choix : le budget est un `timeout` par invocation), **unique et inchangé pour les trois phases globales** (elles ne tournent qu'une fois), et **un plafond de retries pour la nuit entière** — parce que le retry est le seul budget qui soit vraiment une ressource de nuit, et c'est lui qui multiplie par huit.

Valeurs actuelles, relues :

| budget | valeur | portée réelle |
|---|---|---|
| `extract --limit 20 --run-budget-seconds 540 --ticket-budget-seconds 180` | `dream.sh:658` | file **globale**, 17 tickets `pending` mesurés |
| `roadmap --limit 10` | `dream.sh:698` | **10 projets**, pas 10 features |
| `promote --limit 10` | `dream.sh:462` | 10 **candidats** d'**un** projet |

Aucun des trois n'a besoin d'être partagé, pour une raison différente à chaque fois :

- `extract` et `roadmap` sortent de la boucle : leur budget reste littéralement le même.
- `promote --limit 10` **n'est jamais contraignant** — sous le filtre d'aujourd'hui. Rejoué à
  l'identique sur les huit projets du pool, il donne : `red` 3 candidats, `red-shrik` 2,
  **les six autres 0** — dont `brain-v42`. Le plafond de 10 ne mord sur rien.

  **Cette mesure a un jour d'âge, et il faut le dire.** Le prédicat
  `access_count_human >= 3` est entré dans `promote_prepare` avec `62a91030`/`62633f88`
  (2026-08-06) et son classement avec `508439d2` (2026-08-08). Avant, le pool de `brain-v42`
  n'était **pas** vide : `promote` a tourné comme une vraie phase LLM chaque nuit du
  2026-07-18 au 2026-08-06 (`model=gpt-5.6-sol`, 15-19 s, ~75 000 tokens par nuit — mesuré
  dans `dream_runs`). La première nuit à pool vide est le **2026-08-07**, une seule. Le « 2
  projets sur 8 » est donc une photo, pas un régime : il se remesure avant l'étape 6.

  **Et le compteur de lignes `record-empty-pool` n'est pas mesurable : il vaut zéro.**
  `SELECT count(*) FROM dream_runs WHERE phase='promote' AND model IS NULL` → **0 ligne, sur
  toute l'histoire de la table**. Le bloc qui l'appelle n'est entré dans `dream.sh` qu'avec
  `e8b59c3c` (2026-08-07), après le run de 06:00 de cette nuit-là. Le journal du 2026-08-07 le
  montre en creux — `[06:04:36] SKIP promote — empty candidate pool`, puis directement
  `START reorg`, sans la ligne de succès ni le `WARN … row NOT recorded` :

  ```
  [06:04:36] SKIP promote — empty candidate pool
  [06:04:36] START reorg (…)
  …
  - promote [partial]: expected enabled phase missing from dream_runs
  ```

  La dernière ligne est **exactement la fausse alarme que `_record_empty_pool` a été écrit
  pour supprimer**. Donc : la référence « une ligne par nuit aujourd'hui » n'existe pas, et le
  « 6 lignes par nuit » n'est pas un bruit constaté mais une **prédiction** sur du code qui
  n'a jamais tourné en production. Conséquence pour ce chantier : la première nuit du pool
  serait aussi la première vérification de ce chemin, multipliée par six. Il doit être **prouvé
  à un projet** (une nuit avec la ligne `empty-pool dream_runs row recorded` et la row
  correspondante en base) **avant** l'étape 6 de §12.

**Le retry est le vrai sujet.** Il vaut +43 min par projet (`dream.sh:539` : sur échec dur
uniquement, jamais sur timeout, jamais sur `promote`). À huit projets c'est **+344 min de
plafond**, soit la différence entre 459 min (7,7 h) et 803 min (13,4 h).

Recommandation : **une allocation de retries pour la nuit, pas par projet.** Avec deux
retries autorisés sur la nuit et la phase la plus chère (synth, 15 min), le plafond
devient 8 × 53 + 2 × 15 + 35 = **489 min (8,2 h)**, contre 803. On récupère 5,2 h de pire
cas pour un mécanisme d'une ligne.

**Et le plafond systemd doit bouger AVANT la boucle, pas après.** `TimeoutStartSec=10800`
(180 min) ne tient pas deux projets (227 min). Il doit être calé sur la borne **(b)**, le
plafond configuré — pas sur la moyenne mesurée de 80 min — parce que systemd tue à la
borne, pas à la moyenne. Avec l'allocation de retries : ~490 min, à arrondir vers le haut.

**Et il doit bouger dans le TEMPLATE VERSIONNÉ, pas dans l'unité vivante.** La valeur est à
`deploy/systemd/brain-v42-dream.service.tmpl:41` et se retrouve à `:38` de l'unité générée.
`deploy/systemd/install.sh` **régénère l'unité depuis le template** ; son garde-fou
(`:277-345`) n'avertit que sur les lignes `Environment=` ajoutées à la main — `TimeoutStartSec`
n'est pas une ligne `Environment=`. Un plafond relevé à la main dans
`~/.config/systemd/user/brain-v42-dream.service` serait donc **réécrit à 10800 par la
prochaine réinstallation, sans un mot**, et la nuit suivante serait tuée à 180 min au milieu du
troisième projet. C'est le jumeau de l'incident 2026-06-30 cité en tête de `killswitches.conf`
(PROMOTE+REORG éteints deux nuits par une régénération). Le pool, lui, va bien dans le
drop-in : les drop-ins survivent à la régénération.

### Ce qu'on perd

- Avec une allocation de nuit, le projet en tête de pool est mieux servi que celui en
  queue : si les deux premiers brûlent l'allocation, le huitième n'est pas retenté. C'est
  une asymétrie réelle. Atténuation naturelle et déjà éprouvée dans le dépôt : **faire
  tourner l'ordre du pool**, comme `rotate_keys` le fait pour roadmap depuis le 2026-07-04
  (`roadmap_curate.py:474-487`). Sans rotation, c'est toujours le même projet qui est
  sacrifié.
- Le plafond configuré reste très au-dessus de la mesure (489 min de plafond pour 80 min
  de moyenne et 126,6 min de p90). C'est assumé : un plafond sert au pire cas. Mais il rend
  `TimeoutStartSec` inutile comme garde-fou pratique — c'est le p90 qu'il faut surveiller,
  pas l'unité rouge.

---

## 11. Q6 — Huit alertes ou une agrégée ?

### Décision : **une seule alerte agrégée, après la boucle**, et le `--project-key` décoratif est supprimé — remplacé par une ligne « projet » dans le corps du rapport, qui dépend de la 042.

**Ce que l'alerte envoie, et à qui : rien, à personne.** Mesuré, pas supposé.
`build_alert_insight` (`scripts/dream/post_run_alert.py:36-66`) construit une chaîne texte
de 20 lignes au maximum. `_run` (`:138-151`) l'**imprime sur stdout**. `dream.sh:782-784`
redirige ce stdout `>> "$LOG_DIR/$TIMESTAMP.log"` — donc il ne va **même pas** au journal
systemd. Aucune écriture brain, aucun webhook, aucun mail. Le nom `write_alert_if_failed`
est un vestige : il n'écrit rien.

**Seul son code retour compte**, comme troisième condition de la porte de sortie
(`dream.sh:785` puis `:803`) — le « rapporteur muet ».

**Son `--project-key` est décoratif depuis toujours.** Le paramètre est déclaré (`:158`),
passé à `write_alert_if_failed` (`:144`), reçu dans sa signature (`:129`)… et jamais lu :
`fetch_failed_runs(session, run_date)` (`:90-123`) ne le reçoit même pas, et ses trois
requêtes filtrent sur `dream_runs.c.run_date == run_date` seul. Huit invocations
produiraient donc **huit blocs
identiques**, listant les échecs des huit projets, empilés dans un fichier dont §3.2 montre
qu'il ne survivra de toute façon qu'au dernier projet.

Le seul destinataire réel est l'opérateur qui lit `logs/dream/<date>.log` au matin. Une
alerte, une fois, groupée par projet.

**Conséquence mesurée à traiter en même temps** : `MAX_REPORTED_FAILURES = 20`
(`post_run_alert.py:31`) a été dimensionné pour 9 phases par nuit. À 51 lignes, le plafond
devient atteignable et la ligne « N additional failure records omitted » peut masquer des
projets entiers. Soit on l'élève, soit on plafonne **par projet** au lieu du total.

### Ce qu'on perd

- Rien opérationnellement : l'alerte par projet n'a jamais existé. Mais le rapport devient
  plus long, et l'échec d'un seul projet est noyé dans une liste. Le groupement par projet
  est ce qui rend ça lisible — c'est-à-dire que Q6 **dépend de Q1** : sans
  `dream_runs.project_key`, le rapport agrégé ne peut pas étiqueter ses lignes.

---

## 12. Ordre de livraison

La question posée était : *la migration doit-elle précéder la boucle, ou peut-on livrer la
boucle avec des runs mélangés ?*

### Réponse : la 042 précède la boucle. Non négociable, pour une raison qui n'est pas esthétique.

Trois lecteurs **écrivent** sur la ligne qu'ils ont mal identifiée : `promote_validate`
marque `partial` (`:181-196`) et **backfille `dream_promotions.dream_run_id`**
(`:122-127`) ; `connect_validate` marque `partial` ; `REORG_RUN_ID` idem. Livrer la boucle
d'abord produirait un **audit des promotions faux et non réparable** — l'attribution
correcte n'est pas récupérable depuis les lignes une fois écrite. Et `DISTINCT ON (phase)`
effacerait des échecs en silence pendant tout l'intervalle.

Ordre :

| # | ce qui atterrit | régime après merge |
|---|---|---|
| 1 | **Les quatre lignes de prompt** (`phase_synth.md:24,58`, `phase_promote.md:4`, `phase_connect.md:43`) + le contrôle de `project_key` manquant dans `promote_validate` | aucun changement (un seul projet) |
| 2 | **042** appliquée en production, `alembic current` **prouvé** ; puis les 5 écrivains et les 10 lecteurs | aucun changement de comportement, un projet, colonne remplie |
| 3 | **Sortie structurelle des trois phases globales**, `project_key='*'`, ancre de test | aucun changement (elles tournaient déjà une fois) |
| 4 | **Les chemins de journal projetés** (§3.2), la réinit des exports (§3.4), les compteurs `projet/phase` (§9), `TimeoutStartSec` (§10) | aucun changement à un projet |
| 5 | **La boucle**, `BRAIN_DREAM_PROJECT_POOL` absent du drop-in → `brain-v42` seul, plus `expected_dream_phases` cartésien (§6) | **identique à aujourd'hui, à l'octet près** |
| 6 | **L'ouverture du pool, une clé à la fois**, en mesurant la nuit à chaque ajout | changement de régime, réversible en retirant un mot |

Étapes 1 à 5 sont livrables sans qu'une seule nuit change de comportement. C'est la
propriété qui rend ce chantier sûr, et elle vaut qu'on la préserve à chaque commit.

**Contrainte d'ordre entre la base et le merge** : `dream.sh` tourne depuis le working tree
de la **racine**, donc merger sur `main` c'est déployer. La 042 doit être **appliquée en
production avant** le merge qui introduit les lecteurs qui référencent la colonne, sinon la
nuit suivante casse. Et le numéro de révision se **mesure**, il ne se recopie pas d'une
session à l'autre — le CLAUDE.md porte l'incident où « la production reste à 037 » a été
affirmé pendant trois jours après la bascule.

---

## 13. Ce qui se livre killswitch fermé

Doctrine du dépôt : toute capacité neuve se livre fermée, parce que **committer sur une
branche c'est déjà déployer** dès que ça atteint `main`.

- **`BRAIN_DREAM_PROJECT_POOL` n'est pas ajoutée au drop-in.** Le défaut de `dream.sh` est
  `brain-v42` seul, exactement comme l'argument positionnel actuel (`:70`). La boucle
  s'exécute avec un élément. Comportement identique, octet pour octet.
- **Aucun killswitch existant ne change de valeur.** Ni PROMOTE, ni REORG, ni EXTRACT, ni
  ROADMAP, ni SWEEP. Le pool n'est pas une occasion de réviser une cadence.
- **`sweep` reste `ENABLED=false`, `DRY_RUN=true`.** Elle n'a jamais écrit une ligne dans
  `dream_runs` ; son soak est indépendant de ce chantier (§8).
- **`BRAIN_DREAM_CAPABILITY_ENFORCEMENT` reste absente**, donc `false` (§14). Ce chantier
  ne l'arme pas.
- **La 042 n'est pas un killswitch.** Elle est nullable et sans backfill précisément pour
  qu'un lecteur non migré et un écrivain migré coexistent sans casse. Son application reste
  une étape opérateur hors bande.
- **Le test de gate d'installation change en même temps que le template.**
  `tests/integration/test_dream_systemd_install.sh:180` épingle
  `ExecStart=… dream.sh brain-v42`, et `:284,286` épinglent
  `--preflight-capabilities --project-key brain-v42` avec exactement 3 phases. Ces
  assertions sont la preuve que l'unité vivante n'a pas dérivé ; elles ne se contournent
  pas, elles se mettent à jour.

---

## 14. Ce qui reste OUVERT — et c'est le point le plus important

### 14.1 Le scope serveur existe, il est écrit, testé, et **éteint**

`src/brain_v42/services/dream_project_scope.py` et
`src/brain_v42/mcp/dream_capabilities.py:224-261` (`on_call_tool`) implémentent un middleware
complet : un principal `scoped` porte `(phase, project_key)`,
`authorize_dream_project_request` injecte `project_key` dans les appels d'outil selon
`PROJECT_TOOL_POLICIES` — la table vit à `services/dream_project_scope.py:83-120`, ré-exportée
par `mcp/dream_project_authorization.py` — et `bind_dream_project_scope` le rend visible aux
handlers. **19 sites** de `src/` appellent `get_dream_project_scope()` (14 de plus dans
`tests/`).

Il est **inerte en production** : `scripts/dream.sh:20` lit
`BRAIN_DREAM_CAPABILITY_ENFORCEMENT="${BRAIN_DREAM_CAPABILITY_ENFORCEMENT-false}"`, et la
variable est **absente** du `.env` comme des trois drop-ins systemd (`killswitches.conf`,
`nvidia.conf`, `token.conf`) — vérifié. Le principal reste `unscoped`, tous les outils
opèrent sur les 54 projets.

**C'est le vrai levier de §4.1.** Tant qu'il est éteint, boucler huit fois dépense 80 min
pour produire le résultat de 15. Mais l'armer ne suffit pas : `brain_decay_status`,
`brain_consolidation_candidates`, `brain_backfill_links_batch`,
`brain_list_orphans_for_classification` et `brain_get_clusters` portent une
`DreamProjectToolPolicy()` **vide** — aucun scope, même middleware allumé. Or ce sont
exactement les cinq outils que SCAN, CLEAN, CONNECT et SYNTH utilisent.

**Question laissée ouverte, délibérément** : est-ce que le pool a un sens avant que ces cinq
politiques soient remplies ? Cette spec ne la tranche pas, parce que la réponse dépend d'une
mesure qui n'existe pas — la durée d'un run agent réellement scopé sur un projet. Elle doit
être posée à l'opérateur **avant l'étape 6** de §12, pas après.

### 14.2 L'identité de provenance des runs

`scripts/dream/codex_runner.py:244` calcule `agent_header = f"dream-codex-{phase}"` et `:262`
le pose en `X-Brain-Agent` — sans projet.
Les huit runs seraient indiscernables dans `access_log.actor`, la colonne que la 041 a
ajoutée précisément pour rendre ça visible.

Mais mettre le projet dans l'acteur **casse un plafond mesuré** :
`src/brain_v42/metrics/collector.py:130` fixe `_MAX_AGENTS = 32`. Le dream occupe 6 slots
aujourd'hui ; `dream-codex-{phase}-{project}` en occuperait **48** — au-delà du plafond à
lui seul, et il évincerait les acteurs humains. Deux contraintes annexes vérifiées :
`provenance.py:27` `_SYSTEM_ACTOR_PREFIXES = ("dream-codex-",)` classerait toujours ces
acteurs « système », donc `access_count_human` ne serait pas faussé ; et `access_log.actor`
est `VARCHAR(64)`, où `dream-codex-promote-auto-discord` (32 caractères) rentre.

**Ouvert** : ne rien changer à l'acteur dans ce lot est la recommandation par défaut, mais
cela laisse la provenance des runs aveugle au projet pendant que `dream_runs` le connaît.
Les deux vues divergeront.

---

## 15. Tests

TDD, cycle Red-Green-Refactor comme le reste du projet. Base mesurée sur le rayon dream :

```
pytest tests/unit -q -k "dream or promote or reorg or connect_validate or roadmap
                         or extract or sweep or preflight or render_prompt"
→ 952 passed, 47 skipped, 6309 deselected, 22,30 s
```

Ce que le pool doit ajouter, un test = un comportement :

- **042 nullable, jamais bloquante** : un INSERT best-effort sans `project_key` réussit
  encore. Le test doit passer par le chemin `except Exception: print(warning)` de
  `ticket_extract.py:764`, sinon il ne prouve rien.
- **Sentinelle des globales** : `extract`, `roadmap` et `sweep` écrivent `'*'` ; une phase de
  la boucle écrit une clé de projet ; `NULL` n'est écrit par personne.
- **`_clean_dry_streak` de `roadmap` traverse la bascule** : avec 856 lignes à `project_key
  NULL` plus des lignes neuves à `'*'`, le compteur mesuré aujourd'hui (24) ne doit pas
  retomber à 0. C'est le seul streak qui vaut quelque chose ; c'est celui qu'il faut
  épingler.
- **`DISTINCT ON (phase)` n'efface plus** : deux lignes `synth` la même nuit, une `fail` sur
  un projet et une `done` sur un autre → le payload `/dream` doit montrer l'échec.
- **`expected_dream_phases` cartésien** : un projet du pool qui saute `promote` déclenche
  l'alarme **même si** les sept autres l'ont observée. Le test doit simuler les sept
  autres, sinon il passe au vert sans rien prouver.
- **Ordre projet-majeur** : CONNECT du projet N reçoit le rapport CLEAN du projet N, pas
  celui du projet N−1. Ancre textuelle sur la structure de boucle, comme
  `test_dream_sh_agent_provider.py:74`.
- **Réinitialisation des exports** : un projet qui saute PROMOTE ne laisse pas
  `PROMOTE_CANDIDATE_POOL_JSON` chargé pour le suivant.
- **Isolation** : le projet 3 échoue, les projets 4 à 8 tournent quand même, et
  `FAILED_PHASES` contient `projet3/synth` — pas `synth`.
- **Trois phases globales, une seule fois** : avec un pool de 3 projets, exactement une
  ligne `extract`, une `roadmap`, une `sweep`.
- **Chemins de journal disjoints** : deux projets ne se tronquent pas mutuellement leur
  rapport. À écrire contre `codex_runner.py:419,432-433`, qui est l'endroit qui tronque.
- **Chemins de journal PARTAGÉS, à l'inverse** : `$TIMESTAMP.log` et les trois journaux des
  phases globales restent **non projetés** (§3.2). Sans ce test, la consigne « projeter » sera
  appliquée aux douze et fabriquera sept fichiers vides plus un récit de nuit fragmenté.
- **Format du pool, contre le drop-in réel** : un pool à deux projets écrit dans un
  `Environment=` est relu **avec ses deux projets**. Le test doit passer par le vrai texte de
  drop-in, pas par une liste Python déjà découpée — sinon il ne prouve rien sur le piège de
  découpage aux blancs de systemd (§6).
- **`record-empty-pool` écrit vraiment sa ligne** : pool vide → une ligne `dream_runs`
  `phase='promote'`, `model IS NULL`, avec le `project_key` du projet courant, ET la ligne de
  journal de succès. Ce chemin n'a **jamais** tourné en production (§10) ; le test est sa
  première preuve.
- **`fetch_failed_runs` groupe par projet** : deux projets en échec la même nuit produisent
  deux lignes étiquetées, et le plafond `MAX_REPORTED_FAILURES` n'efface pas un projet entier.

À mettre à jour en même temps : `tests/unit/test_dream_sh_agent_provider.py:74,89` (deux
assertions littérales sur le texte du script) et
`tests/integration/test_dream_systemd_install.sh:180,284,286`. Les 8 tests d'intégration
shell **ne sont pas collectés par pytest** (`testpaths=["tests"]` ne ramasse pas les `.sh`)
— remarque déjà consignée dans `test_dream_sh_exit_code.py:60-64`, et qui veut dire qu'ils
se lancent à la main ou pas du tout.

---

## 16. Limites assumées

- **479 artefacts de sous-projets hors périmètre**, dont `red-shrik:agent` (280) plus gros
  que son parent (222). Le pool couvre **62,4 %** du corpus (2 373 / 3 803) ; **37,6 %**
  restent dehors (1 430), dont 26 artefacts sans `project_key` du tout. Aucune mécanique de
  préfixe n'existe pour refermer ça (§2).
- **Tant que le scope serveur est éteint, huit runs font huit fois le même travail.** Le
  pool achète l'infrastructure de la consolidation multi-projets, pas la consolidation
  multi-projets (§14.1).
- **La fourchette de mur d'horloge repose sur l'hypothèse « coût par run indépendant du
  projet »**, vraie aujourd'hui et fausse le jour où le scope est armé. p90 126,6 min et max
  286,3 min sont des simulations sur des nuits réelles, pas des mesures d'une nuit à huit
  projets — qui n'a jamais eu lieu.
- **Le pre-flight ne bornera rien.** `dream_preflight._fetch_signals:98-115` agrège les cinq
  tables **sans filtre de projet**, et le gate ne s'est déclenché que **2 fois sur 121
  nuits** (1,7 %). Avec le pool, le verdict devient partagé par les huit : une écriture dans
  `red` fera tourner les phases Opus pour `red-watcher` (31 artefacts).
- **Les journaux n'ont aucune rotation.** Mesuré sur la production : `logs/dream/` contient
  **1 865 fichiers pour 121 Mo**, et `grep logrotate|mtime|find.*logs` sur `dream.sh` et
  `deploy/systemd/install.sh` ne donne **aucun résultat**. Le pool multiplie le débit de
  fichiers par 8. Ce n'est pas un problème de cette spec, mais elle en avance l'échéance.
- **Le scrub XML tournera 8 fois par nuit.** `scripts/scrub_xml_tool_call_leak.py:122-132`
  balaye `learnings`, `decisions` et `project_contexts` en entier, sans filtre de projet.
  Idempotent → coût seul, pas de corruption. Idem pour le préflight Codex de `dream.sh:363-387`.
- **Jusqu'à 6 lignes `record-empty-pool` par nuit — prédit, pas mesuré.** La production en a
  écrit **zéro** à ce jour ; le chemin n'est entré dans `dream.sh` que le 2026-08-07 après le
  run. La seule nuit à pool vide observée (2026-08-07) n'a produit aucune ligne et **a
  déclenché le `partial` de synthèse** que ce mécanisme devait éteindre (§10). À prouver à un
  projet avant d'en faire six.
- **`sweep` reste globale et hors pool**, donc « le pool » et « ce que `sweep` touche » sont
  deux ensembles différents pour toujours (§7, §8).
- **Aucun quorum d'échec.** Un échec sur le plus petit projet rougit l'unité autant que sept
  sur le plus gros (§9).

---

## 17. Ce que je n'ai pas pu mesurer

Écrit ici plutôt qu'estimé ailleurs.

1. **Le débit maximal de l'abonnement qui authentifie Codex.** ~11,6 M tokens/nuit en
   moyenne et ~30,8 M sur la pire nuit sont des extrapolations depuis `dream_runs` ; rien
   ici ne dit si le plafond de l'abonnement les absorbe. C'est le risque non chiffré le plus
   sérieux du chantier.
2. **Le budget API NVIDIA de `extract` et `roadmap`.** `dream_runs` enregistre `0` token
   pour ces deux phases (vérifié) : leur consommation réelle n'est pas mesurable depuis la
   base. Comme les deux sortent de la boucle, l'exposition est nulle dans la conception
   retenue — mais elle serait invisible dans une autre.
3. **Le nombre d'acteurs humains distincts** face au plafond `_MAX_AGENTS = 32` :
   `access_log` contient **0 ligne** en production au moment de la mesure.
4. **La durée réelle d'un run agent sur un projet non-`brain-v42`.** Jamais exécuté.
   L'hypothèse « coût constant par run » (§4.1) est déduite de la structure des prompts et
   des signatures d'outils, pas d'une mesure.
5. **Le comportement du corpus à huit SYNTH par nuit.** La boucle de rétroaction — SYNTH
   crée des `dream:generated`, exclus du signal pre-flight par `dream_preflight.py:78` — n'a
   jamais été observée à ce débit.
6. **Les consommateurs hors dépôt du payload `/metrics`** (red-monitor) : non inspectés,
   donc l'impact réel des lecteurs `DISTINCT ON` sur leurs tableaux de bord n'est pas
   établi.
7. **Le coût côté Neo4j** de huit passes de `brain_get_clusters` et
   `brain_backfill_links_batch` par nuit.
8. **Le comportement de `record-empty-pool`**, qui n'a jamais écrit une ligne (§10). Tout ce
   que la spec en dit est déduit du code, pas observé.
9. **Le régime réel du pool de PROMOTE.** La mesure « 2 projets sur 8 non vides » a **un
   jour**, sous un prédicat de maturité entré le 2026-08-06 et un classement entré
   aujourd'hui. La série antérieure (pool non vide chaque nuit pour `brain-v42`) est mesurée,
   mais elle décrit un autre filtre. À remesurer avant l'étape 6.
10. **Le taux de nuit rouge sous pool ouvert.** §9 en donne une projection
    (`1 − (1 − p)⁸` → 42-77 %) à partir de deux taux mesurés, sous une hypothèse
    d'indépendance qui est fausse dans le sens conservateur. Aucune nuit à plus d'un projet
    n'a jamais tourné.

## 18. Hors périmètre

- **`scripts/dream/cross_project_resonance.py`** — aucun site d'appel dans `scripts/`,
  `src/` ou `deploy/` (vérifié), killswitch `BRAIN_DREAM_CROSS_PROJECT_ENABLED=false`. À ne
  pas confondre avec le pool : la résonance cross-projet lit plusieurs projets dans un run,
  le pool exécute un run par projet.
- **L'inclusion des six sous-projets** — dette datée et chiffrée (§2), ticket séparé.
- **L'armement de `BRAIN_DREAM_CAPABILITY_ENFORCEMENT` et le remplissage des cinq
  politiques vides** — chantier distinct, et probablement prioritaire sur l'ouverture du
  pool (§14.1).
- **L'ouverture de `sweep` en WET** — décision indépendante, jamais dans le même lot (§8).
- **La rotation des journaux** — problème préexistant qu'on ne referme pas ici.
- **Le projet dans `X-Brain-Agent`** — bloqué par `_MAX_AGENTS` (§14.2).

---

## 19. Revue adversariale — ce qui a été remesuré

Le 2026-08-08, chaque chiffre de cette spec a été rejoué contre la production.

**Reproduits à l'identique** : les masses du pool et des sous-projets (2 373 / 3 803 / 479) ;
la simulation de mur d'horloge dans ses huit décimales (15,05 · 21,22 · 72,76 · 80,04 ·
126,62 · 286,26 · 120,41 · 582,10) ; la décomposition par phase ; le piège `NULL` des tokens
(167 lignes sur 272, 1 455 688 / 3 850 807, 0,6425 $ / 4,6846 $) ; les cinq streaks DRY dont
`roadmap` = 24 ; les 7 fantômes à 7 jours et leur répartition 3 dans le pool / 4 dehors, zéro
pour `brain-v42` ; le pool de PROMOTE (`red` 3, `red-shrik` 2, six autres 0) ; les 17 tickets
`pending` ; `access_log` à 0 ligne ; `logs/dream` à 1 865 fichiers pour 121 Mo ; le pre-flight
à 2 déclenchements sur 121 nuits ; la base pytest 952/47/6309 ; les quatre lignes de prompt en
dur ; le régime du drop-in vivant ; `TimeoutStartSec=10800` ; les 21 tests et les ancres
textuelles de `test_dream_sh_exit_code.py`.

**Corrigés** : la rotation `roadmap` (30 projets et 3 nuits, pas 54 et 6) ; le compteur
`record-empty-pool` (zéro ligne en production, pas une par nuit — §10) ; l'ancienneté d'un jour
de la mesure du pool PROMOTE ; la consigne de projection des chemins (7 sur 12, pas 12 sur 12
— §3.2) ; le transport du pool dans `Environment=` (découpage aux blancs de systemd, parseur
booléen — §6) ; le lieu où déplacer `TimeoutStartSec` (le template versionné, pas l'unité
vivante — §10) ; le taux de nuit rouge attendu (§9) ; la part hors pool (37,6 %) ; le nombre de
sites de scope (19, pas 21) ; les numéros de ligne des cinq `continue`, de `post_run_alert` et
de `codex_runner` ; deux longueurs de chaîne (16 et 32 caractères).

**Non renversé** : aucune des six recommandations. Q1 (042 nullable, sentinelle `'*'`), Q2
(killswitches globaux), Q3 (les trois phases globales hors boucle), Q4 (isolation sans quorum),
Q5 (budgets par projet, retries à l'échelle de la nuit) et Q6 (une alerte agrégée) tiennent
toutes après contre-argumentation. Q4 et Q6 arrivent avec une dette de mesure désormais
explicite ; Q2 arrive avec deux pièges de transport qui ne se voyaient pas.

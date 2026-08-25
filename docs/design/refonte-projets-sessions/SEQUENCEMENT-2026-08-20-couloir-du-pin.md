# DOSSIER DE SÉQUENCEMENT DU COULOIR DU PIN — brain_v42

**Établi le 2026-08-20. Mesures de première main horodatées ; tout le reste est marqué.**
**Statut : PARTIELLEMENT SIGNÉ le 2026-08-20 — décision `9d22bc6a-0d01-4502-b232-e2a6b9c85945`.**
**S6 AMENDÉ — décision `1b742dc7` (2026-08-25) : la 048 est prise par `attribution_mode`, le couloir compte 9 rendez-vous. Bloc d'amendement ci-dessous.**

> **CE QUI EST SIGNÉ** — **S1** : le contrat DR passe en **v5 UNIQUE avec `alembic_head` DÉRIVÉ**
> (précédent `test_alembic_env.py:254-259`), donc **un seul mint pour tout le couloir** au lieu
> de 7-12 ; la révision exacte reste prouvée par le pin `_REQUIRED_ALEMBIC_HEAD` côté code.
> **S6** (**AMENDÉE** par `1b742dc7`, bloc suivant) : **Ordre B avec 048 dégroupé**, soit **8 rendez-vous** — `046 = M-A+M-G` → `047 = M-B`
> → M-C et M-E en têtes **séparées** tant que S9 n'a pas démontré l'indépendance de leurs
> downgrades → `049 = M-D` **isolée** (collision d'attestation pendant sa fenêtre
> trigger-désactivé) → `050` = trio `ADD COLUMN` nullable → `051`/`052`.
>
> **CE QUI RESTE OUVERT, ET BLOQUE TOUJOURS LA PREMIÈRE LIGNE DE LA 046** : **S2**
> (colonne de connexion), **S3** (nature en base), **S4** (CHECK dur ou souple), **S5**
> (SPEC-M-G entière). L'opérateur doit lire `SPEC-M-G.md` pour les trancher.
>
> **AUCUNE LIGNE DE MIGRATION NE DOIT ÊTRE ÉCRITE** — l'exigence auto-imposée par
> `SPEC-M-G.md` tient : S6 donne l'ORDRE, pas l'autorisation d'écrire la 046.

> **AMENDEMENT — décision `1b742dc7` (2026-08-25), qui amende S6 de `9d22bc6a` (2026-08-20).**
> **La 048 est attribuée à `attribution_mode`.** La réparation de la capture dérivée a produit
> `alembic/versions/048_attribution_mode.py` — colonne `attribution_mode VARCHAR(24)` nullable
> sur `brain_session_artifacts`, CHECK à quatre modes, index partiel sur le mode déduit. Le slot
> que S6 réservait à M-C (ou M-E) est donc pris. **M-C, M-E, M-D, le trio `ADD COLUMN` et la
> suite GLISSENT D'UN CRAN ; le couloir compte 9 rendez-vous au lieu de 8.**
>
> | Candidat | Rang S6, Ordre B dégroupé | Rang amendé (`1b742dc7`) |
> |---|---|---|
> | C1 = M-A + M-G | 046 | 046 — inchangé |
> | C2 = M-B | 047 | 047 — inchangé |
> | *(hors dossier)* | — | **048 = `attribution_mode`** |
> | C3 = M-C | 048 | **049** |
> | C4 = M-E | 049 | **050** |
> | C5 = M-D | 050 | **051** |
> | C8 + C9 + C12 = trio `ADD COLUMN` | 051 | **052** |
> | C11 = journal d'accès durable | 052 | **053** |
> | C7 = dimension embedding | 053 | **054** |
>
> *La colonne « Rang S6 » applique le dégroupage que S6 signe. Le texte de S6 ci-dessus écrit
> `049 = M-D`, `050` = trio, `051`/`052` : ce sont les numéros du tableau §2 **groupé**,
> incompatibles avec le dégroupage de la même phrase — M-C et M-E en têtes séparées prennent
> 048 et 049. L'incohérence est dans le texte signé ; `1b742dc7` ne la tranche pas, et seule la
> lecture dégroupée donne les 8 rendez-vous que S6 annonce.*
>
> **L'AMENDEMENT DÉPLACE DES NUMÉROS, IL NE RELÂCHE AUCUNE GARDE.** Tiennent mot pour mot :
> M-C et M-E restent des têtes **séparées** tant que S9 n'a pas démontré l'indépendance de
> leurs downgrades ; M-D reste **isolée** (collision d'attestation pendant sa fenêtre
> trigger-désactivé) ; la règle **jamais deux têtes en vol** ; le critère **(c)** du §2 —
> downgrades pouvant échouer ensemble sans qu'un fail-closed de l'un empêche le rollback
> légitime de l'autre.
>
> **Pourquoi l'insertion coûte si peu.** S1, signée sur le même couloir, a fait passer le
> contrat DR en **v5 unique à `alembic_head` DÉRIVÉ** précisément pour qu'une tête de plus ne
> coûte plus une réécriture de `_expected_v4()`. Le prix d'une insertion se réduit donc à une
> renumérotation documentaire — ce que S1 avait acheté.
>
> **CE QUI RENDRA CES NUMÉROS FAUX**, et qu'aucun test ne garde : une tête consommée par un
> candidat hors dossier — c'est déjà arrivé deux fois — ou une insertion signée. Les mesurer,
> jamais les recopier : `ls alembic/versions/` pour la tête du dépôt,
> `select version_num from alembic_version` pour la production.
>
> **NON TRANCHÉ ICI** : la 047 mergée est `047_end_without_the_capture_receipt`, qui ne porte
> pas M-B. Le compte de 9 conserve le `047 = M-B` de l'ordre signé ; si M-B doit encore prendre
> une tête à elle, il est de 10. `1b742dc7` ne se prononce pas là-dessus.
>
> **Portée de ce bloc : les NUMÉROS seuls.** Les autres affirmations du bloc de signature
> ci-dessus énoncent l'état du 2026-08-20 et ne sont pas rejouées ici.

---

## 0. ÉTAT MESURÉ DU COULOIR — 2026-08-20, ~22h05

| Fait | Valeur mesurée | Commande | Verdict |
|---|---|---|---|
| Tête de production | `045` | `docker exec brain_v42_postgres psql -U brain -d brain -Atc "select version_num from alembic_version;"` | **CONFIRMED** |
| Pin | `_REQUIRED_ALEMBIC_HEAD = "045"` (`plan_index_repair_store.py:63`) | lecture | **CONFIRMED** |
| Tête du dépôt | `045`, 45 fichiers, chaîne linéaire contiguë 001→045 | `ls alembic/versions/*.py` | **CONFIRMED** |
| Aucune 046 nulle part | 0 blob, 0 fichier | `git rev-list --all --objects \| awk '$2 ~ /046/'` + `find` | **CONFIRMED** |
| Couloir vide côté PR/branches | `gh pr list --state open` vide, PR #7 mergée (`14c0385`) | verif 2 | **CONFIRMED** |
| Index `public` | **129** vs `catalog_counts.indexes = 128` épinglé dans `ops/recovery/brain-v42-v4.json` | `select count(*) from pg_indexes where schemaname='public'` + lecture JSON | **CONFIRMED (première main)** |
| FK `public` | 26 = 26 épinglé | psql | **CONFIRMED** |
| Tables `public` | 32 = 32 épinglé | psql | **CONFIRMED** |
| `alembic_head` des assets | **`039`** (`v4.json`, `v4.sql:1789,1791`, `v4-pgrestore.sql`) vs prod `045` | lecture | **CONFIRMED (première main)** |
| `brain_sessions.status` | `varchar(20)` — `closed_inactive` fait **15** caractères, **il entre** | `information_schema.columns` | **CONFIRMED** |
| Vues sur `brain_sessions` | **ZÉRO** (`view_column_usage` + `pg_depend`/`pg_rewrite`, deux angles) | psql | **CONFIRMED** |
| `v4.json` nommé dans les 5 documents de conception | **0/0/0/0/0** | `grep -c v4.json` | **CONFIRMED** |
| SPEC écrites aujourd'hui | `SPEC-M-G.md` (15 750 o, 21:50), `SPEC-checkpoint.md` (16 881 o, 21:50) — **« PROPOSITION SOUMISE À SIGNATURE »** | `ls --time-style=full-iso` | **CONFIRMED** |
| ADR §0ter | **existe**, l.368+ : « M-A et M-G partent en UNE SEULE tête » | lecture | **CONFIRMED** — *réfute l'enquête 2, qui lisait un ADR daté du 08-19* |
| PLAN §8, ligne M-G | **mise à jour** : « **UNE TÊTE AVEC M-A** (signé 2026-08-20, ADR §0ter.1) — *rang dans le couloir toujours à séquencer* » | `PLAN:1421` | **CONFIRMED** — *réfute les deux enquêtes, PLAN retouché à 22:05* |

> **Caveat de fraîcheur, décisif.** ADR et PLAN ont été réécrits à **22:05**, soit *après* les deux passes de vérification. Toute conclusion de la forme « le document ne dit pas X » doit être rejouée à l'instant de l'usage. Le corpus `docs/design/` est **non tracké** (`git status: ?? docs/design/`) : aucun diff ne protège ces lectures.

### 0.1 LA DÉCOUVERTE QUI DOMINE TOUT LE DOSSIER

**Le contrat de récupération épingle `alembic_head`. Donc AUCUNE tête n'est « sans coût d'attestation » — l'attestation passe au rouge à chaque bascule, y compris pour un simple `ADD COLUMN`.** Mesuré : `v4.json` porte `{"id":"alembic_head","kind":"alembic_head_equals","revision":"039"}`, `v4.sql:1789-1791` idem.

Conséquence immédiate, **déjà vraie avant toute nouvelle tête** : l'attestation live est rouge sur **au moins deux** contrôles que j'ai mesurés moi-même (`alembic_head` 039≠045, `indexes` 128≠129). Le runbook annonce `24/25` (l.154-155) *et* exige `25/25` comme **porte d'autorisation** avant `repair` (l.63, l.66, l.583) : **le document se contredit, et la réalité est pire que sa branche pessimiste**. Le troisième échec annoncé par le ticket `eb067b57` (`view_column_mismatches=1`) est **UNVERIFIABLE** de mon côté — je n'ai pas exécuté le `.sql` (périmètre lecture seule).

Cela transforme la question de séquencement. Elle n'est pas « quelles têtes coûtent une régénération d'assets » — **elles la coûtent toutes**. Elle est : *le contrat de récupération suit-il la tête, ou décrit-il une forme ?* Voir §4, signature **S1**.

---

## 1. LE TABLEAU DES CANDIDATS

**Recensé par sept angles indépendants** : `ls alembic/versions/`, `git rev-list --all --objects`, `git grep '\b045\b'`, tickets PostgreSQL (31 ouverts relus), `decisions` récentes, roadmap `features`, catalogue PostgreSQL live. Chaque angle a produit au moins un candidat que les autres n'avaient pas.

### 1.1 File vivante — têtes du plan de refonte

| # | Candidat | Contenu (1 ligne) | Gate | Couplages | Qui attend dessus | Verdict |
|---|---|---|---|---|---|---|
| **C1** | **M-A + M-G** (prendra le n° **046**) | 4 colonnes nullable `brain_sessions` (`started_by_actor` 64, `last_observed_at`, `intent` 500, `nature`) + **colonne de CONNEXION et son index UNIQUE PARTIEL `WHERE status='open'`** + 4ᵉ branche terminale `closed_inactive` + rail Pydantic C7 | **SIGNÉE** (décision `c5160259`, ADR §0ter.1). SPEC-M-G écrite, **non signée**. Cinq trous d'écriture subsistent (§1.4) | Pin + 11 gardes + **6 mécanismes d'attestation** (empreinte colonnes, `COLUMN_DEFINITION_MD5`, `expected_session_indexes`, `SESSION_INDEX_DEFINITION_MD5`×4 fichiers, **DEUX** md5 de CHECK + `expected_session_constraint_fragments`, `catalog_counts.indexes` 129→130). **Pas de danse vue.** Restart MCP requis (`PgBrainSessionRepo` câblé `server.py:372-379`) | Toute la tranche minimale : B1, B2, B9 fermés, B3 mesurable. **Et, par la règle 3, TOUT le reste du couloir** | **CONFIRMED** |
| **C2** | **M-B** — 8ᵉ valeur `'ticket'` au CHECK artefacts | `brain_session_artifacts` : CHECK élargi + **`knowledge_sources` élargi aux tickets et son prédicat au sous-arbre** (Q14=(a), décision `e1f62ea1`) | **Aucun gate ouvert.** Q2 et Q14 répondues le 08-19 | `expected_artifact_constraints` (7→8 valeurs) **+ CTE `knowledge_sources` (`v4.sql:1083-1090`)** ⇒ heurte la **parité de CTE** pgrestore (`test_recovery_contract_v4_pgrestore.py:29-33`, écart autorisé = exactement `{observed_artifact_constraints, observed_session_constraints}`). Pas de danse vue | B4/B5 sur l'axe traçabilité du savoir (axe retenu par Q10) | **CONFIRMED** |
| **C3** | **M-C** — table `brain_session_checkpoints` | Table neuve + trigger append-only, replay idempotent sur `(session_id, seq)` | `SPEC-checkpoint.md` **écrite le 08-20 à 21:50, NON SIGNÉE** (porte une section « Contradiction interne de l'ADR, à trancher ») | `table_set` en **4 endroits** : `test_recovery_contract.py:292` + `_v2.py:33-39` (dérivés de METADATA — la table neuve doit être ajoutée à `post_contract_tables`) + `v4.json` (32→33) + `SCHEMA.md` « 32 tables `public` ». À instruire : le trigger entre-t-il dans `expected_runtime_user_triggers` ? | B7 | **CONFIRMED** |
| **C4** | **M-E** — table `brain_session_staged_captures` + pool hors-session | Table neuve + pool de brouillons non signés survivant hors session, derrière flag fermé | **SPEC DU POOL ABSENTE** — seule spec de Phase 0 encore due (`ls` du répertoire : absente). Gate explicite : « FK et ON DELETE, durée de vie, plafond, tool de signature hors session » | `table_set` (4 endroits). **Piège nommé** : CHECK-NOT-NULL + FK `ON DELETE SET NULL` rend le DELETE parent impossible, et les CHECK ne sont pas deferrables en PostgreSQL | Phase 4.4, **promue avant la Phase 3** par Q10 | **CONFIRMED** |
| **C5** | **M-D** — `project_focus_history` + constraint trigger différé | Table neuve + trigger append-only + **constraint trigger `AFTER UPDATE OF current_focus` sur `project_contexts`, créé DÉSACTIVÉ** | Aucun gate (Q13 module, ne bloque pas). **Portée INSERT à trancher à l'écriture** | **SEULE tête déclenchant la revue DURE du pin** (`project_contexts` est l'une des 3 tables nommées `test_plan_index_repair_head_pin.py:45-52`). **Collision insoluble** : `v4.sql:917` exige `tgenabled='O'` ⇒ **aucun ordre de régénération ne rend l'attestation verte pendant la fenêtre désactivée**. `expected_runtime_user_triggers` = 13 triggers / 5 tables dont **7 sur `project_contexts`**. **Ordre de rollout dérogatoire** : upgrade → restart MCP → **puis** activation du trigger, geste opérateur nommé | B6. Repoussée en dernier par le reséquencement Q10 | **CONFIRMED** |
| **C6** | **M-F** — `brain_session_attribution_moves` | Journal des réattributions d'artefacts entre sessions | **Q8 OUVERTE** — « droit de réattribution journalisée, ou orphelinage = prix de la preuve ? ». Bloque sa propre tête, rien d'autre | `table_set` seul. Downgrade le plus léger du carnet | Rien | **CONFIRMED** |

### 1.2 File vivante — candidats hors plan de refonte

| # | Candidat | Contenu (1 ligne) | Gate | Couplages | Qui attend dessus | Verdict |
|---|---|---|---|---|---|---|
| **C7** | **Dimension embedding honnête** (ticket `c60d023d`) | Révision **terminale convergente** : lit le typmod vivant des **9** colonnes `embedding vector(1536)`, n'émet un ALTER que sur divergence ⇒ NO-OP sur la prod | Gate « M-A attend 046 » **LEVÉ** (ADR §5 pt 5, PLAN l.252-262). **Mais la décision `24495130` (08-18, `active`, non supersédée) ordonne encore « 046 en second lot »** | **Pas de danse vue** (mesuré au niveau colonne : 0 vue ne projette `embedding`). **9 index HNSW à reconstruire** (`m=16, ef_construction=64`, pgvector 0.8.2). Les 9 checks `embedding_<table>` de `v4.json` épinglent `dimensions:1536`. **Migrations strictement auto-portantes** — aucune n'importe `brain_v42` | Rien. Ticket auto-déclaré non urgent | **CONFIRMED** |
| **C8** | **`thinking_tokens` sur `dream_runs`** (ticket `76e11c9f`) | 1 colonne nullable — **sorti de la 045 par arbitrage opérateur**, ticket créé pour que le renoncement soit explicite | Aucun. **Issue (2) acceptable : un renoncement ÉCRIT ne consomme PAS de tête** | Aucun asset (`dream_runs` hors `expected_column_fingerprints`). Pas de danse (ADD COLUMN ; précédent 042). Downgrade = `DROP COLUMN` nu | L'ordre des rails codex→agy→claude, tranché sur un coût comparé faussé de ~38 % | **CONFIRMED** |
| **C9** | **Compteur de volume du sweep** sur `dream_runs` | 1 colonne nullable best-effort (`sessions_swept`) | **AUCUN PORTEUR ÉCRIT** — 0 grep, 0 brain_search, 0 ticket, 0 décision (4 angles, deux enquêtes concordantes) | Identiques à C8, au caractère près | Rien. Le manque est réel (`session_sweep.py:99-113` : 8 champs, aucun volume ; `count` reste dans `render_report`) | **CONFIRMED** (manque réel) / **UNVERIFIABLE** (demande) |
| **C10** | **G7 — `project_key NOT NULL`** sur `decisions`/`learnings`/`snippets` | 3 × `ALTER COLUMN … SET NOT NULL` | **AUCUN PORTEUR ÉCRIT.** Nommé une seule fois, dans `SPEC-M-G.md:256`, sans définition ailleurs. La série G existe (G9 dans la décision `74bf3e6f`) | Aucun asset attendu (les 3 tables sont hors `expected_column_fingerprints`, qui contient 12 objets : 2 tables de sessions + 10 vues). **Pas de danse** (SET NOT NULL ≠ retype). `ACCESS EXCLUSIVE LOCK` + scan | Rien. **Zéro NULL mesuré** — trois dénominateurs différents le même jour (4 232 / 4 235 / 4 237) | **CONFIRMED** (zéro-NULL) / **UNVERIFIABLE** (porteur) |
| **C11** | **Journal d'accès durable** (ticket `b93e32be`) | Rétention `access_log` **ou** journal d'agrégats `(entité, acteur, jour)` — **1 à 2 tables neuves** | Contenu non tranché par le ticket ; coût stockage à dimensionner | `table_set` (4 endroits). Pas de danse vue | **Le seul candidat dont le coût d'attente est strictement croissant et NON RÉCUPÉRABLE** : `access_log` = 0 ligne (régime normal), 754 `access_count_human>0` et 718 `last_accessed_at_human` non nuls **pour zéro événement source**. La 044 a porté l'irréversible sur `access_factor` (poids **0,3**) | **CONFIRMED** |
| **C12** | **Champs structurés priorité/readiness sur `tickets`** (ticket `b68b9692`) | « champs structurés priorité, readiness, dépendances et raison/preuves ; gate directe avant lancement » | Aucun. Ouvert depuis 18 jours, appuyé sur une **précision READY mesurée à 1/3, deux faux positifs sur 27 tickets** | ADD COLUMN sur `tickets` ⇒ **pas de danse** (6 vues en dépendent, aucune n'est cassée par un ADD COLUMN). Une feature roadmap au statut **`building`** demande le même lot (`deadline`, priorité, readiness, `blocked_by`) + « aligner `db/tables.py:1252` et **les deux miroirs SQLite** » | Rien de bloquant | **CONFIRMED** — *candidat absent des deux premières enquêtes* |

### 1.3 Hors file — conditionnels, latents, écartés

| # | Candidat | Statut | Verdict |
|---|---|---|---|
| **C13** | **Poison pill option A** — statut terminal `ExtractionStatus` | **DIFFÉRÉ par signature** (décision `74bf3e6f`, « Stratégie C seule »). **Revue datée : ticket `191b2dba`, 2026-09-03.** Seul candidat où la danse vue/GRANT façon 045 est en jeu — **conditionnelle** : `tickets.extraction_status` est `varchar(10)`, projetée par `codex_ticket_v1`. **Échappatoire mesurée** : `ticket_extraction_attempts.status` (`varchar(10)`, CHECK propre) n'a **ZÉRO** dépendance de vue — y loger l'état supprime toute danse | **CONFIRMED** |
| **C14** | **Hiérarchie de projets (`parent_key`)** — Q4 | **LATENT.** Q4 ouverte, non bloquante ; Phase 4.6 **n'a AUCUNE ligne au tableau §8**. `projects` a 7 colonnes, aucun parent. Huitième tête surprise possible | **CONFIRMED** |
| **C15** | **Contrat codex v2 (9 vues read)** — ticket `52d6b319` | **ÉCARTÉ sous réserve.** 10 vues `codex_*_v1` existent déjà, toutes avec `GRANT SELECT` à `codex_ro`. Le reste-à-faire est le write gateway `:9210` — pas du DDL. **Non comparé nom par nom** | **UNVERIFIABLE** |
| **C16** | **`recovery v5` / dette catalogue DR** — tickets `eb067b57` + `8eaefe36` | **PAS UNE TÊTE — le préalable de toutes** (§0.1). `8eaefe36` porte un verdict d'audit externe **NEEDS_SPLIT**. Trous mesurés : `grep -ci sequence v4.sql` = **0** (9 séquences non couvertes) ; **55 triggers non-internes** en prod contre 13 nommés au contrat ; `pg_get_constraintdef` absent des deux sites de contraintes historiques | **CONFIRMED** |

### 1.4 Ce qui n'est PAS écrivable aujourd'hui, même pour la tête signée

| Trou | Source | Verdict |
|---|---|---|
| **Nom, type et largeur de la colonne de CONNEXION** | grep sur les 5 documents : la pièce centrale du modèle n'est jamais appelée autrement que « la colonne de CONNEXION ». `SPEC-M-G.md:249` : « *Cette spec ne nomme pas les colonnes de M-A* » | **CONFIRMED** |
| **Type, CHECK et défaut EN BASE de `nature`** | `PLAN §8` avertissement (4) le reconnaît ; `SPEC-M-G §3.1` pose la question `nature='agent'` dans le CHECK et la marque **« À SIGNER »** | **CONFIRMED** |
| **`M-G` doit étendre DEUX CHECK, pas un** | Catalogue : `brain_sessions_status_valid` (032) borne à `{open,ended,abandoned}` **en plus** de `brain_sessions_terminal_state_valid` (037). Les documents ne nomment que le second. Trois casses d'attestation, pas une : md5 `4f21eff9…` **et** `9abfd0c6…`, **plus** `expected_session_constraint_fragments` (3 littéraux codés en dur) | **CONFIRMED** |
| **Formulation de la phrase-covenant par nature** | ADR §0ter (d) dit « réécrite », pas « comment ». `test_session_covenant_docstrings_anchor.py` rougira — geste Red voulu | **CONFIRMED** |
| **Rang de la tête dans le couloir** | `SPEC-M-G §8 point 6` : « **Un dossier de séquencement des candidates 046+ est en cours et doit être soumis et signé avant qu'une seule ligne de migration soit écrite** » — c'est le présent document | **CONFIRMED** |

---

## 2. DEUX ORDRES DE PASSAGE

### Préalable commun aux deux : le grain, relu à la lettre

**La règle interdit DEUX TÊTES EN VOL (mergées mais non appliquées). Elle n'interdit PAS qu'une tête porte plusieurs objets.** Vérifié dans le dépôt : la 037 porte tout un lifecycle, la **041 porte trois colonnes sur onze tables**, la **043 porte douze triggers sur six tables**. Les têtes multi-objets sont la **norme** de ce dépôt, pas une exception. **CONFIRMED.**

Et l'argument de l'opérateur lui-même, ADR §0ter.1 : *« Deux têtes ne signifient donc pas "deux petits pas" mais deux rendez-vous de production séquentiels, avec entre les deux exactement la fenêtre que l'argument fonctionnel condamne. Le procédural, appliqué ici, produit le risque qu'il est censé réduire. »* Ce raisonnement est **transférable** à tout regroupement où la séparation crée une fenêtre incohérente — il ne l'est **pas** à un regroupement de pure commodité.

### Base historique des estimations de durée

| Mesure | Valeur | Verdict |
|---|---|---|
| Cadence des 4 dernières bascules | 042 le 08-08, 043 **et** 044 le 08-10, 045 le 08-16 — **4 têtes en 8 jours** | **CONFIRMED** |
| Fenêtre commit→prod | 68 min (042) ; même matin (043, 044, 13 min d'écart) ; **prod avant commit** (045, sur demande opérateur) | **CONFIRMED** |
| Estimation du runbook `22189c08` | `estimated_duration = "45-90 min hors application"`, **`execution_count = 0`** — jamais enregistré comme exécuté | **CONFIRMED** (mesuré en base) |
| **Aucune des 4 bascules n'a régénéré les assets** | mtime `v4.json` et `v4-pgrestore.sql` = **2026-08-01**, inchangés depuis | **CONFIRMED (première main)** |
| Contre-exemple « deux têtes en vol » | 038+039 mergées le 08-01, appliquées le 08-03, mesurées le 08-04 → **2-3 jours** | **CONFIRMED** |

> **Caveat majeur sur toute estimation ci-dessous.** La cadence de 2 jours/tête est mesurée sur **quatre têtes qui n'ont régénéré aucun asset**. Elle ne borne donc **pas** une tête porteuse d'attestation — dont le coût réel est **UNVERIFIABLE**, sans précédent depuis le 2026-08-01. Les durées sont des **estimations**, pas des mesures.

---

### ORDRE A — « au fil de l'eau » : un candidat = une tête = un rendez-vous

**Séquence** : C1 → C2 → C3 → C4 → C5 → C6 → C7 → C8 → C9 → C10 → C11 → C12 (→ C13 si la revue du 09-03 le réveille, → C14 si Q4 tranche).

| Métrique | Valeur | Fondement |
|---|---|---|
| **Rendez-vous de production** | **12 minimum, 14 au plafond** | Décompte du §1 |
| **Versions de contrat DR** | **12 à 14** (v5, v6, v7…) si S1 = « le contrat suit la tête » | §0.1 : `alembic_head` épinglé ⇒ chaque tête invalide le contrat |
| **Durée plausible** | **10 à 16 semaines calendaires** | 12 têtes × (½ journée de travail + fenêtre prod ~1 h), **sérialisées** par la règle 3 ; mais 12 régénérations d'attestation dont **aucune n'a de précédent chiffré** ⇒ borne haute non bornable |

**Risques :**
1. **Le coût dominant est le contrat DR, pas les migrations.** `test_v4_json_is_the_exact_v3_delta` **dérive** `v4.json` de `v3.json` par un delta codé en dur dans `_expected_v4()` et assert la sérialisation **octet pour octet**. Régénérer = **réécrire une fonction de test**, pas éditer un nombre. Douze fois. **CONFIRMED (lecture de `test_recovery_contract_v4.py:44-84`).**
2. **La porte `25/25` du runbook reste infranchissable pendant toute la traversée** — elle l'est déjà (§0.1). Douze rendez-vous, c'est douze occasions de la contourner, donc de désarmer par habitude un gate de reprise après sinistre.
3. **Fenêtre d'incohérence fonctionnelle** entre C1 et C2 : nulle (C1 est déjà le regroupement qui la supprime). Mais entre C3 et C4 (checkpoints livrés, brouillons pas encore), l'axe traçabilité reste à moitié ouvert plusieurs semaines.
4. **Douze revues du pin**, dont **une seule est substantielle** (C5). Onze revues rituelles diluent celle qui compte — mode de panne connu du dépôt.

**Avantages réels :** chaque downgrade reste unitaire ; un échec en prod n'emporte qu'un objet ; le grain procédural n'est jamais discuté.

---

### ORDRE B — regroupement par affinité de coût

**Critère de regroupement, énoncé explicitement :** deux candidats peuvent partager une tête si et seulement si **(a)** ils régénèrent le **même** mécanisme d'attestation, ou **(b)** leur séparation crée une fenêtre fonctionnellement incohérente — **et** si **(c)** leurs downgrades peuvent échouer ensemble sans qu'un fail-closed de l'un empêche le rollback légitime de l'autre.

| Tête | Contenu groupé | Affinité invoquée | Test (c) | Verdict |
|---|---|---|---|---|
| **046** | **C1** = M-A + M-G | **(b)** — M-A seule fait naître des sessions `agent` sans état terminal atteignable (ADR §0ter.1). **Déjà signé** | Deux fail-closed (`intent` non NULL ; lignes `closed_inactive`) — acceptés par la signature | **CONFIRMED (signé)** |
| **047** | **C2** = M-B | Seule à toucher `knowledge_sources` et la parité de CTE. Aucune affinité avec les autres | — | **PROPOSÉ** |
| **048** | **C3 + C4** = M-C + M-E | **(a)** — deux tables neuves ⇒ **une seule** régénération de `table_set` en 4 endroits au lieu de deux. **(b) faible** : les checkpoints sans le pool de brouillons laissent l'axe savoir à moitié ouvert | **RISQUE** : le fail-closed de M-E (`staged` non résolus) bloquerait un rollback de M-C. **Test (c) NON SATISFAIT par défaut** — exige des downgrades indépendants par table | **PROPOSÉ SOUS RÉSERVE** |
| **049** | **C5** = M-D | Seule à poser un trigger, seule à déclencher la revue dure, seule avec un ordre de rollout dérogatoire. **Ne peut RIEN partager** | — | **CONFIRMED (isolement obligatoire)** |
| **050** | **C8 + C9 + C12** = `thinking_tokens` + compteur sweep + champs de pilotage tickets | **(a)** — trois `ADD COLUMN` nullable, zéro empreinte de colonne, zéro danse, downgrades `DROP COLUMN` nus **strictement indépendants** | **SATISFAIT** — trois DROP indépendants, aucun fail-closed | **PROPOSÉ (le regroupement le plus sûr du dossier)** |
| **051** | **C11** = journal d'accès durable | Table(s) neuve(s) + choix de conception non tranché. Pourrait rejoindre 048 si S3 arrive à temps | — | **PROPOSÉ** |
| **052** | **C7** = dimension embedding | Isolée : son coût réel n'est pas le DDL mais un **harnais de test inexistant** (les tests de 045/040/039 sont du **parsing de source**, `tests/integration/conftest.py` lance `alembic upgrade head` une fois par session avec l'env par défaut). Exige soit un planificateur pur `_plan(rows, dim) -> list[str]`, soit une fixture à seconde base | — | **PROPOSÉ** |
| **hors file** | **C6** (Q8), **C10** (sans porteur), **C13** (revue 09-03), **C14** (Q4) | — | — | — |

| Métrique | Valeur | Fondement |
|---|---|---|
| **Rendez-vous de production** | **7** (046 à 052), **6** si C11 rejoint 048 | Tableau ci-dessus |
| **Versions de contrat DR** | **7** si S1 = « le contrat suit la tête » ; **1** si S1 = « le contrat décrit une forme » | §0.1 |
| **Durée plausible** | **5 à 9 semaines** | 7 têtes, dont 3 lourdes (046, 048, 049) et 1 triviale (050) |

> **Numéros amendés.** Les rangs de ce tableau, de ces métriques **et des Risques qui suivent** sont ceux **proposés le
> 2026-08-20**, avant le dégroupage signé par S6 et avant l'insertion de la 048
> (`attribution_mode`, décision `1b742dc7`). Le rang effectif de chaque candidat se lit dans
> le bloc d'amendement en tête. Le CONTENU des lignes — affinité invoquée, test (c), réserve
> sur C3+C4 — n'est PAS amendé.

**Risques :**
1. **Downgrade tout-ou-rien** sur les têtes groupées. Neutralisé sur 050 (trois DROP indépendants), **non neutralisé** sur 048 — c'est la réserve. (Rangs du 2026-08-20 : ce « 048 » désigne le groupe M-C+M-E, **pas** la `048_attribution_mode` réellement mergée, dont le downgrade est propre.)
2. **Une tête groupée est une tête plus grosse à écrire** : plus de surface de test au moment où la fenêtre entre merge et bascule doit rester courte.
3. **Perte de lisibilité du journal des migrations** : « 050 » ne raconte plus une histoire mais trois.
4. **Le regroupement 050 est une commodité, pas une nécessité** — il ne satisfait que le critère (a) au sens faible (« aucun asset » n'est pas « le même asset »). Il doit donc être **signé comme commodité assumée**, pas justifié comme contrainte.

**Réponse à la question posée — « les candidats qui régénèrent les mêmes assets ou dansent la même vue peuvent-ils partager une tête sans violer le grain ? »**
**OUI, sans violer le grain écrit** : la règle porte sur les têtes *en vol*, pas sur le contenu d'une tête, et le dépôt livre déjà des têtes multi-objets (041, 043). **MAIS le partage n'est légitime que si le test (c) passe** — sinon on échange un coût de rendez-vous contre un rollback impossible, ce qui est un mauvais échange dans un couloir dont toute la doctrine est le fail-closed. **CONFIRMED.**

---

## 3. RECOMMANDATION UNIQUE

> **Adopter l'ORDRE B, avec la tête M-C + M-E dégroupée par défaut (donc un rendez-vous de plus — 9 depuis l'amendement `1b742dc7`, voir le bloc en tête) tant que le test (c) n'est pas démontré ; et TRANCHER LA DOCTRINE DU CONTRAT DR (signature S1) AVANT d'écrire la première ligne de la 046.**

**Trois raisons, par ordre de poids :**

1. **S1 domine tout le reste.** Si le contrat DR suit la tête, l'Ordre A coûte 12 mints de contrat et l'Ordre B en coûte 7 — un facteur 1,7. Si le contrat décrit une forme (`alembic_head` dérivé plutôt qu'épinglé, exactement ce que `test_alembic_env.py:254-259` fait déjà ailleurs dans ce dépôt en écrivant *« Le head est DÉRIVÉ, pas épinglé : l'invariant est une seule tête, pas la tête vaut N »*), les deux ordres coûtent **un seul** mint. **Décider S1 change le coût du couloir entier plus que ne le change le choix de l'ordre.**
2. **L'Ordre B respecte le grain écrit** et applique au reste du couloir le raisonnement que l'opérateur a lui-même signé pour C1.
3. **La file réelle n'est pas homogène** : une tête (C5) doit être isolée, une (050) peut être triviale, une (C1) est signée. Un ordre uniforme les traite mal toutes.

### 3.1 PREMIÈRE TÊTE À ÉCRIRE : la **046 = M-A + M-G**

Elle est **signée** ; le dossier ne la choisit pas, il en fixe le rang (premier) et la checklist.

### 3.2 CHECKLIST EXACTE DE LA 046

**Étape 0 — préalables NON techniques, avant la première ligne (tous bloquants) :**
- [ ] **S1 tranchée** (doctrine du contrat DR) — sinon la tête ne sait pas quel asset produire.
- [ ] **S2 signée** : nom, type, largeur, nullabilité de la colonne de CONNEXION.
- [ ] **S3 signée** : type, CHECK et défaut EN BASE de `nature`.
- [ ] **S4 signée** : `nature = 'agent'` **dans** le CHECK, ou garantie applicative seule (`SPEC-M-G §3.1`, marqué « À SIGNER »).
- [ ] **S5 signée** : `SPEC-M-G.md` dans son ensemble (statut actuel : proposition).
- [ ] **Rejouer** `select version_num from alembic_version` — ne jamais recopier `045`.
- [ ] **Rejouer** `git rev-list --all --objects | awk '$2 ~ /046/'` : le couloir doit être vide **à cet instant**.

**Contenu de la migration `046_*.py` :**
- [ ] 4 colonnes nullable sur `brain_sessions`, hors CHECK 037, **sans backfill** (doctrine 040/041 : `NULL` = « avant ») : `started_by_actor VARCHAR(64)` (aligné sur `MAX_ACTOR_LENGTH=64` et `access_log.actor`), `last_observed_at TIMESTAMPTZ`, `intent VARCHAR(500)`, `nature` (type par S3).
- [ ] Colonne de CONNEXION (nom/type par S2) + **`CREATE UNIQUE INDEX … WHERE status = 'open'`**. **Le partiel est impératif** : un unique plein brûlerait la connexion à vie dès la première auto-fermeture — et `closed_inactive` fait précisément sortir des lignes de `status='open'` en masse chaque nuit.
- [ ] 4ᵉ branche `closed_inactive` sur `brain_sessions_terminal_state_valid` — `captured_knowledge_ids` **sans aucune contrainte** (c'est le point entier).
- [ ] **`brain_sessions_status_valid` élargie aussi** — le CHECK 032 rejette le 4ᵉ statut *avant* le CHECK terminal. **Mesuré : `status` est `varchar(20)` et `closed_inactive` fait 15 caractères — aucun élargissement de type, donc aucune danse vue/GRANT.**
- [ ] `revision = "046"`, `down_revision = "045"` — la contiguïté `{001..head}` est assertée (`test_documentation_contract.py:150-168`).

**Pin et gardes de tête (MÊME COMMIT) — 13 points, re-grepés au head 045 :**
- [ ] `src/brain_v42/maintenance/plan_index_repair_store.py:63` → `"046"`
- [ ] `tests/unit/test_plan_index_repair_head_pin.py` (dérive la tête, assert `len(heads)==1`)
- [ ] **Revue écrite exigée par le docstring du pin** — courte ici : la 046 ne touche **aucune** de `indexed_plans`, `indexed_plan_chunks`, `project_contexts`, ne pose aucun trigger, aucune colonne NOT NULL sans défaut. **L'écrire quand même** (règle 6).
- [ ] `README.md:238` — `migration 046`
- [ ] `docs/ARCHITECTURE.md:4` — `migrations 001–046 defined` (**tiret CADRATIN**, chaîne différente de README)
- [ ] `docs/MCP_TOOLS.md:10` — `Migration 046 …` (la garde teste `.lower()`)
- [ ] `docs/OPERATIONS.md:118` **et `:138`** (prose descriptive de la tête courante — **deuxième occurrence non listée par R1**)
- [ ] `docs/SCHEMA.md` — **cinq littéraux** : `« 46 révisions (001 → 046) »`, `« | 046 | »`, `« La cible du dépôt est 046. »`, `« La révision 046 est la tête du dépôt. »`, `« Un schéma neuf au head 046 contient 32 tables public »` (le compte de tables **ne change pas** : aucune table neuve)
- [ ] `tests/unit/test_recovery_contract.py:279` → `assert script.get_heads() == ["046"]`
- [ ] `tests/unit/test_recovery_contract_v4.py:444` → `« La cible du dépôt est 046. »`
- [ ] **RENOMMER** `test_repository_head_045_is_documented_…` → `…_046_…` (`test_documentation_contract.py:1772`) — le nom porte la tête, c'est le garde-fou anti-dérive
- [ ] `tests/unit/test_documentation_contract.py:1790` → `assert _repository_head() == "046"`
- [ ] **`tests/unit/test_model_column_width_contract.py`** : inscrire `brain_sessions` dans `WRITE_MODELS_BY_TABLE` — **absent aujourd'hui**, et son commentaire dit qu'un modèle non inscrit **n'est PAS audité**. `intent VARCHAR(500)` et `started_by_actor VARCHAR(64)` arrivent avec un rail Pydantic. **Oublier cette inscription ne rougit RIEN.**

**Hors commit (fichiers gitignorés / non gardés en CI) :**
- [ ] `CLAUDE.md` (`.gitignore:74`) — **pas seulement de l'hygiène** : `test_documentation_contract.py:1817` assert derrière `if CLAUDE:`, donc `pytest tests/unit` **reste ROUGE en local** tant que CLAUDE.md ne dit pas `migration 046`.
- [ ] `docs/ARCHITECTURE.md:6` et `docs/PLAN_INDEX_REPAIR_RUNBOOK.md:142` portent `Repository target: 040` — **5 révisions de retard, aucun test ne les garde** (`grep 'Pending schema delivery' tests/` = 0). Trancher leur sort.
- [ ] `docs/ARCHITECTURE.md:4` et `:128` portent `31 PG tables modeled` — **aucun test ne l'assert** (`grep 'PG tables' tests/` = 0). Inerte pour la 046 (0 table neuve), **mordant pour C3/C4/C5/C6/C11**.

**Assets d'attestation — six mécanismes de casse, tous certains :**
- [ ] `observed_column_fingerprints['brain_sessions']` (`v4.sql:437-470`, comparé `:565`) — le md5 porte `is_nullable` et l'ordre complet des colonnes ; +5 colonnes le change.
- [ ] `COLUMN_DEFINITION_MD5['brain_sessions'] = 'bf4c2a47e41aa69872119982b390f45a'` (`test_recovery_contract_v3.py:170-172`).
- [ ] `expected_session_indexes` (`v4.sql:404-412`) — liste **FERMÉE** contrôlée **deux fois** (`:665` absent-ou-md5-divergent, `:687` présent-hors-liste). 3 index → 4.
- [ ] `SESSION_INDEX_DEFINITION_MD5` — ces md5 vivent **littéralement dans QUATRE fichiers** (v3, v3-pgrestore, v4, v4-pgrestore).
- [ ] `expected_session_constraints` (`v4.sql:279-280`) — **DEUX** md5 bougent (`4f21eff965e8da6178bb2d1030fc03f8` **et** `9abfd0c69ce694043e32e1935d17ff4f`) — **plus** `expected_session_constraint_fragments` (`v4.sql:283-337`), qui code en dur les **trois** littéraux de statut : un quatrième état exige une quatrième ligne.
- [ ] **`ops/recovery/brain-v42-v4.json`** — le **troisième** asset, nommé **zéro fois** dans les cinq documents de conception. `catalog_counts.indexes` : **129 mesuré → 130** avec l'index de la 046. **Sa régénération n'est PAS une édition** : `test_v4_json_is_the_exact_v3_delta` le dérive de `v3.json` et assert l'octet — il faut réécrire `_expected_v4()`.
- [ ] **Parité de CTE** entre `v4.sql` et `v4-pgrestore.sql` : écart autorisé = exactement `{observed_artifact_constraints, observed_session_constraints}` (`test_recovery_contract_v4_pgrestore.py:29-33`).
- [ ] **Anomalie de posture à corriger au passage** : `ops/recovery/brain-v42-v4.sql` est en **0644** quand ses onze frères sont en **0600** — le runbook impose 0600.

**Downgrade :**
- [ ] Fail-closed si un `intent` non NULL existe (jugement humain) — sauf purge de canary **ou** opt-in `-x` nommé, gabarit `039_project_context_timestamp_cas.py:337-339`.
- [ ] Fail-closed si des lignes `closed_inactive` existent.
- [ ] **Enseigner l'état au downgrade 037→036** — sa garde (`037:229-266`) teste `status <> 'ended'` et réinstalle `_TERMINAL_STATE_V3` par `ADD CONSTRAINT`, qui valide les lignes existantes. **Correction de prémisse : il n'est pas MENTEUR, il devient IMPOSSIBLE.** L'ADR §8 (« perd des sessions terminales sans le dire ») est **REFUTED** sur lecture du source. Une spec écrite sur cette prémisse ajouterait un garde-fou là où il en existe un et manquerait le vrai travail.
- [ ] Éprouver **upgrade ET downgrade** sur `brain_test`, jamais `brain` (runbook `22189c08`, étape 4).

**Rendez-vous de production :**
- [ ] `BRAIN_ALEMBIC_ALLOW_PROD=1 python -m alembic upgrade head`
- [ ] **MESURER** `select version_num from alembic_version` — jamais recopier (cette phrase a menti 3 jours puis 10 jours dans ce dépôt).
- [ ] **Redémarrer `brain-mcp-http`** — requis : `PgBrainSessionRepo` est câblé `server.py:372-379`. Pas de dérogation d'ordre (celle de M-D ne s'applique pas ici).
- [ ] Canary HTTP v4.
- [ ] Redater les mesures dans README / ARCHITECTURE / MCP_TOOLS / SCHEMA (runbook étape 7).
- [ ] Vérifier l'alignement pin ↔ prod après merge (runbook étape 8).

**Geste Red qui ouvre la livraison :** `test_session_covenant_docstrings_anchor.py` **doit rougir** (covenant réécrit par nature, ADR §0ter (d)). C'est voulu, pas un dégât.

---

## 4. SIGNATURES OPÉRATEUR NÉCESSAIRES

### 4.1 Bloquantes AVANT la première ligne de la 046

| # | Signature demandée | Pourquoi elle bloque | Options |
|---|---|---|---|
| **S1** | **Doctrine du contrat de récupération.** Le contrat suit-il la tête (v5, v6, v7… un mint par bascule) ou décrit-il une **forme** (`alembic_head` dérivé, comme `test_alembic_env.py:254-259` le fait déjà) ? | **§0.1** : sans réponse, la 046 ne sait pas quel asset produire, et l'attestation reste rouge quoi qu'il arrive. **Domine le coût du couloir entier.** Contradiction ouverte : le PLAN §8 exige de **régénérer v4** ; le ticket `eb067b57` pose « **ne pas modifier les actifs v4** » en **NON-OBJECTIF** et demande v5 | (i) v5 par tête ; (ii) v5 unique + `alembic_head` dérivé ; (iii) découpler l'attestation du head |
| **S2** | **Nom, type, largeur, nullabilité de la colonne de CONNEXION** | Pièce centrale du modèle, jamais nommée dans aucun des 5 documents | — |
| **S3** | **Type, CHECK et défaut EN BASE de `nature`** | `PLAN §8` avertissement (4) le reconnaît explicitement | — |
| **S4** | **`nature = 'agent'` dans le CHECK terminal, ou garantie applicative seule** | `SPEC-M-G §3.1`, marqué « À SIGNER ». Recommandation de la spec : la garantie dure | dure / souple |
| **S5** | **`SPEC-M-G.md` dans son ensemble** (statut : « PROPOSITION SOUMISE À SIGNATURE ») — dont le nom `closed_inactive`, `next_focus IS NULL`, `focus_outcome IS NULL` | Le contenu de la moitié M-G n'est pas acquis | — |
| **S6** | **Le présent ordre de passage** | Exigé par `SPEC-M-G §8 point 6` | Ordre A / Ordre B / hybride |

### 4.2 Bloquantes plus tard, pas maintenant

> **Échéances renumérotées** par l'amendement `1b742dc7` : elles appliquent le dégroupage signé
> par S6 puis l'insertion de la 048. Aucune signature de ce tableau n'est levée, retirée ni
> assouplie — seul son rang de rendez-vous change.

| # | Signature | Bloque | Échéance |
|---|---|---|---|
| **S7** | `SPEC-checkpoint.md` (écrite, non signée ; porte une « Contradiction interne de l'ADR, à trancher ») | C3 | avant 049 |
| **S8** | **SPEC du pool de brouillons** — FK et `ON DELETE`, durée de vie, plafond, tool de signature hors session. **Non écrite** | C4 | avant 050 |
| **S9** | Regroupement C3+C4 : **le test (c) est-il satisfait** (downgrades indépendants par table) ? | forme de 049/050 | avant 049 |
| **S10** | Portée INSERT de M-D (`create` et la branche INSERT de `get_or_create` échappent à `AFTER UPDATE`) + **fenêtre d'attestation rouge assumée et datée** pendant que le trigger est désactivé | C5 | avant 051 |
| **S11** | **Q8** — droit de réattribution journalisée vs orphelinage comme prix de la preuve | C6 | quand l'opérateur voudra |
| **S12** | `thinking_tokens` : **colonne** ou **renoncement écrit** (l'issue (2) ne consomme pas de tête) | C8 | avant 052 |
| **S13** | Le regroupement du trio `ADD COLUMN` (C8+C9+C12) est une **commodité assumée**, pas une contrainte | forme de 052 | avant 052 |
| **S14** | **Réconcilier ou supersédér la décision `24495130`** (« la 046 dimension part en second lot », `active`, non supersédée) avec la levée du gate par l'ADR §5 pt 5 | C7 | avant 054 |
| **S15** | C11 : rétention `access_log` **ou** journal d'agrégats ; dimensionner le stockage | C11 | avant 053 |
| **S16** | C10 (G7) et C9 (compteur sweep) : **les instruire ou les écarter formellement** — deux propositions sans porteur écrit, nommées par la spec la plus récente | file | avant 052 |
| **S17** | Revue datée du poison-pill A — **2026-09-03**, ticket `191b2dba` | C13 | 2026-09-03 |

### 4.3 CE QUI PEUT PARTIR SANS L'OPÉRATEUR

> **CORRECTION MESURÉE, 2026-08-20 — un item de ce tableau et deux items de la checklist
> §3.2 reposent sur un claim FAUX. Ce n'est pas une signature : c'est une réfutation par
> mesure, posée par le worker qui a exécuté l'item et l'a vu échouer.**
>
> **CE QUI EST RÉFUTÉ.** Le claim *« `docs/ARCHITECTURE.md:6` et
> `PLAN_INDEX_REPAIR_RUNBOOK.md:142` portent `Repository target: 040` — 5 révisions de
> retard, **aucun test ne les garde** »* est faux sur sa seconde moitié. **Deux tests
> épinglent ces chaînes au caractère près** :
> - `tests/unit/test_recovery_contract_v4.py:433` — `assert "Repository target: 040." in architecture`
> - `tests/unit/test_recovery_contract_v4.py:460` — `assert "Repository target: 040. This section claims no live head; measure it." in section`
>
> **Ce ne sont pas des dates périmées : ce sont des CONTRATS DE SECTION.** Ils garantissent
> que ces passages ne revendiquent **aucune tête vivante** — la discipline même que ce
> dépôt s'impose. Éditer les deux fichiers a fait rougir les deux tests ; la modification a
> été **revertée**, gardes vertes.
>
> **POURQUOI LE CLAIM EST PASSÉ** : le grep de vérification cherchait
> `'Pending schema delivery'` et `'PG tables'` dans `tests/`. Les tests, eux, épinglent
> `"Repository target: 040."`. **Angle mort de motif** — exactement le piège « un
> recensement peut être faux trois fois, chacune par un angle mort de grep différent ».
> Compter par plusieurs motifs indépendants, jamais par un seul.
>
> **LE SECOND ITEM (`31 PG tables`) EST INVALIDE POUR UNE AUTRE RAISON, et la nuance
> compte.** Son claim sur les tests est **exact** — aucun test n'assert cette prose. Mais
> l'item est sans objet parce que **le nombre est JUSTE** : `len(METADATA.tables)` = **31**,
> et la base porte **31** tables hors `alembic_version` (deux motifs indépendants, mesuré le
> 2026-08-20). Il n'y a rien à redater. *À savoir tout de même : une table neuve n'est pas
> sans garde — l'attestation épingle la LISTE (`table_set`, 32 entrées avec
> `alembic_version`), et `test_schema_indexes_027.py:350` pose un plancher
> `len(METADATA.tables) >= 18`.*
>
> **ET LA DOC N'EST PAS PÉRIMÉE** : `docs/SCHEMA.md:3` dit « La cible du dépôt est 045 » et
> `docs/ARCHITECTURE.md:4` dit « migrations 001–045 defined ». Les « 040 » sont des énoncés
> **de section**, portant sur la livraison que leur section documente.
>
> **CE QUI RESTAIT VALIDE DE L'ITEM : le `chmod 0600` seul.** Le runbook l'impose (l. 45,
> *« mode `0600` »*) et `ops/recovery/brain-v42-v4.sql` était le seul écart sur onze assets.
> **Fait le 2026-08-20 : 11/11 uniformes.** Git ne suit pas ce mode (seul le bit exécutable
> l'est), donc aucun commit n'en découle.
>
> **La ligne « Redater… » du tableau ci-dessous est donc INVALIDE**, et les items
> correspondants de la checklist §3.2 (l. 225-226) sont à retirer de la 046.



| Travail | Justification |
|---|---|
| **Écrire la SPEC du pool de brouillons** (S8) et la soumettre | Écrire une proposition n'est pas la signer. C'est le dernier gate documentaire de Phase 0 |
| **Rejouer l'attestation `ops/recovery/brain-v42-v4.sql` en READ ONLY** contre la production et publier le reçu réel | Lecture seule. Établit `23/25` ou autre **par mesure** au lieu de l'inférence actuelle — corrige un chiffre que trois documents donnent différemment (25/25, 24/25, 22/25) |
| **Corriger le mode `0644` → `0600`** de `ops/recovery/brain-v42-v4.sql` | Le runbook l'impose déjà ; aucune décision nouvelle |
| **Redater `docs/ARCHITECTURE.md:6` et `PLAN_INDEX_REPAIR_RUNBOOK.md:142`** (`Repository target: 040`) et les compteurs `31 PG tables` | Chiffres faux qu'aucun test ne garde ; pure correction |
| **Reporter le §0ter dans le PLAN §8** (partiellement fait à 22:05 : ligne M-G à jour, note (3) sur les comptes de têtes toujours périmée) | Transcription d'une décision déjà signée |
| **Recompter les têtes du couloir** — critère de sortie de Phase 0 explicitement dû | Le présent dossier le fait : **12 candidats en file, 4 hors file** |
| **Écrire le harnais de test de C7** (planificateur pur `_plan(rows, dim) -> list[str]`) | Du code de test, sans migration ; débloque C7 sans consommer de tête |
| **Instruire le trou de `WRITE_MODELS_BY_TABLE`** pour `brain_sessions` | Garde silencieuse ; l'inscrire ne demande aucune décision |

---

## 5. ANGLES MORTS

| # | Angle mort | Impact | Verdict |
|---|---|---|---|
| **AM1** | **Aucun test ne confronte `v4.json` au catalogue VIVANT.** La garde est une identité `v3 + delta` : le fichier peut être **parfaitement cohérent avec lui-même ET faux sur la production** — c'est exactement l'état d'aujourd'hui. La casse est bruyante en CI mais **la fausseté est silencieuse** | Le contrat DR peut mentir sans qu'aucune suite ne rougisse | **CONFIRMED** *(corrige l'anomalie A1 de l'enquête 1, qui annonçait une casse silencieuse : c'est l'inverse — casse bruyante, fausseté silencieuse)* |
| **AM2** | **Le corpus de conception est NON TRACKÉ et bouge en continu** (ADR 21:43 puis 22:05, PLAN 22:05, deux SPEC apparues à 21:50). Trois lectures du même fichier ont donné trois vérités le même jour | Toute conclusion « le document ne dit pas X » périme en heures | **CONFIRMED** |
| **AM3** | **Aucun test n'a été exécuté** — par aucune des deux passes de vérification ni par moi (`ModuleNotFoundError: No module named 'neo4j'`, venv non activé). Toutes les affirmations sur les gardes sont des **lectures de source** | Une garde pourrait avoir dérivé sans qu'on le voie | **CONFIRMED (limite assumée)** |
| **AM4** | **L'attestation n'a pas été rejouée.** Le troisième échec (`view_column_mismatches=1`) est repris du ticket `eb067b57` ; seuls `alembic_head` et `indexes` sont de première main | Le reçu réel est peut-être pire que 23/25 | **UNVERIFIABLE** |
| **AM5** | **La revue dure du pin pour M-D n'a pas été instruite.** Elle doit démontrer que le trigger scopé `UPDATE OF current_focus` est inerte pour les `UPDATE plan_scan_paths` du repair (`plan_index_repair_store.py:294-308`, `:560-584`). **Non fait** | C5 non écrivable tant que cette revue n'existe pas | **CONFIRMED** |
| **AM6** | **Le trigger de M-C n'a pas été confronté à `expected_runtime_user_triggers`** (liste fermée de 13, indexée par `expected_runtime_trigger_tables`). Raisonné, pas rejoué | C3 pourrait porter un 5ᵉ mécanisme d'attestation non budgété | **UNVERIFIABLE** |
| **AM7** | **`table_set` est DÉRIVÉ de METADATA moins une exclusion codée en dur.** Ajouter une table à `db/tables.py` sans l'ajouter à `post_contract_tables` (`test_recovery_contract.py:281-289` **et** `_v2.py:33-39`) rougit — mais le mécanisme n'est décrit nulle part dans les documents de conception | C3/C4/C5/C6/C11 découvriront ce couplage à l'écriture | **CONFIRMED (première main)** |
| **AM8** | **G7 (C10) et le compteur de sweep (C9) n'ont AUCUN porteur écrit**, alors qu'ils sont nommés par `SPEC-M-G.md:256` comme concurrentes du couloir. Quatre angles négatifs chacun | Deux candidats de la file existent uniquement dans une phrase | **CONFIRMED** |
| **AM9** | **Le périmètre réel de G7 dépasse ses trois tables.** `project_key` est nullable sur **huit** objets ; NULL réels mesurés : `brain_entities` 13/5 590, `search_log` 246/2 321, `dream_runs` 873/1 672 (**sémantique** : « écrit avant la 042 »). Le « trou déjà vide » n'est vrai que sur les trois tables choisies | Un G7 élargi n'est pas gratuit | **CONFIRMED** |
| **AM10** | **Le défaut de `project_key` qui a réellement mordu est la NORMALISATION**, pas la nullité (learning `7bc821a1` : `brain_v42` underscore ⇒ projet fantôme). G7 ferme un trou vide et laisse ouvert celui qui saigne | Argument contre C10 | **CONFIRMED** |
| **AM11** | **Aucune visibilité systemd** dans cet environnement — l'unité `brain-mcp-http` et le minuteur dream n'ont pas pu être inspectés. Le « rendez-vous de production » repose sur le texte et le câblage du code | Durée réelle du redémarrage non bornée | **UNVERIFIABLE** |
| **AM12** | **Toute mesure de population de sessions est périmée en heures** : 472 lignes / 8 `open` aujourd'hui contre 467-469 / 29 la veille. Les chiffres du PLAN (24/29, 21 balayables) doivent être rejoués, jamais recopiés | Les critères de sortie fondés sur ces chiffres sont à recalibrer | **CONFIRMED** |
| **AM13** | **C15 (contrat codex v2) écarté sans comparaison nom par nom** des 9 vues demandées aux 10 livrées | Un candidat DDL pourrait s'y cacher | **UNVERIFIABLE** |
| **AM14** | **`C13` : le dossier de la poison-pill traite `tickets.extraction_status` comme acquis.** Six vues dépendent de `tickets` (une seule projette `extraction_status`), et `ticket_extraction_attempts.status` — `varchar(10)`, CHECK propre, **zéro dépendance de vue** — offre une échappatoire à toute danse, quelle que soit la largeur du nom | La seule danse vue/GRANT du carnet est peut-être évitable par choix de table | **CONFIRMED** |
| **AM15** | **`C14` (Q4, `parent_key`) n'a AUCUNE ligne au tableau §8** : la Phase 4.6 n'est pas budgétée. Huitième tête surprise possible | Le compte de 12 candidats est un plancher | **CONFIRMED** |
| **AM16** | **Six worktrees actifs** sur ce dépôt. Le CLAUDE.md documente une règle GitNexus née d'un worktree périmé résolu comme index canonique (893 commits de retard). Un `impact` lancé pour instruire une tête doit vérifier `npx gitnexus list` d'abord | Analyse d'impact potentiellement résolue contre un index périmé | **CONFIRMED** |
| **AM17** | **Le dénominateur de tout recensement bouge plus vite que le recensement.** Trois valeurs pour le même comptage G7 le même jour (4 232 / 4 235 / 4 237) ; trois valeurs pour le même reçu d'attestation (25/25, 24/25, 22/25 — et ma mesure dit ≤23/25). **Le mode de panne de ce dossier n'est pas l'oubli d'un candidat, c'est la confiance dans un chiffre non rejoué** | S'applique au présent document | **CONFIRMED** |

---

## 6. CORRECTIONS APPORTÉES AUX TROIS ENQUÊTES (traçabilité)

| Affirmation source | Verdict | Correction mesurée |
|---|---|---|
| « SPEC M-G et SPEC-checkpoint non écrites » (enquête 1) | **REFUTED** | Les deux existent depuis 21:50 le 2026-08-20, « PROPOSITION SOUMISE À SIGNATURE ». Un seul gate documentaire reste : le pool (M-E) |
| « L'ADR ne reflète pas la fusion, saute de §0bis.5 à §1 » (enquête 2) | **REFUTED** | §0ter existe (l.368+), 4 occurrences de `2026-08-20`. ADR réécrit à 21:43 puis 22:05 |
| « Le PLAN §8 porte encore `M-G — à séquencer — non tranché` » (enquête 2, verif) | **REFUTED** | `PLAN:1421` porte désormais « **UNE TÊTE AVEC M-A** (signé 2026-08-20) ». *Le `SPEC-M-G §8 pt 6` cite encore l'ancienne formulation — la spec est déjà périmée d'un cran sur ce point* |
| « Aucun test ne garde `v4.json` ⇒ casse silencieuse » (enquête 1, A1) | **REFUTED** | `test_v4_json_is_the_exact_v3_delta` le gèle octet pour octet. Le vrai défaut est inverse (AM1) |
| « Aucune trace de deux têtes en vol » (enquête 3) | **REFUTED** | Deux précédents : collision de révision `038` (2 fichiers, 35 min, 2026-08-01) et 038+039 mergées le 08-01 / appliquées le 08-03 |
| « Le runbook des sept gardes date de la veille de 042 » (enquête 3) | **REFUTED** | `created_at = 2026-08-09 11:14:52 UTC` — **lendemain** de 042 (08-08 18:03). Il n'a gouverné que 043/044/045 |
| « Le downgrade 037→036 perd des sessions sans le dire » (ADR §8, repris par les enquêtes) | **REFUTED** | Source `037:229-266` : il **RAISE**. Il devient impossible, pas menteur |
| « M-A = cinq colonnes nullable » (enquête 1) | **REFUTED** | `PLAN §8` : **quatre** nullable **+ la colonne de connexion traitée à part**, précisément parce que sa nullabilité n'est pas spécifiée. Compter cinq ferme par inadvertance une question ouverte |
| Ticket `b68b9692` absent des deux premières enquêtes | **AJOUTÉ** (C12) | Ouvert 2026-08-02, demande « champs structurés priorité, readiness, dépendances », précision READY mesurée 1/3 |
| « La danse vue/GRANT ne se rejoue sur aucun candidat » | **CONFIRMED, et renforcé** | Mesure supplémentaire : `status` est `varchar(20)`, `closed_inactive` fait 15 caractères ⇒ pas même un élargissement de type sur la tête signée |

---

**Fichiers de référence (chemins absolus)**
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/maintenance/plan_index_repair_store.py:63`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/tests/unit/test_plan_index_repair_head_pin.py:38-52`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/tests/unit/test_recovery_contract_v4.py:23,44-84,444`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/tests/unit/test_recovery_contract_v4_pgrestore.py:29-33`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/tests/unit/test_recovery_contract.py:279,281-295`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/tests/unit/test_documentation_contract.py:150-168,1772-1819`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/tests/unit/test_model_column_width_contract.py` (trou : `brain_sessions` absent)
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/ops/recovery/brain-v42-v4.json` (`alembic_head:039`, `indexes:128`)
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/ops/recovery/brain-v42-v4.sql:267-282,404-412,437-470,917,1789-1791` (mode 0644, anomalie)
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/alembic/versions/037_session_lifecycle_v4.py:229-266`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/alembic/versions/045_dream_run_model_width.py`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/maintenance/session_sweep.py:79-115`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/docs/design/refonte-projets-sessions/ADR-refonte-projets-sessions.md:368-489` (§0ter)
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/docs/design/refonte-projets-sessions/PLAN-phase-0-4.md:1415,1421`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/docs/design/refonte-projets-sessions/SPEC-M-G.md:170-270`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/docs/design/refonte-projets-sessions/SPEC-checkpoint.md`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/docs/PLAN_INDEX_REPAIR_RUNBOOK.md:63,66,142,154-155,583`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/docs/ARCHITECTURE.md:4,6,128` (têtes et compteurs périmés, non gardés)

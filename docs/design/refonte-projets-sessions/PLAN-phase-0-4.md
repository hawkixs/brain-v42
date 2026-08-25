# PLAN Phases 0→4 — Refonte PROJETS + SESSIONS par accrétion instrumentée

- **Date** : 2026-08-18
- **Statut : CIBLE À PROPOSER — RIEN NE DÉMARRE SANS LE CADRAGE EXPLICITE DE
  L'OPÉRATEUR** (ticket d'ancrage `d30cf6e5` : « NE PAS DÉMARRER sans cadrage explicite
  de l'opérateur »). Aucune ligne de code n'existe, aucune migration n'est écrite. Ce
  plan est l'exécution proposée de l'ADR jumelle
  `docs/design/refonte-projets-sessions/ADR-refonte-projets-sessions.md` (statut
  PROPOSED) ; il se lit seul.
- **Origine** : synthèse de trois propositions d'architectes jugées par un panel de
  trois lentilles — base = proposition B (majoritaire), faiblesses relevées par les
  juges corrigées, greffes de A et C intégrées. Rien ici n'est neuf sans preuve
  (ticket, learning, code vérifié le 2026-08-18) ou juge derrière.
- **CADRAGE OPÉRATEUR DU 2026-08-19 — cinq réponses acquises, ce plan en est modifié.**
  La source unique est l'**ADR jumelle, §0**. Résumé opposable : Q10 = la liste B couvre,
  mais l'ordre vient désormais de l'axe **« traçabilité du savoir »** ; Q12 = **piste
  (a)**, deux natures de session ; Q1 = dérivée de Q12 ; Q15 (neuve) = **nouvel état
  terminal**, migration **M-G** sur le CHECK 037 ; séquencement = **P0 → P1 → P2 → P4.4
  → P3**, et **Q6 promue en critère de sortie de Phase 0**. Restent bloquantes pour la
  Phase 0 : **Q2, Q3, Q6, Q14**.
- **SESSION 2 DU MÊME JOUR — LA PHASE 0 EST DÉBLOQUÉE.** Source unique : **ADR §0bis**.
  `start` devient **AUTOMATIQUE**, sur la clé **`(projet, connexion)`** — ce qui **inverse
  le défaut de nature** (désormais `agent` ; un geste unique et rétroactif, le *claim*,
  promeut en `operator`). **Q9 réglée par la mesure : les subagents HÉRITENT**, sans tag,
  et le **corollaire de Q1 se dissout** (« exactement un » devient vrai par construction).
  **Q2 = `from_project`** ; **Q14 = (a) élargir `knowledge_sources`** ; **Q3 = stockage de
  la proposition + forme du payload du ticket**, ses sous-décisions (a) et (b) étant
  **dissoutes** ; **Q6 = acceptée**, brouillons non signés survivants dans un pool en
  attente. **Timeout : une session `operator` n'est JAMAIS fermée par inactivité** ; les
  traçantes le sont à **4 h SIGNÉES** (2026-08-20) — comme **seuil d'ÉLIGIBILITÉ** au
  balayage nocturne, jamais comme délai de fermeture : latence réelle pire cas ≈ 28 h.
  *AMENDÉ le 2026-08-20 — ADR §0ter fait foi (décision `c5160259`).* Restent ouvertes, sans
  bloquer : Q4, Q5, Q7, Q8, Q11, Q13.

---

## 0. Contexte minimal pour lecture autonome

brain-v42 : serveur MCP « second cerveau » (Python 3.12, FastMCP 3, SQLAlchemy 2
async, PostgreSQL 16 + pgvector, Neo4j en index de relations). Lifecycle sessions v4
(migrations 032+037) : sept commandes explicites, machine d'états double rail
(CHECK SQL + Pydantic), CAS de focus non bloquant, ledger d'attribution exclusif
(PK `knowledge_id`), replays idempotents. **Covenant** : `start`/`list`/`resume`/
`capture`/`heartbeat`/`end`/`abandon` sont des commandes explicites de l'utilisateur ;
seule exception serveur, le sweep `auto_stale_7d` — **armé en DRY depuis le
2026-08-18, par décision opérateur** (mesuré ce jour dans le drop-in systemd
`killswitches.conf` : `BRAIN_DREAM_SWEEP_ENABLED=true`, `BRAIN_DREAM_SWEEP_DRY_RUN=true`,
18 fantômes balayables mesurés ce jour-là ; le WET n'a jamais été armé). **Ce « 18 »
est déjà périmé** : re-mesuré le 2026-08-19, **29 sessions `open` dont 21 balayables
>7 j** (sur 467 lignes) — daté, périssable, à rejouer avant toute décision d'armement,
jamais à recopier. Une version antérieure
de ce plan écrivait « jamais armé » — recopie d'un état du 2026-08-16, contre sa
propre règle R3, corrigée. Toute proposition qui fait ouvrir/fermer une session par un
agent, un hook ou un client est un changement de covenant à trancher par l'opérateur.
**Réponse opérateur déjà établie** (`2bd14b24`, 2026-08-06) : une session sert à la
traçabilité du savoir et le cycle de vie doit devenir « automatique, pas déclaratif ».
**La piste est désormais CHOISIE — ce paragraphe disait « aucune choisie » et c'était
vrai jusqu'au 2026-08-18 inclus.** Cadrage du 2026-08-19, Q12 : **piste (a), deux
natures de session** — agent traçante auto-fermée sans rituel, opérateur avec rituel
(ADR §0.1 et D11). Ce plan n'est donc plus « un transitoire compatible avec les trois
pistes » : il a une cible. Deux conséquences qu'il porte mal encore, et qu'il faut lire
avant de l'exécuter — le covenant est **amendé** pour la nature agent (C1), et la
machine d'états 037 est **étendue** par la migration M-G (Q15, ADR §0.4), alors que tout
le reste du plan est écrit sous l'hypothèse inverse.

**Douleurs prouvées** (détail et preuves dans le DOSSIER et l'ADR) : B1 fantômes
subagents (39 d'un coup, critique) ; B2 heartbeat menteur dans les deux sens
(critique) ; B3 capture à 18 %/attribution à 34 % (haute) ; B4 tickets non capturables
+ lot fail-closed en bloc ; B5 fenêtre de capture rigide ; B6 focus sans garde de
contenu ; B7 aucun checkpoint ; B8 `X-Brain-Session` mort (contrainte — **RE-MESURÉE ET CONFIRMÉE le
2026-08-19 sur Claude Code 2.1.234**, verdict inchangé :
`docs/upstream/2026-08-19-b8-session-join-rejeu.md`. Ce passage annonçait un re-jeu
« programmé » ; il est **fait**) ; B9
`client_key` libre ; B10 drift fantôme (fermée, à ne jamais régresser) ; **B11
sous-projets colon — 86 artefacts sans aucun run nocturne, et 533 sans consolidation
croisée** (le « 479 hors consolidation » de la spec `dbb7c5ce` datait du 2026-08-08 et a
été re-mesuré : deux des six clés colon sont au pool dream depuis le 2026-08-10, soit
84 % de cette masse) ; B12 système projets sans doc ; B13
erreur de capture indifférenciée (ids listés, une seule raison agrégée — vérifié) ;
B14 acteur non persisté sur la session (vérifié). La cartographie douleur → phase →
mesure est au §6.

**Ordre de priorité — fixé par l'opérateur le 2026-08-19, il ne vient plus de la
gravité.** L'axe retenu est la **traçabilité du savoir** : **B3, B4, B5 en tête**. La
cotation « critique / haute / moyenne » ci-dessus reste la cotation *de gravité* et
garde sa valeur descriptive, mais elle **ne commande plus le séquencement**. C'est ce
qui déplace la Phase 4.4 juste après la Phase 2 et fait descendre la Phase 3 (voir le
bandeau d'en-tête et l'ADR §0.3).

**Principe directeur** : chaque objet est un FAIT observé par le serveur ou un JUGEMENT
déclaré par l'humain, jamais les deux. Le serveur observe et prépare ; l'opérateur
signe.

---

## 1. Règles transverses (non négociables)

**R1 — Pin Alembic (contrainte DURE, ticket `c60d023d`).**
`_REQUIRED_ALEMBIC_HEAD = "045"` (`src/brain_v42/maintenance/plan_index_repair_store.py:63`,
gardé par `tests/unit/test_plan_index_repair_head_pin.py`) fait fail-closed le
plan-index repair en production au moindre écart. Donc :
1. Chaque migration de ce plan est livrée dans **LE MÊME COMMIT** que le lot ci-dessous.
   **Troisième version de cette recette : les deux premières se déclaraient « le
   couplage complet » et ne l'étaient pas.** Inventaire refait grep en main, gardes
   nommées une à une :
   - bump du pin + son test (`tests/unit/test_plan_index_repair_head_pin.py`) ;
   - **README** et **MCP_TOOLS** : chaîne `migration {head}`
     (`test_documentation_contract.py:1816,1819`) ;
   - **ARCHITECTURE** : chaîne **différente**, `migrations 001–{head} defined`, tiret
     cadratin compris (même test, l.1815). Suivre l'ancienne recette à la lettre sur
     ARCHITECTURE rendait le test rouge ;
   - **CLAUDE.md** : à mettre à jour (contrat de travail), mais **il ne peut être dans
     AUCUN commit de ce dépôt** — `git check-ignore -v CLAUDE.md` → `.gitignore:74`,
     absent de `git ls-files`, et le test garde son assertion derrière `if CLAUDE:`
     (l.25-32 : « CLAUDE.md is tracked only in the private archive »). En CI la clause
     est muette ;
   - **SCHEMA.md** : compte de tables (M-C…M-F en ajoutent chacune une, « 32 tables
     public » et le compte de révisions bougent) **et** la phrase « La cible du dépôt
     est {head}. », doublement épinglée par
     `tests/unit/test_recovery_contract_v4.py:437-446` — doublon que le dépôt
     documente lui-même comme « facile à manquer quand on inventorie les gardes » ;
   - **`docs/OPERATIONS.md:118`** (« The repository migration target is 045. ») —
     absent de toutes les listes antérieures ;
   - `tests/unit/test_recovery_contract.py:279` : `assert script.get_heads() == ["045"]`,
     littéral **dans un test nommé pour la révision 031**. Rouge à **chaque** bump ;
   - `tests/unit/test_recovery_contract.py:292` et `…_v2.py:33-39` : le `table_set` gelé
     est re-dérivé de `METADATA.tables` moins un ensemble d'exclusion codé en dur.
     **M-C, M-D, M-E et M-F ajoutent chacune une table** ⇒ quatre bumps sur six font
     rougir ces deux tests, qui ne parlent ni de pin ni de sessions ;
   - **régénération de `ops/recovery/` (point 5)** pour M-A, M-B et M-D — et **des DEUX
     assets v4, pas d'un seul** : `brain-v42-v4.sql` ET `brain-v42-v4-pgrestore.sql`
     (voir point 5, « quatrième mécanisme » et « deux assets ») ;
   - le **renommage du test-garde head-nommé** (`test_repository_head_045_is_documented_…`).
   (« La même série de commits » reste refusée — fenêtre de désynchronisation.)
2. **Jamais deux têtes en vol** (mergées mais non appliquées) simultanément — celles
   de ce plan entre elles, ET vis-à-vis de la 046 (point 6).
3. Rollout de chaque tête : appliquer → **mesurer** `select version_num from
   alembic_version` (jamais recopier — cette doc a déjà menti trois jours puis dix
   jours sur ce chiffre) → redémarrer le serveur MCP → canary.
   **Exception nommée, M-D** : entre l'`upgrade` et le redémarrage, le processus vivant
   exécute encore le code pré-M-D, qui n'écrit aucune ligne d'historique. Le constraint
   trigger différé ferait donc avorter au COMMIT **tout `brain_session_end` en
   `focus_outcome=applied`** — fail-closed, session laissée ouverte —, et M-D n'a ni
   killswitch ni downgrade praticable. La migration crée donc le trigger **désactivé**
   (`ALTER TABLE … DISABLE TRIGGER`) ; le runbook l'active **après** le redémarrage MCP,
   en un geste opérateur nommé, canary compris. Sans cette exception, la fenêtre
   d'indisponibilité est imposée par la règle du plan elle-même. **Prix à annoncer** :
   toute la fenêtre désactivée est une fenêtre d'attestation `ops/recovery/` **rouge**
   (point 5 — elle exige `tgenabled = 'O'`), donc courte, datée, et la régénération des
   assets se pose dans le geste d'activation, pas dans celui de l'upgrade.
4. **La revue que le pin exige vaut pour NOS têtes.** Message d'échec de
   `test_plan_index_repair_head_pin.py:45-52` : « review what {head} changes on the
   tables the repair writes (**indexed_plans, indexed_plan_chunks, project_contexts**) —
   new triggers, new constraints or new NOT NULL columns … ». **M-D pose un constraint
   trigger sur `project_contexts`.** Son commit porte la revue écrite, au format du
   docstring du pin, montrant que le trigger est scopé `UPDATE OF current_focus` et donc
   inerte pour les `UPDATE plan_scan_paths` du repair
   (`plan_index_repair_store.py:294-308` et `:560-584`).
5. **L'attestation de récupération `ops/recovery/` casse sur trois des six têtes —
   par QUATRE mécanismes, et sur DEUX assets.** Elle n'était nommée nulle part dans ce
   plan ; deux passes plus tard elle l'est, mais deux fois trop court. Le runbook exige
   d'elle « all statuses are pass » et « exactly 25 unique checks »
   (`tests/unit/test_recovery_contract_v4.py:480-486`).

   **DEUX assets v4, pas un — l'inventaire ne nommait que `brain-v42-v4.sql`.** Constat
   du 2026-08-19 : `ops/recovery/` contient aussi **`brain-v42-v4-pgrestore.sql`**, la
   variante destinée à une base **restaurée par `pg_restore`** (celle des preuves
   isolées : `docs/PLAN_INDEX_REPAIR_RUNBOOK.md:62,122-123`). Ce document, l'ADR et le
   DOSSIER la nommaient **zéro fois** — mesuré : `grep -c pgrestore` rendait `0/0/0` sur
   les trois. Elle n'est pourtant ni morte ni décorative :
   - `tests/integration/db/test_recovery_contract_v4_execution.py:106` est **paramétré
     sur les deux** (`@pytest.mark.parametrize("asset", ["brain-v42-v4.sql",
     "brain-v42-v4-pgrestore.sql"])`) et les exécute contre une base réelle, en
     transaction READ ONLY, en vérifiant les 25 checks et l'absence de mutation ;
   - `tests/unit/test_recovery_contract_v4_pgrestore.py:29-33` impose la **parité des
     CTE** : l'écart autorisé est exactement `{observed_artifact_constraints,
     observed_session_constraints}`, et `not (_cte_names(live) - _cte_names(pgrestore))`
     interdit qu'un CTE existe côté live sans exister côté pgrestore ;
   - `tests/unit/test_recovery_contract_v4.py:273-279` exige que le runbook distingue les
     portes pgrestore et live.
   Et elle porte **les mêmes structures** que M-A/M-B/M-D cassent : mesuré le 2026-08-19,
   `brain-v42-v4-pgrestore.sql` donne 12 lignes portant `expected_runtime_user_triggers`,
   `observed_column_fingerprints`, `expected_artifact_constraints` ou `knowledge_sources`
   (15 en comptant `expected_session_indexes`), aux mêmes emplacements logiques que la
   variante live. **Conséquence directe** : suivre R1.1, R1.5 et le §8 à la lettre
   régénérerait **un** des **deux** assets v4, et la parité des CTE ferait rougir
   `test_recovery_contract_v4_pgrestore.py` au premier CTE ajouté côté live. Partout où
   ces documents écrivent « régénérer `ops/recovery/` », lire **les deux assets v4**.
   C'est la **quatrième** itération du même défaut dans ce dossier — inventaire de gardes
   déclaré complet et incomplet (pin seul, puis pin + quatre documents, puis
   `ops/recovery/` oubliée, maintenant la moitié d'`ops/recovery/`) : le mode de panne
   n'est pas l'oubli d'une garde, c'est la **confiance dans l'exhaustivité** d'une liste
   qu'on n'a pas re-grepée.

   Les mécanismes, tête par tête :
   - **M-A** : `observed_column_fingerprints` md5-e la liste ordonnée complète des
     colonnes de `brain_sessions` ⇒ `session_column_mismatches > 0` ; md5 également
     épinglé en unitaire (`…_v3.py:170`, `"bf4c2a47…"`) ;
   - **M-B** : `expected_artifact_constraints` code en dur le CHECK à **sept** valeurs
     ⇒ `artifact_constraint_mismatches > 0` ;
   - **M-D** : `expected_runtime_user_triggers` est une liste **fermée de treize triggers
     sur cinq tables**, dont **sept sur `project_contexts`** (`v4.sql:533-548`, relue le
     2026-08-19 ; les sept sont identiques en production) et `runtime_trigger_mismatches`
     compte les triggers **inattendus** sur `expected_runtime_trigger_tables`, qui
     contient `project_contexts` ⇒ tout trigger ajouté fait échouer le contrôle.
     **Collision avec R1.3, vue le 2026-08-19** : R1.3 fait naître ce trigger
     **désactivé**, or la jointure des triggers attendus porte
     `AND observed_user_trigger.tgenabled = 'O'` (`v4.sql:913-918`) — un trigger
     *attendu mais éteint* compte comme mismatch exactement comme un trigger absent.
     Hors liste il est inattendu, dans la liste il est éteint : **aucun ordre de
     régénération ne rend l'attestation verte tant que le trigger est volontairement
     désactivé.** Donc : régénération séquencée **avec le geste d'activation** et non
     avec l'`alembic upgrade` ; fenêtre désactivée = fenêtre d'attestation rouge, courte,
     datée et annoncée ; et le « DISABLE TRIGGER en guise d'interrupteur » évoqué au
     Rollback de cette phase rouvre cette fenêtre rouge à chaque usage — ce n'est pas un
     killswitch gratuit.
   - **M-A (bis) — `expected_session_indexes`, le QUATRIÈME mécanisme, non recensé
     jusqu'ici.** Ces documents n'en recensaient que trois (colonnes, CHECK d'artefacts,
     triggers) ; il en existe un de plus, et il vise précisément `brain_sessions`.
     `ops/recovery/brain-v42-v4.sql:404-412` fige la liste **FERMÉE** des index de
     `brain_sessions` — trois entrées `(index_name, definition_md5)` :
     `brain_sessions_pkey`, `idx_brain_sessions_project_status_started`,
     `uq_brain_sessions_project_client`. Elle est contrôlée **deux fois** dans
     `session_constraint_mismatches` : `:665` compte les index attendus absents ou dont
     `md5(pg_get_indexdef(...))` a bougé, et `:687` compte les index **présents sur la
     table et absents de la liste**. Un index ajouté sur `brain_sessions` fait donc
     `session_constraint_mismatches > 0`, attendu à 0 comme les trois autres compteurs.
     Doublé côté unitaire par `SESSION_INDEX_DEFINITION_MD5`
     (`tests/unit/test_recovery_contract_v3.py:164-168`) et
     `test_v3_pins_the_exact_session_index_set` (`:488`) — et les trois md5 sont
     littéralement présents dans **quatre** assets (`v3`, `v3-pgrestore`, `v4`,
     `v4-pgrestore`, mesuré le 2026-08-19). **Ce mécanisme ne se déclenche que si une
     tête ajoute un index** ; il est donc dormant tant que M-A se limite à trois colonnes
     nullable — et c'est exactement pour cela qu'il faut le recenser AVANT d'instruire la
     décision d'index de §3.3/§3.6, pas après.
   Les **quatre** compteurs sont attendus à **0** : sans régénération, le check
   `brain_runtime_032_036_037` passe en `fail` **dès la première tête et le reste** — et
   la régénération porte sur les **deux** assets v4 (live et pgrestore), pas sur un seul.
   **Et « trois des six têtes » n'est vrai que sous une hypothèse qu'il faut écrire** :
   celle où aucune tête de ce plan n'ajoute d'index sur `brain_sessions`. Si la décision
   d'index de §3.3/§3.6 (émetteur D5 sur `started_by_actor`) est prise, deux issues :
   l'index voyage **dans M-A**, et alors le compte de têtes ne bouge pas mais M-A casse
   **deux** structures au lieu d'une (colonnes *et* index) — la colonne « Attestation »
   du §8 doit le dire ; ou il est différé dans **sa propre tête**, et alors c'est
   **quatre têtes sur sept**, dans un couloir qui en interdit deux en vol (R1.2). Ne pas
   trancher revient à laisser le §8 mentir sur le périmètre de régénération.
   **Et une part ne se répare pas en régénérant** : `knowledge_sources` (v4.sql:1083-1090)
   est l'UNION des **six** tables de capture — pas les tickets — et
   `artifact_source_matches` exige `source.project_key = session.project_key`. Le premier
   artefact `'ticket'` et la première capture `pk → pk:child` produisent des
   `artifact_source_mismatches` **permanents**, qu'aucune purge de canary ne rattrape.
   Ce sont deux **critères de sortie de phase** de ce plan qui rendraient la preuve de
   restauration fausse : **Q14 tranche avant M-B.**
6. **La 046 (dimension embedding) est en projet sur ce couloir, pas écrite** :
   `ls alembic/versions/` s'arrête à `045_dream_run_model_width.py` (vérifié). Le ticket
   `c60d023d` la qualifie de « non urgent » et énumère un travail non planifié (révision
   terminale convergente, NO-OP DDL, passe 1 fail-closed, killswitch, reconstruction
   HNSW, harnais de test inexistant). **Le gate « M-A ne merge pas tant que la 046 n'est
   pas appliquée » est LEVÉ** : il mettait six têtes en otage d'un ticket non urgent et
   non écrit, alors que le risque qu'il invoquait (deux heads en un rendez-vous) est
   déjà couvert par le point 2, qui vaut dans les **deux** ordres. Les têtes de ce plan
   restent notées M-A…M-F, numéros **relatifs** : celle des deux séries qui arrive la
   première prend le numéro suivant, l'autre attend son application mesurée.
   Récapitulatif au §8.

**R2 — TDD strict** (CLAUDE.md) : chaque incrément suit Red (test qui échoue pour la
bonne raison) → Green (minimum) → Refactor → commit atomique Conventional Commits.
Coverage ≥ 60 %. Vert avant commit : `pytest tests/unit`, `ruff check`,
`ruff format --check`, `mypy src/`.

**R3 — Killswitches** : tout comportement runtime nouveau naît derrière son propre
flag, livré **fermé**, avec un test prouvant le fermé-par-défaut, et son état
production est **mesuré** (drop-in inspecté, processus inspecté — leçon du
client-activity mesuré « ARMED » alors que le défaut code est `false`). Récapitulatif
au §8.

**Correction — R3 tranchait d'avance la sous-question qu'il prétendait laisser
ouverte.** Une version antérieure écrivait : « un tool explicite nouveau (checkpoint)
n'a pas de killswitch — c'est une commande utilisateur, gated par décision d'opérateur ».
C'est poser comme ACQUISE la branche que Q3(a) déclare OUVERTE, et s'en servir pour
justifier l'absence de flag. Or l'artefact livré est **identique sous les deux
réponses** : rien à armer, rien à désarmer, et rien côté serveur ne distingue un appel
d'agent d'un appel d'humain (B8 : `X-Brain-Session` est mort ; `X-Brain-Agent` est un
projet, déclaré par le client). Comme le checkpoint **rafraîchit `last_heartbeat_at`**,
seul signal du sweep vivant (`pg_brain_session.py:520-522`, prédicat heartbeat-seul,
sweep armé DRY mesuré dans le drop-in), un agent qui checkpointe seul maintient sa
session vivante indéfiniment — le faux-vivant que `2bd14b24` condamne — **sans violer
une seule contrainte livrée**, et rend le critère 4.3 « zéro abandon d'une session à
checkpoint récent » auto-satisfiable par ses propres écritures. **Règle corrigée** : le
tool lui-même n'a pas de flag (c'est bien une commande), mais **son effet heartbeat en
a un** — `BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT`, livré **fermé** (§8bis), à moins
que Q3(a)/(b) ne retire l'effet du contrat. Livrer l'effet sans l'un des deux, c'est
armer un changement de covenant par omission.

**R4 — Largeur acteur** : toute colonne acteur nouvelle est `VARCHAR(64)`, alignée sur
`MAX_ACTOR_LENGTH = 64` (`src/brain_v42/provenance.py:23`) et `access_log.actor`
`String(64)` — correctif du panel, aucune des trois propositions ne l'avait vérifié.

**R5 — Liaison d'identité** : toute liaison session↔activité passe par
`(project_key, started_by_actor)`. **Jamais par `access_log` seul** : cette table n'a
pas de colonne projet (vérifié `src/brain_v42/db/tables.py` — `entity_type,
entity_id, access_type, accessed_at, actor`). Règle d'ambiguïté : 0 ou ≥2 sessions
candidates ⇒ zéro écriture + compteur. Le serveur ne devine jamais.

**R6 — Chiffres périssables** : toute mesure citée ici (18 %, 34 %, 39 fantômes, masse
colon…) est datée et périssable ; la Phase 0 les re-mesure toutes ; on ne recopie
jamais, on rejoue le script. **Et un chiffre exact peut porter une inférence fausse** :
les « 10/59 contextes à focus NULL » étaient justes et ne prouvaient pas ce qu'on leur
faisait dire (§5.1) ; les « 479 artefacts hors consolidation » étaient justes le
2026-08-08 et périmés dix jours plus tard (§6, 4.6). Re-mesurer inclut donc **relire ce
que la mesure dit**, pas seulement rejouer la requête.

---

## 2. Phase 0 — Mesurer, ancrer, documenter, soumettre (ZÉRO mutation)

Aucune migration, aucun changement de comportement, aucun flag.

### Contenu

1. **Baseline scriptée et datée** — requêtes SQL read-only versionnées dans
   `docs/design/refonte-projets-sessions/` + un script rejouable qui produit un
   snapshot JSON daté :
   - sessions `open` / stale >24 h / >7 j ; distribution des `client_key` ;
   - taux de capture (sessions fermées capturantes / fermées) et d'attribution
     (artefacts attribués / créés) depuis 30 j — recalcul des 52/291 et 226/661 ;
   - masse par clé colon — **re-mesurée le 2026-08-18, et le remède a bougé** : 533
     artefacts sur six clés, dont `red-shrik:agent` (312) et `red-lab:architect` (135)
     **déjà dans le pool dream depuis le 2026-08-10** (drop-in lu). Reste 86 artefacts
     sur quatre clés `red-lab:*` sans aucun run. Le « 479 / 20,2 % » de la spec
     `dbb7c5ce` datait du 2026-08-08 et ne doit plus être cité tel quel ;
   - **état du focus, ce qui est mesurable et ce qui ne l'est pas.** Mesurable
     aujourd'hui : le compte de contextes à focus NULL **avec leur `focus_revision` et
     leur `focus_updated_at`** — c'est cette ventilation, et elle seule, qui distingue
     « jamais écrit » d'« effacé » (mesure du 2026-08-18 : 10/59 NULL, **tous** à
     révision 0 et jamais datés ⇒ zéro effacement observé) ; plus la distribution des
     `focus_revision` et l'âge des `focus_updated_at`. **NON mesurable, et c'est la
     correction** : une version antérieure annonçait « les écritures de `current_focus`
     **par écrivain** sur 30 j » comme livrable de baseline et critère de sortie.
     Aucune source ne peut la produire — `project_contexts` ne garde qu'un compteur
     sans auteur (`focus_revision`), un dernier changement écrasé (`focus_updated_at`)
     et `updated_at` ; il n'existe aucune table d'historique (`project_focus_history`
     est précisément ce que la Phase 3 crée) ; `access_log` n'enregistre que des
     lectures et est **purgé à chaque flush** (mesuré à 0 ligne) ; les logs structlog
     ne portent que `project_context.upserted`/`created`, sans site ni rétention. Cette
     ventilation devient donc un **résultat de la Phase 3**, pas son préalable ;
   - part des appels HTTP portant un `X-Brain-Agent` normalisable (dimensionne
     `started_by_actor` et la règle « exactement un ») ;
   - **coût du statement de l'émetteur D5 sur le chemin chaud** — mesure **ajoutée** le
     2026-08-19, parce que la décision d'index de §3.3 en dépend et que rien ne la
     produisait. `EXPLAIN (ANALYZE, BUFFERS)` du statement de §3.6 — filtre
     `(status, project_key, started_by_actor)` **plus** la sous-requête corrélée de
     comptage — joué contre `brain_sessions` en production (lecture seule ;
     `started_by_actor` n'existant pas encore, se substituer une colonne de même
     sélectivité, ou mesurer le squelette `status = 'open' AND project_key = :pk` et
     déclarer l'extrapolation). À publier avec la cardinalité du jour : 467 lignes,
     29 `open` le 2026-08-19, et les **trois** index réels de la table
     (`brain_sessions_pkey`, `uq_brain_sessions_project_client`,
     `idx_brain_sessions_project_status_started`), dont **aucun** ne couvre l'acteur.
     Critère : la conclusion — index nécessaire ou non — est **écrite**, et si elle est
     « oui » elle voyage dans M-A (R1.2) en sachant qu'elle casse
     `expected_session_indexes` (R1.5, quatrième mécanisme).
     ✅ **MESURÉ ET CONCLU le 2026-08-19** (`baseline/README.md`), et **la question a
     changé d'objet** : sous la clé `(projet, connexion)` du cadrage (ADR §0bis.2),
     l'émetteur D5 ne filtre plus sur l'acteur — `started_by_actor` sort du chemin chaud
     et **n'a pas besoin d'index**. Une question neuve la remplace : **la colonne de
     connexion, elle, DOIT être indexée** — l'`EXPLAIN` montre qu'une égalité non couverte
     force un Seq Scan de toute la table (63 buffers contre 2), sur le chemin le plus chaud
     qui soit. Forme retenue : **index UNIQUE**, pour que « exactement un par construction »
     soit imposé par la base et non seulement affirmé par le design. Détail mesuré : la
     sous-requête corrélée de comptage consommait **27 des 36 buffers** du plan et
     **disparaît** sous la clé de connexion ;
   - **la population que l'observation toucherait, calculée SANS écrire** — remplace
     l'instrument `access_log` retiré (§3.5) : pour chaque couple
     `(project_key, acteur)` observé sur une fenêtre glissante, le nombre de sessions
     `open` correspondantes (0 / 1 / ≥2). C'est la seule mesure honnête disponible pour
     instruire **Q1** avant tout armement, et elle exerce le résolveur de projet par
     tool que la Phase 1 doit livrer.
     **Cette phase ne part PAS de zéro — un ordre de grandeur existe déjà et n'était
     cité nulle part** (constat du 2026-08-19). Le ticket `7ffe0e8a` porte, sous
     « Mesure du 2026-08-16 (périssable) », la ligne « **Simultanées : `auto-discord` 6,
     `red-arena` 3, `claude-dev-pc`/`red-lab` 2** » : c'est déjà une mesure **partielle**
     de la population « ≥2 sessions ouvertes », celle que R5/N2 érigent en critère et que
     4.1 met en tête de ses mesures de sortie. Partielle en deux sens, à dire tous les
     deux : elle compte par **projet**, non par couple `(projet, acteur)` — donc c'est un
     **plafond**, l'ambiguïté réelle étant plus petite dès que deux acteurs distincts se
     partagent un projet —, et `started_by_actor` n'existant pas encore, **elle ne peut
     pas être raffinée rétroactivement** : la ventilation par acteur ne commencera qu'avec
     M-A. Re-mesurée le 2026-08-19, la même requête rend `auto-discord` 8, `brain-v42` 4,
     `red-arena` 4, puis `datalake-v1`, `red-gift`, `claude-dev-pc`, `red-lab` à 2 — soit
     **24 des 29 sessions `open` dans un projet qui en porte au moins deux**. Autrement
     dit, sous la règle « exactement un », la non-observation n'est pas un cas de bord :
     c'est, au plafond, la majorité du parc. Chiffres datés et **périssables** — la
     Phase 0 les rejoue, elle ne les recopie pas ;
   - seuil de cardinalité attendu des futures tables (checkpoints, staged) pour fixer
     les plafonds de sortie des phases suivantes.
2. **Tests d'ancrage** (pin du comportement actuel, pour que le chantier ne régresse
   rien sans le voir) :
   - canonicalisation stricte/tolérante + alias (protège B10) ;
   - `_validate_captures` : all-or-nothing, fenêtre exacte (projet strict,
     `created_at >= started_at`), **forme actuelle du message d'erreur épinglée** — on
     la changera en Phase 1 en changeant d'abord le test (Red) ;
   - CAS de `end` : `applied` et `conflict`, fermeture garantie dans les deux cas ;
   - sweep UN statement (épinglage textuel, sur le modèle de
     `test_dream_sh_global_phases_outside_loop.py`) ;
   - replays idempotents des quatre chemins (start, capture exacte, end persisté,
     abandon même reason) ;
   - phrase-covenant présente dans les sept docstrings — test écrit pour être
     étendu : la livraison du checkpoint (Phase 2) porte l'énumération à HUIT, et
     docstring du 8e tool, CLAUDE.md et ce test bougent dans le même commit que le
     tool ;
   - fermé-par-défaut des flags sweep existants re-prouvé.
3. **`docs/PROJECTS_SYSTEM.md`** (ferme B12) : le système projets de bout en bout — les
   quatre briques reliées (format, trois tables — `project_contexts` vient de la **001**,
   `projects`/`project_aliases` de la **033**, qui n'ajoute à la première que
   l'immuabilité et les alias par triggers —, convention colon, spec dream), la table
   des trois surfaces, et le **recensement du prédicat colon**. Ce recensement a été
   sous-compté **deux fois** : « l'unique exception » d'une première version, puis
   « trois exemplaires `src/` + deux vues » de la deuxième. Compte re-vérifié le
   2026-08-18, **cinq exemplaires `src/`** :
   `db/project_group_scope.py:24-26` ;
   `services/project_group_ticket_service.py:129-137` (copie SQL) **et `:164-167`
   (seconde copie, en Python, dans la même méthode `_lock_participants_scope`)** ;
   `services/proposal_service.py:377-383` ;
   **`repositories/pg_project_context.py:202-213`** (`get_keys_by_group`, variante
   `split_part`, invisible à un grep sur `not_like("%:%")`).
   Côté base : **sept vues vivantes** (mesuré :
   `codex_brain_entity_v1`, `codex_feature_artifact_v1`, `codex_feature_v1`,
   `codex_roadmap_curation_proposal_v1`, `codex_ticket_extraction_proposal_v1`,
   `codex_ticket_message_v1`, `codex_ticket_v1`), toutes issues de la **036** — deux
   corps de CTE recopiés (`_RED_KEYS_CTE:23-45`, `_BRAIN_RED_KEYS_CTE:205-227`), en
   `split_part(…) <> project_key AND split_part(…) IN red_base`. La 024 n'est pas un
   second objet vivant : la 036 remplace sa vue par `CREATE OR REPLACE`.
   **Trois formulations distinctes du même prédicat**, donc — la doc les nomme toutes.
   **Organisé par la grille fait/jugement** (greffe A via le panel). Dépôt public :
   aucune adresse réseau privée, aucun chemin hors dépôt, aucun secret.
4. **Spec checkpoint séparée** (`docs/design/refonte-projets-sessions/SPEC-checkpoint.md`)
   — livrable **ajouté**, exigé par l'audit du ticket `d04dc588` (« le plus petit lot
   admissible reste documentaire : 1. spec checkpoint séparée ; 2. décision produit
   explicite ; 3. contrat CAS/replay/heartbeat/end, migration/rollback, bornes de
   payload et tests de concurrence ; 4. approbation avant code »). Aucune phase ne le
   livrait. Elle doit trancher explicitement les **deux** divergences d'avec le MVP du
   ticket (stockage append-only vs snapshot+CAS ; et **forme du payload** —
   `progress` + `blocker|null` + `next_step` publiés ensemble « en un appel » contre
   `kind` mutuellement exclusifs + `note` unique), et le sort de l'effet heartbeat
   (Q3(a)/(b)). Sans elle, M-C n'est pas écrivable.
5. **Soumission à l'opérateur** : la liste B1–B14 pour confirmation/priorisation/
   complétion (« pas mal de choses qui ne me plaisent pas » n'est pas détaillé — il
   peut exister des irritants non ticketés), plus les questions ouvertes du §9 avec
   mention des tranches qu'elles bloquent.
6. **Re-jeu du spike B8 sur la version courante de Claude Code** — étape **ajoutée** le
   2026-08-19, parce que B8 est cotée « Haute (contrainte) » sur une mesure périmée et
   qu'aucune phase ne la rejouait. `docs/upstream/2026-08-06-claude-otlp-session-join.md`
   porte en tête « **Version mesurée : Claude Code 2.1.220** » ; `claude --version` rend
   **2.1.234** le 2026-08-19. Le spike lui-même conclut « Re-mesurer à chaque montée de
   version de Claude Code plutôt que de reprendre cette conclusion sur parole », et ses
   deux relais (`2bd14b24`, `7ffe0e8a`) répètent la consigne mot pour mot. **Ce que
   B8 dimensionne, et donc ce qui pend à ce fil** : D1 (`started_by_actor` comme identité
   de repli), D5 (`skipped{no_actor}` en stdio), D8 (liaison des brouillons), R5 et N2
   (règle « exactement un »), le résidu nommé de D6, et le **critère de sortie n° 1 de la
   Phase 4.1**.
   **Méthode** : rejouer le protocole du spike, pas une lecture de code — deux récepteurs
   jetables en loopback, `claude -p` avec `--mcp-config` dédié et `--strict-mcp-config`,
   et la question 1 seule (« `${CLAUDE_CODE_SESSION_ID}` s'expanse-t-il utilement dans un
   en-tête MCP ? »). Le volet OTLP du spike n'est pas rejoué ici : il n'entre dans aucune
   décision de ce plan.
   **Critère** : le résultat est écrit, daté et **versionné avec la version mesurée en
   tête**, quel qu'il soit. Deux issues, deux conséquences déclarées d'avance :
   *(a) inchangé* — `X-Brain-Session` toujours mort, B8 cesse d'être « cotée sur mesure
   périmée » et l'accrétion continue telle quelle ; *(b) changé* — un client sait
   désormais déclarer sa session, ce qui **n'invalide aucun livrable** (le repli
   `(projet, acteur)` reste correct) mais rouvre une option écartée par contrainte, à
   verser à Q1 et Q9 avant tout armement. Le risque est asymétrique dans ce sens-là, et
   c'est pourquoi la re-mesure est une étape de Phase 0 et non un gate.
   **À ajouter à la baseline du point 1** : la part des appels HTTP portant un
   `X-Brain-Session` **normalisable** (`provenance.normalize_session` → non-`None`),
   à côté de la part portant un `X-Brain-Agent` normalisable. Aujourd'hui la baseline
   mesure l'acteur et jamais la session — l'angle mort qui a laissé le spike vieillir de
   quatorze versions sans que rien ne le signale.

### Critères de sortie (mesurables)
- ✅ **SATISFAIT le 2026-08-19** — baseline livrée sous
  `docs/design/refonte-projets-sessions/baseline/` : `queries.sql` (12 mesures, **un
  seul statement** donc un instantané cohérent, chacune portant `proves` et
  `does_not_prove`), `explain.sql`, `snapshot.py` (rejouable en une commande) et le
  premier `snapshot-20260819T191613Z.json`. **Lecture seule imposée par le moteur**
  (`BEGIN READ ONLY`), prouvée dans les deux sens. La ventilation des écritures de focus
  par écrivain en reste explicitement exclue — elle sort de la Phase 3.
  **Trois résultats à ne pas manquer** : l'attribution 30 j est à **30,4 %** contre 34 %
  au 2026-08-16 — **B3 se dégrade, elle ne stagne pas** ; `client_key` rend **465
  distinctes pour 469 sessions** (ratio 1,01), ce qui chiffre B9 ; et les 10 contextes à
  focus NULL sont **tous** « jamais écrit », zéro effacement, ce qui confirme la prémisse
  de Q13.
- ✅ **SATISFAIT le 2026-08-19** — recensement d'abord, écriture ensuite. **Cinq des
  sept items étaient DÉJÀ épinglés** et n'ont pas été redoublés : canonicalisation
  (`test_project_key_canonical.py`, 20 tests), sweep en UN statement
  (`test_pg_brain_session_sweep.py:74`), fermé-par-défaut des flags
  (`test_dream_sh_sweep.py`), CAS `applied`/`conflict`
  (`test_brain_sessions_lifecycle.py`) et **les quatre replays idempotents**
  (`test_start_replays_same_project_client_key`, `test_capture_exact_retry_is_idempotent`,
  `test_end_exact_terminal_retry_is_idempotent_and_read_only`,
  `test_abandon_exact_retry_is_idempotent_and_read_only`).
  **Deux trous réels, comblés** (commit `0207209`, branche
  `test/phase0-session-anchors`) : la **phrase-covenant dans les sept docstrings**, qui
  n'avait **aucune** couverture — elle pouvait disparaître des sept sans qu'une suite ne
  rougisse — et la **forme actuelle de l'erreur de `_validate_captures`**, c'est-à-dire
  B13, épinglée pour que la Phase 1 la casse en Red d'abord.
  Le test des docstrings épingle **aussi** le nombre écrit en toutes lettres dans la
  docstring d'enregistrement : le checkpoint le portera à « eight », dans le même commit
  que le tool — friction voulue.
  **Les deux sont prouvés PAR MUTATION DE CONTRÔLE**, jamais par leur seule couleur :
  retirer la phrase d'un docstring, faire mentir le compte, retirer la borne
  `created_at >= started_at` de la fenêtre de capture, et remplacer la raison agrégée par
  des motifs par id — cette dernière mutation **simule l'arrivée de D2 et fait tomber
  exactement les deux tests conçus pour tomber en premier**. Sources restaurées bit à bit
  après chaque essai (`git diff src/` vide). Suite complète : **7985 passés, 55 skippés**,
  ruff + format + mypy propres.
- ✅ **SATISFAIT le 2026-08-19** — `docs/PROJECTS_SYSTEM.md` écrite et commitée
  (`e04df96`, branche `test/phase0-session-anchors`). Organisée par la grille
  FAIT/JUGEMENT comme exigé. **Le recensement du prédicat colon a été REFAIT, pas
  recopié** — et il a bien fallu : il avait été faux trois fois, la troisième par un
  grep de correction cherchant `":" in ` qui manquait la copie écrite `":" not in`.
  Compte vérifié : **cinq exemplaires `src/`, sept vues, trois formulations**, soit
  douze objets, dont deux dans la même méthode et un dans un module qui importe déjà le
  helper partagé sans l'utiliser (`proposal_service.py:17` contre `:377-383`, vérifié).
  **Dérive neuve, non gardée, nommée dans le document** : la regex de la clé existe en
  **dix-sept endroits sur seize fichiers**, dont **cinq assets `ops/recovery/`**, et
  aucun test ne relie `_KEBAB` au CHECK SQL — le test de la 033 épingle un littéral
  réécrit dans le test. L'élargir casserait la cohérence code/base ET la preuve de
  restauration sans qu'aucune suite ne rougisse. Contrôle dépôt public passé (aucune
  adresse privée, aucun chemin hors dépôt, aucun secret).
- `SPEC-checkpoint.md` relue et soumise avec Q3.
- ✅ **SATISFAIT le 2026-08-19** — spike B8 rejoué sur **Claude Code 2.1.234**, résultat
  écrit, daté et versionné avec la version mesurée en tête :
  `docs/upstream/2026-08-19-b8-session-join-rejeu.md`. **Issue (a) : verdict INCHANGÉ**,
  rien n'est invalidé, l'accrétion continue telle quelle. Les deux cas ont été joués — le
  faux positif du spike d'origine (environnement parent intact ⇒ identifiant du PARENT
  reçu et **accepté** par `normalize_session`) se reproduit à l'identique, et reste donc
  un piège actif pour toute tentative future. Joué **en premier** de la Phase 0, parce que
  Q9 et la clé `(projet, connexion)` reposaient sur cette prémisse ; elles sont confirmées
  par la mesure. La consigne « re-mesurer à chaque montée de version » n'est pas levée :
  elle est honorée, et reconduite.
> ✅ **CE CRITÈRE EST SATISFAIT depuis la session 2 du cadrage, 2026-08-19 (ADR §0bis).**
> Q2 = `from_project` ; Q3 = stockage de la proposition + forme du payload du ticket ;
> Q6 = acceptée avec pool de brouillons en attente ; Q14 = (a) élargir ; Q10 acquise plus
> tôt le même jour. **Les critères NON satisfaits qui restent** : le snapshot baseline, la
> suite d'ancrage, le re-jeu du spike B8, la spec checkpoint, et **la spec de M-G**.

- Réponses opérateur obtenues au minimum sur : Q2 (prédicat tickets — bloque M-B),
  Q3 (checkpoint — bloque M-C), **Q14 (ce que l'attestation de récupération doit
  apprendre — bloque M-B)**, **Q6 (staged captures — PROMUE ici le 2026-08-19, parce que
  l'axe « traçabilité du savoir » remonte la Phase 4.4 juste après la Phase 2)**, et
  Q10 — **cette dernière est ACQUISE depuis le 2026-08-19** (la liste couvre ; l'ordre
  change, ADR §0).
- **Spécification de M-G écrite et soumise** (nouveau critère, cadrage du 2026-08-19) :
  Q15 = route (3) engage un nouvel état terminal dans le CHECK 037. Le nom de l'état, sa
  branche exacte, le sort de `captured_knowledge_ids` / `abandonment_reason` /
  `focus_outcome`, le déclencheur de l'auto-fermeture et le sort du `next_focus` **ne
  sont spécifiés nulle part**. Tant que ce critère n'est pas atteint, la nature `agent`
  de D11 n'a pas d'état terminal et ne peut pas être livrée.

### Rollback / killswitch
Sans objet : rien de mutant.

---

## 3. Phase 1 — Voir sans toucher (migration M-A)

### Contenu

1. **Erreur de capture énumérée** (ferme B13) : `BrainSessionInputError` porte
   `rejections: [{id, reason}]`, `reason ∈ {not_found, wrong_project,
   created_before_session, ambiguous_type, attributed_elsewhere, unsupported_type}`,
   **plus `capturable_subset`** (greffe C retenue par le panel) : les ids qui
   passeraient. Aucun changement de sémantique — mêmes lots acceptés/refusés qu'avant,
   prouvé par les tests d'ancrage Phase 0 (mis à jour en Red d'abord pour la forme du
   message). Le lot reste all-or-nothing.
2. **Suggestions de capture** (lecture pure, jamais bloquant) :
   `project_uncaptured_since_start`
   (≤20, best-effort, calculé après clôture réussie) dans le résultat de `end` ;
   `project_uncaptured_since_start_count` dans `resume` — **noms alignés le 2026-08-19**
   sur le prédicat spécifié plus bas ; la version antérieure spécifiait le prédicat puis
   annonçait « le champ porte son nom exact » tout en gardant `uncaptured_candidates` /
   `uncaptured_candidate_count` deux lignes plus haut, contradiction dans le même item.
   L'erreur XOR « ledger vide sans raison »
   mentionne le compte de candidats. Rôle assumé : ce sont les instruments de mesure du
   dossier E3 (leur usage réel est lui-même mesuré en soak — réponse à la réserve du
   juge simplicité).
   **Prédicat de « candidat », spécifié — il manquait, et l'instrument entier en
   dépend.** Un candidat est un artefact des six `CAPTURE_TABLES` tel que :
   `project_key = session.project_key` **et** `created_at >= session.started_at`
   (mêmes bornes que `_validate_captures`, pour qu'une suggestion soit toujours
   capturable), **et** non déjà présent dans `brain_session_artifacts` (PK
   `knowledge_id` — l'exclusivité rend le test trivial). **La liaison par acteur est
   volontairement ABSENTE du prédicat**, et il faut le dire au lieu de le laisser
   deviner : la règle R5 la rendrait vide exactement pour les deux populations que
   l'instrument doit éclairer — les sessions stdio à `started_by_actor` NULL (B8) et le
   régime fantôme B1 (≥2 sessions ouvertes du même porteur). Conséquence assumée et
   **écrite dans le champ rendu** : ce sont les artefacts créés **dans le projet depuis
   `started_at`**, pas « le travail de cette session ». Ils peuvent appartenir à une
   session sœur. Le champ porte donc son nom exact — `project_uncaptured_since_start`,
   repris tel quel en tête d'item et dans l'ADR (D8) — et la mesure E3 s'en sert comme
   **plafond**, jamais comme numérateur.
3. **Migration M-A** : trois colonnes nullable sur `brain_sessions`, hors CHECK 037,
   sans backfill (`NULL` = « avant », doctrine 040/041) :
   - `started_by_actor VARCHAR(64)` (R4 — ferme B14) ;
   - `last_observed_at TIMESTAMPTZ` (écrite uniquement sous le flag du point 6) ;
   - `intent VARCHAR(500)` (greffe C : le champ humain de triage des fantômes).
   **Décision d'INDEX à instruire, pas à trancher ici — le mot « index » était absent de
   ce plan et de l'ADR** (constat du 2026-08-19). `started_by_actor` est créé **sans
   index**, et c'est la colonne sur laquelle l'émetteur du point 6 filtre à **chaque
   appel outermost de tool**, deux fois dans le même statement (le `WHERE` et la
   sous-requête corrélée de comptage). Les index réels de `brain_sessions`, mesurés le
   2026-08-19 (`pg_indexes`), sont exactement trois — `brain_sessions_pkey` sur `id`,
   `uq_brain_sessions_project_client (project_key, client_key)`,
   `idx_brain_sessions_project_status_started (project_key, status, started_at DESC)` :
   **aucun ne couvre l'acteur**. Le troisième couvre `(project_key, status)` et laisse
   l'égalité sur l'acteur en filtre résiduel, ce qui à la cardinalité mesurée (467 lignes,
   29 `open`) est très probablement gratuit — *probablement* n'est pas *mesuré*, et c'est
   un chemin chaud.
   **Ce qui est décidé ici** : rien. **Ce qui est exigé** : (a) la Phase 0 mesure le coût
   réel du statement de l'émetteur sur la table de production (voir §2, baseline), (b) le
   commit de M-A porte la conclusion écrite — index ou pas —, (c) **si un index est
   ajouté, il casse `expected_session_indexes`**, la liste FERMÉE des index de
   `brain_sessions` (`ops/recovery/brain-v42-v4.sql:404-412`, contrôlée `:665` et `:687`,
   doublée par `SESSION_INDEX_DEFINITION_MD5` en `test_recovery_contract_v3.py:164-168`
   et `:488`) : c'est le **quatrième** mécanisme de casse d'attestation recensé en R1.5,
   et il vaut sur les **deux** assets v4. (d) Le différer dans une tête à lui coûterait un
   rendez-vous de production de plus dans un couloir qui interdit deux têtes en vol
   (R1.2) : si index il doit y avoir, il voyage **dans M-A**.
4. **`start` enrichi** : persiste `started_by_actor` (best-effort, NULL si stdio sans
   header — on dégrade sans attribuer, règle `7ffe0e8a`) ; accepte `intent` optionnel ;
   le résultat gagne `open_sessions_same_carrier` (greffe C : « l'opérateur voit ses
   propres fantômes au moment où il en créerait un de plus »).
5. **`list` enrichi** : filtre `client_key_prefix` ; affichage d'`intent` ; convention
   `client_key` documentée (recommandée, jamais imposée — une contrainte serveur
   casserait tous les clients pour un problème de tri).
   **La « présence à la lecture » est RETIRÉE de cette phase.** Une version antérieure
   la livrait en paramètre opt-in `with_observed_activity`, calculant un
   `last_knowledge_activity_at` par jointure d'`access_log` (`actor = started_by_actor`,
   `accessed_at >= started_at`, **lookback plafonné à 7 j**) vers les six tables de
   connaissance, et la présentait comme « la liveness honnête disponible AVANT tout
   armement, et la mesure de comparaison pour trancher la question n° 1 ». La correction
   du premier passage n'avait vu que la colonne projet absente. **Trois faits vérifiés
   la condamnent :**
   - `access_log` **n'est pas un journal, c'est un tampon vidé en continu** —
     `repositories/pg_access_log.py:38-113` agrège puis exécute
     `sa.delete(access_log).where(id <= max_id)` **dans la même transaction** ;
     l'appelant est `DecayFlusher`, `interval_seconds=300` par défaut (`config.py:379`).
     Un lookback de 7 j sur une table qui retient au mieux ~5 minutes ne rend rien ;
   - l'agrégation **replie l'acteur en compteurs** : la colonne `actor` ne survit pas au
     flush, donc `actor = started_by_actor` n'a plus rien à joindre ;
   - les seuls écrivains sont des **lectures** (`search_hit`, `get_by_id`, `use`,
     `execute`), jamais des créations.
   **Mesure, 2026-08-18** : `select count(*) from access_log;` → **0 ligne**, quand
   `select max(last_accessed_at), count(*) filter (where access_count>0) from learnings;`
   → `2026-08-18 20:11:06+00 | 2209` — le cycle écrit-agrège-purge tourne bien, et
   l'instrument déclaré pour instruire **Q1** mesurait structurellement zéro. Ce qui le
   remplace est en Phase 0 : la population `(projet, acteur)` × sessions ouvertes
   (0 / 1 / ≥2), calculée **sans écrire**, qui mesure exactement ce que l'armement
   toucherait.
6. **Émetteur d'observation dans le middleware, livré FERMÉ**
   (`BRAIN_SESSION_OBSERVED_ACTIVITY_ENABLED=false`). Spécification exigée par le
   panel — UN statement, règle « exactement un » :

   ```sql
   UPDATE brain_sessions
      SET last_observed_at = NOW()
    WHERE status = 'open'
      AND project_key = :pk
      AND started_by_actor = :actor
      AND (SELECT count(*) FROM brain_sessions s2
            WHERE s2.status = 'open'
              AND s2.project_key = :pk
              AND s2.started_by_actor = :actor) = 1
   ```

   Émis sur l'appel outermost d'un tool portant un `project_key` résoluble et un
   acteur normalisé.
   **Chemin chaud, et son coût n'est pas mesuré.** Ce statement — filtre sur
   `(status, project_key, started_by_actor)` **plus** une sous-requête corrélée de
   comptage sur les trois mêmes colonnes — s'exécute à **chaque** appel outermost de
   tool, exactement là où le client-activity a déjà montré qu'un écrivain par appel n'est
   pas gratuit (`1c40c36a`, burst loss au-delà de 8 appels concurrents, tracké et non
   fermé). Or `started_by_actor` naît **sans index** et aucun des trois index de
   `brain_sessions` ne le couvre (mesuré, §3.3). La décision d'index est instruite en
   §3.3 et **mesurée en Phase 0** ; elle n'est pas tranchée ici, et elle n'est pas
   gratuite non plus — voir la casse d'attestation `expected_session_indexes` (R1.5).
   **Correction de prémisse — le middleware NE voit PAS le projet.** Une version
   antérieure écrivait « le middleware voit déjà les deux, y compris derrière
   `brain_call_tool` » ; c'est vrai de l'acteur, faux du projet.
   `src/brain_v42/mcp/provenance_middleware.py:74-96` est intégral et ne lit que des
   **en-têtes** : `get_http_headers(include={'mcp-session-id'})`,
   `normalize_agent(x-brain-agent)` (l.76), `normalize_session(x-brain-session)` (l.77),
   `normalize_transport(...)` (l.83), trois ContextVars, la garde de ré-entrance,
   `call_next`. Il n'inspecte **jamais** `context.message.arguments`
   (`grep -rn '\.arguments' src/brain_v42/mcp/` → seulement
   `dream_capabilities.py:250,258`). Le seul code du dépôt qui résout un projet depuis
   les arguments d'un tool est `services/dream_project_scope.py`, et il le fait par une
   **table de politiques PAR TOOL** (`PROJECT_TOOL_POLICIES:83-120` :
   `project_key`/`project_keys`/`owner_project_key`, `inject_project_key`, références
   typées à résoudre en base) — preuve que c'est un travail par tool, pas une donnée
   disponible.
   **Livrable ajouté à cette phase, sans lequel l'émetteur n'a aucun `:pk` à poser** :
   un **résolveur de projet par tool**, qui réutilise `PROJECT_TOOL_POLICIES` (une seule
   table — même doctrine de consolidation que le prédicat colon) ou déclare pourquoi il
   en pose une seconde. Les tools hors table ne sont **pas** observés et comptent
   `skipped{no_project}` : le serveur ne devine pas un projet. Le même résolveur sert
   l'écrivain staged de la Phase 4.4, qui reposait sur la même prémisse.
   `rowcount = 0` ⇒ aucune écriture ; une requête de comptage
   best-effort distingue alors `ambiguous` de `no_match` pour le compteur. Compteurs :
   `observed_activity_written` / `observed_activity_skipped{ambiguous, no_actor,
   no_project}`. Enveloppe sur le modèle prouvé du client-activity (ticket
   `1c40c36a`) : un échec d'observation ne casse **jamais** l'appel observé — prouvé
   par **test de panne injectée** (greffe A : critère de sortie, pas seulement
   compteurs).

### Critères de sortie (mesurables)
- Ancrage Phase 0 vert (hors tests volontairement passés en Red puis adaptés pour la
  forme d'erreur).
- Canary : un lot mélangé refusé rend une raison **par id** + le sous-ensemble
  capturable ; `resume` montre un compte de candidats sur une session de test ; `start`
  avec `intent` l'affiche dans `list` ; `start` sur un projet où le même porteur a déjà
  une session ouverte rend l'avertissement.
- `started_by_actor` renseigné sur les nouvelles sessions HTTP (part mesurée vs
  baseline Phase 0).
- Killswitch mesuré **fermé** en production : drop-in inspecté, environnement du
  processus inspecté (R3).
- Test de panne injectée vert : l'échec simulé de l'émetteur laisse l'appel de tool
  intact.
- **Le canary ci-dessus consomme la fenêtre de rollback de M-A** — « `start` avec
  `intent` l'affiche dans `list` » écrit un `intent` non NULL, et le downgrade de M-A
  est fail-closed dès qu'un `intent` non NULL existe. Une version antérieure n'écrivait
  cette règle qu'en Phase 2 : le canary de sortie refermait donc **définitivement** la
  fenêtre de rollback de la tête dont M-C, M-D et tout le couloir linéaire dépendent.
  Donc, comme en Phase 2 : canary sur **sessions jetables**, **purge documentée des
  `intent` de canary** après validation, puis **downgrade à blanc sur base de staging**
  prouvant la fenêtre encore ouverte — avant de déclarer la phase sortie.
- Pin : M-A appliquée, `alembic current` mesuré à la nouvelle head, pin bumpé dans le
  même commit, **`ops/recovery/` régénérée — les DEUX assets v4, `brain-v42-v4.sql` et
  `brain-v42-v4-pgrestore.sql`** (M-A change l'empreinte de colonnes de
  `brain_sessions`, R1.5 ; et `expected_session_indexes` en plus si l'index de §3.3 est
  retenu), plan-index repair re-prouvé fonctionnel.

### Rollback
Downgrade M-A = drop de trois colonnes nullable — aucune perte d'état lifecycle, mais
**une perte de jugement nommée** : `intent` est une ligne humaine déclarée (grille
fait/jugement, N7). Même doctrine que M-C (« c'est du jugement, on ne le jette pas en
silence ») : downgrade **fail-closed si au moins un `intent` non NULL existe**, la
purge explicite des intents étant alors un geste opérateur de runbook — et, comme la
039 le montre (`-x allow_project_context_trigger_downgrade=yes`), cette garde **peut**
recevoir un opt-in nommé plutôt qu'une purge destructrice ; le choix se fait à
l'écriture de la migration, pas ici.
`started_by_actor` et `last_observed_at` sont des faits observés, re-mesurables,
jetables sans garde. Revert des commits code. Killswitch : l'émetteur (point 6) — seul
comportement runtime nouveau de la phase.

---

## 4. Phase 2 — Capturer juste + le geste checkpoint (migrations M-B, M-C)

### Contenu

1. **Migration M-B — tickets capturables** (ferme B4 avec la Phase 1) : CHECK
   `brain_session_artifacts_type_valid` élargi à `'ticket'` (l'énumération actuelle —
   vérifiée — est `decision, learning, snippet, runbook, adr, indexed_plan, legacy`).
   `CAPTURE_TABLES`/validation gagnent un prédicat dédié :
   `tickets.from_project == session.project_key AND tickets.created_at >=
   session.started_at` (`from_project` = paternité — gated Q2, veto sans coût avant
   M-B ; la table `tickets` n'a pas de `project_key`, vérifié). Ledger, exclusivité PK,
   idempotence, all-or-nothing : inchangés. Downgrade fail-closed si des lignes
   `'ticket'` existent (gabarit 037).
2. **Prédicat sous-arbre PARTAGÉ** (greffe A, brique de B5/B11) : un module unique
   généralisant `project_group_scope` (`base NOT LIKE '%:%' AND key LIKE base || ':%'`,
   vérifié) — **une seule implémentation**, ce qui est un travail de CONSOLIDATION,
   pas une simple création.
   **Périmètre corrigé : il en visait deux et en ratait deux.** Une version antérieure
   annonçait « trois exemplaires `src/` » et « les deux copies inline sont résorbées
   dans cette phase ». Recompté le 2026-08-18, ils sont **cinq** — et l'un des deux
   manquants vit dans la fonction même que cette phase prétendait nettoyer :
   1. `db/project_group_scope.py:24-26` — la référence, à généraliser ;
   2. `services/project_group_ticket_service.py:129-137` — copie SQL inline, **résorbée
      dans cette phase** ;
   3. `services/proposal_service.py:377-383` — copie SQL inline alors que le module
      importe déjà `project_group_scope`, **résorbée dans cette phase** ;
   4. `services/project_group_ticket_service.py:164-167` — **seconde copie, en Python**
      (`project_key == base_key or (":" not in base_key and
      project_key.startswith(f"{base_key}:"))`), dans la même méthode
      `_lock_participants_scope` que la n° 2. **Résorbée** : le module partagé doit
      donc exposer **deux formes** — un prédicat SQL et un prédicat Python — sur une
      seule définition, sinon la consolidation se contente de déplacer la divergence ;
   5. `repositories/pg_project_context.py:202-213` (`get_keys_by_group`) — même
      sémantique en variante `split_part`, invisible à un grep sur `not_like("%:%")`.
      **Résorbée** si l'équivalence des deux formulations est prouvée en Red ; sinon
      recensée comme troisième formulation, avec le motif écrit.
   Red : test d'équivalence des **cinq** prédicats sur un jeu de clés partagé (dont
   `pk`, `pk:child`, `pk:child:grand`, une clé sans colon, une clé dont le préfixe
   n'est pas une base du groupe). Green : import du module partagé.
   Côté base, ce ne sont pas « deux vues des migrations 024 et 036 » mais **sept vues
   vivantes, toutes issues de la 036** (mesuré), nées de **deux corps de CTE recopiés**
   en `split_part(…) <> project_key AND split_part(…) IN red_base` — troisième
   formulation. Elles restent en place (une migration appliquée ne se réécrit pas) mais
   sont recensées dans `docs/PROJECTS_SYSTEM.md` comme exemplaires de frontière, à
   régénérer depuis le prédicat partagé à leur prochaine révision. Usages futurs
   (capture famille maintenant, lectures `include_descendants` en Phase 4.6 si Q4 le
   décide) : jamais de second exemplaire ad hoc.
3. **Capture famille, flag fermé** (`BRAIN_SESSION_CAPTURE_SUBPARTITIONS=false`) : à
   l'armement (geste opérateur, après Q4), une session sur `pk` peut capturer un
   artefact de `pk:child` — sens parent→enfant uniquement, via le prédicat partagé.
4. **Migration M-C — checkpoints** (ferme B7 ; **conditionnelle à Q3**, l'approbation
   produit que `d04dc588` exige) : table `brain_session_checkpoints` — `id UUID PK`,
   `session_id FK → brain_sessions ON DELETE RESTRICT`, `seq INT` **fourni par le
   client** + `UNIQUE(session_id, seq)`, **`progress TEXT NOT NULL`, `blocker TEXT`
   (nullable), `next_step TEXT NOT NULL`** (≤2 000 chacun côté app), `created_at` ;
   **append-only garanti par trigger en base** (UPDATE/DELETE refusés — culture
   maison, greffe du panel) ; plafond 200/session fail-closed avec message explicite.
   **Idempotence de replay — les retries d'agents sont la norme (invariant du
   dossier)** : `ON CONFLICT (session_id, seq) DO NOTHING` — le replay exact
   n'appende pas de seconde ligne ; un même `seq` avec un payload
   différent est un conflit non destructif, rejeté explicitement.

   > **AMENDÉ le 2026-08-20 — ADR §0bis.4 fait foi, `SPEC-checkpoint.md` en dérive.**
   > Deux corrections de propagation, aucune décision neuve :
   > **(i)** ce paragraphe portait `kind VARCHAR(20)` CHECK ∈ {progress, blocker,
   > next_step, handoff} + `note TEXT` — **la forme ABANDONNÉE**. La forme signée publie
   > `progress` + `blocker|null` + `next_step` **ENSEMBLE, en un appel** (divergence (d)
   > reprise du ticket `d04dc588` : trois `kind` exclusifs permettent d'émettre un
   > `progress` sans jamais de `next_step`, et le lecteur de fraîcheur ne peut alors pas
   > savoir si l'instantané est complet). `handoff` disparaît comme nature.
   > **(ii)** la mention « **ne rafraîchit pas le heartbeat une seconde fois** (refresh
   > conditionné à `rowcount = 1`) » supposait un effet heartbeat que le §0bis.4 a
   > **dissous** : le checkpoint n'écrit ni ne touche `last_heartbeat_at`, jamais.
   > Le stockage append-only `UNIQUE(session_id, seq)`, lui, est **maintenu**.
   **DEUX divergences d'avec le MVP de `d04dc588`, pas une** — la version antérieure
   n'en déclarait qu'une et affirmait par ailleurs reprendre la doctrine « telle
   quelle » (§7, ligne B7) :
   - **stockage** : append-only + `(session_id, seq)` là où le ticket recommande un
     snapshot sur `brain_sessions` + CAS `expected_checkpoint_revision`. Motif :
     l'append-only garde l'histoire des notes (c'est du jugement) et réobtient les
     propriétés P0 (replay sans double effet, conflit non destructif) par la clé ;
   - **forme du payload** (relue dans le ticket) : son contrat est
     `brain_session_checkpoint(session_id, expected_client_key,
     expected_checkpoint_revision, progress, blocker|null, next_step)`, réponse bornée
     distinguant *activity, milestone, blockage, freshness, focus_context*, critère
     « **un appel** ». Le `kind ∈ {progress, blocker, next_step, handoff}` + `note`
     unique transforme trois champs **publiés ensemble** en trois natures **mutuellement
     exclusives** : publier progrès + blocage + prochaine étape demanderait trois
     appels, trois `seq`, trois rafraîchissements de heartbeat, et ferait sauter le
     critère « un appel ». **Q3(d) tranche ; la spec checkpoint de la Phase 0 l'écrit
     avant tout code.**
   Downgrade fail-closed si des checkpoints existent (c'est du jugement, on ne le jette
   pas en silence).
5. **Tool `brain_session_checkpoint(...)`** — signature arrêtée par la spec Phase 0
   selon Q3(d) : garde d'identité avant mutation, et **rafraîchit `last_heartbeat_at`
   en effet de bord** (checkpoint réel seulement, jamais un replay) — le geste « je note
   où j'en suis » remplace le geste vide « je pinge ». **Politique d'appel : fourche
   déclarée, tranchée dans Q3** — soit chaque checkpoint reste une commande explicite
   de l'utilisateur (covenant intact, mais l'adoption dépend de la même discipline
   humaine qui a produit **24 sessions stale sur 29** (re-mesuré le 2026-08-19 ; ce
   passage citait « 21 sur 23 », mesure du 2026-08-16, et « 21 » désigne aujourd'hui les
   balayables >7 j, pas les stale), et « le checkpoint date le
   vivant » ne vaut que s'il est appelé), soit un agent peut checkpointer spontanément
   en longue session autonome — mutation de session hors commande explicite, donc
   **changement de covenant**, jamais armé sans décision opérateur.
   **Et cette fourche a désormais un mécanisme** (R3 corrigée) : rien côté serveur ne
   distingue un appel d'agent d'un appel d'humain, l'artefact livré est identique sous
   les deux réponses, et l'effet heartbeat suffit à un agent pour maintenir sa session
   vivante indéfiniment — le faux-vivant de B2, qui rendrait le critère 4.3
   auto-satisfiable. Donc : **l'effet heartbeat naît derrière
   `BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT=false`** (§8bis), à moins que Q3(a)/(b) ne
   le retire du contrat — **retrait qui serait une TROISIÈME divergence d'avec
   `d04dc588`** (« Checkpoint réel rafraîchit heartbeat atomiquement ; replay non »,
   relu le 2026-08-19), à déclarer avec les deux autres. Le tool, lui, n'a pas de flag —
   c'est bien une commande.
   La livraison
   porte le covenant à **huit** commandes : phrase-covenant dans la docstring du 8e
   tool, énumération CLAUDE.md et test d'ancrage Phase 0 étendus dans le même commit.
   `list` gagne `last_checkpoint_at` ; `resume` rend les checkpoints récents. Doctrine
   `d04dc588` : la fraîcheur affichée dérive de l'âge du dernier checkpoint (ou du
   heartbeat) ; la dérive du focus est exposée séparément et n'est jamais cause de
   péremption. `heartbeat` reste inchangé et documenté « préférer checkpoint » —
   jamais transformé en no-op (mentir à une commande explicite du covenant serait une
   rupture de contrat, motif de rejet de la proposition A par le panel).

### Critères de sortie (mesurables)
- Canary : capture d'un lot `[learning, ticket]` réussit ; un ticket d'un autre
  `from_project` est rejeté avec `reason=wrong_project` ; flag famille **mesuré
  fermé** et, flag fermé, le rejet parent→enfant rend `wrong_project` comme avant
  (ancrage) ; flag armé sur un projet de test, la capture `pk` → `pk:child` passe.
- **Ces deux canaries rendent l'attestation de récupération FAUSSE, et pas seulement
  pour la durée du canary.** `ops/recovery/brain-v42-v4.sql:1083-1090` définit
  `knowledge_sources` comme l'UNION des **six** tables de capture — les tickets n'y sont
  pas — et `artifact_source_matches` (l.1091-1107) joint avec
  `source_record.project_key = session_record.project_key`.
  `artifact_source_mismatches` (l.1109-1113) compte `source_matches <> 1`, attendu à
  **0**. Donc : (a) le premier artefact `knowledge_type='ticket'` n'a aucune ligne
  source ⇒ mismatch **permanent** ; (b) la capture `pk → pk:child` viole l'égalité de
  projet ⇒ mismatch **permanent**. Contrairement aux empreintes de schéma, **cela ne se
  répare pas en régénérant un asset** : il faut décider si l'attestation apprend les
  tickets et le prédicat sous-arbre. **Q14 est donc un préalable de M-B, pas un
  post-traitement** — sans réponse, cette phase transforme une preuve de restauration
  verte en preuve rouge définitive.
- Si Q3 approuvée : un cycle réel `start → checkpoints → end` sur brain-v42 ; `list`
  montre `intent` + `last_checkpoint_at` ; **`last_heartbeat_at` reste INCHANGÉ après un
  checkpoint (test d'ABSENCE d'effet)** ; le replay exact d'un checkpoint n'appende pas
  (test) ; UPDATE/DELETE sur un checkpoint refusés par le
  trigger (test) ; le 201e checkpoint est refusé avec message explicite (test).

  > **AMENDÉ le 2026-08-20 — ADR §0bis.4 fait foi.** Ce critère exigeait « **un
  > checkpoint rafraîchit `last_heartbeat_at` (test)** ». C'est désormais un test à **NE
  > PAS écrire** : l'effet heartbeat est dissous, et le test qui le vérifierait câblerait
  > le comportement que la spec interdit. Il est **retourné** en test d'absence d'effet —
  > c'est lui qui tombera en premier si quelqu'un recâble le heartbeat.
- **Les canaries ci-dessus consomment la fenêtre de rollback de M-B/M-C** (leurs
  downgrades sont fail-closed dès qu'une ligne existe — et ce sont ces canaries qui
  créent les premières lignes). Donc : canaries exécutées sur des **sessions
  jetables**, et **purge documentée des lignes de canary** (artifacts `'ticket'` et
  checkpoints des sessions de canary) exécutée après validation, suivie d'un
  **downgrade à blanc sur base de staging** prouvant la fenêtre encore ouverte —
  avant de déclarer la phase sortie.
- Pin : M-B (puis M-C) appliquées l'une après l'autre — jamais deux en vol —,
  `alembic current` mesuré, pin bumpé même commit à chaque fois.

### Rollback
- Flag famille fermé = comportement Phase 1 à l'identique.
- Downgrade M-B possible tant qu'aucun ticket capturé ; les tickets de canary sont
  purgés par la procédure des critères de sortie, donc la fenêtre reste ouverte après
  validation (ensuite fail-closed sur les premières captures réelles, voulu).
- Downgrade M-C fail-closed si des checkpoints existent (voulu) — même purge de
  canary.
- **Le rollback est séquentiel** (chaîne Alembic linéaire à tête unique — test du
  pin) : revenir sur M-A après M-B/M-C exige de downgrader dans l'ordre inverse.
  « Rollbackable » se lit « réversible depuis la tête courante », jamais « sautable ».
- Killswitch : `BRAIN_SESSION_CAPTURE_SUBPARTITIONS` (le checkpoint, commande
  explicite, n'a pas de flag runtime — R3).

---

## 5. Phase 3 — La mémoire du focus (migration M-D)

### Contenu

1. **Recensement préalable — les écrivains du focus (prémisse corrigée DEUX fois).**
   Une première version affirmait « deux seuls sites d'écriture » ; la deuxième a
   corrigé en « six sites, dont un seul bumpe `focus_revision`, l'upsert écrasant sans
   bump ni CAS ». **Le second énoncé est faux aussi**, et c'est la migration 032 qui le
   dit.
   - **Ce qui existe déjà, mesuré le 2026-08-18 en production (head `045`, lecture
     seule)** : `alembic/versions/032_brain_sessions.py:19-34` crée
     `increment_project_focus_revision()` — « `IF NEW.current_focus IS DISTINCT FROM
     OLD.current_focus THEN NEW.focus_revision := OLD.focus_revision + 1` » — et le
     trigger `project_contexts_focus_revision_trigger BEFORE UPDATE OF current_focus ON
     project_contexts FOR EACH ROW`. `pg_get_triggerdef` le rend toujours en place.
     **Aucun écrivain ne peut donc changer le texte du focus sans bump** : un
     `INSERT … ON CONFLICT DO UPDATE` déclenche les triggers BEFORE UPDATE, et la
     branche ON CONFLICT de `pg_project_context.get_or_create:281-290` met bien
     `current_focus` dans son SET. Contrôle croisé :
     `grep -c focus_revision src/brain_v42/repositories/pg_project_context.py` = **0**,
     idem sur `scripts/scrub_xml_tool_call_leak.py`.
   - **Les six sites, exactement** (docstring de `src/brain_v42/db/focus_stamp.py`) : le
     CAS `applied` de `session_end` (`pg_brain_session.py:713-714`, qui pose
     `focus_revision=expected_revision + 1` **explicitement** — le CHECK 037 l'exige),
     `brain_update_project_focus` (`roadmap_service.py`, `focus_revision + 1`
     explicitement), `pg_project_context.update`, `update_focus`, `create`, et l'upsert
     du tool MCP vivant `brain_set_project_context`. Plus un écrivain hors MCP :
     `scripts/scrub_xml_tool_call_leak.py` (`_PROJECT_CONTEXT_COLS = ("current_focus",)`)
     — **six PLUS le scrub, soit sept**.
   - **L'énoncé exact — corrigé une TROISIÈME fois le 2026-08-19.** La deuxième
     réparation écrivait « deux sites bumpent explicitement ; les cinq autres reçoivent
     le bump du trigger dès que le texte change ; et `brain_update_project_focus` est le
     **seul** à bumper même quand le texte ne change pas ». Deux erreurs :
     - **les DEUX sites explicites bumpent sur texte inchangé.**
       `_apply_focus_if_current` (`pg_brain_session.py:713-714`) pose
       `focus_revision=expected_revision + 1` **sans comparer le texte**, et le CHECK 037
       l'exige (`applied` ⇒ `focus_revision_at_end = end_expected_focus_revision + 1`).
       Le commentaire du code nomme le cas : « Re-posting the previous prose verbatim is
       the copy-forward this column exists to expose ». C'est le régime **normal** d'une
       fin de session, pas un cas de bord de `roadmap_service` ;
     - **le trigger ne voit que les UPDATE.** `create` et la branche INSERT de
       `get_or_create` (`pg_project_context.py:51-71`, `:273-275`) écrivent
       `current_focus` à la naissance de la ligne : pas de trigger, `focus_revision = 0`
       par défaut de colonne (mesuré).
     Énoncé juste : **deux sites posent la révision eux-mêmes, texte changé ou non ;
     quatre reçoivent le bump du trigger sur UPDATE quand le texte change ; les deux
     chemins d'INSERT n'ont ni trigger ni révision à incrémenter.** Conséquence directe
     pour le point 3 (le chemin partagé) et pour la garde en base du point 2.
   - **Ce que l'upsert fait vraiment** : il réécrit `current_focus` — **y compris à NULL
     quand l'argument est omis**, vérifié — **sans CAS**. C'est un vrai canal
     d'écrasement, et c'est le fond de B6. Mais il **bumpe** (trigger 032) et il **date**
     (`focus_updated_at = focus_stamp(excluded.current_focus)`, sous `IS DISTINCT FROM`,
     donc un focus qui part vers NULL compte). Il n'est pas muet : il est **non
     récupérable**, faute d'historique. C'est cela, et cela seul, que M-D répare.
   - **La preuve chiffrée invoquée était mal lue.** « 10 des 59 `project_contexts` ont
     `current_focus IS NULL` — le canal d'écrasement mord déjà » : le nombre est exact
     (re-mesuré : `10/59`), la conclusion non. Les **dix** lignes sont à
     `focus_revision = 0` **et** `focus_updated_at IS NULL` (`perso`, `red-backup`,
     `red-cli`, `red-shrik:agent`, `red-daemon`, `red-llm`, `red-tsdb`,
     `red-lab:developer{,-gemini,-opus}`) : focus **jamais écrit**, pas effacé — et un
     écrasement depuis la 040 aurait daté la colonne. **Zéro contexte de production porte
     la signature d'un effacement** (NULL avec révision > 0). Le canal existe dans le
     code ; la production ne montre pas qu'il ait mordu. Ce plan n'a donc aucun chiffre
     à verser au dossier de Q13, et le dit.
2. **Migration M-D — `project_focus_history`** (ferme B6 en récupérabilité) :
   - `project_key VARCHAR(50) NOT NULL` (sans FK, comme les tables de connaissance —
     le drop d'un contexte ne doit pas emporter l'audit), `focus_revision BIGINT NOT
     NULL`, **`focus TEXT NULL`** — un focus effacé (NULL) est précisément
     l'écrasement destructeur que l'audit doit enregistrer ; et un `NOT NULL` ferait
     avorter `alembic upgrade` en production au seed (`NotNullViolation` sur les 10
     focus NULL mesurés), défaut invisible en CI dont la base est vide au moment du
     seed —, `actor VARCHAR(64) NULL` (R4 — corrigé du VARCHAR(128) des
     propositions), `source VARCHAR(20) NOT NULL` CHECK ∈ {session_end, focus_tool,
     context_upsert, generic_update, maintenance_scrub, migration_seed} — l'enum
     couvre les écrivains réels —, `created_at`.
   - PK `(project_key, focus_revision)` — la monotonie du CAS généralisé la rend
     naturelle ; insert `ON CONFLICT DO NOTHING` rend les replays idempotents.
   - **Append-only garanti par trigger** (UPDATE/DELETE refusés — greffe du panel,
     l'audit devient une contrainte, pas une convention).
   - **Le « trigger-garde » d'une version antérieure est RETIRÉ.** Il devait « refuser
     un UPDATE où `current_focus IS DISTINCT FROM` l'ancien sans
     `focus_revision = old + 1` » : c'est **mot pour mot** ce que fait la 032, en posant
     la valeur au lieu de la refuser. Pire, il cohabiterait mal — PostgreSQL déclenche
     les triggers BEFORE ROW dans l'**ordre alphabétique** de leur nom, que le plan ne
     fixait pas. Nommé avant `project_contexts_focus_revision_trigger` (p. ex.
     `…_focus_history_guard`), il verrait `NEW.focus_revision` non encore incrémenté et
     **rejetterait toute écriture de focus des quatre écrivains qui écrivent par UPDATE
     sans poser la révision eux-mêmes** (`update`, `update_focus`, l'upsert, le scrub —
     `create` écrit par INSERT et échappe aussi bien au garde qu'au trigger) — `brain_set_project_context` fail-closed en production à
     chaque changement de focus. Nommé après, il est trivialement satisfait : code mort.
   - **Ce qui reste, et qui est le vrai livrable en base** : un **constraint trigger
     différé** (`AFTER UPDATE OF current_focus ON project_contexts … DEFERRABLE
     INITIALLY DEFERRED`) qui, en fin de transaction, exige la ligne d'historique à
     `NEW.focus_revision`. Il n'a pas de jumeau, ne dépend d'aucun ordre alphabétique, et
     attrape l'écrivain qui contourne le chemin partagé. **La clause `OF current_focus`
     est obligatoire** : sans elle il se déclencherait sur tout UPDATE de
     `project_contexts`, y compris ceux du plan-index repair
     (`plan_index_repair_store.py:294-308` et `:560-584`, qui n'écrivent que
     `plan_scan_paths`/`updated_at`) et les ferait échouer — c'est exactement la revue
     que le message du pin exige (R1.4).
   - **Ce que ce trigger ne peut PAS voir — trou nommé le 2026-08-19 :** les INSERT.
     `pg_project_context.create` et la branche INSERT de `get_or_create` persistent un
     `current_focus` à la naissance de la ligne (`focus_revision = 0`, défaut de
     colonne). Pour tout `project_context` créé **après** M-D, la révision 0 est donc un
     focus écrit qu'aucune garde en base n'oblige à s'historiser — le seed ne couvre que
     les 59 contextes existant à l'upgrade. **Trois voies, à trancher à l'écriture de
     M-D, pas à découvrir en production** : (a) `AFTER INSERT OR UPDATE OF
     current_focus` — la seule qui tient N1 en base, au prix d'une ligne d'historique à
     chaque création de projet, focus NULL compris ; (b) rester sur UPDATE et faire
     porter la révision 0 par le seul chemin applicatif partagé, en écrivant que la garde
     dure ne commence qu'à la révision 1 ; (c) interdire à `create`/`get_or_create`
     d'écrire un focus non NULL à la naissance. **Ne pas choisir, c'est choisir (b) sans
     le dire**, et livrer un N1 faux pour tout projet neuf.
   - **Créé DÉSACTIVÉ, activé après le redémarrage MCP** (R1.3) : entre l'`upgrade` et
     le redémarrage, le processus vivant exécute encore le code pré-M-D, qui n'écrit
     aucune ligne d'historique ; le trigger ferait avorter au COMMIT **tout
     `brain_session_end` en `focus_outcome=applied`**, fail-closed, session laissée
     ouverte, sans killswitch ni downgrade praticable.
   - **Seed à l'upgrade** : une ligne par `project_context` avec le focus courant —
     **NULL compris** — et sa révision, `source='migration_seed'` : l'ancrage couvre
     les 59 contextes, l'enum n'est plus orpheline.
   - **Où tester le seed — la promesse d'une version antérieure n'était pas tenable.**
     Elle annonçait « testé contre une base NON vide contenant des focus NULL (test
     d'intégration dédié) » en citant `c60d023d` § « OÙ TESTER ». Cette section dit
     l'inverse de ce qu'on lui faisait dire : elle conclut que prouver un tel upgrade
     « demande `tests/integration` **ET UNE SECONDE BASE** —
     `tests/integration/conftest.py` lance `alembic upgrade head` UNE FOIS par session
     avec l'env par défaut » (vérifié : `_run_alembic_upgrade` est un subprocess appelé
     par une fixture `scope="session", autouse=True`). Le plan promettait donc un test
     sans fournir ni la seconde base ni la fixture, et se fermait la seule voie
     in-session (downgrade puis re-upgrade) en rendant ce downgrade fail-closed. **Deux
     voies, à choisir explicitement à l'écriture de M-D** : (a) refactorer le seed en
     **planificateur pur** (une fonction qui rend les lignes à insérer à partir d'un
     jeu de contextes), testable en unitaire y compris sur des focus NULL — c'est la
     voie recommandée par le ticket ; ou (b) livrer la fixture de seconde base
     d'intégration, qui n'existe pas et qui est un chantier en soi.
   - Downgrade : **fail-closed si des lignes autres que le seed existent** — mais
     **avec opt-in nommé**, pas avec une purge destructrice. Une version antérieure
     écrivait « fail-closed inconditionnel … un downgrade Alembic n'a pas de paramètre
     de confirmation ». **C'est faux, et ce dépôt implémente le contraire** dans la
     migration que ce plan cite trois lignes plus haut comme gabarit :
     `alembic/versions/039_project_context_timestamp_cas.py:17,337-339` —
     `_DOWNGRADE_OPT_IN = "allow_project_context_trigger_downgrade"`,
     `context.get_x_argument(as_dictionary=True)`, `raise RuntimeError(
     "project_context_trigger_downgrade_opt_in_required")` si l'argument n'est pas
     `"yes"`. Soit littéralement
     `alembic -x allow_focus_history_downgrade=yes downgrade …`. La correction du
     premier passage avait remplacé une promesse floue par une impossibilité fausse, et
     le coût était réel : elle concluait qu'il faut **détruire l'audit** pour pouvoir
     revenir en arrière. M-D reprend le mécanisme de la 039 : l'audit reste, le geste
     est nommé.
3. **Écriture transactionnelle par UN chemin partagé, aux SIX sites** (même doctrine
   de consolidation que le prédicat colon en Phase 2) : une fonction unique insère la
   ligne d'historique dans la même transaction que CHAQUE écriture persistée de
   `current_focus` — les six sites plus le scrub y passent. **Elle n'ajoute aucun bump
   de son cru** et lit la révision **après** l'écriture (`RETURNING focus_revision`) pour
   historiser sur cette valeur. **Motif corrigé le 2026-08-19** : la version antérieure
   justifiait par « la 032 le fait déjà, et un second incrément applicatif poserait
   `OLD+2` ». Le `OLD+2` n'existe pas — le trigger **assigne**
   (`NEW.focus_revision := OLD.focus_revision + 1`), il n'ajoute pas : la valeur du
   statement est écrasée, pas cumulée. **La preuve est la source plpgsql, et elle
   seule** (`alembic/versions/032_brain_sessions.py:19-34` : une affectation, pas un
   cumul). *Corroboration retirée le 2026-08-19* : « la production avance d'un cran
   (CAS 209→210), pas de deux » était mal attribuée — ce CAS est un `brain_session_end`
   (DOSSIER §B6, deux sessions parallèles sur le snapshot rev 209), pas une écriture de
   `roadmap_service`, et rien ne dit que le TEXTE du focus ait changé, seule condition
   qui fasse parler le trigger. **Donc les deux bumps explicites doivent
   RESTER** : les retirer casserait `end` (trigger muet sur texte inchangé, CHECK 037 qui
   exige quand même `expected + 1`) et le jeton CAS d'un lot blockers-only. Le
   `RETURNING` est la bonne règle parce qu'il est vrai dans les deux régimes — pas parce
   qu'un double incrément menacerait. Un échec d'insert fait échouer l'écriture de focus
   entière — fail-closed assumé et testé (« un audit qui peut se taire ne prouve
   rien »). Un CAS `conflict` n'écrit pas de ligne. **Réserve de clé à instruire — re-dimensionnée le
   2026-08-19** : la version antérieure ne l'attribuait qu'à
   `brain_update_project_focus` (jeton CAS d'un lot blockers-only) et la traitait comme
   un cas de bord. **`brain_session_end` produit le même effet, et c'est son régime
   normal** : le CAS pose `expected + 1` sans comparer le texte, donc toute session qui
   referme en recopiant la prose précédente ajoute une ligne d'historique de focus
   identique. La PK `(project_key, focus_revision)` reste unique — ce n'est pas une
   collision —, mais le volume de doublons de contenu est celui des fins de session, pas
   celui d'un lot blockers-only occasionnel. À rendre lisible dans le tool de lecture
   (marquer « focus inchangé » plutôt que filtrer), et à prendre en compte dans le
   dimensionnement de `brain_focus_history`.
   **Changement de comportement induit, soumis (Q13, veto sans coût avant M-D)** :
   `brain_set_project_context` avec `current_focus` **omis cesse d'effacer** le focus
   (omis ≠ effacement explicite) ; un effacement explicite reste possible, versionné et
   audité. **À trancher sur le raisonnement, pas sur un chiffre** — la production n'en
   montre aucune victime (point 1).
4. **Tool lecture seule `brain_focus_history(project_key, limit≤50, offset)`** :
   revision, focus, actor, source, created_at.
5. **`focus_diff` dans le résultat de `end`** (greffe C) : caractères ajoutés/retirés
   vs le focus de base du CAS — la visibilité avant tout garde. Le garde dur de
   rétrécissement (seuil ~60 % proposé par C) n'est **pas** livré : seuil arbitraire
   disqualifié par deux juges ; il reste la question ouverte n° 7.
6. **Runbook de récupération d'un focus écrasé** (procédure en étapes) + un drill
   exécuté une fois.

### Critères de sortie (mesurables)
- **Toute mutation persistée de `current_focus` laisse sa ligne d'historique** —
  vérifié par le **constraint trigger différé** (test : un UPDATE direct de
  `current_focus` sans ligne d'historique **avorte au COMMIT**, pas à l'instruction) et
  par un canary sur CHAQUE écrivain, dont `brain_set_project_context` (l'upsert
  historise, y compris un effacement explicite). **Le bump de `focus_revision` n'est
  PAS un critère de sortie de cette phase** : il est acquis depuis la 032 et vérifié en
  production ; l'ancrer ici reviendrait à tester Postgres.
  La jointure `focus_revision` ↔ historique n'est pas non plus le critère : elle serait
  verte à 100 % sur une base qui n'a jamais reçu d'écriture après M-D. Le critère
  honnête est le trigger différé, plus le canary par écrivain.
  **Le canary par écrivain inclut les deux chemins d'INSERT** (`create`, branche INSERT
  de `get_or_create`) : ils échappent au trigger différé (§5.2), donc ils sont le seul
  endroit où le critère mesure le chemin applicatif **seul**. Un projet neuf créé avec un
  focus non NULL doit produire sa ligne de révision 0 — ou, si la voie (b) est retenue,
  le critère l'exclut explicitement au lieu de l'oublier.
- Un conflit CAS n'écrit pas de ligne (test) ; le replay d'un `end` persisté n'écrit
  pas de seconde ligne (test) ; UPDATE/DELETE refusés par trigger (test) ; **le seed est
  exercé sur des contextes à focus NULL** — par le planificateur pur en unitaire, ou par
  la fixture de seconde base si elle est livrée (§5.2, « où tester »).
- `end` rend un `focus_diff` correct sur canary.
- Drill de récupération exécuté et documenté.
- **Les canaries par écrivain consomment la fenêtre de rollback de M-D** (chacun écrit
  une ligne hors seed) : sessions et projets jetables, purge documentée, downgrade à
  blanc sur staging avant de déclarer la phase sortie — **ou** usage de l'opt-in nommé
  `-x allow_focus_history_downgrade=yes`, qui rend la purge inutile.
- Pin : M-D appliquée, mesurée, pin bumpé même commit, **revue du plan-index repair
  écrite** (R1.4) et **`ops/recovery/` régénérée — les DEUX assets v4** (R1.5 : M-D
  ajoute un trigger sur `project_contexts`, table de la liste fermée
  `expected_runtime_user_triggers` ; `brain-v42-v4-pgrestore.sql` porte la même liste et
  la parité des CTE est testée, `test_recovery_contract_v4_pgrestore.py:29-33`).

### Rollback
Downgrade M-D fail-closed hors seed, **avec opt-in nommé** (gabarit 039,
`-x allow_focus_history_downgrade=yes`) ; le chemin d'écriture partagé et ses six sites
livrés dans le même commit, revert atomique. Killswitch : non requis pour l'écriture
d'audit, purement additive dans des transactions existantes (R3 documente l'exception) —
**mais le constraint trigger, lui, est livré DÉSACTIVÉ et activé par geste opérateur
après le redémarrage MCP** (§5.2), ce qui lui tient lieu d'interrupteur pendant la
fenêtre de bascule. **Interrupteur payant, pas gratuit** (2026-08-19) : tant qu'il est
éteint, l'attestation `ops/recovery/` est rouge (`tgenabled = 'O'` exigé, R1.5). Le
réutiliser en urgence est légitime ; le faire sans le dater ne l'est pas.

---

## 6. Phase 4 — Armements et lot gated (gestes opérateur, dans cet ordre)

> **ORDRE MODIFIÉ PAR LE CADRAGE DU 2026-08-19 — lire ceci avant le reste de la
> section.** L'axe de priorité choisi par l'opérateur est la **traçabilité du savoir**
> (B3, B4, B5 en tête). Or **4.4 fermait la douleur n° 1 en avant-dernier**, derrière Q6
> *plus* un soak de 14 jours. Nouvel ordre global : **P0 → P1 → P2 → 4.4 → P3**, puis
> 4.1, 4.2-3, 4.5 et 4.6 dans l'ordre ci-dessous.
>
> Deux dépendances qui survivent au reséquencement et qu'on ne peut pas contourner :
> **(i)** 4.4 dépend durement de la Phase 1 — l'écrivain staged y prend le résolveur de
> projet par tool et `started_by_actor` (§3.6) ; remonter 4.4 avant P1 la priverait de
> son `project_key`. **(ii)** 4.1 arme D5, qui devient **portant** pour la nature
> `agent` (Q12 = (a), ADR §0.2) : sous cette réponse, 4.1 n'est plus un armement de
> confort et son rang mérite d'être réexaminé — **ce plan ne l'a pas fait**, et la
> section 4.1 ci-dessous est encore écrite comme si D5 était optionnel.
>
> **Le corps des sous-sections n'a PAS été renuméroté** : les titres « 4.1 » à « 4.6 »
> gardent leurs noms d'origine pour que les renvois des trois documents restent
> valides. Seul l'ordre d'exécution change.

Rien ici n'est livré armé par ce plan. La phase est une **séquence de décisions
d'opérateur**, chacune avec mesure avant/après. Un armement préexiste, hors de ce
plan : le sweep DRY, armé par l'opérateur le 2026-08-18 (mesuré — voir §0 et 4.2).

**Correction — « réversible par un fichier (drop-in/env) » était faux pour la moitié de
cette phase**, et un opérateur qui lisait cette promesse avant d'armer 4.4 découvrait un
rendez-vous de production. Trois gestes ne sont **pas** des fichiers :
- **4.2** change le prédicat SQL du sweep (`GREATEST(...)`) — c'est un déploiement de
  code, et il n'apparaît dans le contenu livré d'aucune des phases 1 à 3 ; il est donc
  **rattaché explicitement à 4.2** et suit R2 (Red/Green) plus un redémarrage MCP ;
- **4.4** livre **M-E**, tête Alembic, downgrade fail-closed sur les `staged` non
  résolus ;
- **4.5** livre **M-F**, tête Alembic, garde non-vide.
Les trois sont dans le couloir du pin (§8) et suivent R1 intégralement. Ce qui reste
réversible en un fichier : 4.1, l'armement WET de 4.3, les flags de 4.4 et 4.6 **une
fois leur tête posée**.

### 4.1 — Armer l'observation (`BRAIN_SESSION_OBSERVED_ACTIVITY_ENABLED=true`) — bloqué par Q1
- Armement par drop-in systemd documenté en runbook. Soak ≥ 14 j.
- **Mesures de sortie, publiées — l'ordre a été corrigé : le critère annoncé en premier
  ne pouvait pas échouer.** Une version antérieure mettait en tête « **zéro faux-mort
  potentiel parmi les sessions OBSERVÉES** — aucune session avec `last_observed_at`
  récent ne serait candidate au sweep », et en reléguait la seule mesure informative en
  second. Or avec le prédicat de 4.2
  (`GREATEST(last_heartbeat_at, COALESCE(last_observed_at, last_heartbeat_at)) < cutoff`),
  `last_observed_at` récent ⇒ `GREATEST(...) > cutoff` ⇒ non-candidate, **par
  construction**, quels que soient les 14 jours de soak. C'est ce que l'ADR §4 appelle
  une « impossibilité de prédicat » : une propriété du design, pas un résultat de
  mesure — et c'est la même classe de défaut que le §5 avait su nommer pour B6
  (« critère trivialement satisfiable »), sur le critère qui déverrouille l'armement WET.
  Ordre corrigé :
  1. **compte de sessions ouvertes sous ambiguïté ET candidates au sweep** — le
     faux-mort résiduel, celui que l'observation ne couvre PAS : sous ambiguïté (≥2
     sessions ouvertes du même porteur, régime fantôme B1, règle R5), AUCUNE session du
     couple n'est observée, y compris la vivante activement travaillée ; idem stdio sans
     header (`skipped{no_actor}`, B8). **C'est ce nombre qui peut échouer, donc c'est
     lui le critère** ; et il ne part pas d'une page blanche — son **plafond** est déjà
     mesuré, par projet (`7ffe0e8a` au 2026-08-16 ; re-mesuré le 2026-08-19 : 24 des
     29 sessions `open` dans un projet qui en porte au moins deux, cf. §2 baseline),
     ce qui donne l'ordre de grandeur à comparer une fois la ventilation par acteur
     disponible ;
  2. ratio `skipped{ambiguous}` et `skipped{no_project}` (si hauts : retour à
     l'opérateur — question n° 1 corollaire, et couverture du résolveur par tool —
     plutôt qu'élargir en silence) ;
  3. ratio `observed_activity_written/skipped`, comparé à la population calculée sans
     écriture en Phase 0 : **c'est la vérification que l'émetteur observe bien ce que la
     baseline avait prédit**, la seule chose que le soak apprend vraiment ;
  4. compte de sessions `observed_only` (observation récente, ni heartbeat ni checkpoint
     depuis 7 j) — la borne mesurée du résidu « session oubliée sous un acteur actif »
     relevé par le panel, et `list` les expose pour triage humain ;
  5. la propriété « zéro session observée candidate au sweep » est **vérifiée comme
     invariant en test**, pas publiée comme résultat de soak.

### 4.2 — Sweep dry : prédicat enrichi (le DRY est DÉJÀ armé) — bloqué par Q5
- **État mesuré le 2026-08-18** : le dry est armé depuis le 2026-08-18 par décision
  opérateur (drop-in `killswitches.conf`, sur le prédicat heartbeat-seul actuel —
  `pg_brain_session.py`, vérifié). Cette étape n'arme donc pas le dry : elle
  **enrichit le prédicat** que le dry déjà armé évalue. Une version antérieure la
  présentait comme une bascule future « bloqué par Q5 » — recopie d'un état du
  2026-08-16, corrigée ; ce qui reste bloqué par Q5, c'est l'enrichissement du
  prédicat et la suite (4.3).
- Prédicat enrichi, **toujours UN statement** (invariant C12, test d'ancrage
  reconduit) :

  ```sql
  ... WHERE status = 'open'
        AND GREATEST(last_heartbeat_at,
                     COALESCE(last_observed_at, last_heartbeat_at)) < :cutoff
  ```

  Le checkpoint (Phase 2) rafraîchissant `last_heartbeat_at`, ce prédicat encode **les
  trois signaux — heartbeat, checkpoint, observation — dans deux colonnes et un seul
  statement**, réévalué sous verrou de ligne. **Mais la règle « les trois signaux,
  pas un seul » n'est PAS une propriété intrinsèque du prédicat** : elle dépend de
  deux armements indépendamment refusables. Si Q1=non ou observation non armée (4.1),
  et/ou Q3=non (pas de checkpoint), `GREATEST(...)` dégénère en heartbeat seul — le
  mode que l'ADR §3.3 écarte parce que `2bd14b24` a condamné ce signal. La dépendance
  est déclarée ici et verrouillée en 4.3. Changement monotone sûr : ne peut
  qu'abandonner *moins* de sessions.
- ≥ 7 j de listes de candidats revues par l'opérateur.

### 4.3 — Sweep wet (`DRY_RUN=false`) — précondition trois-signaux DURE
- **Précondition d'armement, fail-closed** : le WET n'est armable que si les trois
  signaux existent réellement — **Q1=oui ET 4.1 armé + soaké, ET Q3=oui ET checkpoint
  livré**. À défaut, le prédicat dégénère en heartbeat seul (la configuration
  condamnée par `2bd14b24`) : armer quand même redevient une **décision opérateur
  nouvelle, prise en nommant explicitement le mode dégradé heartbeat-seul** — jamais
  un enchaînement par défaut de 4.2. Q5 seul ne suffit pas ; une version antérieure
  laissait ce chemin dégradé armable sans le déclarer.
- Critères : le stock de fantômes > 7 j (**21 balayables re-mesurés le 2026-08-19**,
  pour 29 sessions `open` sur 467 lignes — contre 18 le 2026-08-18 ; daté, périssable,
  à re-mesurer avant chaque décision d'armement) tend vers zéro sans ménage manuel ; **zéro abandon d'une session à
  activité observée ou checkpoint récent** — et le résidu nommé en 4.1 (sessions sous
  ambiguïté ou sans acteur, jamais observées) est publié avec chaque liste
  d'abandons ; le compte d'abandons manuels mensuels tend vers zéro (mesure de
  fermeture du régime B1).

### 4.4 — Capture préparée (migration M-E + `BRAIN_SESSION_STAGED_CAPTURE_ENABLED`) — bloqué par Q6 (amendement de covenant, soumis explicitement)

> **REMONTÉE JUSTE APRÈS LA PHASE 2 par le cadrage du 2026-08-19.** C'est la tranche
> qui ferme **B3**, promue douleur n° 1 par l'axe « traçabilité du savoir ». Deux
> conséquences : **Q6 devient un critère de sortie de Phase 0** (elle ne gate plus une
> tranche de fin de plan), et son **soak de 14 jours démarre bien plus tôt** — c'est le
> gain principal du reséquencement, puisque ce soak était le chemin critique de la
> fermeture de B3. Prérequis dur inchangé : la **Phase 1** (résolveur de projet par tool
> et `started_by_actor`).
- **Migration M-E** : table `brain_session_staged_captures` — PK
  `(session_id, knowledge_id)` (un brouillon est une hypothèse : **pas** de PK sur
  `knowledge_id` seul ; seule la promotion dans `brain_session_artifacts`, PK
  `knowledge_id`, confère l'exclusivité), `knowledge_type`, `observed_at`,
  `source='provenance'`, `status` CHECK ∈ {staged, promoted, dismissed}. Downgrade
  fail-closed si des lignes `staged` non résolues existent.
> **AMENDÉ le 2026-08-20 — §0bis.2 et §0bis.5 font foi.** La puce ci-dessous décrit la
> liaison de l'écrivain par **`(project_key, started_by_actor)` avec la règle « exactement
> un » (R5)**. C'est **PÉRIMÉ** : la clé est `(projet, CONNEXION)`, et sur la connexion
> **la liaison est EXACTE** — il n'y a plus de règle d'ambiguïté à appliquer. Le §0bis.5
> le dit lui-même : *« Q6 perd son principal risque d'implémentation avant même d'être
> armée »*, la population des 24 `open` ambiguës sur 29 cessant de le concerner.
>
> **CE QUI RESTE VRAI dans la puce, et qui compte** : « jamais `access_log` » (cette table
> est un tampon purgé, mesuré à 0 ligne) ; la consommation du **résolveur de projet par
> tool** livré en Phase 1 — sans lui l'écrivain n'a pas de `project_key`, le middleware ne
> lisant que des en-têtes ; le **plafond 500/session** et son compteur
> `staged_capture_skipped{overflow}` en métriques process ; et l'enveloppe `1c40c36a`
> (l'échec de l'observateur ne peut pas casser l'appel qu'il observe).
>
> Voir `SPEC-pool-brouillons.md`, qui propose le second plafond que 500/session ne donne
> pas : sous l'ouverture automatique, rien ne borne le NOMBRE de sessions.

- Écrivain middleware derrière le flag, **liaison `(project_key, started_by_actor)`
  avec la même règle « exactement un » (R5) — jamais `access_log`** (correctif du
  panel sur la greffe C ; et cette table est un tampon purgé, cf. §3.5). **Il consomme
  le résolveur de projet par tool livré en Phase 1** — sans lui il n'a pas de
  `project_key`, le middleware ne lisant que des en-têtes (§3.6). Plafond 500/session :
  au-delà on cesse d'observer et le
  compteur métrique `staged_capture_skipped{overflow}` compte (emplacement du compteur
  spécifié : métriques process, comme les compteurs d'observation — réponse à la
  question du panel « où vit le compteur ? »). Enveloppe `1c40c36a` + test de panne
  injectée, comme en Phase 1.
- **Promotion uniquement par commande explicite** : `capture` ou
  `end(capture_staged=true)`, qui repasse par `_validate_captures` **avant**
  l'évaluation du XOR ledger/raison. Un rejet est journalisé `dismissed`, pas
  supprimé. Le serveur prépare, l'opérateur signe.
- Sortie (soak 14 j armé sur brain-v42 seul) : taux d'attribution ≥ 2× la baseline
  Phase 0 sur le projet pilote ; zéro échec de tool causé par l'écrivain ; ratios
  staged/promoted/dismissed publiés.
- **Horizon documenté, jamais livré ici** (greffe A retenue par deux juges) : si la
  promotion signée plafonne, l'attribution à la création (même transaction que
  l'artefact, `method='auto_provenance'`) est la fermeture totale de B3 — changement
  de covenant plein (E3), à trancher par l'opérateur sur le dossier chiffré que
  produisent 4.1–4.4 (candidats non capturés, `dismissed`, ambiguïtés).

### 4.5 — Réattribution explicite (migration M-F) — bloqué par Q8
- Table `brain_session_attribution_moves` : `knowledge_id`, `from_session_id`,
  `to_session_id`, `reason TEXT NOT NULL`, `moved_by VARCHAR(64) NOT NULL`,
  `moved_at` — **sans FK piégée** (le panel a rejeté chez A la combinaison CHECK
  propriétaire × `ON DELETE SET NULL`). L'exclusivité devient « un seul propriétaire
  courant », l'histoire intégralement journalisée ; débloque les artefacts verrouillés
  par les fantômes balayés. Geste opérateur (script de maintenance ou tool — modalité
  incluse dans Q8), jamais silencieux.

### 4.6 — Lectures famille (`include_descendants`) — bloqué par Q4
- Paramètre opt-in sur briefing/search scopés, via **le** prédicat partagé de Phase 2
  (jamais un second exemplaire), derrière son flag propre
  `BRAIN_READ_INCLUDE_DESCENDANTS_ENABLED=false` — ajouté au §8bis, dont une version
  antérieure l'omettait alors que le récapitulatif se veut exhaustif (R3). Brique de
  fermeture de B11 sans gonfler le pool dream.
- **Interaction avec le scope capability dream, spécifiée — le périmètre est ARMÉ en
  production depuis le 2026-08-10** : sous un bearer `(projet, phase)`,
  `brain_service` force `project_key = scope.project_key` en **égalité stricte**
  (`services/brain_service.py`, vérifié ; propriété mesurée au cutover : 751/2760
  learnings sous scope `red`). Honorer `include_descendants` sous scope ferait lire
  `pk:*` à un bearer scopé sur `pk` — l'élargissement d'un périmètre de sécurité
  armé. Règle : **sous scope dream, le paramètre est REFUSÉ fail-closed** (erreur
  explicite — jamais ignoré en silence, ce qui cacherait la sémantique). Élargir un
  bearer aux sous-partitions serait une décision de sécurité opérateur distincte,
  hors de ce plan, avec sa propre matrice de registre.
- **Chiffré pour que personne ne le découvre une nuit — et au présent, pas au
  conditionnel.** Une version antérieure écrivait « **si** E5 conduisait plutôt à
  ajouter des clés colon au pool dream… » : c'est **déjà fait, en partie, depuis le
  2026-08-10**. Drop-in `killswitches.conf` lu le 2026-08-18 :
  `BRAIN_DREAM_PROJECT_POOL=…,red-shrik:agent,…,red-lab:architect,…` — deux des six clés
  colon sont des membres à part entière du pool, soit 447 des 533 artefacts colon
  re-mesurés (84 %). Le remède « ajouter les six clés » de la spec `dbb7c5ce` est donc
  **2/6 exécuté**, et le pool est **au plafond de dix**. Conséquence pour les quatre
  clés restantes (86 artefacts) : les entrer exige de relever `_MAX_POOL`
  (`tests/unit/test_dream_systemd_timeout_covers_the_pool.py:41`) **et**
  `TimeoutStartSec` ensemble — le test échoue si on n'en change qu'un — plus la matrice
  complète du registre `MCP_HTTP_DREAM_TOKENS` (six phases × projet ; préflight
  fail-closed : sans elle **la nuit entière** échoue). Ce n'est pas une hypothèse
  d'avenir, c'est le coût du prochain cran.
- La colonne `parent_key` + CHECK (design de la proposition A) reste l'option
  documentée pour E5 : **prédicat d'abord, colonne éventuelle après preuve d'usage**
  (doctrine du panel).

### Rollback global de la phase
Les gestes **de flag** (4.1, WET de 4.3, flags de 4.4 et 4.6) sont des drop-in/env
réversibles en un fichier ; l'ordre inverse les désarme proprement. Les **trois autres**
ne le sont pas et suivent R1 : le prédicat enrichi de 4.2 est un déploiement de code
(revert de commit + redémarrage MCP) ; M-E et M-F sont des têtes Alembic à downgrade
fail-closed, donc rollback séquentiel depuis la tête courante, jamais sautable. Elles ne
sont écrites que si leurs questions sont tranchées en leur faveur.

---

## 7. Cartographie douleur → phase → mesure de fermeture

| Douleur | Se ferme en | Comment on le MESURE |
|---|---|---|
| **B1** fantômes subagents (critique) | Triage : P1 (`intent`, `open_sessions_same_carrier`, `started_by_actor`). Régime : P4.2-3 (sweep armé). **Résidu conditionné à Q9** (subagents) **et Q12** (cycle de vie automatique, réponse établie `2bd14b24`) — assumé | Stock de sessions > 7 j → 0 sans ménage manuel ; abandons manuels/mois → 0 ; part de sessions neuves avec `intent` et `started_by_actor` renseignés |
| **B2** heartbeat menteur (critique) | Faux-mort : P4.1-2 (observation + prédicat GREATEST, monotone sûr) — **pour les sessions observées seulement** : sous ambiguïté R5 ou stdio sans header, pas d'observation — résidu nommé en 4.1, défendu par checkpoint/heartbeat seuls. Faux-vivant : P2 (checkpoint date le vivant **s'il est appelé** — politique d'appel dans Q3, effet heartbeat derrière son flag) + P4.3 (trois signaux, précondition dure) | **Compte « ambiguë ET candidate au sweep »** (le seul qui puisse échouer) ; `skipped{ambiguous, no_actor, no_project}` ; comparaison à la population prédite en P0 ; `observed_only` publié ; zéro abandon erroné en wet. « Zéro session observée candidate au sweep » est un **invariant testé**, pas une mesure de soak (§6, 4.1) |
| **B3** capture 18 %/34 % (haute) | P1 (suggestions, instrument) → P4.4 (staged signé, ≥ 2× baseline) → fermeture totale = décision E3 (horizon documenté) | Taux de capture et d'attribution vs baseline P0, par phase ; ratios staged/promoted/dismissed |
| **B4** tickets + lot en bloc | P1 (erreur énumérée) + P2 (M-B, prédicat `from_project`) | Canary : lot `[learning, ticket]` passe ; mauvais `from_project` rejeté `wrong_project` |
| **B5** fenêtre rigide | **Partiellement** : P2 ne relaxe que l'axe PROJET (flag famille parent→enfant, prédicat partagé — armement Q4). **Résidu assumé, non traité par ce plan** : l'axe TEMPOREL (`created_at >= started_at`) reste intact — un artefact créé avant le `start` demeure incapturable (vécu `d30cf6e5`) ; le relâcher toucherait la sémantique de provenance du ledger et n'est instruit par aucune phase ni question — à soumettre si la douleur persiste | Canary flag armé : capture `pk` → `pk:child` passe ; flag fermé : ancrage inchangé ; résidu temporel visible dans l'erreur `created_before_session` (P1) |
| **B6** focus sans garde (haute) | P3 (audit append-only + constraint trigger différé + `focus_diff` + runbook). Prévention dure = Q7 | **Constraint trigger différé vert** (un UPDATE de `current_focus` sans ligne d'historique avorte au COMMIT) + canary sur CHAQUE écrivain, **y compris les deux chemins d'INSERT que le trigger ne voit pas** (`create`, branche INSERT de `get_or_create` — §5.2) ; drill de récupération réussi ; `focus_diff` rendu sur canary. **PAS la jointure focus↔historique** : elle serait verte à 100 % sur une base sans écriture post-M-D — critère trivialement satisfiable, que le §5 disqualifie et qu'une version antérieure de cette ligne réinstallait six sections plus loin |
| **B7** aucun checkpoint | P2 (M-C + tool, gated Q3, doctrine de fraîcheur `d04dc588` reprise — **avec DEUX divergences déclarées, stockage ET forme du payload**, cf. §4.4 et Q3(c)(d) ; la spec checkpoint séparée que l'audit du ticket exige est livrée en P0) | Cycle réel start→checkpoints→end ; `list` montre `last_checkpoint_at` ; fraîcheur affichée dérivée de l'âge du checkpoint |
| **B8** X-Brain-Session mort (contrainte) — **cotée sur une mesure périmée** : spike mesuré sur Claude Code 2.1.220, `claude --version` = 2.1.234 le 2026-08-19 | **P0 d'abord : re-jeu du spike** (§2, contenu n° 6) — c'est la contrainte qui dimensionne D1, D5, D8, R5, N2, le résidu de D6 et le critère n° 1 de 4.1. Puis assumée dès P1. **AMENDÉ 2026-08-20** : l'identité N'EST PLUS `(projet, started_by_actor)` mais `(projet, CONNEXION)` (ADR §0bis.2), et stdio ne dégrade plus « sans attribuer » — il n'ouvre **AUCUNE** session automatique (ADR §0ter.2), le cycle explicite y restant inchangé | Spike rejoué, résultat daté avec sa version en tête ; **part des appels portant un `X-Brain-Session` normalisable** (mesure absente de la baseline jusqu'ici) ; part de sessions neuves avec `started_by_actor` non NULL vs baseline ; compteur `skipped{no_actor}` |
| **B9** `client_key` libre | P1 (`intent` + convention documentée + `client_key_prefix`) | Part de sessions neuves avec `intent` ; triage des fantômes démontré dans `list` |
| **B10** drift fantôme (fermée) | P0 (tests d'ancrage — ne jamais régresser) | Ancrage canonicalisation vert en CI, en continu |
| **B11** sous-projets : **86 artefacts sans run, 533 sans consolidation croisée** (moyenne — revue à la baisse, cf. §0) | **Déjà 2/6 fermée hors de ce plan** : `red-shrik:agent` et `red-lab:architect` sont au pool dream depuis le 2026-08-10 (447/533 artefacts, 84 %). Reste : P2 (prédicat partagé + capture famille), P4.6 (`include_descendants`, refusé sous scope dream) pour la consolidation **croisée** ; les 4 clés hors pool exigent `_MAX_POOL` + `TimeoutStartSec` + matrice de registre. Décision de fond = **Q4** (un renvoi antérieur vers Q5 était erroné — Q5 est le sweep) | Masse colon **re-mesurée** en P0 (pas recopiée de `dbb7c5ce`) ; part de cette masse couverte par un run nocturne ; après armement éventuel : le briefing du parent compte les artefacts des enfants |
| **B12** pas de doc projets | P0 (`docs/PROJECTS_SYSTEM.md`, grille fait/jugement) | Doc relue/livrée ; les quatre briques y sont reliées et le prédicat colon y est recensé **au complet — cinq exemplaires `src/` + sept vues issues de la 036, en trois formulations** (« l'exception unique » d'une version antérieure de cette ligne est la formulation que le §2 déclare fausse ; ne pas la réinstaller comme mesure de fermeture) |
| **B13** erreur indifférenciée | P1 (rejections par id + `capturable_subset`) | Canary : chaque id rejeté porte sa raison ; test d'ancrage de la nouvelle forme |
| **B14** acteur non persisté | P1 (M-A, `started_by_actor VARCHAR(64)`) | Colonne renseignée sur les nouvelles sessions HTTP, part mesurée |

---

## 8. Récapitulatif migrations (couloir du pin — R1)

| Tête | Phase | Contenu | Downgrade | Attestation `ops/recovery/` | Conditionnelle ? |
|---|---|---|---|---|---|
| (046) | — | Dimension embedding, **en projet, pas écrite** (`alembic/versions/` s'arrête à 045) — hors de ce plan ; ordre libre, jamais en vol en même temps (R1.2/R1.6) | — | à sa charge | — |
| M-A | 1 | **4** colonnes nullable `brain_sessions` (`started_by_actor` 64, `last_observed_at`, `intent` 500, **`nature`** — cadrage 2026-08-19) **+ la colonne de CONNEXION et son index UNIQUE**. ✅ **Décision d'index MESURÉE ET CONCLUE le 2026-08-19** (`baseline/README.md`), et elle a changé d'objet : **pas d'index sur `started_by_actor`** (il sort du chemin chaud sous la clé `(projet, connexion)`), mais **index UNIQUE sur la connexion**, obligatoire — une égalité non couverte force un Seq Scan complet sur chaque appel outermost | Fail-closed si un `intent` non NULL (jugement) — purge de canary **ou** opt-in `-x` nommé | **empreinte colonnes `brain_sessions` à régénérer** (+ `COLUMN_DEFINITION_MD5` unitaire) ; **si un index est ajouté, `expected_session_indexes` (liste FERMÉE, `v4.sql:404-412`, contrôlée `:665` et `:687`) casse AUSSI** — plus `SESSION_INDEX_DEFINITION_MD5` (`test_recovery_contract_v3.py:164-168,488`) | Non |
| M-B | 2 | CHECK artifacts + `'ticket'` (8e valeur) | Fail-closed si lignes `ticket` (canaries purgées, §4) | **`expected_artifact_constraints` à régénérer** ; et `knowledge_sources` ne connaît pas les tickets ⇒ **Q14 avant** | Q2 (prédicat), **Q14** |
| M-C | 2 | `brain_session_checkpoints` (trigger append-only, replay idempotent `(session_id, seq)`) | Fail-closed si lignes (canaries purgées, §4) | `table_set` (nouvelle table) | **Q3** |
| M-D | 3 | `project_focus_history` (`focus` NULLABLE, trigger append-only + **constraint trigger différé** `AFTER UPDATE OF current_focus` sur `project_contexts`, **créé désactivé**, seed NULL compris, six écrivains + scrub consolidés ; **pas de trigger-garde de révision — la 032 le fait déjà** ; **portée INSERT à trancher à l'écriture** — `create`/`get_or_create` échappent à `AFTER UPDATE`, §5.2) | Fail-closed hors seed **avec opt-in nommé** (gabarit 039, `-x allow_focus_history_downgrade=yes`) | **`expected_runtime_user_triggers` (13 triggers / 5 tables, fermée) + `table_set` à régénérer — régénération posée AVEC l'activation du trigger, la fenêtre désactivée étant rouge (`tgenabled='O'`)** | Non (Q13 module l'upsert) |
| M-E | 4.4 | `brain_session_staged_captures` | Fail-closed si `staged` non résolus | `table_set` | **Q6** |
| M-F | 4.5 | `brain_session_attribution_moves` | Garde non-vide | `table_set` | **Q8** |
| **M-G** | **UNE TÊTE AVEC M-A** (signé 2026-08-20, ADR §0ter.1) — *rang dans le couloir toujours à séquencer* | **Nouvelle branche terminale de `brain_sessions_terminal_state_valid` pour l'auto-fermeture des sessions de nature `agent`** (cadrage 2026-08-19, Q15 = route (3) ; ADR §0.4 et D11). **La seule tête de ce plan qui touche le NOYAU** — toutes les autres travaillent en périphérie. Branche exacte non spécifiée ; **double rail Pydantic à faire bouger avec le CHECK** (C7) | Fail-closed si des lignes portent le nouvel état ; **le downgrade 037→036 doit apprendre cet état**, sans quoi il perd des sessions terminales sans le dire | **Empreinte du CHECK terminal de `brain_sessions` à régénérer, sur les DEUX assets v4.** C'est l'objet le plus surveillé de l'attestation | **Q15 répondue ; le CONTENU reste à spécifier (critère de sortie de Phase 0)** |

**Lecture de la colonne « Attestation » — quatre avertissements** (deux à l'origine, deux ajoutés par le cadrage du 2026-08-19)**.** (1) « Régénérer
`ops/recovery/` » veut dire **les deux assets v4** : `brain-v42-v4.sql` **et**
`brain-v42-v4-pgrestore.sql`. Cette seconde variante n'était nommée nulle part dans les
trois documents (`grep -c pgrestore` = 0/0/0 le 2026-08-19) alors qu'elle est vivante et
testée — `tests/integration/db/test_recovery_contract_v4_execution.py:106` exécute **les
deux** contre une base réelle, et
`tests/unit/test_recovery_contract_v4_pgrestore.py:29-33` impose la **parité des CTE**
(écart autorisé : `{observed_artifact_constraints, observed_session_constraints}`, et
aucun CTE côté live qui n'existe côté pgrestore). Régénérer un seul asset laisse la
preuve de restauration à moitié fausse et fait rougir la parité au premier CTE ajouté.
(2) Le tableau suppose qu'**aucune tête n'ajoute d'index sur `brain_sessions`** ; si la
décision d'index de M-A est prise, voir R1.5 (« trois des six têtes » devient « trois
têtes, deux structures » ou « quatre têtes sur sept »). ~~**(3) Les comptes de têtes de ce
tableau et de R1.5 datent d'avant M-G et n'ont pas été recalculés** — M-G est une
septième tête du couloir (huitième avec la 046), et elle touche l'empreinte la plus
surveillée. Recompter fait partie de la spécification de M-G, critère de sortie de la
Phase 0.~~

> **AMENDÉ le 2026-08-20 — RECOMPTÉ, et la note (3) est doublement périmée.**
> Source : `SEQUENCEMENT-2026-08-20-couloir-du-pin.md`, décision `9d22bc6a` (S6).
>
> **(i) M-G n'est PAS une septième tête.** Elle ne consomme aucun rendez-vous propre :
> elle part **dans la 046, avec M-A** (ADR §0ter.1, signé). Le couloir compte
> **12 candidats en file** et **4 hors file**, pour **8 rendez-vous** sous l'Ordre B
> signé : `046 = M-A+M-G` → `047 = M-B` → M-C et M-E **séparées** (tant que S9 n'a pas
> démontré l'indépendance de leurs downgrades) → `049 = M-D` **isolée** → `050` = trio
> `ADD COLUMN` nullable → `051`/`052`. Le recomptage réclamé par cette note est **fait**,
> et il ne vivait pas dans la spec de M-G mais dans un dossier à lui.
>
> **(ii) La prémisse de la note (2) est tombée aussi.** Elle suppose « qu'aucune tête
> n'ajoute d'index sur `brain_sessions` ». **La décision d'index de M-A est prise**
> (`c3d09355`) : un index **UNIQUE PARTIEL** (`WHERE status = 'open'`) sur la colonne de
> connexion. Il casse donc `expected_session_indexes`, la liste FERMÉE des deux assets v4
> — quatrième mécanisme de casse d'attestation.
>
> **(iii) Et le cadre entier de ces notes a changé.** Le dossier §0.1 mesure que
> **l'attestation est DÉJÀ rouge en production** avant toute nouvelle tête —
> `alembic_head` **039 ≠ 045** et `indexes` **128 ≠ 129**. Donc « quelles têtes coûtent une
> régénération d'assets » n'est plus la bonne question : **elles la coûtent toutes**.
> C'est ce qui a produit la signature **S1** — contrat DR **v5 unique à head DÉRIVÉ**, un
> seul mint pour tout le couloir au lieu de 7-12. **(4) M-A gagne une quatrième colonne**, `nature` (ADR D1 et D11, cadrage du
2026-08-19) : la ligne M-A ci-dessus en annonce trois et n'a pas été réécrite, faute de
spécification de sa contrainte et de son défaut en base.

Chaque tête, **dans le même commit** (R1.1 complet) : migration + bump
`_REQUIRED_ALEMBIC_HEAD` + test du pin + README/MCP_TOOLS (`migration {head}`) +
ARCHITECTURE (`migrations 001–{head} defined`) + SCHEMA.md (tables, révisions, « La
cible du dépôt est {head}. ») + `docs/OPERATIONS.md:118` +
`test_recovery_contract.py:279` + les deux `table_set` gelés (`…py:292`, `…_v2.py:33-39`)
+ régénération de `ops/recovery/` — **`brain-v42-v4.sql` ET `brain-v42-v4-pgrestore.sql`** —
quand la colonne « Attestation » l'indique + renommage
du test-garde head-nommé. CLAUDE.md est mis à jour **hors commit** (gitignoré).
Jamais deux têtes du couloir en vol — 046 comprise, dans l'ordre qui se présente ;
production mesurée avant la tranche suivante ; redémarrage MCP + canary après chaque
application ; **pour M-D, activation du constraint trigger APRÈS le redémarrage**
(R1.3) ; purge de canary — ou opt-in de downgrade — rejouée là où le downgrade est
fail-closed (§3, §4, §5).

## 8bis. Récapitulatif killswitches (R3)

| Flag | Né en | Ce qu'il arme | Défaut |
|---|---|---|---|
| `BRAIN_SESSION_OBSERVED_ACTIVITY_ENABLED` | P1 | Émetteur `last_observed_at` (UN statement, « exactement un ») | `false` |
| `BRAIN_SESSION_CAPTURE_SUBPARTITIONS` | P2 | Capture parent→enfant via le prédicat partagé | `false` |
| ~~`BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT`~~ | ~~P2~~ | ❌ **SUPPRIMÉ — n'a plus d'objet depuis le cadrage du 2026-08-19 (ADR §0bis.4).** Ce flag n'existait que pour rendre armable la fourche Q3(a), dont le danger était : « un agent qui checkpointe seul garde sa session vivante indéfiniment ». Sous Q12 = (a) + ouverture automatique, la vivacité d'une session agent vient de `last_observed_at`, qui bouge à CHAQUE appel d'outil — le checkpoint cesse d'être spécial, et une session `operator` n'est jamais fermée par inactivité. Il n'y a plus rien à armer | — |
| **`BRAIN_SESSION_IDLE_CLOSE_SECONDS`** | **(nouveau, cadrage 2026-08-19)** | **Seuil d'inactivité observée qui auto-ferme une session de nature `agent`. 4 h SIGNÉES le 2026-08-20** — seuil d'**ÉLIGIBILITÉ** au sweep nocturne, PAS un délai (latence pire cas ≈ 28 h ; ADR §0ter.5).** Réglage PROPRE — surtout pas `MCP_HTTP_SESSION_IDLE_SECONDS` (900 s), qui gouverne un objet réseau et n'a pas à piloter un objet de connaissance. **Ne touche JAMAIS une session `operator`** | à spécifier |
| `BRAIN_SESSION_STAGED_CAPTURE_ENABLED` | P4.4 | Écrivain de brouillons staged | `false` |
| `BRAIN_DREAM_SWEEP_ENABLED` / `BRAIN_DREAM_SWEEP_DRY_RUN` | existants | Sweep 7 j (prédicat enrichi P4.2) | `false` / `true` (code) — **production mesurée 2026-08-18 : `true` / `true`, DRY armé par décision opérateur** |
| `BRAIN_READ_INCLUDE_DESCENDANTS_ENABLED` | P4.6 | Lectures famille `include_descendants` (REFUSÉ fail-closed sous scope dream) | `false` |

Chacun : test fermé-par-défaut obligatoire, armement par runbook, état production
mesuré (drop-in + processus) — jamais présumé.

---

## 9. Questions ouvertes bloquantes (détail dans l'ADR jumelle, §6)

> **Cadrage du 2026-08-19 — quatre lignes de ce tableau ont changé d'état.** Q1, Q10 et
> Q12 sont **répondues** ; Q6 reste ouverte mais **bloque désormais la Phase 0** ; **Q15
> est neuve**. La source unique de ces réponses est l'**ADR §0**, pas ce tableau.
> **Encore ouvertes et bloquantes pour sortir la Phase 0 : Q2, Q3, Q6, Q14.**

| # | Question | Bloque |
|---|---|---|
| Q1 | ✅ **RÉPONDUE (dérivée de Q12, 2026-08-19)** — pour la nature `agent`, `last_observed_at` **est** le signal de vivacité, par construction ; pour la nature `operator`, D5 reste de l'observation pure (ADR §0.2). **Corollaire encore OUVERT** : « exactement un » vs « toutes », sur 24 des 29 sessions `open` (mesuré 2026-08-19) | Le corollaire bloque toujours 4.1. **D5 cesse d'être optionnel** : il porte la moitié agent du modèle |
| Q2 | ✅ **RÉPONDUE (2026-08-19, session 2) : `from_project`**, la paternité — la capture répond à « qu'a PRODUIT cette session ». Mesuré : 231 tickets, **187 self** (question sans objet) et **44 cross-projet** (où elle tranche). All-or-nothing **confirmé**, non contesté | M-B débloquée, avec Q14 |
| Q3 | ✅ **RÉPONDUE (2026-08-19, session 2), après DISSOLUTION de ses deux sous-décisions piégées.** (a) et (b) n'ont plus d'objet : la vivacité d'une session agent vient de `last_observed_at`, donc le checkpoint cesse d'être spécial, et une session `operator` n'est jamais fermée par inactivité ⇒ `BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT` disparaît. Restait : **(c) stockage = la PROPOSITION** (append-only `(session_id, seq)`, replay idempotent — les retries d'agents sont la norme, C6) et **(d) forme = LE TICKET** (`progress` + `blocker\|null` + `next_step` en UN appel ; trois `kind` exclusifs laisseraient un instantané à moitié vide sans que le lecteur puisse le savoir). Divergence (d) **abandonnée**, (c) **maintenue**. ADR §0bis.4 | M-C débloquée. La **spec checkpoint séparée** reste due en P0 |
| ~~Q3 (texte d'origine)~~ | approbation produit (`d04dc588`) + (a) **cercle des appelants** (commande explicite seule, ou agent autonome = changement de covenant) + (b) **effet heartbeat et son mécanisme d'armement** (retiré du contrat, ou derrière `BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT` livré fermé — sans l'un des deux, (a) est tranchée par omission ; **et « retirer » n'est pas l'option neutre** : le ticket exige « Checkpoint réel rafraîchit heartbeat atomiquement ; replay non », donc le retrait est une **troisième** divergence d'avec son MVP) + (c) **divergence de STOCKAGE** (append-only + `(session_id, seq)` vs snapshot + CAS du ticket) + (d) **divergence de FORME DU PAYLOAD**, non déclarée jusqu'ici : `progress` + `blocker\|null` + `next_step` publiés ensemble « en un appel » contre `kind` mutuellement exclusifs + `note` unique | M-C (Phase 2) ; la **spec checkpoint séparée** est livrée en P0 dans tous les cas |
| Q4 | Armer capture famille ? Livrer `include_descendants` (refusé sous scope dream — élargir un bearer = décision de sécurité distincte) ? Direction E5 (hiérarchie vs platitude) ? **Reformulée sur l'état mesuré** : `red-shrik:agent` et `red-lab:architect` sont **déjà au pool** depuis le 2026-08-10 (84 % de la masse colon) — reste à trancher (a) accepte-t-on que le parent ne voie jamais l'enfant, ou veut-on `include_descendants` ? (b) entre-t-on les 4 clés `red-lab:*` restantes (86 artefacts), au prix de `_MAX_POOL` + `TimeoutStartSec` + matrice de registre ? (c) une **fusion** `red-shrik:agent` → `red-shrik` relève de Q11, pas d'ici | P2 armement, P4.6 |
| Q5 | Ordre/seuils du sweep (7 j ? durée du dry — DÉJÀ armé, mesuré 2026-08-18 ?) — le WET exige en sus la précondition trois-signaux de 4.3 | Phase 4.2-3 |
| Q6 | ✅ **RÉPONDUE (2026-08-19, session 2) : ACCEPTÉE**, et les brouillons non signés d'une traçante **SURVIVENT** dans un pool en attente de signature, hors session. Écartées : l'auto-promotion à l'auto-fermeture (ce serait **E3 pour toute la moitié agent** — covenant plein) et l'abandon des brouillons (détruire ce que la nature agent produit). **Sa fragilité technique disparaît** : la liaison `(project_key, started_by_actor)` + « exactement un » devient exacte sur la connexion. ADR §0bis.5 | M-E débloquée. **Ce que le pool ajoute à M-E reste à spécifier** : FK et `ON DELETE`, durée de vie, plafond, tool de signature hors session |
| Q7 | Garde dur de contenu du focus, ou audit + `focus_diff` suffisent ? | (optionnel, post-P3) |
| Q8 | Droit de réattribution journalisée, ou orphelinage = prix de la preuve ? | M-F (Phase 4.5) |
| Q9 | ✅ **RÉGLÉE (2026-08-19, session 2) PAR LA MESURE : HÉRITAGE.** Les subagents héritent de la session du porteur, sans tag, parce qu'ils partagent sa connexion. **Aucun des trois en-têtes ne les distingue** — `X-Brain-Agent` porte le PROJET, `X-Brain-Session` est mort (B8), `Mcp-Session-Id` porte la CONNEXION — et aucun ne le fera : la config des en-têtes est par serveur MCP, pas par subagent. ADR §0bis.2 | B1 résiduel fermé par l'auto-fermeture, pas par le tag |
| Q10 | ✅ **RÉPONDUE (2026-08-19)** : la liste **couvre** — aucun B15. Mais la **priorité est rejetée** : l'ordre vient de l'axe **« traçabilité du savoir »** (B3, B4, B5 en tête), plus de la gravité dérivée | Débloquée. **Emporte le reséquencement P0 → P1 → P2 → P4.4 → P3** et la promotion de Q6 |
| Q11 | Renommage/fusion de projets : chantier séparé souhaité ? | (hors plan) |
| Q12 | ✅ **RÉPONDUE (2026-08-19) : piste (a) — deux natures de session.** Agent traçante auto-fermée sans rituel, opérateur avec rituel. La nature est **déclarée** au `start` (B8 ne bloque donc pas), **défaut `operator` forcé par C2**. Voir ADR §0.1 et D11 | Débloquée, mais **amende C1** et **ouvre Q15**. Ce plan n'est plus « compatible avec les trois pistes » : il a une cible |
| Q13 | `brain_set_project_context` : `current_focus` omis cesse-t-il d'effacer le focus (omis ≠ effacement explicite) ? **Question reformulée : sa motivation chiffrée était fausse.** Les 10/59 contextes NULL sont **tous** à `focus_revision = 0` et `focus_updated_at IS NULL` ⇒ jamais écrits, pas effacés ; **zéro effacement mesuré en production**. Le canal existe dans le code, il n'a pas été observé en train de mordre. À trancher sur le raisonnement : défaut à corriger avant qu'il morde, ou sémantique d'upsert assumée qu'il vaut mieux ne pas changer sous les clients existants ? | M-D (Phase 3) |
| **Q15** | ✅ **RÉPONDUE (2026-08-19) : route (3) — nouvel état terminal.** *Question NEUVE, posée par aucune version antérieure.* Le CHECK `brain_sessions_terminal_state_valid` interdit `ended` sans `summary` **et** `next_focus` non vides, et impose `captured_knowledge_ids = {}` à `abandoned` (`037_session_lifecycle_v4.py:14-91`) : une session agent auto-fermée **sans rituel** n'a aucun état terminal disponible. Routes écartées : (1) `abandoned` — l'instantané terminal déclarerait zéro capture sur le chemin de capture principal ; (2) résumé synthétisé — c'est la piste (c) par la porte de derrière, objection C9 | **Migration M-G sur le noyau** (§8). Amende **C7**. **Son CONTENU reste à spécifier — critère de sortie de Phase 0** |
| Q14 | ✅ **RÉPONDUE (2026-08-19, session 2) : voie (a) — ÉLARGIR.** `knowledge_sources` s'ouvre aux tickets et son prédicat de projet au sous-arbre ; l'attestation reste verte ET continue de prouver ce qu'elle prétend. (b) « documenter le trou » écartée parce qu'elle creuse un trou dans la preuve de RESTAURATION, donc dans l'histoire DR, déjà un blocker ouvert. **Travail sur les DEUX assets v4**, pas un | M-B débloquée |
| ~~Q14 (texte d'origine)~~ | **Ce que l'attestation de récupération doit apprendre.** `ops/recovery/brain-v42-v4.sql` définit la légitimité d'un artefact capturé par l'UNION des **six** tables de connaissance et par `source.project_key = session.project_key`. Les tickets capturables (M-B) et la capture `pk → pk:child` produiraient des `artifact_source_mismatches` **permanents** — attestation rouge, alors que son runbook exige « all statuses are pass ». (a) élargir `knowledge_sources` aux tickets et son prédicat au sous-arbre, (b) restreindre le contrôle aux six types en documentant le trou, ou (c) renoncer à l'un des élargissements ? | **M-B (Phase 2)**, et le flag famille |

---

*Sources et vérifications identiques à l'ADR jumelle (code au 2026-08-18 ; tickets
`d30cf6e5`, `2bd14b24` — dont la réponse opérateur établie —, `d04dc588` (contrat relu
mot pour mot : `expected_checkpoint_revision, progress, blocker|null, next_step`,
critère « un appel »), `7ffe0e8a`, `c60d023d` ; spike
`docs/upstream/2026-08-06-claude-otlp-session-join.md` — verdict « JOINTURE
IMPOSSIBLE » ; le ticket `2dfbb83d` est fermé LIVRÉ, pas négatif ; spec `dbb7c5ce`
(chiffres du 2026-08-08, **re-mesurés ici**) ; learnings `7bc821a1`, `367e27ae`,
`1c40c36a` ; jugements du panel — non archivés dans le dépôt, à traiter comme contexte
de rédaction, pas comme preuves).
**Mesures production du 2026-08-18, lecture seule** : head `045` ;
`10/59 project_contexts` à `current_focus IS NULL`, **tous à `focus_revision = 0` et
`focus_updated_at IS NULL`** ; les **sept** triggers utilisateur de `project_contexts`,
dont `project_contexts_focus_revision_trigger` (032) ; `access_log` à **0 ligne** ;
**sept** vues `public` contenant `split_part` ; masse colon `red-shrik:agent` 312 /
`red-lab:architect` 135 / quatre `red-lab:*` restantes 86 (`red-shrik` parent : 245) ;
drop-in `killswitches.conf` — sweep `ENABLED=true` / `DRY_RUN=true`, pool à dix incluant
deux clés colon.
**Vérifications code ajoutées à cette passe** : `alembic/versions/032_brain_sessions.py:19-34`
et `001_initial.py:244-247` ; `036_codex_contract_views.py:23-45,205-227,230` ;
`039_project_context_timestamp_cas.py:17,337-339` (opt-in `-x` de downgrade) ;
`repositories/pg_access_log.py:38-113` + `services/decay_flusher.py` + `config.py:379` ;
`mcp/provenance_middleware.py:74-96` et `services/dream_project_scope.py:83-120` ;
`repositories/pg_project_context.py:202-213,281-290` ;
`services/project_group_ticket_service.py:129-137,164-167` ;
`repositories/pg_brain_session.py:520-522,713-714` ; `services/roadmap_service.py` ;
`ops/recovery/brain-v42-v4.sql` (352-380, **404-412**, 437-470, 533-557, **665, 687**,
895-945, 1083-1113, 1135-1180) **et `ops/recovery/brain-v42-v4-pgrestore.sql`** ;
`tests/unit/test_recovery_contract{,_v2,_v3,_v4}.py`,
`tests/unit/test_recovery_contract_v4_pgrestore.py:29-33`,
`tests/unit/test_recovery_contract_v3.py:164-168,488`,
`tests/integration/db/test_recovery_contract_v4_execution.py:106` ;
`tests/unit/test_documentation_contract.py:25-32,1815-1819` ;
`tests/unit/test_plan_index_repair_head_pin.py:45-52` ; `tests/integration/conftest.py:129-155` ;
`docs/OPERATIONS.md:118`. Répertoire migrations vérifié : `alembic/versions/`, head
versionnée 045, **aucune 046**. Aucun commit, aucune écriture brain, aucune écriture DB,
aucun fichier touché hors `docs/design/refonte-projets-sessions/`.*

**Passe du 2026-08-19 — ce qu'elle a changé, et pourquoi.** Deux des quatre corrections
portent sur des affirmations **introduites par la réparation précédente** : une prémisse
fausse peut survivre à sa propre correction, et c'est le mode de panne de ce dossier.

| Ce qui était écrit | Ce qui est vrai | Où |
|---|---|---|
| « `brain_update_project_focus` est le **seul** à bumper sur texte inchangé » | Le CAS de `end` (`pg_brain_session.py:713-714`) le fait aussi, sans comparer le texte, et le CHECK 037 l'exige : régime normal d'une fin de session | §5.1 |
| « un second incrément applicatif poserait `OLD+2` » | Le trigger 032 **assigne** (`NEW.focus_revision := OLD.focus_revision + 1`), il n'ajoute pas ⇒ les deux bumps explicites doivent **rester** | §5.3 |
| Le constraint trigger « attrape l'écrivain qui contourne le chemin partagé » | `AFTER UPDATE` ne voit pas les INSERT : `create` et la branche INSERT de `get_or_create` écrivent un focus à `focus_revision = 0` hors de sa portée — trou nommé, trois voies, N1 borné | §5.2, critères de sortie |
| Trigger « créé désactivé » + « `ops/recovery/` régénérée » (deux corrections indépendantes) | `v4.sql:913-918` exige `tgenabled = 'O'` : hors liste il est inattendu, dans la liste il est éteint — **aucune régénération ne rend l'attestation verte pendant la fenêtre désactivée** | R1.3, R1.5, §5 Rollback |

*Plus : `d04dc588` relu — retirer l'effet heartbeat serait une **troisième** divergence
(Q3(b)) ; champs de suggestion renommés `project_uncaptured_since_start(_count)`, la
version antérieure spécifiant le prédicat puis gardant l'ancien nom deux lignes plus
haut ; `expected_runtime_user_triggers` précisée à **treize** triggers sur cinq tables,
dont sept sur `project_contexts`. Mesures du 2026-08-19, lecture seule, inchangées
depuis la veille : head `045` ; `10/59` focus NULL tous à révision 0 et jamais datés ;
`access_log` 0 ligne ; sept vues `split_part` ; masse colon 312/135/64/15/5/2 = 533
contre `red-shrik` 245 ; drop-in sweep `true`/`true`, pool à dix.*

**Passe de pliage des résidus, 2026-08-19 (seconde passe du jour).** Six corrections,
dont trois majeures. Aucune n'ajoute de contenu neuf au plan : chacune répare un
inventaire ou une cotation que le plan croyait complets.

| Ce qui manquait ou était faux | Ce qui est vrai | Où |
|---|---|---|
| « régénérer `ops/recovery/` » lu comme **un** asset | **Deux** assets v4 : `brain-v42-v4-pgrestore.sql` porte les mêmes structures (12 lignes des quatre tokens, 15 avec les index, mesuré), est exécutée contre une base réelle (`…v4_execution.py:106`, `parametrize` sur les deux) et tenue en **parité de CTE** (`…v4_pgrestore.py:29-33`). `grep -c pgrestore` sur les trois documents rendait **0/0/0** | R1.1, R1.5, §3 et §5 critères, §8 |
| « L'attestation casse par **trois** mécanismes » | **Quatre** : `expected_session_indexes` (`v4.sql:404-412`, contrôlée `:665`/`:687`, doublée par `SESSION_INDEX_DEFINITION_MD5`, `test_recovery_contract_v3.py:164-168,488`) fige la liste **FERMÉE** des index de `brain_sessions` | R1.5 |
| « trois des six têtes », énoncé sans condition | Vrai **seulement si** aucune tête n'ajoute d'index sur `brain_sessions`. Sinon : trois têtes / deux structures pour M-A, ou **quatre têtes sur sept** | R1.5, §8 |
| Le mot « index » absent du plan | `started_by_actor` naît **sans index**, et l'émetteur D5 filtre dessus **deux fois par appel outermost**. Index mesurés le 2026-08-19 : `brain_sessions_pkey`, `uq_brain_sessions_project_client`, `idx_brain_sessions_project_status_started` — aucun ne couvre l'acteur. Décision **instruite, non tranchée** ; coût **mesuré en Phase 0** | §3.3, §3.6, §2 (baseline), §8 |
| B8 « Haute (contrainte) » sans date | **Cotée sur une mesure périmée** : spike mesuré sur Claude Code **2.1.220**, `claude --version` = **2.1.234** le 2026-08-19. Re-jeu du spike = **étape de Phase 0** (§2 contenu n° 6) avec critère, et la baseline mesure désormais la part d'appels portant un `X-Brain-Session` normalisable | §0, §2, §7 |
| Population d'ambiguïté présentée comme à découvrir en P0 | Mesure **partielle** déjà dans `7ffe0e8a` (2026-08-16 : `auto-discord` 6, `red-arena` 3, `claude-dev-pc`/`red-lab` 2), plafond par projet et non par couple ; re-mesurée le 2026-08-19 : **24 des 29 `open`** dans un projet à ≥2 | §2 (baseline) |
| « la production avance d'un cran (CAS 209→210) » | **Corroboration retirée** — ce CAS est un `brain_session_end` (DOSSIER §B6), pas `roadmap_service`, et rien ne dit que le TEXTE du focus ait changé. Preuve = source plpgsql seule (`032_brain_sessions.py:19-34`) | §5.3 |
| « 18 balayables » sans caveat | **29 `open` / 21 balayables >7 j / 24 stale >24 h** sur 467 lignes, mesurés le 2026-08-19 | §0, §6 (4.3) |

*Mesures de cette passe, lecture seule, datées et **périssables** : head `045` ;
`brain_sessions` 467 lignes, 29 `open`, 21 >7 j, 24 >24 h ; trois index sur
`brain_sessions`, aucun sur un acteur ; sessions `open` par projet ≥2 → `auto-discord` 8,
`brain-v42` 4, `red-arena` 4, quatre projets à 2 ; `claude --version` = 2.1.234 ;
`grep -c pgrestore` = 0/0/0 sur les trois documents avant la passe. Aucun commit, aucune
écriture brain, aucune écriture DB, aucun fichier touché hors
`docs/design/refonte-projets-sessions/`.*

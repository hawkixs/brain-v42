# Dossier d'instruction — refonte conjointe PROJETS + SESSIONS

**Statut : état des lieux, pas un go.** Le ticket d'ancrage `d30cf6e5` (2026-08-18) dit
explicitement « NE PAS DÉMARRER sans cadrage explicite de l'opérateur ». Ce dossier est
l'instruction du chantier pour trois architectes : surface mesurée, douleurs prouvées,
invariants, historique, et questions que seul l'opérateur peut trancher. Toutes les
mesures chiffrées sont datées et **périssables** — les re-mesurer, jamais les recopier.

---

## A. Surface actuelle mesurée

### A.1 Système PROJETS — quatre briques, aucune doc de bout en bout

**Brique 1 — Le format** (`src/brain_v42/models/project_key.py`, seule source de vérité) :

- Regex canonique `^[a-z0-9]+([:-][a-z0-9]+)*$` (kebab-case ; `:` accepté comme
  séparateur au même titre que `-`).
- Deux alias auto-canonicalisés, matchés exactement et sensibles à la casse :
  `brain` et `brain_v42` → `brain-v42` (`_ALIASES`).
- **Asymétrie écriture/lecture** : `canonicalize_project_key(value, strict=True)`
  (défaut, chemin d'écriture) lève `ValueError` avec suggestion sur toute clé non
  kebab-case ; `strict=False` (chemin de lecture) laisse passer tel quel — une lecture
  avec une mauvaise clé rend simplement zéro résultat.
- `None` traverse inchangé (connaissance globale non scopée).
- `ProjectKeyCanonicalMixin` applique la canonicalisation sur tout modèle Pydantic
  déclarant `project_key`. Le service session canonicalise en strict au `start`
  (`services/brain_session_service.py:101`) et en tolérant au `list` (`:166`).

**Brique 2 — La base** (`docs/SCHEMA.md`) — trois surfaces projet DISTINCTES :

1. `projects` (migration 033) : registre. PK `project_key VARCHAR(50)`, CHECK regex
   identique au code, `registry_status` ∈ {claimed, unclaimed, archived},
   `source` ∈ {context, reference, manual}. Un contexte crée une entrée `claimed` ;
   une simple référence crée `unclaimed` ; supprimer le contexte repasse en
   `unclaimed/reference`.
2. `project_aliases` (033) : `alias_key VARCHAR(128)` PK → `project_key` FK CASCADE.
   La migration a enregistré les alias historiques (`brain_v42`→`brain-v42`,
   `auto_discord`→`auto-discord`), normalisé les colonnes projet et
   `related_projects` à l'upgrade, puis des **triggers** appliquent la règle aux
   écritures suivantes. Le downgrade ne restaure pas les anciennes orthographes.
3. `project_contexts` (l'objet opérationnel réel) : `project_key VARCHAR(50) UNIQUE`,
   `current_focus`, `focus_revision BIGINT` (032, CAS), `focus_updated_at` (040,
   écrit par `db/focus_stamp` sous `IS DISTINCT FROM`, jamais par trigger, NULL =
   jamais mesuré), `related_projects TEXT[]`, `project_group VARCHAR(50)`, roadmap,
   compteurs. **Après 033, `project_contexts.project_key` est immuable** : renommer
   un projet exige une migration explicite, un UPDATE direct échoue.

Les tables de connaissance (`decisions`, `learnings`, `snippets` : `project_key
VARCHAR(50)` nullable ; `runbooks`, `adrs`, `indexed_plans` : NOT NULL) portent la clé
**sans FK** — la cohérence repose sur la frontière Pydantic + les triggers 033 qui
maintiennent le registre, les identités graph et l'outbox.

**Brique 3 — La « hiérarchie » est PLATE.** Aucun lien parent/enfant en base ; le
deux-points est une convention de nommage.

**Correction du 2026-08-18 — « une seule exception dans tout le code » était faux, et
ce sous-compte s'est propagé.** Ce dossier écrivait que `db/project_group_scope.py:20-27`
était l'unique endroit encodant la sémantique de sous-partition. Recompté grep en main,
il y en a **cinq dans `src/`** :
1. `db/project_group_scope.py:24-26` — `base_key NOT LIKE '%:%' AND project_key LIKE
   base||':%'` ;
2. `services/project_group_ticket_service.py:129-137` — copie SQL inline ;
3. `services/proposal_service.py:377-383` — copie SQL inline, alors même que le module
   importe `project_group_scope` ;
4. `services/project_group_ticket_service.py:164-167` — **copie en Python**, dans la
   même méthode `_lock_participants_scope` que la n° 2 ;
5. `repositories/pg_project_context.py:202-213` (`get_keys_by_group`) — variante
   `split_part`, invisible à un grep sur `not_like("%:%")`.
Et **sept vues vivantes** côté base (mesuré :
`select table_name from information_schema.views where table_schema='public' and
view_definition like '%split_part%'`), toutes issues de la migration **036**, nées de
deux corps de CTE recopiés (`_RED_KEYS_CTE`, `_BRAIN_RED_KEYS_CTE`) en
`split_part(project_key, ':', 1) <> project_key AND split_part(…) IN red_base`. La 024
n'est pas un second objet vivant : la 036 remplace sa vue par `CREATE OR REPLACE`.
**Trois formulations distinctes du même prédicat, donc, et non une exception unique.**

Ce qui reste vrai, et c'est le point de fond : partout **ailleurs**, égalité stricte —
le pipeline dream filtre `l.project_key = :pk` (spec `dbb7c5ce`, §2 — « il n'existe
aucun filtre par préfixe dans le pipeline »).

**Brique 4 — Chiffres.** *Ceux de la spec `dbb7c5ce` (2026-08-08) ont été re-mesurés le
2026-08-18 et ont bougé — les recopier serait la faute que ce dossier interdit en tête.*
- **2026-08-08 (spec)** : corpus 3 803 artefacts sur 54 `project_contexts`, 26 artefacts
  à `project_key IS NULL`, zéro contexte à clé nulle ; six clés colon à part entière ;
  `red-shrik:agent` 280 > `red-shrik` 222 ; **479 artefacts colon = 20,2 % de la masse
  du pool**, hors consolidation.
- **2026-08-18 (re-mesuré)** : corpus 4 316 artefacts sur **59** `project_contexts` ;
  `red-shrik:agent` **312** > `red-shrik` **245** (la proposition tient) ;
  `red-lab:architect` 135, `red-lab:orchestrator` 64, `:reviewer` 15, `:sentinel` 5,
  `:developer` 2 — **533 artefacts colon**.
- **Et le remède a commencé sans que ce dossier le sache.** La spec proposait « soit
  ajouter les six clés au pool ». Le drop-in `killswitches.conf`, lu le 2026-08-18,
  porte `BRAIN_DREAM_PROJECT_POOL=…,red-shrik:agent,…,red-lab:architect,…` **depuis
  l'ouverture du pool le 2026-08-10** : deux des six clés ont leur propre nuit, soit
  **447 des 533 artefacts (84 %)**. Le résidu sans aucun run est de **86 artefacts** sur
  quatre clés `red-lab:*`. Ce qui reste entier pour les six, en revanche, c'est la
  consolidation **croisée** : le pipeline filtrant en égalité stricte, la nuit de
  `red-lab` ne verra jamais `red-lab:architect`, fût-il dans le pool. Le pool est par
  ailleurs **au plafond de dix**.

### A.2 Système SESSIONS — schéma (migrations 032 + 037, lifecycle v4 en prod depuis le 2026-07-24)

`brain_sessions` : `project_key` **FK → `project_contexts(project_key)` ON DELETE
RESTRICT** (une session exige un contexte existant ; `start` sur projet inconnu →
NotFound). `client_key VARCHAR(128)`, `status` ∈ {open, ended, abandoned},
`started_focus` + `started_focus_revision` (snapshot au démarrage), `summary`,
`next_focus`, `captured_knowledge_ids UUID[]` (snapshot terminal, ≤100),
`nothing_to_capture_reason`, `abandonment_reason`, `end_expected_focus_revision`,
`focus_outcome` ∈ {applied, conflict}, `focus_at_end`, `focus_revision_at_end`,
`started_at`, `last_heartbeat_at`, `ended_at`.

- `UNIQUE (project_key, client_key)` : idempotence du démarrage. Conséquence dure :
  **une `client_key` ne peut jamais être réutilisée après état terminal**
  (`pg_brain_session.py:112-116` : « use a new client_key »).
- **Index — mesurés le 2026-08-19, exactement trois** (`pg_indexes` sur
  `brain_sessions`) : `brain_sessions_pkey` sur `id`,
  `uq_brain_sessions_project_client (project_key, client_key)`,
  `idx_brain_sessions_project_status_started (project_key, status, started_at DESC)`.
  Aucun ne porte sur un acteur — la colonne n'existe pas encore. Ce n'est pas une note
  d'exploitation : cette liste est **FERMÉE** dans l'attestation de récupération
  (`ops/recovery/brain-v42-v4.sql:404-412`, `expected_session_indexes`, contrôlée `:665`
  et `:687`, doublée par `SESSION_INDEX_DEFINITION_MD5` en
  `tests/unit/test_recovery_contract_v3.py:164-168,488`), de sorte qu'**ajouter un index
  à `brain_sessions` casse l'attestation** au même titre qu'ajouter une colonne ou un
  trigger. Cardinalité du jour : 467 lignes, 29 `open`.
- CHECK `brain_sessions_terminal_state_valid` (037) : machine d'états complète en SQL.
  `open` = zéro champ terminal ; `ended` = summary + next_focus non blancs, outcome
  non nul, cohérence arithmétique du CAS (`applied` ⇒ `focus_revision_at_end =
  end_expected_focus_revision + 1` et `focus_at_end = next_focus` ; `conflict` ⇒
  révisions différentes), et **XOR strict** ledger non vide / `nothing_to_capture_reason` ;
  `abandoned` = reason seul, aucun champ de fin, snapshot vide.
- Le modèle Pydantic (`models/brain_session.py:148-232`) refait ces validations côté
  application — double rail DB/app.

`brain_session_artifacts` : **PK `knowledge_id`** = exclusivité absolue (un artefact
appartient à une seule session, à vie). `knowledge_type` CHECK ∈ {decision, learning,
snippet, runbook, adr, indexed_plan, legacy}. `attributed_knowledge_ids` (champ dérivé,
pas une colonne) réhydrate le ledger dans tous les résultats, y compris pour une
session abandonnée.

Constantes (`models/brain_session.py:13-21`) : `MAX_CAPTURED_KNOWLEDGE_IDS = 100` ;
`SESSION_STALE_AFTER = 24 h` (flag **dérivé**, affiché, ne change aucun statut) ;
`AUTO_STALE_AFTER = 7 j` + `AUTO_STALE_ABANDONMENT_REASON = "auto_stale_7d"` (seul
chemin serveur).

### A.3 Les sept tools et leurs contrats exacts (`mcp/tools/session_lifecycle_tools.py`, version 4.0)

Bornes d'arguments : `project_key` ≤50 ; `client_key`/`expected_client_key` ≤128 ;
`summary`/`next_focus` ≤10 000 ; `reason` ≤2 000 ; `knowledge_ids` 1–100 ; `limit` ≤100.
Chaque docstring porte « No hook or auto-close may invoke this lifecycle boundary » —
le covenant est écrit dans le contrat du tool.

| Tool | Contrat | Idempotence / erreurs |
|---|---|---|
| `brain_session_start(project_key, client_key)` | Canonicalise strict ; INSERT `ON CONFLICT DO NOTHING` sur (projet, client_key) ; snapshot focus + révision ; briefing best-effort (échec ⇒ placeholder, session reste ouverte) | Replay si la paire existe et est `open` ; `ClientKeyConflictError` si terminale |
| `brain_session_resume(session_id, expected_client_key)` | Garde identité (paire UUID + clé) AVANT toute lecture mutante ; rend `current_focus`, `current_focus_revision`, briefing, `open_session_count` | Session non-open ⇒ erreur d'état |
| `brain_session_capture(session_id, key, knowledge_ids)` | Lock session FOR UPDATE puis contexte ; `_validate_captures` (`pg_brain_session.py:618-648`) : chaque UUID doit exister dans UNE des six tables `CAPTURE_TABLES` (`:55-62`), **même `project_key`**, **`created_at >= started_at`** ; lot all-or-nothing ; insert `ON CONFLICT DO NOTHING` + résolution de course ; rafraîchit le heartbeat atomiquement | Replay exact idempotent, y compris sur session terminée si tous les ids appartiennent déjà à la session ; sinon `StateError` |
| `brain_session_heartbeat(session_id, key)` | Rafraîchit `last_heartbeat_at` seul | Session non-open ⇒ erreur |
| `brain_session_end(session_id, key, summary, next_focus, expected_focus_revision, nothing_to_capture_reason?)` | Lit le ledger serveur (pas d'ids en entrée) ; XOR ledger/raison ; re-valide les captures ; CAS focus : révision courante ⇒ `applied` + focus écrit ; concurrente ⇒ `conflict`, focus intact, **session fermée quand même** ; fige `focus_at_end`/`focus_revision_at_end` | Erreurs identité/capture/état fail-closed, session reste ouverte ; replay d'un end persisté idempotent |
| `brain_session_abandon(session_id, key, reason)` | Ferme sans toucher le focus ; conserve les attributions du ledger | Replay idempotent si même reason ; sinon `TerminalConflictError` |
| `brain_session_list(project_key?, status, limit, offset)` | Canonicalisation tolérante ; filtres open/stale/ended/abandoned/all ; `stale` = filtre dérivé | Lecture pure |

### A.4 Écrivains périphériques

- **Sweep serveur** (`repositories/pg_brain_session.py:493-560`,
  `maintenance/session_sweep.py`) : seul chemin sans garde `expected_client_key`.
  **UN SEUL statement** UPDATE…WHERE…RETURNING — sous READ COMMITTED, le prédicat est
  réévalué sous verrou de ligne, donc un heartbeat qui commit pendant le balayage
  retire sa ligne (réponse directe au faux-mort du 2026-08-06). Livré fermé et dry.
  **ÉTAT CORRIGÉ — mesuré le 2026-08-18 : le DRY est ARMÉ.** Ce dossier affirmait
  « jamais armé en production », en recopiant une mesure du 2026-08-16 (`7ffe0e8a` :
  zéro env, zéro `abandonment_reason='auto_stale_7d'`) — l'état avait changé le
  2026-08-18 vers 20:52. Lecture directe du drop-in
  `brain-v42-dream.service.d/killswitches.conf`, lignes 53-63 : « SWEEP : armé (DRY) le
  2026-08-18, décision opérateur — reprise post-crash. 18 sessions fantômes balayables
  mesurées ce jour », puis `BRAIN_DREAM_SWEEP_ENABLED=true` et
  `BRAIN_DREAM_SWEEP_DRY_RUN=true`. **Le DRY liste sans écrire ; le WET n'a jamais été
  armé.** Le « 18 » est une citation du drop-in, pas une mesure courante : **re-mesuré
  le 2026-08-19, 29 sessions `open` dont 21 balayables >7 j et 24 stale >24 h, sur 467
  lignes** (`count(*) filter (where status='open')`, le même filtre sur
  `last_heartbeat_at < now() - interval '7 days'`, idem à 24 h). Daté et **périssable** —
  le rejouer, jamais le recopier ; l'ADR et le PLAN citaient ce 18 sans caveat. Un lecteur de ce seul dossier aurait tranché la question E3 sur un état faux.
- **ProvenanceMiddleware** (`mcp/provenance_middleware.py`) : lit `x-brain-agent` et
  `x-brain-session` sur CHAQUE appel de tool, voit le tool réel derrière
  `brain_call_tool`, avec ré-entrance (`enter_call`/`is_outermost_call`).
  `access_log.actor` (041) persiste l'acteur. Il **observe tout mais n'écrit rien**
  dans le cycle de vie — c'est le cœur du ticket `7ffe0e8a`.
- **Focus hors session** : `brain_update_project_focus(project_key, focus,
  expected_focus_revision, feature_status?)` — CAS lot atomique focus+roadmap, ne
  ferme aucune session. `focus_revision` appartient au **projet**, pas à la session.

---

## B. Douleurs PROUVÉES

**B1 — Le lifecycle déclaratif ne survit pas aux subagents éphémères.**
Source : ticket `2bd14b24` (red-shrik, 2026-08-06). 39 sessions abandonnées d'un coup
au ménage manuel (37 `red-writer`, 2 `red-shrik`), toutes sans summary ni capture —
quasiment une par subagent dispatché. « Ce n'est pas un incident, c'est le régime
permanent : sans ménage, ça recommence en deux semaines. » **Gravité : critique** —
c'est la douleur structurante du chantier.

**B2 — `last_heartbeat_at` ment dans les deux sens, le même jour.**
Source : ticket `2bd14b24`. Faux-mort : la purge a abandonné une session VIVANTE
(9b6f7e18, en plein chantier) parce que le heartbeat ne bouge que sur commande
explicite. Faux-vivant : 39 sessions mortes depuis deux semaines paraissaient
ouvertes. « Un signal qui se trompe dans les deux sens n'est pas à calibrer, il est à
remplacer. » **Gravité : critique.** L'auto-heartbeat (`7ffe0e8a`) règle le premier
cas seulement.

**B3 — Capture et heartbeat n'ont que des écrivains explicites quand le middleware voit tout.**
Source : ticket `7ffe0e8a` + code. Mesures 2026-08-16 (périssables) : 23 sessions
`open`, 21 stale >24 h, 8 >7 j ; depuis le 2026-08-01, **18 %** des sessions fermées
capturent (52/291) et **34 %** des artefacts sont attribués (226/661). La provenance
existe (X-Brain-Agent, `access_log.actor`) mais n'alimente ni heartbeat ni ledger.
**Gravité : haute** — la raison d'être des sessions (traçabilité du savoir) est
réalisée à un tiers.

**B4 — Le ledger de capture refuse par TYPE : les tickets ne sont pas capturables, et un lot mélangé est fail-closed sur son pire élément.**
Source : ticket d'ancrage `d30cf6e5` (vécu du 2026-08-18) + code. `CAPTURE_TABLES`
(`pg_brain_session.py:55-62`) énumère six tables ; les tickets n'y sont pas.
`_validate_captures` (`:618-648`) rejette **tout le lot** en une seule
`BrainSessionInputError` dès qu'un id est invalide. Un agent qui capture
[learning, ticket] perd aussi le learning. **Gravité : moyenne**, ergonomie + trou de
traçabilité (le travail « ticket » n'est jamais attribuable).

**B5 — Fenêtre de capture rigide : même projet exact + `created_at >= started_at`.**
Source : code (`pg_brain_session.py:630-633`). Un artefact créé sous `red-lab:architect`
n'est pas capturable par une session `red-lab` (égalité stricte) ; un artefact créé
juste avant `start` (ou dont la session parent a été balayée) est orphelin à vie —
la PK exclusive interdit toute réattribution. **Gravité : moyenne**, aggravée par B1
(les fantômes balayés gardent leurs attributions verrouillées).

**B6 — La discipline focus repose sur l'agent, rien côté serveur.**
Source : ticket `d30cf6e5` (vécu du jour). Deux sessions parallèles sur le snapshot
rev 209 ont fermé proprement (CAS applied 209→210), mais la règle « une ligne ajoutée,
SHA-256 comparé » n'a tenu que par discipline d'agent. Le serveur garantit
l'arithmétique de révision, **pas le contenu** : un `next_focus` qui écrase tout passe
le CAS. **Gravité : haute** — le focus est la seule mémoire inter-sessions.

**B7 — Aucun checkpoint sémantique ; la fraîcheur, c'est le silence.**
Source : ticket `d04dc588`. Entre `start` et `end`, aucune surface pour publier
progrès/blocage/prochaine étape ; la fraîcheur dérive du seul âge du heartbeat.
Audit 2026-08-02 : rien de tel dans les surfaces lifecycle à `8c436aa7` ; le plus
petit lot admissible est documentaire (spec + décision produit + contrat CAS/replay).
**Gravité : moyenne.**

**B8 — Aucun client ne peut déclarer sa session : X-Brain-Session est mort.**
Source : ticket `7ffe0e8a` + `docs/upstream/2026-08-06-claude-otlp-session-join.md`
(jointure impossible). `${CLAUDE_CODE_SESSION_ID}` est expansé de l'env du processus
au chargement de la config MCP → gabarit littéral ou id du parent ;
`normalize_session` → `None` est le cas NOMINAL. À re-mesurer à chaque montée de
version de Claude Code. **Gravité : haute** pour toute refonte fondée sur l'identité
client.
**Et cette mesure est PÉRIMÉE — constat du 2026-08-19.** Le spike porte en tête
« **Version mesurée : Claude Code 2.1.220** » ; `claude --version` rend aujourd'hui
**2.1.234**. Ses deux sources déclarent elles-mêmes la mesure périssable, dans les mêmes
termes : le spike (« Re-mesurer à chaque montée de version de Claude Code plutôt que de
reprendre cette conclusion sur parole ») et le ticket `2bd14b24` (« à re-mesurer à chaque
montée de version de Claude Code »), `7ffe0e8a` reprenant « re-mesurer à chaque montée de
version ». Quatorze versions ont passé sans que personne rejoue le spike, et **aucune
baseline de ce chantier ne mesure l'en-tête de session** : B8 est donc une contrainte
**cotée sur une mesure périmée**, pas une contrainte re-vérifiée. Elle reste l'hypothèse
de travail — le sens du risque est asymétrique, un `X-Brain-Session` redevenu vivant
*ouvrirait* des options au lieu d'en fermer — mais toute décision qui s'appuie sur B8
doit dire qu'elle s'appuie sur 2.1.220.

**B9 — `client_key` est libre et non conventionné.**
Source : ticket `2bd14b24` (`codex-task01-schema-20260727`, `codex-desktop`…).
Impossible de distinguer agent/opérateur, et l'unicité (projet, clé) rend chaque
retry post-terminal générateur d'une clé neuve — prolifération mécanique de lignes.
**Gravité : moyenne.**

**B10 — Projets : le drift fantôme a déjà eu lieu et reste le mode de panne de référence.**
Source : learnings `7bc821a1` (high) et `367e27ae` (tombstone). 2026-06-27→29 :
15 entités sous `brain_v42`/`brain`, invisibles du briefing/recherche scopés hyphen,
**auto-entretenu** (un focus de handoff enseignait littéralement la mauvaise clé).
Corrigé par migration des 15 entités + canonicalisation à la frontière (fix 6e513c9)
+ alias/triggers 033. Le tombstone `367e27ae` archive la note qui enseignait l'erreur.
**Gravité : historique (fermée), mais dimensionnante** : toute refonte du format doit
conserver la propriété « la mauvaise clé est impossible à persister ».

**B11 — Sous-projets : la moitié lourde de certains projets est hors de toute consolidation.**
Source : spec `dbb7c5ce` §2. Voir chiffres en A.1. Fermer la dette = soit ajouter les
six clés au pool (six runs de plus), soit **introduire une sémantique de préfixe qui
n'existe nulle part** (sauf `project_group_scope`). « Les deux sont des chantiers, pas
des réglages. » **Gravité : revue à MOYENNE le 2026-08-19** (l'ADR jumelle l'a
requalifiée) : la première branche du remède est **2/6 exécutée** depuis le 2026-08-10 —
`red-shrik:agent` et `red-lab:architect` sont au pool, soit 447 des 533 artefacts colon.
Reste 86 artefacts sans aucun run (quatre clés `red-lab:*`, pool au plafond de dix) et,
entière, la consolidation **croisée** : un parent au pool ne voit toujours pas ses
enfants (égalité stricte). C'est cette moitié-là qui reste « haute » côté valeur du
corpus ; le nombre 479 de la spec, lui, ne doit plus être cité.

**B12 — Système projets éparpillé, sans doc de bout en bout.**
Source : ticket `d30cf6e5` (constat mesuré) ; recoupé ici : le « système projets »
vit dans 4 briques (format code, 3 tables, convention colon, spec dream) qu'aucun
document ne relie. Ce dossier est la première tentative. **Gravité : moyenne** (coût
de compréhension, risque d'incohérence à chaque évolution).

---

## C. Invariants à préserver coûte que coûte

1. **Le covenant d'explicitation est un choix d'opérateur, pas un défaut technique.**
   `start/list/resume/capture/heartbeat/end/abandon` sont des commandes explicites de
   l'utilisateur ; seule exception serveur, le sweep `auto_stale_7d` (**livré** fermé et
   dry — mais **armé en DRY le 2026-08-18** par décision opérateur, voir A.4 : ne pas
   lire cette parenthèse comme un état courant).
   Toute proposition qui fait fermer/ouvrir une session par un agent, un hook ou un
   client est un **changement de covenant** à trancher par l'opérateur — jamais glissé
   en douce dans une refonte.
   > ⚠️ **AMENDÉ PAR L'OPÉRATEUR LE 2026-08-19 (cadrage, Q12 = piste (a)).** Le covenant
   > devient **à deux régimes**, par *nature* de session déclarée au `start` :
   > **`operator`** — inchangé au caractère près, les sept commandes restent explicites ;
   > **`agent`** — session traçante, **auto-fermée sans rituel**, sans `heartbeat`, dont
   > la vivacité vient de l'observation serveur (`last_observed_at`). Ce n'est pas un
   > glissement : c'est un amendement soumis puis signé, et il ne vaut que pour la nature
   > `agent`.
   >
   > ⚠️ **AMENDEMENT ÉLARGI PAR LA SESSION 2 DU MÊME JOUR (ADR §0bis.1) : le serveur
   > n'auto-FERME plus seulement, il auto-OUVRE.** `start` devient automatique, sur la clé
   > `(projet, connexion)`. Un geste explicite unique et **rétroactif** — le *claim* —
   > promeut une traçante en `operator`. Le sweep `auto_stale_7d` n'est donc plus la seule
   > autre exception serveur : l'ouverture et la fermeture automatiques des traçantes en
   > sont deux de plus, signées par l'opérateur.
   >
   > **Le défaut s'INVERSE et devient `agent`.** Une version antérieure de cet encadré
   > écrivait « défaut `operator`, forcé par C2 », ce qui était juste **tant que `start`
   > restait explicite** ; sous l'ouverture automatique, ce défaut ferait naître une
   > session à rituel par appel d'agent — B1 industrialisé. Le canal de jugement n'est pas
   > perdu pour autant : **le claim EST la déclaration d'intention de l'écrire**. Pas de
   > claim ⇒ personne n'attend de résumé ⇒ l'auto-fermeture ne détruit rien.
   >
   > **Garantie centrale exigée par l'opérateur** : une session `operator` **n'est JAMAIS**
   > fermée par le timeout d'inactivité. Seul le sweep 7 j peut la prendre. Les traçantes
   > le sont à **4 h SIGNÉES le 2026-08-20**, sur leur propre réglage, jamais pendant
   > qu'un appel est en vol (ADR §0bis.3, amendé par §0ter.5). **Seuil d'ÉLIGIBILITÉ au
   > balayage nocturne, jamais un délai de fermeture** — latence réelle pire cas ≈ 28 h.
2. **Fail-closed sur les fins.** XOR ledger/`nothing_to_capture_reason` ; les erreurs
   d'identité, de capture ou d'état laissent la session ouverte.
3. **La garde d'identité (UUID, `expected_client_key`) avant toute mutation** —
   isolation entre sessions parallèles, explicitement PAS une authentification.
4. **CAS non bloquant sur le focus** : un conflit n'empêche jamais une session valide
   de fermer, et fige un snapshot honnête (`conflict`, `focus_at_end`,
   `focus_revision_at_end`). `focus_revision` appartient au projet.
5. **Exclusivité et immuabilité de l'attribution** (PK `knowledge_id`) : la provenance
   déclarée puis persistée ne peut pas être réécrite silencieusement. Si la refonte
   ajoute une réattribution, elle doit être une opération explicite et journalisée.
6. **Idempotence des replays** (start, capture exacte, end persisté, abandon même
   reason) : les retries d'agents sont la norme, pas l'exception.
7. **États non représentables impossibles** : le CHECK terminal 037 + le double rail
   Pydantic. Le downgrade 037→036 est fail-closed (refuse de perdre captures
   ouvertes/abandonnées ou un outcome `conflict`).
   > ⚠️ **AMENDÉ PAR L'OPÉRATEUR LE 2026-08-19 (cadrage, Q15 = route (3)).** La
   > *propriété* est préservée — elle n'est pas assouplie d'un cran. Ce qui change, c'est
   > que la machine gagne un **état terminal de plus**, pour l'auto-fermeture des
   > sessions de nature `agent` : le CHECK actuel interdit `ended` sans `summary` **et**
   > `next_focus` non vides et impose `captured_knowledge_ids = {}` à `abandoned`
   > (`037_session_lifecycle_v4.py:14-91`), donc une fermeture sans rituel n'a
   > aujourd'hui **aucun état légal**. C'est la migration **M-G**, la seule du chantier
   > qui touche le noyau. Elle emporte : le double rail Pydantic déplacé **avec** le
   > CHECK et jamais après, le couloir du pin (C10), la régénération des **deux** assets
   > `ops/recovery/` v4, et un downgrade 037→036 qui doit apprendre le nouvel état sous
   > peine de perdre des sessions terminales en silence. **Son contenu n'est pas
   > spécifié** (ADR §0.4).
8. **Canonicalisation stricte en écriture / tolérante en lecture** du `project_key`,
   alias en base, et **immuabilité de `project_contexts.project_key`** (033). La
   propriété « le drift fantôme ne peut pas se reproduire » ne doit pas régresser.
9. **Le focus ne contient que du jugement non dérivable** (docstring `FocusArg`) :
   l'état mesurable est recalculé au briefing, jamais recopié.
10. **Contrainte migration DURE** : `_REQUIRED_ALEMBIC_HEAD = "045"`
    (`maintenance/plan_index_repair_store.py:63`, gardé par
    `tests/unit/test_plan_index_repair_head_pin.py`) fait fail-closed le plan-index
    repair en prod au moindre head non appliqué (ticket `c60d023d` : ce serait le
    cinquième incident de head figé). **Toute nouvelle head proposée par la refonte
    doit être explicitement séquencée avec le bump de ce pin et son application en
    production** — et une 046 (dimension embedding) est **en projet** sur ce couloir.
    *Correction du 2026-08-18* : « déjà en attente » surestimait — `ls
    alembic/versions/` s'arrête à `045_dream_run_model_width.py`, aucune 046 n'existe,
    et `c60d023d` la qualifie lui-même de « non urgent ». Ce n'est donc pas un gate qui
    précède la refonte, seulement une seconde série à ne jamais faire voler en même
    temps que la première.
    *Second point du même invariant, non vu jusqu'ici* : le pin n'est pas la seule
    garde de tête. `docs/OPERATIONS.md:118` et `tests/unit/test_recovery_contract.py:279`
    portent aussi le numéro en littéral, et l'attestation `ops/recovery/` empreinte le
    schéma de `brain_sessions`, le CHECK de `brain_session_artifacts`, la liste
    **fermée** des triggers de `project_contexts` **et la liste FERMÉE des index de
    `brain_sessions`** (`expected_session_indexes`, `brain-v42-v4.sql:404-412`,
    contrôlée `:665` et `:687`) — soit exactement les **quatre** objets qu'une refonte
    sessions touche. *Complété le 2026-08-19 : la liste d'index était le quatrième, non
    recensé.*
    *Troisième point, du même jour* : l'attestation v4 est **deux fichiers**, pas un.
    `ops/recovery/brain-v42-v4-pgrestore.sql` — la variante pour base restaurée par
    `pg_restore`, celle des preuves isolées du runbook
    (`docs/PLAN_INDEX_REPAIR_RUNBOOK.md:62,122-123`) — porte les mêmes structures et est
    **vivante et testée** : `tests/integration/db/test_recovery_contract_v4_execution.py:106`
    exécute **les deux** assets contre une base réelle, et
    `tests/unit/test_recovery_contract_v4_pgrestore.py:29-33` impose la **parité des
    CTE** entre les deux. Ce dossier, l'ADR et le PLAN ne la nommaient nulle part
    (`grep -c pgrestore` = 0/0/0) : « régénérer `ops/recovery/` » veut dire **les deux
    assets v4**, sans quoi la preuve de restauration n'est régénérée qu'à moitié.
11. **Une session exige un `project_context` existant** (FK RESTRICT) — pas de session
    orpheline de projet.
12. **Le sweep reste UN statement** (réévaluation sous verrou) : c'est la réponse
    gravée au faux-mort ; toute réécriture doit conserver cette propriété.

---

## D. Historique utile

- **032** : `brain_sessions` v3 + `focus_revision` + trigger CAS conditionné
  (`IS DISTINCT FROM`). **033** : registre `projects`/`project_aliases`, triggers de
  normalisation, immuabilité de la clé, backfill graph/outbox. **037** (prod
  2026-07-24, prouvé avant redémarrage MCP + canaries HTTP v4) : lifecycle v4 —
  provenance persistante, outcomes de focus, heartbeat ; upgrade refuse un UUID
  attribué à plusieurs sessions ; captures v3 copiées en `knowledge_type='legacy'`.
  **040** : `focus_updated_at`. Production **re-mesurée à `045` le 2026-08-18** (ne pas
  recopier ; la mesurer).
  **Détail de la 032 à ne pas perdre — l'ADR et le PLAN l'avaient perdu** : ce « trigger
  CAS conditionné » a un nom et un effet précis. `increment_project_focus_revision()` +
  `project_contexts_focus_revision_trigger BEFORE UPDATE OF current_focus ON
  project_contexts FOR EACH ROW`, « `IF NEW.current_focus IS DISTINCT FROM
  OLD.current_focus THEN NEW.focus_revision := OLD.focus_revision + 1` ». Conséquence :
  **aucun écrivain ne peut changer le texte du focus sans bumper la révision**, upsert
  `ON CONFLICT DO UPDATE` compris. Ne pas le confondre avec la **040**, qui date la
  prose (`focus_updated_at`) et qui, elle, est écrite par le code applicatif
  (`db/focus_stamp`), jamais par un trigger.
  **Trois précisions ajoutées le 2026-08-19, parce que l'ADR et le PLAN se sont trompés
  deux fois de suite en les déduisant de mémoire** — les inscrire ici, à la source :
  (1) le trigger est `BEFORE UPDATE` : il ne voit **pas** les INSERT, donc une ligne créée
  avec un focus naît à `focus_revision = 0` (défaut de colonne) sans que rien ne se
  déclenche ; (2) il **assigne**, il n'ajoute pas — un statement qui pose lui-même
  `focus_revision + 1` est écrasé par la même valeur, jamais cumulé en `OLD+2` ;
  (3) **deux écrivains posent la révision explicitement et bumpent donc même sur un texte
  INCHANGÉ**, là où le trigger reste muet : le CAS de `brain_session_end`
  (`pg_brain_session.py:713-714`, exigé par le CHECK 037 : `applied` ⇒
  `focus_revision_at_end = end_expected_focus_revision + 1`) et
  `brain_update_project_focus` (jeton CAS d'un lot blockers-only). Ces deux bumps ne sont
  pas redondants avec le trigger : ils couvrent le cas que le trigger ne couvre pas.
- **2026-06-27→29** : incident projet fantôme underscore (learning `7bc821a1`),
  15 entités migrées, canonicalisation à la frontière (6e513c9), garde CLAUDE.md
  (077cbb7), tombstone `367e27ae`.
- **2026-08-06** : soirée fondatrice des douleurs sessions — purge d'une session
  vivante (faux-mort), découverte des 39 fantômes (faux-vivant), 25 abandons manuels ;
  ouverture des tickets `2bd14b24` et `7ffe0e8a` ; réponse opérateur actée : une
  session sert à la **traçabilité du savoir**, le cycle de vie doit être
  « automatique, pas déclaratif » — sans choix de mécanisme.
- **Spike X-Brain-Session** : le verdict « JOINTURE IMPOSSIBLE » vient du spike du
  2026-08-06, `docs/upstream/2026-08-06-claude-otlp-session-join.md`, **mesuré sur
  Claude Code 2.1.220** — `claude --version` rend **2.1.234** le 2026-08-19, et le spike
  exige d'être rejoué « à chaque montée de version » (voir B8).
  **Correction du 2026-08-18 — attribution fausse** : ce dossier écrivait « ticket
  `2dfbb83d` fermé **négatif** ». Relu (`brain_ticket_get`), `2dfbb83d` n'est pas le
  spike : c'est « Généraliser le panneau live workload … à tous les clients du brain »,
  `status: closed`, fermé le 2026-08-16 par audit de péremption **avec « PREUVE POSITIVE
  DU CONTRAIRE, aux quatre étages »** (commit 88bacd5, `metrics/client_activity.py`,
  `POST /v1/client-activity` → 200). Fermé **LIVRÉ**, donc, pas négatif. L'erreur vient
  du ticket amont `7ffe0e8a` et s'était propagée ici puis dans l'ADR.
- **2026-08-07/10** : design sweep (D1 : le porteur = (projet, acteur), le subagent
  hérite du X-Brain-Agent parent) ; sweep livré fermé/dry — **armé en DRY le 2026-08-18,
  voir A.4** ; 9 abandons manuels le 2026-08-11.
- **2026-08-10** : pool dream multi-projets ouvert **à dix projets, dont DEUX clés
  colon** (`red-shrik:agent`, `red-lab:architect`) — ce que la spec `dbb7c5ce`
  présentait encore comme exclu ; scope serveur par (projet, phase) armé.
- **2026-08-18 (même jour que le ticket d'ancrage)** : REORG repassé WET et sweep armé
  en DRY, tous deux par décision opérateur dans le drop-in `killswitches.conf`.
- **2026-08-18** : ticket d'ancrage `d30cf6e5` ; vécu : lot de capture mélangé
  fail-closed sur les tickets ; CAS 209→210 propre entre deux sessions parallèles,
  discipline de contenu côté agent seulement ; annonce opérateur « pas mal de choses
  qui ne me plaisent pas », sans détail.

---

## E. Trous de connaissance — à trancher par l'opérateur seul

> **Sessions de cadrage du 2026-08-19 : E1, E2, E4, E7 et E10 sont tranchés** (voir leurs encadrés).
> Une note antérieure disait ici que E2 (subagents) « devient plus tranchant sous la
> réponse (a) » : la session 2 l'a **réglé**, et par la mesure — aucun en-tête ne
> distingue un subagent de son porteur, donc héritage. Restent entiers : **E3** (ordre
> d'armement de l'automatisation), **E5** (sous-projets), **E6** (renommage/fusion),
> **E8** (garde de contenu du focus) et **E9** (réattribution). **La source unique des
> réponses est l'ADR §0 et §0bis.**

1. > ✅ **TRANCHÉE LE 2026-08-19 : piste (a).** Deux natures de session — agent
   > traçante auto-fermée sans rituel, opérateur avec rituel. Le rituel de fin
   > (`summary` + `next_focus`) **survit pour la nature `operator`** et reste le seul
   > moment où du jugement non dérivable s'écrit ; la nature `agent` n'en a pas. Détail,
   > exigences et coûts : **ADR §0.1, D11 et §0.4**. Cette réponse ouvre une question
   > neuve que ce dossier ne posait pas — l'état terminal d'une session sans rituel
   > (Q15, tranchée le même jour : nouvel état, migration M-G).
   >
   > *Texte d'origine :*

   **La question non tranchée du ticket `2bd14b24`, posée à lui et laissée ouverte** :
   si la fermeture devient automatique, que devient le rituel de fin (summary +
   next_focus, seul moment où du jugement non dérivable est écrit) ? Trois pistes
   présentées, aucune choisie : (a) deux natures de session — agent traçante
   auto-fermée sans rituel, opérateur avec rituel ; (b) plus de rituel du tout,
   jugement migré vers un objet dédié ; (c) fin auto avec résumé dérivé par le serveur
   (réserve : produit de l'état mesurable recopié, ce que la doctrine interdit).
2. > ✅ **RÉGLÉE LE 2026-08-19 (session 2) PAR LA MESURE, pas par arbitrage :
   > HÉRITAGE.** Les subagents se rattachent à la session de leur porteur, sans aucun tag,
   > parce qu'ils **partagent sa connexion**. Ce n'est plus une préférence de design mais
   > une contrainte mesurée : **aucun des trois en-têtes ne distingue un subagent de son
   > porteur** — `X-Brain-Agent` porte le PROJET (`${PWD}` réduit au basename, ou le
   > littéral `brain-v42` du `.mcp.json`), `X-Brain-Session` est mort (B8),
   > `Mcp-Session-Id` porte la CONNEXION — et aucun ne le fera : la configuration des
   > en-têtes est **par serveur MCP**, pas par subagent. Tagger exigerait une capacité
   > amont inexistante, même classe que B8. Le design sweep (D1) avait donc raison, et on
   > sait maintenant **pourquoi**. Détail : ADR §0bis.2.
   >
   > *Texte d'origine :*

   **Un subagent mérite-t-il une session propre, ou se rattache-t-il à celle de son
   parent ?** Le ticket `2bd14b24` dit que ce choix « conditionne tout le reste ».
   Le design sweep (D1) penche pour « l'activité du subagent EST celle de
   l'opérateur », mais rien n'est acté côté sessions.
3. **Armer ou non l'automatisation existante** — *question re-posée sur l'état mesuré,
   2026-08-19.* Une version antérieure de cette ligne écrivait « sweep 7 j (livré,
   jamais armé) » : c'est l'état périmé que la section A.4 corrige déjà, et le laisser
   ici était le pire endroit possible, puisque c'est **cette ligne** qui demande à
   l'opérateur de décider. **Le sweep est armé en DRY depuis le 2026-08-18** (drop-in
   `killswitches.conf` : `BRAIN_DREAM_SWEEP_ENABLED=true`,
   `BRAIN_DREAM_SWEEP_DRY_RUN=true`) ; seul le **WET** reste à décider. Restent donc :
   le passage du sweep en WET, l'auto-heartbeat et l'auto-capture (`7ffe0e8a`, spec
   prête, changement de covenant explicite). Dans quel ordre, et avec quelles limites
   (le ticket liste : N sessions ouvertes sans porteur — refuser ? dernière active ? ;
   stdio sans headers — dégrader sans attribuer) ?
4. > ✅ **TRANCHÉE LE 2026-08-19 (session 2) : OUI, capturables, sur `from_project`.**
   > La paternité, pas la destination : la capture répond à « qu'a PRODUIT cette
   > session », et la session qui écrit le ticket est dans `from_project` — analogie exacte
   > des six tables existantes. Mesuré ce jour : **231 tickets, 187 self**
   > (`from_project = to_project`, où la question n'a pas d'objet) **et 44 cross-projet**,
   > où elle tranche. **Le lot mélangé reste all-or-nothing** — point non contesté.
   > Conséquence obligée : Q14 = voie (a), `knowledge_sources` de l'attestation de
   > récupération s'élargit aux tickets, sur les **deux** assets v4.
   >
   > *Texte d'origine :*

   **Les tickets doivent-ils devenir capturables** (7e type du ledger), ou leur
   exclusion est-elle un choix de conception à documenter ? Et un lot mélangé
   doit-il rester all-or-nothing ?
5. **Sous-projets** — *question re-posée sur l'état mesuré, 2026-08-19.* Elle demandait
   « fermer la dette des **479** artefacts autrement ? Qui consolide `red-shrik:agent` ? »
   sur les chiffres du 2026-08-08, alors que la section A.1 les a re-mesurés : **533**
   artefacts colon, dont **447 (84 %)** sur deux clés qui sont **au pool dream depuis le
   2026-08-10** (`red-shrik:agent`, `red-lab:architect`) — le remède « ajouter les six
   clés » est **2/6 exécuté**, et « qui consolide `red-shrik:agent` ? » a déjà sa réponse
   pour la moitié nocturne. Ce qui reste à trancher : (a) introduire une vraie sémantique
   parent/enfant (préfixe ou lien en base) ou généraliser `project_group` — c'est la
   consolidation **croisée**, que le pool ne donne pas (égalité stricte : la nuit de
   `red-lab` ne voit pas `red-lab:architect`) ; (b) entrer les **86** artefacts des
   quatre clés `red-lab:*` restées hors pool, sachant que le pool est **au plafond de
   dix** ; (c) assumer la platitude.
6. **Renommage/fusion de projets** : la clé est immuable depuis 033 et « renommer
   exige une migration explicite ». La refonte doit-elle livrer une opération de
   renommage/fusion outillée (avec alias), ou l'immuabilité est-elle un invariant
   voulu ?
7. > ✅ **APPROUVÉE LE 2026-08-19 (session 2), après dissolution de sa moitié piégée.**
   > La doctrine de fraîcheur de `d04dc588` est reprise. Ce qui a changé : le checkpoint
   > **cesse d'être un mécanisme de vivacité** pour devenir un **objet de jugement pur** —
   > sous l'ouverture automatique, la vivacité d'une session agent vient de
   > `last_observed_at`, et une session `operator` n'est jamais fermée par inactivité.
   > Le checkpoint repasse donc du bon côté de la grille FAIT/JUGEMENT, et son unique
   > métier redevient B7. **Forme retenue, mixte** : stockage append-only
   > `UNIQUE(session_id, seq)` (replay idempotent, C6) mais payload **du ticket** —
   > `progress` + `blocker|null` + `next_step` en UN appel. ADR §0bis.4.
   >
   > *Texte d'origine :*

   **Fraîcheur et checkpoint** (`d04dc588`) : décision produit explicite exigée —
   fraîcheur dérivée du seul âge du checkpoint, dérive du focus exposée séparément et
   jamais cause de péremption. Le MVP est spécifié mais gated sur approbation.
8. **Discipline du focus côté serveur** (B6) : faut-il un garde serveur (append-only,
   diff borné, ou objet focus structuré), au prix d'une rigidité nouvelle sur le seul
   canal de jugement libre ?
9. **Réattribution** : une session balayée emporte ses attributions (exclusivité PK).
   Droit de réattribution explicite, ou l'orphelinage est-il le prix de la preuve ?
10. > ✅ **SOUMISE ET RÉPONDUE LE 2026-08-19.** La liste B **couvre** : aucun irritant
    > supplémentaire, donc pas de B15. La **priorisation, elle, est rejetée** — l'ordre
    > ne vient plus de la gravité dérivée mais de l'axe **« traçabilité du savoir »**,
    > B3, B4, B5 en tête. Conséquence sur le PLAN : reséquencement
    > **P0 → P1 → P2 → P4.4 → P3** et promotion de Q6 en critère de sortie de Phase 0
    > (ADR §0.3). *Réserve de méthode qui reste vraie :* une liste dérivée des tickets
    > capture ce qui a été **incidenté**, pas ce qui frotte au quotidien ; l'absence de
    > B15 est une réponse, pas une preuve d'exhaustivité.
    >
    > *Texte d'origine :*

    **Quelles sont les douleurs de l'opérateur ?** « Pas mal de choses qui ne me
    plaisent pas » n'est pas détaillé. Ce dossier dérive les douleurs des preuves
    disponibles ; la liste B doit lui être soumise pour confirmation, priorisation,
    et complétion — il peut y avoir des irritants non ticketés que rien ici ne capture.

---

*Sources primaires : tickets `d30cf6e5`, `2bd14b24`, `d04dc588`, `7ffe0e8a`,
`c60d023d` ; plan spec `dbb7c5ce` ; learnings `7bc821a1`, `367e27ae` ; code aux
chemins cités (état du dépôt au 2026-08-18) ; `docs/SCHEMA.md` ; migrations
032/033/036/037/040.*

*Passe de vérification du 2026-08-19 (lecture seule, aucune écriture DB) : head `045` ;
sept triggers utilisateur sur `project_contexts`, dont
`project_contexts_focus_revision_trigger` (`pg_get_triggerdef` + `prosrc` relus) ;
`10/59` contextes à `current_focus IS NULL`, **tous** à `focus_revision = 0` et
`focus_updated_at IS NULL` ; `access_log` à 0 ligne ; sept vues `public` contenant
`split_part` ; masse colon `red-shrik:agent` 312 / `red-lab:architect` 135 /
`red-lab:orchestrator` 64 / `:reviewer` 15 / `:sentinel` 5 / `:developer` 2 = 533, contre
`red-shrik` 245 et `red-lab` 184 ; drop-in `killswitches.conf` (mtime 2026-08-18 20:52) :
sweep `ENABLED=true`/`DRY_RUN=true`, pool à dix dont deux clés colon. Code relu :
`pg_project_context.py:51-71,202-213,273-281,295-305` ; `pg_brain_session.py:713-714` ;
`roadmap_service.py:191-196` ; `db/focus_stamp.py` ; `ops/recovery/brain-v42-v4.sql`
(`:533-556` liste fermée de treize triggers, `:913-918` `tgenabled='O'`, `:1083-1113`
`knowledge_sources` à six tables, **`:404-412` liste fermée de trois index contrôlée
`:665`/`:687`**) **et `ops/recovery/brain-v42-v4-pgrestore.sql`, qui porte les mêmes
structures** (`tests/integration/db/test_recovery_contract_v4_execution.py:106` exécute
les deux ; `tests/unit/test_recovery_contract_v4_pgrestore.py:29-33` impose la parité des
CTE). Trois affirmations de ce dossier ont été re-posées
sur cet état : E.3 (sweep), E.5 et B11 (sous-projets), plus le détail 032 en D.*

*Passe de pliage des résidus, 2026-08-19 (lecture seule, aucune écriture DB, aucun
commit). Mesures du jour, **datées et périssables** :*
- *`select version_num from alembic_version` → **045**.*
- *`brain_sessions` : **29 `open`**, dont **21** à `last_heartbeat_at < now() -
  interval '7 days'` et **24** au-delà de 24 h, sur **467** lignes — le « 18 fantômes
  balayables » du drop-in datait du 2026-08-18 et était repris sans caveat par l'ADR
  §1.1 et le PLAN §0.*
- *Index de `brain_sessions` : exactement **trois** (`pg_indexes`), aucun sur un acteur
  (A.2).*
- *Sessions `open` par projet, ≥2 : `auto-discord` 8, `brain-v42` 4, `red-arena` 4,
  `datalake-v1`/`red-gift`/`claude-dev-pc`/`red-lab` 2 — **24 des 29 `open`** dans un
  projet qui en porte au moins deux. C'est le **plafond** de la population « ambiguë »
  au sens de la règle « exactement un » : il compte par projet, pas par couple
  (projet, acteur), et ne pourra être raffiné qu'une fois `started_by_actor` livrée. Le
  ticket `7ffe0e8a` portait déjà la même mesure partielle au 2026-08-16 (« Simultanées :
  `auto-discord` 6, `red-arena` 3, `claude-dev-pc`/`red-lab` 2 ») sans qu'aucun des trois
  documents la cite.*
- *`claude --version` → **2.1.234**, quand le spike `docs/upstream/2026-08-06-claude-otlp-session-join.md`
  déclare « Version mesurée : Claude Code 2.1.220 » et exige d'être rejoué à chaque
  montée de version (voir B8).*
- *`grep -c pgrestore` sur les trois documents de ce dossier → **0/0/0** avant cette
  passe, alors que `ops/recovery/brain-v42-v4-pgrestore.sql` existe, est exécutée contre
  une base réelle et est tenue en parité de CTE avec la variante live (invariant 10).*

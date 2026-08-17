# Design — Tickets cross-projet (coordination inter-sessions)

**Date** : 2026-07-04
**Statut** : validé (brainstorming avec l'utilisateur, options tranchées)
**Contexte** : les sessions Claude Code de projets différents (ex. red-shrik → red-data)
ont besoin de se passer des demandes ("modifie X pour moi") et des heads-up
("j'ai changé le contrat Y"). Aujourd'hui c'est détourné via `brain_learn` :
pas de destinataire, pas d'état, pas de fil de réponse, et ça pollue la
mémoire durable avec du transient.

## 1. Concept : une 2ème famille, pas un 7ème type de connaissance

Le brain a une famille **mémoire** (decision, learning, snippet, runbook, adr,
plan) : embeddings, search, decay, domaines, graph. Les tickets sont une
famille **coordination**, orthogonale : **adressés, transients, à état**.

Conséquences dures (non négociables dans l'implémentation) :
- Les tickets sont **hors** `brain_search`, hors embeddings, hors decay,
  hors classification domaines, hors sync Neo4j.
- Le seul pont vers la mémoire est l'extraction (§6) : c'est le learning
  extrait qui est cherchable, jamais le ticket.

## 2. Modèle de données — 2 tables PG (migration Alembic)

### `tickets`

| Champ | Type | Note |
|---|---|---|
| `id` | UUID PK | |
| `kind` | `request` \| `fyi` | détermine le cycle de vie |
| `title` | str ≤ 200 | |
| `body` | text | la demande initiale |
| `from_project` | project_key canonique | émetteur |
| `to_project` | project_key canonique | destinataire |
| `status` | enum, cf. §3 | |
| `extraction_status` | `null` \| `pending` \| `proposed` \| `skipped` \| `done` | piloté par le job dream ; `pending` posé à l'entrée en état terminal |
| `created_at` / `updated_at` | timestamps | |
| `resolved_at` / `closed_at` | timestamps nullable | |

**Validation projets** : `from_project` et `to_project` sont canonicalisés
(mixin existant `ProjectKeyCanonicalMixin`) **et validés contre le registre
des projets existants** — refus si projet inconnu. Leçon du drift
`brain_v42`/`brain-v42` : aucune création de projet fantôme par typo.

### `ticket_messages`

| Champ | Type | Note |
|---|---|---|
| `id` | UUID PK | |
| `ticket_id` | FK → tickets, ON DELETE CASCADE | |
| `author_project` | project_key canonique | quel côté a écrit |
| `body` | text | |
| `status_to` | enum nullable | si le message accompagne une transition — le fil se raconte tout seul |
| `created_at` | timestamp | |

**Index** : `(to_project, status)`, `(from_project, status)` sur `tickets` ;
`(ticket_id, created_at)` sur `ticket_messages`.

## 3. Machine à états

```
request : open ──start──▶ in_progress ──resolve/wontfix──▶ resolved | wontfix ──confirm──▶ closed
          (resolve/wontfix légaux aussi depuis open)             └───reopen (demandeur)───▶ open
fyi     : open ──ack──▶ acked
```

| Acteur | Actions autorisées |
|---|---|
| Exécutant (`to_project`) | `start` (→ in_progress), `resolve`, `wontfix`, `ack` (fyi) |
| Demandeur (`from_project`) | `confirm` (→ closed), `reopen`, `cancel` (→ closed, à tout moment) |

- La boucle est complète : `resolved` **et** `wontfix` attendent la
  confirmation du demandeur (il les voit dans son briefing ; il peut
  contester un wontfix via reply + reopen).
- Les messages sont postables à tout moment quel que soit l'état — le
  statut contraint les *transitions*, pas la discussion.
- États terminaux : `closed`, `acked` → posent `extraction_status='pending'`.

## 4. Surface MCP — 5 tools

| Tool | Rôle |
|---|---|
| `brain_ticket_create(from_project, to_project, kind, title, body)` | ouvrir |
| `brain_ticket_reply(ticket_id, author_project, body)` | poster dans le fil |
| `brain_ticket_transition(ticket_id, author_project, action, message?)` | `start` / `resolve` / `wontfix` / `confirm` / `reopen` / `ack` / `cancel` — **valide qui a le droit** selon `author_project` vs from/to, et la légalité de la transition selon l'état courant et le kind |
| `brain_ticket_list(project_key)` | groupé par action : `a_traiter` (je suis to_project, open/in_progress) / `a_confirmer` (je suis from_project, resolved/wontfix) / `en_attente` (l'autre doit agir) |
| `brain_ticket_get(ticket_id)` | ticket + fil complet |

Un seul tool de transition polymorphe plutôt que 7 micro-tools — la surface
brain est déjà à ~30 tools.

## 5. Intégration briefing (`brain_session_start`)

Nouvelle section haute (c'est de l'actionnable), même graceful-degrade que
les autres sections (spec §9 du briefing) :

```
### Tickets (2 à traiter · 1 à confirmer)
⬅️ #a1b2 [request] de red-shrik : « exposer /api/signals en ndjson » (open, 2j)
⬅️ #c3d4 [fyi] de red-data : « format réponse changé » — à ack
➡️ #e5f6 vers red-data : résolu — vérifie et confirme
```

Cap à ~5 entrées + renvoi vers `brain_ticket_list`.

## 6. Job dream `ticket_extract` — proposer-only

- Nouveau job nocturne + **killswitch dédié `EXTRACT`**, démarre en **dry**,
  même trajectoire de soak que REORG.
- Scanne les tickets `extraction_status='pending'`, envoie le fil complet au
  LLM (NVIDIA API, deepseek JSON strict **sans tools** — pattern validé du
  domain backfill), qui propose **0..n** entités : learning ou decision, avec
  `target_project` choisi parmi {from_project, to_project} + rationale.
  0 proposition → `skipped`.
- Avant toute persistance, une gate de déduplication calcule le texte
  d'embedding canonique de chaque draft et cherche, dans le même projet, le
  meilleur match parmi les learnings et décisions actifs, puis parmi les drafts
  déjà retenus dans le run. Un cosine `>= 0,85` supprime la draft, même entre
  types ; les entités archivées, fusionnées ou supersédées ne bloquent pas une
  nouvelle extraction.
- La recherche corpus reste exacte et project-scoped pendant le soak : une
  requête ANN pourrait rater un doublon. Son coût doit être mesuré avant WET.
- La gate est **fail-closed** : ligne active avec embedding absent ou non
  comparable (norme `<= 1e-6`), nouveau vecteur invalide/indisponible ou
  lecture du corpus impossible → aucune proposal du run n'est persistée, aucun
  WET n'est appliqué, les tickets restent `pending` et la phase Dream est
  `fail`.
- La persistance verrouille et revalide le ticket `pending`. Deux runners ayant
  scanné le même ticket ne peuvent donc pas créer deux lots de proposals.
- Propositions stockées en table `ticket_extraction_proposals`
  (pattern PROMOTE) : review humaine → apply.
- En wet (post-soak) : création directe avec `source_type="automated"`,
  `source="ticket:<id>"`, confidence **fixée à `medium`** (leçon `6dfb9064` :
  la calibration de confidence LLM est plate, on ne la propage pas).
- Le learning/decision extrait entre dans la mémoire normale (embeddings,
  graph, search) via les services existants.
- Le killswitch reste en DRY après livraison de la gate. Le passage WET exige
  encore quelques nuits de soak, la calibration du seuil à `0,85`, une mesure
  humaine du taux de doublons et un contrôle du coût de la recherche exacte.
- `--apply-ids` reste un override opérateur pour des proposals déjà relues et
  ne rejoue pas la gate automatique.

### 6.1 Opt-out extraction — `extraction='skipped'`

Certains tickets ne doivent pas alimenter la mémoire durable : tickets
opérationnels haute-fréquence (ex. daemon `red-lab-factory`), jobs
automatisés, signaux transitoires — du bruit, pas du savoir.

- **À la création** : `brain_ticket_create(..., extraction='skipped')` pose
  `extraction_status='skipped'` immédiatement ; aucune valeur n'est générée à
  la transition terminale.
- **Transition terminale** : le side-effect qui pose `extraction_status='pending'`
  à l'entrée dans `closed` / `acked` est inhibé si `extraction_status` vaut
  déjà `'skipped'` — le flag est **préservé**, pas écrasé.
- **Job extract** : `fetch_pending_threads` filtre `WHERE extraction_status='pending'` ;
  les tickets `'skipped'` sont invisibles au scan LLM nocturne, de bout en bout.
- **Pas de migration** : la valeur `'skipped'` est autorisée par le CHECK de la
  migration 028 depuis l'origine.

## 7. Non-goals v1 (YAGNI explicite)

Pas de : priorités, broadcast multi-destinataires (fan-out = créer N
tickets), purge des tickets terminaux, intégration Watchk, sync Neo4j des
tickets, inclusion dans `brain_search`, notion d'utilisateur/assignee
(l'« identité » est le project_key, mono-utilisateur).

## 8. Tests (TDD obligatoire)

- **Unit** : matrice complète des transitions (qui × action × état × kind —
  légales et illégales), validation project registry, tools MCP (5),
  section briefing (rendu + cap + degrade), job extract avec LLM mocké
  (0 proposition, n propositions, JSON invalide).
- **Integration** : round-trip PG complet create → reply → resolve →
  confirm → extraction_status.
- Coverage ≥ 60 % (gate CI existant).

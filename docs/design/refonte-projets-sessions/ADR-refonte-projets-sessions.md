# ADR — Refonte PROJETS + SESSIONS : accrétion instrumentée
## « Le serveur observe et prépare ; l'opérateur signe »

- **Date** : 2026-08-18
- **Statut** : **PROPOSED** — ce statut ne passera jamais à *accepted* sans le cadrage
  explicite de l'opérateur. Le ticket d'ancrage `d30cf6e5` dit : « NE PAS DÉMARRER sans
  cadrage explicite de l'opérateur ». **Rien dans ce document n'autorise à démarrer quoi
  que ce soit.** Aucune ligne de code, aucune migration n'existe.
- **Périmètre** : systèmes PROJETS et SESSIONS de brain-v42 (serveur MCP « second
  cerveau », Python 3.12, FastMCP 3, SQLAlchemy 2 async, PostgreSQL 16 + pgvector,
  head Alembic production mesurée à 045 le 2026-08-16 — à re-mesurer, jamais recopier).
- **Genèse** : synthèse de trois propositions d'architectes (A « refonte assumée »,
  B « évolution incrémentale », C « opérateur d'abord ») jugées par un panel de trois
  lentilles (technique, opérateur, simplicité). Vote : B majoritaire (2/3). Cette ADR
  part de B, corrige chaque faiblesse relevée par les juges sur B, et greffe les idées
  de A et C que le panel a explicitement retenues. Rien ici n'est neuf sans preuve
  (ticket, learning, code vérifié) ou juge derrière. **Limite de provenance assumée** :
  les trois propositions et les verdicts du panel ne sont pas archivés dans le dépôt —
  seuls ADR, DOSSIER et PLAN existent. Les scores et attributions « exigé par le
  panel » sont donc invérifiables pour le lecteur : les traiter comme du contexte de
  rédaction, jamais comme des preuves. Les preuves opposables restent les tickets,
  learnings et vérifications code cités, chacun re-vérifiable indépendamment.
- **Document jumeau** : `docs/design/refonte-projets-sessions/PLAN-phase-0-4.md`
  (plan phasé détaillé). Chacun des deux se lit seul.
- **Cadrage opérateur — DEUX sessions le 2026-08-19.** La seconde (**§0bis**) raffine
  Q12 sur l'ouverture, règle Q9 par la mesure, dissout le corollaire de Q1 et tranche les
  quatre dernières bloquantes : **Q2, Q3, Q6, Q14**. **LA PHASE 0 EST DÉBLOQUÉE.**
  Restent ouvertes, sans bloquer : Q4, Q5, Q7, Q8, Q11, Q13. Lire le §0bis **avant** le
  §0 : il en corrige une dérivation — le défaut de nature s'inverse sous l'ouverture
  automatique.
- **Cadrage opérateur — première session, 2026-08-19** : **cinq réponses acquises**
  (Q10 et son axe, Q12, Q1 dérivée, Q15 neuve, séquencement). Elles sont consignées au
  **§0** ci-dessous, qui en est la source unique, et propagées dans le corps du
  document. Le statut reste **PROPOSED** : Q2, Q3, **Q6** et Q14 manquent encore pour
  sortir la Phase 0. Deux réponses touchent ce que l'accrétion promettait d'épargner —
  le covenant (Q12) et la machine d'états 037 (Q15) : le §1.3 a été corrigé en
  conséquence.

---

## 0. Cadrage opérateur — session du 2026-08-19

Première session de cadrage. Ce qui n'est pas listé ici reste ouvert : une question
absente de ce tableau n'a **pas** été tranchée, quel que soit ce qu'en dit le corps du
document.

| # | Question | Réponse opérateur | Ce qu'elle emporte |
|---|---|---|---|
| **Q10** | La liste B1–B14 couvre-t-elle « pas mal de choses qui ne me plaisent pas » ? | **Elle couvre — aucun irritant à ajouter.** La **priorité**, elle, est à revoir | Pas de B15. La cotation par gravité dérivée cesse de commander l'ordre |
| **Q10 bis** | Quel axe commande l'ordre, alors ? | **La traçabilité du savoir** — B3, B4, B5 en tête | Reséquencement des phases (§0.3) |
| **Q12** | Cycle de vie automatique : piste (a), (b) ou (c) ? | **(a) — deux natures de session** : agent traçante auto-fermée sans rituel, opérateur avec rituel | Amende le covenant (C1) pour la nature agent ; fait tomber Q1 ; ouvre Q15 |
| **Q1** | `last_observed_at` : observation acceptable ou auto-heartbeat déguisé ? | **Dérivée de Q12, plus une question libre** (§0.2) | D5 devient portant. Son **corollaire** (« exactement un » vs « toutes ») reste ouvert |
| **Q15** | *(neuve — posée par aucun des trois documents)* Comment une session agent atteint-elle un état terminal ? | **(3) — nouvel état terminal dans le CHECK 037** | Migration **M-G** sur le noyau ; amende C7 ; invalide le constat du §1.3 |
| **Séq.** | P4.4 avant P3 ? Q6 promue en Phase 0 ? | **Oui aux deux** | §0.3 |

**Restent ouvertes et bloquantes** : Q2, Q3, **Q6**, Q14 (sortie de Phase 0) ; puis Q4,
Q5, Q7, Q8, Q9, Q11, Q13 et le corollaire de Q1.

### 0.1 Ce que (a) exige, et que le dossier n'avait pas instruit

**La nature est DÉCLARÉE, pas détectée — B8 ne bloque donc pas (a).** Le serveur ne sait
pas distinguer un appel d'agent d'un appel d'humain : D4 l'écrit noir sur blanc,
`X-Brain-Session` est mort et `X-Brain-Agent` est un projet déclaré par le client. Il
n'a pas à le faire. La nature devient un paramètre de `start` et une **quatrième colonne
nullable** de D1, sur la doctrine exacte des trois autres (`NULL` = « ouverte avant
M-A », aucun backfill). C'est le régime déjà en vigueur ailleurs : la provenance du
ledger est « déclarée par le client, pas prouvée cryptographiquement », et la garde
d'identité « n'authentifie pas le client » (C3). (a) ne demande donc pas de résoudre B8
d'abord — elle demande d'assumer une déclaration de plus.

**Le défaut est forcé par C2 ; il n'est pas affaire de goût.** Les deux erreurs de
déclaration ne coûtent pas le même prix :

- un agent qui se déclare `operator` ouvre une session à rituel qui ne se referme
  jamais : c'est **B1 inchangé**, et le sweep sait déjà traiter ce cas ;
- une session d'opérateur qui part en `agent` s'auto-ferme sans rituel : `summary` et
  `next_focus` ne sont **jamais écrits**, et le seul canal de jugement non dérivable est
  perdu **sans trace**.

Le premier est récupérable, le second non. **Défaut = `operator` ; `agent` doit être
déclaré explicitement.** Une implémentation qui inverse ce défaut viole C2 (fail-closed
sur les fins), et le viole silencieusement — c'est la pire forme.

### 0.2 Q1 tombe, et devient une réponse différente par nature

Une session de nature agent n'a pas de rituel, donc pas de `brain_session_heartbeat` —
qui est une commande explicite. Son **seul** signal de vivacité devient
`last_observed_at` (D5). « Observation acceptable ou auto-heartbeat déguisé ? » n'a donc
plus d'objet pour cette nature : c'est le signal de vivacité **par construction**, et ce
n'est pas un covenant glissé, puisque la réponse Q12 vient de l'amender explicitement
pour elle. Pour la nature opérateur, rien ne bouge : `last_heartbeat_at` reste la seule
trace de la commande explicite, et D5 reste de l'observation pure.

Deux conséquences. **(i)** D5 cesse d'être un confort : il devient portant pour la
nature agent, et `BRAIN_SESSION_OBSERVED_ACTIVITY_ENABLED` cesse d'être un killswitch de
prudence pour devenir la condition de fonctionnement d'une moitié du modèle. **(ii)** Le
**corollaire** de Q1 ne tombe pas et reste entier : « exactement un » ou « toutes »
quand plusieurs sessions matchent (acteur, projet) — sur une population mesurée le
2026-08-19 de **24 sessions `open` sur 29** logées dans un projet qui en porte au moins
deux.

### 0.3 Reséquencement — l'axe « traçabilité du savoir » retourne le plan

Le PLAN était ordonné par gravité dérivée. Sous l'axe choisi, il est à l'envers :

- **B3** (18 % de capture, 34 % d'attribution) devient la douleur n° 1 — et elle était
  fermée par D8 / **Phase 4.4**, l'avant-dernière tranche, derrière Q6 (amendement de
  covenant) *plus* un soak de 14 jours ;
- **B6** (discipline du focus) occupait **toute la Phase 3** et sa migration M-D, alors
  qu'il descend dans le classement.

**Nouvel ordre : P0 → P1 → P2 → P4.4 → P3**, puis les armements restants (4.1, 4.2-3,
4.5, 4.6) dans l'ordre du PLAN §6.

P1 reste en tête et ce n'est pas négociable : l'écrivain de 4.4 dépend durement du
résolveur de projet par tool **et** de `started_by_actor`, tous deux nés en Phase 1
(PLAN §6.4.4 et §3.6). Remonter 4.4 avant P1 la priverait de son `project_key`.

**Q6 est promue critère de sortie de Phase 0** : elle gate désormais la tranche qui suit
immédiatement la Phase 2, non plus une tranche de fin de plan. Les critères de sortie de
la Phase 0 deviennent **Q2, Q3, Q6, Q10 et Q14**.

Bénéfice secondaire, non recherché mais réel : repousser la Phase 3 repousse la fenêtre
pendant laquelle l'attestation de récupération est rouge — M-D crée son constraint
trigger **désactivé**, et `brain-v42-v4.sql:913-918` exige `tgenabled = 'O'` de chaque
trigger attendu.

### 0.4 Q15 — l'état terminal des sessions agent (question neuve)

Aucun des trois documents ne posait cette question, parce qu'aucun n'avait relu le CHECK
terminal en pensant à une fermeture **sans rituel**.
`brain_sessions_terminal_state_valid` (`alembic/versions/037_session_lifecycle_v4.py:14-91`,
relu le 2026-08-19) impose :

```
status = 'ended'      →  summary IS NOT NULL AND btrim(summary) <> ''
                         AND next_focus IS NOT NULL AND btrim(next_focus) <> ''
                         AND focus_outcome IS NOT NULL
status = 'abandoned'  →  summary IS NULL AND next_focus IS NULL
                         AND cardinality(captured_knowledge_ids) = 0
                         AND abandonment_reason IS NOT NULL
```

**Une session ne peut pas atteindre `ended` sans un résumé ET un `next_focus` non vides,
et c'est garanti en base, pas par convention.** Une session agent auto-fermée sans
rituel n'a donc **aucun état terminal disponible** dans la machine 037. Trois routes ont
été soumises :

| Route | Ce qu'elle coûte |
|---|---|
| (1) terminer en `abandoned` | Zéro migration. Mais le CHECK force `captured_knowledge_ids = {}` : le ledger réel survit (`brain_session_artifacts`, PK `knowledge_id`, FK CASCADE), tandis que **l'instantané terminal de chaque session agent réussie déclarerait zéro capture** — sur le chemin de capture principal, et sous l'axe que l'opérateur vient précisément de choisir. Et `abandoned` signifierait trois choses à la fois : fantôme balayé, opérateur qui renonce, agent qui a réussi |
| (2) résumé synthétisé par le serveur | Zéro migration aussi. Mais c'est **la piste (c) par la porte de derrière**, avec l'objection déjà versée au dossier : de l'état mesurable recopié dans le seul canal de jugement non dérivable (C9, doctrine `FocusArg`) |
| **(3) nouvel état terminal — RETENUE** | Migration **M-G** sur la machine d'états elle-même. Amende C7 |

**Réponse opérateur : (3).** Ce qu'elle emporte, et qui **reste à écrire** :

- une **migration M-G** étendant `brain_sessions_terminal_state_valid` d'une branche
  terminale, avec son downgrade fail-closed. Le **nom** de l'état, sa branche exacte, et
  le sort de `captured_knowledge_ids`, `abandonment_reason` et `focus_outcome` dans
  cette branche **ne sont pas spécifiés ici** ;
- le **double rail** : la branche Pydantic bouge avec le CHECK, jamais après (C7) ;
- le **couloir du pin** (C10, ticket `c60d023d`) : M-G est une tête, séquencée avec le
  bump de `_REQUIRED_ALEMBIC_HEAD` et son test **dans le même commit**. *Amendé le
  2026-08-20 (§0ter.1) : cette tête est COMMUNE avec M-A — une seule, pas deux ;*
- la **régénération des deux assets** `ops/recovery/` v4 — l'empreinte du CHECK terminal
  de `brain_sessions` en fait partie ;
- le **downgrade 037→036**, déjà fail-closed, doit apprendre le nouvel état ;
- une question ouverte que (3) ne referme pas : une session agent terminée dans ce
  nouvel état applique-t-elle un `next_focus` ? Si non, elle ne fait pas de CAS, et
  **c'est une bonne nouvelle** — sinon chaque auto-fermeture bumperait `focus_revision`
  et produirait des `focus_outcome = 'conflict'` systématiques sur les sessions
  opérateur concurrentes.

**M-G touche le noyau que l'accrétion était construite pour ne pas toucher.** C'est le
prix déclaré du couple (a) + (3), assumé par l'opérateur, et il invalide un constat du
§1.3 — corrigé sur place.


---

## 0bis. Cadrage opérateur — session 2 du 2026-08-19

Seconde session, le même jour. Elle **raffine Q12 = (a)** sur un point que la première
n'avait pas explicité — l'ouverture — et **tranche les quatre dernières bloquantes de la
Phase 0**. Mesures du jour, lecture seule : head `045` ; `brain_sessions` 324 `ended` /
117 `abandoned` / 28 `open` ; `tickets` 231 dont **187 self** (`from_project =
to_project`) et **44 cross-projet**.

> **CAVEAT posé le 2026-08-20 — ces trois nombres ne bouclent pas, et aucun n'est
> corrigé ici.** `324 + 117 + 28 = 469`, alors que le reste du document mesure
> **467 lignes** (§1.1, §3.3, passe de pliage). Et le `28 open` de cette ligne contredit
> le **29** cité six fois ailleurs (§0.2, §0bis.2, §0ter.2, §1.1, §3.3). Les deux écarts
> sont réels et **non résolus** : la mesure du 2026-08-19 n'est plus rejouable, la table
> ayant bougé depuis. **Ne dériver aucun raisonnement de cette ligne** — la rejouer.
> *Rien n'en dépend : les conclusions du §0bis reposent sur le ratio « 24 sur 29 », pas
> sur ce total.*

| # | Réponse opérateur | Ce qu'elle emporte |
|---|---|---|
| **Ouverture** | **`start` devient AUTOMATIQUE.** Un geste explicite unique — le *claim* — marque la session comme humaine | **Inverse le défaut de nature** (§0bis.1). Amende C1 une seconde fois : le serveur OUVRE aussi |
| **Q9** | *(réglée par la mesure, pas par arbitrage)* Les subagents **héritent** de la session du porteur | §0bis.2. Aucun tag à poser : ils partagent la connexion |
| **Corollaire Q1** | *(dissous)* « exactement un » devient vrai **par construction** | §0bis.2. La clé d'ouverture est `(projet, connexion)` |
| **Timeout** | Une session **claimed n'est jamais** fermée par inactivité. Traçantes : seuil généreux, propre réglage | §0bis.3 |
| **Q2** | **`from_project`** — la paternité | La capture répond à « qu'a produit cette session ». Débloque M-B |
| **Q14** | **(a)** élargir `knowledge_sources` aux tickets, et son prédicat au sous-arbre | L'attestation reste verte et continue de prouver ce qu'elle prétend. **Sur les DEUX assets v4** |
| **Q3** | **Stockage de la proposition, forme du payload du ticket** | Append-only `(session_id, seq)` pour le replay idempotent ; `progress` + `blocker\|null` + `next_step` en **un appel**. (a) et (b) sont **dissoutes** (§0bis.4) |
| **Q6** | **Acceptée**, et les brouillons non signés d'une traçante **survivent** dans un pool en attente | §0bis.5. Nouvelle sous-question, née de l'auto-fermeture |

**LA PHASE 0 EST DÉBLOQUÉE** : Q2, Q3, Q6, Q10 et Q14 sont répondues. Restent ouvertes,
mais **aucune ne bloque la Phase 0** : Q4, Q5, Q7, Q8, Q11, Q13.

### 0bis.1 L'ouverture automatique inverse le défaut de nature

**Correction d'une dérivation du §0.1, à lire avant de l'appliquer.** Le §0.1 conclut
« défaut = `operator`, forcé par C2 ». C'était juste **sous l'hypothèse que `start`
reste une commande explicite**. Sous l'ouverture automatique, cette conclusion s'inverse :
si toute session auto-ouverte naissait `operator`, chaque appel d'outil d'un agent
créerait une session à rituel qui ne se referme jamais — **B1 industrialisé**.

**Nouveau défaut : `agent` (traçante).** Et le danger que le §0.1 cherchait à éviter —
perdre le canal de jugement sans trace — ne revient pas, pour une raison propre : **le
claim EST la déclaration d'intention d'écrire du jugement.** Pas de claim ⇒ personne
n'attend de résumé ⇒ l'auto-fermeture ne détruit rien. La règle de C2 est donc respectée
sous une forme différente, et le §0.1 n'est pas contredit dans son principe, seulement
dans son application.

**Le claim est RÉTROACTIF** — à n'importe quel moment tant que la session est ouverte, pas
seulement à l'ouverture. C'est ce qui supprime le dernier mode de panne (« j'ai oublié de
claim au début »). Il **promeut** une traçante en `operator` ; il ne construit pas une
session.

**Amendement de covenant plus large que celui du §0 :** le serveur n'auto-ferme plus
seulement, il **auto-OUVRE**. C1 est amendé une seconde fois. L'objection versée en §3.3
contre l'ouverture automatique (« B8 prouve qu'elle attribuerait faux en stdio ») s'en
trouve fortement affaiblie en HTTP — voir §0bis.2 — mais **reste entière en stdio**, qui
n'a aucun en-tête. Production = HTTP loopback ; stdio = dev/fallback. À écrire comme une
dégradation connue, jamais à passer sous silence.

### 0bis.2 La clé de connexion — mesurée, déjà branchée, et lue par personne

La question posée par l'opérateur était : *comment tagger facilement les subagents ?*
**La réponse mesurée est qu'on ne peut pas, et qu'il ne faut pas.** Ce que le serveur
voit sur chaque appel, vérifié dans le code le 2026-08-19 :

| En-tête | Ce qu'il porte réellement | Distingue un subagent ? |
|---|---|---|
| `X-Brain-Agent` | **Le projet.** `~/.claude.json` envoie `${PWD}`, que `normalize_agent` réduit au basename ; le `.mcp.json` du dépôt envoie le littéral `brain-v42` | **Non** — chaîne identique pour l'opérateur, ses agents et ses subagents |
| `X-Brain-Session` | Mort : `normalize_session → None` en nominal (B8) | **Non** |
| `Mcp-Session-Id` | **La connexion**, frappée par le **SERVEUR** (`uuid4().hex`, `streamable_http_manager`) | **Non — et c'est exactement la propriété recherchée** |

**Aucun signal ne distingue un subagent de son porteur, et aucun ne le fera** : les
en-têtes viennent de la configuration du client, qui est **par serveur MCP**, pas par
subagent. Tagger exigerait une capacité amont inexistante — même classe que B8. **Donc
les subagents héritent** (réponse à Q9), gratuitement, parce qu'ils partagent la
connexion de leur porteur.

**Trois propriétés qui tombent de là :**

1. **`Mcp-Session-Id` est le seul des trois identifiants que le client NE DÉCLARE PAS.**
   Il est frappé côté serveur. Ouvrir automatiquement sur cette clé, c'est attribuer sur
   le seul signal non falsifiable de la chaîne — nettement plus solide que le régime
   « déclaré, pas prouvé » qui gouverne tout le reste.
2. **Il distingue deux fenêtres Claude Code sur le même projet**, ce qu'`X-Brain-Agent`
   ne peut pas faire. `config.py` porte déjà ce raisonnement à propos du mode sans état :
   « sans identifiant de connexion, quatre moteurs lancés dans un même répertoire
   déclarent le même acteur et s'effondrent en UNE ligne. »
3. **Le corollaire de Q1 se dissout.** « Exactement un » vs « toutes » n'a plus d'objet :
   sur une clé `(projet, connexion)`, c'est exactement un **par construction**. La
   population ambiguë mesurée (24 sessions `open` sur 29 dans un projet à ≥2) cesse
   d'être un problème à arbitrer.

**Il est déjà là et personne ne le lit.** `provenance.py` définit `normalize_transport`,
`set_current_transport` et `get_current_transport` ; le middleware appelle
`set_current_transport()` sur **chaque** appel ; la valeur part vers le sidecar de
métriques (`metrics/client_observation.py`). Et **`get_current_transport()` a ZÉRO site
d'appel** dans tout le dépôt — mesuré. Le discriminant nécessaire est plombé, alimenté et
inutilisé.

**Précondition de production, vérifiée** : `mcp_http_stateless` vaut `False` par défaut
(`config.py:229`) et **aucun drop-in ni `.env` ne l'écrase** — le serveur tourne donc avec
état et l'identifiant de connexion existe. En mode sans état il n'y en a pas : c'est le
levier de secours documenté, et il **casserait cette clé**. À traiter comme une
précondition dure, pas comme un détail de configuration.

### 0bis.3 Le timeout des traçantes

Contrainte opérateur : *« je veux un bon timeout, histoire de ne pas avoir des sessions
qui n'ont pas fini qui se font couper en cours — ça ne doit pas arriver. »*

**La garantie principale n'est pas le seuil, c'est la nature.** Une session **claimed
n'est JAMAIS fermée par le timeout d'inactivité** — c'est le sens même du claim. Seul le
sweep 7 j existant peut la prendre, et c'est ce pour quoi il a été construit. Le risque
redouté est donc **structurellement impossible** sur les sessions de l'opérateur, pas
seulement improbable.

Le timeout ne s'applique qu'aux traçantes, avec trois garanties :

1. **Jamais pendant qu'un appel est en vol.** La machinerie existe :
   `provenance.py` tient la profondeur d'appel (`enter_call` / `exit_call` /
   `is_outermost_call`). Le « coupé au milieu » littéral est gratuit à interdire.
2. **Le seuil compte l'inactivité OBSERVÉE**, et `last_observed_at` bouge à *chaque*
   appel d'outil. Une session qui travaille ne s'en approche jamais.
3. **Être coupé coûte un DÉCOUPAGE, pas une perte.** Le ledger de la session fermée est
   conservé ; l'activité suivante ouvre une nouvelle traçante. Le pire cas est cosmétique.

**Valeur proposée : 4 heures d'inactivité observée, sur son PROPRE réglage** — surtout pas
les 900 s de `MCP_HTTP_SESSION_IDLE_SECONDS`, qui gouverne un objet réseau et n'a pas à
piloter un objet de connaissance. 4 h survit à une pause déjeuner et à tout trou de
réflexion plausible, et reste 42× plus court que le sweep 7 j, donc les fantômes ne
s'accumulent pas. ~~**Proposée, pas signée**~~ — l'opérateur a demandé « généreux » sans
donner de nombre.

> **AMENDÉ le 2026-08-20 — §0ter.5 fait foi.** Le nombre est **signé**, mais sa lecture a
> changé : logée dans le sweep nocturne (résolution (d) ratifiée), la valeur 4 h est un
> **seuil d'ÉLIGIBILITÉ** évalué une fois par nuit, **pas un délai de fermeture**. Une
> traçante devenue inactive juste après un passage vit jusqu'au suivant : latence réelle
> pire cas ≈ **28 h**. Le « 42× plus court que le sweep 7 j » ci-dessus compare donc deux
> seuils d'éligibilité, pas deux délais. Les trois garanties numérotées, elles, tiennent
> inchangées.

### 0bis.4 Ce que Q3 perd, et ce qu'il devient

Q3 avait quatre sous-décisions, dont deux piégées — (a) le cercle des appelants et (b)
l'effet heartbeat et son mécanisme d'armement, dont D4 signalait qu'elles s'armaient **par
omission**. **Les deux se dissolvent sous Q12 = (a) + l'ouverture automatique.**

Le danger nommé par D4 était : *le checkpoint rafraîchit `last_heartbeat_at`, seul signal
du sweep, donc un agent qui checkpointe seul maintient sa session vivante indéfiniment —
le faux-vivant que `2bd14b24` condamne — et rend le critère 4.3 auto-satisfiable*. Sous le
nouveau modèle, la vivacité d'une session agent vient de `last_observed_at`, qui bouge à
**chaque** appel d'outil. Le checkpoint cesse d'être spécial : c'est une observation parmi
d'autres, et un agent actif qui maintient sa session vivante est le comportement
**correct**. Côté `operator`, le timeout ne mord pas du tout. `BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT`
n'a donc plus d'objet.

**Le checkpoint change de nature** : il cesse d'être un mécanisme de vivacité pour devenir
un **objet de jugement pur**. Il repasse du bon côté de la grille FAIT/JUGEMENT du §1.3,
et son unique métier redevient B7 — la fraîcheur sémantique.

**Réponse sur ce qui restait, et elle est mixte, à dessein :**
- **(c) stockage — la proposition.** Append-only, `UNIQUE(session_id, seq)`,
  `ON CONFLICT DO NOTHING` : le replay d'un retry est **idempotent**, ce que le CAS
  `expected_checkpoint_revision` du ticket ne donne pas. Les retries d'agents sont la
  norme (invariant C6), pas l'exception.
- **(d) forme du payload — le TICKET.** `progress` + `blocker|null` + `next_step` publiés
  **ensemble, en un appel**, contre trois `kind` mutuellement exclusifs d'une note unique.
  Motif : trois `kind` permettent d'émettre un `progress` sans jamais de `next_step`, et
  le lecteur de fraîcheur ne peut pas savoir si l'instantané est complet. La divergence
  (d) est donc **abandonnée** ; celle de stockage (c) est **maintenue et assumée**.

### 0bis.5 Q6 — acceptée, et les brouillons non signés survivent

**La fragilité technique de Q6 disparaît.** L'écrivain de brouillons devait lier ses
observations à une session par `(project_key, started_by_actor)` et la règle « exactement
un » — fragile sur une population à 24 ambiguës sur 29. **Sur la connexion, la liaison est
exacte** (§0bis.2). Q6 perd son principal risque d'implémentation avant même d'être armée.

**Mais « le serveur prépare, l'opérateur signe » suppose un opérateur, et une traçante
s'auto-ferme sans que personne ne signe.** Cette sous-question naît de l'auto-fermeture et
ne figure nulle part dans le dossier.

**Réponse : les brouillons non signés SURVIVENT** à l'auto-fermeture de leur session, dans
un pool en attente de signature, hors session. Rien n'est perdu ; rien n'est attribué sans
geste humain. Les deux alternatives ont été écartées explicitement : l'auto-promotion à
l'auto-fermeture serait **E3 pour toute la moitié agent du modèle** — changement de
covenant plein, pas le demi-pas que Q6 proposait —, et jeter les brouillons non signés
reviendrait à détruire précisément ce que la nature agent produit, sous un axe de priorité
qui est la traçabilité du savoir.

**Ce que ça ajoute à M-E, et qui reste à spécifier** : un brouillon doit pouvoir survivre à
la session qui l'a observé. Le `status` CHECK `∈ {staged, promoted, dismissed}` tient, mais
la FK vers `brain_sessions` et son `ON DELETE`, la durée de vie du pool, son plafond, et le
tool de signature hors session ne sont **pas** spécifiés.

---

## 0ter. Cadrage opérateur — session du 2026-08-20

Troisième session, le lendemain. Elle ne tranche aucune question neuve : elle **signe les
trois dernières questions que le §0bis avait ouvertes en les résolvant**, plus les quatre
résolutions d'implémentation qui avaient été *proposées* sans jamais être soumises
(décision `23bf6088`), plus le seuil des traçantes que le §0bis.3 laissait explicitement
« proposé, pas signé ». Décision d'origine : `c5160259-a33a-4dfc-b343-992746604b7a`.

**Ces trois questions bloquaient la première ligne de code.** Elles ne la bloquent plus.

| # | Réponse opérateur | Ce qu'elle emporte |
|---|---|---|
| **(a) Têtes** | **M-A et M-G partent en UNE SEULE tête.** | Une régénération des deux assets v4, **un** rendez-vous de production, **un** bump du pin. Surtout : plus de fenêtre où des sessions `agent` existent sans état terminal atteignable (§0ter.1) |
| **(b) stdio** | **PAS DE SESSION AUTOMATIQUE du tout en stdio.** Le cycle explicite `start`/`resume`/`end` y reste disponible, inchangé | L'auto-ouverture n'existe qu'en **HTTP**, sur la clé `(projet, connexion)`. Dégradation connue et **écrite**, pas subie (§0ter.2) |
| **(c) Garde** | **`expected_client_key` RETIRÉE du chemin résolu-par-connexion, GARDÉE partout ailleurs** | Le chemin neuf ne transporte pas une clé qui ne garde rien. Les cinq chemins explicites existants (`resume`, `capture`, `heartbeat`, `end`, `abandon`) ne bougent pas (§0ter.3) |
| **(d) Résolutions** | **Les QUATRE résolutions de `23bf6088` RATIFIÉES telles que proposées** | Fail-open ; fermeture des traçantes dans le sweep nocturne ; `nature IS NULL` au régime 7 j ; covenant réécrit par nature (§0ter.4) |
| **Seuil** | **4 h SIGNÉ — mais sous une lecture qui a changé** | Seuil d'**ÉLIGIBILITÉ** au balayage nocturne, jamais un délai de fermeture. Latence réelle pire cas ≈ **28 h**. Amende le §0bis.3 (§0ter.5) |

**LA TRANCHE MINIMALE EST DÉBLOQUÉE** — M-A+M-G en une tête, auto-ouverture, tool *claim*,
fermeture d'inactivité, derrière un flag fermé. **`SPEC M-G` devient écrivable** : elle
n'attendait que (a). Restent ouvertes, et aucune ne bloque : Q4, Q5, Q7, Q8, Q11, Q13.

> **Précondition dure reconduite** : `mcp_http_stateless=False`. Toute la clé de connexion
> en dépend. Elle n'est pas re-signée ici parce qu'elle n'a jamais été mise en question —
> mais elle reste le point unique dont la bascule ferait tomber (b) et (c) ensemble.

### 0ter.1 Une tête, et pourquoi l'argument procédural cède

Les deux arguments n'étaient pas de même nature, et c'est ce qui a tranché. « Deux têtes »
est **procédural** — le grain du couloir du pin, une tête à la fois, chacune avec son bump
et son test dans le même commit. « Une tête » est **fonctionnel** : `M-A` livrée seule est
une livraison *inerte et malsaine*, parce que les sessions `agent` qu'elle fait naître
n'ont aucun état terminal atteignable tant que `M-G` n'est pas là (§0.4).

Or le couloir interdit **deux têtes en vol** (§5.3). Deux têtes ne signifient donc pas
« deux petits pas » mais **deux rendez-vous de production séquentiels**, avec entre les
deux exactement la fenêtre que l'argument fonctionnel condamne. Le procédural, appliqué
ici, produit le risque qu'il est censé réduire.

**Ce que la tête unique emporte, et qu'il faut tenir ensemble** : les deux assets
`ops/recovery/` v4 régénérés en une passe — l'empreinte du CHECK terminal **et** la liste
fermée `expected_session_indexes`, que l'index UNIQUE de connexion de `M-A` casse
(quatrième mécanisme, §5.2 *(ii)*) ; un seul bump de `_REQUIRED_ALEMBIC_HEAD` ; et le
double rail Pydantic (C7) qui bouge **avec** le CHECK, jamais après.

### 0ter.2 stdio : pas de session du tout, et la dégradation est écrite

Le §0bis.2 avait laissé le point en suspens : l'objection B8 contre l'auto-ouverture
« reste entière en stdio, qui n'a aucun en-tête ». Deux issues étaient possibles — pas de
session, ou repli sur `(projet, acteur)`.

**Le repli est écarté** parce qu'il réintroduirait exactement l'ambiguïté que la clé de
connexion dissout : **24 des 29 sessions `open`** mesurées logées dans un projet qui en
porte au moins deux (2026-08-19). Il attribuerait sur du **déclaratif** — un en-tête
`X-Brain-Agent` que le client choisit — là où toute la valeur du modèle est d'attribuer
sur le seul signal **non falsifiable**, le `Mcp-Session-Id` frappé côté serveur.

**Ce qui est perdu, et qu'on écrit plutôt que de le taire** : on ne peut pas exercer
l'auto-ouverture en local sous stdio. Le développement de cette feature se fait donc
contre le transport HTTP loopback, pas contre le fallback. C'est acceptable parce que la
production **est** HTTP loopback et que stdio est le chemin dev/fallback — mais c'est une
dégradation réelle du confort de développement, pas un détail de configuration.

### 0ter.3 La garde retirée là où elle ne garde rien

`expected_client_key` prévient **le mauvais ciblage entre sessions parallèles** : on
adresse un UUID, on prouve qu'on connaît la clé qui va avec. Elle n'a jamais authentifié
personne, et le contrat le dit déjà (« c'est une garde d'isolation, pas une
authentification »).

Sur le chemin résolu-par-connexion, **le mauvais ciblage est impossible par construction** :
il n'y a pas d'UUID adressé, la connexion *est* l'identité. Garder la clé y obligerait le
chemin neuf à transporter un jeton qui ne protège de rien — du bruit dans un contrat qu'on
est précisément en train de simplifier.

**Elle reste telle quelle sur les cinq chemins explicites** (`resume`, `capture`,
`heartbeat`, `end`, `abandon`), où l'UUID est adressé et où le risque existe. Aucun de ces
contrats ne bouge, et c'est voulu : la signature ne rend pas la garde inutile, elle
constate qu'un chemin neuf n'en a pas besoin.

### 0ter.4 Les quatre résolutions, ratifiées telles que proposées

Elles avaient été **proposées et jamais vues** — la décision `23bf6088` les portait comme
recommandations, pas comme acquis. Elles sont maintenant signées, sans amendement :

| Résolution | Argument retenu |
|---|---|
| **Auto-ouverture FAIL-OPEN** | Asymétrie de coût : un hoquet de base ne doit pas casser tout le serveur MCP. Même posture que l'émetteur d'activité client (`1c40c36a`), dont l'échec ne peut pas casser l'appel qu'il observe |
| **Fermeture des traçantes dans le SWEEP NOCTURNE** | Zéro machinerie neuve, et des gardes déjà **prouvées** : réévaluation sous verrou de ligne, confirmée PROPRE par l'instruction de la nuit 19→20 (21/21 sessions, zéro faux positif, focus intact) |
| **`nature IS NULL` reste au régime sweep 7 j** | Ne pas changer les règles rétroactivement. Les sessions antérieures à `M-A` n'ont pas de nature ; les soumettre au seuil neuf les jugerait sous un contrat qu'elles n'ont jamais connu |
| **Phrase-covenant RÉÉCRITE par nature, pas supprimée** | Le contrat doit rester lisible **là où l'agent le lit** — dans la docstring du tool. Conséquence assumée : le test d'ancrage `0207209` rougira. C'est voulu, et c'est le geste Red qui ouvre la livraison |

**Le prix du fail-open, écrit une fois pour toutes** : si l'ouverture automatique échoue et
que l'appel passe quand même, les artefacts créés avant l'ouverture réussie tombent **hors
de la fenêtre `created_at >= started_at`** de la capture. **B5 redevient donc mordante,
ponctuellement.** Ce n'est pas un effet de bord découvert après coup : c'est le coût
accepté de ne pas faire tomber le serveur, et la `SPEC M-G` doit le porter.

### 0ter.5 4 h : signé, et sa signification a changé

Le §0bis.3 proposait « 4 heures d'inactivité observée » et se concluait par
« **Proposée, pas signée** ». Le nombre est signé. **Sa lecture, elle, n'est plus la
même**, et c'est le point à ne pas manquer.

Sous la résolution (d) — fermeture logée dans le **sweep nocturne** — 4 h cesse d'être un
délai. C'est un **seuil d'éligibilité** évalué une fois par nuit : une traçante devenue
inactive juste après le passage nocturne devient éligible quatre heures plus tard, mais
**vit jusqu'au passage suivant**. La latence réelle pire cas est de l'ordre de **28 h**,
pas de 4.

**Ne jamais annoncer « 4 h » comme une garantie de délai de fermeture.** Le §0bis.3 est
amendé sur ce point : ses trois garanties (jamais pendant un appel en vol, inactivité
observée et non horloge murale, découpage plutôt que perte) tiennent inchangées, et la
garantie principale reste la nature — **une session `claimed` n'est jamais fermée par
inactivité**, quel que soit le seuil.

Q5 (seuils du sweep) et ce seuil se répondront **ensemble**, dans le même statement
nocturne : les séparer produirait deux passes concurrentes sur la même table pour deux
règles qui décrivent le même geste.

---

## 1. Contexte

### 1.1 Ce qui existe (vérifié dans le code au 2026-08-18)

**Sessions** — lifecycle v4 (migrations 032 + 037, production depuis le 2026-07-24) :
sept commandes MCP explicites (`start`/`list`/`resume`/`capture`/`heartbeat`/`end`/
`abandon`), machine d'états en double rail (CHECK SQL 037 + Pydantic), CAS de focus non
bloquant (`applied`/`conflict`), ledger d'attribution exclusif (PK `knowledge_id` dans
`brain_session_artifacts`), replays idempotents. Le **covenant** (CLAUDE.md, docstrings
des sept tools) : ces commandes sont des gestes explicites de l'utilisateur ; seule
exception serveur, le sweep `auto_stale_7d` à 7 jours. État production **mesuré le
2026-08-18** (drop-in `killswitches.conf` de l'unité dream inspecté — règle N6
appliquée à ce document même) : le sweep est **armé en DRY depuis le 2026-08-18, par
décision opérateur** (`BRAIN_DREAM_SWEEP_ENABLED=true`, `BRAIN_DREAM_SWEEP_DRY_RUN=true`,
18 fantômes balayables mesurés ce jour-là) ; le WET n'a jamais été armé.
**Ce « 18 » est déjà périmé — re-mesuré le 2026-08-19 (lecture seule) : 29 sessions
`open`, dont 21 balayables à plus de sept jours** (`count(*) filter (where
status='open')` et le même filtre sur `last_heartbeat_at < now() - interval '7 days'`,
sur 467 lignes). Chiffre daté et **périssable** : le rejouer avant toute décision
d'armement, jamais le recopier — y compris depuis ce paragraphe. Une version
antérieure de ce paragraphe recopiait « jamais armé » depuis un ticket du 2026-08-16 —
l'état avait changé avant la finalisation du document, exactement le mode de panne que
N6 interdit.

**Projets** — quatre briques éparses : le format (`src/brain_v42/models/project_key.py`,
canonicalisation stricte en écriture / tolérante en lecture, alias `brain`/`brain_v42`
→ `brain-v42`), trois tables — `project_contexts`, l'objet opérationnel réel, créée par
la **001** (`alembic/versions/001_initial.py`, « 8. Create project_contexts table ») ;
`projects` et `project_aliases`, le registre, créés par la **033**, qui n'ajoute à
`project_contexts` que l'immuabilité de la clé et la normalisation des alias, par
triggers (mesuré : `project_contexts_project_key_immutable_trigger`,
`project_contexts_project_alias_trigger`) — une version antérieure de cette ligne
compressait les trois en « migration 033 », régression d'avec le DOSSIER qui l'écrit
juste ; et une convention colon (`red-shrik:agent`).

Le prédicat de sous-partition `base NOT LIKE '%:%' AND key LIKE base || ':%'` vit en
**cinq exemplaires recopiés à la main** dans `src/` — deux de plus que ne le disait une
version antérieure de ce paragraphe, qui se félicitait pourtant d'avoir corrigé
« partout sauf un endroit ». Le recensement re-vérifié le 2026-08-18, grep par grep :
1. `db/project_group_scope.py:24-26` — l'implémentation de référence ;
2. `services/project_group_ticket_service.py:129-137` — copie SQL inline ;
3. `services/proposal_service.py:377-383` — copie SQL inline alors même que le module
   importe `project_group_scope` ;
4. `repositories/pg_project_context.py:202-213` (`get_keys_by_group`) — **même
   sémantique en variante `split_part`** (« la clé contient un deux-points ET son
   préfixe est une clé de base du groupe »), invisible à un grep sur `not_like("%:%")` ;
5. `services/project_group_ticket_service.py:164-167` — **seconde copie, en Python**
   (`project_key == base_key or (":" not in base_key and
   project_key.startswith(f"{base_key}:"))`), dans la méthode `_lock_participants_scope`
   même que la copie n° 2.

Côté base, ce ne sont pas « deux vues des migrations 024 et 036 » mais **sept vues
vivantes, toutes issues de la 036** (mesuré : `select table_name from
information_schema.views where table_schema='public' and view_definition like
'%split_part%'` → `codex_brain_entity_v1`, `codex_feature_artifact_v1`,
`codex_feature_v1`, `codex_roadmap_curation_proposal_v1`,
`codex_ticket_extraction_proposal_v1`, `codex_ticket_message_v1`, `codex_ticket_v1`).
Elles naissent de **deux corps de CTE recopiés** dans
`alembic/versions/036_codex_contract_views.py` (`_RED_KEYS_CTE:23-45` pour six d'entre
elles, `_BRAIN_RED_KEYS_CTE:205-227` pour `codex_brain_entity_v1`), tous deux en
`split_part(project_key, ':', 1) <> project_key AND split_part(…) IN red_base` —
formulation **différente** de celle de `src/`. La 024 n'est pas un second objet vivant :
elle avait posé `codex_brain_entity_v1` (`024:80`), que la 036 remplace par
`CREATE OR REPLACE VIEW` (`036:230`). Total réel : **cinq exemplaires `src/` + deux
corps de CTE servant sept vues**, en trois formulations distinctes — la thèse de la
greffe A (« la sémantique ad-hoc dérive ») est prouvée plus fort que ce que le document
en disait. La spec dream `dbb7c5ce`, elle, filtre en égalité stricte.

**Observation** — `ProvenanceMiddleware` (`src/brain_v42/mcp/provenance_middleware.py`)
lit `x-brain-agent` et `x-brain-session` sur chaque appel de tool, voit le tool réel
derrière `brain_call_tool`, persiste l'acteur dans `access_log.actor` (`String(64)`,
migration 041) — et n'alimente ni heartbeat, ni ledger, ni liveness.

### 1.2 Les douleurs prouvées (dossier d'instruction, condensé)

| # | Douleur | Preuve | Gravité |
|---|---|---|---|
| B1 | Lifecycle déclaratif ≠ subagents éphémères : 39 sessions fantômes en un ménage | ticket `2bd14b24` | **Critique** |
| B2 | `last_heartbeat_at` ment dans les deux sens le même jour (faux-mort d'une session vivante + 39 faux-vivants) | ticket `2bd14b24` | **Critique** |
| B3 | 18 % des sessions fermées capturent, 34 % des artefacts attribués — pendant que le middleware voit tout | ticket `7ffe0e8a`, mesures 2026-08-16 (périssables) | **Haute** |
| B6 | La discipline de contenu du focus repose sur l'agent seul ; le CAS ne garantit que l'arithmétique | ticket `d30cf6e5` | **Haute** |
| B8 | `X-Brain-Session` est mort (`normalize_session → None` nominal) : l'identité de session côté client est impossible | spike du 2026-08-06, verdict « JOINTURE IMPOSSIBLE » (`docs/upstream/2026-08-06-claude-otlp-session-join.md`), relayé par `7ffe0e8a` — le ticket `2dfbb83d`, lui, a été fermé LIVRÉ le 2026-08-16, pas négatif (citation corrigée) | **Haute (contrainte) — RE-MESURÉE ET CONFIRMÉE le 2026-08-19.** Une version antérieure de cette case disait « cotée sur une mesure PÉRIMÉE » (spike sur 2.1.220, `claude --version` à 2.1.234) : **le re-jeu a été fait**, sur 2.1.234, et le verdict est **inchangé** — `docs/upstream/2026-08-19-b8-session-join-rejeu.md`. Les deux cas ont été joués : environnement parent intact ⇒ l'identifiant reçu est celui du **PARENT** (faux positif reproduit) ; `CLAUDE_CODE_SESSION_ID` retirée ⇒ littéral non expansé, `normalize_session → None`. Témoin `${PWD}` correctement expansé : le mécanisme marche, la variable n'existe pas au bon moment. B8 n'est donc plus cotée sur du périmé |
| B11 | Sous-projets colon : **86 artefacts sur 533 n'ont AUCUN run nocturne** ; pour les six clés, le parent ne voit jamais l'enfant (égalité stricte). `red-shrik:agent` (312) pèse toujours plus que son parent (245) | spec `dbb7c5ce` §2 **re-mesurée** le 2026-08-18 + pool dream lu dans le drop-in | **Moyenne** (revue à la baisse — voir ci-dessous) |
| B4 | Tickets non capturables (`CAPTURE_TABLES` = six tables) ; lot mélangé fail-closed en bloc | vécu `d30cf6e5` + code | Moyenne |
| B5 | Fenêtre de capture rigide : projet exact + `created_at >= started_at` | code `pg_brain_session.py` | Moyenne |
| B7 | Aucun checkpoint sémantique ; la fraîcheur, c'est le silence | ticket `d04dc588` | Moyenne |
| B9 | `client_key` libre, prolifération mécanique post-terminal | ticket `2bd14b24` | Moyenne |
| B12 | Système projets sans doc de bout en bout | ticket `d30cf6e5` | Moyenne |
| B10 | Drift fantôme underscore — fermée, mais dimensionnante : « la mauvaise clé est impossible à persister » ne doit jamais régresser | learnings `7bc821a1`, `367e27ae` | Historique |
| B13 | L'erreur de `_validate_captures` est indifférenciée : les ids rejetés sont listés mais une **seule raison agrégée** pour tous (vérifié) | code + panel | Moyenne |
| B14 | `brain_sessions` ne persiste pas l'acteur qui démarre, alors qu'`access_log.actor` existe depuis la 041 | code + panel | Moyenne |

**B11 était chiffrée sur un état périmé de dix jours — correction.** Une version
antérieure reprenait « 479 artefacts hors consolidation » de la spec `dbb7c5ce`
(mesures du 2026-08-08) sans relire le pool vivant. Or le remède que cette spec proposait
en premier — « soit ajouter les six clés au pool » — est **2/6 exécuté depuis le
2026-08-10** : le drop-in `killswitches.conf` (lu le 2026-08-18) porte
`BRAIN_DREAM_PROJECT_POOL=…,red-shrik:agent,…,red-lab:architect,…`. Re-mesuré ce jour
(`group by project_key` sur les cinq tables de connaissance) : `red-shrik:agent` 312,
`red-lab:architect` 135 — **447 des 533 artefacts colon, soit 84 %, reçoivent désormais
leur propre nuit**. Le résidu sans aucun run est de **86 artefacts** sur quatre clés
(`red-lab:orchestrator` 64, `:reviewer` 15, `:sentinel` 5, `:developer` 2). Ce qui
reste entier pour les six, en revanche, c'est la consolidation **croisée** : le pipeline
filtre en égalité stricte, donc la nuit de `red-lab` ne verra jamais
`red-lab:architect`, fût-il dans le pool. C'est cette moitié-là que le prédicat partagé
et `include_descendants` (D3/D10) adressent — pas le nombre 479, qui a fondu. Et le
pool est **au plafond de dix** : ajouter les quatre clés restantes exige de relever
`_MAX_POOL` **et** `TimeoutStartSec` ensemble (le PLAN 4.6 le pose au conditionnel, à
tort — c'est déjà la situation).

L'opérateur a annoncé « pas mal de choses qui ne me plaisent pas » **sans détailler** :
cette liste est dérivée des preuves et doit lui être soumise pour confirmation,
priorisation et complétion (question ouverte n° 10).

**Une réponse opérateur est déjà ÉTABLIE et cadre tout ce document** (ticket
`2bd14b24`, 2026-08-06) : une session sert à la **traçabilité du savoir**, et « le
cycle de vie doit être automatique, pas déclaratif » — ce qui condamne à terme le
modèle actuel où ouverture ET fermeture sont des actes déclaratifs. Trois pistes de
mécanisme lui ont été présentées ((a) deux natures de session — agent traçante
auto-fermée sans rituel, opérateur avec rituel ; (b) plus de rituel du tout, jugement
non dérivable migré vers un objet dédié ; (c) fin auto avec résumé dérivé par le
serveur). **La piste (a) A ÉTÉ CHOISIE par l'opérateur le 2026-08-19** (§0, Q12) : deux
natures de session. Ce paragraphe a écrit « aucune n'a été choisie — il s'est
explicitement réservé ce choix » et c'était exact jusqu'au 2026-08-18 inclus ; le laisser
tel quel après le cadrage aurait été l'erreur classique de ce dossier — un instantané qui
ment dès que le sujet bouge.

Ce que cela change pour la lecture du reste du document : l'accrétion décrite plus bas
conserve le lifecycle déclaratif **pour la seule nature `operator`**, et non plus « à
titre transitoire » pour tout le monde. Les signaux instrumentés (observation,
checkpoint, sweep) restent bons — c'était le pari du plan et il tient — mais D5 cesse
d'être optionnel : il devient le **seul** signal de vivacité de la nature `agent`
(§0.2). Deux invariants sont amendés en conséquence, C1 et C7.

Une version antérieure de ce document présentait ces pistes comme des alternatives
écartées et « confirmées par le panel » — c'était un arbitrage de covenant glissé,
corrigé alors (voir §3.3). Le choix qui figure ici, lui, vient de l'opérateur.

### 1.3 Le constat structurant (l'insight que le panel a retenu de B)

Sur les quatorze douleurs, **onze se fermaient sans toucher à la machine d'états 037 ni
au covenant** — c'était vrai du plan tel que proposé, et **le cadrage du 2026-08-19 l'a
invalidé** : Q12 = (a) amende le covenant pour la nature agent, et Q15 = (3) ouvre la
migration M-G sur le CHECK terminal lui-même (§0.4). **Ce compte de onze n'a pas été
recalculé** et ne doit pas être cité tel quel : le recompter fait partie du contenu de la
Phase 0. Ce qui survit du constat, et qui reste vrai, est plus étroit — et c'est
l'essentiel : le noyau (états, CAS, exclusivité, idempotence) est sain et prouvé en
production ; c'est sa **périphérie aveugle** qui fait mal : signaux de fraîcheur,
validation de capture, observabilité du focus, ergonomie des erreurs. Et la grille de
lecture qui organise le tout (reprise de la proposition A sur avis du panel) : **chaque
objet du modèle est soit un FAIT que le serveur observe, soit un JUGEMENT que l'humain
déclare — jamais les deux.** Le serveur n'écrit jamais de jugement ; l'agent n'écrit
jamais un fait que le serveur observe déjà.

---

## 2. Décision

Évoluer **par accrétion instrumentée**, sans réécrire le noyau, en dix composants.
Tout comportement runtime nouveau naît derrière un killswitch **fermé**, armé seulement
par geste opérateur documenté ; tout ce qui frôle le covenant est **soumis**, jamais
glissé.

> **Amendé par le cadrage du 2026-08-19 (§0) — lire ce chapeau avec sa limite.**
> « Sans réécrire le noyau » ne tient plus tout à fait : Q15 = (3) ajoute la migration
> **M-G**, qui étend le CHECK terminal 037 d'une branche. Le noyau n'est pas *réécrit*
> — CAS, exclusivité, idempotence et double rail sont intacts — mais il est **étendu**,
> et par choix de cible, non par nécessité de réparation. De même, « soumis, jamais
> glissé » a été tenu : le covenant a bien été soumis (Q12) et il a été **amendé**, pour
> la seule nature agent. Enfin, les dix composants sont devenus **onze** : D11 (nature
> de session) naît du cadrage, et D1 gagne une quatrième colonne.

**D1 — Identité et intention à la source (ferme B14, B9-triage, contrainte B8 assumée).**

> **AMENDÉ le 2026-08-20 — §0bis.2 et §0ter.2 font foi.**
> Cette greffe est conservée telle qu'elle a été soumise ; **sa clé de liaison est
> périmée**. Elle affirme plus bas « **L'identité qui fonctionne est `(projet,
> acteur)`** ». Ce n'est plus vrai : la clé retenue est **`(projet, CONNEXION)`**, frappée
> côté serveur (`Mcp-Session-Id`), donc **non falsifiable** — là où `X-Brain-Agent` est
> déclaré par le client. Le repli `(projet, acteur)` réintroduirait l'ambiguïté que la
> clé de connexion dissout : **24 des 29 sessions `open`** logées dans un projet qui en
> porte au moins deux (mesuré le 2026-08-19).
>
> **Corollaire sur stdio, signé le 2026-08-20** : stdio ne « dégrade » plus « sans
> attribuer » — il **n'ouvre AUCUNE session automatique**. Le cycle explicite
> `start`/`resume`/`end` y reste disponible, inchangé (§0ter.2).
>
> **Ce qui reste vrai** : la portance de B8 et sa date, `access_log` sans colonne projet,
> et le fait que `started_by_actor` reste utile — mais comme **attribut** d'une session,
> plus comme **clé** d'ouverture. Sa décision d'index a d'ailleurs changé d'objet
> (§0bis.2, décision `c3d09355`) : c'est la colonne de CONNEXION qui doit être indexée.
Trois colonnes nullable sur `brain_sessions`, hors machine d'états 037, sans backfill
(doctrine 040/041 : `NULL` = « avant ») :
- `started_by_actor VARCHAR(64)` — acteur `X-Brain-Agent` observé au `start`.
  **Largeur 64, pas 128** : alignée sur `MAX_ACTOR_LENGTH = 64`
  (`src/brain_v42/provenance.py:23`) et `access_log.actor` `String(64)` — correctif
  demandé par le panel, aucune des trois propositions n'avait vérifié ce point.
- `last_observed_at TIMESTAMPTZ` — dernière observation serveur (voir D5). N'est
  **pas** un heartbeat : `last_heartbeat_at` reste la seule trace de la commande
  explicite.
- `intent VARCHAR(500)` — une ligne humaine « pourquoi cette session », paramètre
  optionnel de `start`, affichée dans `list` (greffe C) : c'est l'intention qui manque
  au triage des fantômes, pas une convention de `client_key`.
- **`nature` — quatrième colonne, ajoutée par le cadrage du 2026-08-19 (Q12 = (a), voir
  §0.1 et D11).** Nullable comme les trois autres, sans backfill (`NULL` = « ouverte
  avant M-A »). **Déclarée** par le client au `start`, jamais détectée : B8 rend la
  détection impossible et (a) n'en a pas besoin. **DÉFAUT `agent` — corrigé par la
  session 2 du cadrage (§0bis.1).** Ce paragraphe a d'abord écrit « défaut `operator`,
  forcé par C2 », ce qui était juste **tant que `start` restait explicite** ; sous
  l'ouverture automatique ce défaut ferait naître une session à rituel par appel d'agent,
  soit B1 industrialisé. Le canal de jugement n'est pas perdu pour autant : **le claim est
  la déclaration d'intention de l'écrire**, et il est rétroactif.
> ✅ **MESURÉE ET TRANCHÉE le 2026-08-19** (`baseline/README.md`), **et elle a changé
> d'objet.** Sous la clé `(projet, connexion)` du §0bis.2, l'émetteur D5 ne filtre plus
> sur l'acteur : `started_by_actor` sort du chemin chaud, devient informatif (affichage
> `list`, triage) et **n'a pas besoin d'index**. Ce qui le remplace est plus sérieux :
> **la colonne de connexion DOIT porter un index UNIQUE**. L'`EXPLAIN` du jour montre
> qu'une égalité non couverte force un **Seq Scan de toute la table** (63 buffers contre
> 2 pour un scan d'index), et c'est le chemin le plus chaud du serveur. L'unicité, en
> prime, fait **imposer par la base** la propriété « exactement un par construction » que
> le cadrage se contente d'affirmer. Mesuré au passage : la sous-requête corrélée de
> comptage consommait **27 des 36 buffers** du plan et **disparaît** sous cette clé.
> Réserve : à 469 lignes tout est sous la milliseconde — la conclusion porte sur la
> FORME du plan, pas sur une lenteur constatée.
>
> *Texte d'origine, qui posait la question :*

**Décision d'INDEX à instruire, ajoutée le 2026-08-19 — le mot « index » était absent de
ce document comme du PLAN.** `started_by_actor` est créé **sans index**, et c'est la
colonne sur laquelle D5 filtre à **chaque appel outermost de tool**, deux fois dans le
même statement (le `WHERE` et la sous-requête corrélée de comptage qui implémente
« exactement un »). Les index réels de `brain_sessions`, mesurés le 2026-08-19, sont
exactement trois — `brain_sessions_pkey` (sur `id`),
`uq_brain_sessions_project_client (project_key, client_key)` et
`idx_brain_sessions_project_status_started (project_key, status, started_at DESC)` :
**aucun ne couvre l'acteur**. Le troisième porte le préfixe `(project_key, status)` et
laisse l'égalité sur l'acteur en filtre résiduel, ce qui à la cardinalité mesurée
(467 lignes, 29 `open`) est vraisemblablement gratuit — *vraisemblablement* n'est pas
*mesuré*, et c'est un chemin chaud avec un précédent de perte (le burst loss du
client-activity, `1c40c36a`, tracké et non fermé). Ce document **ne tranche pas** : il
exige que la Phase 0 mesure le statement de D5 sur la table réelle et que le commit de
M-A porte la conclusion écrite. Trois conséquences à connaître avant de la prendre :
(1) un index sur `brain_sessions` casse `expected_session_indexes`, la liste FERMÉE de
l'attestation (§5.2, quatrième mécanisme), **sur les deux assets v4** ; (2) le différer
dans une tête à lui coûte un rendez-vous de production de plus dans un couloir qui
interdit deux têtes en vol (§5.3) — donc s'il faut un index, il voyage **dans M-A** ;
(3) ne rien décider revient à livrer l'émetteur sur un chemin chaud dont le coût n'a
jamais été mesuré.
L'identité qui fonctionne est `(projet, acteur)` : `X-Brain-Session` est mort (B8,
spike du 2026-08-06 « JOINTURE IMPOSSIBLE ») et **`access_log` n'a pas de colonne projet** (vérifié
`db/tables.py` : `entity_type, entity_id, access_type, accessed_at, actor`) — toute
liaison passe par `started_by_actor`, jamais par `access_log` seul.
**Portance de B8, et sa date — à ne pas perdre de vue.** Ce n'est pas une douleur parmi
quatorze : c'est la contrainte qui *dimensionne* le repli sur `(projet, acteur)`, donc D1
(cette colonne), D5 (l'émetteur, `skipped{no_actor}` en stdio), D8 (la liaison des
brouillons), R5 et N2 (la règle d'ambiguïté « exactement un »), le résidu nommé de D6, et
le premier critère de sortie de la Phase 4.1. Or elle est **cotée sur une mesure
périmée** : le spike dit « Version mesurée : Claude Code 2.1.220 » quand
`claude --version` rend **2.1.234** le 2026-08-19, et ses deux sources (`2bd14b24`,
`7ffe0e8a`) exigent l'une comme l'autre de « re-mesurer à chaque montée de version ».
Aucun des trois documents ne programmait cette re-mesure, et la baseline Phase 0 ne
mesurait jamais l'en-tête de session : le PLAN §2 (contenu n° 6) la porte désormais comme
une **étape**, avec son critère de sortie. Tant que le spike n'est pas rejoué, écrire
« B8 » veut dire « B8 tel que mesuré sur 2.1.220 ». Le risque est asymétrique — un
`X-Brain-Session` redevenu vivant n'invaliderait aucun livrable, il ouvrirait une option
que ce plan a écartée par contrainte —, ce qui justifie de continuer à concevoir sous B8,
pas de cesser de le dater.

**D2 — Des erreurs qui expliquent (ferme B13, moitié de B4).**
`BrainSessionInputError` de capture devient structurée : `rejections: [{id, reason}]`
avec `reason ∈ {not_found, wrong_project, created_before_session, ambiguous_type,
attributed_elsewhere, unsupported_type}` + `capturable_subset` (greffe C) listant les
ids qui passeraient. Le lot reste **all-or-nothing** (un lot partiellement appliqué a
deux histoires possibles — la sémantique de replay l'interdit) mais l'agent rejoue le
sous-lot valide en un seul appel informé.

**D3 — Tickets capturables + fenêtre famille (ferme B4, brique de B5/B11).**
8e valeur `ticket` dans le CHECK `brain_session_artifacts_type_valid` (qui en compte
déjà sept — `decision, learning, snippet, runbook, adr, indexed_plan, legacy`,
vérifié `db/tables.py` ; le PLAN les énumère correctement, cette ligne disait « 7e »
à tort) et dans la
validation : prédicat `tickets.from_project == session.project_key AND created_at >=
started_at` (`from_project` = paternité, cohérent avec « self-tickets valides » ;
`to_project` en question ouverte n° 2). La capture parent→enfant d'une sous-partition
(`pk` capture un artefact de `pk:child`) passe par **LE prédicat sous-arbre partagé** —
une seule implémentation, qui **consolide les CINQ exemplaires `src/` recensés en
§1.1** (la greffe A parlait de « divergence solitaire » ; le recensement re-vérifié
prouve encore mieux sa thèse : trois copies SQL, une variante `split_part` et une copie
Python, plus deux corps de CTE servant sept vues côté base — le PLAN Phase 2 §2 porte
le périmètre de résorption, corrigé lui aussi : il en visait deux et en ratait deux,
dont une dans la fonction même qu'il prétendait nettoyer) — derrière un flag fermé.

**D4 — Le checkpoint comme geste (ferme B7, comble le faux-vivant de B2).**

> **AMENDÉ le 2026-08-20 — §0bis.4 fait foi, et `SPEC-checkpoint.md` en dérive.**
> Cette greffe est conservée telle qu'elle a été soumise au panel ; **deux de ses
> affirmations sont périmées** et il faut les lire comme telles.
>
> 1. **L'EFFET HEARTBEAT EST DISSOUS.** La greffe dit « **Effet de bord : rafraîchit
>    `last_heartbeat_at`** sur un checkpoint réel, jamais sur un replay ». Sous
>    Q12 = (a) + l'ouverture automatique, la vivacité d'une session `agent` vient de
>    `last_observed_at`, qui bouge à CHAQUE appel d'outil ; le checkpoint cesse d'être
>    spécial, et une session `operator` n'est jamais fermée par inactivité.
>    `BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT` **n'a plus d'objet** et n'est pas livré.
>    **Le checkpoint n'écrit ni ne touche `last_heartbeat_at`** — ni sur un checkpoint
>    réel, ni sur un replay. La spec en fait un test d'ABSENCE d'effet.
> 2. **LA FORME DU PAYLOAD A CHANGÉ.** `kind ∈ {progress, blocker, next_step, handoff}`
>    + `note` unique est **abandonnée** au profit de `progress` + `blocker|null` +
>    `next_step` publiés **ensemble, en un appel** (§0bis.4, divergence (d) reprise du
>    ticket `d04dc588`). Le stockage append-only `UNIQUE(session_id, seq)` +
>    `ON CONFLICT DO NOTHING`, lui, **est maintenu**.
>
> **Ce qui reste vrai dans la greffe** : le trigger append-only en base, le `seq`
> monotone fourni par le client, l'idempotence du replay exact, le plafond 200/session
> fail-closed, et la fourche de politique d'appel — qui reste ouverte, mais dont le
> danger (le faux-vivant) a disparu avec l'effet heartbeat.

Greffe majeure de C, promue au cœur du plan par les trois juges : tool explicite
`brain_session_checkpoint(session_id, expected_client_key, seq, note ≤2000, kind ∈
{progress, blocker, next_step, handoff})`. Table append-only **garantie par trigger en
base** (culture maison : la 039 épingle par SHA256, pas par absence de chemin de code),
`seq` monotone fourni par le client, `UNIQUE(session_id, seq)` + `ON CONFLICT DO
NOTHING` : **le replay exact d'un checkpoint est idempotent** — pas de seconde ligne,
et le heartbeat n'est pas rafraîchi une seconde fois (les retries d'agents sont la
norme, invariant du dossier). Plafond 200/session fail-closed. **Effet de bord :
rafraîchit `last_heartbeat_at`** sur un checkpoint réel, jamais sur un replay.
**La politique d'appel est une fourche, déclarée, pas glissée** : si chaque checkpoint
reste une commande explicite de l'utilisateur, le covenant est intact — mais l'adoption
dépend alors de la même discipline humaine qui a produit **24 sessions stale sur 29**
(re-mesuré le 2026-08-19 ; ce paragraphe citait « 21 sur 23 », mesure du 2026-08-16, et
« 21 » désigne aujourd'hui le compte des balayables >7 j, pas celui des stale)
(mesure du 2026-08-16), et « le checkpoint date le vivant » (fermeture du faux-vivant
de B2) ne vaut que **s'il est appelé** ; si au contraire un agent peut checkpointer
spontanément en longue session autonome — le cas d'usage qui motive B7 —, c'est une
mutation de session hors commande explicite, donc un **changement de covenant** :
extension du cercle des appelants soumise dans la question n° 3.

**Correction : cette fourche n'a AUCUN mécanisme d'armement, et la règle R3 du PLAN la
tranche d'avance sans le voir.** R3 dispense le checkpoint de killswitch au motif que
« c'est une commande utilisateur, gated par décision d'opérateur » — c'est-à-dire en
posant comme acquise la branche que Q3(a) déclare ouverte. Conséquence matérielle :
l'artefact livré est **identique sous les deux réponses**, il n'existe rien à armer ni à
désarmer, et rien côté serveur ne distingue un appel d'agent d'un appel d'humain (B8 :
`X-Brain-Session` est mort, `X-Brain-Agent` est un projet et reste déclaré par le
client). Or le checkpoint **rafraîchit `last_heartbeat_at`**, seul signal du sweep
vivant : un agent qui checkpointe seul maintient sa session indéfiniment vivante — le
faux-vivant que `2bd14b24` condamne — sans violer une seule contrainte livrée, et rend
le critère 4.3 « zéro abandon d'une session à checkpoint récent » auto-satisfiable par
ses propres écritures. **Conséquence de design, à trancher dans Q3(a) AVANT la
livraison** : soit l'effet heartbeat est retiré du contrat (le checkpoint reste du
jugement daté, la liveness reste heartbeat + observation), soit le tool naît derrière un
flag `BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT`, livré fermé, seul objet armable de la
fourche. Livrer l'effet heartbeat sans l'un des deux, c'est armer le changement de
covenant par omission. **Et le retrait n'est pas un retour au ticket** (relecture du
2026-08-19) : `d04dc588` écrit « Checkpoint réel rafraîchit heartbeat atomiquement ;
replay non ». Supprimer l'effet serait une **troisième** divergence d'avec son MVP — à
déclarer comme telle, jamais à présenter comme l'option conservatrice.

Doctrine de **fraîcheur** `d04dc588` intégrée telle quelle : la fraîcheur dérive de
l'âge du dernier checkpoint ; la dérive du focus est exposée séparément et n'est
**jamais** cause de péremption. **Deux divergences d'avec le MVP du ticket, pas une
seule** — la version antérieure de ce paragraphe ne déclarait que la première, et
affirmait reprendre la doctrine « telle quelle » :
- **Stockage** : le ticket recommande un snapshot sur `brain_sessions` + CAS
  `expected_checkpoint_revision` + replay exact sans nouveau timestamp ; ici,
  append-only + `(session_id, seq)`. Motif : l'append-only garde l'histoire des notes
  (c'est du jugement, grille N7), et les propriétés P0 du ticket (replay exact sans
  double effet, conflit non destructif) sont réobtenues par la clé au lieu du CAS.
- **Forme du payload** (divergence non déclarée jusqu'ici, relue dans le ticket) : son
  contrat est `brain_session_checkpoint(session_id, expected_client_key,
  expected_checkpoint_revision, progress, blocker|null, next_step)`, avec une réponse
  bornée distinguant *activity, milestone, blockage, freshness, focus_context*, et le
  critère explicite « **un appel** ». La proposition `kind ∈ {progress, blocker,
  next_step, handoff}` + `note` unique transforme trois champs sémantiques **publiés
  ensemble** en trois natures **mutuellement exclusives** : publier progrès + blocage +
  prochaine étape demanderait trois appels, trois `seq`, trois rafraîchissements de
  heartbeat, et ferait sauter le critère « un appel ». Ce n'est pas un détail de
  sérialisation, c'est le contrat que l'audit du ticket exige de spécifier avant tout
  code (« le plus petit lot admissible reste documentaire : 1. spec checkpoint
  séparée »). **Ni l'ADR ni le PLAN ne livrent cette spec** : elle est ajoutée au
  contenu de la Phase 0, et Q3(c) porte désormais les deux divergences.

Gated sur l'approbation produit explicite que ce ticket exige (question ouverte n° 3).
La livraison du tool porte le covenant à **huit** commandes : phrase-covenant dans la
8e docstring, énumération CLAUDE.md et test d'ancrage Phase 0 étendus dans le même
commit.

**D5 — L'observation d'activité, fail-closed et soumise (ferme le faux-mort de B2).**
Le middleware gagne un émetteur derrière `BRAIN_SESSION_OBSERVED_ACTIVITY_ENABLED=false` :
sur l'appel outermost d'un tool portant un `project_key` résoluble et un acteur
normalisé, **un seul UPDATE** pose `last_observed_at = NOW()` sur la session `open` de
`(project_key, started_by_actor)` — **seulement si exactement une** matche (sous-requête
scalaire de comptage dans le même statement, spécifiée dans le PLAN — le panel a exigé
cette spécification). Zéro ou plusieurs matches ⇒ aucune écriture, compteur
`observed_activity_skipped{reason}`.
**Ce statement est un chemin chaud et son coût n'est pas mesuré** : il filtre sur
`(status, project_key, started_by_actor)` deux fois — une pour l'UPDATE, une pour le
comptage corrélé — à chaque appel outermost, sur une colonne créée **sans index** et que
les trois index existants ne couvrent pas (voir D1, mesure du 2026-08-19). La décision
d'index est instruite en D1 et **mesurée en Phase 0**, avec sa conséquence sur
`expected_session_indexes` (§5.2).

**Correction de dimensionnement — le middleware ne voit PAS le projet.** Une version
antérieure de ce composant (et de D8) écrivait « le middleware voit déjà les deux ».
C'est vrai de l'acteur, faux du projet. `src/brain_v42/mcp/provenance_middleware.py:74-96`
est intégral et ne lit que des **en-têtes** : `get_http_headers(include={'mcp-session-id'})`,
`normalize_agent(x-brain-agent)`, `normalize_session(x-brain-session)`,
`normalize_transport(...)`, trois ContextVars, la garde de ré-entrance, `call_next`. Il
n'inspecte jamais `context.message.arguments` (`grep -rn '\.arguments' src/brain_v42/mcp/`
ne rend que `dream_capabilities.py:250,258`). Le seul code du dépôt qui résout un projet
depuis les arguments d'un tool est la couche capability dream
(`services/dream_project_scope.py`), et elle le fait par une **table de politiques PAR
TOOL** (`PROJECT_TOOL_POLICIES:83-120` : `project_key`/`project_keys`/`owner_project_key`,
`inject_project_key`, références typées à résoudre en base). Résoudre un projet est donc
un travail par tool, pas une donnée disponible. **Conséquence sur le périmètre** : D5 et
D8 gagnent chacun une brique de livraison explicite — un **résolveur de projet par tool**,
qui doit soit réutiliser `PROJECT_TOOL_POLICIES` (une seule table, doctrine de
consolidation), soit déclarer pourquoi il en pose une seconde ; les tools hors table ne
sont **pas** observés et comptent `skipped{no_project}`. Sans cette brique, l'émetteur
n'a aucun `:pk` à poser dans son UPDATE, et la Phase 1 ne compilerait pas. L'émetteur est enveloppé sur le modèle prouvé du
client-activity (ticket `1c40c36a`) : son échec ne casse jamais l'appel observé, et un
**test de panne injectée** le prouve (greffe A, critère de sortie explicite).
**Frontière covenant assumée et soumise** : je soutiens que c'est une observation
(comme `access_log.actor`), pas un heartbeat — mais la question est posée à l'opérateur
(n° 1) et le killswitch reste fermé tant qu'elle n'est pas tranchée.

**D6 — Le sweep aux trois signaux, toujours UN statement (ferme B2 en armement, B1 en régime).**
Le prédicat du sweep 7 j devient
`GREATEST(last_heartbeat_at, COALESCE(last_observed_at, last_heartbeat_at)) < cutoff`,
toujours réévalué sous verrou de ligne dans l'unique `UPDATE … RETURNING` (invariant
gravé du faux-mort du 2026-08-06). Élégance structurelle issue de la synthèse : le
checkpoint (D4) rafraîchit `last_heartbeat_at`, donc **les trois signaux — heartbeat
explicite, checkpoint, observation — tiennent dans deux colonnes et un statement**.
**Mais la règle « les trois signaux, pas un seul » n'est PAS une propriété intrinsèque
du prédicat** : elle dépend de deux armements indépendamment refusables. Si Q1=non
(observation jamais armée) et/ou Q3=non (pas de checkpoint), `GREATEST(...)` dégénère
en heartbeat seul — précisément le « armer le sweep tel quel » que le §3.3 écarte.
D'où la précondition dure, gravée dans le PLAN (4.3) : **l'armement WET exige Q1=oui
avec 4.1 armé et soaké, ET Q3=oui avec le checkpoint livré ; à défaut, armer le WET
redevient une décision opérateur nouvelle, prise en nommant explicitement le mode
dégradé heartbeat-seul** — jamais un enchaînement par défaut. Le DRY, lui, est déjà
armé (décision opérateur du 2026-08-18, sur le prédicat heartbeat-seul actuel —
mesuré, §1.1). Le changement de prédicat va dans le seul sens sûr : il ne peut
qu'abandonner *moins* de sessions, jamais plus.
**Résidu assumé et borné** (faiblesse n° 1 relevée par le juge technique sur B) : une
session oubliée sous un acteur encore actif sur le projet devient durablement vivante
sous observation. Trois bornes : (1) la règle « exactement un » — dès qu'une nouvelle
session du même porteur ouvre sur le projet, l'ancienne cesse d'être observée et
redevient balayable ; **symétriquement, la nouvelle session — vivante, activement
travaillée — cesse aussi d'être observée** (count=2) : sous ambiguïté, AUCUNE session
du porteur n'est observée, et une session vivante redevient balayable à 7 j si son
porteur ne checkpointe ni ne heartbeate ; même exposition pour les sessions stdio sans
header (`skipped{no_actor}`, contrainte B8). Le faux-mort ne devient donc une
impossibilité de prédicat que pour les sessions OBSERVÉES — le résidu est nommé et
mesuré, pas nié (voir §4 et PLAN 4.1) ; (2) `list` expose `observed_only`
(observation récente mais ni heartbeat ni checkpoint depuis 7 j) pour le triage
humain ; (3) le compte de sessions `observed_only` ET le compte de sessions ouvertes
sous ambiguïté candidates au sweep sont des mesures de soak publiées AVANT tout
armement WET.
**Ordre de grandeur déjà mesuré, et qu'aucun des trois documents ne citait** (2026-08-19) :
le ticket `7ffe0e8a` donne, au 2026-08-16, « Simultanées : `auto-discord` 6,
`red-arena` 3, `claude-dev-pc`/`red-lab` 2 » — une mesure **partielle** de la population
« ≥2 sessions ouvertes ». Partielle parce qu'elle compte par projet et non par couple
`(projet, acteur)` : c'est un **plafond** de l'ambiguïté au sens de N2, et il ne pourra
être raffiné qu'après M-A, `started_by_actor` n'existant pas encore. Re-mesuré le
2026-08-19 : `auto-discord` 8, `brain-v42` 4, `red-arena` 4, quatre projets à 2 — soit
**24 des 29 sessions `open`** dans un projet qui en porte au moins deux. La règle
« exactement un » ne laisse donc pas de côté un cas de bord ; au plafond, elle laisse de
côté la majorité du parc, et c'est bien pour cela que le résidu est le critère de 4.1 et
non l'impossibilité de prédicat. Chiffres datés et périssables — les rejouer.

**D7 — La mémoire du focus (ferme B6 en récupérabilité ; la prévention reste une question).**
**Deuxième correction au même endroit — la première réparation avait remplacé une
prémisse fausse par une autre.** Ce document a d'abord affirmé « deux seuls sites
d'écriture » ; la passe suivante a corrigé en « six sites, dont un seul bumpe la
révision, l'upsert écrasant le focus sans bump ». **Ce second énoncé est faux aussi, et
la base le prouve.**

*Ce qui existe déjà, mesuré le 2026-08-18 en production (head `045`, lecture seule).*
La migration **032** — que le DOSSIER cite correctement et que l'ADR avait perdue — crée
`increment_project_focus_revision()` (`alembic/versions/032_brain_sessions.py:19-34`) :
« `IF NEW.current_focus IS DISTINCT FROM OLD.current_focus THEN NEW.focus_revision :=
OLD.focus_revision + 1` », et le trigger
`project_contexts_focus_revision_trigger BEFORE UPDATE OF current_focus ON
project_contexts FOR EACH ROW`. Il est toujours en place (`pg_get_triggerdef` relu ce
jour). **Aucun écrivain ne peut donc changer le texte du focus sans bump** : un
`INSERT … ON CONFLICT DO UPDATE` déclenche les triggers BEFORE UPDATE, et la branche
ON CONFLICT de `pg_project_context.get_or_create` met bien `current_focus` dans son SET.
Contrôle croisé qui rend la chose visible : `grep -c focus_revision
src/brain_v42/repositories/pg_project_context.py` = **0**, idem sur
`scripts/scrub_xml_tool_call_leak.py` — quatre des six sites (`create`, `update`,
`get_or_create`, `update_focus`) plus le scrub ne nomment jamais la colonne. **Précision
de la passe du 2026-08-19** : ceux qui écrivent par UPDATE obtiennent bien le bump ;
`create`, et la branche INSERT de `get_or_create`, écrivent par INSERT — le trigger est
`BEFORE UPDATE`, il ne s'y déclenche pas, et la ligne naît simplement à la valeur par
défaut de la colonne (`focus_revision = 0`, mesuré). Il n'y a rien à bumper à la
naissance ; il y a, en revanche, un focus écrit sans trace (voir M-D).

*Les six sites, exactement.* Docstring de `src/brain_v42/db/focus_stamp.py` (« six call
sites across three modules ») : le CAS `applied` de `end` (`pg_brain_session.py:713-714`,
qui pose `focus_revision=expected_revision + 1` **explicitement** — le CHECK 037 l'exige,
« `applied` ⇒ `focus_revision_at_end = end_expected_focus_revision + 1` »), `brain_update_project_focus`
(`roadmap_service.py`, qui pose `focus_revision + 1` explicitement lui aussi),
`pg_project_context.update`, `update_focus`, `create`, et le tool MCP vivant
`brain_set_project_context` (upsert). S'y ajoute un écrivain hors MCP,
`scripts/scrub_xml_tool_call_leak.py` (`_PROJECT_CONTEXT_COLS = ("current_focus",)`) :
**sept écrivains au total**, six + le scrub.

*Troisième correction au même endroit, 2026-08-19 — la deuxième réparation avait laissé
deux affirmations fausses derrière elle.* Elle écrivait « deux sites bumpent
explicitement ; les cinq autres reçoivent le bump du trigger ; et
`brain_update_project_focus` est **le seul** à bumper même quand le texte NE change
PAS ». Relu ligne à ligne :
- **Les DEUX sites explicites bumpent sur texte inchangé**, pas un seul.
  `_apply_focus_if_current` (`pg_brain_session.py:713-714`) pose
  `focus_revision=expected_revision + 1` **sans comparer le texte** — et le CHECK 037
  l'exige (`applied` ⇒ `focus_revision_at_end = end_expected_focus_revision + 1`), y
  compris quand la session referme sur la même prose. Le commentaire du code le nomme :
  « Re-posting the previous prose verbatim is the copy-forward this column exists to
  expose ». `roadmap_service.py` fait de même pour consommer le jeton CAS d'un lot
  blockers-only. Deux écrivains, pas un — et c'est le régime **normal** d'une fin de
  session, pas un cas de bord.
- **Le trigger ne voit que les UPDATE.** `create` et la branche INSERT de
  `get_or_create` (`pg_project_context.py:51-71` et `:273-275`) persistent un
  `current_focus` à la naissance de la ligne : aucun trigger BEFORE UPDATE ne s'y
  déclenche, la ligne naît à `focus_revision = 0` (défaut de colonne, mesuré) et
  `focus_updated_at` y est posé en Python, pas par `focus_stamp`. Conséquence pour M-D,
  développée plus bas : la révision 0 d'un contexte est un focus écrit que **rien en
  base** ne peut obliger à s'historiser.
L'énoncé exact est donc : **deux sites posent la révision eux-mêmes, sur texte changé
comme inchangé ; quatre reçoivent le bump du trigger quand le texte change sous un
UPDATE ; et les deux chemins d'INSERT n'ont ni trigger ni révision à incrémenter.**

*Ce que l'upsert fait vraiment.* Sa branche ON CONFLICT réécrit `current_focus` — **y
compris à NULL quand l'argument est omis**, vérifié — **sans CAS** : c'est un vrai canal
d'écrasement, et c'est le fond de B6. Mais il **bumpe** (trigger 032) et il **date**
(`focus_updated_at = focus_stamp(excluded.current_focus)`, sous `IS DISTINCT FROM`, donc
un focus qui part vers NULL compte). Il n'est pas muet ; il est simplement **non
récupérable**, faute d'historique. C'est cela que M-D doit réparer, et rien d'autre.

*La preuve chiffrée invoquée ne prouvait pas ce qu'on lui faisait dire.* Une version
antérieure écrivait « ce chemin mord déjà : 10 des 59 `project_contexts` ont
`current_focus IS NULL` ». Le nombre est exact (re-mesuré ce jour : `10/59`, head `045`),
la conclusion ne l'est pas. Les **dix** lignes sont à `focus_revision = 0` **et**
`focus_updated_at IS NULL` (`perso`, `red-backup`, `red-cli`, `red-shrik:agent`,
`red-daemon`, `red-llm`, `red-tsdb`, `red-lab:developer{,-gemini,-opus}`). Révision 0 +
jamais datée = focus **jamais écrit**, pas focus effacé — et un écrasement par l'upsert
depuis la 040 aurait daté la colonne. **Zéro contexte de production porte la signature
d'un effacement** (NULL avec révision > 0). Le canal existe dans le code ; la production
ne montre pas qu'il ait mordu. Ce document n'a donc **aucune preuve mesurée** à verser
au dossier de Q13, et le dit.

Le design couvre **tous** les écrivains, avec la même doctrine de consolidation
que le prédicat colon (une seule implémentation) :
- **Un chemin d'écriture applicatif unique** (fonction partagée) par lequel passent
  les six sites et le scrub : il insère la ligne d'historique **dans la même
  transaction** que l'écriture du focus. Il lit la révision **après** l'écriture
  (`RETURNING focus_revision`) et historise sur cette valeur — jamais sur une révision
  calculée à l'avance. **Motif corrigé le 2026-08-19** : une version antérieure
  justifiait ce point par « il n'a pas à bumper, la 032 le fait déjà, et un second
  incrément applicatif poserait `OLD+2` ». Le `OLD+2` n'existe pas : le trigger
  **assigne** (`NEW.focus_revision := OLD.focus_revision + 1`), il n'ajoute pas — la
  valeur posée par le statement est écrasée, pas cumulée. **La preuve est la source
  plpgsql, et elle seule** : `alembic/versions/032_brain_sessions.py:19-34` écrit
  `NEW.focus_revision := OLD.focus_revision + 1` — une affectation, pas un `+= 1` sur la
  valeur du statement. *Corroboration retirée le 2026-08-19* : une version antérieure
  ajoutait « et les révisions avancent d'un cran (CAS 209→210 du dossier), pas de deux ».
  Ce CAS-là est un `brain_session_end` (DOSSIER §B6 : deux sessions parallèles sur le
  snapshot rev 209), pas une écriture de `roadmap_service`, et rien n'établit que le
  TEXTE du focus ait changé — or c'est exactement la condition qui fait parler le
  trigger. L'exemple ne pouvait donc pas départager les deux hypothèses qu'il était censé
  départager. La conséquence pratique est l'inverse de ce que le motif faux
  suggérait : **les deux bumps explicites doivent RESTER**. Les retirer casserait
  `end` — le trigger reste muet sur un texte inchangé, et le CHECK 037 exige quand même
  `expected + 1` — et casserait le jeton CAS d'un lot blockers-only. Le `RETURNING`
  reste la bonne règle parce qu'il est vrai dans les deux régimes, pas parce qu'un
  double incrément menacerait.
- Table `project_focus_history` append-only **garantie par trigger** (greffe A sur la
  table de B) : PK `(project_key, focus_revision)` — la monotonie du CAS généralisé la
  rend naturelle —, **`focus TEXT NULL`** (un focus effacé est précisément l'écrasement
  destructeur que l'audit doit savoir enregistrer), `actor VARCHAR(64)`, `source ∈
  {session_end, focus_tool, context_upsert, generic_update, maintenance_scrub,
  migration_seed}` — l'enum couvre les écrivains réels, `ON CONFLICT DO NOTHING` pour
  l'idempotence des replays. **Réserve de clé, à instruire dans M-D — re-dimensionnée le
  2026-08-19** : la version antérieure n'attribuait ce bruit qu'aux lots blockers-only de
  `brain_update_project_focus`. **`brain_session_end` fait de même, et c'est son régime
  normal** — le CAS pose `expected + 1` sans comparer le texte, donc toute fin de session
  qui recopie la prose précédente ajoute une ligne de focus identique à la révision
  suivante. La PK reste unique (pas de collision), mais le volume attendu de doublons de
  contenu est celui des fins de session. Le rendre lisible dans le tool de lecture
  (marquer « focus inchangé ») plutôt que le filtrer, et en tenir compte au
  dimensionnement.
- **Le garde en base proposé par la version antérieure est RETIRÉ.** Il devait « refuser
  tout UPDATE où `current_focus IS DISTINCT FROM` l'ancien sans `focus_revision = old + 1` »
  — c'est mot pour mot ce que la 032 fait déjà, en posant la valeur au lieu de la
  refuser. Pire, il cohabiterait mal : PostgreSQL déclenche les triggers BEFORE ROW dans
  l'**ordre alphabétique** de leur nom. Nommé avant
  `project_contexts_focus_revision_trigger` (p. ex. `…_focus_history_guard`), il verrait
  `NEW.focus_revision` non encore incrémenté et **rejetterait toute écriture de focus
  des quatre écrivains qui écrivent par UPDATE sans poser la révision eux-mêmes**
  (`update`, `update_focus`, l'upsert, le scrub — `create` écrit par INSERT et échappe
  aussi bien au garde qu'au trigger) — `brain_set_project_context`
  fail-closed en production à chaque changement de focus. Nommé après, il est
  trivialement satisfait : code mort. Dans les deux cas il ne protège de rien.
  **Ce qui reste utile en base**, et qui est le vrai livrable de M-D : un **constraint
  trigger différé** (`AFTER UPDATE OF current_focus … DEFERRABLE INITIALLY DEFERRED`)
  qui, en fin de transaction, exige la ligne d'historique à `NEW.focus_revision`. Lui
  n'a pas de jumeau, ne dépend d'aucun ordre alphabétique, et attrape l'écrivain qui
  contourne le chemin partagé. Il doit être **scopé sur `UPDATE OF current_focus`** :
  sans cette clause il se déclencherait sur tout UPDATE de `project_contexts`, y compris
  celui du plan-index repair (`plan_index_repair_store.py:294-308`, qui n'écrit que
  `plan_scan_paths`/`updated_at`), et le ferait échouer.
  **Trou nommé, pas refermé par ce trigger — découvert le 2026-08-19** : `AFTER UPDATE`
  ne voit **pas** les INSERT. `pg_project_context.create` et la branche INSERT de
  `get_or_create` écrivent un `current_focus` à la naissance de la ligne (révision 0,
  défaut de colonne). Pour tout `project_context` créé **après** M-D, la révision 0 est
  donc un focus persisté qu'aucune garde en base n'oblige à s'historiser — le seed de la
  migration, lui, ne couvre que les 59 contextes existant à l'upgrade. Trois voies, à
  trancher à l'écriture de M-D : (a) étendre le constraint trigger à
  `AFTER INSERT OR UPDATE OF current_focus` — la voie qui tient l'invariant N1 en base,
  au prix d'une ligne d'historique obligatoire à chaque création de projet (y compris
  quand le focus naît NULL) ; (b) le laisser sur UPDATE et faire porter la révision 0
  par le seul chemin applicatif partagé, en documentant que la garde dure ne commence
  qu'à la révision 1 ; (c) refuser à `create`/`get_or_create` d'écrire un focus non NULL
  à la naissance. **Aucune n'est gratuite, et ne rien décider revient à choisir (b) sans
  le dire** — c'est-à-dire à livrer un invariant N1 qui est faux pour tout projet neuf.
- **Changement de comportement induit sur `brain_set_project_context`, soumis** : son
  écrasement du focus devient récupérable et attribué ; et il est proposé que l'argument
  `current_focus` **omis cesse d'effacer** le focus (distinguer « omis » d'« effacement
  explicite ») — question ouverte n° 13, veto sans coût avant M-D. À trancher **sur le
  raisonnement, pas sur un chiffre** : la production n'en montre aucune victime.
La migration **seed** une ligne par `project_context` avec le focus courant — **NULL
compris** (`source='migration_seed'`) : l'ancrage couvre les 59 contextes mesurés, pas
seulement les 49 à focus non nul, et le seed ne peut pas avorter sur une contrainte
NOT NULL qui n'existe plus. Échec d'insert = échec de l'écriture de focus entière :
fail-closed assumé (« un audit qui peut se taire ne prouve rien »). Tool lecture seule
`brain_focus_history`. Le résultat de `end` gagne `focus_diff` (caractères
ajoutés/retirés vs focus de base — greffe C) : la visibilité d'abord ; le garde dur de
contenu (seuil de rétrécissement) reste une question ouverte (n° 7) car son seuil
serait arbitraire (deux juges l'ont disqualifié tel quel).

**D8 — La capture préparée, signée par l'opérateur (porte B3 au-delà des suggestions — gated covenant).**
Greffe majeure de C, corrigée par le juge technique : table
`brain_session_staged_captures` (PK `(session_id, knowledge_id)` — un brouillon est une
hypothèse, **pas** une attribution ; seule la promotion dans `brain_session_artifacts`,
PK `knowledge_id`, confère l'exclusivité), `status ∈ {staged, promoted, dismissed}`,
plafond 500/session (au-delà : on cesse d'observer et le compteur métrique
`staged_capture_skipped{overflow}` compte — emplacement du compteur spécifié, réponse à
la question du panel). Écrivain middleware derrière
`BRAIN_SESSION_STAGED_CAPTURE_ENABLED=false`, **liaison par `(project_key,
started_by_actor)` avec la même règle « exactement un », jamais par `access_log`**
(correctif du panel : `access_log` n'a pas de projet — et il est purgé, voir §3.2).
**Il dépend donc du résolveur de projet par tool spécifié en D5** : sans lui, cet
écrivain n'a pas plus de `project_key` que l'émetteur d'observation. La promotion repasse par
`_validate_captures` et n'a lieu que sur commande explicite (`capture` ou
`end(capture_staged=true)`). Avant ce mécanisme, dès la première phase : suggestions en
lecture pure — `project_uncaptured_since_start` (≤20) dans le résultat de `end`,
`project_uncaptured_since_start_count` dans `resume`, jamais bloquant. **Le nom est le
contrat** (aligné le 2026-08-19 sur le prédicat spécifié au PLAN §3.2, qui ne lie pas
par acteur) : ces artefacts sont ceux du **projet** depuis `started_at`, pas « le travail
de cette session » ; les appeler `uncaptured_candidates` laissait croire l'inverse et
ferait lire la mesure E3 comme un numérateur au lieu d'un plafond. **Horizon documenté**
(greffe A retenue par deux juges) : si le taux de promotion signée plafonne,
l'attribution à la création (`method='auto_provenance'`, même transaction que
l'artefact) est la voie structurellement la plus simple pour fermer B3 en entier —
c'est un changement de covenant plein, instruit par le dossier chiffré que ce plan
produit (candidats non capturés, brouillons `dismissed`, ratios d'ambiguïté), et remis
à l'opérateur pour trancher E3.

**D9 — La réattribution explicite et journalisée (réponse préparée à E9 — gated).**
Greffe de A retenue par les trois juges, débarrassée de son piège (FK vers une table de
spans qui n'existe pas ici ; A combinait un CHECK de propriétaire avec `ON DELETE SET
NULL`, contradiction relevée par le juge technique) : table
`brain_session_attribution_moves` — `knowledge_id`, `from_session_id`,
`to_session_id`, `reason TEXT NOT NULL`, `moved_by VARCHAR(64) NOT NULL`, `moved_at` —
sans FK piégée. L'exclusivité devient « un seul propriétaire courant », l'histoire est
intégralement journalisée. **Livrée seulement si l'opérateur tranche la question n° 8
en faveur du droit de réattribution** ; sinon l'orphelinage reste le prix de la preuve.

**D10 — Le système projets : documenter, ancrer, prédicat avant colonne (ferme B12, protège B10, prépare B11).**
Zéro changement au cœur projets dans ce plan : format, registre 033, immuabilité de la
clé, canonicalisation — intacts (B10 est un acquis anti-drift). Livrés : la doc de bout
en bout `docs/PROJECTS_SYSTEM.md` organisée par la grille fait/jugement (greffe A via
le juge simplicité), les tests d'ancrage, et **UN** prédicat sous-arbre partagé (D3).
Doctrine actée pour E5 (greffe du juge simplicité) : **le prédicat partagé pour toutes
les lectures AVANT toute colonne `parent_key`** — la colonne + CHECK de préfixe conçus
par la proposition A restent l'option documentée si l'opérateur tranche E5 vers une
vraie hiérarchie ; elle ne vient, éventuellement, qu'après la preuve d'usage du
prédicat. Un paramètre de lecture `include_descendants` (briefing, search scopé) est
préparé derrière flag fermé (`BRAIN_READ_INCLUDE_DESCENDANTS_ENABLED`, ajouté au
récapitulatif killswitches du PLAN §8bis — il en manquait) comme brique de fermeture
de B11 sans gonfler le pool dream. **Interaction avec le scope capability dream, ARMÉ
en production depuis le 2026-08-10, spécifiée** : sous un bearer `(projet, phase)`,
`brain_service` force `project_key = scope.project_key` en **égalité stricte**
(vérifié `services/brain_service.py` ; propriété mesurée au cutover — 751/2760
learnings visibles sous scope `red`). Honorer `include_descendants` sous scope ferait
lire `pk:*` à un bearer scopé sur `pk` — l'élargissement d'un périmètre de sécurité
armé. Règle du design : **sous scope dream, le paramètre est REFUSÉ fail-closed**
(erreur explicite, jamais ignoré en silence) ; élargir un bearer aux sous-partitions
serait une décision de sécurité opérateur distincte, hors de ce plan. L'armement du
flag et la consolidation de `red-shrik:agent` sont des décisions opérateur
(question n° 4 — et elle seule : la n° 5 est le sweep, un renvoi antérieur vers elle
était erroné).

**D11 — Deux natures de session (né du cadrage du 2026-08-19, Q12 = (a) ; ferme B1 et
le faux-vivant de B2 à la racine, amende C1 et C7).**
Ce composant n'est **pas** issu du panel : il est la traduction de la réponse opérateur à
Q12, et c'est le seul du document dans ce cas. Il est donc le moins instruit des onze —
le lire comme une cible acquise, pas comme un design fini.

- **Session AUTO-OUVERTE sur la clé `(projet, connexion)`**, nature `agent` par défaut
  (§0bis.1). Un geste explicite unique et **rétroactif**, le *claim*, la promeut en
  `operator`. La clé de connexion (`Mcp-Session-Id`) est le seul identifiant de la chaîne
  que le client **ne déclare pas** — elle est frappée par le serveur (§0bis.2), ce qui
  place cette liaison au-dessus du régime « déclaré, pas prouvé » qui gouverne le reste.
- **Les subagents HÉRITENT de la session de leur porteur** (réponse à Q9), sans aucun
  tag : ils partagent sa connexion. Ce n'est pas un arbitrage de confort mais une
  contrainte mesurée — aucun des trois en-têtes ne distingue un subagent de son porteur,
  et aucun ne le fera, la configuration des en-têtes étant par serveur MCP (§0bis.2).
- **Nature `operator`** : rien ne change. Les sept commandes restent explicites, le
  rituel de fin reste le seul moment où du jugement non dérivable s'écrit,
  `last_heartbeat_at` reste la seule trace de présence explicite.
- **Nature `agent`** : pas de rituel. Pas de `heartbeat` — la vivacité vient de
  `last_observed_at` (D5), qui devient portant pour elle (§0.2). Auto-fermeture sur
  inactivité observée (**4 h signées** comme seuil d'éligibilité au sweep nocturne, propre
  réglage, §0bis.3 amendé par §0ter.5), dans le **nouvel état
  terminal de M-G** (Q15 = (3), §0.4). Le ledger d'attribution reste exclusif et
  immuable : c'est le point entier de la nature agent sous l'axe « traçabilité du savoir ».
- **Une session `operator` n'est JAMAIS fermée par le timeout d'inactivité** (§0bis.3).
  C'est la garantie centrale demandée par l'opérateur, et elle tient par la NATURE, pas
  par la valeur du seuil. Seul le sweep 7 j existant peut la prendre.
- **Ce qui n'est pas tranché et bloque l'écriture de M-G** : le nom de l'état ; le seuil
  et le déclencheur de l'auto-fermeture (inactivité observée ? sweep à seuil court par
  nature ?) ; si une session agent applique un `next_focus` (le §0.4 argumente que non) ;
  et le comportement d'une session agent dont le ledger est vide — a-t-elle besoin d'un
  équivalent de `nothing_to_capture_reason`, alors qu'aucun humain n'est là pour
  l'écrire ?
- **Ce que D11 ne résout pas** : Q9 (subagents — session propre ou héritage du porteur)
  devient plus tranchante, pas moins. Sous D11, « session propre » signifie « session de
  nature agent », donc quasi gratuite ; l'arbitrage se déplace du coût vers le bruit.

### Invariants préservés et ajoutés

**Dix des douze invariants du dossier sont conservés au caractère près ; deux sont
amendés par le cadrage du 2026-08-19** — une version antérieure de ce paragraphe écrivait
« les douze », et c'était vrai avant le cadrage. Conservés : fail-closed des fins, garde
d'identité, CAS non bloquant, exclusivité PK, idempotence des replays,
canonicalisation/immuabilité, focus non dérivable, pin `_REQUIRED_ALEMBIC_HEAD`
(voir §5), FK RESTRICT, sweep UN statement. **Amendés, explicitement et par l'opérateur** :

- **C1 (covenant)** — les sept commandes restent explicites **pour la nature
  `operator`** ; la nature `agent` s'ouvre déclarée et se ferme seule (D11, Q12 = (a)).
  Le sweep 7 j reste la seule autre exception serveur.
- **C7 (états non représentables impossibles)** — le CHECK terminal 037 gagne une
  branche via M-G (Q15 = (3), §0.4). La propriété elle-même est **préservée** : ce n'est
  pas une dérogation, c'est un état de plus, à rendre non contournable par le même double
  rail. Le double rail Pydantic bouge avec le CHECK, jamais après.

S'y ajoutent :
- **N1** : toute écriture persistée de `current_focus` — par n'importe lequel de ses
  **six sites d'écriture PLUS le script de maintenance**, soit sept écrivains (une
  version antérieure comptait le scrub *parmi* les six, d'où un « 7e écrivain de
  demain » qui est en réalité le huitième) — laisse sa ligne d'historique dans la même
  transaction, sous constraint trigger différé en base. Le **bump** de `focus_revision`,
  lui, n'est pas un invariant *ajouté* : il est acquis depuis la 032 et vérifié en
  production ; N1 ne le rétablit pas, il l'assume et lui adosse la récupérabilité — un
  focus écrasé (ou effacé) redevient lisible avec sa provenance, et aucun écrivain futur
  ne peut historiser en silence. **Portée exacte de la garantie en base, 2026-08-19** :
  le constraint trigger est `AFTER UPDATE`, donc N1 n'est tenu **en base** que pour les
  écritures par UPDATE. Les deux chemins d'INSERT (`create`, branche INSERT de
  `get_or_create`) écrivent la révision 0 hors de sa portée ; tant que la voie (a) de
  D7 n'est pas retenue, N1 y repose sur le seul chemin applicatif partagé — une
  convention, exactement ce que N5 refuse ailleurs. Le dire est la condition pour ne
  pas croire l'invariant plus fort qu'il n'est.
- **N2** : une observation ou une liaison ambiguë (0 ou ≥2 sessions candidates) n'écrit
  **rien** et incrémente un compteur visible — le serveur ne devine jamais.
- **N3** : un rejet de capture nomme chaque id rejeté **et sa raison**, plus le
  sous-ensemble capturable.
- **N4** : un brouillon (`staged`) n'est jamais une attribution ; la promotion repasse
  par la validation et n'a lieu que sur commande explicite ; un rejet est journalisé
  (`dismissed`), pas supprimé.
- **N5** : l'append-only (checkpoints, historique focus) est garanti par trigger en
  base, pas par absence de chemin de code.
- **N6** : tout comportement runtime nouveau naît derrière son killswitch, livré fermé,
  avec un test prouvant le fermé-par-défaut, et son état production est **mesuré**
  (drop-in inspecté, processus inspecté — leçon du client-activity « ARMED »).
- **N7** : chaque objet nouveau est un fait OU un jugement, jamais les deux.

---

## 3. Alternatives écartées

### 3.1 Proposition A — « refonte assumée » (écartée comme base ; quatre greffes reprises)

Scission de la session en trois objets (`activity_spans`, `attributions`,
`engagements`), focus en journal + rendu matérialisé, hiérarchie projets en base
(`parent_key` + CHECK), 4-5 nouvelles heads Alembic. Écartée par le panel (scores
58/71/56) pour des raisons **vérifiées** :
- **Le plan tel qu'écrit ne s'applique pas** : la migration des attributions déclare un
  FK vers `activity_spans` créée une phase plus tard ; le CHECK « un propriétaire
  obligatoire » rend le critère de sortie de sa Phase 2 inatteignable pour tout agent
  sans session ouverte ; et la combinaison CHECK propriétaire × `ON DELETE SET NULL`
  sur la purge des spans est le piège classique qui bloque toute purge ou viole le
  CHECK (juge technique).
- **Exposition maximale au couloir du pin** : 4-5 heads couplées à
  `_REQUIRED_ALEMBIC_HEAD` là où le ticket `c60d023d` rappelle qu'un head figé a déjà
  produit quatre incidents, et une 046 en projet sur le même couloir (deux juges — le
  panel disait « déjà en file » ; vérifié : aucune 046 n'existe dans
  `alembic/versions/`, voir §5.5).
- **Churn sans douleur payeuse** : renommage `brain_sessions`→`engagements`, heartbeat
  transformé en no-op silencieux (mentir à un client qui invoque une commande explicite
  du covenant est une rupture de contrat, pas une dépréciation), tool de réattribution
  livré alors que la question E9 est explicitement non tranchée (juges technique et
  simplicité).
- **Double représentation du focus** (journal + rendu matérialisé + kind `compact` qui
  réintroduit la réécriture que le journal prétend interdire) : la garantie vendue est
  plus faible que la machinerie payée ; l'audit append-only donne la récupérabilité
  sans dualité (juge simplicité).
- **Deux écritures chaudes par appel de tool** avec un précédent de perte mesuré et non
  fermé (burst loss du client-activity au-delà de 8 appels concurrents) : la refonte
  réintroduisait par le canal d'observation le mode de panne qu'elle voulait tuer.

**Repris de A** (avec l'aval explicite des juges) : la grille fait/jugement (N7, et
grille de la doc Phase 0) ; le prédicat sous-arbre implémenté UNE fois (D3/D10) ; les
`attribution_moves` sans le piège FK (D9) ; le test de panne injectée comme critère de
sortie (D5) ; l'attribution à la création comme **horizon** E3 instruit par les mesures
de ce plan, pas comme livraison (D8) ; le design `parent_key` + CHECK comme option
documentée pour E5, prédicat d'abord (D10).

### 3.2 Proposition C — « opérateur d'abord » (écartée comme base ; cinq greffes reprises)

Trois couches observer/préparer/signer, checkpoint, staged captures, présence calculée
à la lecture, garde de rétrécissement du focus. Écartée par le panel (scores 81/84/73)
pour :
- **Son défaut central est une hypothèse de schéma fausse, vérifiée** : la « présence
  calculée à la lecture depuis access_log (activité du couple (projet, acteur)) » ne
  marche pas telle quelle — `access_log` n'a **pas** de colonne projet ; il faudrait
  joindre `entity_id` vers six tables par `entity_type` (coûteux par appel `list`) et
  la couverture serait partielle. « Le "aucun écrivain nouveau" de sa Phase 2 repose
  sur une hypothèse de schéma non vérifiée — précisément le défaut que cette
  proposition reproche aux autres » (juge technique).
- **Et un second défaut, plus dirimant, que ce document avait raté en réadoptant le
  mécanisme** : `access_log` **n'est pas un journal, c'est un tampon vidé en continu**.
  `repositories/pg_access_log.py:38-113` agrège puis exécute
  `sa.delete(access_log).where(access_log.c.id <= max_id)` **dans la même transaction** ;
  l'appelant est `DecayFlusher`, périodique, `interval_seconds=300` par défaut
  (`config.py:379`). L'agrégation replie de surcroît l'acteur en compteurs : la colonne
  `actor` ne survit pas au flush. **Mesuré le 2026-08-18 : `select count(*) from
  access_log;` → 0 ligne**, alors que `select max(last_accessed_at), count(*) filter
  (where access_count>0) from learnings;` → `2026-08-18 20:11:06+00 | 2209` — le cycle
  écrit-agrège-purge tourne bien. Troisième limite du même ordre : les seuls écrivains
  sont des **lectures** (`search_hit`, `get_by_id`, `use`, `execute` — quatre sites),
  jamais des créations. Un « lookback de 7 j » sur cette table ne rend donc rien.
- **Garde de rétrécissement ~60 %** : heuristique de longueur sur prose libre dans un
  chemin d'erreur fail-closed, seuil arbitraire de son propre aveu — faux positifs
  garantis sur une compaction légitime (deux juges).
- **La machinerie staged livrée d'emblée** (machine d'états propre, plafond, promotion,
  downgrade fail-closed sur des brouillons — « on paie le prix de la preuve pour un
  objet qui n'en est pas une ») plus 4+ flags à gouverner, alors que la décision
  d'armement n'est pas prise (juge simplicité).
- **« Pin bumpé dans la même série de commits »** : formulation plus lâche que « le
  même commit » — fenêtre de désynchronisation tête/pin, exactement le mode de panne de
  `c60d023d` (juge technique).

**Repris de C** (avec l'aval explicite des juges) : le checkpoint complet avec effet
heartbeat (D4) ; `intent` + `open_sessions_same_carrier` (D1 et PLAN) ;
`capturable_subset` (D2) ; les staged captures **en Phase 4, gated covenant, liaison
corrigée** (D8) ; `focus_diff` (D7) ; le principe « aucun tool dont la seule raison
d'être est de faire plaisir à la machine ».

**NON repris, contrairement à ce qu'écrivait une version antérieure : la présence
calculée à la lecture.** Ce document annonçait la reprendre « dans une forme corrigée et
bornée » (opt-in, jointure entité par entité, lookback plafonné à 7 j) et en faisait
l'instrument de comparaison destiné à instruire la question n° 1 — celle qui bloque la
Phase 4.1. La correction ne portait que sur la colonne projet absente ; elle ratait que
la table est **purgée toutes les cinq minutes** et ne retient que des lectures (§3.2
ci-dessus, mesure à 0 ligne). Un instrument qui mesure structurellement zéro ne peut pas
instruire une décision : `with_observed_activity` est **retiré** de la Phase 1. Ce qui
le remplace pour instruire Q1 est nommé au PLAN Phase 0 — les compteurs de l'émetteur
en mode « calcul sans écriture », qui mesurent exactement la population que l'armement
toucherait, sans rien écrire.

### 3.3 Alternatives de fond écartées (héritées de B, confirmées par le panel)

- *Statu quo + armer sweep/auto-heartbeat tels quels* : on calibrerait un signal que le
  ticket `2bd14b24` a condamné (« un signal qui se trompe dans les deux sens est à
  remplacer »).
- *Auto-capture directe dans le ledger* : violerait l'attribution explicite et
  immuable sur la foi d'une heuristique ; le staging (D8) rend l'heuristique réversible
  et signable, et instruit la décision E3 au lieu de la prendre.
- *Capture partielle des lots mélangés* : un lot partiellement appliqué a deux
  histoires possibles — casse la sémantique de replay ; l'erreur énumérée +
  `capturable_subset` font mieux sans ambiguïté d'état.
- *Contrainte serveur sur `client_key`* : casserait tous les clients existants pour un
  problème de tri ; `intent` + filtre de liste font le triage humain.
- *Sessions implicites serveur / suppression du rituel / résumé dérivé* : **PAS
  écartées — ce sont les pistes a/b/c que l'opérateur s'est explicitement réservées**
  (réponse établie `2bd14b24` : cycle de vie « automatique, pas déclaratif » ; voir
  §1.2 et question n° 12). Aucun panel n'a mandat pour trancher ce qu'il s'est
  réservé. Ce document verse seulement deux objections techniques au dossier de cette
  décision : le résumé dérivé (piste c) recopierait de l'état mesurable dans le seul
  canal de jugement non dérivable (doctrine `FocusArg` — réserve déjà notée dans le
  ticket lui-même), et B8 prouve que l'ouverture automatique attribuerait faux en
  stdio. L'accrétion proposée ici est un transitoire compatible avec les trois
  pistes, pas un vote contre elles.
  > ✅ **Tranché le 2026-08-19 : l'opérateur a choisi la piste (a)** (§0, Q12). Les deux
  > objections ci-dessus gardent leur valeur et ne sont pas caduques : celle contre (c)
  > (recopie d'état mesurable dans le canal de jugement, C9) a **resservi telle quelle**
  > pour écarter la route (2) de Q15, qui était (c) par la porte de derrière ; celle
  > tirée de B8 s'est révélée **non bloquante pour (a)**, la nature étant *déclarée* au
  > `start` et non détectée (§0.1). Ce qui n'était pas instruit — et que le choix a
  > révélé — c'est que (a) n'a **aucun état terminal légal** dans le CHECK 037 : d'où
  > Q15 et la migration M-G (§0.4).
- *Hiérarchie en base immédiate* : prédicat partagé d'abord, colonne éventuelle après
  preuve d'usage (D10).

---

## 4. Conséquences

**Positives.**
- Chaque tranche est livrable et testable seule. Le rollback, lui, est **séquentiel,
  pas indépendant** : la chaîne Alembic est linéaire à tête unique (imposée par le
  test du pin), donc revenir sur M-A exige de downgrader d'abord M-D, M-C, M-B — dont
  les downgrades sont fail-closed dès qu'une ligne existe, et ce sont les canaries de
  sortie de phase qui créent ces lignes. Le PLAN impose donc, **à CHAQUE phase qui pose
  une tête** : canaries sur sessions jetables, purge documentée des lignes de canary, et
  downgrade à blanc prouvant la fenêtre encore ouverte avant de déclarer la phase sortie.
  **Correction : cette règle n'était écrite qu'en Phase 2** (M-B/M-C) alors que la
  Phase 1 la subit aussi — son canary obligatoire « `start` avec `intent` l'affiche dans
  `list` » écrit un `intent` non NULL, et le downgrade de M-A est fail-closed dès qu'un
  `intent` non NULL existe. Le canary de sortie refermait donc définitivement la fenêtre
  de rollback de **la tête dont M-C, M-D et tout le couloir linéaire dépendent**. La
  procédure de purge + downgrade à blanc est étendue à la Phase 1 (et à la Phase 3, dont
  le canary sur chaque écrivain écrit des lignes hors seed). Le noyau 037 et le soak du
  sweep restent valides.
- Le taux d'attribution devient pilotable (baseline Phase 0 → suggestions → staged
  gated → dossier E3) au lieu de dépendre de la vertu d'agent.
- La liveness devient honnête : trois signaux dans deux colonnes et un statement ;
  pour une session **observée**, le faux-mort devient une impossibilité de prédicat.
  **Corollaire à ne pas confondre avec une mesure de soak** : puisque c'est une
  impossibilité *de prédicat*, « zéro session observée candidate au sweep » ne peut pas
  échouer — `last_observed_at` récent implique `GREATEST(...) > cutoff` par construction,
  quels que soient les 14 jours d'observation. Une version antérieure du PLAN en faisait
  le critère de sortie **principal** de la Phase 4.1, celle qui déverrouille l'armement
  WET : c'est la même classe de défaut que le §5 du PLAN avait su nommer pour B6
  (« un critère trivialement satisfiable »). Le critère qui mesure quelque chose est
  l'autre : **le compte de sessions ouvertes sous ambiguïté ET candidates au sweep**,
  plus le ratio `skipped{ambiguous}`. Corrigé dans le PLAN 4.1.
  Résidu nommé (D6) : une session sous ambiguïté (≥2 ouvertes du même porteur — le
  régime fantôme de B1) ou stdio sans header n'est jamais observée et reste exposée au
  balayage 7 j comme aujourd'hui, checkpoint et heartbeat restant ses seules défenses.
- Les fantômes deviennent triables (`intent`, `started_by_actor`, `observed_only`,
  avertissement au `start`) puis auto-nettoyés (sweep armé par l'opérateur).
- Un focus écrasé est récupérable à une requête de distance, avec son auteur ; la
  fraîcheur cesse d'être le silence (`d04dc588` fermable).
- Les mesures produites (candidats non capturés, ambiguïtés, `dismissed`,
  `observed_only`) sont exactement le dossier dont l'opérateur a besoin pour trancher
  E1–E9 en connaissance de cause : ce plan n'usurpe aucune de ses décisions, il les
  instrumente.

**Négatives, assumées.**
- B1 n'est fermée qu'à moitié tant que les questions subagents (n° 9) et cycle de vie
  automatique (n° 12 — la réponse établie de `2bd14b24`) ne sont pas tranchées : rien
  côté serveur n'empêche un subagent d'ouvrir une session ; le sweep armé transforme
  le régime permanent en régime auto-nettoyé — c'est la moitié utile.
- B3 ne montera pas à ~100 % sans attribution à la création (horizon E3, décision de
  covenant réservée à l'opérateur).
- Deux horloges coexistent (`last_heartbeat_at`, `last_observed_at`) : dualité lisible
  mais réelle, documentée côte à côte — comme les deux seuils de staleness le sont déjà
  dans `models/brain_session.py`.
- Deux à six heads Alembic — **deux** fermes (M-A, M-D) et **quatre** conditionnelles
  (M-B suspendue à Q2 *et* Q14, M-C à Q3, M-E à Q6, M-F à Q8 ; le compte « trois/trois »
  d'une version antérieure précédait l'ouverture de Q14). Chacune est un rendez-vous
  production couplé au pin — discipline stricte requise (§5).
- Une session oubliée sous un acteur actif reste durablement vivante sous observation
  (résidu D6, borné et mesuré).

---

## 5. Migrations et pin — règle de séquencement (contrainte DURE)

`_REQUIRED_ALEMBIC_HEAD = "045"` (`src/brain_v42/maintenance/plan_index_repair_store.py:63`,
gardé par `tests/unit/test_plan_index_repair_head_pin.py`) fait fail-closed le
plan-index repair au moindre head non appliqué (ticket `c60d023d`). Règle de ce plan,
la plus stricte des trois propositions, exigée par deux juges :
1. Chaque migration est livrée dans **le même commit** que le lot ci-dessous. Deux
   versions successives de ce document ont donné une recette incomplète — « pin + test
   seul », puis « pin + quatre documents » — chacune se déclarant « le couplage
   complet ». **Elle l'est toujours moins que ce que le dépôt exige, et un inventaire
   des gardes a donc été refait en les listant une à une, grep en main** :
   - le bump du pin et de son test
     (`tests/unit/test_plan_index_repair_head_pin.py`) ;
   - **README** et **MCP_TOOLS**, qui attendent la chaîne `migration {head}`
     (`test_documentation_contract.py::test_documented_migration_head_matches_repository`) ;
   - **ARCHITECTURE**, qui n'attend **pas** cette chaîne mais
     `migrations 001–{head} defined` — tiret **cadratin** compris (même test, l.1815).
     Suivre l'ancienne recette à la lettre sur ARCHITECTURE produisait un test rouge ;
   - **CLAUDE.md**, qui est **hors du dépôt** : `git check-ignore -v CLAUDE.md` rend
     `.gitignore:74`, `git ls-files` ne le contient pas, et le test lui-même garde son
     assertion derrière `if CLAUDE:` (« CLAUDE.md is tracked only in the private
     archive », l.25-32). Il faut donc le mettre à jour — c'est le contrat de travail —
     mais **il ne peut être dans aucun commit**, et en CI la clause est muette. Le
     compter comme un « document du commit » est une erreur de catégorie ;
   - **SCHEMA.md** : compte de tables (M-C à M-F en ajoutent chacune une, « 32 tables
     public » et le compte de révisions bougent) **et** la phrase « La cible du dépôt
     est {head}. », épinglée en outre par `tests/unit/test_recovery_contract_v4.py:437-446`
     — doublon que le dépôt documente lui-même comme « facile à manquer quand on
     inventorie les gardes », ayant coûté « une passe de plus » à trois bascules
     consécutives ;
   - **`docs/OPERATIONS.md:118`** (« The repository migration target is 045. ») —
     absent de toutes les listes antérieures ;
   - `tests/unit/test_recovery_contract.py:279` : `assert script.get_heads() == ["045"]`,
     littéral **dans un test nommé pour la révision 031**. Il rougit à **chaque** bump,
     M-A comme M-F ;
   - `tests/unit/test_recovery_contract.py:292` et
     `tests/unit/test_recovery_contract_v2.py:33-39` : le `table_set` gelé est
     re-dérivé de `METADATA.tables` moins un ensemble d'exclusion codé en dur. **M-C,
     M-D, M-E et M-F ajoutent chacune une table** — quatre bumps sur six font rougir ces
     deux tests, qui ne parlent ni de pin ni de sessions ;
   - le **renommage du test-garde head-nommé** (`test_repository_head_045_is_documented_…`).
2. **L'attestation de récupération versionnée `ops/recovery/` est impactée par trois
   des six têtes — et aucune version antérieure de ce document ne prononçait le mot.**
   *(Le « trois des six » n'est vrai que si aucune tête n'ajoute d'index sur
   `brain_sessions` — voir le point (ii) ci-dessous ; et « l'attestation » est **deux**
   fichiers, voir le point (i).)*
   `ops/recovery/brain-v42-v4.sql` empreinte exactement ce que M-A, M-B et M-D changent,
   et le runbook exige d'elle « all statuses are pass » et « exactly 25 unique checks »
   (`tests/unit/test_recovery_contract_v4.py:480-486`).

   **Deux corrections de périmètre, 2026-08-19 — il y a DEUX assets v4, et QUATRE
   mécanismes de casse.**

   *(i) L'asset oublié.* `ops/recovery/` contient aussi
   **`brain-v42-v4-pgrestore.sql`**, la variante destinée à une base restaurée par
   `pg_restore` — celle des preuves isolées du runbook
   (`docs/PLAN_INDEX_REPAIR_RUNBOOK.md:62,122-123`). Ce document, le PLAN et le DOSSIER
   la nommaient **zéro fois** (`grep -c pgrestore` = 0/0/0), tous trois écrivant
   `brain-v42-v4.sql` comme si l'asset était unique. Elle est pourtant **vivante et
   testée** : `tests/integration/db/test_recovery_contract_v4_execution.py:106` la place
   dans le `parametrize` **avec** la variante live et exécute **les deux** contre une base
   réelle en transaction READ ONLY ;
   `tests/unit/test_recovery_contract_v4_pgrestore.py:29-33` impose la **parité des
   CTE** — l'écart autorisé est exactement `{observed_artifact_constraints,
   observed_session_constraints}`, et `not (_cte_names(live) - _cte_names(pgrestore))`
   interdit qu'un CTE naisse côté live sans naître côté pgrestore ;
   `tests/unit/test_recovery_contract_v4.py:273-279` exige du runbook qu'il distingue les
   deux portes. Et elle porte **les mêmes structures** que M-A/M-B/M-D cassent : mesuré le
   2026-08-19, la variante pgrestore compte 12 lignes portant
   `expected_runtime_user_triggers`, `observed_column_fingerprints`,
   `expected_artifact_constraints` ou `knowledge_sources` — 15 avec
   `expected_session_indexes`. **Conséquence** : appliquer la règle du point 1 et du
   §8 du PLAN à la lettre régénérerait **un** des **deux** assets v4, et la parité des CTE
   rougirait au premier CTE ajouté. Partout où ces documents disent « régénérer
   `ops/recovery/` », lire **les deux assets v4**. C'est la **quatrième** itération du
   même défaut dans ce dossier — un inventaire de gardes déclaré complet et incomplet ;
   le mode de panne n'est pas l'oubli, c'est la confiance dans une liste non re-grepée.

   *(ii) Le quatrième mécanisme.* Ces documents recensaient trois façons de casser
   l'attestation (empreinte de colonnes, CHECK d'artefacts, liste de triggers). Il en
   existe une **quatrième**, et elle vise `brain_sessions` :
   **`expected_session_indexes`** (`v4.sql:404-412`) fige la liste **FERMÉE** des index de
   la table — `brain_sessions_pkey`, `idx_brain_sessions_project_status_started`,
   `uq_brain_sessions_project_client`, chacun avec son md5 de définition — et
   `session_constraint_mismatches` la contrôle **deux fois** : `:665` pour les index
   attendus absents ou dont `md5(pg_get_indexdef(...))` a bougé, `:687` pour les index
   **présents et absents de la liste**. Un index ajouté sur `brain_sessions` suffit donc à
   faire `session_constraint_mismatches > 0`, attendu à 0. Doublé côté unitaire par
   `SESSION_INDEX_DEFINITION_MD5` (`tests/unit/test_recovery_contract_v3.py:164-168`) et
   `test_v3_pins_the_exact_session_index_set` (`:488`) ; les trois md5 vivent
   littéralement dans **quatre** assets (`v3`, `v3-pgrestore`, `v4`, `v4-pgrestore`).
   Ce mécanisme est **dormant** tant qu'aucune tête n'ajoute d'index — raison de plus pour
   le recenser avant d'instruire la décision d'index de D1/D5, pas après. **Et « trois des
   six têtes » cesse alors d'être vrai** : si l'index voyage dans M-A, ce sont trois têtes
   mais **deux** structures cassées pour M-A ; s'il est différé dans sa propre tête, ce
   sont **quatre têtes sur sept**, dans un couloir qui en interdit deux en vol (point 3).

   Les trois mécanismes déjà recensés, tête par tête :
   - **M-A** — `observed_column_fingerprints` calcule un md5 sur la liste ordonnée
     **complète** des colonnes de `brain_sessions` ; trois colonnes nullable de plus
     changent ce md5 ⇒ `session_column_mismatches > 0`. Le même md5 est épinglé côté
     unitaire (`test_recovery_contract_v3.py:170` :
     `COLUMN_DEFINITION_MD5["brain_sessions"] = "bf4c2a47…"`) ;
   - **M-B** — `expected_artifact_constraints` code en dur la définition du CHECK à
     **sept** valeurs ⇒ `artifact_constraint_mismatches > 0` ;
   - **M-D** — `expected_runtime_user_triggers` est une liste **fermée de treize
     triggers sur cinq tables**, dont **sept sur `project_contexts`** (relue le
     2026-08-19, `v4.sql:533-548` ; les sept sont identiques en production), et
     `runtime_trigger_mismatches` additionne un compteur `unexpected_runtime_trigger`
     sur `expected_runtime_trigger_tables`, qui contient `project_contexts` ⇒ chaque
     trigger ajouté fait échouer le contrôle.
     **Collision entre deux corrections de la passe précédente, vue le 2026-08-19.**
     Le point 4 fait naître ce trigger **désactivé** pour survivre à la fenêtre
     upgrade→redémarrage. Or la jointure des triggers attendus porte
     `AND observed_user_trigger.tgenabled = 'O'` (`v4.sql:913-918`) : un trigger
     **attendu mais désactivé** compte comme mismatch au même titre qu'un trigger
     absent. Les deux issues sont donc rouges — hors liste il est *inattendu*, dans la
     liste il est *attendu et éteint* — et **aucun ordre de régénération ne rend
     l'attestation verte tant que le trigger est volontairement éteint**. Conséquences à
     écrire dans le runbook de M-D : la régénération des assets est séquencée **avec le
     geste d'activation**, pas avec l'`alembic upgrade` ; la fenêtre désactivée est une
     fenêtre d'attestation rouge **assumée et datée** ; et le « DISABLE TRIGGER comme
     interrupteur de secours » que le PLAN §5 propose en rollback n'est pas neutre — il
     rouvre cette fenêtre rouge à chaque usage.
   Ces compteurs — **quatre** avec `session_constraint_mismatches` (point *(ii)*) — sont
   attendus **à 0**. Sans mise à jour des assets, le check
   `brain_runtime_032_036_037` passe en `fail` **dès la première migration du plan et le
   reste**. Chaque tête concernée porte donc, dans son commit, la régénération de
   l'attestation — **des DEUX assets v4** (point *(i)*) — et de ses empreintes
   unitaires. **Ce que ce plan ne peut pas décider
   seul** : l'attestation casse aussi sur les **données**, au premier canary —
   `knowledge_sources` (v4.sql:1083-1090) est l'UNION des **six** tables de capture
   (pas les tickets) et `artifact_source_matches` exige
   `source_record.project_key = session_record.project_key`. Un artefact
   `knowledge_type='ticket'` n'a donc aucune ligne source, et une capture famille
   `pk → pk:child` viole l'égalité — deux `artifact_source_mismatches` **permanents**,
   qu'aucune purge de canary ne rattrape. Il faut décider si l'attestation doit
   apprendre les tickets et le prédicat sous-arbre : **question ouverte n° 14**, à
   trancher avant M-B, pas pendant.
3. **Jamais deux têtes en vol** (non appliquées) simultanément — celles de ce plan
   entre elles, ET vis-à-vis de la 046 (point 5).
4. La production est **mesurée** (`select version_num from alembic_version`) — jamais
   recopiée — avant d'ouvrir la tranche suivante ; puis redémarrage MCP et canary.
   **Ordre imposé pour M-D, et pour elle seule** : entre `alembic upgrade` et le
   redémarrage, le processus vivant exécute encore le code pré-M-D, qui n'écrit aucune
   ligne d'historique. Le constraint trigger différé ferait donc avorter au COMMIT
   **tout `brain_session_end` en `focus_outcome=applied`** et toute écriture de focus,
   fail-closed, session laissée ouverte. M-D est donc la seule tête dont la migration
   **crée le trigger désactivé** (`ALTER TABLE … DISABLE TRIGGER`) et dont le runbook
   l'active **après** le redémarrage MCP, en un geste opérateur nommé — sinon la fenêtre
   d'indisponibilité est imposée par la règle du plan elle-même, pas par un accident.
   **Prix de cette exception, mesuré le 2026-08-19** : pendant toute la fenêtre
   désactivée, l'attestation de récupération est rouge (point 2 — elle exige
   `tgenabled = 'O'` de chaque trigger attendu). La fenêtre doit donc être **courte,
   datée et annoncée**, et la régénération des assets `ops/recovery/` posée dans le même
   geste que l'activation, pas dans celui de l'upgrade.
5. Une **046 (dimension embedding) est en projet sur ce couloir, pas encore écrite** :
   `ls alembic/versions/` s'arrête à `045_dream_run_model_width.py` (vérifié). Le ticket
   `c60d023d` la qualifie lui-même de « non urgent » et énumère un travail non planifié
   (révision terminale convergente, NO-OP DDL sur prod, passe 1 en lecture seule
   fail-closed, killswitch, reconstruction HNSW, harnais de test inexistant). Une
   version antérieure de ce document en faisait un **gate dur** — « M-A ne merge pas
   tant que la 046 n'est pas appliquée » —, ce qui mettait les six têtes du chantier en
   otage d'un ticket que son auteur déclare non urgent et non écrit. **Le gate est
   levé** : le risque qu'il invoquait (deux heads en un rendez-vous) est déjà couvert
   par la règle 3, qui vaut dans les **deux** ordres. Ce qui reste, et qui suffit : les
   têtes de ce plan sont numérotées **relativement** (M-A à M-F) ; celle des deux
   séries qui arrive la première prend le numéro suivant, et l'autre attend son
   application mesurée. La revue par head qu'exige le docstring du pin reste due dans
   les deux sens.
6. **La revue que le pin exige vaut aussi pour NOS têtes, pas seulement pour celles des
   autres.** Le message d'échec de `test_plan_index_repair_head_pin.py:45-52` est
   explicite : « Do not bump it blindly: review what {head} changes on the tables the
   repair writes (**indexed_plans, indexed_plan_chunks, project_contexts**) — new
   triggers, new constraints or new NOT NULL columns without a default would each change
   the repair's behaviour. » **M-D pose un constraint trigger sur `project_contexts`**,
   une des trois. Le docstring du pin donne le format attendu de cette revue (la 043
   « TOUCHE `indexed_plans` […] donc la revue ne pouvait pas se contenter de "elle ne
   touche à rien" ») ; le commit de M-D porte la sienne, écrite, montrant que le
   trigger est scopé `UPDATE OF current_focus` et donc inerte pour les
   `UPDATE plan_scan_paths` du repair (`plan_index_repair_store.py:294-308` et
   `:560-584`). Une version antérieure n'appliquait cette vigilance qu'à la 046.

Le répertoire des migrations est `alembic/versions/` (head versionnée : 045) — les
propositions divergeaient sur ce chemin ; il a été vérifié.

---

## 6. Questions ouvertes — à trancher par l'opérateur seul

Aucune n'est tranchée *par ce document* ; chacune bloque ou module une tranche du PLAN.

> **État après la session de cadrage du 2026-08-19 — le §0 fait foi.** Trois questions
> de cette liste sont **répondues** (Q1 par dérivation, Q10, Q12), une est **neuve**
> (Q15) et une a **changé de rang** (Q6, promue en critère de sortie de Phase 0). Les
> entrées correspondantes ci-dessous portent leur réponse en tête ; leur texte d'origine
> est conservé dessous, parce qu'il documente ce qui a été soumis. **Ne pas lire une
> entrée sans lire son encadré de réponse.**
>
> **Mise à jour après la session 2 du même jour (§0bis) : Q2, Q3, Q6, Q9 et Q14 sont
> répondues à leur tour, et le corollaire de Q1 est DISSOUS. LA PHASE 0 EST DÉBLOQUÉE.**
> Restent ouvertes, sans bloquer la Phase 0 : **Q4, Q5, Q7, Q8, Q11, Q13**.
>
> **Mise à jour après la session du 2026-08-20 (§0ter) — décision
> `c5160259-a33a-4dfc-b343-992746604b7a`.** Les trois questions que le §0bis avait ouvertes
> *en résolvant* les précédentes sont **signées** : (a) M-A et M-G en **une seule tête** ;
> (b) **pas de session automatique en stdio** ; (c) `expected_client_key` **retirée du
> chemin résolu-par-connexion**, gardée sur les cinq chemins explicites. Les **quatre
> résolutions** de `23bf6088` sont **ratifiées telles que proposées**, et le seuil des
> traçantes est **signé à 4 h** — comme seuil d'**éligibilité** au balayage nocturne, jamais
> comme délai (§0ter.5). **LA TRANCHE MINIMALE EST DÉBLOQUÉE ; `SPEC M-G` devient
> écrivable.** La liste des questions restant ouvertes ne bouge pas : **Q4, Q5, Q7, Q8,
> Q11, Q13** — aucune ne bloque.
>
> **Q5 et le seuil 4 h se répondront ENSEMBLE**, dans le même statement nocturne.

1. > ✅ **RÉPONDUE le 2026-08-19, par dérivation de Q12 = (a) — voir §0.2.** Pour la
   > nature `agent`, `last_observed_at` **est** le signal de vivacité, par construction
   > et sans covenant glissé, puisque Q12 vient de l'amender pour cette nature. Pour la
   > nature `operator`, D5 reste de l'observation pure. **Le corollaire reste OUVERT** :
   > « exactement un » vs « toutes », sur 24 sessions `open` sur 29 logées dans un projet
   > qui en porte au moins deux (mesuré le 2026-08-19).
   >
   > *Texte soumis à l'origine :*

   **Observation vs covenant (bloque l'armement Phase 4.1)** : `last_observed_at`
   écrit par le middleware est-il une *observation* acceptable (cadrage proposé), ou un
   auto-heartbeat déguisé, donc un changement de covenant ? Corollaire : si plusieurs
   sessions matchent (acteur, projet), rester à « exactement un » (proposé) ou accepter
   « toutes » ?
2. > ✅ **RÉPONDUE le 2026-08-19 (session 2) : `from_project`, la paternité.** La capture
   > répond à « qu'a PRODUIT cette session » ; la session qui écrit le ticket est dans
   > `from_project`, et c'est l'analogie exacte des six tables existantes. Mesuré ce jour :
   > **231 tickets, 187 self** (`from_project = to_project`, où la question ne se pose pas)
   > **et 44 cross-projet**, où elle tranche. Le lot mélangé **reste all-or-nothing** —
   > non contesté. Débloque M-B, avec Q14.
   >
   > *Texte soumis à l'origine :*

   **Prédicat tickets (Phase 2)** : `from_project` (paternité, proposé) ou `to_project`
   (destination) ? Veto sans coût avant la migration M-B. Le lot mélangé reste
   all-or-nothing dans les deux cas — contestes-tu ce point ?
3. > ✅ **RÉPONDUE le 2026-08-19 (session 2) — et considérablement RÉDUITE avant de
   > l'être.** Les deux sous-décisions piégées, **(a)** le cercle des appelants et **(b)**
   > l'effet heartbeat et son armement, **se dissolvent** sous Q12 = (a) + l'ouverture
   > automatique : la vivacité d'une session agent vient de `last_observed_at`, qui bouge à
   > chaque appel d'outil, donc le checkpoint cesse d'être spécial, et une session
   > `operator` n'est jamais fermée par inactivité.
   > `BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT` n'a plus d'objet. Le checkpoint devient un
   > **objet de jugement pur** dont le seul métier est B7.
   >
   > Réponse sur ce qui restait, **mixte à dessein** : **(c) stockage = la proposition**
   > (append-only, `UNIQUE(session_id, seq)`, `ON CONFLICT DO NOTHING` — le replay d'un
   > retry est idempotent, ce que le CAS du ticket ne donne pas, et les retries d'agents
   > sont la norme, invariant C6) ; **(d) forme du payload = LE TICKET** (`progress` +
   > `blocker|null` + `next_step` en **un appel**, parce que trois `kind` exclusifs
   > laissent émettre un `progress` sans jamais de `next_step` et le lecteur de fraîcheur
   > ne peut pas savoir si l'instantané est complet). La divergence (d) est **abandonnée**,
   > celle de stockage (c) **maintenue**. Détail : §0bis.4.
   >
   > *Texte soumis à l'origine :*

   **Checkpoint MVP (bloque la moitié de la Phase 2)** : approbation produit explicite
   de `d04dc588` — fraîcheur = âge du checkpoint, dérive du focus séparée, jamais
   cause de péremption. Quatre sous-décisions nommées : (a) le **cercle des
   appelants** — commande explicite de l'utilisateur seulement (covenant intact, mais
   adoption bornée par la discipline humaine qui a produit **24/29** stale — mesure du
   2026-08-19, celle du 2026-08-16 disait 21/23), ou
   checkpoint spontané d'agent en longue session autonome (mutation hors commande
   explicite = **changement de covenant**) ; (b) l'effet heartbeat en bord de
   commande — **et, si (a) reste ouverte, par quel mécanisme on l'arme ou non**, car
   l'artefact livré est identique sous les deux réponses (retirer l'effet, ou le
   mettre derrière `BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT` livré fermé : sans l'un
   des deux, la réponse « agent autonome » est armée par omission — voir D4). **Noter
   que « retirer l'effet » n'est pas l'option neutre** : le ticket exige « Checkpoint
   réel rafraîchit heartbeat atomiquement ; replay non », donc ce retrait est une
   **troisième** divergence d'avec son MVP ;
   (c) la **divergence de STOCKAGE vs le MVP du ticket** (append-only + idempotence par
   `(session_id, seq)` au lieu du CAS `expected_checkpoint_revision` — propriétés P0
   conservées sous une autre forme) ; (d) la **divergence de FORME DU PAYLOAD**, non
   déclarée jusqu'ici : le ticket veut `progress` + `blocker|null` + `next_step`
   publiés **ensemble en un appel**, la proposition en fait trois `kind` mutuellement
   exclusifs d'une note unique. Accepter (d), c'est renoncer au critère « un appel » du
   ticket ; le refuser, c'est revenir à sa signature. **Dans les deux cas, la spec
   checkpoint séparée que l'audit du ticket exige reste due — elle est ajoutée au
   contenu de la Phase 0.**
4. **Famille / sous-projets (Phase 2 flag, Phase 4.6)** : armer la capture
   parent→enfant ? Livrer `include_descendants` en lecture — sachant que sous scope
   capability dream (ARMÉ en production), le paramètre est **refusé fail-closed** par
   design, et qu'élargir un bearer à `pk:*` serait une décision de périmètre de
   sécurité distincte (D10) ? Et la direction de fond E5 : vraie hiérarchie (option
   `parent_key` documentée, prédicat d'abord) ou platitude assumée ? (Cette question
   vit entièrement ici — pas dans la n° 5, qui est le sweep.)
   **Question reformulée sur l'état mesuré, pas sur celui de la spec.** « Qui consolide
   `red-shrik:agent` ? » n'a plus le même objet : `red-shrik:agent` et
   `red-lab:architect` sont **dans le pool dream depuis le 2026-08-10** et ont donc
   leurs propres nuits (447 des 533 artefacts colon, 84 %). Ce qui reste à trancher,
   dans cet ordre :
   (a) accepte-t-on que le parent ne voie **jamais** l'enfant (égalité stricte du
   pipeline), ou veut-on la consolidation croisée — c'est-à-dire `include_descendants` ?
   (b) les **86 artefacts** des quatre clés `red-lab:*` restées hors pool
   (`orchestrator` 64, `reviewer` 15, `sentinel` 5, `developer` 2) : les entrer coûte
   un dépassement du plafond de dix, donc `_MAX_POOL` **et** `TimeoutStartSec`
   ensemble, plus la matrice complète de `MCP_HTTP_DREAM_TOKENS` (préflight
   fail-closed : sinon la nuit entière échoue) ;
   (c) veut-on à terme **fusionner** `red-shrik:agent` dans `red-shrik` — ce qui n'est
   plus une question de consolidation nocturne mais de renommage/fusion, donc la
   n° 11 ?
5. **Sweep (bloque Phase 4.2-3)** : ordre et seuils d'armement (7 j confirmés ? durée
   du dry ?) — critère proposé : les trois signaux muets, pas un seul.
6. > ✅ **RÉPONDUE le 2026-08-19 (session 2) : ACCEPTÉE**, et les brouillons non signés
   > d'une traçante **survivent** dans un pool en attente de signature, hors session. Rien
   > n'est perdu, rien n'est attribué sans geste humain. Écartées explicitement :
   > l'auto-promotion à l'auto-fermeture (ce serait **E3 pour toute la moitié agent** —
   > covenant plein, pas le demi-pas de Q6) et l'abandon des brouillons (détruire
   > précisément ce que la nature agent produit, sous un axe qui est la traçabilité).
   > **Sa fragilité technique disparaît au passage** : la liaison par
   > `(project_key, started_by_actor)` + « exactement un » devient une liaison exacte sur
   > la connexion. Ce que ça ajoute à M-E reste à spécifier (§0bis.5).
   >
   > *Rappel du rang, acquis plus tôt le même jour :* l'axe « traçabilité du savoir »
   > avait remonté la Phase 4.4 juste après la Phase 2 (§0.3), faisant de Q6 un critère de
   > sortie de Phase 0 — critère désormais **satisfait**.
   >
   > *Texte inchangé :*

   **Staged captures (bloque Phase 4.4 — désormais juste après la Phase 2)** : la capture préparée-signée est-elle un
   amendement de covenant acceptable ? Et à terme, l'attribution à la création (E3,
   fermeture totale de B3) — décision à prendre sur le dossier chiffré produit par les
   phases 1–4.
7. **Garde du focus (B6)** : l'audit + `focus_diff` (récupérabilité + visibilité)
   suffisent-ils, ou veux-tu un garde dur de contenu, au prix d'un seuil arbitraire sur
   le seul canal de jugement libre ?
8. **Réattribution (Phase 4.5)** : droit de réattribution explicite et journalisée
   (`attribution_moves`, design prêt), ou l'orphelinage reste le prix de la preuve ?
9. > ✅ **RÉGLÉE le 2026-08-19 (session 2) PAR LA MESURE, pas par arbitrage : HÉRITAGE.**
   > Les subagents héritent de la session de leur porteur, sans aucun tag, parce qu'ils
   > partagent sa connexion. Ce n'est plus une préférence de design : **aucun des trois
   > en-têtes ne distingue un subagent de son porteur** — `X-Brain-Agent` porte le PROJET,
   > `X-Brain-Session` est mort (B8), `Mcp-Session-Id` porte la CONNEXION — et aucun ne le
   > fera, la configuration des en-têtes étant par serveur MCP et non par subagent.
   > Tagger exigerait une capacité amont inexistante. Détail : §0bis.2.
   >
   > *Texte soumis à l'origine :*

   **Subagents (conditionne l'ampleur résiduelle de B1)** : session propre ou héritage
   du porteur (design sweep D1 : « l'activité du subagent EST celle de l'opérateur ») ?
   Le plan fonctionne sous les deux réponses mais recommande l'héritage.
10. > ✅ **RÉPONDUE le 2026-08-19 — voir §0.** La liste **couvre** : aucun irritant
    > supplémentaire, donc pas de B15. Mais la **priorité est rejetée** : l'ordre ne
    > vient plus de la gravité dérivée, il vient de l'axe **« traçabilité du savoir »**
    > — B3, B4, B5 en tête. Conséquence : reséquencement P0 → P1 → P2 → **P4.4** → P3, et
    > promotion de Q6 en critère de sortie de Phase 0 (§0.3).
    >
    > *Texte soumis à l'origine :*

    **Tes douleurs** : la liste B est dérivée des preuves ; « pas mal de choses qui ne
    me plaisent pas » peut en contenir d'autres. La Phase 0 te soumet la liste —
    confirme, priorise, complète avant tout go.
11. **Renommage/fusion de projets** : hors périmètre de ce plan (l'immuabilité 033 est
    préservée) ; à cadrer séparément si tu le souhaites (option outillée
    script + `project_aliases` esquissée par A et C).
12. > ✅ **RÉPONDUE le 2026-08-19 : piste (a) — deux natures de session.** Voir §0.1
    > (ce qu'elle exige), D11 (le composant) et §0.4 (Q15, qu'elle ouvre). Trois effets
    > immédiats : la nature est **déclarée** au `start`, pas détectée — B8 ne bloque donc
    > pas (a) ; le **défaut est `operator`**, forcé par C2 ; et l'auto-fermeture des
    > sessions agent se heurte au CHECK terminal 037, d'où Q15.
    >
    > *Texte soumis à l'origine :*

    **Cycle de vie automatique — l'arbitrage que tu t'es réservé** (ticket `2bd14b24`,
    réponse établie : « automatique, pas déclaratif ») : quelle piste — (a) deux
    natures de session (agent traçante auto-fermée sans rituel, opérateur avec
    rituel), (b) plus de rituel du tout, jugement non dérivable migré vers un objet
    dédié, ou (c) fin auto avec résumé dérivé (objection doctrine `FocusArg` versée au
    dossier, §3.3) ? L'accrétion de ce plan est un transitoire compatible avec les
    trois ; ce choix conditionne la cible finale, et personne d'autre que toi ne le
    prend.
13. **Sémantique de `brain_set_project_context` (module M-D, Phase 3)** — *question
    reformulée : sa motivation chiffrée était fausse.* Sous la discipline D7, il est
    proposé que l'argument `current_focus` **omis cesse d'effacer** le focus (distinguer
    « omis » d'un « effacement explicite »). **Le chiffre « 10 contextes sur 59 à NULL »
    ne soutient PAS cette proposition et a été retiré de son argumentaire** : les dix
    lignes sont à `focus_revision = 0` et `focus_updated_at IS NULL`, donc leur focus
    n'a jamais été écrit, pas effacé (mesure du 2026-08-18, D7). Zéro effacement mesuré
    en production. La question se pose donc **sur le raisonnement seul** : le canal
    existe (la branche ON CONFLICT réécrit `current_focus` à NULL quand l'argument est
    omis — vérifié dans le code), il n'a simplement jamais été observé en train de
    mordre. Est-ce un défaut à corriger avant qu'il morde, ou une sémantique d'upsert
    assumée (« je pose l'état complet du contexte ») qu'il vaut mieux ne pas changer
    sous les clients existants ? Veto sans coût avant M-D.
14. > ✅ **RÉPONDUE le 2026-08-19 (session 2) : voie (a) — élargir.** `knowledge_sources`
    > s'ouvre aux tickets, et son prédicat de projet au sous-arbre. L'attestation reste
    > verte **et continue de prouver ce qu'elle prétend prouver** — les voies (b)
    > « documenter le trou » et (c) « renoncer » ont été écartées : (b) creuse un trou dans
    > la preuve de RESTAURATION, c'est-à-dire dans l'histoire DR, laquelle est déjà un
    > blocker ouvert. **Travail sur les DEUX assets v4** (`brain-v42-v4.sql` et
    > `brain-v42-v4-pgrestore.sql`, tenus en parité de CTE), pas un.
    >
    > *Texte soumis à l'origine :*

    **Ce que l'attestation de récupération doit apprendre (bloque M-B)** :
    `ops/recovery/brain-v42-v4.sql` définit la légitimité d'un artefact capturé par
    l'UNION des **six** tables de connaissance et par l'égalité
    `source.project_key = session.project_key`. Les tickets capturables (M-B) et la
    capture famille `pk → pk:child` (D3) produiraient chacun des
    `artifact_source_mismatches` **permanents** — l'attestation, dont le runbook exige
    « all statuses are pass », deviendrait rouge et le resterait. Contrairement aux
    empreintes de schéma, cela ne se répare pas en régénérant un asset : faut-il
    (a) élargir `knowledge_sources` aux tickets et son prédicat de projet au sous-arbre,
    (b) restreindre le contrôle aux six types historiques en documentant le trou, ou
    (c) renoncer à l'un des deux élargissements ? Aucune de ces voies n'est instruite
    ailleurs dans ce dossier.
15. > ✅ **RÉPONDUE le 2026-08-19 : route (3) — nouvel état terminal.** Question
    > **neuve**, posée par aucune version antérieure de ce dossier.
    >
    > **L'état terminal d'une session de nature agent.** Le CHECK
    > `brain_sessions_terminal_state_valid` interdit `ended` sans `summary` **et**
    > `next_focus` non vides, et impose `captured_knowledge_ids = {}` à `abandoned`
    > (`037_session_lifecycle_v4.py:14-91`). Une session auto-fermée **sans rituel** n'a
    > donc aucun état terminal disponible. Trois routes soumises — (1) `abandoned`, au
    > prix d'un instantané terminal qui déclare zéro capture sur le chemin de capture
    > principal ; (2) résumé synthétisé par le serveur, c'est-à-dire la piste (c) de Q12
    > par la porte de derrière, avec son objection C9 ; (3) **nouvel état terminal**.
    >
    > **Retenue : (3)**, migration **M-G** sur le CHECK 037. Détail complet, coûts et
    > ce qui reste à spécifier : **§0.4**. C'est la réponse qui fait tomber le constat
    > du §1.3 et qui amende C7.

---

*Sources : dossier d'instruction `docs/design/refonte-projets-sessions/DOSSIER.md` ;
tickets `d30cf6e5`, `2bd14b24` (dont la réponse opérateur établie du 2026-08-06),
`d04dc588`, `7ffe0e8a`, `c60d023d` ; spike
`docs/upstream/2026-08-06-claude-otlp-session-join.md` (verdict « JOINTURE
IMPOSSIBLE » — le ticket `2dfbb83d`, fermé LIVRÉ le 2026-08-16, n'est pas la source de
ce verdict) ; spec
`dbb7c5ce` ; learnings `7bc821a1`, `367e27ae`, `1c40c36a` ; jugements du panel (trois
lentilles) sur les propositions A/B/C. Vérifications code du 2026-08-18 :
`src/brain_v42/models/project_key.py` ; `src/brain_v42/provenance.py:23`
(`MAX_ACTOR_LENGTH = 64`) ; `src/brain_v42/db/tables.py` (`access_log` sans colonne
projet, `actor String(64)` ; CHECK `brain_session_artifacts_type_valid` ;
`tickets.from_project`/`to_project` `String(50)`, `created_at`) ;
`src/brain_v42/repositories/pg_brain_session.py` (`CAPTURE_TABLES` six tables,
`_validate_captures` — ids listés, raison unique agrégée —, `abandon_stale` UN
statement, prédicat heartbeat-seul ; `_apply_focus_if_current:713-714` qui pose
`focus_revision=expected_revision + 1`) ; les **cinq** exemplaires `src/` du prédicat
colon — `db/project_group_scope.py:24-26`,
`services/project_group_ticket_service.py:129-137` **et `:164-167`**,
`services/proposal_service.py:377-383`, `repositories/pg_project_context.py:202-213` —
et `alembic/versions/036_codex_contract_views.py` (deux corps de CTE, **sept** vues
vivantes mesurées) ; `alembic/versions/001_initial.py:244-247` (`project_contexts`) ;
**`alembic/versions/032_brain_sessions.py:19-34`** (`increment_project_focus_revision()`
+ `project_contexts_focus_revision_trigger`, re-mesuré en production ce jour) ;
`src/brain_v42/db/focus_stamp.py` (« six call sites across three modules », `IS DISTINCT
FROM` « so a focus moving to or from NULL counts ») et
`repositories/pg_project_context.py:281-290` (upsert qui écrase `current_focus`, **avec**
bump par trigger et datation par `focus_stamp`, **sans** CAS) ;
`repositories/pg_access_log.py:38-113` + `services/decay_flusher.py` + `config.py:379`
(le tampon `access_log` est purgé à chaque flush, 300 s par défaut) ;
`services/roadmap_service.py` (le seul écrivain qui bumpe sur un texte inchangé) ;
`src/brain_v42/services/brain_service.py` (scope dream, égalité stricte) ;
`services/dream_project_scope.py:83-120` (`PROJECT_TOOL_POLICIES` — résoudre un projet
est un travail par tool) ; `ops/recovery/brain-v42-v4.sql` **et `ops/recovery/brain-v42-v4-pgrestore.sql`**
(`:404-412` `expected_session_indexes`, liste fermée de trois index contrôlée `:665`
et `:687`) +
`tests/unit/test_recovery_contract{,_v2,_v3,_v4}.py`,
`tests/unit/test_recovery_contract_v4_pgrestore.py:29-33` (parité des CTE),
`tests/unit/test_recovery_contract_v3.py:164-168,488` (`SESSION_INDEX_DEFINITION_MD5`),
`tests/integration/db/test_recovery_contract_v4_execution.py:106` (les **deux** assets
exécutés contre une base réelle) (empreintes, `table_set` dérivé de
`METADATA`, doublons de tête) ; `docs/OPERATIONS.md:118` ;
`tests/unit/test_documentation_contract.py:25-32,1815-1819` ;
`tests/integration/conftest.py:129-155` ; `alembic/versions/039_…:17,337-339`
(`-x allow_project_context_trigger_downgrade=yes`) ; drop-in systemd
`killswitches.conf` de l'unité dream (sweep DRY armé et pool à dix, lus le 2026-08-18) ;
production mesurée 2026-08-18 : head `045` ; `10/59` `project_contexts` à focus NULL,
**tous à `focus_revision = 0` et jamais datés** ; `access_log` à **0 ligne** ; masse
colon `red-shrik:agent` 312 / `red-lab:architect` 135 / quatre `red-lab:*` restantes 86 ;
`src/brain_v42/mcp/provenance_middleware.py:74-96` (en-têtes seuls, jamais les
arguments) ; `src/brain_v42/maintenance/plan_index_repair_store.py:63` +
`tests/unit/test_plan_index_repair_head_pin.py:45-52` ; `alembic/versions/` (head
versionnée 045, **aucune 046**). Aucun commit, aucune écriture brain, aucune écriture
DB, aucun fichier touché hors `docs/design/refonte-projets-sessions/`.*

*Passe du 2026-08-19 — quatre corrections, dont deux portant sur des affirmations
introduites par la réparation précédente (lecture seule, aucune écriture) :*
1. *`brain_update_project_focus` **n'est pas** le seul à bumper sur texte inchangé : le
   CAS de `end` (`pg_brain_session.py:713-714`) pose `expected + 1` sans comparer le
   texte, et le CHECK 037 l'exige — c'est le régime normal d'une fin de session (D7).*
2. *Le `OLD+2` invoqué pour interdire un bump applicatif **n'existe pas** : le trigger
   032 **assigne**, il n'ajoute pas ; les deux bumps explicites doivent rester (D7).*
3. *Le constraint trigger `AFTER UPDATE OF current_focus` **ne voit pas les INSERT** —
   `pg_project_context.create:51-71` et la branche INSERT de `get_or_create:273-275`
   écrivent un focus à `focus_revision = 0` hors de sa portée : trou nommé, trois voies
   proposées, N1 borné en conséquence.*
4. *`ops/recovery/brain-v42-v4.sql:913-918` exige `tgenabled = 'O'` de chaque trigger
   attendu : le trigger de M-D **créé désactivé** (§5.4) rend l'attestation rouge dans
   les deux configurations d'asset — fenêtre à dater, régénération à poser avec
   l'activation (§5.2). La liste `expected_runtime_user_triggers` compte **treize**
   triggers sur cinq tables (`:533-548`), dont sept sur `project_contexts`.*
*Aussi : `d04dc588` relu — « Checkpoint réel rafraîchit heartbeat atomiquement ; replay
non » ⇒ retirer l'effet heartbeat serait une troisième divergence (Q3(b)) ; champs de
suggestion renommés `project_uncaptured_since_start(_count)` pour coller au prédicat ;
compte des têtes corrigé (deux fermes, quatre conditionnelles). Mesures du 2026-08-19,
lecture seule : head `045` ; `10/59` focus NULL, tous à révision 0 et jamais datés ;
`access_log` 0 ligne ; sept vues `split_part` ; sept triggers utilisateur sur
`project_contexts` ; masse colon 312/135/64/15/5/2 = 533 contre `red-shrik` 245.*

*Passe de pliage des résidus, 2026-08-19 (seconde passe du jour ; lecture seule, aucune
écriture DB, aucun commit). Six corrections, dont trois majeures :*

| Ce qui manquait ou était faux | Ce qui est vrai | Où |
|---|---|---|
| L'attestation v4 était traitée comme **un** fichier (`brain-v42-v4.sql`) | Il y en a **deux** : `brain-v42-v4-pgrestore.sql` porte les mêmes structures, est exécutée contre une base réelle (`…v4_execution.py:106`) et tenue en **parité de CTE** (`…v4_pgrestore.py:29-33`). « Régénérer `ops/recovery/` » = les deux | §5.2 *(i)*, §5.1, §8 du PLAN |
| Trois mécanismes de casse d'attestation recensés | **Quatre** : `expected_session_indexes` (`v4.sql:404-412`, contrôlée `:665`/`:687`, doublée par `SESSION_INDEX_DEFINITION_MD5`) fige la liste FERMÉE des index de `brain_sessions` | §5.2 *(ii)* |
| Le mot « index » absent du document | `started_by_actor` naît **sans index** et D5 filtre dessus à chaque appel outermost ; les trois index réels (mesurés) ne le couvrent pas. Décision **à instruire**, coût **à mesurer en Phase 0**, pas tranchée ici | D1, D5 |
| B8 citée comme contrainte établie | **Cotée sur une mesure périmée** : spike mesuré sur Claude Code 2.1.220, `claude --version` = **2.1.234** ; re-jeu programmé comme étape de Phase 0 | tableau §1.2, D1 |
| Population d'ambiguïté présentée comme à découvrir | Mesure **partielle** déjà disponible dans `7ffe0e8a` (2026-08-16 : `auto-discord` 6, `red-arena` 3, `claude-dev-pc`/`red-lab` 2) ; re-mesurée le 2026-08-19 : 24 des 29 `open` dans un projet à ≥2 | D6, borne (3) |
| « les révisions avancent d'un cran (CAS 209→210 du dossier) » | **Corroboration retirée** : ce CAS est un `brain_session_end`, pas `roadmap_service`, et rien ne dit que le texte du focus ait changé. La preuve reste la source plpgsql seule (`032_brain_sessions.py:19-34`) | D7 |
| « 18 fantômes balayables » sans caveat | Re-mesuré le 2026-08-19 : **29 `open`, 21 balayables >7 j** sur 467 lignes | §1.1 |

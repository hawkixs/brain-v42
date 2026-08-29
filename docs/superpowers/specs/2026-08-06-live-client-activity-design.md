# Activité live des clients du brain — généraliser « Codex activity »

**Date** : 2026-08-06
**Statut** : design validé par l'opérateur, plan d'implémentation à écrire
**Ticket brain** : `2dfbb83d-f6cf-4570-9b13-502acc8c776c`
**Périmètre** : bout en bout — `brain_v42` puis le panneau red-monitor

## Le problème, mesuré

Le panneau « live workload — Codex activity » de red-monitor est spécifique à
Codex, du récepteur jusqu'aux libellés. L'opérateur veut le concept pour tout ce
qui se connecte au brain, à commencer par les sessions Claude.

La chaîne existante, lue et non supposée :

1. Le CLI Codex pousse ses propres logs OTLP en loopback sur `POST /v1/logs` du
   sidecar — `metrics/server.py:156`. Durci : loopback-only (403 sinon),
   256 KiB, 4 requêtes en vol (503), `Content-Type` et encodage stricts.
   Configuré dans `~/.codex/config.toml` : `endpoint =
   "http://127.0.0.1:9200/v1/logs"`.
2. `metrics/codex_telemetry.py` en fait un registre pseudonymisé en mémoire :
   `conversation.id` → HMAC `codex-<32hex>`, TTL 600 s, cap 64 conversations,
   dédup par empreinte, 5 attributs projetés seulement.
3. `metrics/cockpit.py:113` l'injecte dans `GET /api/cockpit`.
4. red-monitor : `internal/web/brain.go` reproxifie les octets bruts (cache TTL
   1,5 s + stale-on-error) ; `frontend/src/tabs/brain/BrainActivity.jsx`
   affiche. Libellés « Codex » en dur aussi dans `BrainStatusBar.jsx:55` et
   `brainPresentation.js:50` (`codex-anonymous`).

### Le biais de nature

Ce panneau ne mesure pas ce qui se connecte au brain. Il mesure ce que le CLI
Codex veut bien raconter sur lui-même. Un Codex qui n'appelle jamais un tool
brain y apparaît quand même. Généraliser en ajoutant simplement Claude au même
récepteur hériterait du biais au lieu de le corriger.

C'est la même erreur de nature que celle corrigée côté dream : confondre un
proxy avec ce qu'on veut savoir.

## Terrain existant

Ce que le brain observe déjà de ses clients, sans rien ajouter :

- **L'acteur de provenance.** `brain_v42/provenance.py` (livré le 2026-08-06,
  commits `a66c92ba` et `fb34a826`) expose `normalize_agent`, un ContextVar
  d'acteur et `is_human_actor`. `ProvenanceMiddleware`
  (`mcp/provenance_middleware.py`, enregistré **inconditionnellement** à
  `mcp/server.py:261`) pose l'acteur avant chaque appel de tool.
  `metrics/instrument.py:51` ne lit plus l'en-tête : il appelle
  `get_current_actor()`. L'identité est donc déjà sortie des métriques.
- **La table `brain_sessions`** (`db/tables.py:689`) : `project_key`,
  `client_key`, `status`, `last_heartbeat_at`, ledger de capture.

Deux réserves mesurées, à ne pas redécouvrir :

- `brain_sessions.last_heartbeat_at` ne bouge que sur commande explicite de
  l'utilisateur (règle CLAUDE.md : aucun hook ne touche au lifecycle). Une
  session `open` peut être morte depuis deux jours. **La liveness côté brain,
  c'est le débit d'appels de tools, pas la table des sessions.** Cette spec
  n'utilise donc pas `brain_sessions` comme source de vie.
- L'acteur est un **projet**, pas une session : Claude Code envoie `${PWD}`,
  normalisé en basename. Deux sessions Claude dans `brain-v42` sont
  indiscernables sans corrélation neuve.

## Mesures du 2026-08-06

Faites, pas déduites :

- `CLAUDE_CODE_SESSION_ID` est exporté dans l'environnement d'une session Claude
  Code (valeur observée : un UUID). Mesuré depuis une session `sdk-cli` avec
  `CLAUDE_CODE_CHILD_SESSION=1` — **l'environnement d'une session interactive
  peut différer**.
- `.mcp.json` expanse `${VAR}` dans les en-têtes : `X-Brain-Agent: "${PWD}"` est
  en place dans `~/.claude.json:3807`.
- `~/.codex/config.toml` envoie `X-Brain-Agent = "codex"` en **littéral
  statique**, sans identifiant de conversation. Codex ne peut donc pas être
  joint, et tous ses appels de tools s'écrasent sur un seul acteur `codex`.
- `internal/web/brain.go` proxifie les octets bruts : ajouter des champs à
  `/api/cockpit` ne demande **aucun** travail Go.

## Décisions structurantes

### Univers : fusion des deux sources

Option retenue par l'opérateur : ni « les agents de la machine » (OTLP seul,
qui hérite du biais), ni « les clients du brain » (dérivé seul, sans tokens ni
coût), mais **les deux fusionnés** — une ligne par client, colonnes remplies par
la source disponible.

### Unité de ligne : la session, avec résiduel assumé

**Une ligne = une session quand une identité de session existe ; sinon une
ligne résiduelle par acteur, étiquetée « non attribué ».**

Conséquence pour Codex : N lignes de conversation (tokens, turns, aucun appel
brain) **plus** une ligne `codex — non attribué` (ses appels de tools, aucun
token). Cette apparente redondance est le résultat honnête : Codex ne dit pas
quelle conversation appelle le brain. Le panneau montre ce trou au lieu de le
combler par une corrélation inventée.

Conséquence pour Claude : lignes jointes des deux côtés, plus un résiduel pour
tout client qui n'enverrait pas l'en-tête de session.

### Jointure dans l'espace des pseudonymes

La clé de fusion est le **HMAC de l'UUID de session**, jamais l'UUID. Les deux
côtés hachent avec le secret de processus déjà utilisé par `codex_telemetry`.
La propriété actuelle — aucun identifiant brut ne quitte le registre — est
préservée, pas contournée.

### Observation côté brain dans le middleware, pas dans l'instrumentation

L'incrément de liveness se branche dans `ProvenanceMiddleware`, qui tourne
inconditionnellement, et non dans `instrument_tool`, branché seulement si un
collecteur de métriques existe. Une provenance silencieusement muette est pire
que pas de provenance ; le même raisonnement vaut pour l'activité.

Le registre enregistre `calls` et `last_seen`, rien de plus — pas de nom de tool.

**Un incrément naïf serait faux en production.** Mesuré le 2026-08-06 (commit
`58329a84`) : en profil `compact`, la passerelle exécute l'appel interne via
`FastMCP.call_tool` avec `run_middleware=True` (défaut, fastmcp 3.4.2), donc
`on_call_tool` se déclenche **deux fois** par appel client —
`['brain_call_tool', 'inner_tool']`. Un `calls += 1` compterait donc double en
`compact`, qui est le profil de production, et simple en profil natif : deux
`brain_calls` incomparables entre eux. `last_seen` y est insensible, étant
idempotent.

Correctif retenu : **garde de ré-entrance** — un ContextVar de profondeur,
incrément au seul `depth == 1`. Préféré à un filtre sur les noms de passerelle,
qui raterait `brain_find_tool` et les tools lifecycle, et qu'il faudrait
maintenir à chaque ajout de passerelle. La garde compte exactement un événement
par appel client, dans les deux profils.

Ne pas citer le learning `b77dba43` sur ce point : il est réfuté par
`310a9953`. Synergie à noter pour plus tard — le ticket `c352eaaa` (retrait du
monkey-patch des métriques) devient viable, et métriques comme activité
partageraient alors ce middleware et cette garde.

### Le registre change de nom, pas de discipline

`CodexConversationRegistry` devient `ClientActivityRegistry`
(`metrics/client_activity.py`). TTL 600 s, cap, HMAC, dédup par empreinte : le
patron est bon, c'est le nom qui est trop étroit. Les deux décodeurs OTLP
deviennent deux projections vers ce registre unique.

### La frontière de processus, et pourquoi le rail existant ne convient pas

Mesuré le 2026-08-06 : ce sont **deux processus**, pas un.

| PID | Commande | Port | Porte |
|-----|----------|------|-------|
| 2925883 | `-m brain_v42.mcp.server --http-server` | 8765 | `ProvenanceMiddleware` |
| 1144772 | `-m brain_v42.metrics` | 9200 | récepteur OTLP, registre, `/api/cockpit` |

Deux conséquences que la première rédaction de cette spec ignorait :

1. Le middleware et le registre ne partagent aucune mémoire. Le schéma de flux
   les faisait converger dans le même objet — c'était faux.
2. Le secret HMAC est `secrets.token_bytes(32)` **par processus**
   (`codex_telemetry.py:320`). « Même HMAC des deux côtés » était donc
   impossible : deux secrets distincts, une jointure qui ne matcherait jamais.

**Le rail cross-processus existant ne convient pas.** `flusher.py:34` écrit dans
`process_metrics` toutes les **30 s**, et `collector_db.py:137` lit une fenêtre
de 60 s. Pour un panneau qui poll à 2 s, `brain_calls` et `last_seen` seraient
en retard de 30 s pendant que `tokens` serait frais à 2 s — deux fraîcheurs
différentes dans la même ligne, soit exactement le genre de mélange qui trompe.
Écarté.

**Retenu : un second récepteur loopback sur le sidecar.** Le processus MCP pousse
ses observations vers `:9200`, avec le durcissement du récepteur OTLP existant —
loopback-only, corps borné, requêtes en vol plafonnées, rejet fail-closed.

**Le hachage se fait à la réception, côté sidecar.** Donc pas de secret partagé,
et `secrets.token_bytes(32)` par processus reste intact. L'UUID de session brut
traverse alors une socket loopback entre deux processus locaux — c'est
**exactement** la posture du récepteur OTLP actuel, qui reçoit déjà les
`conversation.id` bruts de Codex en clair sur loopback et les hache à l'arrivée.
On ajoute un émetteur à un récepteur qui existe, on ne change pas la propriété :
« aucun identifiant brut ne quitte le registre » porte sur ce qui sort dans le
payload, pas sur ce qui entre en loopback.

L'alternative — un secret partagé provisionné aux deux processus, chacun hachant
de son côté — évite le transit de l'UUID mais transforme un secret éphémère en
élément de configuration à provisionner et faire tourner. Refusée pour ça.

### Contrat additif

On **ajoute** `clients[]` à `/api/cockpit` et on laisse `activeConvs`,
`metrics.active_convs` et `metrics.ctx_tokens` intacts le temps que le panneau
bascule. Aucune rupture pour le panneau existant.

## Flux de données

```text
     processus MCP (:8765)                    sidecar métriques (:9200)
┌───────────────────────────┐        ┌──────────────────────────────────────┐
│ ProvenanceMiddleware      │        │ Codex CLI ────OTLP────┐              │
│  X-Brain-Agent            │        │ Claude Code ──OTLP────┤              │
│  X-Brain-Session          │        │                  décodeurs (2)       │
│  garde de ré-entrance     │        │                       │              │
└─────────────┬─────────────┘        │                       ▼              │
              │ push loopback borné  │        ClientActivityRegistry        │
              └──────────────────────┼──────────────→ (HMAC à la réception) │
                                     │                       │              │
                                     │  cockpit.py → /api/cockpit.clients[] │
                                     └───────────────────────┬──────────────┘
                                                             │
                                        red-monitor brain.go (proxy inchangé)
                                                             │
                                             frontend « Live workload »
```

## Forme d'une ligne

```json
{
  "id": "<pseudonyme>",
  "kind": "session" | "unattributed",
  "agent": "claude",
  "actor": "brain-v42",
  "started": "12:31",
  "last_seen_s": 4,
  "model": "claude-opus-5",
  "turns": 12,
  "tokens": 128000,
  "cost": 1.23,
  "brain_calls": 37
}
```

Tout champ non mesuré vaut `null`, jamais `0` — doctrine déjà en place dans
`cockpit.py` (« None = non mesuré ; 0.0 serait indiscernable d'un vrai zéro »).

Deux champs se ressemblent et ne disent pas la même chose :

- **`agent`** vient de la source OTLP et d'elle seule : le décodeur qui a produit
  la ligne sait s'il parlait à Codex ou à Claude Code. Une ligne alimentée
  uniquement côté brain a `agent: null`. Ne pas le déduire du nom d'acteur : ce
  serait deviner.
- **`actor`** est la valeur normalisée de `X-Brain-Agent`, telle que
  `provenance.normalize_agent` la produit. C'est un vocabulaire déjà en place
  dans le code, et il est volontairement hétérogène : Claude Code y met le
  basename du **répertoire de LANCEMENT** (`${PWD}` expansé — ce n'est PAS un
  nom de projet : lancé depuis `/home/hawixs` l'acteur est `hawixs`, depuis un
  worktree c'est le nom du worktree, et le même projet produit `brain_v42`
  (underscore, basename) ou `brain-v42` (tiret, littéral du `.mcp.json`) selon
  le chemin emprunté — mesuré, fil du ticket `a3fa6696`) ; Codex y met un
  libellé de service (`codex`). Le panneau l'affiche tel quel sans prétendre
  que c'est un projet.

Par type de ligne : une `unattributed` a `tokens`, `turns`, `cost`, `model` et
`agent` à `null`, et son `id` est dérivé de l'acteur. Une ligne OTLP-only a
`actor` et `brain_calls` à `null`. Une ligne jointe les a tous.

## Le panneau red-monitor

Dépôt distinct : `~/hawkixs_infra/git_repo/ReD_v1/projects/red-monitor`, avec ses
propres conventions et sa propre suite. Aucun travail Go : `brain.go` proxifie
les octets bruts, les champs neufs arrivent seuls.

`BrainActivity.jsx` cesse d'être spécifique à Codex :

- il consomme `clients[]` au lieu de `live.activeConvs` ;
- le titre passe de « Codex activity » à « Live workload », et les libellés en
  dur disparaissent de `BrainStatusBar.jsx:55` (`<StatusMetric label="Codex">`)
  et de `brainPresentation.js:50` (`shortPseudonym` retourne `codex-anonymous`
  par défaut — devient neutre) ;
- une ligne `unattributed` est visuellement distincte et porte son motif, pour
  que « Codex ne dit pas quelle conversation appelle le brain » se lise sans
  connaître cette spec ;
- la mise en garde « déclaré par le client, pas prouvé » est **dans le panneau**,
  pas seulement dans la documentation.

`Brain.test.jsx` porte déjà des fixtures `activeConvs` ; elles restent valides
tant que le contrat est additif, et basculent avec le panneau.

## Le spike est une porte, la conception y survit

Tâche 1 du plan, avant toute conception détaillée. Deux choses à prouver :

1. `${CLAUDE_CODE_SESSION_ID}` s'expanse bien dans un en-tête `.mcp.json`, dans
   une session **interactive** (la mesure du jour vient d'une session `sdk-cli`).
2. L'attribut `session.id` de l'OTLP Claude Code porte **le même UUID** que cette
   variable.

Si le spike échoue, rien ne tombe : toutes les lignes deviennent `unattributed`
plus des lignes OTLP-only. C'est exactement le mode « deux sections non jointes »
écarté au brainstorming. La conception se dégrade, elle ne se réécrit pas.

## Tests

TDD, cycle Red-Green-Refactor comme le reste du projet.

Unitaire :

- décodeur OTLP Claude Code — schéma d'attributs distinct de Codex
  (`claude_code.user_prompt`, `claude_code.api_request` ; `session.id`,
  `input_tokens`, `output_tokens`, `cost_usd`) ;
- jointure en espace pseudonyme : deux sources, même UUID, une seule ligne ;
- résiduel non attribué : appels sans en-tête de session → ligne d'acteur ;
- **garde de ré-entrance** : un appel en profil `compact` produit exactement un
  incrément malgré les deux déclenchements de `on_call_tool` — le test doit
  simuler l'imbrication réelle (passerelle puis tool interne), sinon il passe au
  vert sans rien prouver ;
- récepteur d'observations : durcissement identique à `/v1/logs` — non-loopback
  rejeté, corps hors borne rejeté, saturation en 503, malformé fail-closed ;
- non-régression des bornes : TTL, cap, dédup, rejets malformés ;
- forme du payload : `null` et non `0` pour tout champ sans source.

Le spike est manuel et n'est pas automatisable : il dépend de l'environnement
d'un client externe.

## Limites assumées

- **`X-Brain-Agent` et `X-Brain-Session` sont déclarés par le client, donc
  falsifiables.** Signal d'hygiène, pas frontière de sécurité — même posture que
  le `client_key` de session, « déclarée, pas prouvée ». `collector.py:100`
  plafonne déjà la cardinalité contre exactement ça. Cette mise en garde doit
  accompagner le panneau : sans elle, quelqu'un le lira un jour comme une preuve.
- **Activer l'OTLP de Claude Code est une modification de config sur chaque
  client**, pas un flip côté serveur.
- **Changement de posture sur la vie privée** : les lignes exposent le nom du
  projet (dérivé de `${PWD}`), là où le panneau Codex était intégralement
  pseudonyme. Choix explicite de l'opérateur sur sa propre machine, pas un effet
  de bord.
- **Codex restera partiellement aveugle** tant que sa config MCP ne portera pas
  d'identifiant de conversation. Ce n'est pas un défaut de cette conception.
- **Un émetteur de plus vers le sidecar est une surface de plus.** Loopback-only
  et borné comme `/v1/logs`, mais la frontière réseau du projet est suivie de
  près : à inscrire dans le bloc « Tracked network boundary » du CLAUDE.md au
  moment du rollout, pas après.
- **L'UUID de session brut transite en loopback** entre les deux processus
  locaux. Assumé, et identique à ce que fait déjà le récepteur OTLP. À
  re-arbitrer si les deux processus cessent un jour de partager la machine.

## Hors périmètre

- Toute corrélation avec `brain_sessions` : la table n'est pas une source de
  liveness (voir « Terrain existant »).
- La persistance des lignes : le registre est en mémoire, borné, et perd tout au
  redémarrage. Un historique agrégé est un autre chantier.
- Le tableau de bord d'usage Claude Code de red-monitor
  (`internal/claudeusage`, agrégats journaliers depuis les transcripts JSONL) :
  rétrospectif par nature, sans rapport avec le temps réel.
- La suppression de `activeConvs` du payload, différée à la bascule du panneau.
- L'authentification applicative du sidecar, hors sujet ici.

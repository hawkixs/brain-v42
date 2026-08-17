# Spike — jointure de session Claude Code, et schéma OTLP réel

**Date** : 2026-08-06
**Version mesurée** : Claude Code 2.1.220
**Plan** : `docs/superpowers/plans/2026-08-06-live-client-activity.md`, tâche 1
**Verdict** : **JOINTURE IMPOSSIBLE**

## Méthode

Contrairement à ce que le plan annonçait, le spike n'a demandé aucune session
interactive. `claude -p` lance une vraie session Claude Code en sous-processus.
Deux récepteurs jetables sur `127.0.0.1:4318` — un pour les en-têtes MCP
(`--mcp-config` dédié, `--strict-mcp-config`), un pour l'OTLP — suffisent à
répondre aux deux questions.

## Question 1 — `${CLAUDE_CODE_SESSION_ID}` s'expanse-t-il dans un en-tête MCP ?

**Non, pas utilement.**

| Environnement parent | En-tête `X-Brain-Session` reçu |
|---|---|
| `CLAUDE_CODE_SESSION_ID` présent | `3d7a88d7-…` — **l'identifiant du parent** |
| `CLAUDE_CODE_SESSION_ID` absent | `${CLAUDE_CODE_SESSION_ID}` — **littéral non expansé** |

Claude Code expanse `${VAR}` depuis l'environnement du **processus**, au
chargement de la configuration MCP. Il ne pose pas `CLAUDE_CODE_SESSION_ID`
pour lui-même avant cette lecture : l'identifiant qu'il se donne
(`e7734ce9-…` au second run) n'est jamais visible à l'expansion.

Le premier run semblait concluant ; il ne l'était pas. L'environnement de la
session appelante fuyait dans le sous-processus et y injectait le **mauvais**
identifiant. Une jointure bâtie dessus aurait rattaché toutes les sessions
filles à leur parent, silencieusement.

`${PWD}` s'expanse correctement (`/tmp` observé) : le mécanisme fonctionne,
c'est la variable qui n'existe pas au bon moment.

**Conséquence** : la ligne `unattributed` prévue par la conception n'est pas un
cas dégradé théorique, c'est le cas **nominal** pour Claude Code aujourd'hui.
`provenance.normalize_session` rejette déjà le gabarit non expansé et renvoie
`None` — le comportement mesuré tombe exactement dans le chemin prévu.

## Question 2 — le schéma OTLP réel

`session.id` existe bien, ainsi que tous les compteurs voulus. Trois écarts
avec ce que le plan supposait.

### Écart 1 — `event.name` n'est PAS préfixé

Le plan attendait `claude_code.user_prompt`. Mesuré :

| Champ | Valeur |
|---|---|
| `event.name` (attribut) | `user_prompt`, `api_request`, `assistant_response` |
| `body.stringValue` | `claude_code.user_prompt`, `claude_code.api_request` |

Le préfixe est dans le **corps**, pas dans l'attribut. Un décodeur filtrant sur
`claude_code.*` via `event.name` ne reconnaîtrait aucun enregistrement.

Autres événements vus, hors périmètre : `hook_execution_start`,
`hook_execution_complete`, `hook_registered`, `plugin_loaded`.

### Écart 2 — `input_tokens` ne mesure pas le contexte

Relevé sur un `api_request` réel :

| Attribut | Type | Valeur |
|---|---|---|
| `input_tokens` | intValue | **10** |
| `cache_read_tokens` | intValue | 11 776 |
| `cache_creation_tokens` | intValue | 6 804 |
| `output_tokens` | intValue | 43 |
| `cost_usd` | doubleValue | 0.0150106 |
| `cost_usd_micros` | intValue | 15011 |
| `duration_ms` | intValue | 1496 |
| `model` | stringValue | `claude-haiku-4-5-20251001` |

Le contexte réel de cette requête est ~18 590 tokens ; `input_tokens` en
rapporte 10. Afficher `input_tokens` comme « context tokens » — ce que fait le
panneau Codex avec `input_token_count` — sous-estimerait une session Claude de
trois ordres de grandeur.

La somme utile est `input_tokens + cache_read_tokens + cache_creation_tokens`.

### Écart 3 — chaque enregistrement porte des données personnelles

**Tous** les enregistrements, y compris les événements de hook et de plugin,
portent en clair :

| Attribut | Contenu observé |
|---|---|
| `user.email` | l'adresse e-mail du compte, en clair |
| `user.id` | empreinte de 64 caractères |
| `user.account_uuid`, `user.account_id` | identifiants de compte |
| `organization.id` | identifiant d'organisation |

`prompt` et `response` valaient `<REDACTED>` — la rédaction est le défaut, mais
`OTEL_LOG_USER_PROMPTS=1` la lève. `prompt_length` et `response_length` sont
toujours en clair.

**Conséquence de conception** : la projection par liste blanche du récepteur
n'est pas une précaution de style, c'est la seule chose qui empêche l'adresse
e-mail de l'opérateur d'entrer dans un registre exposé par HTTP. Elle devient
une exigence justifiée par la mesure, à tester explicitement.

`terminal.type` valait `non-interactive` sous `claude -p`.

## Ce que ça change dans le plan

1. **Tâche 5** — `_KNOWN_EVENTS` devient `{"user_prompt", "api_request"}`, sans
   préfixe. La fixture `tests/fixtures/claude_otlp_logs.json` est l'oracle.
2. **Tâche 5** — projeter aussi `cache_read_tokens` et `cache_creation_tokens`,
   et ajouter un test prouvant que `user.email` ne survit pas à la projection.
3. **Tâche 8** — les tokens d'une ligne Claude sont la somme des trois
   compteurs d'entrée, pas `input_tokens` seul.
4. **Tâche 8** — le test de jointure devient un test d'**absence** de jointure :
   une session Claude produit une ligne OTLP-only et une ligne `unattributed`
   distinctes, comme Codex. La jointure reste implémentée pour le jour où un
   client saura déclarer sa session ; elle n'a simplement aucun client
   aujourd'hui.

## Ce qui rouvrirait la question

Un `X-Brain-Session` renseigné suppose que le client connaisse son identifiant
de session avant de charger sa configuration MCP. Aujourd'hui ni Claude Code ni
Codex ne le permettent. Re-mesurer à chaque montée de version de Claude Code
plutôt que de reprendre cette conclusion sur parole.

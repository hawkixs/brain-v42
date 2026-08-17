# Armer le scope serveur du dream — bascule, rotation, rollback

Étape 8 de `docs/superpowers/specs/2026-08-08-dream-v2-design.md`. Basculé en
production le 2026-08-10.

## Ce que ça change, et pourquoi c'est le lot qui compte

Tant que `BRAIN_DREAM_CAPABILITY_ENFORCEMENT` est absente, le principal d'une
phase est `unscoped` et `on_call_tool` laisse passer sans périmètre. Une nuit
lancée pour `red` lit alors le corpus des 54 projets et peut le muter. À dix
projets, c'est dix fois le même travail global — la spec chiffre 8 × 15 min de
travail pour la valeur de 15.

Armé, chaque phase reçoit un bearer propre à `(projet, phase)`. Les outils
lisent le périmètre par `get_dream_project_scope()` et filtrent leurs requêtes.
**Mesuré le 2026-08-10** : un SCAN scopé sur `red` voit 751 learnings sur 2 760,
soit 27 % du corpus.

Second effet, qui n'était pas l'objectif : les prompts INTERDISAIENT déjà des
outils en prose (« Do NOT call brain_learn », `## Forbidden tools`). Ces
interdictions sont maintenant adossées à un refus serveur.
`tests/unit/test_dream_prompts_match_phase_allowlists.py` garde les deux
d'accord.

## Le mode d'échec, qui est vert

Le 2026-07-03, un bearer manquant a fait tourner chaque phase en 401 — zéro
outil brain — et la nuit a rendu « 6/6 OK ». Le drop-in `token.conf` existe à
cause de ça. Un registre incomplet, mal formé, ou présent d'un seul côté produit
exactement la même nuit. **Toute étape ci-dessous se termine par une preuve
positive, jamais par une absence d'erreur.**

## Bascule

Le registre exige une **matrice complète** : les six phases pour chaque projet,
sinon `parse_dream_capability_registry` lève au démarrage du serveur MCP et la
production ne repart pas. À dix projets, soixante profils.

```bash
# 1. Frapper. L'outil valide sa sortie avec le parseur du SERVEUR avant
#    d'écrire, donc il ne peut pas produire un registre refusé au démarrage.
#    Le bearer admin entre par l'environnement, jamais par un argument.
#    --from-drop-in reprend exactement le pool de l'unité vivante.
MCP_HTTP_TOKEN="$(sed -n 's/^MCP_HTTP_TOKEN=//p' ~/.config/brain-v42/mcp-token.env)" \
  uv run python -m scripts.mint_dream_capability_registry \
    --output ~/.config/brain-v42/dream-registry.staged --from-drop-in

# 2. Sauvegarder le fichier privé AVANT de le toucher. Le rollback est ce fichier.
cp -p ~/.config/brain-v42/mcp-token.env \
      ~/.config/brain-v42/mcp-token.env.bak-$(date -u +%Y%m%d-%H%M%S)
```

Composer ensuite le nouveau fichier privé avec un éditeur local — jamais par un
`echo`, dont l'argument reste dans l'historique du shell. Il porte exactement
trois affectations, et le preflight rejette toute autre clé :

```
MCP_HTTP_TOKEN=<inchangé>
MCP_HTTP_DREAM_TOKENS=<la ligne frappée à l'étape 1, sans son préfixe de clé>
BRAIN_DREAM_CAPABILITY_ENFORCEMENT=true
```

```bash
# 3. Valider AVANT de redémarrer, dans les conditions de systemd — le preflight
#    compare le fichier à l'environnement EFFECTIF, donc il faut le charger.
env $(cat ~/.config/brain-v42/mcp-token.env | xargs -d '\n') \
  .venv/bin/python scripts/check_mcp_http_port.py \
    --shared .env --expected 8765 --expected-host 127.0.0.1 \
    --token-file ~/.config/brain-v42/mcp-token.env \
    --require-effective-runtime-settings

# 4. Redémarrer le seul service MCP HTTP.
systemctl --user restart brain-mcp-http.service
curl -fsS -m 3 http://127.0.0.1:8765/health
```

## Preuves à exiger, dans cet ordre

1. **Le bearer admin passe encore.** Un appel `brain_*` depuis un client
   existant doit répondre. Une erreur de validation d'argument est une preuve
   suffisante — elle vient d'APRÈS l'authentification. Un 401 ne l'est pas.
2. **Un bearer scopé passe.** `POST /mcp` avec le token d'un profil doit rendre
   200. C'est la preuve de non-cécité, et c'est celle qui manquait en 2026-07-03.
3. **Un token inventé est refusé.** 401. Sans cette troisième sonde, les deux
   premières ne prouvent pas qu'il existe une garde.
4. **La matrice couvre le pool, et rien de plus.**
   `codex_runner --preflight-capabilities --project-key X` pour chaque projet du
   pool, puis pour un projet HORS pool — le second doit ÉCHOUER. Sans cette
   garde inverse, la matrice ne prouve rien.

`dream.sh` rejoue le préflight pour chaque projet du pool avant toute mutation,
donc un trou dans la matrice arrête la nuit avant qu'elle commence.

## Élargir le pool

Le registre est frappé pour un pool donné. **Ajouter un projet au drop-in sans
refrapper le registre fait échouer le préflight de ce projet, donc la nuit
entière.** C'est fail-closed et voulu. La séquence est : refrapper pour le
nouveau pool, remplacer le fichier privé, redémarrer MCP HTTP, revérifier.

## Rotation

`accepted` existe pour le recouvrement : y placer l'ancien token le temps que
les clients prennent le nouveau, puis le retirer. `verify_token` honore
`active` **et** tous les `accepted`. La frappe initiale laisse `accepted` vide.

## Rollback

Un seul geste, et il est complet :

```bash
cp -p ~/.config/brain-v42/mcp-token.env.bak-<horodatage> \
      ~/.config/brain-v42/mcp-token.env
systemctl --user restart brain-mcp-http.service
```

Sans `BRAIN_DREAM_CAPABILITY_ENFORCEMENT`, `_configure_http_security` repose le
`BearerTokenGuard` historique sur `MCP_HTTP_TOKEN` : les clients existants ne
voient aucune différence. Le côté dream retombe sur `unscoped` par le même
fichier — les deux unités le lisent en `EnvironmentFile`, donc la bascule et le
rollback sont un seul fichier, pas deux à garder synchrones.

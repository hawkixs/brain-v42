# Passation — 2026-08-10, soirée

Écrit parce que le client MCP de la session émettrice est mort à la bascule (voir
§1). Le brain, lui, a bien tout reçu : ce fichier ne remplace pas la mémoire, il
donne les identifiants pour aller la lire et l'état de ce qui était en cours.

---

## 0. À faire en premier, dans l'ordre

1. **Reconnecter le MCP brain** (`/mcp`, ou nouvelle session). Une connexion
   neuve fonctionne — mesuré : `tools/list` → 9 outils.
2. **Relire le brain** plutôt que ce fichier pour le détail : les entrées ci-dessous
   sont plus complètes et portent leurs mesures.
3. **La session brain `8f2dc148-73ac-403c-bb6d-fb61aa6a83fc` est restée OUVERTE.**
   `client_key = brain-v42-session-2026-08-10`, `started_focus_revision = 200`.
   Elle vit en base, la bascule ne l'a pas touchée. La fermer ou la reprendre est
   une commande explicite de l'utilisateur — ne rien faire sans elle.

---

## 1. Ce qui a été livré, et qui tourne

**Commit `8ed57969`** — `feat(provenance): identité de transport`. Déployé et vérifié
en production le 2026-08-10 vers 21:15.

Le problème résolu : le panneau brain agrégeait par acteur, or l'acteur est le
basename du `cwd` (`X-Brain-Agent: ${PWD}`). Les quatre moteurs Claude mesurés ce
jour tournent dans le même répertoire et s'effondraient en **une seule ligne**.

Ce qui change : le serveur frappe un `Mcp-Session-Id` (`stateless_http=False`), le
middleware le lit, il traverse le fil dans un champ **distinct** de `session`, et le
registre en fait une ligne `kind="transport"`.

Contrôles passés sur la production vivante :

| Contrôle | Résultat |
|---|---|
| Le serveur frappe un sid | 32 hex (`uuid4().hex`) |
| Sid inventé | **404** |
| `tools/list` sans sid | **400** |
| Sans bearer | **401** |
| Échéance d'inactivité | `session_idle_timeout seconds=900.0` |
| `Terminating session: None` | **1732 avant → 0 après** |
| Jeton dream | 200, sid frappé |
| Deux connexions d'un acteur | **2 lignes** `transport-…`, 2 et 4 appels |

Suite complète : 7416 passés, mypy Success, ruff clean.
`test_container_image_pins` échoue — **pré-existant**, vérifié par stash.

### Rollback, si jamais

```bash
mkdir -p ~/.config/systemd/user/brain-mcp-http.service.d
printf '[Service]\nEnvironment=MCP_HTTP_STATELESS=true\n' \
  > ~/.config/systemd/user/brain-mcp-http.service.d/stateless.conf
systemctl --user daemon-reload && systemctl --user restart brain-mcp-http
```

Le commit reste ; seul le réglage décide. Aucune migration, aucune écriture
persistante n'est engagée par ce chantier.

---

## 2. Rotation des jetons dream — faite

60 profils refrappés après une fuite en transcript (5 fichiers, tous `0600`, tous
sous `wf_6370847b-70e`). Registre `0d8b13378932` → `738d1e8ff170`, zéro jeton
recyclé. Prouvé par quatre sondes dont **deux négatives** (ancien jeton → 401).

- Sauvegarde : `~/.config/brain-v42/mcp-token.env.bak-20260810-203428`
- Runbook complet : brain `da84204f`
- `MCP_HTTP_TOKEN` (admin) **n'a pas été tourné** — voir ticket `842d1bb4`.

---

## 3. Ce qui vient ensuite — la jointure OTLP

**La conception est tranchée et prouvée. Il reste à l'écrire.**

Chaîne établie en direct le 2026-08-10 : l'attribut OTLP `conversation.id` de Codex
**est** son `session_id`, identique au `session_meta.session_id` du rollout et au nom
du fichier. Mesuré sur `019fecfb-2ecc-7a71-b26d-0aeefb5230b8`.

Et 37 `client_key` de sessions brain portent déjà l'UUID d'une vraie session Codex.
La clé de jointure existe donc **des deux côtés** ; personne ne les avait reliées.

### Ce qu'il faut écrire

Un **paramètre explicite** `agent_conversation_id` sur `brain_session_start`,
validé comme UUID canonique, persisté (migration 045), et reporté au sidecar pour
que `_session_key()` calcule la même clé agent-neutre que le côté OTLP.

### Ce qu'il ne faut SURTOUT PAS faire

Gratter l'UUID dans `client_key` au regex. Chiffre qui tranche : **114** `client_key`
portent un UUID canonique, **37 seulement** correspondent à une session Codex. Les
**78 autres** sont des UUID `red-mission`, `red-worker`, etc. Le grattage produirait
78 jointures fausses et silencieuses.

### Deux propriétés à respecter dans la conception

- La relation est **N sessions brain → 1 conversation** (mesuré : 3 `client_key`
  distinctes sur `019fec5a-…`). Ne pas la supposer injective.
- Un seul `codex exec` émet **DEUX** `conversation.id`, dont un seul persiste. Il
  restera des lignes OTLP fantômes non joignables — c'est le régime, pas un bug.

### Ce que la jointure apporte que le transport n'apporte pas

Le transport **sépare** les lignes ; il ne les **nomme** pas. Un `transport-0ae…f9e5`
n'est rattachable à aucun pid, aucune tâche, aucun onglet. La session brain, elle,
porte un projet, une `client_key` et un focus. Les deux sont complémentaires.

---

## 4. Les entrées brain à relire

| Type | Sujet | id |
|---|---|---|
| Décision | Voie de jointure : paramètre explicite, pas grattage | `4890a475` |
| Décision | Rotation des jetons dream, admin conservé | `ac75678e` |
| Learning | `conversation.id` OTLP == session_id Codex (chaîne prouvée) | `3747bb5e` |
| Learning | Le hook `SessionStart` ne peut pas déclarer la session (réfuté) | `06332ea4` |
| Learning | `access_log` est une file de ~5 min, pas un journal | `1de79d26` |
| Learning | Fenêtre 60 s des métriques : 2 appelants sur 3 invisibles | `5bd39821` |
| Learning | Deux unités systemd + bascule casse les connexions vivantes | `896d1e35` |
| Runbook | Tourner le registre dream sans casser la nuit (10 étapes) | `da84204f` |

### Tickets ouverts

- **`40dbfeb1` → red-monitor** : séparer les deux sources en trois tableaux, re-trier
  globalement, renommer `brain_calls` en « tentatives », étiqueter les seaux.
  **L'utilisateur lance cette session lui-même** — lui donner le contexte, ne pas
  démarrer sans lui.
- `842d1bb4` → brain-v42 : `MCP_HTTP_TOKEN` en clair dans 13 transcripts, 4 projets.
- `d2a669c6` → brain-v42 : `collector_db.py:137`, fenêtre 60 s contre purge 1 h.
  **Une constante**, et le panneau passe de 1 agent à 3-4.

---

## 5. Les pièges qui ont coûté du temps ce soir

1. **La chaîne traverse DEUX unités systemd.** Le format de fil et le registre vivent
   dans `brain-metrics.service`, pas dans `brain-mcp-http.service`. Redémarrer le MCP
   seul laisse le sidecar décoder avec l'ancien schéma — et comme son décodeur ignore
   les clés inconnues **par conception**, il jette le champ en silence. Symptôme : une
   ligne `unattributed calls=6` au lieu de deux lignes `transport` à 2 et 4. Réflexe :
   comparer l'âge des **deux** processus à celui du commit.

2. **Basculer en mode avec état casse les connexions déjà établies**, et le client ne
   s'en remet pas. Un client connecté avant reçoit `400 Missing session ID` puis
   `-32602`. L'étude avait mesuré une récupération sur **404** ; ce chemin-là est un
   **400**, et il ne déclenche pas la même reprise. Prévoir la reconnexion.

3. **Un pseudonyme d'affichage n'est pas une clé de jointure.** J'ai comparé
   `codex-8f05…` (38 car., pseudonyme HMAC) au plafond de 36 de `normalize_session` et
   j'en ai tiré une conclusion fausse. La vraie clé est le HMAC du UUID.

4. **Le registre de capacités a des clés PLATES** `"projet:phase"`, et certains projets
   contiennent eux-mêmes un `:` (`red-shrik:agent`). Un `split(':')` naïf rend un faux
   verdict « matrice incomplète ». Utiliser `rpartition(':')`.

5. **Substituer un symbole dans un module tiers fuit entre les tests.** Mon injection
   de `session_idle_timeout` faisait échouer cinq tests qui passaient isolément. D'où
   `tests/unit/mcp/conftest.py` et l'idempotence de l'installation.

---

## 6. Ce qui reste NON MESURÉ

1. **Une phase dream réelle de bout en bout sous mode avec état.** J'ai prouvé qu'un
   bearer dream obtient un sid ; pas qu'une phase complète va au bout. C'était déjà
   l'inconnue n°1 de l'étude, elle le reste. L'utilisateur a accepté le risque
   (« au pire on la rejoue en manuel »).
2. Le nombre de sessions par invocation Claude Code — 1 ou 2 ? Deux lentilles se
   contredisent. Facteur 2 sur le nombre de lignes du panneau.
3. Le comportement de moteurs **interactifs** sur plusieurs heures : combien de
   reconnexions par jour ? Décide si le panneau montre 4 lignes ou 12.
4. Le coût mémoire par session sur le vrai catalogue de production (~142 kB estimé,
   mesuré sur un FastMCP nu à 55 kB). À surveiller : la prod grossissait déjà de
   ~16 MB/h **avant** ce chantier, en mode sans état.
5. `len(_server_instances)` n'est **pas** exposé dans `/metrics` : l'efficacité du TTL
   de 900 s n'est donc pas observable en production. À livrer avant de faire confiance
   à l'échéance.

---

## 7. Contexte de la nuit du 2026-08-11

Première nuit à **dix projets** avec le scope serveur armé, départ **06:01**. Le
registre a été refrappé ce soir, donc les phases prendront les nouveaux jetons au
démarrage (elles lisent l'`EnvironmentFile`). REORG est en **DRY**.

Le contrôle du matin est le **nombre d'insights par projet**, pas la couleur de
l'unité — une phase peut « réussir » sur du vide si le scope lui cache tout. Lire le
contenu du rapport, groupé par projet.

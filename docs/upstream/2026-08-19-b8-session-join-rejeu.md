# Re-jeu du spike B8 — jointure de session Claude Code

**Date** : 2026-08-19
**Version mesurée** : **Claude Code 2.1.234**
**Spike d'origine** : `docs/upstream/2026-08-06-claude-otlp-session-join.md`
(version mesurée 2.1.220)
**Verdict** : **INCHANGÉ — JOINTURE TOUJOURS IMPOSSIBLE**

## Pourquoi ce re-jeu

Le spike d'origine conclut lui-même : « Re-mesurer à chaque montée de version de
Claude Code plutôt que de reprendre cette conclusion sur parole. » Ses deux relais
(`2bd14b24`, `7ffe0e8a`) répètent la consigne mot pour mot. B8 était donc cotée
« Haute (contrainte) » sur une mesure de **quatre versions mineures** d'écart, et
aucune phase du plan de refonte ne la rejouait.

Ce re-jeu est le contenu n° 6 de la Phase 0 du PLAN. Il a été exécuté **en premier**,
avant tout autre travail de Phase 0, pour une raison précise : le cadrage du
2026-08-19 a tranché **Q9** (les subagents héritent) et fixé la **clé d'ouverture
automatique sur `(projet, connexion)`** en s'appuyant sur la prémisse « `X-Brain-Session`
est mort ». Si la mesure avait changé, ces deux décisions se rouvraient.

Le volet OTLP du spike d'origine n'est **pas** rejoué : il n'entre dans aucune décision
de ce plan.

## Méthode

Protocole du spike d'origine, question 1 seule. Un récepteur jetable sur
`127.0.0.1:4318` journalise les en-têtes de toute requête ; `claude -p` lance une vraie
session Claude Code en sous-processus avec `--mcp-config` dédié et
`--strict-mcp-config`.

Configuration MCP du spike — `${PWD}` sert de **témoin** : si lui s'expanse, le
mécanisme d'expansion fonctionne et seule la variable est en cause.

```json
{ "mcpServers": { "spike": {
    "type": "http", "url": "http://127.0.0.1:4318/mcp",
    "headers": { "X-Brain-Session": "${CLAUDE_CODE_SESSION_ID}",
                 "X-Brain-Agent":   "${PWD}" } } } }
```

**Les deux cas sont joués, et c'est essentiel.** Le spike d'origine note que son
premier run « semblait concluant ; il ne l'était pas » — l'environnement de la session
appelante fuyait dans le sous-processus et y injectait le **mauvais** identifiant. Un
re-jeu qui ne testerait que l'environnement courant reproduirait le faux positif.

## Résultat

| Environnement parent | `X-Brain-Session` reçu | Verdict |
|---|---|---|
| `CLAUDE_CODE_SESSION_ID` **présent** | `630e63c7-eb36-5491-8858-b48c70b46532` — **l'identifiant du PARENT**, celui de la session appelante | Faux positif, reproduit à l'identique |
| `CLAUDE_CODE_SESSION_ID` **retiré** (`env -u`) | `${CLAUDE_CODE_SESSION_ID}` — **littéral non expansé** | Cas nominal |

Témoin, dans les deux cas : `X-Brain-Agent` reçoit `/tmp/b8spike`, c'est-à-dire `${PWD}`
correctement expansé. **Le mécanisme d'expansion fonctionne ; c'est la variable qui
n'existe pas au moment où la configuration MCP est lue.** Claude Code ne se pose pas
`CLAUDE_CODE_SESSION_ID` à lui-même avant cette lecture.

Passage par les normaliseurs du serveur, vérifié le même jour :

| Entrée | Fonction | Sortie |
|---|---|---|
| `${CLAUDE_CODE_SESSION_ID}` | `normalize_session` | `None` |
| `630e63c7-…` (id du parent) | `normalize_session` | `630e63c7-…` — **accepté, et c'est le danger** |
| `/tmp/b8spike` | `normalize_agent` | `b8spike` |

## Conséquences

1. **B8 cesse d'être « cotée sur une mesure périmée ».** C'est l'issue (a) prévue par le
   PLAN : rien n'est invalidé, l'accrétion continue telle quelle. La ligne
   `unattributed` reste le cas **nominal**, pas un cas dégradé théorique.
2. **Les décisions du cadrage du 2026-08-19 TIENNENT** : Q9 (héritage des subagents) et
   la clé d'ouverture `(projet, connexion)` reposaient sur cette prémisse. Elles sont
   confirmées par la mesure, pas seulement par l'argument.
3. **Confirmation utile pour le cadrage** : `normalize_agent('/tmp/b8spike')` rend
   `b8spike`, le **basename du répertoire**. `X-Brain-Agent` porte donc bien le PROJET,
   et non l'agent — ce qui est exactement pourquoi il ne peut pas distinguer un subagent
   de son porteur.
4. **Le faux positif est un piège actif, pas historique.** Un `X-Brain-Session` reçu et
   *valide* n'est pas une preuve de jointure : `normalize_session` l'accepte alors qu'il
   désigne le parent. Toute future tentative de jointure doit tester le cas
   environnement-retiré, sinon elle rattachera silencieusement toutes les sessions filles
   à leur parent.

## Portée de cette mesure

Datée et **périssable**, comme celle qu'elle remplace. Elle vaut pour Claude Code
**2.1.234** et pour rien d'autre. À rejouer à la prochaine montée de version — la
consigne du spike d'origine n'est pas levée par ce re-jeu, elle est honorée par lui.

*Lecture seule : aucune écriture DB, aucun commit, aucun fichier du dépôt touché hors
ce document. Artefacts du spike sous `/tmp/b8spike/`, récepteur arrêté et port 4318
libéré après mesure.*

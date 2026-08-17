# Provenance du corpus — distinguer le métabolisme du dream de l'activité humaine

**Date** : 2026-08-06
**Statut** : design validé, plan d'implémentation à écrire
**Périmètre** : chantier A d'un lot de quatre (voir « Hors périmètre »)

## Le problème, mesuré

Trois défaillances observées en production le 2026-08-06 :

1. **PROMOTE tourne en boucle depuis 23 nuits.** Le learning `1d1037e8`
   (« brain-v42 architecture overview ») est évalué chaque nuit depuis le
   2026-07-07 et rend le même verdict `classification_uncertain`. 22 lignes dans
   `dream_promotions`. Sur l'ensemble des rapports PROMOTE : 64
   `classification_uncertain` contre 18 promotions réelles.

2. **Le gate préflight ne se déclenche plus.** Construit pour épargner ~40 % des
   nuits de phases deep, il a rendu **48 RUN pour 2 SKIP**. Dernier SKIP le
   2026-07-09.

3. **`access_count` est contaminé.** Le filtre de maturité de PROMOTE
   sélectionne sur `access_count >= 3`, et le dream incrémente lui-même ce
   compteur en paginant le corpus chaque nuit (REORG : 63 à 106 tool calls).

### Cause racine unique

`promote_prepare.py` porte un cache anti-rejugement qui exclut un learning déjà
jugé incertain sur sa version courante, via `u.created_at >= l.updated_at`. Ce
cache ne peut structurellement pas tenir :

```
verdict PROMOTE       2026-08-06 04:06:13 UTC
learnings.updated_at  2026-08-06 04:08:51 UTC   ← postérieur, donc réadmis
```

`trg_learnings_updated` est un `BEFORE UPDATE` **inconditionnel**, et
`decay_flusher` écrit `UPDATE learnings SET access_count=…, last_accessed_at=…`,
une écriture de compteur pur. **Lire une entité suffit donc à rajeunir son
`updated_at`** et à invalider le verdict rendu deux minutes plus tôt.

Le dépôt connaît déjà ce défaut et l'a contourné à un seul endroit —
`repositories/pg_learning.py:8` : *« validate() stays local: it must update
validated_at WITHOUT bumping updated_at »*. La migration 040 a résolu le même
problème pour `project_contexts` avec `focus_updated_at`. Le concept est acquis,
il n'a jamais été posé au niveau du schéma des entités.

## Modèle : deux axes orthogonaux

Le système doit répondre à deux questions distinctes, que `updated_at` confond :

- « **Qui** a touché cette ligne ? » → axe *acteur*
- « Est-ce le **contenu** qui a changé, ou seulement un compteur ? » → axe
  *nature de l'écriture*

| Consommateur | Question réelle | Axe |
|---|---|---|
| Cache anti-rejugement PROMOTE | le contenu a-t-il changé depuis mon verdict ? | nature |
| Filtre de maturité PROMOTE | des **humains** lisent-ils ce learning ? | acteur |
| Gate préflight | le corpus a-t-il bougé pour une raison **autre que moi** ? | acteur |

## Terrain existant

L'identité de l'appelant **arrive déjà au serveur** et n'est simplement jamais
écrite :

- `scripts/dream/codex_runner.py:262` envoie `X-Brain-Agent: dream-codex-<phase>`.
- Les sessions Claude Code interactives envoient `${PWD}`.
- `metrics/instrument.py:_normalize_agent()` normalise déjà ces valeurs
  (`/home/u/git/red-lab` → `red-lab`, `${…}` non expansé → `_unexpanded`,
  vide → `unknown`).

Ce signal ne sert qu'au collecteur de métriques. `access_log` n'a pas de colonne
acteur ; `decay_flusher` incrémente `access_count` sans savoir qui a lu.

**La provenance n'est pas à construire, elle est à brancher.**

Réserve : le header est déclaré par le client, donc falsifiable
(`metrics/collector.py:100` le dit et plafonne la cardinalité). Même posture que
le `client_key` de session : *déclarée, pas prouvée*. Signal d'hygiène, pas
frontière de sécurité.

## §1 — Middleware : remplacer le monkey-patch

`brain_tools.py:106-122` réassigne `mcp.tool` pour envelopper les tools de
métriques. Trois défauts :

- conditionné à `metrics_collector is not None` — métriques désactivées, plus
  aucun tool instrumenté ;
- dépendant de l'ordre : seuls les tools déclarés après la ligne 122 sont
  couverts ;
- mutation d'une méthode d'objet tiers, fragile en montée de version.

FastMCP 3.4.2 (installé) expose le point d'extension prévu :
`FastMCP.add_middleware()` avec un hook `on_call_tool`. Un **middleware unique**
porte les deux préoccupations :

```
on_call_tool:
    poser le ContextVar acteur      ← inconditionnel, toujours
    si collecteur actif : mesurer   ← conditionnel, à l'intérieur
```

Le couplage aux métriques disparaît par construction, la dépendance à l'ordre
aussi, et on retire un monkey-patch au lieu d'en ajouter un second.

Contraintes : préserver exactement la capture d'`AuthorizationError` et la
mesure de latence de `instrument_tool`. Ne pas toucher `instrument_embedding`
ni `instrument_reranker`, qui ne sont pas des tools.

**Première tâche du plan** : prouver que `get_http_headers()` est joignable
depuis `on_call_tool`. Si ce n'est pas le cas, la lecture du header reste où
elle est et le middleware ne fait que propager la valeur.

Étape **détachable** : elle ne touche à aucun schéma. Si elle dérape, on la
retire sans perdre le reste du chantier.

## §2 — Migration 041

Aucun backfill. `0` et `NULL` signifient « jamais mesuré », discipline héritée
de la 040.

**a)** `access_log.actor VARCHAR(64) NOT NULL DEFAULT 'unknown'`, alimenté par
`_normalize_agent()`.

**b)** `access_count_human INTEGER NOT NULL DEFAULT 0` sur `learnings`,
`decisions`, `snippets`, `runbooks`, `adrs`. `access_count` reste le total et ne
change pas de sémantique : aucun consommateur existant ne casse.

**c)** `content_updated_at TIMESTAMPTZ NULL` sur les cinq mêmes tables. La
migration crée une fonction `stamp_content_updated_at()` et **un trigger par
table** (cinq au total), chacun **conditionnel sur le changement de valeur** et
portant la liste de colonnes propre à sa table. Exemple pour `learnings` :

```sql
CREATE TRIGGER trg_learnings_content_updated
  BEFORE UPDATE OF topic, insight ON learnings
  FOR EACH ROW
  WHEN (OLD.topic   IS DISTINCT FROM NEW.topic
     OR OLD.insight IS DISTINCT FROM NEW.insight)
  EXECUTE FUNCTION stamp_content_updated_at();
```

Colonnes de contenu par table (mesurées) :

| Table | Colonnes de contenu |
|---|---|
| `learnings` | `topic, insight` |
| `decisions` | `title, description, reasoning, consequences` |
| `snippets` | `title, code` |
| `runbooks` | `title, description, trigger, steps` |
| `adrs` | `title, context, decision, consequences` |

`tags`, `project_key`, `freshness_status` et les compteurs sont **hors** de cet
ensemble : REORG qui normalise un tag ne rajeunit pas le contenu. Comportement
voulu.

### Divergence assumée d'avec la migration 040

La 040 écrit `focus_updated_at` depuis le code applicatif, « jamais par un
trigger ». Ce design fait l'inverse, pour trois raisons :

1. La clause `WHEN … IS DISTINCT FROM` donne exactement la sémantique de valeur
   que la 040 cherchait : réécrire le même texte ne rajeunit rien, un recopiage
   reste visible. C'était l'argument contre le trigger ; il tombe.
2. Le focus a **un** écrivain. Le contenu des entités en a beaucoup :
   `brain_learn`, `brain_update`, REORG, les merges de CLEAN, les scripts de
   backfill. Une discipline applicative sur N écrivains sera oubliée par le
   N+1 — c'est littéralement ce qui s'est produit ici.
3. Un trigger se vérifie en une requête ; une discipline applicative se vérifie
   en relisant tout le code.

## §3 — Classification de l'acteur

Fonction pure unique, `brain_v42/provenance.py : is_human_actor(actor) -> bool`,
couverte par un test unitaire énumérant les cas :

| Acteur | Humain ? | Motif |
|---|---|---|
| `dream-codex-<phase>` | non | le dream se déclare |
| `unknown` | non | fail-closed : un appelant non identifié ne débloque rien |
| `_unexpanded` | non | session démon sans `PWD` |
| tout le reste | oui | session interactive → basename du `PWD` |

## §4 — Chemin de la donnée et agrégation

### Où l'acteur est lu — piège à éviter

L'acteur doit être lu **au moment de la mise en file**, dans
`AccessLogger.log_access()`, et stocké dans l'événement mis en queue :

```
middleware on_call_tool   → pose le ContextVar (contexte de requête)
log_access()              → LIT le ContextVar, l'attache à l'événement   ← ici
_flush_batch()            → insère l'événement, ContextVar hors de portée
```

`_flush_batch()` s'exécute dans une tâche de fond (`_run_loop`, toutes les 5 s),
**hors du contexte de requête** : y lire le ContextVar rendrait `unknown` pour
tout le monde. Les 6 sites d'appel de `log_access` restent inchangés — seule
l'implémentation de la méthode évolue.

### Agrégation

`decay_flusher` agrège aujourd'hui `access_log` par
`(entity_type, entity_id)` → `count` + `max(accessed_at)`. Il agrège désormais
aussi `count_human`, et écrit les deux compteurs :

- `access_count += count` (inchangé)
- `access_count_human += count_human`

## §5 — Recâblage des trois consommateurs

### a) Cache anti-rejugement — `promote_prepare.py`

```sql
u.created_at >= COALESCE(l.content_updated_at, l.created_at)
```

Le repli sur `created_at` — **pas** sur `updated_at` — est le point délicat.
Sans backfill, `content_updated_at` est `NULL` partout ; un repli sur
`updated_at` reproduirait le bug à l'identique. `created_at` dit la seule chose
qu'on sache : *le contenu n'a jamais été observé changer, il a donc l'âge de la
ligne.* C'est un fait mesuré, pas une valeur fabriquée.

Effet immédiat : verdict du 2026-08-06 ≥ `created_at` du 2026-03-23, donc
`1d1037e8` sort du pool dès la première nuit.

**Angle mort assumé** : une ligne dont le contenu a réellement changé *avant* la
migration mais *après* son dernier verdict sera exclue à tort. Elle se corrige
d'elle-même à la première édition suivante.

### b) Filtre de maturité — `promote_prepare.py`

`l.access_count >= 3` devient `l.access_count_human >= 3`.

**Conséquence à annoncer** : le pool de PROMOTE sera vide pendant un moment, le
temps que des lectures humaines s'accumulent depuis zéro. Ce n'est pas une
régression mais le résultat correct — on ignore qui a lu quoi avant la
migration. Rien n'est perdu : PROMOTE ne produit rien depuis 23 nuits. Le
chantier C remplacera de toute façon cette porte par le verdict de revue.

### c) Gate préflight — `scripts/dream/dream_preflight.py`

Deux changements :

1. `greatest(created_at, updated_at)` → `greatest(created_at, content_updated_at)`,
   ce qui élimine le bruit des écritures de compteur ;
2. exclure les entités taguées `dream:generated` du signal de mutation. Sans
   cela, SYNTH garantit en créant 3 insights que la nuit suivante synthétisera
   par-dessus sa propre production — l'echo-drift au niveau de l'ordonnanceur.
   Vérifié : les cinq tables portent une colonne `tags`.

Le caractère fail-safe du gate est conservé : toute erreur ou incertitude imprime
`RUN`.

## §6 — Rollout et vérification

TDD strict (exigence CLAUDE.md) : test rouge d'abord à chaque étape.

Trois étapes indépendantes, dans cet ordre :

1. **Middleware** — aucun schéma touché, détachable.
2. **Migration 041** — colonnes et triggers, aucun backfill.
3. **Recâblage** des trois consommateurs.

### Critères d'acceptation

| Ce qu'on prouve | Comment |
|---|---|
| L'acteur arrive | `select actor, count(*) from access_log group by 1` après une nuit → `dream-codex-*` présent |
| La boucle s'arrête | `1d1037e8` absent de la sortie de `promote_prepare` |
| Le contenu ne bouge plus pour rien | après une nuit de REORG, `content_updated_at` inchangé sur les lignes dont seuls les tags ont bougé |
| Le gate revit | taux de SKIP sur 2 semaines, contre la ligne de base **2/50** |

### Garde-fous

- **Aucun killswitch ni variable d'environnement du dream n'est modifié.** Le
  pipeline garde sa configuration exacte, pour ne pas confondre l'effet de ce
  changement avec autre chose.
- La 041 ne fait aucun backfill : son downgrade est une simple perte de
  colonnes, sans arbitrage fail-closed à concevoir.

## Limites assumées

- **`access_log` est purgée après agrégation.** Si la règle de classification
  humain/système change plus tard, `access_count_human` ne pourra pas être
  recalculé. C'est le prix de la dénormalisation, accepté en connaissance de
  cause ; le journal durable fait l'objet d'un ticket de report.
- **Le header `X-Brain-Agent` est déclaré par le client.** La provenance est un
  signal d'hygiène, pas une frontière de sécurité.
- **Aucun backfill** : les entités existantes démarrent à
  `access_count_human = 0` et `content_updated_at = NULL`.

## Hors périmètre

Chantiers du même lot, à spécifier séparément :

- **B** — surface de revue pour les 86 insights SYNTH : table de verdicts à trois
  états (garder / rejeter / promouvoir), tool dédié, section `### À revoir` dans
  le briefing.
- **C** — porte de PROMOTE : le verdict de revue remplace le filtre d'origine
  d'ADR #4. Le garde-fou anti-echo-drift n'est pas levé mais remplacé par une
  version qui mesure ce qu'il visait — l'endossement humain plutôt que
  l'origine.
- **D** — routage projet de SYNTH : retirer le `project_key="brain-v42"` codé en
  dur dans `phase_synth.md`. Les 86 insights sont tous sous `brain-v42` alors que
  plusieurs portent sur d'autres projets ; la garde du commit `87389e6d` rejette
  déjà les clés inconnues. Prérequis des sessions de revue par projet.

Ticket de report à ouvrir : **journal d'accès durable** (rétention d'`access_log`
avec acteur, compteurs dérivés à la demande), pour mesurer l'usage réel du
corpus — notamment « ces insights, un humain les a-t-il lus ? ».

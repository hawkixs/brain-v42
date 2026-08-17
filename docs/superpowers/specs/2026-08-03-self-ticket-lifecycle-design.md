# Lifecycle des self-tickets — design

**Date** : 2026-08-03
**Statut** : validé, prêt pour plan d'implémentation
**Prior art** : `3ecb4a91` (exclusion des self-tickets du groupe `en_attente`)

## 1. Problème

`brain-v42` porte 59 tickets non terminaux, dont **46 self-tickets** (`from_project == to_project`) :

| Statut | Self | Cross | Total |
|---|---|---|---|
| `open` | 23 | 6 | 29 |
| `resolved` | 11 | 5 | 16 |
| `in_progress` | 12 | 1 | 13 |
| `wontfix` | 0 | 1 | 1 |

La machine à états a été conçue pour une coordination **inter-projets à deux parties** :
un exécutant résout, un demandeur confirme. L'usage réel est à 78 % mono-partie.

### 1.1 Le contrôle de rôle est inopérant

`src/brain_v42/services/ticket_service.py:125` :

```python
expected = ticket.to_project if role == "executor" else ticket.from_project
if author != expected:
    raise NotAllowedError(...)
```

Quand `from_project == to_project`, les deux branches renvoient la même valeur. Le contrôle
passe donc toujours, quel que soit le rôle exigé par la transition. Il ne contraint rien tout
en prétendant contraindre — le pire des deux mondes, parce qu'un lecteur du code croit à une
garantie qui n'existe pas.

### 1.2 La boucle de confirmation est de la cérémonie

`TERMINAL_STATUSES = {CLOSED, ACKED}`. Un ticket `resolved` compte encore comme du travail
ouvert et exige un `confirm` du demandeur pour atteindre `closed`.

Sur un self-ticket, ce `confirm` est le même agent confirmant sa propre résolution : zéro
information ajoutée, une transition obligatoire de plus. C'est là que les tickets s'échouent.
Un agent résout, considère la tâche finie, et le système la compte encore à traiter.

### 1.3 Le symptôme a déjà été patché, pas la cause

`3ecb4a91` (2026-08-01) a exclu les self-tickets du groupe `en_attente` de la requête de
briefing, parce qu'« en attente de l'autre partie » n'a pas de sens quand on est les deux
parties. Le patch portait sur l'affichage ; la machine à états n'a pas bougé, et les
self-tickets sont restés dans le groupe `_CONFIRMABLE`.

### 1.4 Vérification des 11 bloqués

Les 11 self-tickets en `resolved` ont tous été résolus le 2026-08-02, en un seul lot.
Croisés avec les commits de la période, **10 ont une livraison identifiable** :

| Ticket | Commit |
|---|---|
| `brain_workflow_guide` (prototype et design) | `8c5a24a2`, `3ce98056`, `d27e2a73` |
| Baseline MCP ToolError | `9c8aedbf` |
| 7 tests d'intégration ToolError | `79068186` |
| Pin aiohttp `embedding_supervisor` | `320bb328` |
| Gate catalogue hermétique | `2277efce` et suivants |
| CI 4300 zero-norm | `3686805f` |
| Faux dry-run systemd | `5f88e86b` |
| `render_parent` / `render_dir` | `ac7f501e` |
| Trim des output schemas session | `4721c74e`, `1cea2f4c` |

Le onzième — « Admission live séparée : prouver backfill et deux canaries Dream » — porte sur
une preuve en production, pas sur du code. C'est un cas légitime de « fait mais pas encore
vérifié », et il justifie à lui seul de conserver l'état `resolved` comme option explicite
plutôt que de le supprimer.

## 2. Décisions de cadrage

**`resolve` ferme directement sur un self-ticket**, et une action explicite permet de
s'arrêter à `resolved`. Le cas courant devient gratuit, le cas utile reste exprimable.

**Pas de `wontfix_pending`.** Un `wontfix` est une décision, pas une livraison : il n'y a rien
à vérifier ensuite.

**Les 11 existants ne sont pas fermés par cette livraison.** Ils sont listés et vérifiés
(§1.4) ; leur sort est une décision opérateur distincte du code.

## 3. Blast radius

`impact({target: "allowed_actions", direction: "upstream"})` retourne **HIGH** : 6 symboles,
3 processus, 2 modules, `direct: 3`.

| Appelant | Usage |
|---|---|
| `mcp/tools/ticket_tools.py` → `brain_ticket_get` | expose les actions légales à l'agent |
| `mcp/tools/ticket_tools.py` → `brain_ticket_transition` | construit le message d'erreur |
| `codex_gateway/ticket_routes.py` → `transition_ticket` | corps de la réponse **409** |

Les trois disposent du ticket au point d'appel, donc `from_project == to_project` est
calculable partout sans plomberie supplémentaire.

Le troisième est une **surface HTTP externe**, consommée par `red-codex`. La forme de la
réponse ne change pas — `allowed_actions` reste une liste de chaînes — mais ses valeurs
peuvent désormais inclure `resolve_pending` sur un self-ticket.

## 4. Conception

### 4.1 Une seconde table, sans rôle

`SELF_TRANSITIONS` est consultée quand `from_project == to_project`. Elle mappe
`(kind, status, action) -> new_status`, **sans champ `Role`**.

C'est ce qui corrige §1.1 : sur un self-ticket il n'y a qu'une partie, donc le contrôle de
rôle est sauté explicitement au lieu d'être exécuté pour toujours passer.

| État | Action | → | Note |
|---|---|---|---|
| `open` | `start` | `in_progress` | inchangé |
| `open`, `in_progress` | `resolve` | **`closed`** | le défaut |
| `open`, `in_progress` | `resolve_pending` | `resolved` | « fait, reste à vérifier » |
| `open`, `in_progress` | `wontfix` | **`closed`** | |
| `resolved` | `confirm` | `closed` | sortie du `resolve_pending`, et ferme les 11 existants |
| `resolved` | `reopen` | `open` | |
| `open`, `in_progress`, `resolved`, `wontfix` | `cancel` | `closed` | |
| `wontfix` | `confirm`, `reopen` | `closed`, `open` | inertes aujourd'hui (0 ligne) |

Les deux dernières entrées sont conservées délibérément : aucun self-ticket n'est en `wontfix`
aujourd'hui, mais un ticket peut y atterrir entre cette livraison et son déploiement. Sans
elles, cet état deviendrait une impasse.

**`SELF_TRANSITIONS` doit être complète, `fyi` compris.** La table est consultée *à la place*
de `TRANSITIONS` dès que `from_project == to_project` : si elle ne contenait que les entrées
`request`, un self-ticket `fyi` n'y trouverait aucune règle et deviendrait intransitionnable.
Elle reprend donc `open -> acked` et `open -> closed` (cancel) à l'identique, sans le champ
`Role`. Le comportement de `fyi` ne change pas — il n'a jamais eu de boucle de confirmation —
mais il doit être représenté.

Un test doit verrouiller cette complétude : pour chaque `(kind, status)` atteignable, la table
self expose au moins une action tant que le statut n'est pas terminal.

### 4.2 `TicketAction` gagne `RESOLVE_PENDING`

Une action distincte plutôt qu'un paramètre booléen, parce que `allowed_actions` la rend
**découvrable**. Un agent qui lit un self-ticket `open` verra
`["cancel", "resolve", "resolve_pending", "start", "wontfix"]` et comprendra les deux
intentions sans documentation externe.

Un paramètre booléen serait invisible dans cette liste : il faudrait que l'agent l'ait lu
ailleurs. C'est précisément le mode de transmission qui échoue en pratique.

`resolve_pending` est absent de la table inter-projets et y reste donc **illégal** : un
`resolve` cross-projet mène déjà à `resolved` en attente du demandeur, la variante serait
redondante.

### 4.3 `allowed_actions` devient conscient du cas

```python
def allowed_actions(kind, status, *, self_ticket: bool = False) -> list[str]
```

Le défaut `False` préserve le comportement actuel : un appelant qui ne passe rien obtient la
table inter-projets. Chaque site d'appel opte explicitement, ce qui rend le changement
rétrocompatible et rend visible, à la revue, quels consommateurs ont été traités.

Le message de `IllegalTransitionError` doit passer le même drapeau, sans quoi il proposerait
des actions inapplicables au ticket concerné.

### 4.4 Ce qui ne change pas

Aucune migration : pas de changement de schéma.

Les tickets inter-projets conservent leur protocole à deux parties à l'identique, contrôle de
rôle compris. C'est le test de non-régression obligatoire.

Le groupe `_CONFIRMABLE` du briefing garde les self-tickets, et cette appartenance devient
**correcte** : après ce changement, un self-ticket en `resolved` y est arrivé par un
`resolve_pending` explicite, donc il attend réellement une vérification.

## 5. Tests

**Self-tickets**

1. `resolve` depuis `open` → `closed`
2. `resolve` depuis `in_progress` → `closed`
3. `resolve_pending` depuis `open` → `resolved`
4. `wontfix` depuis `open` → `closed`
5. `confirm` depuis `resolved` → `closed` — les 11 existants restent fermables
6. `reopen` depuis `resolved` → `open`
7. Aucun `NotAllowedError` possible quel que soit l'auteur déclaré

**Inter-projets, non-régression**

8. `resolve` → `resolved` inchangé
9. `resolve_pending` → illégal, avec les actions permises dans le message
10. Le contrôle de rôle rejette toujours le mauvais auteur

**Découvrabilité**

11. `allowed_actions` retourne deux listes différentes selon `self_ticket`

**Complétude de la table**

12. Pour chaque `(kind, status)` non terminal — `fyi` inclus — `SELF_TRANSITIONS` expose au
    moins une action. Sans ce test, un `fyi` self-ticket deviendrait intransitionnable au
    premier oubli d'entrée.

## 6. Hors périmètre

- **Le sort des 11 self-tickets `resolved`** — vérifiés en §1.4, décision opérateur séparée.
- **Les statuts de features** : trois jeux divergents (`VALID_FEATURE_STATUSES` à 7,
  `CREATABLE_FEATURE_STATUSES` à 6, `StatusEngine.STATUS_ORDER` à 6 et ordonné), plus un
  `legacy` cité en commentaire et absent partout.
- **L'asymétrie update manuel / signaux** : `feature_service.py:264` ne contrôle que
  l'appartenance, donc `deployed -> planned` passe manuellement alors que les signaux sont
  strictement monotones.
- **Le double archivage** : `status="archived"` et `archived=True` représentent le même état.

Ces trois derniers points forment le chantier suivant.

## 7. Critère de succès

Après livraison, `resolve` sur un self-ticket le ferme en un appel, `resolve_pending` reste
disponible et visible dans `allowed_actions`, et les tickets inter-projets se comportent
exactement comme avant, contrôle de rôle inclus.

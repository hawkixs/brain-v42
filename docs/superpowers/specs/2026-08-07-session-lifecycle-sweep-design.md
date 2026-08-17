# Cycle de vie des sessions — tarir les sessions fantômes

**Date** : 2026-08-07
**Statut** : design validé par l'opérateur, plan d'implémentation à écrire
**Ticket brain** : `2bd14b24-ccfe-4372-adf2-245b00304402` (idée, ouvert par red-shrik)
**Tickets voisins** : `7ffe0e8a` (auto-heartbeat), `2dfbb83d` (identité), `d04dc588` (checkpoint)
**Périmètre** : `brain_v42` seul, tous projets confondus

## Le problème, mesuré

Relevé le 2026-08-06 sur la production, pas supposé :

```
21 sessions status='open'   ·   17 STALE   ·   4 actives le jour même
```

Les 17 mortes s'étalent du 2026-07-13 au 2026-07-27, soit **10 à 24 jours**.
Leurs `client_key` disent d'où elles viennent : `codex-factory-28aeb338-*`,
`codex-github-migration-20260719-*`, `red-session-orchestrator-plan-*`,
`auto-discord-worktree-*`. Une par agent dispatché, aucune refermée.

Elles couvrent **neuf projets** — auto-discord, red-lab, red-arena, red-codex,
red-story, red-watcher, red-viewer, red-monitor, claude-dev-pc. Ce n'est pas un
défaut de brain-v42, c'est un régime de tout l'écosystème.

Le ticket d'origine rapportait 39 sessions nettoyées à la main la veille.
**17 s'étaient déjà réaccumulées.** Sans mécanisme, le ménage manuel est une
tâche perpétuelle.

### Les deux mensonges de `last_heartbeat_at`

Le même jour, le signal s'est trompé dans les deux sens :

- **Faux-mort** — la purge a abandonné une session VIVANTE (`9b6f7e18`, en plein
  chantier) parce que le heartbeat ne bouge que sur commande explicite.
- **Faux-vivant** — 17 sessions mortes depuis des semaines paraissent ouvertes,
  parce que **rien ne les ferme**.

Ces deux modes ont des causes distinctes. Le faux-mort vient du heartbeat
déclaratif ; le faux-vivant vient de l'absence d'état terminal automatique. Les
17 étaient correctement détectées `stale` — elles restaient simplement `open`.

### Ce qu'une garde technique ne peut pas faire

`is_human_actor` a été évaluée comme garde d'ouverture, et écartée sur mesure :

| Acteur observé | Classé |
|---|---|
| `brain-v42`, `red-shrik`, `codex` | humain |
| `dream-codex-*` | machine |
| `${PWD}` non expansé | `_unexpanded` → machine |

Elle sépare Dream du reste — ce pour quoi elle a été écrite — mais **pas un
opérateur d'un agent**, ni un parent de son subagent : un subagent hérite de la
config MCP de son parent, donc du même `X-Brain-Agent`. Une garde « seuls les
humains ouvrent une session » refuserait Codex et laisserait passer tous les
subagents Claude.

Les trois schémas d'identité coexistants confirment qu'aucun ne désigne une
session :

```
.mcp.json (projet)       X-Brain-Agent = "brain-v42"    une clé de projet
~/.codex/config.toml     X-Brain-Agent = "codex"        une identité de client
~/.claude.json (global)  X-Brain-Agent = "${PWD}"       un gabarit, expansion non prouvée
```

## Décisions

### D1 — Une session est une unité de travail, pas un acteur

Une session appartient à l'acteur qui a la durée de vie **et** le mandat de la
fermer. Les subagents n'en ouvrent aucune ; leur traçabilité passe par la
provenance par artefact, qui existe et fonctionne.

Fondement retenu par l'opérateur : **si un subagent consulte le brain, c'est
pour l'opérateur.** L'activité d'un subagent EST le travail de l'opérateur.

Cette formulation change la nature du problème d'attribution. Ce qui ressemblait
à une ambiguïté irréductible — subagent indistinguable de son parent — devient
la sémantique correcte : attribuer au couple *(projet, acteur)* désigne la
session de l'opérateur, et c'est le résultat voulu.

D1 n'est **pas applicable techniquement**, et ce n'est pas un oubli. C'est une
doctrine. Comme elle touche neuf projets, elle relève d'un ticket écosystème
distinct, hors de cette spec.

### D2 — L'état terminal d'une session morte est `abandoned`, jamais `ended`

`abandon` est déjà la sémantique exacte : il ne touche pas au focus, ne réclame
pas de summary, et conserve les captures et leur exclusivité.

Une session que personne n'a fermée n'a précisément **aucun jugement à
préserver**. L'auto-abandon nomme honnêtement ce qui s'est passé, là où un
résumé dérivé par le serveur ferait passer de l'état mesurable recopié pour du
jugement — ce que la doctrine du focus interdit explicitement.

### D3 — Pas de dépendance à l'auto-heartbeat

Le ticket d'origine ordonnait « `7ffe0e8a` d'abord », au motif qu'il faut une
staleness digne de confiance avant de fermer dessus. La mesure rend cette
dépendance inutile :

| Population | Âge |
|---|---|
| Fantôme le plus récent | 10 jours |
| Session vivante la plus ancienne | 0 jour |

**Un fossé de dix jours sépare les deux populations.** Un seuil à 7 jours
appliqué au `last_heartbeat_at` tel qu'il fonctionne aujourd'hui — explicite
seulement — attrape les 17 fantômes et épargne les 4 vivantes, avec de la marge
des deux côtés.

Prix assumé : un chantier dépassant 7 jours doit appeler `brain_session_heartbeat`
une fois. C'est un coût réel mais borné, contre une dépendance à une brique
d'identité dont le seul porteur connu (`X-Brain-Session`) est mesuré non
fonctionnel.

### D4 — Le balayage est une phase Dream, en DRY d'abord

Dream fournit déjà l'ordonnancement nocturne, l'idiome de killswitch, et la
trace consultable dans `dream_runs`. Surtout, il fournit l'idiome **DRY/WET**
éprouvé par les phases `reorg`, `extract` et `roadmap`.

Particularité assumée : cette phase est **déterministe et n'appelle aucun
modèle**. `dream_runs.model` sera `NULL` — forme déjà admise, observée sur
`extract` et sur le run `roadmap` du 2026-08-05.

## Mécanisme

| Élément | Valeur |
|---|---|
| Killswitches | `BRAIN_DREAM_SWEEP_ENABLED`, `BRAIN_DREAM_SWEEP_DRY_RUN` |
| Prédicat | `status = 'open' AND last_heartbeat_at < now() - interval '7 days'` |
| Action | transition vers `abandoned` |
| `abandonment_reason` | `auto_stale_7d` |
| Portée | tous les projets |
| Trace | une ligne `dream_runs`, phase `sweep`, `model` NULL |

Le `abandonment_reason` distinctif n'est pas décoratif : il garde l'abandon
automatique et l'abandon manuel **distinguables à jamais**, donc auditables
séparément. Un abandon manuel porte la raison écrite par l'opérateur ; celui-ci
porte une constante reconnaissable.

En mode DRY, la phase journalise exactement ce qu'elle **aurait** abandonné et
n'écrit **rien** dans `brain_sessions`.

## Sûreté et déploiement

1. **DRY plusieurs nuits.** Lire ce que la phase aurait abandonné et vérifier
   qu'elle ne vise que des fantômes.
2. **Re-mesurer le fossé avant le flip WET.** Ne jamais recopier les chiffres de
   cette spec : ils datent du 2026-08-06 et se périment. La requête de mesure
   fait partie de la procédure, pas de la documentation.
3. **Irréversibilité.** `brain_session_resume` exige `status='open'` : un abandon
   est terminal. Trois atténuations — seuil généreux, DRY préalable, et
   conservation des captures garantie par le contrat existant.
4. **Pas d'`unabandon`.** Tant que le DRY n'a produit aucun faux positif,
   construire une annulation serait résoudre un problème non observé.

## Amendement doctrinal

Le `CLAUDE.md` porte aujourd'hui une interdiction catégorique :

> Aucun hook, auto-close, livraison de travail ou fin de réponse ne ferme une
> session.

Cette spec la contredirait si elle n'était pas amendée explicitement. L'intention
d'origine visait **l'agent et le client** : empêcher qu'un process ferme une
session vivante et détruise le rituel de fin, seul moment où du jugement non
dérivable est écrit.

L'amendement doit donc être étroit et énoncé, pas glissé :

- l'interdiction reste entière pour l'agent et le client — `start`, `resume`,
  `end` et `abandon` restent des commandes explicites de l'opérateur ;
- le **serveur** peut abandonner une session sans signe de vie depuis 7 jours ;
- cet abandon automatique ne produit ni summary ni `next_focus`, et ne touche
  jamais le focus du projet.

## Tests

- **Unitaire, frontière du prédicat** : à N−1 jour la session n'est pas touchée,
  à N+1 elle est abandonnée. La frontière exacte, pas un cas au milieu.
- **Unitaire, DRY n'écrit rien** : en mode DRY, aucune ligne `brain_sessions`
  n'est modifiée, alors que la phase rapporte des candidats.
- **Intégration, invariants préservés** : le sweep n'altère ni `current_focus`,
  ni `focus_revision`, ni `attributed_knowledge_ids`.
- **Intégration, distinction d'origine** : un abandon automatique porte
  `abandonment_reason = 'auto_stale_7d'`, un abandon manuel garde le sien.

## Hors périmètre

Explicitement, et chacun pour une raison :

- **Auto-heartbeat (`7ffe0e8a`)** — non nécessaire ici (D3). Noter cependant que
  le principe de D1 le débloque conceptuellement : l'attribution par
  *(projet, acteur)* suffit, `X-Brain-Session` n'a jamais été indispensable.
  `is_human_actor` y retrouve un rôle utile — exclure les écritures de Dream de
  toute attribution automatique, ce que le ticket exige.
- **Identité de session (`2dfbb83d`)** — mesurée non fonctionnelle, et rendue
  non bloquante par D3.
- **Checkpoint sémantique (`d04dc588`)** — jugé BLOCKED par son propre audit,
  spec distincte requise.
- **Doctrine « les subagents n'ouvrent pas de session »** — touche neuf projets,
  donc ticket écosystème séparé.
- **Nettoyage manuel des 17 fantômes** — le DRY les listera, le flip WET les
  traitera. C'est la meilleure démonstration du mécanisme, et s'en priver
  rendrait le succès invérifiable.

## Limites connues

- D1 n'est pas applicable techniquement et ne le sera pas avec les briques
  actuelles. Une dérive future est possible sans qu'aucune garde ne crie ; seul
  le comptage des sessions ouvertes la révélera.
- Le seuil de 7 jours est calibré sur une seule mesure. Un changement de régime
  de travail — chantiers plus longs, sessions volontairement persistantes —
  invaliderait la marge et exigerait de re-mesurer.
- `last_heartbeat_at` reste déclaratif jusqu'à `7ffe0e8a`. Une session vivante
  mais silencieuse plus de 7 jours sera abandonnée à tort ; c'est le compromis
  accepté en D3, atténué par le fait que l'abandon conserve les captures.

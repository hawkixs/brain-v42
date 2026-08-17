# Quatrième quadrant du groupement de tickets — design

**Date** : 2026-08-03
**Statut** : validé, prêt pour plan d'implémentation
**Prior art** : `3ecb4a91` (exclusion des self-tickets d'`en_attente`)

## 1. Problème

`PgTicketRepo.list_grouped` (`src/brain_v42/repositories/pg_ticket.py:143`) répartit les tickets
d'un projet en trois groupes. Croisés avec les deux dimensions réelles — notre rôle sur le
ticket, et l'avancement — ils ne couvrent que trois des quatre cas :

| | `to_project` = nous (exécutant) | `from_project` = nous (demandeur) |
|---|---|---|
| `open`, `in_progress` | `a_traiter` | `en_attente` |
| `resolved`, `wontfix` | **aucun groupe** | `a_confirmer` |

Le quadrant manquant est « **nous avons livré, le demandeur n'a pas encore confirmé** ». Ces
tickets ne sont ni terminaux ni visibles : `TERMINAL_STATUSES` ne contient que `closed` et
`acked`, donc ils comptent comme du travail ouvert, mais aucune requête ne les remonte au
projet qui les a livrés.

### 1.1 Six tickets concernés, mesurés le 2026-08-03

| Invisible pour | Attend la confirmation de | Statut | Créé |
|---|---|---|---|
| red-shrik | red-story | resolved | 2026-07-05 |
| red-writer | red-story | resolved | 2026-07-12 |
| claude-dev-pc | red-codex | resolved | 2026-07-21 |
| claude-dev-pc | red-gift | resolved | 2026-07-23 |
| brain-v42 | red-writer | wontfix | 2026-07-24 |
| red-monitor | red | resolved | 2026-07-31 |

Le plus ancien dort depuis un mois. Le défaut est structurel à l'écosystème ReD, pas propre à
brain-v42.

### 1.2 Aucune action légale n'existe dans ce quadrant

`confirm`, `reopen` et `cancel` sont tous réservés au `requester` par la table `TRANSITIONS`.
L'exécutant qui a livré ne peut donc rien transitionner : le seul geste disponible est un
`brain_ticket_reply`, qui n'est pas une transition et reste permis quel que soit l'état.

C'est ce qui justifie de ne pas lister ces tickets dans le briefing, dont le contrat est
« ce sur quoi tu peux agir » — `en_attente` en est déjà exclu pour la même raison
(`session_tools.py:38-56` ne rend que `a_traiter` et `a_confirmer`).

## 2. Conception

### 2.1 La requête

Miroir exact d'`en_attente`, avec `to_project` et `_CONFIRMABLE` :

```python
awaiting_requester_confirmation = (
    await session.execute(
        _q(tickets.c.to_project, _CONFIRMABLE).where(
            tickets.c.from_project != tickets.c.to_project
        )
    )
).mappings().all()
```

**L'exclusion des self-tickets est obligatoire, pas cosmétique.** Sans elle, un self-ticket en
`resolved` remonterait dans `a_confirmer` (`from_project` = nous) *et* dans le nouveau groupe
(`to_project` = nous), donc compté deux fois. C'est le raisonnement de `3ecb4a91` appliqué un
cran plus loin.

### 2.2 Nommage

`TicketGroups` gagne le champ `awaiting_requester_confirmation`.

Le nom réutilise le vocabulaire de rôle déjà défini dans `models/ticket.py`
(`Role = Literal["executor", "requester"]`) et lève l'ambiguïté avec `a_confirmer`, qui
désigne la confirmation que **nous devons**, pas celle que **nous attendons**.

Le modèle devient linguistiquement mixte (`a_traiter`, `a_confirmer`, `en_attente`,
`awaiting_requester_confirmation`). Aligner les trois autres est un rename touchant
`session_tools.py` et `ticket_tools.py` : **hors périmètre**, mentionné en §4.

### 2.3 Briefing — compteur seul, pas de liste

`session_tools.py:41` rend aujourd'hui :

```
### Tickets (N à traiter · M à confirmer)
```

Il devient, uniquement quand le nouveau groupe est non vide :

```
### Tickets (N à traiter · M à confirmer · P livrés à valider)
```

Aucun ticket du nouveau groupe n'est listé : le briefing garde son contrat d'actionnabilité.
Le libellé reste en français, le briefing étant rédigé en français de bout en bout.

**La garde d'early-return doit être élargie.** `session_tools.py:39` sort quand `a_traiter` et
`a_confirmer` sont tous deux vides. Sans y ajouter le nouveau groupe, le cas exact que ce
chantier corrige — rien d'actionnable mais des livraisons bloquées — resterait invisible.
C'est la situation de `red-shrik`, dont le ticket dort depuis le 5 juillet.

### 2.4 `brain_ticket_list` — section complète

`ticket_tools.py:101` inclut le nouveau groupe dans le total, et une quatrième section est
rendue par le même helper que les trois autres.

L'intitulé doit porter les deux informations utiles : la balle est chez le demandeur, et nous
n'avons aucune transition légale — le seul geste est un `brain_ticket_reply` de relance.

## 3. Tests

1. Un ticket inter-projets `resolved` avec `to_project` = nous atterrit dans
   `awaiting_requester_confirmation`
2. Idem pour `wontfix`
3. **Un self-ticket `resolved` reste dans `a_confirmer` et n'apparaît PAS dans le nouveau
   groupe** — verrouille le non-double-comptage
4. `a_traiter`, `a_confirmer` et `en_attente` sont inchangés (non-régression)
5. Le briefing affiche le troisième compteur quand le groupe est non vide, et l'omet sinon
6. Le briefing rend la section Tickets même quand `a_traiter` et `a_confirmer` sont vides mais
   que le nouveau groupe ne l'est pas
7. Le total de `brain_ticket_list` inclut le nouveau groupe

## 4. Hors périmètre

- **`resolved_at` reste `NULL` sur un `wontfix`.** `ticket_service.py:135` ne l'horodate que
  sur `RESOLVED`, donc l'âge d'un `wontfix` retombe sur `created_at`. Information encore
  utile, défaut distinct dans une autre fonction ; le corriger ici mélangerait deux
  changements.
- **Le rename des trois champs français** en anglais (§2.2).
- **Le sort des six tickets listés en §1.1.** Ce chantier les rend visibles ; les traiter est
  une décision opérateur, et cinq d'entre eux appartiennent à d'autres projets.

## 5. Critère de succès

Après livraison, `brain_ticket_list` sur brain-v42 fait apparaître `732aa639` — le ticket
ouvert par red-writer que brain-v42 a passé `wontfix` le 24 juillet, et que red-writer n'a
jamais confirmé — et le briefing en porte le compte sans le lister. Les trois groupes
existants sont identiques, et aucun self-ticket n'apparaît deux fois.

# SPEC — M-G : l'état terminal des sessions `agent`

> **Statut : PROPOSITION SOUMISE À SIGNATURE.** Le `§0.4` de l'ADR répond **Q15 = route (3)**
> — un nouvel état terminal — puis écrit noir sur blanc : *« Le **nom** de l'état, sa
> branche exacte, et le sort de `captured_knowledge_ids`, `abandonment_reason` et
> `focus_outcome` dans cette branche **ne sont pas spécifiés ici** »*. **Ce document les
> PROPOSE. Il ne les tranche pas.** Chaque proposition porte son alternative et son coût.
>
> **Débloquée par le `§0ter.1`** (M-A + M-G en une seule tête) : cette spec attendait
> cette réponse et rien d'autre.
>
> **Sources qui font foi** : ADR `§0.4` (Q15), `§0ter` (signatures du 2026-08-20),
> `§0bis.1` (natures), `§0bis.3` amendé (seuil), `§5.2`/`§5.3` (attestation, couloir du
> pin), PLAN `§8` (ligne M-G). **Le CHECK cité ci-dessous a été relu dans
> `alembic/versions/037_session_lifecycle_v4.py` le 2026-08-20**, pas recopié depuis l'ADR.

---

## 1. Le problème, relu à la source

`brain_sessions_terminal_state_valid` (037, `_TERMINAL_STATE_V4`) est une disjonction de
**trois** branches — `open`, `ended`, `abandoned` — et **aucune** n'accueille une session
fermée sans rituel :

| Branche | Ce qu'elle EXIGE, et qui bloque |
|---|---|
| `ended` | `summary` **et** `next_focus` non vides, `focus_outcome NOT NULL`, **et** (`captured_knowledge_ids > 0` **XOR** `nothing_to_capture_reason` non vide) |
| `abandoned` | `summary IS NULL`, `next_focus IS NULL`, **`cardinality(captured_knowledge_ids) = 0`**, `abandonment_reason` non vide |

**Le point qui tue la route (1)**, et qu'il faut voir dans le SQL pour le croire :
`abandoned` **force le ledger terminal à zéro capture**. Une session `agent` qui a fait
son travail et attribué des artefacts déclarerait donc, dans son instantané terminal,
n'avoir rien capturé — sur le chemin de capture principal, et sous l'axe « traçabilité du
savoir » que l'opérateur a précisément choisi. Le ledger réel survit dans
`brain_session_artifacts`, mais l'instantané ment.

**Le point qui tue la route (2)** : `ended` exige un `summary` **et** un `next_focus`. Les
synthétiser côté serveur, c'est recopier de l'état mesurable dans le seul canal de
jugement non dérivable — objection C9, doctrine `FocusArg`.

D'où la route (3), et cette spec.

---

## 2. PROPOSITION 1 — le nom de l'état : `closed_inactive`

**Retenu : `closed_inactive`.**

| Candidat | Pourquoi écarté |
|---|---|
| `expired` | Suggère une péremption du contenu, pas de la session. Le savoir capturé, lui, n'expire pas |
| `auto_ended` | « ended » y est un préfixe : un `LIKE 'ended%'` ou une lecture pressée les confond, et la distinction avec le rituel est **tout** l'objet de cet état |
| `timed_out` | Dit un délai. Le `§0ter.5` vient précisément d'établir que 4 h est un seuil d'**éligibilité**, pas un délai — le nom re-mentirait |
| `swept` | Décrit le **mécanisme** (le sweep), pas l'**état**. Le mécanisme peut changer ; l'état non |
| **`closed_inactive`** | Dit la cause (inactivité observée) et le résultat (fermée), sans promettre de délai ni se confondre avec `ended` |

**Coût assumé** : c'est un quatrième vocabulaire dans une machine qui en a trois. Tout
lecteur de `status` doit apprendre qu'il existe **deux** façons d'être terminé
proprement — `ended` (rituel) et `closed_inactive` (sans rituel) — plus une d'être
terminé sans l'être vraiment (`abandoned`).

---

## 3. PROPOSITION 2 — la branche exacte du CHECK

```sql
OR (
    status = 'closed_inactive'
    AND ended_at IS NOT NULL
    AND nature = 'agent'                     -- ⬅ voir §3.1 : la garde qui compte
    AND summary IS NULL                      -- pas de rituel : rien à résumer
    AND next_focus IS NULL                   -- ⬅ voir §3.2
    AND abandonment_reason IS NULL           -- ce n'est PAS un abandon
    AND end_expected_focus_revision IS NULL  -- aucun CAS tenté
    AND focus_outcome IS NULL                -- ⬅ voir §3.3
    AND focus_at_end IS NULL
    AND focus_revision_at_end IS NULL
    AND nothing_to_capture_reason IS NULL    -- ⬅ voir §3.4
    -- captured_knowledge_ids : AUCUNE contrainte. C'est le point entier.
)
```

### 3.1 `nature = 'agent'` dans le CHECK — proposé, et discutable

**Pour** : rend impossible en base qu'une session `operator` (donc *claimed*) atteigne cet
état. La garantie du `§0bis.3` — *« une session claimed n'est JAMAIS fermée par
inactivité »* — cesse d'être une promesse applicative pour devenir une contrainte.

**Contre, et il est sérieux** : la résolution ratifiée (d) laisse `nature IS NULL` au
régime du sweep 7 j. Les sessions **antérieures à M-A** n'ont pas de nature. Si un jour on
voulait fermer une session `NULL` dans cet état, le CHECK l'interdirait — et le
contournement serait un backfill de `nature`, c'est-à-dire de changer les règles
rétroactivement, ce que (d) refuse.

> **À SIGNER** : `nature = 'agent'` (garantie dure, moins souple) **ou** aucune contrainte
> de nature (souple, garantie applicative seulement) ? *Recommandation : la garantie dure.
> Le cas « fermer une session NULL en closed_inactive » n'existe pas au catalogue, et un
> CHECK se relâche plus facilement qu'il ne se resserre.*

### 3.2 `next_focus IS NULL` — et pourquoi c'est une bonne nouvelle

Le `§0.4` posait la question sans la fermer : *« une session agent terminée dans ce nouvel
état applique-t-elle un `next_focus` ? Si non, elle ne fait pas de CAS, et **c'est une
bonne nouvelle** »*.

**Proposition : NON.** Elle n'écrit pas de `next_focus` et ne tente aucun CAS. Autrement,
chaque auto-fermeture bumperait `focus_revision` et produirait des
`focus_outcome = 'conflict'` **systématiques** sur les sessions opérateur concurrentes —
un bruit fabriqué par le serveur, sur le seul canal de jugement.

**Renforcé par le contexte du sweep** : la fermeture est **groupée**, une fois par nuit
(§0ter.4). N sessions fermées en un statement, chacune tentant un CAS, produiraient N−1
conflits par construction. C'est un argument de plus, indépendant du premier.

### 3.3 `focus_outcome IS NULL` — conséquence, pas choix

Découle de §3.2 : pas de CAS tenté ⇒ aucun résultat de CAS à déclarer. `NULL` veut dire
« aucune tentative », ce qui est exactement le fait.

*Alternative écartée : une troisième valeur `not_attempted`. Elle ajouterait un vocabulaire
pour dire ce que `NULL` dit déjà, et obligerait à toucher l'énumération Pydantic de
`focus_outcome` en plus de celle de `status` — deux rails au lieu d'un.*

### 3.4 `captured_knowledge_ids` LIBRE, `nothing_to_capture_reason` INTERDIT

**C'est la raison d'être de toute la migration.** Une session `agent` auto-fermée garde son
ledger **tel quel** : zéro, un ou cent artefacts, sans contrainte et sans justification.

`nothing_to_capture_reason` est **interdit** (`IS NULL`) et ce n'est pas un détail : sur la
branche `ended`, il est la **contrepartie fail-closed** d'un ledger vide — l'opérateur doit
*dire pourquoi* il n'a rien capturé. C'est un **jugement**. Un serveur qui le remplirait
pour une session agent fabriquerait du jugement, exactement l'objection C9 qui a tué la
route (2). Le laisser `NULL` sur un ledger vide dit la vérité : *personne n'a été
interrogé.*

**Conséquence à assumer** : une session agent à ledger vide devient **indiscernable** d'une
session agent qui n'avait rien à capturer. B3 ne se mesure donc pas sur cet état. C'est le
prix de ne pas fabriquer de jugement, et il doit être écrit dans les critères de sortie
plutôt que découvert en les mesurant.

---

## 4. PROPOSITION 3 — le déclencheur

**Le sweep nocturne, un seul statement, seuil d'éligibilité 4 h** (`§0ter.4` et `§0ter.5`).

- **Éligible** : `status = 'open'` **ET** `nature = 'agent'` **ET**
  `last_observed_at < now() - interval '4 hours'`.
- **Jamais** : une session `claimed` (⇒ `nature = 'operator'`), quel que soit son âge —
  seul le sweep 7 j existant peut la prendre.
- **Jamais** pendant un appel en vol. `provenance.py` tient déjà la profondeur d'appel
  (`enter_call` / `exit_call` / `is_outermost_call`) : le « coupé au milieu » littéral est
  gratuit à interdire.
- **`nature IS NULL`** : hors périmètre, reste au régime sweep 7 j (résolution (d)).

**Ce que ce n'est PAS, et qu'il ne faut jamais annoncer autrement** : 4 h n'est **pas** un
délai de fermeture. Une traçante devenue inactive juste après un passage vit jusqu'au
suivant — **latence réelle pire cas ≈ 28 h**.

> **PROPOSITION — un seul statement, partagé avec Q5.** Le `§0ter.5` acte que Q5 (seuils du
> sweep) et le seuil 4 h se répondent **ensemble**. Deux passes nocturnes concurrentes sur
> `brain_sessions` pour deux règles qui décrivent le même geste seraient une machinerie
> gratuite. Le statement existant est déjà **UN** `UPDATE … RETURNING` (épinglé
> textuellement par `test_pg_brain_session_sweep.py`) : M-G doit **étendre ce statement**,
> pas en ajouter un.
>
> **Garde à reconduire explicitement** : le test « un seul statement » existe précisément
> pour interdire la fenêtre `SELECT`-puis-`UPDATE`. L'étendre ne doit pas la rouvrir.

---

## 5. Ce que la tête unique M-A+M-G emporte

Signé au `§0ter.1`. **Une tête**, donc **un** rendez-vous, **un** bump de
`_REQUIRED_ALEMBIC_HEAD` avec son test **dans le même commit**, et pas de fenêtre où des
sessions `agent` naissent sans état terminal atteignable.

**Cinq objets bougent ensemble. Aucun ne peut être différé :**

1. **Le CHECK** `brain_sessions_terminal_state_valid` — quatrième branche.
2. **Le rail Pydantic (C7)** — `BrainSessionStatus` (`models/brain_session.py:27-29`)
   gagne `CLOSED_INACTIVE = "closed_inactive"`. **Avec le CHECK, jamais après.**
3. **Les DEUX assets `ops/recovery/` v4** — `brain-v42-v4.sql` **et**
   `brain-v42-v4-pgrestore.sql`, tenus en parité de CTE. Deux empreintes bougent :
   - l'**empreinte du CHECK terminal** (`md5` de la définition de contrainte) ;
   - **`expected_session_indexes`** (`v4.sql:404`, contrôlée `:665` et `:687`), liste
     **fermée** que l'index UNIQUE partiel de connexion apporté par **M-A** casse
     — quatrième mécanisme de casse d'attestation (`§5.2 (ii)`).
   *C'est la raison la plus concrète de la tête unique : une seule régénération pour deux
   causes de casse qui, séparées, en demanderaient deux.*
4. **Le downgrade `037→036`**, déjà fail-closed, doit **apprendre le nouvel état** : refuser
   s'il existe des lignes `closed_inactive`, sans quoi il perd des sessions terminales
   silencieusement.
5. **Le covenant réécrit par nature** (`§0ter` (d)) — la phrase change dans les docstrings
   des tools de cycle de vie, et `test_session_covenant_docstrings_anchor.py` **rougit**.
   C'est le geste Red qui ouvre la livraison, pas un dégât.

> **Piège d'index, hérité et à ne pas redécouvrir.** L'index UNIQUE sur la colonne de
> connexion (M-A) **doit être PARTIEL** — `WHERE status = 'open'`. Un unique plein, sur le
> modèle de `uq_brain_sessions_project_client`, **brûlerait la connexion à vie** dès la
> première auto-fermeture, et détruirait la propriété « être coupé coûte un découpage, pas
> une perte ». **Sous M-G, ce piège s'aggrave** : `closed_inactive` fait précisément sortir
> des lignes de `status = 'open'` en masse, toutes les nuits.

---

## 6. Le prix du fail-open, écrit ici parce que c'est ici qu'on le paie

Ratifié au `§0ter.4` : l'auto-ouverture est **fail-open**. Si l'ouverture échoue et que
l'appel passe quand même, **les artefacts créés avant l'ouverture réussie tombent hors de
la fenêtre `created_at >= started_at`** de la capture.

**B5 redevient donc mordante, ponctuellement.** Ce n'est pas un effet de bord découvert
après coup : c'est le coût accepté de ne pas faire tomber tout le serveur MCP sur un hoquet
de base — même posture que l'émetteur d'activité client (`1c40c36a`), dont l'échec ne peut
pas casser l'appel qu'il observe.

**Interaction avec §3.4, et elle est désagréable** : une session dont l'ouverture a
fail-open ET qui se ferme en `closed_inactive` présente un ledger vide **sans
`nothing_to_capture_reason`** — indiscernable d'une session qui n'avait rien à capturer.
La perte est donc **silencieuse par construction**.

> **PROPOSITION — la rendre bruyante sans fabriquer de jugement.** Compter les
> fail-open dans une métrique dédiée (l'émetteur d'activité client sait déjà le faire) et
> refuser de lire B3 sur une fenêtre où ce compteur est non nul. On ne répare pas la
> fenêtre — on refuse de mesurer une couverture qu'on sait fausse. *Non signé.*

---

## 7. Critères de sortie — mesurables, et ce qu'ils NE prouvent pas

| Critère | `proves` | `does_not_prove` |
|---|---|---|
| `alembic current` = nouvelle tête, pin bumpé dans le même commit | La tête est passée sans fenêtre à deux têtes | Rien sur le comportement |
| Les DEUX assets v4 verts après régénération | Empreinte du CHECK **et** liste d'index d'accord avec la base | Rien sur les autres tables |
| Downgrade refusé fail-closed avec ≥ 1 ligne `closed_inactive` | Le rollback ne perd pas de session terminale | Ne prouve pas qu'il réussit sans ces lignes — à tester aussi |
| Une nuit : N sessions `agent` inactives > 4 h passées `closed_inactive`, **0** `operator` prise, **0** ledger vidé | Le périmètre et la préservation | **Ne prouve rien sur les sessions actives** : il faut aussi montrer qu'une session ayant observé un appel dans les 4 h **n'est pas** prise |
| `focus_revision` du projet **inchangée** après la passe | Aucun CAS induit, aucun `conflict` fabriqué | — |

---

## 8. Ce qui reste NON SPÉCIFIÉ

1. **`nature = 'agent'` dans le CHECK** (§3.1) — les deux options ont un coût réel.
2. **La métrique de fail-open** (§6) — proposée, non signée.
3. **La colonne `last_observed_at`** est supposée livrée par **M-A**. Si M-A la nomme
   autrement, §4 suit ce nom. *Cette spec ne nomme pas les colonnes de M-A.*
4. **Le seuil de Q5** — répondu **avec** le 4 h, dans le même statement.
5. **La formulation exacte de la phrase-covenant par nature** — le `§0ter` (d) dit
   « réécrite », pas « comment ». Elle doit être écrite avant la livraison, avec la mise à
   jour du test d'ancrage.
6. **LE RANG DE LA TÊTE DANS LE COULOIR DU PIN.** Le PLAN `§8` porte encore, en toutes
   lettres, `M-G | **à séquencer — non tranché**`. Le `§0ter.1` dit **avec quoi** elle part
   (M-A), pas **quand** — et le couloir interdit deux têtes en vol, donc l'ordre est une
   contrainte, pas une préférence. D'autres candidates existent au même couloir : la
   « neuf colonnes » de `c60d023d`, `G7 NOT NULL`, le compteur de sweep. **Un dossier de
   séquencement des candidates 046+ est en cours et doit être soumis et signé avant qu'une
   seule ligne de migration soit écrite.** Cette spec décrit le CONTENU de M-G ; elle ne
   revendique aucun rang.

   > **AMENDÉ le 2026-08-20 — le dossier existe et son ORDRE est signé.**
   > `SEQUENCEMENT-2026-08-20-couloir-du-pin.md`, décision `9d22bc6a`. **S6** place M-G en
   > **`046 = M-A + M-G`, première tête du couloir**, sur 8 rendez-vous (Ordre B, 048
   > dégroupé). Le rang n'est donc plus ouvert.
   >
   > **Mais l'autorisation d'écrire ne l'est pas non plus.** S2 (colonne de connexion),
   > S3 (nature en base), **S4 (CHECK dur ou souple — c'est le §3.1 de cette spec)** et
   > S5 (cette spec en entier) restent **NON SIGNÉES**. L'exigence de ce point 6 tient
   > donc telle quelle : aucune ligne de migration.
   >
   > **Fait mesuré qui valide le §2** : `brain_sessions.status` est `varchar(20)` et
   > `closed_inactive` fait **15** caractères — **il entre**, sans élargissement de
   > colonne (mesuré le 2026-08-20, `information_schema.columns`). Un nom plus long aurait
   > ajouté un `ALTER TYPE` à la tête la plus surveillée du couloir.
   >
   > **Second fait mesuré, qui décharge M-G d'un risque** : **ZÉRO vue** ne dépend de
   > `brain_sessions` — vérifié par deux angles (`view_column_usage` et
   > `pg_depend`/`pg_rewrite`). Contrairement à la 045, aucune vue à faire tomber et
   > revenir autour du changement de contrainte.
7. **Le nom de la colonne d'inactivité** — cette spec écrit `last_observed_at` parce que
   c'est le nom employé par D5 et le `§0bis.4`. Si **M-A** la livre sous un autre nom, le
   `§4` suit ce nom sans discuter : M-A fait foi sur ses propres colonnes.
   *(Redite volontaire du point 3 : c'est l'endroit où un implémenteur regardera.)*

---

*Écrit le 2026-08-20 en Phase 0 — ZÉRO mutation. Aucune migration n'est écrite ici, et
aucune ne doit l'être avant que le dossier de séquencement du couloir du pin (candidates
046+) soit soumis et signé.*

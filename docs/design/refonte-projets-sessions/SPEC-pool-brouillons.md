# SPEC — Le pool de brouillons non signés (migration M-E, signature S8)

> **Statut : PROPOSITION SOUMISE À SIGNATURE.** Le `§0bis.5` répond **Q6 = acceptée** —
> *« les brouillons non signés SURVIVENT à l'auto-fermeture de leur session, dans un pool
> en attente de signature, hors session »* — puis liste ce qui **reste à spécifier** :
> *« la FK vers `brain_sessions` et son `ON DELETE`, la durée de vie du pool, son plafond,
> et le tool de signature hors session ne sont **pas** spécifiés »*. Ce document les
> **PROPOSE**.
>
> **Dernier gate documentaire de la Phase 0.** Avec `SPEC-checkpoint.md` et `SPEC-M-G.md`,
> il porte le 6ᵉ contenu à complétion.
>
> **Sources qui font foi** : ADR `§0bis.5` (Q6), `§0bis.2` (clé de connexion), `§0ter`
> (signatures du 2026-08-20), PLAN `§4.4` et `§8` (ligne M-E), décision `fc8651ad`,
> dossier `SEQUENCEMENT-2026-08-20-couloir-du-pin.md` (S6 : M-E en tête **séparée**).
> *Écrit le 2026-08-20. Aucune ligne de migration — S2-S5 ne sont pas signées.*

---

## 0. Le malentendu à dissiper d'abord : survivre à quoi ?

Le `§0bis.5` dit qu'un brouillon doit *« pouvoir survivre à la session qui l'a observé »*.
Lu vite, on entend « survivre à la SUPPRESSION de la ligne de session », et on part
chercher un `ON DELETE` malin. **C'est le mauvais problème.**

Une session ne se **supprime** pas dans ce système : elle se **termine**. `ended`,
`abandoned`, et bientôt `closed_inactive` (M-G) sont des **états**, pas des effacements —
la ligne `brain_sessions` reste. **La fermeture ne touche donc aucune clé étrangère**, et
l'exigence de survie du `§0bis.5` est satisfaite **sans un octet de FK** : le brouillon
reste, sa session aussi, seul le `status` de la session a changé.

Ce que la FK gouverne est un cas **différent et rare** : quelqu'un exécute un `DELETE`
sur `brain_sessions`. Cela n'arrive ni au rituel, ni au sweep, ni à l'auto-fermeture.

**Conséquence sur la conduite de la spec** : la question du `ON DELETE` est réelle mais
**petite**, et elle ne doit pas être traitée comme le cœur du sujet. Le cœur, c'est **qui
signe, quand, et ce qui arrive si personne ne signe jamais**.

---

## 1. PROPOSITION 1 — la FK et son `ON DELETE` : `RESTRICT`

**Retenu : `session_id … REFERENCES brain_sessions(id) ON DELETE RESTRICT`.**

### 1.1 `SET NULL` est structurellement impossible ici — et c'est une bonne nouvelle

Le dossier de séquencement rappelle le piège maison `CHECK` + `ON DELETE SET NULL`
(skill `postgres-check-vs-on-delete-set-null`) : un `CHECK` n'est **pas** deferrable en
PostgreSQL, donc une cascade `SET NULL` qui viole un `CHECK` fait échouer le `DELETE` du
parent, sans recours.

**Ce piège ne peut pas mordre sur cette table**, et pour une raison plus forte qu'une
précaution : le PLAN `§4.4` fixe la **PK `(session_id, knowledge_id)`**. Une colonne de
clé primaire est `NOT NULL` par définition — `ON DELETE SET NULL` serait **rejeté par
Postgres à la déclaration**, pas au premier `DELETE`. L'erreur est impossible à commettre
en silence.

*(À dire explicitement dans la migration, en commentaire : sans cette note, le prochain
lecteur qui voudra « faire survivre le brouillon à la suppression de sa session »
essaiera `SET NULL`, se heurtera à la PK, et sera tenté de casser la PK pour y arriver —
détruisant au passage la propriété que la PK porte, §1.3.)*

### 1.2 `RESTRICT` plutôt que `CASCADE`

`CASCADE` détruirait les brouillons non signés d'une session supprimée — c'est-à-dire
exactement ce que le `§0bis.5` a écarté (« jeter les brouillons non signés reviendrait à
détruire précisément ce que la nature agent produit »). `RESTRICT` refuse la suppression
tant que des brouillons existent.

**Coût assumé, et il faut le nommer** : une session ayant des brouillons non résolus
devient **non supprimable** tant qu'ils ne sont pas signés ou rejetés. C'est cohérent avec
le downgrade fail-closed déjà prévu pour M-E (« fail-closed si des lignes `staged` non
résolues existent »), et c'est la même posture que `SPEC-checkpoint.md` retient pour les
checkpoints. **Deux tables qui rendent leur session indélébile, c'est une propriété du
système, pas un accident** — elle mérite d'être écrite une fois, ici.

### 1.3 La PK ne bouge pas, et pourquoi

`(session_id, knowledge_id)` — **pas** de PK sur `knowledge_id` seul. Un brouillon est une
**hypothèse** : deux sessions peuvent observer le même artefact sans que l'une prive
l'autre. **Seule la promotion dans `brain_session_artifacts`** (PK `knowledge_id`) confère
l'exclusivité. Cette asymétrie est le mécanisme entier : le pool est permissif, le ledger
est exclusif.

---

## 2. PROPOSITION 2 — la durée de vie : AUCUNE expiration automatique

**Retenu : un brouillon `staged` ne périme JAMAIS tout seul.**

C'est le choix qui demande le plus de justification, parce qu'il produit une table qui ne
se vide pas d'elle-même.

| Option | Pourquoi écartée |
|---|---|
| Expiration à N jours (purge) | **Détruit du savoir non signé sans geste humain** — l'inverse exact de Q6, et sous l'axe « traçabilité du savoir » qui commande l'ordre du plan |
| Expiration → `dismissed` automatique | Même destruction, avec une trace. Mais `dismissed` est un **jugement** (« je n'en veux pas ») ; le serveur le fabriquerait — objection C9, celle qui a tué la route (2) de Q15 |
| Rattachement automatique à la session suivante du même projet | Attribue sans geste humain. C'est **E3**, le changement de covenant plein que le `§0bis.5` refuse |
| **Aucune expiration** | Rien n'est détruit, rien n'est attribué. Le pool grossit — c'est un coût **visible et mesurable**, pas une perte silencieuse |

**Le coût est réel et se gère par la mesure, pas par une horloge.** Proposition : exposer
`staged_pool_size` et `staged_pool_oldest_age` en métriques process. Un pool qui gonfle
est un signal que personne ne signe — **une information sur l'usage**, pas un incident de
stockage. La réponse à « le pool est gros » est un geste opérateur, jamais un `DELETE`
programmé.

> **À SIGNER** : accepte-t-on une table qui ne se purge pas ? *Recommandation : oui.
> `brain_session_artifacts` et `brain_session_checkpoints` ne se purgent pas non plus, et
> le volume attendu (voir §3) est très inférieur au corpus, mesuré à 4 405 entités le
> 2026-08-20.*

---

## 3. PROPOSITION 3 — les plafonds : DEUX, pas un

Le PLAN `§4.4` fixe déjà **500 par session**, avec un compteur
`staged_capture_skipped{overflow}` en métriques process. **Ce plafond est conservé** et
n'est pas rediscuté.

**Il ne suffit pas**, et c'est l'apport de cette spec : 500/session borne ce qu'**une**
session peut produire, pas ce que le **pool** peut accumuler. Sous l'ouverture automatique,
les sessions traçantes naissent seules et se ferment seules ; rien ne borne leur nombre.

**Proposition : un second plafond, sur le pool entier**, `BRAIN_SESSION_STAGED_POOL_MAX`,
proposé à **5 000** lignes `staged` — dix sessions pleines.

**Au dépassement : on cesse d'observer, on ne jette rien.** Exactement la posture du
plafond par session — le nouvel écrivain est refusé, l'existant est intact. Un pool plein
dégrade l'observation ; il ne détruit pas ce qui est déjà observé.

> **À SIGNER** : la valeur 5 000, et le principe même d'un second plafond.
> ⚠️ **Ne pas répéter l'erreur du seuil des traçantes** : si ce plafond est évalué dans le
> balayage nocturne plutôt qu'à l'écriture, c'est un **seuil d'éligibilité**, pas une
> borne — et le pool peut dépasser entre deux passages. *Recommandation : l'évaluer à
> l'écriture, où la borne est vraie au sens strict.*

---

## 4. PROPOSITION 4 — le tool de signature hors session

```
brain_staged_capture_sign(
    project_key: str,
    knowledge_ids: list[UUID],       # 1..100, uniques
    target_session_id: UUID | None = None,
) -> StagedSignResult
```

**Il n'appartient PAS au cycle de vie de session**, et c'est sa propriété la plus
importante : il s'appelle **hors session**, sur un pool qui a survécu à la fermeture de
celle qui l'a observé. Il ne porte donc **pas** `expected_client_key` — il n'adresse
aucune session vivante.

**Ce qu'il fait** : passe des brouillons de `staged` à `promoted`, et écrit les lignes
correspondantes dans `brain_session_artifacts` — où la PK `knowledge_id` **impose
l'exclusivité**. Un artefact déjà attribué à une autre session est **refusé**, pas
réattribué : la réattribution est **Q8/M-F**, une autre tête, une autre décision.

**Le pendant obligatoire** : `brain_staged_capture_dismiss(...)`, même forme, `staged` →
`dismissed`. Sans lui, un brouillon qu'on ne veut pas garder n'a **aucune sortie** — il
resterait `staged` pour toujours et bloquerait la suppression de sa session (§1.2). Les
trois valeurs du CHECK `∈ {staged, promoted, dismissed}` doivent chacune être atteignables
par un geste ; une valeur inatteignable est un défaut de conception, pas une réserve.

> **À SIGNER, et c'est la vraie question produit** : `target_session_id` — un brouillon
> promu s'attribue-t-il **à la session qui l'a observé** (paternité, cohérent avec Q2 =
> `from_project`) ou **à la session courante de l'opérateur qui signe** (le signataire) ?
> *Recommandation : à la session qui l'a OBSERVÉ, `target_session_id` omis par défaut.*
> C'est l'analogie exacte de Q2 : la capture répond à « qu'a **produit** cette session ».
> Signer n'est pas produire.
>
> **Le covenant passe alors à NEUF ou DIX commandes**, pas huit. `SPEC-checkpoint.md` §4
> annonce huit (les sept + le checkpoint) ; `sign` et `dismiss` en ajoutent deux. **Les
> trois specs doivent s'accorder sur ce compte avant la première livraison** — le test
> d'ancrage `test_session_covenant_docstrings_anchor.py` porte le nombre en toutes
> lettres, et il rougira une fois par tool ajouté. *Ce n'est pas un détail de
> comptage : c'est la surface du contrat que lit un agent.*

---

## 5. Ce que M-E casse, et ce qu'elle ne casse pas

- **Attestation** : table NEUVE ⇒ `table_set` change dans les **deux** assets v4. Sous la
  signature **S1** (contrat v5 à `alembic_head` **dérivé**), c'est désormais **le seul**
  coût d'attestation de cette tête — la révision ne compte plus.
- **Aucun index sur `brain_sessions`**, donc `expected_session_indexes` ne bouge pas.
- **Tête SÉPARÉE de M-C**, par la signature **S6** : le regroupement n'est légitime que si
  les downgrades peuvent échouer indépendamment, et **S9 ne l'a pas démontré**. Les deux
  downgrades sont fail-closed sur des lignes non résolues ; groupés, l'échec de l'un
  bloquerait le rollback de l'autre.
- **`WRITE_MODELS_BY_TABLE`** (`test_model_column_width_contract.py`) : si M-E livre un
  modèle Pydantic écrivain, il doit y être **inscrit**. Vérifié le 2026-08-20 : la table y
  porte **12 entrées** et `brain_sessions` **n'y est pas** — un modèle non inscrit n'est
  pas audité, et sa garde est silencieuse. La checklist de la 046 porte déjà ce trou
  (`SEQUENCEMENT §3.2`) ; **M-E ne doit pas le rouvrir pour sa propre table.**

---

## 6. Ce qui reste NON SPÉCIFIÉ

1. **Les quatre propositions ci-dessus** — aucune n'est signée.
2. **Le compte du covenant** (§4) — huit, neuf ou dix, à accorder entre les trois specs.
3. **L'écrivain de brouillons lui-même** reste gated derrière
   `BRAIN_SESSION_STAGED_CAPTURE_ENABLED`, livré **fermé**. Cette spec décrit le pool et
   sa sortie, **pas** la politique d'observation.
4. **Un site périmé, repéré et non corrigé ici** : PLAN `§4.4` décrit encore l'écrivain
   comme liant par **`(project_key, started_by_actor)` avec la règle « exactement un »**.
   C'est faux depuis le `§0bis.2` — sur la connexion, **la liaison est exacte**, et le
   `§0bis.5` note lui-même que « Q6 perd son principal risque d'implémentation ». Amender
   le PLAN sur ce point demande le même geste que pour D1/D4 : un encadré, une signature.

---

*Écrit le 2026-08-20 en Phase 0 — ZÉRO mutation. Aucune migration ne sera écrite avant
que S2-S5 soient signées et que le rang de M-E dans le couloir soit atteint (S6 : après
046 et 047, en tête séparée de M-C).*

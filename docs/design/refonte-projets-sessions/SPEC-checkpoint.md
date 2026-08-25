# SPEC — `brain_session_checkpoint` (migration M-C)

> **Statut : PROPOSITION SOUMISE À SIGNATURE.** Rien ici n'est acquis. Ce document
> existe parce que l'audit du ticket `d04dc588` a jugé que « le plus petit lot
> admissible reste documentaire », et que ni l'ADR ni le PLAN ne livraient cette spec.
> Sans elle, **M-C n'est pas écrivable**.
>
> **Sources qui font foi** : ADR `§0bis.4` (réponse à Q3), ADR `§2 / D4` (le geste),
> ADR `§3.2` (les deux divergences d'avec le MVP du ticket), ADR `§0ter` (signatures du
> 2026-08-20), PLAN `§2 contenu n° 4` (le livrable) et `§8` (la tête M-C).
> *Rédigé le 2026-08-20. Aucun chiffre recopié : les mesures citées portent leur date.*

---

## 0. Ce que le checkpoint est devenu, et pourquoi ça change tout

Le checkpoint a **changé de nature** entre sa proposition et aujourd'hui, et une spec
écrite sur l'ancienne lecture serait fausse dès sa première ligne.

**Avant** (greffe C, D4) : un mécanisme de **vivacité**. Il rafraîchissait
`last_heartbeat_at`, seul signal du sweep. D4 en signalait le danger : *un agent qui
checkpointe seul maintient sa session vivante indéfiniment — le faux-vivant que
`2bd14b24` condamne — et rend le critère 4.3 auto-satisfiable.*

**Après** (`§0bis.4`, sous Q12 = (a) + ouverture automatique) : un **objet de jugement
pur**. La vivacité d'une session `agent` vient de `last_observed_at`, qui bouge à *chaque*
appel d'outil ; le checkpoint cesse d'être spécial. Côté `operator`, le timeout ne mord
pas du tout. Son unique métier redevient **B7 — la fraîcheur sémantique**.

> ### ⚠️ Contradiction interne de l'ADR, à trancher explicitement
>
> Le texte de **D4** (`§2`) porte encore : « **Effet de bord : rafraîchit
> `last_heartbeat_at`** sur un checkpoint réel, jamais sur un replay ». Le **`§0bis.4`**,
> postérieur, conclut que `BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT` « **n'a plus
> d'objet** ».
>
> **Les deux ne peuvent pas être vrais.** Cette spec retient le `§0bis.4` — il est
> postérieur, et il résout Q3(a)/(b) que D4 se contentait de signaler comme piégées.
> **Proposition : D4 doit être amendé sur place** d'une note « effet heartbeat retiré,
> voir §0bis.4 », comme l'ont été le §0.4 et le §0bis.3. Sans cet amendement, un
> implémenteur qui lit D4 en premier câblera un effet que la spec interdit.

**Conséquence directe et non négociable** : le checkpoint **n'écrit ni ne touche**
`last_heartbeat_at`. Ni sur un checkpoint réel, ni sur un replay. Le flag
`BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT` **n'est pas livré** — un flag sans objet est
une dette, pas une précaution.

---

## 1. Les deux divergences d'avec le MVP du ticket, tranchées

L'audit exigeait que la spec « tranche explicitement les deux divergences ». Elles sont
tranchées **dans des sens opposés**, et c'est délibéré.

### 1.1 Stockage — divergence MAINTENUE et assumée

| | Ticket `d04dc588` | **Retenu** |
|---|---|---|
| Forme | Snapshot sur `brain_sessions` | **Table append-only dédiée** |
| Idempotence | CAS `expected_checkpoint_revision` | **`UNIQUE(session_id, seq)` + `ON CONFLICT DO NOTHING`** |

**Motif.** Une note de checkpoint est du **jugement** (grille FAIT/JUGEMENT, `§1.3`) :
l'écraser par snapshot détruit l'histoire que le checkpoint existe pour produire. Et les
deux propriétés P0 du ticket — *replay exact sans double effet*, *conflit non destructif* —
sont **réobtenues par la clé** au lieu du CAS.

**Ce que ça coûte, écrit plutôt que tu.** Le CAS donnait un signal de **conflit** : deux
écrivains concurrents sur la même révision, l'un des deux le sait. La clé, elle, rend le
retry *silencieusement* idempotent : un second appelant qui réutilise le même `seq` avec un
**contenu différent** est absorbé sans un mot par `ON CONFLICT DO NOTHING`. Les retries
d'agents étant la norme (invariant C6) et le `seq` étant fourni par le client, ce cas
n'est pas théorique.

> **DÉJÀ TRANCHÉ PAR LE PLAN — cette spec ne fait que le rendre implémentable.** Le
> PLAN `§4` l'écrit : *« un même `seq` avec un payload différent est un conflit non
> destructif, **rejeté explicitement** »*. Ce n'est donc pas une proposition neuve, et je
> l'avais d'abord présentée comme telle — correction faite en relisant le PLAN.
>
> **Mécanique proposée pour l'obtenir** (le PLAN dit le QUOI, pas le COMMENT) :
> `ON CONFLICT DO NOTHING … RETURNING` rend zéro ligne aussi bien pour un replay exact que
> pour une collision de contenu. Relire la ligne existante quand `RETURNING` est vide,
> comparer le triplet, puis **`replayed: true` si identique / `CheckpointSeqConflict` si
> différent**. Le replay exact reste gratuit ; le conflit cesse d'être silencieux.

### 1.2 Forme du payload — divergence ABANDONNÉE, le ticket l'emporte

| | Proposition D4 | **Retenu (= ticket)** |
|---|---|---|
| Forme | `kind ∈ {progress, blocker, next_step, handoff}` + `note` unique | **`progress` + `blocker\|null` + `next_step`, publiés ENSEMBLE** |
| Appels | Trois natures mutuellement exclusives | **Un appel** |

**Motif, littéral.** Trois `kind` exclusifs permettent d'émettre un `progress` sans
jamais de `next_step` — et **le lecteur de fraîcheur ne peut alors pas savoir si
l'instantané est complet**. Publier progrès + blocage + prochaine étape demanderait trois
appels, trois `seq`, et ferait sauter le critère explicite « **un appel** » du ticket.

`handoff` disparaît comme *nature*. **Proposition** : il n'a pas besoin d'un champ — un
handoff est un checkpoint dont le `next_step` s'adresse à quelqu'un d'autre, et le texte
le dit mieux qu'une énumération.

---

## 2. Contrat du tool

```
brain_session_checkpoint(
    session_id:      UUID,
    expected_client_key: str,        # voir §2.1 — conservée
    seq:             int,            # ≥ 1, monotone, fourni par le CLIENT
    progress:        str,            # 1..2000, non vide après btrim
    next_step:       str,            # 1..2000, non vide après btrim
    blocker:         str | None = None,   # None ou 1..2000 après btrim
) -> CheckpointResult
```

```
CheckpointResult {
    session_id, seq, created_at,
    replayed: bool,                  # true = ligne déjà présente, à l'identique
    checkpoint_count: int,           # après cet appel, pour lire le plafond
}
```

### 2.1 `expected_client_key` est CONSERVÉE ici — et ce n'est pas une contradiction

Le `§0ter.3` retire la garde **du chemin résolu-par-connexion**. Le checkpoint **n'est pas
ce chemin** : il adresse un `session_id` explicite, donc le mauvais ciblage entre sessions
parallèles qu'elle prévient y est **possible**. La garde reste, exactement comme sur
`resume`, `capture`, `heartbeat`, `end` et `abandon`.

*Rappel du contrat existant, inchangé : c'est une garde d'isolation, pas une
authentification.*

### 2.2 Bornes de payload — fail-closed, toutes

| Borne | Valeur | Comportement au dépassement |
|---|---|---|
| `progress`, `next_step`, `blocker` | **2000 caractères** chacun | **`ValueError`, jamais de troncature silencieuse** |
| `progress`, `next_step` non vides | après `btrim` | `ValueError` |
| `seq` | entier ≥ 1 | `ValueError` |
| Checkpoints par session | **200** | `ValueError` fail-closed (repris de D4) |

**Pourquoi refuser plutôt que tronquer**, alors que `parse_and_validate` tronque
« forgivingly » les `topic` à 200 : là c'est un modèle qui produit, ici c'est un **objet de
jugement**. Tronquer un jugement à 2000 caractères produit une phrase qui *a l'air*
complète et ne l'est pas. Le plafond est généreux ; le franchir est une erreur d'appelant.

**Le plafond de 200 est par session, pas par nuit** : sous l'ouverture automatique, une
traçante vit au plus jusqu'au sweep. 200 notes de jugement dans une seule session est déjà
un signal en soi.

### 2.3 Ce que le tool ne fait PAS

- Il **ne touche pas** `last_heartbeat_at` (§0).
- Il **ne touche pas** `current_focus` ni `focus_revision`. Aucun CAS de focus, donc
  aucun `focus_outcome = 'conflict'` induit sur les sessions sœurs.
- Il **n'attribue aucun artefact**. Le ledger de capture reste `brain_session_capture`.
- Il **n'ouvre ni ne ferme** aucune session. Le covenant tient : *aucun hook, aucune
  auto-fermeture n'invoque une frontière de cycle de vie* — et le checkpoint n'en est pas une.

### 2.4 Surfaces de lecture (PLAN `§4`)

- **`brain_session_list`** gagne `last_checkpoint_at`.
- **`brain_session_resume`** rend les **checkpoints récents**. *Borne non spécifiée —
  proposition : les 5 derniers par `seq` décroissant, pour rester sous le plafond de
  briefing. À signer.*
- **Doctrine de fraîcheur `d04dc588`** : la fraîcheur affichée dérive de l'âge du dernier
  checkpoint **ou du heartbeat** ; la dérive du focus est exposée séparément et **n'est
  jamais cause de péremption**.
- **`brain_session_heartbeat` reste inchangé**, documenté « préférer checkpoint », et
  **jamais transformé en no-op** — mentir à une commande explicite du covenant serait une
  rupture de contrat (motif de rejet de la proposition A par le panel).

> **Attention — « ou du heartbeat » reste vrai, l'inverse ne l'est plus.** La fraîcheur
> peut LIRE `last_heartbeat_at` ; le checkpoint ne l'ÉCRIT plus (§0). Les deux phrases
> cohabitent, et il est facile de lire la première comme autorisant la seconde.

---

## 3. Migration M-C

```sql
CREATE TABLE brain_session_checkpoints (
    id            bigserial PRIMARY KEY,
    session_id    uuid NOT NULL REFERENCES brain_sessions(id) ON DELETE RESTRICT,
    seq           integer NOT NULL,
    progress      text NOT NULL,
    next_step     text NOT NULL,
    blocker       text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT brain_session_checkpoints_seq_positive CHECK (seq >= 1),
    CONSTRAINT brain_session_checkpoints_progress_nonempty CHECK (btrim(progress) <> ''),
    CONSTRAINT brain_session_checkpoints_next_step_nonempty CHECK (btrim(next_step) <> ''),
    CONSTRAINT brain_session_checkpoints_blocker_nonempty CHECK (blocker IS NULL OR btrim(blocker) <> ''),
    CONSTRAINT uq_brain_session_checkpoints_session_seq UNIQUE (session_id, seq)
);
```

**Append-only garanti PAR TRIGGER en base**, pas par absence de chemin de code — culture
maison, la 039 épingle par SHA256 plutôt que par confiance : un trigger `BEFORE UPDATE OR
DELETE` qui `RAISE EXCEPTION`.

> **`ON DELETE RESTRICT`, repris du PLAN `§4` — et c'est le bon choix pour une autre
> raison que la sienne.** Avec `RESTRICT`, aucune suppression en cascade n'existe : le
> trigger append-only peut refuser **tout** `UPDATE` et **tout** `DELETE` sans exception à
> écrire. Un `CASCADE` obligerait à distinguer le `DELETE` direct du `DELETE` cascadé dans
> le trigger — une exception de plus dans la garde qui protège le ledger de jugement.
> *(J'avais d'abord proposé `CASCADE` sans avoir relu le PLAN. Corrigé : le PLAN fait foi.)*
>
> **Coût à assumer** : supprimer une session ayant des checkpoints devient impossible sans
> les supprimer d'abord — et le trigger l'interdit. **Une session à checkpoints est donc
> indélébile.** C'est cohérent avec « append-only », mais ce n'est écrit nulle part
> ailleurs et un opérateur le découvrirait au premier `DELETE`.
>
> ⚠️ **Piège voisin, cité pour qu'on ne « corrige » pas le `RESTRICT`** : `CHECK` +
> `ON DELETE SET NULL` est un piège documenté du dossier (skill
> `postgres-check-vs-on-delete-set-null`). Ni `SET NULL` ni `CASCADE` ici.

**Index** : `uq_brain_session_checkpoints_session_seq` sert la clé d'idempotence **et** la
lecture par session. Aucun autre index n'est proposé — et surtout aucun sur
`brain_sessions`, dont la liste d'index est **fermée** par `expected_session_indexes` dans
les deux assets v4 (quatrième mécanisme de casse d'attestation, `§5.2 (ii)`).

### 3.1 Rollback

Le downgrade `DROP TABLE`. **Il perd des données de jugement** — il doit donc être
**fail-closed** sur le modèle du downgrade `037→036` : refuser s'il existe au moins un
checkpoint, et exiger un geste opérateur explicite pour passer outre.

### 3.2 Ce que M-C ne casse PAS, vérifié plutôt qu'affirmé

- **Attestation** : table NEUVE, aucun index sur `brain_sessions`, aucun CHECK de
  `brain_sessions` touché → l'empreinte du CHECK terminal et
  `expected_session_indexes` ne bougent pas. **À re-vérifier au moment d'écrire la
  migration**, pas à croire sur cette ligne.
- **Couloir du pin** : M-C est **une tête**, séquencée avec le bump de
  `_REQUIRED_ALEMBIC_HEAD` et son test dans le **même commit**. Elle ne part **pas** avec
  la tête M-A+M-G (`§0ter.1`) : jamais deux têtes en vol.

---

## 4. Le covenant passe à HUIT

La livraison du tool porte l'énumération du covenant de **sept à huit**. Trois objets
bougent **dans le même commit que le tool**, et c'est une friction voulue :

1. la **docstring** du 8ᵉ tool porte la phrase-covenant ;
2. **CLAUDE.md** énumère huit commandes ;
3. `tests/unit/mcp/tools/test_session_covenant_docstrings_anchor.py` :
   `_EXPECTED_TOOL_COUNT` passe à `8`, et le mot en toutes lettres passe à `"eight"`.
   *Le test a été écrit pour ça* (commit `0207209`).

> **⚠️ « HUIT » N'EST PEUT-ÊTRE PAS LE BON NOMBRE — voir `SPEC-pool-brouillons.md` §4.**
> Ce compte suppose que le checkpoint est le seul tool ajouté. La spec du pool de
> brouillons en propose **deux autres** — `brain_staged_capture_sign` et
> `brain_staged_capture_dismiss` — ce qui porterait l'énumération à **dix**. Les trois
> specs doivent s'accorder **avant la première livraison** : le test d'ancrage porte le
> nombre en toutes lettres et rougira une fois par tool. *Ce n'est pas du comptage, c'est
> la surface du contrat que lit un agent avant d'appeler.*
>
> **Interaction avec `§0ter` (d), à ne pas manquer.** La résolution ratifiée dit
> « **phrase-covenant RÉÉCRITE par nature**, pas supprimée ». Le 8ᵉ tool doit donc porter
> la variante **par nature**, pas la phrase historique. Les deux changements touchent le
> même test d'ancrage. **Proposition : les livrer dans le même commit** — sinon le test
> rougit deux fois pour deux raisons différentes, et la seconde sera lue comme du bruit.

---

## 5. Concurrence — les tests que l'audit exige

`d04dc588` demandait des « tests de concurrence ». Quatre, minimum :

1. **Replay exact** — deux appels identiques `(session_id, seq, progress, next_step, blocker)` :
   une seule ligne, second `replayed: true`, `created_at` **inchangé**.
2. **Collision de contenu** — même `(session_id, seq)`, contenu différent :
   `CheckpointSeqConflict` (§1.1), aucune ligne écrite, aucune ligne modifiée.
3. **Deux `seq` concurrents** — deux écrivains, `seq` distincts, en parallèle :
   deux lignes, aucune perdue, ordre par `seq` stable.
4. **Plafond sous course** — 200 atteint par deux écrivains simultanés : le 201ᵉ échoue
   **fail-closed**, et le compte réel ne dépasse jamais 200.

Plus deux tests d'**absence d'effet**, qui tombent en premier si quelqu'un recâble le
heartbeat : `last_heartbeat_at` **inchangé** après un checkpoint réel, et `focus_revision`
**inchangée** après un checkpoint.

---

## 6. Ce qui reste NON SPÉCIFIÉ, et attend une signature

1. **La détection de collision de contenu** (§1.1) — proposée, non signée.
2. **L'amendement de D4** sur l'effet heartbeat (§0) — proposé, non signé. *C'est le plus
   urgent : tant que D4 dit l'inverse de §0bis.4, l'ADR se contredit.*
2bis. **L'amendement du PLAN `§4`**, qui porte encore **la forme abandonnée** :
   `kind VARCHAR(20)` CHECK ∈ {progress, blocker, next_step, handoff} + `note TEXT`,
   plus « **ne rafraîchit pas le heartbeat une seconde fois** (refresh conditionné à
   `rowcount = 1`) », qui suppose un effet heartbeat que `§0bis.4` a dissous. Ses
   **critères de sortie** portent la même dette : *« un checkpoint rafraîchit
   `last_heartbeat_at` (test) »* est un test à **retirer**, pas à écrire.
   *Même règle du fil que pour le ticket : un document qui vit rend faux ce qui le cite.*
3. **La politique d'appel.** D4 la nomme « une fourche, déclarée, pas glissée » et elle
   n'est **pas** tranchée : le checkpoint reste-t-il une commande **explicite** de
   l'utilisateur (covenant intact, adoption dépendante de la même discipline humaine qui a
   produit 24 sessions stale sur 29, re-mesuré le 2026-08-19), ou un agent peut-il
   checkpointer de lui-même ? **Cette spec ne tranche pas** — mais elle observe que sous
   `§0bis.4`, checkpointer ne maintient plus rien vivant, donc **le risque de faux-vivant
   qui rendait la fourche dangereuse a disparu**. La question redevient un choix produit
   ordinaire.
4. **Le seuil de fraîcheur B7** : à partir de quel âge de dernier checkpoint une session
   est-elle « sémantiquement périmée » ? Non spécifié. À répondre **avec Q5 et le seuil
   4 h** — même famille, et le `§0ter.5` dit qu'ils se répondent ensemble.
5. **L'approbation produit explicite** que `d04dc588` exige (sa question ouverte n° 3).
   Elle gate la livraison, pas cette spec.

---

*Écrit le 2026-08-20 en Phase 0 — ZÉRO mutation. Aucune ligne de code, aucune migration,
aucun flag. Ce document est un livrable de la Phase 0, pas son exécution.*

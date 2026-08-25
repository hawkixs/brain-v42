# Mint v5 — les mesures, figées

> **Statut : MESURES PRISES, ASSETS NON ÉCRITS.** Ce document existe pour que
> l'écriture des assets v5 soit mécanique et vérifiable, plutôt que refaite de
> mémoire. Toutes les valeurs ci-dessous sont **mesurées le 2026-08-21**, avec
> les expressions exactes du contrat v4 — pas recalculées à la main.
>
> Doctrine : **S1** (décision `9d22bc6a`) — `alembic_head` **DÉRIVÉ**, un seul
> mint pour tout le couloir. Périmètre : **v5 MINIMAL** (décision `567f6298`) —
> 25 checks, mécanismes de la 046 ; le contrôle du trigger 041 part en ticket de
> suite (`23962510`).

## 0. La source du mint, et pourquoi ce n'est pas la production

La prod est à **`045`**. Les assets v5 doivent décrire l'**après-046**. Le mint se
frappe donc contre **`brain_test`**, qui est à `046`.

**Et cette source a été VALIDÉE, pas supposée.** Un miroir divergent ferait entrer
sa divergence dans l'attestation :

```
prod : 129 index    brain_test : 130 index
diff → une seule ligne : + public.brain_sessions.uq_brain_sessions_connection
tables → IDENTIQUES (32 des deux côtés)
```

Un écart, exactement celui de la 046. C'est la seule preuve qui autorise à minter
ailleurs que sur la prod.

## 1. Reçu de référence AVANT le mint

`22/25` mesuré le 2026-08-20 sur la prod à `045` (fil `eb067b57`). Trois échecs,
**tous bénins**, tous tracés à une migration postérieure au mint de v4 (figé à `039`) :

| Échec | Cause |
|---|---|
| `alembic_head` 039 ≠ 045 | six révisions de retard — **c'est S1 qui le supprime** |
| `catalog_counts.indexes` 128 ≠ 129 | `idx_dream_runs_date_project`, migration **042** |
| `view_column_mismatches` = 1 | `codex_dream_run_v1`, colonne `model` élargie par la **045** |

La 046 en ajoute deux : l'index de connexion (129 → **130**) et l'empreinte de
colonnes de `brain_sessions` (+5 colonnes).

**Ne pas recopier ce 22/25 : le rejouer.** Il changera à chaque tête.

## 2. Les valeurs mesurées — `brain_sessions`

Expressions reprises **littéralement** du contrat v4 :
- index : `md5(pg_get_indexdef(indexrelid))`
- contraintes : `md5(regexp_replace(lower(pg_get_constraintdef(oid, TRUE)), '[[:space:]]+', ' ', 'g'))`

### `expected_session_indexes` — liste FERMÉE, 3 → 4 entrées

| index | md5 | état |
|---|---|---|
| `brain_sessions_pkey` | `6763cd8159ef6f0131abbfedfea044bc` | inchangé |
| `idx_brain_sessions_project_status_started` | `daf2b70c6799177168837efedcb0dbe8` | inchangé |
| `uq_brain_sessions_project_client` | `28c33a3d73bf9f0c64d322978b7118a4` | inchangé |
| **`uq_brain_sessions_connection`** | **`62b298d247237eddf60cb4ba28693af4`** | **NEUF** |

⚠️ Cette liste est contrôlée **DEUX FOIS** (`v4.sql:665` absent-ou-md5-divergent,
`:687` présent-hors-liste). Une entrée oubliée casse dans les deux sens.

### `expected_session_constraints` — 8 → 9 entrées

| contrainte | v4 | v5 |
|---|---|---|
| `brain_sessions_status_valid` | `4f21eff965e8da6178bb2d1030fc03f8` | **`f5065acef0a32bfc97e66f6d802b9585`** |
| `brain_sessions_terminal_state_valid` | `9abfd0c69ce694043e32e1935d17ff4f` | **`aab51404804e113ec2c452ba0bc21aa8`** |
| **`brain_sessions_nature_valid`** | — | **`b3899128eb71e5e3023e994b0f1e26db`** (`'c'`, `NULL::text`) |

Inchangées : `capture_ids_valid` `1a8756bd34b4ea7e8d835643d0fa7ceb`,
`client_key_nonblank` `8ec1e8c3738bbe2178e04689dd038e0d`,
`focus_outcome_valid` `8d97a41480c2c3b6ec5a87bf0e64fb03`,
`pkey` `cc3552dbb61b18accca876af5296eb1f`,
`project_key_fkey` `b863ba166c02670d9dad0a56f9582d59` (`'f'`, `'r'`),
`uq_brain_sessions_project_client` `153c25b1acb665316ea262444b4d0d79`.

### `expected_session_constraint_fragments` — un QUATRIÈME littéral de statut

La définition observée, normalisée, est désormais :

```
check (status::text = any (array['open'::character varying, 'ended'::character varying,
'abandoned'::character varying, 'closed_inactive'::character varying]::text[]))
```

Le bloc code en dur les littéraux de statut : il en faut **quatre**.

### `catalog_counts`

`indexes` : **128 → 130**. `foreign_keys` : **26**, inchangé. `table_set` :
**inchangé**, aucune table neuve (32 avec `alembic_version`).

## 3. Ce qui reste à ÉCRIRE, et le piège à ne pas déclencher

1. **`alembic_head` devient DÉRIVÉ** (S1) — c'est la seule partie de CONCEPTION.
   Le check v4 est `{"kind": "alembic_head_equals", "revision": "039"}` et son CTE
   vit en `v4.sql:1766-1791`. L'invariant devient « une seule tête, cohérente avec
   le dépôt », gabarit `test_alembic_env.py:254-259` (« Le head est DÉRIVÉ, pas
   épinglé »). La révision exacte reste prouvée par `_REQUIRED_ALEMBIC_HEAD`.

2. ⚠️ **JAMAIS DE `sed` GLOBAL SUR « 039 ».** `v4.sql` en porte **sept**
   occurrences, et **cinq nomment l'invariant installé PAR la 039** —
   `recovery_039_observation` (l. 1766, 1964, 1966) et
   `project_context_updated_at_039` (l. 1962). **Seules deux** sont le pin de tête
   (l. 1789, 1791). Un remplacement global corromprait l'asset **en silence**.

3. **Le troisième asset : `brain-v42-v4.json`.** Nommé **zéro fois** dans les cinq
   documents de conception. Sa régénération **n'est pas une édition** :
   `test_v4_json_is_the_exact_v3_delta` le dérive de `v3.json` et assert l'octet —
   il faut écrire `_expected_v5()` sur le même gabarit, en partant de `v4.json`.

4. **Parité de CTE** entre `v5.sql` et `v5-pgrestore.sql` : écart autorisé =
   exactement `{observed_artifact_constraints, observed_session_constraints}`
   (`test_recovery_contract_v4_pgrestore.py:29-33`).

5. **Les deux portes du runbook** (`PLAN_INDEX_REPAIR_RUNBOOK.md`) : il annonce
   `24/25` et exige `25/25` comme porte d'autorisation avant `repair` — deux
   endroits, et le document se contredit déjà.

6. **`chmod 0600`** sur les assets v5 dès leur création (runbook l. 45). Fait pour
   `brain-v42-v4.sql` le 2026-08-20 ; les 11 assets sont uniformes aujourd'hui.

## 4. Ce que ce mint NE fait pas

Le contrôle du trigger `content_updated_at_041` (~270 lignes de SQL catalogue)
reste **hors périmètre** — ticket de suite `23962510`. Le contrat v5 garde donc
**25 checks**, pas 26.

---

*Mesures prises le 2026-08-21 entre 06:05 et 06:20, en lecture seule, contre
`brain_test` à la tête `046`. Aucune écriture, aucune migration jouée sur `brain`.*

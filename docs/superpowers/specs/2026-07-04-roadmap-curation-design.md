# Roadmap curée — purge, cureur nocturne, surfaçage sessions

**Date** : 2026-07-04
**Statut** : spec validée (brainstorm session 2026-07-04)
**Décisions liées** : `532b9401` (produit : red-monitor observe / red-codex travaille, pas de merge), `a91dc271` (webhook GitLab décommissionné), `3ed36af1` (résolution préfixe id — réutilisée par le write-back)

## 1. Contexte & diagnostic

Roadmap V2 (mars 2026) a un pipeline d'écriture qui marche — 652 features,
3 417 `feature_artifacts`, FeatureLinker + ClusterGuard lient chaque artifact
inséré, dernier liage le jour de cette spec — et une exploitation morte :

- `brain_get_roadmap` jamais appelé ; seul surfaçage = 2 features pinned
  stale (101 j) dans le briefing (« In-flight »).
- 500 features en `research` : ClusterGuard crée une feature par cluster
  d'artifacts non matchés, personne ne cure. Les ~40 vraies features
  (deployed/done/building) sont noyées.
- Statuts figés depuis juin : le StatusEngine mappe les types de signaux
  vers les statuts, et les seuls signaux au-dessus de `design` étaient les
  events GitLab (`mr_opened`→building, `mr_merged`/`pipeline_success`→
  deployed). Le webhook est mort le 2026-06-24 (secret perdu) et a été
  décommissionné le 2026-07-04 — l'échelle n'a plus de barreaux hauts.
- Bruit : project_keys fantômes (`red` 117 features, `refondrre` 36).

## 2. Décisions de cadrage (validées en brainstorm)

1. **Modèle émergent assumé + curation forte** : ClusterGuard continue de
   créer ; un cureur nocturne nettoie en continu. (Rejeté : hybride
   candidates/promotion ; déclaratif pur.)
2. **Stock initial** : purge mécanique sans LLM, puis LLM sur l'ambigu
   restant. (Rejeté : tout-LLM ; reset total.)
3. **Opérations du cureur** : merge doublons, archive mortes, transitions
   de statut, rename titres — les quatre, proposer-only.
4. **Sessions** : section briefing enrichie (remplace In-flight) +
   write-back par tool dédié. (Non retenus : get_roadmap seul ; « focus
   reste roi » explicite — le focus reste de fait la source du contexte
   narratif, la roadmap devient la vue structurée des features.)
5. **Architecture cureur** : pattern extract/backfill (CLI + killswitch +
   table proposals + review `--apply-ids`). (Rejeté : phase `claude -p` ;
   StatusEngine++ mécanique seul — preuve empirique contre : la mécanique
   existait et la roadmap a pourri quand même.)
6. **Produit** : la roadmap vit dans brain ; red-monitor l'affiche,
   red-codex la travaillera. Aucun chantier front dans cette spec.

## 3. §1 — Données : migration 030

- `features.status` : ajouter `archived` au CHECK
  (`features_status_check`, actuellement planned/research/design/building/
  deployed/done).
- `features.merged_into uuid NULL REFERENCES features(id)` — même pattern
  que decisions/learnings. Un merge re-pointe les `feature_artifacts` du
  perdant vers le survivant PUIS marque le perdant `merged_into=<survivant>`
  + `status='archived'`. Jamais de DELETE : le FK `feature_artifacts` est
  ON DELETE CASCADE, supprimer effacerait l'historique de liage.
  Attention au gotcha CHECK vs ON DELETE SET NULL (skill
  `postgres-check-vs-on-delete-set-null`) : aucun CHECK ne doit contraindre
  `merged_into`.
- Table `roadmap_curation_proposals` — miroir de
  `ticket_extraction_proposals` :

  ```
  id                bigserial PK
  op                varchar(10) NOT NULL CHECK (op IN ('merge','archive','status','rename'))
  feature_id        uuid NOT NULL REFERENCES features(id) ON DELETE CASCADE
  payload           jsonb NOT NULL      -- merge: {"into": uuid} · status: {"status": "building"}
                                        -- rename: {"name": "…"} · archive: {}
  rationale         text
  status            varchar(10) NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed','applied','rejected'))
  created_at        timestamptz NOT NULL DEFAULT now()
  applied_at        timestamptz
  ```

  Index : `(status)`, `(feature_id)`.

## 4. §2 — Purge mécanique : `scripts/roadmap_purge.py`

One-shot, SQL pur, `--dry` par défaut, rapport par projet. Règles :

1. `project_key` absent de `project_contexts` ET hors groupe `red`
   (réutiliser `get_keys_by_group`, parité vue codex) → `archived`.
   Le cas `red` (117 features) est vérifié à l'exécution : si `red` est
   une vraie clé legacy, la règle l'épargne et on tranche à la review.
2. Features à 0 artifact → `archived`.
3. Features à 1 artifact, aucun artifact lié créé depuis 60 j
   (max(`feature_artifacts.created_at`) — PAS `status_updated_at`), statut
   non terminal (ni deployed, ni done, ni archived) → `archived`.
4. `pinned=true` : jamais touchée par la purge.

Sortie attendue : ~100-150 features vivantes restantes pour le cureur.
Réversibilité : tout est `archived`, un UPDATE inverse suffit.

## 5. §3 — Cureur LLM : `scripts/roadmap_curate.py`

Squelette exact de `ticket_extract.py` : `_post_chat` partagé de
`domain_backfill` (retry timeouts inclus, `_exc_str`), NVIDIA API JSON
strict sans tools, `--limit`, `--dry` par défaut / `--wet`, `--apply-ids`,
row `dream_runs` phase=`roadmap`.

- **Batch par projet** : le prompt reçoit les features vivantes d'UN projet
  (statut non terminal, non archived, non merged) + par feature un digest
  des artifacts récents : titre, type, date — PAS les corps complets.
  Budget borné : cap ~30 features/projet/nuit, artifacts cap 10/feature
  (les plus récents).
- **Ops proposées** : les 4, format JSON array
  `[{op, feature_id, payload, rationale}]`, validation stricte à la
  `parse_and_validate` (op inconnu rejeté, feature_id hors batch rejeté,
  merge cross-projet rejeté).
- **Garde-fous** :
  - feature `pinned` : seule l'op `status` est proposable ;
  - features `done`/`archived` : intouchables ;
  - merge intra-projet uniquement, `into` doit être dans le batch ;
  - cap global N=40 proposals/nuit (log du drop si dépassé — pas de
    troncature silencieuse).
- **Apply (`--apply-ids`)** : transaction unique par proposal,
  post-conditions positives vérifiées après chaque op (relire la row et
  vérifier l'état attendu — pattern learnings F-09), `Result.mappings()`
  (gotcha mypy scripts/ : tests avec `MagicMock(spec=AsyncSession)`).
  `--wet` (propose+apply même run) existe mais N'EST JAMAIS utilisé par la
  nightly.

## 6. §4 — Step dream + killswitches

Bloc `dream.sh` à l'identique du step extract :

- `BRAIN_DREAM_ROADMAP_ENABLED=false` (défaut) +
  `BRAIN_DREAM_ROADMAP_DRY_RUN=true` — drop-in
  `killswitches.conf` (jamais dans l'unit : incident 2026-06-30).
- `timeout 10m`, log `${TIMESTAMP}_roadmap.log`, `SKIP roadmap
  (killswitch…)` loggé quand fermé, `FAIL roadmap (rc=…)` sinon.
- Ligne killswitch ROADMAP dans le briefing session_start (pattern
  KillswitchState existant, testé dans test_session_tools).
- Rollout : ≥2 nuits dry propres → review des proposals au matin
  (`--apply-ids` des bonnes) → quand le taux d'acceptation est stable et
  haut, envisager wet nocturne pour `archive`/`status` seulement (merge et
  rename restent à review indéfiniment).

## 7. §5 — Briefing : section « Roadmap » (remplace « In-flight »)

- Features **vivantes** du projet : statut ∉ {done, archived} ∧
  merged_into IS NULL, triées par dernière activité artifact desc, cap 5.
- Format : `- <nom> [<statut>] — <N> artifacts, dernier il y a <X>j`.
- `pinned` en tête de liste (mais soumis au même critère de vie).
- La section « Stale-pinned » existante est conservée telle quelle
  (alerte séparée).
- Dégradation gracieuse : erreur → section omise + warning structlog
  (contrat §9 du briefing).

## 8. §6 — Write-back : tool `brain_feature_update`

Nouveau tool MCP (surface 41→42) :

```
brain_feature_update(feature: str, status: str, project_key: str) -> str
```

- `feature` accepte : nom exact → préfixe d'id (≥8 hex,
  `resolve_entity_id` réutilisé, passthrough FeatureService/repo à créer
  sur le pattern resolve_id_prefix) → ILIKE unique sur le nom.
  Ambiguïté → erreur listant les candidats (id + nom). Aucun match →
  erreur explicite.
- `status` : valeurs du CHECK uniquement (dont `archived` — une session
  peut archiver une fausse feature à la main).
- Side-effects : `status_updated_at=now()`, `pinned=true` (même
  comportement que le chemin update_feature_statuses actuel).
- L'ancien chemin `brain_update_project_focus(feature_status=…)` reste
  fonctionnel (rétro-compat) mais la doc pointe vers le nouveau tool.
- CLAUDE.md projet : ajouter la consigne « feature livrée →
  `brain_feature_update(name, 'deployed'|'done') »`.

## 9. §7 — Sidecar dream metrics à jour (consommé par red-monitor)

Constaté le 2026-07-04 sur le /metrics live, deux défauts dans
`collector_dream.py` :

1. **Agrégat pollué par les re-runs** : la requête prend TOUTES les rows de
   `dream_runs` du dernier `run_date` (L46-53). Un run rejoué le même jour
   (ex. extract fail 06:13 puis done 10:58) donne `phases_fail:1,
   status:"partial"` alors que chaque phase a fini done. Fix :
   `DISTINCT ON (phase) … ORDER BY phase, id DESC` — dernière row par
   phase ; `phases_ok`/`phases_fail`/`status` calculés sur l'ensemble
   dédupliqué.
2. **Pas de flag dry/wet par phase** : `dream_runs.phase_dry_run` existe
   mais n'est pas exposé. Ajouter `"dry_run": bool` dans chaque entrée de
   `phases` — red-monitor peut badger wet/dry. Une phase absente du jour =
   killswitch fermé (le SKIP ne crée pas de row) : le front peut déduire
   « off » par diff avec les phases vues dans `history`.

Contraintes :

- **Contrat additif uniquement** : /metrics est consommé par red-monitor —
  nouvelles clés OK, aucun rename/suppression (réflexe contrats red-triad,
  synthèse `f32168ae`).
- **Restart `brain-metrics` obligatoire** après déploiement : le sidecar
  n'est dans aucune boucle de deploy (learning `f13144b3`).
- Les phases CLI (extract, roadmap) ont légitimement `cost:0`.
  **Amendé le 2026-08-05** : `model:null` ne vaut plus pour `roadmap`, qui
  renseigne désormais le modèle réellement utilisé. Laisser cette colonne
  nulle a masqué dix nuits servies par le modèle de secours après l'EOL du
  primaire (ticket `911bb6f5`). `extract` reste à `model:null`.

## 10. §8 — Successeur du signal GitLab

- **v1 (incluse)** : le cureur lit le CONTENU des artifacts — une decision
  « livré X », un runbook « déployer X », un plan status=done → propose
  `status: deployed`/`done`. Plus riche que l'ancien mapping par type
  d'event.
- **v2 (différée, hors scope)** : step nightly `gitlab_poll` — PULL de
  l'API GitLab (commits/MRs/pipelines par projet, token read-only) ingéré
  en `gitlab_events`. Zéro port exposé, zéro secret webhook : le pull
  répare ce qui a tué le push. À spécifier séparément si le besoin de
  corrélation commit↔feature revient.

## 11. Non-goals & risques

- **Non-goals v1** : ClusterGuard/FeatureLinker inchangés (l'écriture
  marche) ; aucun chantier front (red-monitor lit la même donnée en mieux
  via ses contrats existants ; red-codex plus tard) ; pas de gitlab_poll.
- **Risque Watchk** : chevauchement conceptuel (Watchk = features
  déclaratives manuelles, projet 6). Position : brain roadmap = émergent/
  auto-lié, Watchk = suivi déclaratif. À re-trancher si le doublon devient
  douloureux — hors scope ici.
- **Risque qualité cureur** : la roadmap dépend du jugement nocturne. Mitigé
  par : proposer-only + review, garde-fous durs, caps, et le précédent
  domain_backfill (36/36 acceptés).
- **Coût NVIDIA** : ~8 projets vivants × 1 appel/nuit, latence queue
  variable (retry en place). Borné par les caps.

## 12. Critères de succès

1. Purge : features vivantes ≤ 150, zéro project_key fantôme non arbitré.
2. Après 1 semaine de curation : statuts `deployed`/`done` reflètent les
   livraisons réelles de la semaine (vérifiable contre les focus).
3. Le briefing montre des features vivantes avec activité < 7 j (fini le
   « planned — updated 101d ago »).
4. Au moins un `brain_feature_update` réussi par session de livraison.
5. Sidecar : une nuit avec re-run d'une phase s'affiche `done` (pas
   `partial`), et chaque phase porte son flag `dry_run`.
6. Gates habituels : pytest vert, ruff, mypy src/, coverage ≥ 60 %.

## 13. Découpage TDD (indicatif pour le plan)

1. Migration 030 + tests schéma (pattern test_schema_indexes_027).
2. `roadmap_purge.py` + tests règles (mocked session, pas de vraie DB).
3. `roadmap_curate.py` propose + parse_and_validate + garde-fous.
4. Apply + post-conditions.
5. Step dream.sh + test_dream_sh_roadmap (pattern test_dream_sh_extract).
6. Section briefing (test_session_tools).
7. `brain_feature_update` + resolver (test tools, pattern préfixe id).
8. collector_dream : dédup DISTINCT ON + dry_run par phase
   (tests/unit/metrics/, contrat additif) + restart brain-metrics au deploy.
9. Docs : CLAUDE.md consigne, MCP_TOOLS, .env exemple killswitches.

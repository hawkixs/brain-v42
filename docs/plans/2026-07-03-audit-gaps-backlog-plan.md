# Audit projet 2026-07-03 — gaps, sujets oubliés, backlog d'idées

> Passe exhaustive multi-agents (workflow 9 agents : 4 sweeps brain-DB/code/docs/trackers,
> 3 lentilles d'idéation, synthèse + critique de complétude — 43 findings bruts, 21 idées).
> Entrées brain associées : décision `1b5fb11f` (soak invalidé + fix token),
> supersession `99e9f5f7`→`29df5dc8` (replatform), learnings `f13144b3` (sidecar),
> `d2ebb101` (sécu DB), `e3287cdf` (PROMOTE affamé), `6fac2352` (engagements datés),
> `ac78436a` (Spec C fantôme).

## Synthèse exécutive

Le cœur de brain_v42 est solide (89% coverage, cutover HTTP livré, Spec A PROMOTE en prod),
mais la couche autonome (Dream nightly) a enregistré une nuit AVEUGLE comme « 6/6 OK »
(4e récidive false-green) et PROMOTE n'a rien matérialisé depuis le 06-26 sans qu'aucune
alerte n'existe. Deuxième pathologie structurelle : le brain enregistre des engagements
datés mais ne les rappelle jamais (≥6 promesses pourries). La valeur court-terme est dans
l'observabilité (brain_doctor, canary, vitality gate, alerting PROMOTE) ; la valeur
structurelle est de faire porter ces pathologies par le modèle mémoire lui-même
(review_by, CONTRADICTS, cite_ratio). Spec B reste le gros pari, à lancer seulement une
fois le pipeline redevenu digne de confiance.

## Incident traité en session (2026-07-03 matin)

- **Dream 07-03 aveugle** : token absent de l'unit user → 401 → 0 tools brain → « 6/6 OK »
  mensonger. Fix : drop-in `token.conf` (EnvironmentFile 0600), validé E2E (probe 200 +
  claude -p headless charge 35 tools). **Soak REORG : compteur à zéro, flip WET ≥ 07-06.**
  Check matinal augmenté : `tool_calls > 0` obligatoire sur clean/connect/synth/reorg.

## Sujets oubliés

| # | Sujet | Depuis | Action suggérée |
|---|-------|--------|-----------------|
| 1 | Replatform embedding : rollback 06-27, SPOF NON résolu, décision mensongère (✅ supersédée `99e9f5f7`), état résiduel dev-pc (docker-ce masqué, tarball 9.2 GB, PS1 orphelin) | 06-27 | Mini-spec « WSL2 LAN exposure », trancher les 3 options (forwarder nssm/socat recommandé), dater le retry |
| 2 | Spec C RESONANCE jamais schedulée (0 run, killswitch ouvert, code mergé) + brain-v42 à 1 edge domaine vs 108 red-shrik | 06-12 | Wiring post-CONNECT + 5 nuits DRY (runbook `0a4467ca`) + backfill domaines |
| 3 | cite_ratio anti echo-drift : validé empiriquement 05-08 (insights récents à 82-91% = CRITICAL), jamais codé | 04-25 | Exécuter le design de `c81d976b` (effort S) |
| 4 | Spec A §9 : réévaluation scope +4 sem. dépassée de ~6 sem. ; drill tombstone 0/11 promotions | 05-22 | Décision explicite (étendre ou re-différer daté) + drill 30-45 min |
| 5 | Gate cutover Task 5.3 jamais fermé — reaper q15min sans critère de décommission | 06-29 | Après 1 nuit dream propre + 48h stable : reaper log-only + décision de preuve |
| 6 | Spec B méta-synthèse promise « next » en avril — 0 doc, 0 commit | 04-17 | Trancher : MVP (voir idées) ou abandon explicite |
| 7 | Graph hardening Angle 2 (CONTRADICTS/APPLIED_TO) « future spec » jamais écrite | 04-24 | MVP write-time (voir idées) |
| 8 | Code Mode câblé 03-12, jamais testé ni tranché ; fastmcc.experimental peut casser silencieusement | 03-12 | Trancher <1h : test unique ou suppression (recommandé) |
| 9 | Pollution test dans le corpus prod : « E2E test decision — DELETE ME » + ~15 entités, sentinelles ZZQX, 2 specs poubelle | 06-24 | Purge manuelle ~30 min (pas de subsystem — sur-ingénierie) |
| 10 | MCP completeness : pagination semantic_search + 3 Future Considerations différées sans suite | 03-15 | Clore explicitement (won't-do annoté ou feature datée) |

## Gaps & dettes (priorisés)

**HIGH**
1. ~~Dream aveugle token~~ ✅ fixé en session (décision `1b5fb11f`)
2. **False-green systémique** : `detect_terminal_failure` signature-only, `post_run_alert` seulement si FAIL_TOTAL>0 ; le croisement `preflight=RUN × tool_calls=0` lève l'objection historique (dream_parser.py:80-109) — fix générique désormais possible
3. ~~Soak REORG invalidé~~ ✅ acté (compteur 0, flip ≥07-06)
4. **PROMOTE affamé** : 20/28 nuits dedup_unavailable, 0 promotion depuis 06-26, zéro alerting (learning `e3287cdf`) ; investiguer la vague 06-13→25 (« tools absent from namespace » malgré le flag bloquant)
5. **SÉCU (critique)** : PG 5433 + Neo4j 7687 sur 0.0.0.0, creds faibles `brain:brain` documentés — porte arrière DB contourne tout le hardening MCP (learning `d2ebb101`)

**MEDIUM**
6. CI : 39 tests DB-backed des validators PROMOTE/REORG jamais exécutés (BRAIN_V42_TEST_DB_URL absent des jobs) + `scripts/dream/` hors du gate coverage
7. Thresholds : 8/9 hand-picked (dont search_min_score=0.20), calibration arrêtée après 1 seuil
8. Drift documentaire systémique : CLAUDE.md (« M5 en cours », « 30 tools », « stdio »), README (34 vs 36 tools, 19 vs 27 migrations), commentaires killswitch de dream.sh inversés, roadmap brain périmée, 138 cases non cochées sur 3 plans livrés

**LOW**
9. Coverage localisée : indexed_plan_search_service 22%, metrics/__main__ 42%, pg_indexed_plan_repo 51%
10. Flaky PytestUnraisableExceptionWarning (AsyncMock, full-suite only) ouvert depuis 06-12
11. Branche `feat/mcp-http-server-foundation` mergée non supprimée ; GitLab 0 issue (le brain EST le tracker — par design)

## Backlog d'idées (retenues par la synthèse)

| Idée | Effort | Valeur | Note |
|------|--------|--------|------|
| `brain_doctor` + canary fail-fast en tête de dream.sh | S | high | Probe PG/Neo4j/embedding/reranker/**auth** → manifeste de capacités ; abort + alerte avant de payer Opus |
| Vitality gate `detect_anomalous_night()` | M | high | `done`+BLOCKED, ou `preflight=RUN × tool_calls=0`, ou promote dedup_unavailable → fail/partial + alerte ; TDD avec les logs du 07-03 en fixtures |
| Alerting débit PROMOTE (streaks + nuits sans promotion) | S | high | Le SLO manquant de Spec A ; aurait attrapé les 3 vagues |
| cite_ratio garde-fou du SYNTH | S | high | Design déjà écrit (`c81d976b`) : parse trailer + gauge + section REQUIRED + skip soft >60% |
| Engagements datés : `review_by` + `brain_commitments` + briefing « échéances dépassées » | M | high | Attaque la pathologie n°1 (learning `6fac2352`) |
| CONTRADICTS au write-time (warning inline + edge + contested) | M | high | Cas réel : `29df5dc8` vs `a09fbfbf` ; résout follow-up n°2 Spec C + Angle 2 |
| Backfill classification domaine + métrique de couverture | S | medium | Débloque le briefing cross-project brain-v42 (glue code) |
| Soak ledger codifié (`dream_soak_nights` + flip refusé sans preuve) | S | medium | Le soak « à la main » vient de prouver qu'il est falsifiable ; sécurise tous les rollouts dry→wet futurs |
| Spec B MVP : digests hebdo par communautés (LazyGraphRAG) | L | high | PRÉREQUIS : idées 1-4 livrées d'abord |

## Quick wins (~2h30)

- [x] Fix token dream + validation E2E *(fait en session)*
- [x] Soak 07-03 invalidé, flip repoussé *(décision `1b5fb11f`)*
- [x] Superseder `29df5dc8` *(fait : `99e9f5f7`)*
- [ ] Purger la pollution test du corpus (E2E DELETE ME, ZZQX, specs `346865e7`/`a7d3d3e4`)
- [ ] Roadmap brain : passer done les features shippées, archiver les mortes
- [ ] `git branch -d feat/mcp-http-server-foundation`
- [ ] Refresh CLAUDE.md + README (36 tools, 27 migrations, HTTP :8765, PROMOTE WET, M5 retiré)
- [ ] Commentaires killswitch dream.sh:18-33 + bandeau DONE sur les 3 plans livrés
- [ ] Trancher Code Mode (suppression recommandée) avec brain_log_decision

## Critique de complétude (angles hors rapport)

- **Sécu données** : vérifié, trou réel → learning `d2ebb101` (priorité HIGH ci-dessus)
- **Backups** : PG couvert par red-backup (quotidien 05:00, 7/7 OK au 07-03) ; **Neo4j absent
  du backup** (reconstructible par reconcile — choix à documenter) ; **aucun restore drill** ;
  dumps pré-migration 03-02 qui traînent dans ~/backups/
- **Alembic** : propre (27 migrations, head unique, down_revision complets)
- **Non couverts** : coûts API Dream agrégés/mensuels, perfs/bloat pgvector, CVE deps
- **Chiffres à re-vérifier avant décision chiffrée** : « 15 » entités pollution (16 comptées),
  « 20 » dedup_unavailable (18 ids cités), « ~1,04$/nuit » non sourcé

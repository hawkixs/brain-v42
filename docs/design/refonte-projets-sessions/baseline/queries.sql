-- Baseline Phase 0 — refonte PROJETS + SESSIONS
--
-- LECTURE SEULE, GARANTIE PAR LA TRANSACTION. L'appelant ouvre `BEGIN READ ONLY`
-- (voir snapshot.py) : toute écriture accidentelle échoue au lieu de passer.
--
-- UN SEUL statement, donc UNE SEULE transaction, donc UN instantané COHÉRENT.
-- Des mesures réparties sur plusieurs transactions pourraient se contredire entre
-- elles et personne ne le verrait.
--
-- CHAQUE MESURE PORTE SON CAVEAT. C'est le mode de panne nommé de ce chantier :
-- les « 10/59 contextes à focus NULL » étaient JUSTES et ne prouvaient pas ce qu'on
-- leur faisait dire ; les « 479 artefacts » étaient JUSTES le 2026-08-08 et périmés
-- dix jours plus tard. Un nombre sans son caveat est un piège, pas une mesure.

WITH
-- ── S1 · sessions par statut ────────────────────────────────────────────────
s1 AS (
  SELECT jsonb_object_agg(status, n) AS v
  FROM (SELECT status, count(*) AS n FROM brain_sessions GROUP BY status) t
),
-- ── S2 · fraîcheur des sessions ouvertes ────────────────────────────────────
s2 AS (
  SELECT jsonb_build_object(
    'open_total',      count(*),
    'stale_gt_24h',    count(*) FILTER (WHERE last_heartbeat_at < now() - interval '24 hours'),
    'sweepable_gt_7d', count(*) FILTER (WHERE last_heartbeat_at < now() - interval '7 days'),
    'oldest_heartbeat_age_days',
      round(EXTRACT(epoch FROM (now() - min(last_heartbeat_at))) / 86400.0, 1)
  ) AS v
  FROM brain_sessions WHERE status = 'open'
),
-- ── S3 · distribution des client_key ────────────────────────────────────────
s3 AS (
  SELECT jsonb_build_object(
    'distinct_client_keys', count(DISTINCT client_key),
    'total_sessions',       count(*),
    'reuse_ratio',          round(count(*)::numeric / NULLIF(count(DISTINCT client_key), 0), 2)
  ) AS v
  FROM brain_sessions
),
-- ── S4 · taux de CAPTURE sur 30 j ───────────────────────────────────────────
-- « Capturante » = au moins une ligne dans brain_session_artifacts. On NE se fie
-- PAS à captured_knowledge_ids : le CHECK 037 force ce tableau à vide sur la
-- branche `abandoned`, alors que le ledger réel, lui, survit.
s4 AS (
  SELECT jsonb_build_object(
    'window_days', 30,
    'closed_total', count(*),
    'closed_capturing', count(*) FILTER (WHERE a.n > 0),
    'rate_pct', round(100.0 * count(*) FILTER (WHERE a.n > 0) / NULLIF(count(*), 0), 1),
    'ended_total', count(*) FILTER (WHERE s.status = 'ended'),
    'ended_capturing', count(*) FILTER (WHERE s.status = 'ended' AND a.n > 0),
    'abandoned_total', count(*) FILTER (WHERE s.status = 'abandoned'),
    'abandoned_capturing', count(*) FILTER (WHERE s.status = 'abandoned' AND a.n > 0)
  ) AS v
  FROM brain_sessions s
  LEFT JOIN LATERAL (
    SELECT count(*) AS n FROM brain_session_artifacts x WHERE x.session_id = s.id
  ) a ON true
  WHERE s.status IN ('ended', 'abandoned')
    AND s.ended_at >= now() - interval '30 days'
),
-- ── S5 · taux d'ATTRIBUTION sur 30 j ────────────────────────────────────────
created_30d AS (
  SELECT id, project_key FROM decisions     WHERE created_at >= now() - interval '30 days'
  UNION ALL SELECT id, project_key FROM learnings     WHERE created_at >= now() - interval '30 days'
  UNION ALL SELECT id, project_key FROM snippets      WHERE created_at >= now() - interval '30 days'
  UNION ALL SELECT id, project_key FROM runbooks      WHERE created_at >= now() - interval '30 days'
  UNION ALL SELECT id, project_key FROM adrs          WHERE created_at >= now() - interval '30 days'
  UNION ALL SELECT id, project_key FROM indexed_plans WHERE created_at >= now() - interval '30 days'
),
s5 AS (
  SELECT jsonb_build_object(
    'window_days', 30,
    'created', count(*),
    'attributed', count(*) FILTER (WHERE x.knowledge_id IS NOT NULL),
    'rate_pct', round(100.0 * count(*) FILTER (WHERE x.knowledge_id IS NOT NULL)
                      / NULLIF(count(*), 0), 1)
  ) AS v
  FROM created_30d c
  LEFT JOIN brain_session_artifacts x ON x.knowledge_id = c.id
),
-- ── S6 · masse par clé colon ────────────────────────────────────────────────
all_knowledge AS (
  SELECT project_key FROM decisions
  UNION ALL SELECT project_key FROM learnings
  UNION ALL SELECT project_key FROM snippets
  UNION ALL SELECT project_key FROM runbooks
  UNION ALL SELECT project_key FROM adrs
  UNION ALL SELECT project_key FROM indexed_plans
),
s6 AS (
  SELECT jsonb_build_object(
    'colon_keys', COALESCE((
      SELECT jsonb_object_agg(project_key, n)
      FROM (SELECT project_key, count(*) AS n FROM all_knowledge
            WHERE project_key LIKE '%:%' GROUP BY project_key) t
    ), '{}'::jsonb),
    'colon_total', (SELECT count(*) FROM all_knowledge WHERE project_key LIKE '%:%'),
    'parents_of_colon_keys', COALESCE((
      SELECT jsonb_object_agg(project_key, n)
      FROM (SELECT project_key, count(*) AS n FROM all_knowledge
            WHERE project_key IN (
              SELECT DISTINCT split_part(project_key, ':', 1)
              FROM all_knowledge WHERE project_key LIKE '%:%')
            GROUP BY project_key) t
    ), '{}'::jsonb)
  ) AS v
),
-- ── S7 · focus : la VENTILATION, seule à distinguer « jamais écrit » d'« effacé »
s7 AS (
  SELECT jsonb_build_object(
    'contexts_total', count(*),
    'focus_null', count(*) FILTER (WHERE current_focus IS NULL),
    'focus_null_never_written',
      count(*) FILTER (WHERE current_focus IS NULL AND focus_revision = 0
                         AND focus_updated_at IS NULL),
    'focus_null_previously_written',
      count(*) FILTER (WHERE current_focus IS NULL
                         AND (focus_revision > 0 OR focus_updated_at IS NOT NULL)),
    'focus_revision_max', max(focus_revision),
    'focus_revision_median',
      percentile_cont(0.5) WITHIN GROUP (ORDER BY focus_revision),
    'focus_dated', count(*) FILTER (WHERE focus_updated_at IS NOT NULL),
    'focus_stale_gt_30d',
      count(*) FILTER (WHERE focus_updated_at < now() - interval '30 days')
  ) AS v
  FROM project_contexts
),
-- ── S10 · population d'ambiguïté (PLAFOND, voir caveat) ─────────────────────
open_by_project AS (
  SELECT project_key, count(*) AS n FROM brain_sessions
  WHERE status = 'open' GROUP BY project_key
),
s10 AS (
  SELECT jsonb_build_object(
    'projects_with_open', count(*),
    'projects_with_ge2',  count(*) FILTER (WHERE n >= 2),
    'open_in_ambiguous_projects', COALESCE(sum(n) FILTER (WHERE n >= 2), 0),
    'open_total', COALESCE(sum(n), 0),
    'per_project', COALESCE((SELECT jsonb_object_agg(project_key, n)
                             FROM open_by_project WHERE n >= 2), '{}'::jsonb)
  ) AS v
  FROM open_by_project
),
-- ── S11 · tickets : self vs cross (prédicat Q2) ─────────────────────────────
s11 AS (
  SELECT jsonb_build_object(
    'total', count(*),
    'self',  count(*) FILTER (WHERE from_project = to_project),
    'cross', count(*) FILTER (WHERE from_project <> to_project),
    'cross_pct', round(100.0 * count(*) FILTER (WHERE from_project <> to_project)
                       / NULLIF(count(*), 0), 1)
  ) AS v FROM tickets
),
-- ── S12 · index réels de brain_sessions (R1.5 : liste FERMÉE dans l'attestation)
s12 AS (
  SELECT jsonb_build_object(
    'count', count(*),
    'names', jsonb_agg(indexname ORDER BY indexname),
    'covers_an_actor_column', bool_or(indexdef ILIKE '%actor%')
  ) AS v FROM pg_indexes WHERE tablename = 'brain_sessions'
),
-- ── S13 · cardinalités pour dimensionner les futures tables ─────────────────
s13 AS (
  SELECT jsonb_build_object(
    'brain_sessions', (SELECT count(*) FROM brain_sessions),
    'brain_session_artifacts', (SELECT count(*) FROM brain_session_artifacts),
    'knowledge_total', (SELECT count(*) FROM all_knowledge),
    'project_contexts', (SELECT count(*) FROM project_contexts),
    'artifacts_per_capturing_session_max', COALESCE((
      SELECT max(n) FROM (SELECT count(*) AS n FROM brain_session_artifacts
                          GROUP BY session_id) t), 0)
  ) AS v
),
-- ── S14 · access_log : pourquoi il ne peut PAS servir d'instrument ──────────
s14 AS (
  SELECT jsonb_build_object('rows', count(*)) AS v FROM access_log
),
-- ── S15 · head Alembic ──────────────────────────────────────────────────────
s15 AS (
  SELECT jsonb_build_object('head', (SELECT version_num FROM alembic_version)) AS v
)
SELECT jsonb_pretty(jsonb_build_object(
  'measured_at_utc', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'alembic_head', (SELECT v FROM s15),
  'measurements', jsonb_build_object(
    'S1_sessions_by_status', jsonb_build_object(
      'value', (SELECT v FROM s1),
      'proves', 'La forme du parc de sessions.',
      'does_not_prove', 'Rien sur la fraîcheur : un `open` peut dater de six mois.'),
    'S2_open_freshness', jsonb_build_object(
      'value', (SELECT v FROM s2),
      'proves', 'Combien de sessions ouvertes le sweep 7j prendrait, et combien sont stale >24h.',
      'does_not_prove', 'Qu''elles sont MORTES. `is_stale` est un filtre dérivé du seul heartbeat, qui est une commande explicite : une session vivante dont personne ne bat le coeur paraît morte. C''est B2, dans son sens faux-mort.'),
    'S3_client_key_distribution', jsonb_build_object(
      'value', (SELECT v FROM s3),
      'proves', 'L''ampleur de la prolifération mécanique des client_key (B9).',
      'does_not_prove', 'Qu''une clé réutilisée soit le MÊME travail : la clé est libre et déclarée.'),
    'S4_capture_rate_30d', jsonb_build_object(
      'value', (SELECT v FROM s4),
      'proves', 'La part des sessions fermées qui ont attribué au moins un artefact (B3).',
      'does_not_prove', 'Qu''il y avait quelque chose à capturer. Une session sans artefact produit peut légitimement ne rien capturer — c''est ce que `nothing_to_capture_reason` déclare. Croiser avec S5 avant de conclure.'),
    'S5_attribution_rate_30d', jsonb_build_object(
      'value', (SELECT v FROM s5),
      'proves', 'La part des artefacts créés qui sont rattachés à une session (B3). C''est la moitié SOLIDE de B3 : le dénominateur ne dépend d''aucune déclaration.',
      'does_not_prove', 'Que les non-attribués soient des oublis : un artefact créé hors de toute session (dream, script) n''a pas de session à qui appartenir.'),
    'S6_colon_mass', jsonb_build_object(
      'value', (SELECT v FROM s6),
      'proves', 'La masse réelle par clé colon et celle de leurs parents (B11).',
      'does_not_prove', 'Qu''elle soit hors consolidation : deux clés colon sont au pool dream depuis le 2026-08-10. Le pool vit dans le drop-in systemd, PAS en base — cette requête ne peut pas le voir. Lire `killswitches.conf` avant de conclure.'),
    'S7_focus_state', jsonb_build_object(
      'value', (SELECT v FROM s7),
      'proves', 'La VENTILATION qui distingue « jamais écrit » (revision 0 ET jamais daté) d''« effacé » (revision > 0 OU daté). C''est elle, et elle seule, qui instruit Q13.',
      'does_not_prove', 'QUI a écrit. La ventilation par écrivain est INCALCULABLE aujourd''hui — project_contexts ne garde qu''un compteur sans auteur, il n''existe aucune table d''historique (c''est ce que la Phase 3 crée), et access_log est purgé à chaque flush (voir S14). C''est un RÉSULTAT de la Phase 3, pas son préalable.'),
    'S10_ambiguity_population', jsonb_build_object(
      'value', (SELECT v FROM s10),
      'proves', 'Le PLAFOND de la population que la règle « exactement un » laisserait non observée (Q1).',
      'does_not_prove', 'Le chiffre réel. Ce comptage est PAR PROJET, pas par couple (projet, acteur) : dès que deux acteurs distincts se partagent un projet, l''ambiguïté réelle est plus petite. Il ne pourra être raffiné qu''après M-A, `started_by_actor` n''existant pas — et jamais rétroactivement.'),
    'S11_ticket_predicate', jsonb_build_object(
      'value', (SELECT v FROM s11),
      'proves', 'Sur quelle part du corpus le choix from_project/to_project change quelque chose (Q2).',
      'does_not_prove', 'Que les self-tickets soient sans enjeu : le prédicat fixe la SÉMANTIQUE pour tous, y compris ceux où il ne départage rien.'),
    'S12_session_indexes', jsonb_build_object(
      'value', (SELECT v FROM s12),
      'proves', 'Les index réels, et qu''aucun ne couvre une colonne d''acteur — ce qui instruit la décision d''index de M-A.',
      'does_not_prove', 'Que l''index soit nécessaire : il faut l''EXPLAIN du statement D5, mesuré à part (voir explain.sql).'),
    'S13_cardinalities', jsonb_build_object(
      'value', (SELECT v FROM s13),
      'proves', 'De quoi fixer les plafonds de sortie des futures tables (checkpoints, staged).',
      'does_not_prove', 'Le régime futur : les tables checkpoints et staged n''existent pas et leur trafic est une hypothèse.'),
    'S14_access_log', jsonb_build_object(
      'value', (SELECT v FROM s14),
      'proves', 'Que access_log ne peut PAS servir d''instrument de mesure historique : c''est un TAMPON purgé à chaque flush du decay (300 s par défaut). Un compte à 0 est le régime NORMAL, pas une panne.',
      'does_not_prove', 'Qu''il n''y ait pas eu d''accès.')
  )
));

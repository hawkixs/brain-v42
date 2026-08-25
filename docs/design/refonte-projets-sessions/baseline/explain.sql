-- Coût du statement de l'émetteur D5 sur le chemin chaud — LECTURE SEULE.
-- Encadré par BEGIN READ ONLY par l'appelant.
--
-- `started_by_actor` n'existe PAS encore : on mesure le SQUELETTE
-- (`status = 'open' AND project_key = :pk`) plus la sous-requête corrélée de
-- comptage qui implémente « exactement un », et on DÉCLARE l'extrapolation.
-- C'est la voie que le PLAN §2 autorise explicitement.

\echo '=== A · squelette D5 : filtre + sous-requete correlee de comptage ==='
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING ON)
SELECT id FROM brain_sessions s
WHERE s.status = 'open'
  AND s.project_key = 'brain-v42'
  AND (SELECT count(*) FROM brain_sessions b
       WHERE b.status = 'open' AND b.project_key = s.project_key) = 1;

\echo ''
\echo '=== B · lookup par CONNEXION (la forme que le cadrage du 2026-08-19 impose) ==='
\echo '--- substitut : egalite sur une colonne NON couverte par un index ---'
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING ON)
SELECT id FROM brain_sessions
WHERE status = 'open' AND started_focus_revision = 212;

\echo ''
\echo '=== C · balayage complet, borne haute a la cardinalite du jour ==='
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING ON)
SELECT count(*) FROM brain_sessions;

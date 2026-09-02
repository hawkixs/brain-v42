#!/usr/bin/env bash
# Catch-up run for the ten biggest — one night per project, in sequence (the
# dream.sh lock is global), with a LEAK GUARD between each.
#
# The server scope is off (BRAIN_DREAM_CAPABILITY_ENFORCEMENT absent, the five
# DreamProjectToolPolicy() empty): a night launched for one project CAN mutate
# another's knowledge. `watchk-claude` did not do so on 2026-08-09, over 4
# phases and 109 entities — that proves nothing for 870.
#
# The guard compares the five knowledge tables, project by project, before and
# after each night. Any variation ANYWHERE but on the project served stops the
# chain: compounding a leak over seven more nights would make it unrepairable.
#
# Order: increasing mass. We measure how the duration scales before
# reaching `red` (870), and a leak is cheaper to repair on 113 entities
# than on 870.
#
# This script stays the MANUAL escape hatch, and it keeps one thing the
# nightly loop does not have: the leak guard between two projects. The
# `dream.sh` loop serves the pool without ever comparing the knowledge
# tables — until step 8 ships, it is here, and only here, that a write
# overflow stops the chain.
#
# Usage : rattrapage-10-gros.sh [projet ...]
#   With no argument, the default list below (the eight already caught up on
#   2026-08-09). With arguments, that list is replaced.
set -uo pipefail

# Root derived from the script's own location: it once lived in `.claude/` with
# a hard-coded absolute path, which tied it to one machine. A versioned script
# cannot know the directory of whoever wrote it.
# The CANONICAL form, to the letter: `check_container_image_pins.py` compares
# this line against a literal constant to decide whether `$SCRIPT_DIR` is
# trusted. The variant with `--` was equivalent for bash and unknown to the
# gate, which then refused every path derived from the variable.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
cd "$REPO_ROOT" || exit 1

# `logs/` is already ignored by git (.gitignore). The snapshots carry project
# names and entity counts: they do not belong in the index.
OUT="logs/dream-rattrapage/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"

PROJETS=(auto-discord "red-lab:architect" refondrre red-lab red-shrik red-writer "red-shrik:agent" red)
if [[ $# -gt 0 ]]; then
  PROJETS=("$@")
fi

# The guard is worth something only if it fails LOUDLY. First writing: the
# `group by 1` covered the whole expression, count(*) included — PostgreSQL
# refused, the "before" file contained the error message, the "after" one did
# too, and the diff was empty. The guard was disabled in silence, which is worse
# than no guard at all. So it now validates its own output.
snapshot() {
  local cible="$1" sortie rc brut
  # Redirection rather than substitution: `check_container_image_pins.py`
  # refuses any Docker execution inside a `$(…)`, and it is right to — the rule
  # does not know the verb and so cannot prove there is no image ingress. The
  # gate running BEFORE pytest in `test:unit`, a violation here does not fail
  # alone: it stops the whole unit suite from running.
  brut=$(mktemp)
  docker exec brain_v42_postgres psql -U brain -d brain -Atc "
    with a as (
      select project_key from learnings union all select project_key from decisions
      union all select project_key from snippets union all select project_key from runbooks
      union all select project_key from adrs
    )
    select coalesce(project_key,'<null>')||'='||count(*)
    from a group by coalesce(project_key,'<null>') order by 1;" > "$brut" 2>&1
  rc=$?
  sortie=$(<"$brut")
  rm -f "$brut"

  if [[ $rc -ne 0 ]] || (( $(grep -c '=' <<<"$sortie") < 40 )); then
    echo "!!! SNAPSHOT INVALIDE — la garde de fuite serait aveugle. Arrêt." >&2
    echo "$sortie" >&2
    exit 4
  fi
  printf '%s\n' "$sortie" > "$cible"
}

echo "=== Rattrapage démarré $(date -Is) ==="
echo "Projets : ${PROJETS[*]}"
echo "Sortie  : $OUT"
echo

for projet in "${PROJETS[@]}"; do
  echo "───────────────────────────────────────────────────────"
  echo ">>> $projet — $(date +%H:%M:%S)"
  snapshot "$OUT/avant-$projet.txt"

  debut=$SECONDS
  "$SCRIPT_DIR/../dream.sh" "$projet" > "$OUT/nuit-$projet.log" 2>&1
  rc=$?
  duree=$(( SECONDS - debut ))

  snapshot "$OUT/apres-$projet.txt"

  # Leak guard: any line that differs and does NOT concern the project served.
  fuite=$(diff "$OUT/avant-$projet.txt" "$OUT/apres-$projet.txt" \
          | grep -E '^[<>]' | grep -v "^[<>] ${projet}=" || true)

  # What the night wrote into dream_runs, with its key. Same gate constraint as
  # `snapshot`: redirection, never Docker inside a substitution.
  brut_runs=$(mktemp)
  docker exec brain_v42_postgres psql -U brain -d brain -Atc \
    "select count(*)||' lignes, clés: '||string_agg(distinct coalesce(project_key,'<NULL>'),',')
     from dream_runs where run_date=current_date and created_at >= now() - interval '$((duree+60)) seconds';" \
    > "$brut_runs" 2>&1
  lignes=$(<"$brut_runs")
  rm -f "$brut_runs"

  echo "    rc=$rc  durée=${duree}s  dream_runs: $lignes"
  echo "    $(grep -c 'DONE ' "$OUT/nuit-$projet.log" 2>/dev/null || echo 0) phases DONE, $(grep -c 'FAIL \|TIMEOUT ' "$OUT/nuit-$projet.log" 2>/dev/null || echo 0) en échec"

  if [[ -n "$fuite" ]]; then
    echo
    echo "!!! FUITE DÉTECTÉE — la nuit de '$projet' a modifié un AUTRE projet."
    echo "$fuite"
    echo
    echo "CHAÎNE ARRÊTÉE. Projets non traités : ${PROJETS[*]}"
    echo "C'est l'étape 8 (scope serveur) qui manque, pas un bug de ce lot."
    exit 3
  fi
  echo "    garde de fuite : OK (aucun autre projet touché)"
  echo
done

echo "═══════════════════════════════════════════════════════"
echo "=== Rattrapage terminé $(date -Is) ==="

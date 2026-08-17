#!/usr/bin/env bash
# Rattrapage des 10 gros — une nuit par projet, en séquence (le verrou de
# dream.sh est global), avec une GARDE DE FUITE entre chaque.
#
# Le scope serveur est éteint (BRAIN_DREAM_CAPABILITY_ENFORCEMENT absente, les
# cinq DreamProjectToolPolicy() vides) : une nuit lancée pour un projet PEUT
# muter la connaissance d'un autre. `watchk-claude` ne l'a pas fait le
# 2026-08-09, sur 4 phases et 109 entités — ça ne prouve rien pour 870.
#
# La garde compare les cinq tables de connaissance, projet par projet, avant et
# après chaque nuit. Toute variation AILLEURS que sur le projet servi arrête la
# chaîne : composer une fuite sur sept nuits de plus la rendrait irréparable.
#
# Ordre : masse croissante. On mesure comment la durée passe à l'échelle avant
# d'arriver à `red` (870), et une fuite est moins chère à réparer sur 113 que
# sur 870.
#
# Ce script reste l'échappatoire MANUELLE, et il garde une chose que la boucle
# nocturne n'a pas : la garde de fuite entre deux projets. La boucle de
# `dream.sh` sert le pool sans jamais comparer les tables de connaissance —
# tant que l'étape 8 n'est pas livrée, c'est ici, et seulement ici, qu'un
# débord d'écriture arrête la chaîne.
#
# Usage : rattrapage-10-gros.sh [projet ...]
#   Sans argument, la liste par défaut ci-dessous (les huit déjà rattrapés le
#   2026-08-09). Avec arguments, cette liste est remplacée.
set -uo pipefail

# Racine dérivée de la position du script : il a vécu dans `.claude/` avec un
# chemin absolu en dur, ce qui le liait à une machine. Un script versionné ne
# peut pas connaître le répertoire de qui l'a écrit.
# Forme CANONIQUE, à la lettre près : `check_container_image_pins.py` compare
# cette ligne à une constante littérale pour décider si `$SCRIPT_DIR` est de
# confiance. La variante avec `--` était équivalente pour bash et inconnue du
# gate, qui refusait alors tout chemin dérivé de la variable.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
cd "$REPO_ROOT" || exit 1

# `logs/` est déjà ignoré par git (.gitignore). Les snapshots portent des noms
# de projet et des décomptes d'entités : ils ne vont pas dans l'index.
OUT="logs/dream-rattrapage/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"

PROJETS=(auto-discord "red-lab:architect" refondrre red-lab red-shrik red-writer "red-shrik:agent" red)
if [[ $# -gt 0 ]]; then
  PROJETS=("$@")
fi

# La garde ne vaut que si elle échoue BRUYAMMENT. Première écriture : le
# `group by 1` portait sur l'expression entière, count(*) inclus — PostgreSQL
# refusait, le fichier « avant » contenait le message d'erreur, le « après »
# aussi, et le diff était vide. La garde était désactivée en silence, ce qui est
# pire que pas de garde du tout. Elle valide donc maintenant sa propre sortie.
snapshot() {
  local cible="$1" sortie rc brut
  # Redirection plutôt que substitution : `check_container_image_pins.py` refuse
  # toute exécution Docker dans un `$(…)`, et il a raison de le faire — la règle
  # ne connaît pas le verbe et ne peut donc pas prouver qu'il n'y a pas d'ingress
  # d'image. Le gate tournant AVANT pytest dans `test:unit`, une violation ici
  # n'échoue pas seule : elle empêche toute la suite unitaire de s'exécuter.
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

  # Garde de fuite : toute ligne qui diffère et qui ne concerne PAS le projet servi.
  fuite=$(diff "$OUT/avant-$projet.txt" "$OUT/apres-$projet.txt" \
          | grep -E '^[<>]' | grep -v "^[<>] ${projet}=" || true)

  # Ce que la nuit a écrit dans dream_runs, avec sa clé. Même contrainte de gate
  # que `snapshot` : redirection, jamais de Docker dans une substitution.
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

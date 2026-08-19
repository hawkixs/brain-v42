#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DREAM_DIR="$SCRIPT_DIR/dream"
LOG_DIR="$SCRIPT_DIR/../logs/dream"
# Pinned MCP config for the explicit Claude rollback path — see run_phase.
# Codex ignores ambient user configuration and declares only brain-v42 through
# scripts.dream.codex_runner.
MCP_CONFIG="$SCRIPT_DIR/../.mcp.json"

# Arg parsing — flag-style args come first, then the positional project_key.
# Env-var forms (DRY_RUN=true, BRAIN_DREAM_PROMOTE_ENABLED=false) also work.
DRY_RUN="${DRY_RUN:-false}"
# Agent provider for SCAN/CLEAN/CONNECT/SYNTH/PROMOTE/REORG. Codex is the
# subscription-backed default; Claude remains an explicit operator rollback.
# There is deliberately no automatic fallback after a phase starts because a
# WET MCP call may already have committed a mutation.
BRAIN_DREAM_AGENT_PROVIDER="${BRAIN_DREAM_AGENT_PROVIDER:-codex}"
# LA CHAÎNE. Liste ORDONNÉE de providers, séparée par des virgules — même
# contrainte de transport que le pool, et pour la même raison : `Environment=`
# de systemd découpe sur les blancs non protégés.
#
# Par défaut elle vaut le provider unique, donc une nuit qui ne la configure
# pas se comporte EXACTEMENT comme avant : un provider, aucune bascule. C'est
# volontaire — la chaîne doit être une option qu'on arme, pas un changement de
# comportement qu'on subit.
BRAIN_DREAM_AGENT_PROVIDERS="${BRAIN_DREAM_AGENT_PROVIDERS:-$BRAIN_DREAM_AGENT_PROVIDER}"
# Le code qu'un runner rend quand il a échoué ET peut PROUVER qu'aucun appel
# d'outil Brain n'a abouti. Il vit dans scripts/dream/_agent_capability.py ;
# tests/unit/test_dream_provider_chain.py tient les deux d'accord.
#
# Lui seul fait avancer la chaîne. Élargir à `!= 0` rendrait la nuit capable de
# rejouer une phase ayant déjà écrit — en doublant ses écritures, sans un mot.
PROVIDER_FALLBACK_EXIT_CODE=3
BRAIN_DREAM_CAPABILITY_ENFORCEMENT="${BRAIN_DREAM_CAPABILITY_ENFORCEMENT-false}"
BRAIN_DREAM_CODEX_FAST_MODEL="${BRAIN_DREAM_CODEX_FAST_MODEL:-gpt-5.6-terra}"
BRAIN_DREAM_CODEX_DEEP_MODEL="${BRAIN_DREAM_CODEX_DEEP_MODEL:-gpt-5.6-sol}"
BRAIN_DREAM_CODEX_FAST_REASONING="${BRAIN_DREAM_CODEX_FAST_REASONING:-medium}"
BRAIN_DREAM_CODEX_DEEP_REASONING="${BRAIN_DREAM_CODEX_DEEP_REASONING:-high}"
BRAIN_DREAM_CODEX_BIN="${BRAIN_DREAM_CODEX_BIN:-codex}"
BRAIN_DREAM_CLAUDE_BIN="${BRAIN_DREAM_CLAUDE_BIN:-claude}"
BRAIN_DREAM_AGY_BIN="${BRAIN_DREAM_AGY_BIN:-agy}"
# Modèles du maillon Google. GEMINI, délibérément : agy expose aussi
# claude-sonnet-4-6 et claude-opus-4-6-thinking, et les prendre annulerait
# l'intérêt du maillon — si Anthropic tombe, ces modèles tombent avec, et la
# chaîne aurait deux maillons corrélés déguisés en trois.
BRAIN_DREAM_AGY_FAST_MODEL="${BRAIN_DREAM_AGY_FAST_MODEL:-gemini-3.6-flash-medium}"
BRAIN_DREAM_AGY_DEEP_MODEL="${BRAIN_DREAM_AGY_DEEP_MODEL:-gemini-3.1-pro-high}"
# Ship the PROMOTE killswitch CLOSED (false) by default. Flip to true once
# §8 step 5 of the spec (first live rollout) has been cleared.
BRAIN_DREAM_PROMOTE_ENABLED="${BRAIN_DREAM_PROMOTE_ENABLED:-false}"
# REORG killswitch — independent of PROMOTE. Kept CLOSED until the two
# tool-shape blockers surfaced by Night 3 REORG (2026-04-23) are fixed:
# brain_list token ceiling + brain_get missing access_count/freshness_status.
# Both shipped in commit 0f521cd (2026-04-28). Re-arming proceeds via the
# REORG-only DRY_RUN sub-flag below before flipping WET.
BRAIN_DREAM_REORG_ENABLED="${BRAIN_DREAM_REORG_ENABLED:-false}"
# REORG-only dry-run override. When true AND BRAIN_DREAM_REORG_ENABLED=true,
# the REORG phase renders its prompt with Dry run: true while every other
# phase (notably PROMOTE) keeps the global DRY_RUN value. Lets REORG soak
# safely against live data without rolling back PROMOTE's WET status —
# applies the killswitch-as-cadence-decoupling pattern (learning 9a677c1a)
# at the dry-run dimension.
BRAIN_DREAM_REORG_DRY_RUN="${BRAIN_DREAM_REORG_DRY_RUN:-false}"
# EXTRACT killswitch — ticket knowledge extraction (proposer-only, spec
# 2026-07-04). Ship CLOSED; once enabled it starts in DRY (propose-only,
# review humaine via ticket_extraction_proposals) — même trajectoire de
# soak que REORG avant tout flip WET.
BRAIN_DREAM_EXTRACT_ENABLED="${BRAIN_DREAM_EXTRACT_ENABLED:-false}"
BRAIN_DREAM_EXTRACT_DRY_RUN="${BRAIN_DREAM_EXTRACT_DRY_RUN:-true}"
# ROADMAP killswitch — curation nocturne de la roadmap (spec 2026-07-04).
# Ship CLOSED ; défauts DRY. Régime agressif depuis le 2026-07-04 soir
# (décision Armand) : en WET le CLI applique les QUATRE ops (merge/rename
# inclus, WET_APPLYABLE_OPS = VALID_OPS) et le prompt consolide les features
# granulaires en gros sujets — Claude valide les applications au check matinal.
BRAIN_DREAM_ROADMAP_ENABLED="${BRAIN_DREAM_ROADMAP_ENABLED:-false}"
BRAIN_DREAM_ROADMAP_DRY_RUN="${BRAIN_DREAM_ROADMAP_DRY_RUN:-true}"
# SWEEP killswitch — tarissement des sessions fantômes (spec 2026-08-07).
# Livré FERMÉ et DRY. Phase déterministe, sans modèle ni réseau : le seuil
# vit dans brain_v42.models.brain_session.AUTO_STALE_AFTER, jamais ici.
BRAIN_DREAM_SWEEP_ENABLED="${BRAIN_DREAM_SWEEP_ENABLED:-false}"
BRAIN_DREAM_SWEEP_DRY_RUN="${BRAIN_DREAM_SWEEP_DRY_RUN:-true}"
# Allocation de retries pour la NUIT ENTIÈRE, tous projets confondus (§10).
# Deux, parce que la phase la plus chère (synth) vaut 15 min : 2 × 15 = 30 min
# de rallonge maximale, contre 43 min PAR PROJET si le retry restait par phase.
BRAIN_DREAM_RETRY_BUDGET="${BRAIN_DREAM_RETRY_BUDGET:-2}"
RETRY_BUDGET_LEFT="$BRAIN_DREAM_RETRY_BUDGET"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --) shift; break ;;
    -*)
      echo "Unknown flag: $1" >&2; exit 2 ;;
    *) break ;;
  esac
done

# No default, deliberately. A default here would satisfy the parsers'
# required --project-key with `brain-v42` and label another project's whole
# night, one layer above where that guard was placed. The failure mode is
# silent by construction: the rows are valid, nothing in the corpus says they
# lie, and there is no backfill. The systemd unit passes the key explicitly.
if [[ $# -eq 0 || -z "${1:-}" ]]; then
  echo "Usage: dream.sh [--dry-run] <project_key>[,<project_key>...]" >&2
  echo "Refusing to guess the project: a mislabelled night cannot be measured after the fact." >&2
  exit 2
fi

# --- Le pool de projets de la nuit ----------------------------------------
#
# Le positionnel reste REQUIS. La garde ci-dessus ne se dilue pas parce qu'un
# pool existe : refuser de deviner vaut toujours.
#
# BRAIN_DREAM_PROJECT_POOL, quand elle est posée, REMPLACE le positionnel. Le
# sens de priorité n'est pas un goût. `ExecStart=` vit dans le template
# versionné, que deploy/systemd/install.sh RÉGÉNÈRE ; le drop-in, lui, survit à
# la régénération — c'est tout l'objet de killswitches.conf et de l'incident du
# 2026-06-30 qu'il documente. Si le positionnel gagnait, élargir le pool dans le
# drop-in ne changerait RIEN : la nuit resterait à un projet, verte et muette.
# C'est le mode de panne que §3.1 de la spec refuse nommément.
#
# Séparateur VIRGULE, et c'est une contrainte de transport, pas d'esthétique :
# `Environment=` de systemd découpe sur les blancs NON protégés et traite chaque
# morceau comme une affectation distincte. `Environment=BRAIN_DREAM_PROJECT_POOL=a b`
# pose la variable à `a` et jette `b`, sans une erreur au démarrage. Un blanc
# INTERNE est donc la signature d'un transport cassé, et il fait sortir en 2
# plus bas plutôt que de fabriquer une clé que canonicalize_project_key
# rejettera au fond d'une fonction best-effort qui avale son exception.
declare -a PROJECT_POOL=()
POOL_SOURCE="positional argument"
_pool_raw="$1"
if [[ -n "${BRAIN_DREAM_PROJECT_POOL:-}" ]]; then
  _pool_raw="$BRAIN_DREAM_PROJECT_POOL"
  POOL_SOURCE="BRAIN_DREAM_PROJECT_POOL"
fi

IFS=',' read -r -a _pool_parts <<< "$_pool_raw"
for _entry in "${_pool_parts[@]}"; do
  # Trim des blancs de bord seulement : `a, b` est une écriture humaine
  # naturelle et sans ambiguïté.
  _entry="${_entry#"${_entry%%[![:space:]]*}"}"
  _entry="${_entry%"${_entry##*[![:space:]]}"}"
  if [[ -z "$_entry" ]]; then
    echo "Empty project key in $POOL_SOURCE: '$_pool_raw'" >&2
    exit 2
  fi
  if [[ "$_entry" == *[[:space:]]* ]]; then
    echo "Whitespace inside a project key from $POOL_SOURCE: '$_entry'" >&2
    echo "Pool entries are comma-separated: BRAIN_DREAM_PROJECT_POOL=brain-v42,red,red-lab" >&2
    exit 2
  fi
  if [[ "$_entry" == */* ]]; then
    # Une clé de projet entre dans un nom de fichier de journal (§3.2). Un
    # slash y creuserait un répertoire, ou ferait échouer l'ouverture.
    echo "Slash inside a project key from $POOL_SOURCE: '$_entry'" >&2
    exit 2
  fi
  case "$_entry" in
    brain|brain_v42) _entry="brain-v42" ;;
  esac
  for _seen in ${PROJECT_POOL[@]+"${PROJECT_POOL[@]}"}; do
    if [[ "$_seen" == "$_entry" ]]; then
      # Servir deux fois le même projet dans une nuit, c'est exactement le
      # gaspillage que §7 mesure pour les phases globales. Une répétition est
      # une faute de frappe, pas une intention.
      echo "Duplicate project key in $POOL_SOURCE: '$_entry'" >&2
      exit 2
    fi
  done
  PROJECT_POOL+=("$_entry")
done

# §10 : l'allocation de retries est une ressource de NUIT, donc le projet en
# tête de pool est mieux servi que celui en queue. Sans rotation, c'est toujours
# le même qui est sacrifié. Même idiome que roadmap_curate.rotate_keys, en
# service depuis le 2026-07-04. `10#` force la base 10 : `date +%j` rend 001-366
# et bash lirait 008 comme un octal invalide.
if (( ${#PROJECT_POOL[@]} > 1 )); then
  _rotation=$(( 10#$(date +%j) % ${#PROJECT_POOL[@]} ))
  PROJECT_POOL=("${PROJECT_POOL[@]:$_rotation}" "${PROJECT_POOL[@]:0:$_rotation}")
fi

# Le projet servi à un instant donné. Réaffecté à chaque itération de la boucle
# de projets : run_phase, les trois validateurs et _render_prompt le lisent tous
# comme un global.
PROJECT_KEY="${PROJECT_POOL[0]}"
TIMESTAMP=$(date +%Y-%m-%d)

case "$BRAIN_DREAM_AGENT_PROVIDER" in
  codex|claude|agy) ;;
  *)
    echo "Unsupported BRAIN_DREAM_AGENT_PROVIDER: $BRAIN_DREAM_AGENT_PROVIDER" >&2
    exit 2
    ;;
esac

# --- La chaîne de providers ------------------------------------------------
#
# Validée ENTIÈREMENT ici, avant la première phase. Un maillon inconnu au
# milieu de la chaîne ne doit pas se découvrir à 4 h du matin, sur la seule
# nuit où le premier maillon meurt : ce serait précisément le jour où le
# secours compte, et il ne serait pas là.
declare -a PROVIDER_CHAIN=()
IFS=',' read -r -a _provider_parts <<< "$BRAIN_DREAM_AGENT_PROVIDERS"
for _provider in "${_provider_parts[@]}"; do
  _provider="${_provider#"${_provider%%[![:space:]]*}"}"
  _provider="${_provider%"${_provider##*[![:space:]]}"}"
  if [[ -z "$_provider" ]]; then
    echo "Empty entry in BRAIN_DREAM_AGENT_PROVIDERS: $BRAIN_DREAM_AGENT_PROVIDERS" >&2
    exit 2
  fi
  case "$_provider" in
    codex|claude|agy) ;;
    *)
      echo "Unsupported provider in BRAIN_DREAM_AGENT_PROVIDERS: $_provider" >&2
      exit 2
      ;;
  esac
  for _seen in "${PROVIDER_CHAIN[@]}"; do
    if [[ "$_seen" == "$_provider" ]]; then
      echo "Duplicate provider in BRAIN_DREAM_AGENT_PROVIDERS: $_provider" >&2
      exit 2
    fi
  done
  PROVIDER_CHAIN+=("$_provider")
done
if (( ${#PROVIDER_CHAIN[@]} == 0 )); then
  echo "BRAIN_DREAM_AGENT_PROVIDERS is empty" >&2
  exit 2
fi
# Le premier maillon est le provider nominal : les préflights, les journaux et
# le choix de modèle par palier le lisent tous comme un global.
BRAIN_DREAM_AGENT_PROVIDER="${PROVIDER_CHAIN[0]}"

case "$BRAIN_DREAM_CAPABILITY_ENFORCEMENT" in
  true|false) ;;
  *)
    echo "Invalid BRAIN_DREAM_CAPABILITY_ENFORCEMENT value" >&2
    exit 2
    ;;
esac
# Le refus historique de `claude` sous enforcement a été LEVÉ le 2026-08-11,
# et seulement parce que sa cause a disparu. Ce rail passait par le .mcp.json
# du dépôt, dont l'Authorization interpole ${MCP_HTTP_TOKEN} — le jeton ADMIN —
# et par le joker `mcp__brain-v42__*`. Sous enforcement, c'était exactement la
# combinaison que le pare-feu existe pour interdire : six phases non scopées,
# des journaux verts, et rien pour le dire. Refuser était le seul fail-closed
# disponible tant que le rail ne savait pas porter un bearer par phase.
#
# scripts/dream/claude_runner.py le sait désormais : il rend une configuration
# MCP par phase (en-tête d'agent dédié, allowlist exacte, sans joker) et donne
# au processus fils le jeton de (projet, phase) sous le nom que cette
# configuration référence. Le garde n'a donc plus rien à protéger.
#
# Ne pas le retirer sans cette contrepartie : le supprimer seul rendrait au
# rail le jeton admin, en silence et avec la même nuit verte.

mkdir -p "$LOG_DIR"

# Phase definitions: name:model_tier:timeout_minutes:legacy_claude_max_turns
# max_turns raised after 2026-04-14 scan fail + 2026-04-10 synth fail.
# synth bumped 10→15 on 2026-05-03 after 3 timeouts in 16 nights post-04-24
# (brain_graph_* tool surface expansion + corpus mega-cluster +27% growth);
# pre-04-24 SYNTH ran 2-5 min, post-04-24 it ranges 4-10+ min, leaving zero
# headroom against the original 10m budget. Pinned by
# tests/unit/test_dream_sh_phase_timeouts.py.
PHASES=(
  "scan:fast:5:30"
  "clean:fast:5:25"
  "connect:fast:8:40"
  "synth:deep:15:50"
  "promote:deep:10:50"
  "reorg:deep:10:50"
)

# Phase dependencies: which previous phase logs to inject
declare -A PHASE_DEPS=(
  [scan]=""
  [clean]="scan"
  [connect]="clean"
  [synth]="connect"
  [promote]="synth"
  [reorg]="scan synth"
)

# Les trois phases GLOBALES, nommées pour le seul calcul de `planned_phases`.
# Les trois blocs restent écrits à la main HORS de la boucle (épinglé par
# tests/unit/test_dream_sh_global_phases_outside_loop.py) : ce tableau ne les
# pilote pas, il les compte.
DREAM_GLOBAL_PHASES=(extract roadmap sweep)

# --- Manifeste de la nuit ---------------------------------------------------
# Ce que la nuit DÉCLARE — ses attendus, ses skips et leur raison — écrit AU
# SITE DE CHAQUE DÉCISION et relu au matin par scripts.dream.post_run_alert.
#
# Ticket 0a9c067e : le comparateur de couverture existe et il a tiré trois nuits
# de suite, mais son attendu vient du drop-in systemd, qui n'a de clé que pour
# promote et reorg. La nuit du 2026-08-16 a donc annoncé 20 phases manquantes
# quand il en manquait 60. L'élargir depuis le drop-in serait un no-op ; la nuit
# est le seul témoin qui sache ce qu'elle a réellement tenté.
#
# INCRÉMENTAL, jamais vidé en fin de nuit : une nuit tuée par TimeoutStartSec,
# un OOM ou un `set -e` non gardé n'atteindrait aucun vidage final, et le rejeu
# du matin retomberait sur l'attendu du drop-in — c'est-à-dire sur le trou même
# que ce fichier ferme. L'absence du bloc de clôture devient donc le marqueur
# d'interruption, et interdit tout verdict vert.
#
# BEST-EFFORT INTÉGRAL : `set -euo pipefail` est actif depuis la ligne 2, et un
# `logs/` en lecture seule ne doit jamais tuer une nuit. Une télémétrie qui
# échoue ne fait pas tomber la phase qu'elle observe.
#
# Aucun échappement n'est nécessaire : une clé de projet portant un blanc ou un
# slash est déjà refusée plus haut, et les noms de phase comme les raisons de
# skip sont des littéraux d'un ensemble fermé.
MANIFEST_FILE="$LOG_DIR/${TIMESTAMP}_manifest.tsv"
manifest_put() {
  printf '%s\t%s\t%s\t%s\n' "$1" "${2-}" "${3-}" "${4-}" >> "$MANIFEST_FILE" 2>/dev/null || true
}
: > "$MANIFEST_FILE" 2>/dev/null || true
manifest_put meta run_date "$TIMESTAMP"
manifest_put meta pool_source "$POOL_SOURCE"
manifest_put meta pool "$(IFS=,; echo "${PROJECT_POOL[*]}")"
manifest_put meta planned_phases \
  "$(( ${#PHASES[@]} * ${#PROJECT_POOL[@]} + ${#DREAM_GLOBAL_PHASES[@]} ))"
manifest_put meta started "$(date -Iseconds)"

# OTEL env vars for Claude Code telemetry
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_LOGS_EXPORTER=console
export OTEL_METRICS_EXPORTER=console

# Force the headless `claude -p` subagents to BLOCK on MCP connection before
# the first turn (default is non-blocking). Without this, claude snapshots the
# turn's tool list ~450ms after init while the stdio brain-v42 server needs
# ~1.2s to finish importing + register tools — so brain_* is intermittently
# absent (regression 27430ae1: scan/reorg win the race, promote loses).
# Counter-intuitive flag semantics (decompiled from claude 2.1.186): setting
# NONBLOCKING=false flips connect to the BLOCKING branch, which awaits all MCP
# servers up to MCP_CONNECT_TIMEOUT_MS. --strict-mcp-config alone only removed
# the fast HTTP gitlab competitor; it did NOT make brain-v42 win the race.
export MCP_CONNECTION_NONBLOCKING=false
export MCP_CONNECT_TIMEOUT_MS=10000

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_DIR/$TIMESTAMP.log"; }

#
# Exit codes from run_phase:
#   0 → phase succeeded (DONE)
#   1 → phase hard-failed (non-zero exit, not a timeout)
#   2 → phase timed out (exit 124 from `timeout`)
run_phase() {
  local name="$1" model_tier="$2" timeout="$3" max_turns="$4"
  local prompt_file="$DREAM_DIR/phase_${name}.md"
  # §3.2 : ces chemins portent le projet. codex_runner ouvre `events` et
  # `stderr` en "w" et fait `report_log.write_text("")` — de la TRONCATURE, pas
  # de l'append. Sans composante de projet, seuls les journaux du DERNIER projet
  # survivraient au matin, et l'injection de dépendance de la §3.3 relirait le
  # rapport du projet précédent.
  local raw_log="$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_${name}.raw.log"
  local report_log="$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_${name}.log"
  local otel_log="$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_${name}.otel.log"
  local events_log="$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_${name}.events.jsonl"
  local stderr_log="$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_${name}.stderr.log"

  local model reasoning_effort=""
  case "$BRAIN_DREAM_AGENT_PROVIDER:$model_tier" in
    codex:fast)
      model="$BRAIN_DREAM_CODEX_FAST_MODEL"
      reasoning_effort="$BRAIN_DREAM_CODEX_FAST_REASONING"
      ;;
    codex:deep)
      model="$BRAIN_DREAM_CODEX_DEEP_MODEL"
      reasoning_effort="$BRAIN_DREAM_CODEX_DEEP_REASONING"
      ;;
    claude:fast) model="sonnet" ;;
    claude:deep) model="opus" ;;
    agy:fast) model="$BRAIN_DREAM_AGY_FAST_MODEL" ;;
    agy:deep) model="$BRAIN_DREAM_AGY_DEEP_MODEL" ;;
    *)
      log "FAIL  $name — unsupported provider/model tier: $BRAIN_DREAM_AGENT_PROVIDER/$model_tier"
      return 1
      ;;
  esac

  if [[ ! -f "$prompt_file" ]]; then
    log "SKIP $name — prompt file missing: $prompt_file"
    return 0
  fi

  local prompt
  # Per-phase DRY_RUN override: REORG soaks dry while PROMOTE runs WET.
  # The override only fires when REORG is enabled — disabled phases never
  # reach the renderer (they `continue` above).
  local effective_dry_run="$DRY_RUN"
  if [[ "$name" == "reorg" && "$BRAIN_DREAM_REORG_DRY_RUN" == "true" ]]; then
    effective_dry_run="true"
  fi
  # Delegate to scripts/dream/_render_prompt.py — sed used to break here
  # when the candidate pool JSON contained `|`, `&`, or `\` (all have
  # special meaning in sed's s/// replacement). On 2026-04-19 a `|` in a
  # topic string produced an empty prompt and the validator flagged a
  # missing PROMOTE REPORT.
  prompt=$(python3 -m scripts.dream._render_prompt \
    "$prompt_file" "$PROJECT_KEY" "$TIMESTAMP" "$effective_dry_run" \
    "${PROMOTE_CANDIDATE_POOL_JSON:-[]}" \
    "${PROMOTE_RECENT_PROMOTIONS_JSON:-[]}")

  # Inject dependency phase outputs into the prompt.
  # PREPENDED (not appended) so the phase prompt's own output-format
  # instructions are the LAST thing the model sees. When deps were
  # appended, the prose style of the SYNTH log (markdown candidate
  # reports) primed PROMOTE to emit prose + empty markers — the
  # "last example sticks" failure mode caught on 2026-04-20 runs.
  local deps="${PHASE_DEPS[$name]:-}"
  if [[ -n "$deps" ]]; then
    local dep_section=""
    for dep in $deps; do
      local dep_log="$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_${dep}.log"
      if [[ -f "$dep_log" && -s "$dep_log" ]]; then
        dep_section+="
### ${dep^^} phase output
$(cat "$dep_log")
"
      fi
    done
    if [[ -n "$dep_section" ]]; then
      prompt="## Previous Phase Reports (reference context — do not mimic style)
The orchestrator has injected the output from dependency phases below.
$dep_section

---

$prompt"
    fi
  fi

  if [[ "$BRAIN_DREAM_AGENT_PROVIDER" == "agy" ]]; then
    log "START $name (provider=agy, model=$model, timeout=${timeout}m)"
  elif [[ "$BRAIN_DREAM_AGENT_PROVIDER" == "codex" ]]; then
    log "START $name (provider=codex, model=$model, reasoning=$reasoning_effort, timeout=${timeout}m)"
  else
    log "START $name (provider=claude, model=$model, timeout=${timeout}m, max_turns=$max_turns)"
  fi

  local phase_start=$SECONDS
  local status="done"
  local phase_rc=0

  # The prompt always travels through stdin to avoid ARG_MAX when dependency
  # reports are large. Codex writes final report, JSONL events, and stderr to
  # separate files. The Claude rollback keeps its historical mixed OTEL path.
  local code=0
  if [[ "$BRAIN_DREAM_AGENT_PROVIDER" == "agy" ]]; then
    # agy ne prend aucune configuration en ligne de commande : son runner lui
    # compose un HOME éphémère portant le bearer scopé ET la garde d'outils.
    # Voir scripts/dream/agy_runner.py — le seul rail dont le bearer touche un
    # fichier, confiné à un tmpfs et détruit avec le HOME.
    if printf '%s' "$prompt" | uv run python -m scripts.dream.agy_runner \
      --phase "$name" \
      --project-key "$PROJECT_KEY" \
      --model "$model" \
      --timeout-seconds "$(( timeout * 60 ))" \
      --events-log "$events_log" \
      --report-log "$report_log" \
      --stderr-log "$stderr_log" \
      --agy-executable "$BRAIN_DREAM_AGY_BIN"; then
      code=0
    else
      code=$?
    fi
  elif [[ "$BRAIN_DREAM_AGENT_PROVIDER" == "codex" ]]; then
    if printf '%s' "$prompt" | uv run python -m scripts.dream.codex_runner \
      --phase "$name" \
      --project-key "$PROJECT_KEY" \
      --model "$model" \
      --reasoning-effort "$reasoning_effort" \
      --timeout-seconds "$(( timeout * 60 ))" \
      --report-log "$report_log" \
      --events-log "$events_log" \
      --stderr-log "$stderr_log" \
      --codex-executable "$BRAIN_DREAM_CODEX_BIN"; then
      code=0
    else
      code=$?
    fi
  else
    # Le rail claude passe par son runner isolé depuis le 2026-08-11. Il
    # remplace le joker `mcp__brain-v42__*` par l'allowlist exacte de la phase
    # et substitue le bearer de (projet, phase) au jeton admin. Le flux mixte
    # continue d'atterrir dans $raw_log : otel_split le lit juste après.
    if printf '%s' "$prompt" | uv run python -m scripts.dream.claude_runner \
      --phase "$name" \
      --project-key "$PROJECT_KEY" \
      --model "$model" \
      --max-turns "$max_turns" \
      --timeout-seconds "$(( timeout * 60 ))" \
      --raw-log "$raw_log" \
      --claude-executable "$BRAIN_DREAM_CLAUDE_BIN"; then
      code=0
    else
      code=$?
    fi
  fi

  if [[ $code -eq 0 ]]; then
    log "DONE  $name"
  else
    if [[ $code -eq 124 ]]; then
      status="timeout"
      phase_rc=2
      log "TIMEOUT $name (>${timeout}m)"
    elif [[ $code -eq $PROVIDER_FALLBACK_EXIT_CODE ]]; then
      # Échec PROUVÉ sans écriture. C'est un échec pour les métriques comme
      # pour l'unité — `status` reste `fail` — mais le code doit REMONTER tel
      # quel jusqu'à run_phase_chain, seul endroit qui sait s'il existe un
      # maillon suivant. L'écraser en 1 ici, comme le faisait la version
      # d'origine, rendait la chaîne inerte : elle ne voyait jamais sa
      # condition de bascule.
      status="fail"
      phase_rc=$PROVIDER_FALLBACK_EXIT_CODE
      log "FAIL  $name (exit=$code — aucun appel d'outil Brain abouti)"
    else
      status="fail"
      phase_rc=1
      log "FAIL  $name (exit=$code)"
    fi
  fi

  local duration=$(( SECONDS - phase_start ))

  local err_log=""
  if [[ "$BRAIN_DREAM_AGENT_PROVIDER" == "claude" ]]; then
    # Preserve the failed mixed stream before otel_split removes it.
    if [[ "$status" != "done" && -f "$raw_log" ]]; then
      err_log="$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_${name}.err.log"
      cp "$raw_log" "$err_log"
    fi

    if uv run python -m brain_v42.metrics.otel_split \
        "$raw_log" --report "$report_log" --otel "$otel_log" \
        >> "$LOG_DIR/$TIMESTAMP.log" 2>&1; then
      rm -f "$raw_log"
    else
      log "WARN  otel_split failed for $name — leaving raw log in place"
      [[ -f "$raw_log" ]] && cp "$raw_log" "$report_log"
      : > "$otel_log"
    fi
  else
    # The runner already separated the streams. Always leave a readable report
    # path for dependency injection and validators, including failed phases.
    [[ -f "$report_log" ]] || : > "$report_log"
    err_log="$stderr_log"
  fi

  # --project-key lands in the SHARED array, before the codex/claude fork, so
  # both rails receive it from one edit. Teaching it to a single parser would
  # hand the other an unknown argument: argparse exits 2, pipefail propagates,
  # and the WARN below swallows it — the night silently loses its six
  # per-project rows while the test suite stays green.
  local parser_args=(--phase "$name" --model "$model" --date "$TIMESTAMP"
                     --status "$status" --duration "$duration"
                     --project-key "$PROJECT_KEY")
  parser_args+=(--phase-dry-run "$effective_dry_run")
  local scan_log="$err_log"
  if [[ -z "$scan_log" && -f "$report_log" ]]; then
    scan_log="$report_log"
  fi
  if [[ -n "$scan_log" ]]; then
    parser_args+=(--raw-log "$scan_log")
  fi
  if [[ "$BRAIN_DREAM_AGENT_PROVIDER" == "agy" ]]; then
    if uv run python -m brain_v42.metrics.agy_dream_parser \
      "${parser_args[@]}" --report-log "$report_log" "$events_log" \
      2>&1 | tee -a "$LOG_DIR/$TIMESTAMP.log"; then
      :
    else
      log "WARN  agy_dream_parser failed for $name (non-fatal)"
    fi
  elif [[ "$BRAIN_DREAM_AGENT_PROVIDER" == "codex" ]]; then
    if uv run python -m brain_v42.metrics.codex_dream_parser \
      "${parser_args[@]}" --report-log "$report_log" "$events_log" \
      2>&1 | tee -a "$LOG_DIR/$TIMESTAMP.log"; then
      :
    else
      log "WARN  codex_dream_parser failed for $name (non-fatal)"
    fi
  else
    if uv run python -m brain_v42.metrics.dream_parser \
      "${parser_args[@]}" "$otel_log" 2>&1 | tee -a "$LOG_DIR/$TIMESTAMP.log"; then
      :
    else
      log "WARN  dream_parser failed for $name (non-fatal)"
    fi
  fi

  return "$phase_rc"
}

#
# Joue une phase sur la CHAÎNE de providers.
#
# Avance au maillon suivant sur le seul code PROVIDER_FALLBACK_EXIT_CODE, qui
# signifie « échec, et je peux prouver qu'aucun appel d'outil Brain n'a abouti ».
# Un échec ordinaire (1) et un timeout (2) s'arrêtent là où ils sont tombés :
# aucun des deux ne prouve que rien n'a été écrit, et rejouer une phase qui a
# muté la ferait écrire deux fois.
#
# Codes de retour identiques à run_phase (0 / 1 / 2), pour que tout l'appelant
# — retry de nuit, validateurs, compteurs — reste inchangé. La chaîne est une
# couche AU-DESSUS du contrat existant, pas une modification de ce contrat.
run_phase_chain() {
  local name="$1" model_tier="$2" timeout="$3" max_turns="$4"
  local nominal_provider="$BRAIN_DREAM_AGENT_PROVIDER"
  local rc=0
  local index=0

  for provider in "${PROVIDER_CHAIN[@]}"; do
    index=$(( index + 1 ))
    BRAIN_DREAM_AGENT_PROVIDER="$provider"
    # Surtout PAS de `set +e` / `set -e` ici : un `set -e` posé dans une
    # fonction survit à son `return`, et l'errexit ainsi restauré ferait sortir
    # le script sur le `return $rc` final — la nuit s'arrêterait à la première
    # phase en échec, avant même son résumé. `|| rc=$?` capture le code sans
    # toucher au mode du shell.
    rc=0
    run_phase "$name" "$model_tier" "$timeout" "$max_turns" || rc=$?

    if (( rc != PROVIDER_FALLBACK_EXIT_CODE )); then
      break
    fi

    if (( index < ${#PROVIDER_CHAIN[@]} )); then
      # Nommer le maillon abandonné ET la preuve qui l'autorise. Une bascule
      # muette rendrait indiscernables « codex a marché » et « codex est mort,
      # claude a sauvé la nuit » — or c'est exactement ce qu'il faut voir au
      # matin pour savoir si un abonnement est encore vivant.
      log "FALLBACK $PROJECT_KEY/$name — $provider a échoué sans aucun appel d'outil Brain abouti, bascule vers ${PROVIDER_CHAIN[$index]}"
      # Agrégé pour le résumé de fin. Inscrit à la BASCULE, pas au succès du
      # maillon suivant : ce qu'on veut mesurer est la mort du primaire, qu'un
      # secours la rattrape ou non. Si plus personne ne rattrape, la phase
      # rejoint FAILED_PHASES ci-dessous et sera comptée aux deux titres.
      FALLBACK_PHASES+=("$PROJECT_KEY/$name")
    else
      # Dernier maillon : plus personne derrière. La phase redevient un échec
      # ordinaire, pour que l'unité rougisse comme avant.
      log "FALLBACK-END $PROJECT_KEY/$name — $provider était le dernier maillon de la chaîne"
      rc=1
    fi
  done

  BRAIN_DREAM_AGENT_PROVIDER="$nominal_provider"
  return "$rc"
}

# --- Main ---

# Advisory lock: prevent concurrent dream cycles (cron overlap, manual
# re-trigger during an active run). File descriptor 9 is held for the
# full lifetime of this process; flock -n returns immediately if another
# process already owns it.
LOCK_FILE="${XDG_RUNTIME_DIR:-/tmp}/brain-v42-dream.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "dream cycle already running (lock=$LOCK_FILE), skipping"
  exit 0
fi

log "=== Dream started (project=$PROJECT_KEY, provider=$BRAIN_DREAM_AGENT_PROVIDER, dry_run=$DRY_RUN, promote_enabled=$BRAIN_DREAM_PROMOTE_ENABLED, reorg_enabled=$BRAIN_DREAM_REORG_ENABLED, reorg_dry_run=$BRAIN_DREAM_REORG_DRY_RUN) ==="
# Le pool sur sa propre ligne, et la SOURCE avec lui. Sans la source, une nuit
# à un projet ne dit pas si le drop-in a été lu ou si systemd a mangé la
# variable — les deux rendent la même ligne.
log "=== Pool (${#PROJECT_POOL[@]}) from $POOL_SOURCE: ${PROJECT_POOL[*]} ==="

# Préflights, joués sur TOUTE la chaîne — pas seulement sur son premier maillon.
#
# C'est le point qui décide si une chaîne sert à quelque chose. Le préflight
# détecte exactement ce qui tue un rail : binaire absent, jeton manquant,
# abonnement expiré. Le faire échouer la NUIT plutôt que le MAILLON reviendrait
# à tuer la nuit précisément le jour où le secours devait servir — la panne que
# la chaîne existe pour absorber.
#
# Un maillon qui ne passe pas son préflight est donc RETIRÉ de la chaîne, et
# nommé. La nuit ne s'arrête que si TOUS échouent : le fail-closed est conservé,
# il porte simplement sur l'ensemble au lieu du premier.
#
# Rien de tout cela n'est un fallback silencieux : chaque retrait est journalisé
# avec sa raison, et la ligne PROVIDERS finale dit avec quoi la nuit part.
preflight_provider() {
  local provider="$1"
  local binary runner label
  case "$provider" in
    codex)  binary="$BRAIN_DREAM_CODEX_BIN";  runner="scripts.dream.codex_runner";  label="Codex" ;;
    claude) binary="$BRAIN_DREAM_CLAUDE_BIN"; runner="scripts.dream.claude_runner"; label="Claude" ;;
    agy)    binary="$BRAIN_DREAM_AGY_BIN";    runner="scripts.dream.agy_runner";    label="Agy" ;;
    *)
      log "FAIL $provider preflight — unsupported provider"
      return 1
      ;;
  esac

  if ! command -v "$binary" >/dev/null 2>&1; then
    log "FAIL $label preflight — executable not found: $binary"
    return 1
  fi
  if [[ -z "${MCP_HTTP_TOKEN:-}" ]]; then
    log "FAIL $label preflight — MCP_HTTP_TOKEN is not set"
    return 1
  fi
  # Le préflight de capacités est par PROJET : la politique d'outils dépend de
  # la clé. Le vérifier sur le seul premier projet laisserait une nuit démarrer
  # puis échouer au troisième, après deux projets déjà mutés.
  if [[ "$BRAIN_DREAM_CAPABILITY_ENFORCEMENT" == "true" ]]; then
    for _preflight_project in "${PROJECT_POOL[@]}"; do
      if ! uv run python -m "$runner" \
        --preflight-capabilities --project-key "$_preflight_project"; then
        log "FAIL $label preflight — Dream capability configuration is invalid for $_preflight_project"
        return 1
      fi
    done
  fi

  # Le rail agy EXIGE l'enforcement. Sa garde d'outils est sondée par son
  # runner dans --preflight-capabilities ci-dessus ; sans enforcement ce chemin
  # n'est jamais emprunté, et la phase partirait avec un shell libre et un
  # bearer admin. Le maillon est donc retiré plutôt que joué sans filet.
  if [[ "$provider" == "agy" && "$BRAIN_DREAM_CAPABILITY_ENFORCEMENT" != "true" ]]; then
    log "FAIL $label preflight — le rail agy exige BRAIN_DREAM_CAPABILITY_ENFORCEMENT=true"
    return 1
  fi

  # Codex seul expose une sonde d'authentification non interactive. Elle pinne
  # aussi forced_login_method=chatgpt, pour qu'une clé d'API ambiante ne puisse
  # pas déplacer ces phases vers la facturation à l'usage sans le dire.
  # `claude` n'a pas d'équivalent, et en inventer un qui échoue ouvert ne
  # prouverait rien — l'absence est donc assumée, pas comblée.
  if [[ "$provider" == "codex" ]]; then
    # Pas de `set +e` / `set -e` ici : un `set -e` posé DANS une fonction
    # survit à son `return`, et l'errexit ainsi restauré faisait sortir le
    # script sur le `return 1` au lieu de laisser la chaîne retomber sur le
    # maillon suivant. Le `|| true` capture le rc sans toucher au mode du shell.
    local codex_login_status codex_login_rc=0
    codex_login_status="$("$binary" login status 2>&1)" || codex_login_rc=$?
    if (( codex_login_rc != 0 )) || [[ "$codex_login_status" != *"Logged in using ChatGPT"* ]]; then
      log "FAIL $label preflight — ChatGPT login is not active"
      return 1
    fi
  fi

  log "PREFLIGHT $label — ready"
  return 0
}

declare -a READY_PROVIDERS=()
for _provider in "${PROVIDER_CHAIN[@]}"; do
  set +e
  preflight_provider "$_provider"
  _preflight_rc=$?
  set -e
  if (( _preflight_rc == 0 )); then
    READY_PROVIDERS+=("$_provider")
  else
    log "DROP $_provider — préflight échoué, retiré de la chaîne de cette nuit"
  fi
done

if (( ${#READY_PROVIDERS[@]} == 0 )); then
  log "FAIL — aucun provider de la chaîne ne passe son préflight (${PROVIDER_CHAIN[*]})"
  exit 1
fi

PROVIDER_CHAIN=("${READY_PROVIDERS[@]}")
BRAIN_DREAM_AGENT_PROVIDER="${PROVIDER_CHAIN[0]}"
log "=== Providers (${#PROVIDER_CHAIN[@]}) prêts, dans l'ordre : ${PROVIDER_CHAIN[*]} ==="

# Defense-in-depth: scrub any new XML tool-call leaks that landed in
# learnings/decisions since the last run. Pure deterministic cleanup,
# non-LLM, quiet single-line summary. Root cause of the leak is upstream
# in Claude Code MCP client tool-call serialization (2026-04-22 decision
# 509783da) — safe to re-run daily because it's idempotent and protected
# against meta-reference false positives by regex-level tests.
# Failures here MUST NOT abort the dream run; we capture and continue.
set +e
uv run python -m scripts.scrub_xml_tool_call_leak --live --quiet \
  2>&1 | tee -a "$LOG_DIR/$TIMESTAMP.log"
scrub_rc=${PIPESTATUS[0]}
set -e
if (( scrub_rc != 0 )); then
  log "WARN  pre-phase XML scrub failed (rc=$scrub_rc); continuing dream run"
fi

declare -a FAILED_PHASES=()
declare -a TIMED_OUT_PHASES=()
# Sous-ensemble de TIMED_OUT_PHASES : les échéances BORNÉES qu'une phase
# s'impose à elle-même APRÈS avoir enregistré son dream_run terminal. Ce n'est
# pas une panne, c'est une nuit normale menée jusqu'à sa limite de temps. Elles
# alertent (compteur FAIL_TOTAL) mais ne rougissent plus l'unité systemd.
# Fail-closed par construction : un timeout qu'on oublie de classer ici reste
# compté comme un échec, donc bruyant. Un timeout de garde-fou EXTERNE
# (`timeout` a tué le process) n'entre JAMAIS ici : l'état de la phase est
# inconnu.
declare -a CONTROLLED_TIMEOUT_PHASES=()
declare -a SKIPPED_PHASES=()
# Phases qu'un maillon de secours a rattrapées. Volontairement HORS de
# FAIL_TOTAL et de la garde de sortie : elles ont réussi. Elles n'existent que
# pour que le résumé distingue « codex a marché » de « codex est mort, agy a
# sauvé la nuit » — la distinction que `run_phase_chain` journalise déjà par
# phase mais que rien n'agrégeait, six nuits durant.
declare -a FALLBACK_PHASES=()
TOTAL_PHASES=0

# --- Pre-flight gate: skip the costly deep phases (synth/promote/reorg) when
# the brain corpus is provably unchanged since the previous run. ~40% of nights
# re-process a static corpus, decide nothing (0 tool_calls) and burn ~$1
# (2026-06-22 audit). The verdict is computed once; the decision logic lives in
# Python (scripts.dream.dream_preflight) and is fail-safe — any error or
# uncertainty prints RUN, so a wrong skip is impossible.
OPUS_SKIP=false
preflight_verdict="$(
  uv run python -m scripts.dream.dream_preflight --date "$TIMESTAMP" \
    2>> "$LOG_DIR/$TIMESTAMP.log" || echo "RUN"
)"
if [[ "$preflight_verdict" == SKIP* ]]; then
  OPUS_SKIP=true
  log "PREFLIGHT $preflight_verdict"
  log "PREFLIGHT → skipping synth/promote/reorg (deep phases) this run"
else
  log "PREFLIGHT RUN — corpus changed since last run (or check inconclusive)"
fi

# Sert UN projet : ses six phases agent, dans l'ordre, avec leurs killswitches
# et leurs validateurs. Extraire ce bloc en fonction n'est pas cosmétique.
#
# §9 relève cinq `continue` qui appartiennent à la boucle de PHASES. Une boucle
# de projets posée AUTOUR d'eux ne les aurait pas cassés — bash les rattache
# toujours à la boucle la plus interne — mais elle aurait rendu la lecture
# ambiguë au point que le prochain remaniement le casse. Une frontière de
# fonction supprime la question : ce corps n'a qu'une boucle.
#
# La fonction écrit dans les tableaux de compteurs, qui restent globaux : ils
# agrègent la nuit entière, tous projets confondus.
run_project_phases() {
  # Affectation GLOBALE, volontairement pas `local` : run_phase, les trois
  # validateurs et _render_prompt lisent tous $PROJECT_KEY comme un global.
  PROJECT_KEY="$1"

  # §3.4 — ces deux exports survivent aux itérations. Si un projet saute
  # PROMOTE (killswitch, pool vide, ou échec de promote_prepare), la valeur du
  # projet PRÉCÉDENT reste chargée et le projet courant promeut sur le pool
  # d'un autre. La remise à zéro est en TÊTE d'itération, pas en queue : les
  # cinq `continue` du corps sauteraient un nettoyage placé à la fin.
  export PROMOTE_CANDIDATE_POOL_JSON='[]'
  export PROMOTE_RECENT_PROMOTIONS_JSON='[]'
  CANDIDATES_JSON=""
  DREAM_RUN_ID=""
  REORG_RUN_ID=""

  log "--- Projet $PROJECT_KEY ---"

  for phase_spec in "${PHASES[@]}"; do
    IFS=':' read -r name model_tier timeout max_turns <<< "$phase_spec"
    TOTAL_PHASES=$(( TOTAL_PHASES + 1 ))
    # Même site, même instant que le compteur : une SEPTIÈME phase ajoutée à
    # PHASES étend l'attendu toute seule, sans garde à maintenir ailleurs.
    manifest_put expected "$name" "$PROJECT_KEY"

    # --- Pre-flight: skip the costly Opus phases on a provably-static corpus ---
    if [[ "$OPUS_SKIP" == true ]] \
       && { [[ "$name" == "synth" ]] || [[ "$name" == "promote" ]] || [[ "$name" == "reorg" ]]; }; then
      log "SKIP $name (pre-flight: corpus unchanged since last run)"
      SKIPPED_PHASES+=("$PROJECT_KEY/$name")
      manifest_put skipped "$name" "$PROJECT_KEY" preflight
      continue
    fi

    # --- PROMOTE: killswitch + candidate-pool pre-compute ------------------
    if [[ "$name" == "promote" ]]; then
      if [[ "$BRAIN_DREAM_PROMOTE_ENABLED" != "true" ]]; then
        log "SKIP promote (killswitch BRAIN_DREAM_PROMOTE_ENABLED=$BRAIN_DREAM_PROMOTE_ENABLED)"
        SKIPPED_PHASES+=("$PROJECT_KEY/promote")
        manifest_put skipped promote "$PROJECT_KEY" killswitch
        continue
      fi

      CANDIDATES_JSON="$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_promote_candidates.json"
      promote_prep_start=$SECONDS
      set +e
      uv run python -m scripts.dream.promote_prepare \
        --project-key "$PROJECT_KEY" --limit 10 \
        > "$CANDIDATES_JSON" 2>> "$LOG_DIR/$TIMESTAMP.log"
      prep_rc=$?
      set -e
      if (( prep_rc != 0 )); then
        log "FAIL promote — candidate pool fetch failed (rc=$prep_rc)"
        FAILED_PHASES+=("$PROJECT_KEY/promote")
        manifest_put failed promote "$PROJECT_KEY"
        continue
      fi

      pool_size=$(jq 'length' "$CANDIDATES_JSON" 2>/dev/null || echo 0)
      if [[ "$pool_size" -eq 0 ]]; then
        log "SKIP promote — empty candidate pool"
        # Synthesize a no_candidates report so downstream tooling has
        # something to read (otel_split + dream_parser rely on the file).
        {
          echo '=== PROMOTE REPORT ==='
          echo '{"dry_run":false,"target_type":"none","reason":"no_candidates"}'
          echo '=== END ==='
        } > "$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_promote.log"
        # Record a REAL dream_runs row for the phase. Promote stays an EXPECTED
        # phase (killswitch open), so with no row at all post_run_alert
        # manufactures a synthetic `partial` every single night — a false alarm
        # that pushes the operator to "repair" the migration-041 maturity filter
        # that legitimately emptied the pool. Writing the row makes the phase
        # OBSERVED; a promote that really crashes still writes nothing and still
        # rings. Non-fatal: a missing row only brings the alarm back.
        set +e
        uv run python -m scripts.dream._promote_helpers record-empty-pool \
          --date "$TIMESTAMP" --duration-seconds "$(( SECONDS - promote_prep_start ))" \
          --project-key "$PROJECT_KEY" \
          >> "$LOG_DIR/$TIMESTAMP.log" 2>&1
        record_rc=$?
        set -e
        # La déclaration vit DANS chacune des deux branches, jamais après le
        # `if` : le push `SKIPPED_PHASES+=` ci-dessous est commun aux deux, donc
        # « sautée » et « sa ligne est écrite » sont deux faits INDÉPENDANTS.
        # Une raison unique déclarerait « aucune ligne due » alors que dream.sh
        # vient d'imprimer que l'écriture a ÉCHOUÉ, et le trou serait muet.
        if (( record_rc == 0 )); then
          log "promote — empty-pool dream_runs row recorded (phase observed, not failed)"
          manifest_put skipped promote "$PROJECT_KEY" empty-pool-recorded
        else
          log "WARN  promote — empty-pool dream_runs row NOT recorded (rc=$record_rc)"
          manifest_put skipped promote "$PROJECT_KEY" empty-pool-unrecorded
        fi
        SKIPPED_PHASES+=("$PROJECT_KEY/promote")
        continue
      fi

      export PROMOTE_CANDIDATE_POOL_JSON
      PROMOTE_CANDIDATE_POOL_JSON="$(cat "$CANDIDATES_JSON")"
      export PROMOTE_RECENT_PROMOTIONS_JSON
      PROMOTE_RECENT_PROMOTIONS_JSON="$(
        uv run python -m scripts.dream._promote_helpers recent-promotions --limit 10 \
          2>> "$LOG_DIR/$TIMESTAMP.log" || echo '[]'
      )"
    fi

    # --- REORG: killswitch --------------------------------------------------
    # Independent of PROMOTE. Night 3 DRY_RUN (2026-04-23) surfaced two
    # tool-shape blockers (brain_list token ceiling, brain_get missing
    # access_count / freshness_status) that make WET-mode guardrails
    # structurally unverifiable. Keep REORG gated until both ship.
    if [[ "$name" == "reorg" ]]; then
      if [[ "$BRAIN_DREAM_REORG_ENABLED" != "true" ]]; then
        log "SKIP reorg (killswitch BRAIN_DREAM_REORG_ENABLED=$BRAIN_DREAM_REORG_ENABLED)"
        SKIPPED_PHASES+=("$PROJECT_KEY/reorg")
        manifest_put skipped reorg "$PROJECT_KEY" killswitch
        continue
      fi
    fi

    # `set -e` is active, so we must guard the call (run_phase's non-zero
    # return is expected on phase failure and MUST NOT abort the script — we
    # want every phase to run for diagnostic completeness).
    set +e
    run_phase_chain "$name" "$model_tier" "$timeout" "$max_turns"
    phase_rc=$?
    # Retry once on hard-fail (exit 1) — NOT on timeout (2), because timeouts
    # already burned the full budget and retrying would double the wall-clock
    # cost. Previously a single transient fail lost the whole phase (e.g.
    # 2026-04-14 scan hit max_turns=20). PROMOTE is NOT retried because a
    # partial materialization (atomic repo write committed, something
    # downstream hiccuped) would surface as a duplicate-source IntegrityError
    # on retry — ambiguous signal, safer to leave the validator handle it.
    #
    # §10 — le retry est le SEUL budget qui soit vraiment une ressource de
    # nuit, et c'est lui qui multiplie par le nombre de projets : +43 min
    # éligibles PAR PROJET, soit +344 min de plafond à huit. Une allocation
    # de nuit ramène le pire cas configuré de 803 min à ~489.
    if (( phase_rc == 1 )) && [[ "$name" != "promote" ]]; then
      if (( RETRY_BUDGET_LEFT > 0 )); then
        RETRY_BUDGET_LEFT=$(( RETRY_BUDGET_LEFT - 1 ))
        log "RETRY $PROJECT_KEY/$name (first attempt failed, re-running once; night budget left=$RETRY_BUDGET_LEFT)"
        run_phase_chain "$name" "$model_tier" "$timeout" "$max_turns"
        phase_rc=$?
      else
        # Pas un échec silencieux : la phase garde son rc=1 et rougit l'unité
        # comme avant. Seule la SECONDE chance disparaît, et le journal le dit.
        log "NO-RETRY $PROJECT_KEY/$name — night retry budget exhausted (BRAIN_DREAM_RETRY_BUDGET=$BRAIN_DREAM_RETRY_BUDGET)"
      fi
    fi
    set -e

    # --- CONNECT: post-phase validator ------------------------------------
    # A zero agent exit is insufficient: the exact report must also contain
    # zero tool-level errors. Validation runs after the final retry so only the
    # retained report determines the phase outcome.
    if [[ "$name" == "connect" && "$phase_rc" == "0" ]]; then
      set +e
      uv run python -m scripts.dream.connect_validate \
        --report-log "$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_${name}.log" \
        --run-date "$TIMESTAMP" \
        --project-key "$PROJECT_KEY" \
        >> "$LOG_DIR/$TIMESTAMP.log" 2>&1
      validator_rc=$?
      set -e
      if (( validator_rc != 0 )); then
        log "FAIL connect — validator rejected CONNECT report; see validation detail"
        phase_rc=1
      fi
    fi

    # --- PROMOTE: post-phase validator ------------------------------------
    if [[ "$name" == "promote" && "$phase_rc" == "0" ]]; then
      DREAM_RUN_ID=$(
        uv run python -m scripts.dream._promote_helpers dream-run-id --date "$TIMESTAMP" \
          --project-key "$PROJECT_KEY" \
          2>> "$LOG_DIR/$TIMESTAMP.log" | tr -d '\n'
      )
      set +e
      uv run python -m scripts.dream.promote_validate \
        --report-log "$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_${name}.log" \
        --candidates-json "$CANDIDATES_JSON" \
        --project-key "$PROJECT_KEY" \
        ${DREAM_RUN_ID:+--dream-run-id "$DREAM_RUN_ID"} \
        >> "$LOG_DIR/$TIMESTAMP.log" 2>&1
      validator_rc=$?
      set -e
      if (( validator_rc != 0 )); then
        log "FAIL promote — validator flagged integrity issues (dream_runs marked partial)"
        phase_rc=1
      fi
    fi

    # --- REORG: post-phase validator --------------------------------------
    # Symmetric to PROMOTE's validator. Runs only when the phase succeeded
    # (phase_rc==0). The validator never fails the pipeline — it marks the
    # dream_runs row partial and exits 1, which we translate to phase_rc=1
    # so the FAIL_TOTAL counter captures it, but the pipeline continues.
    # In dry-run mode the validator detects this from the JSON trailer and
    # skips all DB checks (nothing should have mutated).
    #
    # NOTE: effective_dry_run is local to run_phase() and is out of scope
    # here.  Recompute the same logic from the global inputs — this is the
    # exact same derivation run_phase uses for the REORG phase.
    if [[ "$name" == "reorg" && "$phase_rc" == "0" ]]; then
      reorg_effective_dry_run="$DRY_RUN"
      [[ "$BRAIN_DREAM_REORG_DRY_RUN" == "true" ]] && reorg_effective_dry_run="true"

      # Fetch the dream_runs.id for this reorg run (same helper, different phase filter).
      REORG_RUN_ID=$(
        uv run python -c "
import asyncio, sys, datetime as dt, sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from brain_v42.config import Settings
from brain_v42.db.tables import dream_runs

async def _get() -> None:
    eng = create_async_engine(Settings().postgres_url, pool_pre_ping=True)
    sf = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with sf() as s:
        r = (await s.execute(
            sa.select(dream_runs.c.id)
            .where(dream_runs.c.phase == 'reorg')
            .where(dream_runs.c.run_date == dt.date.fromisoformat('$TIMESTAMP'))
            .where(dream_runs.c.project_key == '$PROJECT_KEY')
            .order_by(dream_runs.c.id.desc())
            .limit(1)
        )).scalar_one_or_none()
    sys.stdout.write(str(r) if r is not None else '')

asyncio.run(_get())
" 2>> "$LOG_DIR/$TIMESTAMP.log" | tr -d '\n'
      )
      reorg_validator_flags=()
      reorg_validator_flags+=(--report-log "$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_${name}.log")
      reorg_validator_flags+=(--run-date "$TIMESTAMP")
      # Périmètre du run, comme pour promote et connect. Le serveur borne déjà
      # REORG à son projet, mais brain_list est le seul outil CRUD sans contrôle
      # de scope PROPRE — sa borne vit dans le middleware seul, et l'enforcement
      # vaut false par défaut dans le code. Si elle retombe, ceci est le dernier
      # endroit qui peut encore dire qu'une passe a franchi la frontière.
      reorg_validator_flags+=(--project-key "$PROJECT_KEY")
      [[ -n "$REORG_RUN_ID" ]] && reorg_validator_flags+=(--dream-run-id "$REORG_RUN_ID")
      [[ "$reorg_effective_dry_run" == "true" ]] && reorg_validator_flags+=(--dry-run)
      set +e
      uv run python -m scripts.dream.reorg_validate \
        "${reorg_validator_flags[@]}" \
        >> "$LOG_DIR/$TIMESTAMP.log" 2>&1
      validator_rc=$?
      set -e
      if (( validator_rc != 0 )); then
        log "FAIL reorg — validator flagged integrity issues (dream_runs marked partial)"
        phase_rc=1
      fi
    fi

    case "$phase_rc" in
      0) ;;
      2) TIMED_OUT_PHASES+=("$PROJECT_KEY/$name")
         manifest_put timeout "$name" "$PROJECT_KEY" ;;
      *) FAILED_PHASES+=("$PROJECT_KEY/$name")
         manifest_put failed "$name" "$PROJECT_KEY" ;;
    esac
  done
}

for _project in "${PROJECT_POOL[@]}"; do
  run_project_phases "$_project"
done  # fin de la boucle de projets


# --- EXTRACT: ticket knowledge extraction (proposer-only) -----------------
# Pas une phase claude -p : CLI Python direct (pattern domain_backfill,
# NVIDIA API JSON strict sans tools). Insère sa propre row dream_runs
# (phase='extract') pour la visibilité briefing (killswitches + last failure).
TOTAL_PHASES=$(( TOTAL_PHASES + 1 ))
manifest_put expected extract '*'
if [[ "$BRAIN_DREAM_EXTRACT_ENABLED" != "true" ]]; then
  log "SKIP extract (killswitch BRAIN_DREAM_EXTRACT_ENABLED=$BRAIN_DREAM_EXTRACT_ENABLED)"
  SKIPPED_PHASES+=("*/extract")
  manifest_put skipped extract '*' killswitch
else
  # The CLI owns a 9m deadline and checkpoints each ticket before returning
  # rc=3. The outer 10m timeout remains only as a last-resort process guard.
  extract_args=(--limit 20 --run-budget-seconds 540 --ticket-budget-seconds 180)
  if [[ "$BRAIN_DREAM_EXTRACT_DRY_RUN" != "true" ]]; then
    extract_args+=(--wet)
  fi
  log "extract: ticket_extract starting (dry_run=$BRAIN_DREAM_EXTRACT_DRY_RUN)"
  set +e
  timeout 10m uv run python -m scripts.ticket_extract "${extract_args[@]}" \
    >> "$LOG_DIR/${TIMESTAMP}_extract.log" 2>&1
  extract_rc=$?
  set -e
  if (( extract_rc == 0 )); then
    log "DONE extract"
  elif (( extract_rc == 3 )); then
    log "TIMEOUT extract (controlled deadline; terminal dream_run recorded)"
    TIMED_OUT_PHASES+=("*/extract")
    CONTROLLED_TIMEOUT_PHASES+=("*/extract")
    manifest_put timeout extract '*'
  elif (( extract_rc == 4 )); then
    # Deferral, not failure: the run declined to START tickets it could not
    # finish inside the budget, so nothing was cut short. Deferred tickets keep
    # their row in ticket_extraction_attempts and are retried first next run,
    # oldest closed_at first. Marking the unit failed here fired the alarm every
    # single night on designed behaviour, which is how it stopped being read.
    log "DEFERRED extract (nominal — tickets owed, see ticket_extraction_attempts)"
  elif (( extract_rc == 124 )); then
    log "TIMEOUT extract (outer guard; inspect ${TIMESTAMP}_extract.log)"
    TIMED_OUT_PHASES+=("*/extract")
    manifest_put timeout extract '*'
  else
    log "FAIL extract (rc=$extract_rc) — see ${TIMESTAMP}_extract.log"
    FAILED_PHASES+=("*/extract")
    manifest_put failed extract '*'
  fi
fi

# --- ROADMAP: curation nocturne de la roadmap (proposer-only) --------------
# Pas une phase claude -p : CLI Python direct (pattern extract). Insère sa
# propre row dream_runs (phase='roadmap') pour la visibilité briefing.
TOTAL_PHASES=$(( TOTAL_PHASES + 1 ))
manifest_put expected roadmap '*'
if [[ "$BRAIN_DREAM_ROADMAP_ENABLED" != "true" ]]; then
  log "SKIP roadmap (killswitch BRAIN_DREAM_ROADMAP_ENABLED=$BRAIN_DREAM_ROADMAP_ENABLED)"
  SKIPPED_PHASES+=("*/roadmap")
  manifest_put skipped roadmap '*' killswitch
else
  roadmap_args=(--limit 10)
  if [[ "$BRAIN_DREAM_ROADMAP_DRY_RUN" != "true" ]]; then
    roadmap_args+=(--wet)
  fi
  log "roadmap: roadmap_curate starting (dry_run=$BRAIN_DREAM_ROADMAP_DRY_RUN)"
  set +e
  # 20m : premier run réel (2026-07-04) à 597s/600s — zéro marge sous 10m.
  # Pinned par tests/unit/test_dream_sh_roadmap.py.
  timeout 20m uv run python -m scripts.roadmap_curate "${roadmap_args[@]}" \
    >> "$LOG_DIR/${TIMESTAMP}_roadmap.log" 2>&1
  roadmap_rc=$?
  set -e
  if (( roadmap_rc == 0 )); then
    log "DONE roadmap"
  else
    log "FAIL roadmap (rc=$roadmap_rc) — see ${TIMESTAMP}_roadmap.log"
    FAILED_PHASES+=("*/roadmap")
    manifest_put failed roadmap '*'
  fi
fi

# --- SWEEP: tarissement des sessions fantômes ------------------------------
# Pas une phase d'agent : CLI Python direct (pattern extract/roadmap). Insère
# sa propre row dream_runs (phase='sweep', model NULL) pour la visibilité
# briefing. Le seuil n'est PAS passé en argument : une seule constante.
TOTAL_PHASES=$(( TOTAL_PHASES + 1 ))
manifest_put expected sweep '*'
if [[ "$BRAIN_DREAM_SWEEP_ENABLED" != "true" ]]; then
  log "SKIP sweep (killswitch BRAIN_DREAM_SWEEP_ENABLED=$BRAIN_DREAM_SWEEP_ENABLED)"
  SKIPPED_PHASES+=("*/sweep")
  manifest_put skipped sweep '*' killswitch
else
  sweep_args=()
  if [[ "$BRAIN_DREAM_SWEEP_DRY_RUN" != "true" ]]; then
    sweep_args+=(--wet)
  fi
  log "sweep: session_sweep starting (dry_run=$BRAIN_DREAM_SWEEP_DRY_RUN)"
  set +e
  # 5m : une requête indexée, sans appel modèle ni réseau. Un dépassement
  # signale une base en souffrance, pas une phase lente.
  timeout 5m uv run python -m brain_v42.maintenance.session_sweep "${sweep_args[@]}" \
    >> "$LOG_DIR/${TIMESTAMP}_sweep.log" 2>&1
  sweep_rc=$?
  set -e
  if (( sweep_rc == 0 )); then
    log "DONE sweep"
  else
    log "FAIL sweep (rc=$sweep_rc) — see ${TIMESTAMP}_sweep.log"
    FAILED_PHASES+=("*/sweep")
    manifest_put failed sweep '*'
  fi
fi

# Deux questions distinctes — surtout pas une seule.
#   FAIL_TOTAL : « faut-il ALERTER ? » Sensibilité inchangée depuis toujours ;
#                il pilote aussi OK_TOTAL et le résumé.
#   la garde de sortie plus bas : « faut-il rougir l'unité systemd ? »
# Les fusionner remettrait le défaut d'origine : soit l'unité rouge toutes les
# nuits (état sans information), soit l'alerte éteinte avec elle.
FAIL_TOTAL=$(( ${#FAILED_PHASES[@]} + ${#TIMED_OUT_PHASES[@]} ))
OK_TOTAL=$(( TOTAL_PHASES - FAIL_TOTAL ))

# Le résumé dit toute la vérité, quel que soit le code de sortie retenu plus
# bas : on ne cache aucun timeout, on cesse seulement d'en faire un échec
# d'unité quand l'échéance était bornée (voir 2026-04-09 postmortem : un
# timeout silencieux non détecté — et 2026-08-07 : une unité rouge en
# permanence, devenue tout aussi muette).
summary="${OK_TOTAL}/${TOTAL_PHASES} phases OK"
if (( ${#FAILED_PHASES[@]} > 0 )); then
  summary+=", ${#FAILED_PHASES[@]} failed (${FAILED_PHASES[*]})"
fi
if (( ${#TIMED_OUT_PHASES[@]} > 0 )); then
  summary+=", ${#TIMED_OUT_PHASES[@]} timed out (${TIMED_OUT_PHASES[*]})"
fi
if (( ${#SKIPPED_PHASES[@]} > 0 )); then
  summary+=", ${#SKIPPED_PHASES[@]} skipped (${SKIPPED_PHASES[*]})"
fi
# Une phase repliée est une phase RÉUSSIE — elle n'entre donc ni dans FAIL_TOTAL
# ni dans la garde de sortie plus bas, et c'est voulu : le secours a fait son
# travail, rougir l'unité pour ça la rendrait muette. Mais elle doit se VOIR.
# Du 2026-08-11 au 2026-08-17 les 60 phases codex de chaque nuit échouaient,
# agy les rattrapait toutes, et la nuit signait « 63/63 phases OK » : six nuits
# sans rail primaire, sans un seul voyant. FAIL_TOTAL compte des phases quand
# dream_runs compte des tentatives, et seul le résumé est lu au matin.
if (( ${#FALLBACK_PHASES[@]} > 0 )); then
  summary+=", ${#FALLBACK_PHASES[@]} repliées sur un secours (${FALLBACK_PHASES[*]})"
fi
log "=== Dream finished: $summary ==="

# Le bloc de CLÔTURE, seule partie non incrémentale du manifeste. Son absence
# est le marqueur d'une nuit interrompue : le lecteur refuse alors tout verdict
# vert. `total_phases` est le compteur propre de dream.sh, à confronter au
# `planned_phases` d'en-tête et au nombre d'attendus réellement atteints —
# trois nombres, trois instants, trois chemins de code.
manifest_put meta total_phases "$TOTAL_PHASES"
manifest_put meta ok_total "$OK_TOTAL"
manifest_put meta fail_total "$FAIL_TOTAL"
manifest_put meta finished "$(date -Iseconds)"

# Keep a bounded operational report in the dated Dream log. Session briefings
# read the same failures directly from dream_runs. The helper's failure must
# NOT mask the Dream failure exit code — it is added to it, never substituted.
set +e
# UNE alerte, après la boucle, groupée par projet (§11). Pas une par projet :
# le rapport ne filtre pas sur la clé, donc N invocations produiraient N blocs
# IDENTIQUES listant les échecs de tous les projets.
#
# La sortie est CAPTURÉE, plus redirigée à l'aveugle. `log()` fait un `tee` vers
# stdout — donc vers journald — quand cette redirection-ci n'écrivait que dans le
# fichier daté : le corps de l'alerte n'a jamais atteint `journalctl`, ce qui est
# la moitié physique du « personne ne la lit » (ticket 0a9c067e). Le rapport est
# borné en amont (MAX_FETCHED_FAILURES), donc la capture en variable l'est aussi.
alert_out="$(uv run python -m scripts.dream.post_run_alert \
  --date "$TIMESTAMP" --manifest "$MANIFEST_FILE" 2>&1)"
alert_rc=$?
set -e
printf '%s\n' "$alert_out" >> "$LOG_DIR/$TIMESTAMP.log"
coverage_line="$(printf '%s\n' "$alert_out" | grep -m1 '^COVERAGE ' || true)"
if [[ -n "$coverage_line" ]]; then
  # Les deux nombres que le ticket dit que personne ne rapproche, côte à côte
  # dans journald, TOUTES les nuits — y compris les vertes, sans quoi la ligne
  # ne serait lue que les jours où il est déjà trop tard.
  log "=== dream_runs $coverage_line ==="
fi
if (( alert_rc == 2 )); then
  log "FAIL  dream_runs coverage — des lignes attendues manquent sans explication"
  coverage_silent="$(printf '%s\n' "$alert_out" | grep -m1 '^COVERAGE_SILENT ' || true)"
  # T2 — le verdict porté jusqu'à un lecteur qui existe. Cette ligne atteint
  # « ### Last failure » du briefing de session et /metrics nightly.last_failure
  # SANS une ligne de code chez eux. T1 seul n'atteint que journald, et la leçon
  # du ticket est qu'un signal sans lecteur est indiscernable d'un signal absent.
  #
  # `set +e` est INDISPENSABLE : errexit est actif ici, la garde de sortie vit
  # une trentaine de lignes plus bas, et ce writer rend 1 sur échec — comme
  # `record-empty-pool`, dont il est le calque. Sans cet encadrement, dream.sh
  # sortirait AVANT sa garde structurelle, sans même imprimer le WARN.
  set +e
  uv run python -m scripts.dream.record_coverage_gap \
    --date "$TIMESTAMP" --summary "$coverage_line" --detail "$coverage_silent" \
    >> "$LOG_DIR/$TIMESTAMP.log" 2>&1
  record_gap_rc=$?
  set -e
  if (( record_gap_rc != 0 )); then
    log "WARN  coverage — ligne dream_runs 'coverage' NON enregistrée (rc=$record_gap_rc)"
  fi
  # Interrupteur de secours, lu par dream.sh SEUL : ce n'est pas une phase, donc
  # il n'entre pas dans la table des killswitches. Désarmé, il continue
  # d'imprimer le verdict ET de dire qu'il est désarmé — le détecteur ne peut pas
  # être éteint en silence.
  if [[ "${BRAIN_DREAM_COVERAGE_STRICT:-true}" != "true" ]]; then
    log "WARN  escalade désarmée (BRAIN_DREAM_COVERAGE_STRICT=false) — unité laissée verte"
    alert_rc=0
  fi
elif (( alert_rc != 0 )); then
  log "WARN  post_run_alert failed (rc=$alert_rc)"
fi

# Garde STRUCTURELLE, pas arithmétique. La forme d'origine soustrayait
# ${#CONTROLLED_TIMEOUT_PHASES[@]} du total : elle n'était fail-closed que tant
# que CONTROLLED ⊆ TIMED_OUT, invariant qu'aucune garde n'imposait — une phase
# inscrite par erreur dans FAILED **et** CONTROLLED effaçait son propre échec
# et le script sortait en 0 après avoir imprimé « 1 failed (synth) ».
# Ici FAILED_PHASES est interrogé pour lui-même, donc inmasquable.
#   1. échec dur                      -> rouge
#   2. timeout NON borné (garde-fou externe, état inconnu) -> rouge
#   3. rapporteur muet (l'alerte n'est PAS partie) -> rouge : depuis que la
#      nuit à échéance contrôlée sort en 0, le log est le seul témoin restant.
if (( ${#FAILED_PHASES[@]} > 0 )) \
  || (( ${#TIMED_OUT_PHASES[@]} > ${#CONTROLLED_TIMEOUT_PHASES[@]} )) \
  || (( alert_rc != 0 )); then
  exit 1
fi

# Seules des échéances contrôlées : la nuit s'est déroulée comme prévu jusqu'à
# sa limite de temps. L'alerte est partie, l'unité reste verte — pour qu'un
# `failed` veuille de nouveau dire quelque chose.
#
# Conditionné à FAIL_TOTAL depuis que le cas propre passe par ici : sans cette
# garde, une nuit SANS la moindre anomalie signerait « anomalies bornées
# uniquement », ce qui est faux et use la ligne exactement comme une unité
# rouge en permanence use la couleur.
if (( FAIL_TOTAL > 0 )); then
  log "=== Dream exit 0 — anomalies bornées uniquement (échéances contrôlées) ==="
fi
exit 0

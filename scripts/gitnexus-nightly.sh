#!/usr/bin/env bash
# Nightly GitNexus reindex for brain-v42.
# Invoked by user crontab at 04:30. See spec:
#   docs/superpowers/specs/2026-04-20-gitnexus-integration-design.md

set -u  # note: NOT -e — we want the script to keep going past a single reindex failure so the rotation step still runs.

REPO_DIR="/home/hawixs/hawkixs_infra/git_repo/brain_v42"
LOG_DIR="/tmp"
LOG_FILE="${LOG_DIR}/gitnexus-nightly.log"
# Literal, not "${GITNEXUS_BIN:-...}": test_container_image_pins.py refuses an
# executable path that an env var can redirect, so the pinned-binary audit stays
# decidable by reading this file alone.
GITNEXUS_BIN="/home/hawixs/.npm-global/bin/gitnexus"
# The registry entry name `gitnexus list` uses for this repo (see project_context).
GITNEXUS_ENTRY_NAME="brain-v42"

# --- Index-freshness check (learning cb7d7164) -----------------------------
# `gitnexus analyze` has been observed to exit 0 twice while the index did NOT
# actually advance (two distinct failure modes, one of them a duplicate
# CodeEmbedding primary key on 2026-08-05). The exit code alone cannot be
# trusted. We instead compare git HEAD against the `lastCommit` GitNexus
# reports for this repo via `gitnexus list` after the run.
#
# The nuance that matters: if HEAD hasn't moved since the previous night, an
# unchanged indexed commit is the NORMAL case, not a failure — there was
# nothing new to index. Comparing "indexed commit == current HEAD" (rather
# than "indexed commit changed since last run") captures this for free: on a
# no-commit night, HEAD already equals the previously-indexed commit, so the
# check still passes without any extra state file.

# gitnexus_list_entries: reads `gitnexus list` output on stdin, emits one
# "name<TAB>path<TAB>commit" row per registry entry. Pure parsing, no I/O.
gitnexus_list_entries() {
  awk '
    function flush() {
      if (have) printf "%s\t%s\t%s\n", name, path, commit
    }
    BEGIN { name=""; path=""; commit=""; have=0 }
    /^  [^ ]/ {
      flush()
      line = $0
      sub(/^  /, "", line)
      sub(/ \(.*/, "", line)
      name = line
      path = ""
      commit = ""
      have = 1
      next
    }
    /^    Path:/ {
      v = $0
      sub(/^    Path:[ \t]*/, "", v)
      path = v
      next
    }
    /^    Commit:/ {
      v = $0
      sub(/^    Commit:[ \t]*/, "", v)
      commit = v
      next
    }
    END { flush() }
  '
}

# resolve_indexed_commit LIST_OUTPUT ENTRY_NAME REPO_PATH
# Prints the indexed commit for the single registry entry matching
# ENTRY_NAME/REPO_PATH on stdout. Return codes:
#   0 = exactly one match, commit printed
#   1 = no entry with that name found
#   2 = ambiguous — more than one entry with that name (e.g. a stray
#       worktree registration; see project_context on the two-brain-v42
#       registry incident)
#   3 = a unique entry with that name exists but its Path does not match
#       REPO_PATH
resolve_indexed_commit() {
  local list_output="$1" entry_name="$2" repo_path="$3"
  local rows count mpath mcommit

  rows="$(printf '%s\n' "$list_output" | gitnexus_list_entries | awk -F'\t' -v n="$entry_name" '$1==n')"
  if [ -z "$rows" ]; then
    return 1
  fi

  count="$(printf '%s\n' "$rows" | grep -c '.')"
  if [ "$count" -gt 1 ]; then
    return 2
  fi

  mpath="$(printf '%s' "$rows" | cut -f2)"
  mcommit="$(printf '%s' "$rows" | cut -f3)"
  if [ "$mpath" != "$repo_path" ]; then
    return 3
  fi

  printf '%s\n' "$mcommit"
  return 0
}

# commit_matches_head HEAD_SHA INDEXED_COMMIT
# True if INDEXED_COMMIT is a (possibly abbreviated) prefix of HEAD_SHA.
commit_matches_head() {
  local head="$1" idx="$2"
  [ -n "$idx" ] && [ "${head#"$idx"}" != "$head" ]
}

main() {
  # Rotate previous log (keep last 7 nights).
  for i in 6 5 4 3 2 1; do
    src="${LOG_FILE}.${i}"
    dst="${LOG_FILE}.$((i+1))"
    [ -f "$src" ] && mv "$src" "$dst"
  done
  [ -f "$LOG_FILE" ] && mv "$LOG_FILE" "${LOG_FILE}.1"

  {
    echo "=== gitnexus-nightly run at $(date --iso-8601=seconds) ==="
    cd "$REPO_DIR" || { echo "ERROR: cannot cd to $REPO_DIR"; exit 2; }

    head_commit="$(git rev-parse HEAD)"
    echo "=== HEAD before analyze: ${head_commit:-<unknown>} ==="

    # --no-stats, not --skip-agents-md: the gitnexus:* block in CLAUDE.md/AGENTS.md
    # is allowed to refresh, but without the volatile symbol/relationship counters
    # that made every reindex produce a diff. --skip-agents-md froze the block
    # instead, which let it drift (it still claimed 19448 symbols against 19482).
    # --wal-checkpoint-threshold 67108864: without it, analyze has exited 0 twice
    # while the index did not actually advance (learning cb7d7164, incl. a
    # duplicate CodeEmbedding primary key on 2026-08-05) — the exit code is not
    # trusted here, see the index-freshness check below.
    "$GITNEXUS_BIN" analyze --embeddings --no-stats --wal-checkpoint-threshold 67108864 . 2>&1
    status=$?
    echo "=== analyze exit status: $status ==="

    if [ -z "$head_commit" ]; then
      echo "=== index check: FAILED — could not determine git HEAD ==="
      status=1
    else
      list_output="$("$GITNEXUS_BIN" list 2>&1)"
      indexed_commit="$(resolve_indexed_commit "$list_output" "$GITNEXUS_ENTRY_NAME" "$REPO_DIR")"
      resolve_status=$?

      case "$resolve_status" in
        0)
          if commit_matches_head "$head_commit" "$indexed_commit"; then
            echo "=== index check: OK (indexed commit $indexed_commit matches HEAD $head_commit) ==="
          else
            echo "=== index check: FAILED — indexed commit $indexed_commit does not match HEAD $head_commit; index did not advance ==="
            status=1
          fi
          ;;
        1)
          echo "=== index check: FAILED — no '$GITNEXUS_ENTRY_NAME' entry in 'gitnexus list' ==="
          status=1
          ;;
        2)
          echo "=== index check: FAILED — ambiguous registry: more than one '$GITNEXUS_ENTRY_NAME' entry in 'gitnexus list' ==="
          status=1
          ;;
        3)
          echo "=== index check: FAILED — '$GITNEXUS_ENTRY_NAME' entry found but its Path does not match $REPO_DIR ==="
          status=1
          ;;
        *)
          echo "=== index check: FAILED — unexpected resolve_indexed_commit status $resolve_status ==="
          status=1
          ;;
      esac
    fi

    echo "=== exit status: $status ==="
    exit "$status"
  } > "$LOG_FILE" 2>&1
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
fi

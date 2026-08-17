#!/usr/bin/env bash

# Resolve the Python used by the systemd integration smoke without assuming
# that the current checkout is a Git worktree or carries its own virtualenv.
_absolute_executable_path() {
  local candidate="$1"
  local candidate_dir=""
  local candidate_name=""
  local absolute_dir=""

  [[ -f "$candidate" && -x "$candidate" ]] || return 1
  candidate_name="${candidate##*/}"
  candidate_dir="${candidate%/*}"
  if [[ "$candidate_dir" == "$candidate" ]]; then
    candidate_dir="."
  fi
  absolute_dir="$(
    builtin cd -- "$candidate_dir" 2>/dev/null \
      && builtin pwd -P
  )" || return 1

  if [[ "$absolute_dir" == "/" ]]; then
    printf '/%s\n' "$candidate_name"
  else
    printf '%s/%s\n' "$absolute_dir" "$candidate_name"
  fi
}

resolve_test_python() {
  local source_root="$1"
  local candidate=""
  local common_git_dir=""
  local common_repo_root=""
  local git_bin=""

  if [[ -n "${BRAIN_TEST_PYTHON:-}" ]]; then
    if ! candidate="$(_absolute_executable_path "$BRAIN_TEST_PYTHON")"; then
      echo "ERROR: BRAIN_TEST_PYTHON is not executable: $BRAIN_TEST_PYTHON" >&2
      return 1
    fi
    printf '%s\n' "$candidate"
    return 0
  fi

  git_bin="$(type -P git || true)"
  if [[ -n "$git_bin" && -f "$git_bin" && -x "$git_bin" ]] \
    && common_git_dir="$(
      "$git_bin" -C "$source_root" rev-parse \
        --path-format=absolute --git-common-dir 2>/dev/null
    )" \
    && [[ -n "$common_git_dir" ]]; then
    common_repo_root="${common_git_dir%/*}"
    candidate="$common_repo_root/.venv/bin/python"
    if candidate="$(_absolute_executable_path "$candidate")"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  fi

  candidate="$(type -P python3 || true)"
  if candidate="$(_absolute_executable_path "$candidate")"; then
    printf '%s\n' "$candidate"
    return 0
  fi

  candidate="$(type -P python || true)"
  if candidate="$(_absolute_executable_path "$candidate")"; then
    printf '%s\n' "$candidate"
    return 0
  fi

  echo "ERROR: no executable Python interpreter found" >&2
  return 1
}

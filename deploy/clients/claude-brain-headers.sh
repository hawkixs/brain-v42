#!/bin/sh
# Claude Code headersHelper for the brain-v42 MCP (user scope, Linux workstation).
#
# Prints the bearer read from the 0600 token file and, as X-Brain-Agent, what the
# server needs to attribute the session to a project. The server keeps the basename
# of a path and requires an existing kebab project; anything else falls back to the
# client actor and counts mcp_auth_agent_unresolved (ticket 8955769a). So:
#   - the working directory of the Claude Code process running this helper is used,
#     never ${PWD}: Desktop sessions inherit the app's PWD (the home directory);
#   - an operator-owned map (lines "<absolute path> <project_key>", longest path
#     prefix wins) names the project of a directory whose basename is not one,
#     and the project key is then sent instead of the path;
#   - the home directory itself and the daemon's prewarmed /tmp/cc-daemon-*/spare
#     processes claim no project: no X-Brain-Agent at all, so the call runs under
#     the client actor without counting a miss;
#   - a path that cannot be carried safely in the JSON line claims no project.
# Fails (non-zero, nothing on stdout) when the token file is missing or empty, or
# when the token holds a character JSON cannot carry unescaped.
set -eu
file="${BRAIN_CLAUDE_TOKEN_FILE:-$HOME/.config/brain-v42/clients/workstation-claude.token}"
map="${BRAIN_AGENT_MAP_FILE:-$HOME/.config/brain-v42/clients/agent-map}"
token=$(tr -d '[:space:]' < "$file")
[ -n "$token" ] || { echo "brain-v42 headersHelper: empty token file $file" >&2; exit 1; }
case "$token" in
  *[\"\\]*|*[[:cntrl:]]*)
    echo "brain-v42 headersHelper: unusable token in $file" >&2; exit 1 ;;
esac

claude_cwd() {
  pid=$PPID
  while [ "$pid" -gt 1 ]; do
    exe=$(readlink "/proc/$pid/exe" 2>/dev/null || true)
    case "$exe" in
      */claude|*/ccd-cli/*|*/claude/versions/*|*/claude-code/*)
        readlink "/proc/$pid/cwd" 2>/dev/null || true
        return 0 ;;
    esac
    pid=$(awk '{print $4}' "/proc/$pid/stat" 2>/dev/null || echo 1)
  done
}

# Only a regular file owned by this user and closed to group and others is trusted:
# anyone else able to write it, or to own it, could redirect attribution.
map_is_private() {
  [ -f "$map" ] && [ ! -L "$map" ] && [ -r "$map" ] &&
    [ -n "$(find "$map" -prune -type f -user "$(id -u)" ! -perm /077 2>/dev/null)" ]
}

# The server's project key syntax: ^[a-z0-9]+([:-][a-z0-9]+)*$
is_project_key() {
  case "$1" in
    ''|*[!a-z0-9:-]*|[:-]*|*[:-]|*[:-][:-]*) return 1 ;;
  esac
}

mapped_project() {
  best="" best_len=-1
  map_is_private || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    key=${line##* }
    path=${line% *}
    [ "$path" != "$line" ] || continue
    case "$path" in /*) ;; *) continue ;; esac
    is_project_key "$key" || continue
    while [ "$path" != "/" ] && [ "${path%/}" != "$path" ]; do path=${path%/}; done
    if [ "$path" = "/" ]; then
      matched=yes   # the root prefixes every absolute path
    else
      case "$1" in "$path"|"$path"/*) matched=yes ;; *) matched=no ;; esac
    fi
    if [ "$matched" = yes ] && [ "${#path}" -gt "$best_len" ]; then
      best=$key best_len=${#path}
    fi
  done < "$map"
  printf '%s' "$best"
}

agent=$(claude_cwd)
home=$HOME
while [ "$home" != "/" ] && [ "${home%/}" != "$home" ]; do home=${home%/}; done
case "$agent" in
  "$home"|/tmp/cc-daemon-*/spare) agent="" ;;
esac
case "$agent" in
  /*)
    project=$(mapped_project "$agent")
    [ -z "$project" ] || agent=$project ;;
esac
# JSON cannot carry a quote, a backslash or a control character unescaped.
case "$agent" in
  *[\"\\]*|*[[:cntrl:]]*) agent="" ;;
esac
if [ -n "$agent" ]; then
  printf '{"Authorization":"Bearer %s","X-Brain-Agent":"%s"}\n' "$token" "$agent"
else
  printf '{"Authorization":"Bearer %s"}\n' "$token"
fi

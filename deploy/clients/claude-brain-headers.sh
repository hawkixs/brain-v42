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
# Credential tokens are URL-safe ASCII ("bk1_" + token_urlsafe); anything else could
# not be printed as valid JSON, so it is refused rather than passed through.
case "$token" in
  *[!A-Za-z0-9_.~+/=-]*)
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

# Only a map nobody else can change is trusted, since it decides attribution: its
# directory must be this user's and closed to group and others, the path must not be a
# symlink, and the file actually opened (checked through its descriptor, so a swap
# between the check and the read cannot slip another file in) must be a regular file
# owned by this user with no group or other permission.
uid=$(id -u)
private_entry() {  # $1 = path, $2 = find type
  [ -n "$(find "$1" -prune -type "$2" -user "$uid" ! -perm /077 2>/dev/null)" ]
}
map_dir_is_private() {
  dir=${map%/*}
  [ -n "$dir" ] || dir=/
  [ ! -L "$dir" ] && private_entry "$dir" d
}
opened_map_is_private() {  # the map is open on descriptor 3
  [ -n "$(find -L /proc/self/fd/3 -prune -type f -user "$uid" ! -perm /077 2>/dev/null)" ]
}

# The server's project key syntax: ^[a-z0-9]+([:-][a-z0-9]+)*$
is_project_key() {
  case "$1" in
    ''|*[!a-z0-9:-]*|[:-]*|*[:-]|*[:-][:-]*) return 1 ;;
  esac
}

mapped_project() {
  best="" best_len=-1
  [ -f "$map" ] && [ ! -L "$map" ] && map_dir_is_private || return 0
  exec 3< "$map" || return 0
  opened_map_is_private || { exec 3<&-; return 0; }
  while IFS= read -r line <&3 || [ -n "$line" ]; do
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
  done
  exec 3<&-
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
# The agent is printed into JSON unescaped: only printable ASCII without a quote or a
# backslash can be; any other path claims no project rather than break the line.
case "$agent" in
  *[!\ -~]*|*[\"\\]*) agent="" ;;
esac
if [ -n "$agent" ]; then
  printf '{"Authorization":"Bearer %s","X-Brain-Agent":"%s"}\n' "$token" "$agent"
else
  printf '{"Authorization":"Bearer %s"}\n' "$token"
fi

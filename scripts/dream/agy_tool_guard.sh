#!/usr/bin/env bash
#
# Garde d'outils du rail agy — hook `PreToolUse`, deny par défaut.
#
# POURQUOI ELLE EXISTE. agy n'a aucun équivalent du `--tools ""` de claude ni de
# l'`enabled_tools` de codex. Mesuré le 2026-08-11 sur v1.1.11 : il expose 56
# outils, dont run_command, write_to_file, invoke_subagent, schedule et le
# contrôle du navigateur. `--dangerously-skip-permissions`, REQUIS en headless
# sinon la session attend une approbation qui ne viendra jamais, les
# auto-approuve tous. Sur un prompt demandant UN appel MCP, agy a exécuté douze
# étapes d'outils et lancé `ps aux`. Sans cette garde, une phase de dream non
# surveillée obtient un shell sous un compte membre de `sudo` et `docker`.
#
# Trois autres mécanismes ont été essayés et MESURÉS inopérants : `disabledTools`
# dans settings.json (aucun effet), `--mode plan` documenté « read-only »
# (exécute quand même le shell), et un flag CLI de restriction (n'existe pas).
# Le hook `PreToolUse` rendant `{"decision":"deny"}` est le seul qui tienne — et
# il survit à `--dangerously-skip-permissions`, ce qui est le point qui aurait
# pu tout invalider.
#
# CE QU'ELLE NE FAIT PAS. Elle protège la MACHINE, pas le corpus. Ce qu'une
# phase peut faire au brain est borné côté SERVEUR par le bearer de
# (projet, phase) — vérifié : un tools/call direct hors périmètre répond
# « Dream capability authorization denied ». D'où l'autorisation en bloc de
# `call_mcp_tool` ici : cette garde n'a pas à rejouer un contrôle que le serveur
# fait mieux, et le dupliquer créerait deux listes à tenir d'accord à la main.
#
# INCONDITIONNELLE, sans interrupteur. Une version antérieure la conditionnait à
# BRAIN_DREAM_PHASE ; le HOME éphémère du runner rend ça inutile, puisqu'elle
# n'est câblée que là. Un interrupteur ne serait qu'une chose à oublier de
# poser — et son oubli serait silencieux.
#
# Contrat (docs/hooks.md d'agy) : payload JSON sur stdin, décision JSON sur
# stdout. Tout ce qui s'écrirait d'autre sur stdout casserait le parsing et
# transformerait un refus en autorisation.

set -uo pipefail

payload=$(cat 2>/dev/null || true)

# Allowlist, pas denylist. agy expose 56 outils aujourd'hui et en gagnera
# d'autres à chaque version : une allowlist se périme dans le bon sens (un outil
# neuf est refusé), une denylist dans le mauvais (un outil neuf passe).
_allow() { printf '{"decision":"allow"}'; }
_deny()  { printf '{"decision":"deny","reason":"%s"}' "$1"; }

tool_name=$(
  printf '%s' "$payload" | python3 -c '
import json, sys
try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if not isinstance(payload, dict):
    sys.exit(0)
call = payload.get("toolCall")
if not isinstance(call, dict):
    sys.exit(0)
name = call.get("name")
if isinstance(name, str):
    print(name)
' 2>/dev/null
)

# Fail-closed. Un payload vide, illisible ou d'une forme inattendue ne prouve
# rien — et le jour où agy changera ce format, une garde qui s'ouvre sur
# l'inconnu laisserait tout passer sans un bruit.
if [[ -z "$tool_name" ]]; then
  _deny "payload de hook illisible — refus par defaut"
  exit 0
fi

case "$tool_name" in
  # La passerelle vers brain-v42, et le strict nécessaire pour qu'un tour
  # d'agent puisse rendre son rapport.
  call_mcp_tool|list_resources|read_resource|finish|send_message)
    _allow
    ;;
  *)
    _deny "outil hors perimetre d'une phase de dream: $tool_name"
    ;;
esac

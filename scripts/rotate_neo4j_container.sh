#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/../docker-compose.yml"

action="${1:-}"
case "$action" in
  compose-config)
    [[ $# -eq 1 ]] || exit 64
    exec docker compose -f "$COMPOSE_FILE" config --no-interpolate --quiet
    ;;
  compose-up)
    [[ $# -eq 1 ]] || exit 64
    exec docker compose -f "$COMPOSE_FILE" up -d --pull never --wait --wait-timeout 90 \
      --no-deps --force-recreate neo4j
    ;;
  inspect)
    [[ $# -eq 2 ]] || exit 64
    exec docker inspect --format "$2" brain_v42_neo4j
    ;;
  *)
    exit 64
    ;;
esac

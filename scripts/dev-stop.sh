#!/bin/zsh

# RidePilot AI — Stop and remove all containers
# Stops containers and removes them (volumes are preserved)

set -e

SCRIPT_DIR=$(dirname "$0")
PROJECT_DIR="$SCRIPT_DIR/.."

echo "Stopping RidePilot AI..."

docker compose -f "$PROJECT_DIR/docker-compose.yml" down

echo "All containers stopped and removed."
echo "Postgres data volume is preserved — run './scripts/dev-reset-db.sh' to wipe it."

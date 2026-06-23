#!/bin/zsh

# RidePilot AI — Reset database
# Stops containers, wipes the postgres volume, and restarts everything fresh

set -e

SCRIPT_DIR=$(dirname "$0")
PROJECT_DIR="$SCRIPT_DIR/.."

echo "Resetting database..."
echo "WARNING: This will permanently delete all data in the postgres volume."
echo ""
read "REPLY?Are you sure? (y/N) "

if [[ "$REPLY" != "y" && "$REPLY" != "Y" ]]; then
  echo "Aborted."
  exit 0
fi

docker compose -f "$PROJECT_DIR/docker-compose.yml" down -v

echo "Volume wiped. Restarting with a fresh database..."

docker compose -f "$PROJECT_DIR/docker-compose.yml" up --build -d

echo ""
echo "Fresh database ready. All services running:"
echo "  AI Service       → http://localhost:8001/health"
echo "  Quote Service    → http://localhost:8002/health"
echo "  Booking Service  → http://localhost:8003/health"
echo "  Mock Providers   → http://localhost:8004/health"

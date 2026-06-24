#!/bin/zsh

# RidePilot AI — Reset database only
# Wipes the postgres data and brings postgres back up. Other services are left
# running untouched.

set -e

SCRIPT_DIR=$(dirname "$0")
PROJECT_DIR="$SCRIPT_DIR/.."

COMPOSE="docker compose -f $PROJECT_DIR/docker-compose.yml -f $PROJECT_DIR/docker-compose.override.yml"

echo "Resetting database..."
echo "WARNING: This will permanently delete all data in the postgres volume."
echo ""
read "REPLY?Are you sure? (y/N) "

if [[ "$REPLY" != "y" && "$REPLY" != "Y" ]]; then
  echo "Aborted."
  exit 0
fi

# Stop and remove only the postgres container along with its named volume,
# leaving the other services running.
eval "$COMPOSE rm -sfv postgres"
docker volume rm ridepilot-ai_postgres_data 2>/dev/null || true

echo "Volume wiped. Starting a fresh postgres..."

eval "$COMPOSE up -d postgres"

# The DB-backed services create their tables in their startup hook, which only
# runs at service start. Restart them so they recreate tables on the fresh DB.
echo "Restarting DB-backed services to recreate tables..."
eval "$COMPOSE restart quote-service booking-service ai-service"

echo ""
echo "Fresh database ready (postgres → localhost:5433). DB-backed services restarted."
echo "  AI Service       → http://localhost:8001/health"
echo "  Quote Service    → http://localhost:8002/health"
echo "  Booking Service  → http://localhost:8003/health"
echo "  Mock Providers   → http://localhost:8004/health (left running)"

#!/bin/zsh

# RidePilot AI — Start all services WITHOUT building
# Starts existing containers/images and applies the dev override (bind mounts +
# uvicorn --reload). Use this for day-to-day startup once images already exist.
# If you changed requirements.txt or a Dockerfile, use './scripts/dev-start.sh'
# instead (it rebuilds), or rebuild the affected service manually.

set -e

SCRIPT_DIR=$(dirname "$0")
PROJECT_DIR="$SCRIPT_DIR/.."

echo "Starting RidePilot AI (no build)..."

# Copy .env from .env.example if .env doesn't exist
if [ ! -f "$PROJECT_DIR/.env" ]; then
  echo ".env not found — copying from .env.example"
  cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
  echo "Created .env — update OPENAI_API_KEY before using the AI service"
fi

docker compose -f "$PROJECT_DIR/docker-compose.yml" -f "$PROJECT_DIR/docker-compose.override.yml" up -d

echo ""
echo "All services running:"
echo "  Frontend         → http://localhost:3000"
echo "  AI Service       → http://localhost:8001/health"
echo "  Quote Service    → http://localhost:8002/health"
echo "  Booking Service  → http://localhost:8003/health"
echo "  Mock Providers   → http://localhost:8004/health"

#!/bin/zsh

# RidePilot AI — Start all services
# Builds images if not already built, then starts all containers

set -e

SCRIPT_DIR=$(dirname "$0")
PROJECT_DIR="$SCRIPT_DIR/.."

echo "Starting RidePilot AI..."

# Copy .env from .env.example if .env doesn't exist
if [ ! -f "$PROJECT_DIR/.env" ]; then
  echo ".env not found — copying from .env.example"
  cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
  echo "Created .env — update OPENAI_API_KEY before using the AI service"
fi

docker compose -f "$PROJECT_DIR/docker-compose.yml" up --build -d

echo ""
echo "All services running:"
echo "  AI Service       → http://localhost:8001/health"
echo "  Quote Service    → http://localhost:8002/health"
echo "  Booking Service  → http://localhost:8003/health"
echo "  Mock Providers   → http://localhost:8004/health"

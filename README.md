# RidePilot AI

An AI-powered ride assistant that helps users search for rides, compare providers, monitor live prices, and book — built as a production-style microservices project.

## Project Structure

```
ridepilot/
├── services/
│   ├── ai-service/          # Central hub — FastAPI, handles all frontend communication
│   ├── quote-service/       # Quote sessions, provider calls, price monitoring
│   ├── booking-service/     # Booking lifecycle, confirmations, ride status
│   └── mock-providers/      # Simulated Uber, Lyft, Waymo APIs
├── frontend/                # Next.js + TypeScript
├── docs/
│   └── design.md            # Full architecture and design document
└── docker-compose.yml       # Local dev environment
```

## Scripts

| Script | What it does |
|---|---|
| `./scripts/dev-start.sh` | Builds images if needed, starts all containers |
| `./scripts/dev-stop.sh` | Stops and removes containers (preserves database) |
| `./scripts/dev-reset-db.sh` | Wipes the database volume and restarts fresh |

## Quick Start

```zsh
cp .env.example .env        # first time only — add your OPENAI_API_KEY
./scripts/dev-start.sh
```

## Hot Reload (Dev)

`docker-compose.override.yml` is automatically picked up by Docker Compose in development. It mounts local service folders into containers so any file save triggers an instant reload — no rebuild needed.

For production, use only the base `docker-compose.yml`:
```zsh
docker compose -f docker-compose.yml up -d
```

## Service URLs

| Service | URL |
|---|---|
| AI Service | http://localhost:8001/health |
| Quote Service | http://localhost:8002/health |
| Booking Service | http://localhost:8003/health |
| Mock Providers | http://localhost:8004/health |
| PostgreSQL | localhost:5432 |

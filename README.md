# RidePilot AI

An AI-powered ride assistant that helps users search for rides, compare providers, monitor live prices, and book — built as a production-style microservices project. Users chat (typed or voice) with an AI agent that calls real backend services through a tool-calling loop; the UI streams replies token by token and shows a live route map, ride options, and ride status.

## Architecture

Five containers plus PostgreSQL, orchestrated with `docker-compose`.

| Service | Role | Port |
|---|---|---|
| **ai-service** | Central hub. Owns all frontend communication, the LLM tool-calling loop, chat state, and the SSE event stream. | 8001 |
| **quote-service** | Quote sessions, parallel provider fan-out, and background price monitoring. | 8002 |
| **booking-service** | Booking lifecycle: create, verify final price, confirm (idempotent), cancel, and ride-status tracking. | 8003 |
| **mock-providers** | Simulated Uber / Lyft / Waymo APIs (in-memory, fluctuating prices). | 8004 |
| **frontend** | Next.js chat UI with streaming, route map, and ride cards. | 3000 |
| **postgres** | Shared database (each service owns its own tables). | 5433 |

### Request flow (a typical turn)

```
Browser ──POST /api/chat/message──▶ ai-service
                                      │  loads chat context + injects a "ride state" block
                                      │  runs the LLM tool-calling loop
                                      ├─ tool: create_quote_session ─▶ quote-service ─▶ mock-providers
                                      ├─ tool: fetch_quotes          ─▶ quote-service ─▶ mock-providers
                                      ├─ tool: select_quote / create_booking / verify / confirm ─▶ booking-service ─▶ mock-providers
                                      ▼
Browser ◀──SSE token stream + events (quotes, route map, booking, ride status)── ai-service
```

The **quote-service** also runs a background monitor that re-fetches prices on an interval and pushes `QUOTE_DELTA` events to the ai-service, which relays them to the browser over SSE. The **booking-service** runs a background worker that polls ride status after confirmation.

Boundary rule: the LLM never touches a database or a provider directly. It can only request named backend tools; the ai-service executes them against the quote/booking services, which are the source of truth.

## Tech stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2 (async) + asyncpg, httpx, Pydantic v2, LiteLLM.
- **Frontend:** Next.js 14, React 18, TypeScript, Leaflet / react-leaflet (route map), Web Speech API (voice).
- **Infra:** Docker Compose, PostgreSQL 16.
- **LLM:** LiteLLM connects directly to either the Gemini API or a local Ollama model. A no-key deterministic stub mode is built in for a zero-config first run.

## Backend architecture (layered)

All four backend services follow a layered **Router → Service → Repository** pattern, so HTTP concerns, business logic, and data access stay separated:

```
booking-service/            # (quote-service and mock-providers mirror this layout)
├── main.py                 # app assembly: FastAPI, middleware, lifespan (DB init + workers)
├── api/                    # HTTP layer — thin routers + FastAPI dependency wiring
│   ├── booking_routes.py
│   └── dependencies.py
├── services/               # business logic (BookingService)
├── repositories/           # all database access (BookingRepository)
├── schemas/                # Pydantic request/response models
├── models/                 # SQLAlchemy ORM models
├── db/                     # Database class: engine, session factory, schema bootstrap
├── provider/               # outbound provider client (mock-providers uses a catalog/ instead)
├── workers/                # background workers (ride tracker / quote monitor)
└── core/                   # domain exceptions (mapped to HTTP status codes in the routes)
```

The **ai-service** follows the same layering, with a couple of extra folders for
its agent-specific concerns (it's a hub, not a plain CRUD service):

```
ai-service/
├── main.py                 # app, logging (incl. optional file trace), CORS, lifespan
├── api/                    # HTTP layer
│   ├── chat_routes.py      # chat endpoint (SSE stream) + stream/stop endpoints
│   ├── event_routes.py     # POST /internal/events consumer
│   └── dependencies.py     # session + service singletons wiring
├── services/               # ChatResponder (stub + seam), LLMService (live tool loop),
│   │                       # EventService, and pure notification-decision logic
│   ├── responder.py
│   ├── llm_service.py
│   ├── event_service.py
│   └── notifications.py
├── tools/                  # backend tool layer (calls quote/booking services) + tool schemas
├── repositories/           # ChatRepository — chat session/message/context access
├── schemas/                # chat + internal-event Pydantic models
├── models/                 # SQLAlchemy ORM models (chat sessions/messages)
├── db/                     # Database class
├── infra/                  # EventBus (SSE pub/sub) + StreamPublisher (typed push API)
├── core/                   # logging helpers
└── prompts/system_prompt.md
```

## Project structure

```
ridepilot-ai/
├── services/
│   ├── ai-service/
│   ├── quote-service/
│   ├── booking-service/
│   └── mock-providers/
├── frontend/                    # Next.js + TypeScript
├── scripts/                     # dev helpers (start/stop/reset)
├── docs/                        # architecture and design notes
├── docker-compose.yml           # base compose (production-style)
├── docker-compose.override.yml  # dev overrides: bind mounts + --reload
└── .env.example
```

## Prerequisites

- Docker + Docker Compose.
- (Optional, for a real local model) [Ollama](https://ollama.com) on the host with a tool-calling capable model pulled, e.g. `ollama pull qwen2.5:7b`.

## Quick start

```zsh
cp .env.example .env        # first time only
./scripts/dev-start.sh
```

Then open the UI at http://localhost:3000.

### Choosing how the AI runs

Configure the provider with environment variables only — no code changes:

- **No-key stub (zero config):** leave `GEMINI_API_KEY` empty. It is deterministic and makes no real model request.
- **Gemini API:**
  ```
  LLM_PROVIDER=gemini
  GEMINI_API_KEY=<your Gemini API key>
  GEMINI_MODEL=gemini/gemini-3.5-flash-lite
  ```
- **Local Ollama:**
  ```
  LLM_PROVIDER=ollama
  OLLAMA_BASE_URL=http://host.docker.internal:11434
  OLLAMA_MODEL=qwen2.5:7b
  ```
  Pull the model first (`ollama pull qwen2.5:7b`). Choose a model with tool-calling support.

Gemini models are allow-listed. An unsupported `GEMINI_MODEL` value falls back
to `gemini/gemini-3.5-flash-lite`. Gemini 3.5 uses `LLM_TEMPERATURE=1.0`;
keep that default unless you have a tested reason to change it.

## Scripts

| Script | What it does |
|---|---|
| `./scripts/dev-start.sh` | Builds images if needed and starts all containers (with dev bind mounts) |
| `./scripts/dev-stop.sh` | Stops and removes containers (preserves the database volume) |
| `./scripts/dev-reset-db.sh` | Wipes the Postgres volume and restarts fresh |

## Hot reload (dev)

`docker-compose.override.yml` is applied automatically in development. It bind-mounts each service's source into its container and runs `uvicorn --reload`, so saving a file reloads instantly — no rebuild needed. A rebuild is only required when a service's `requirements.txt` or `Dockerfile` changes.

For a production-style run (no mounts, no reload), use the base compose file only:

```zsh
docker compose -f docker-compose.yml up -d --build
```

## Service URLs

| Service | URL |
|---|---|
| Frontend | http://localhost:3000 |
| AI Service | http://localhost:8001/health |
| Quote Service | http://localhost:8002/health |
| Booking Service | http://localhost:8003/health |
| Mock Providers | http://localhost:8004/health |
| PostgreSQL | localhost:5433 |

## Key features

- **Streaming chat** — replies stream token by token over Server-Sent Events.
- **Tool-calling agent** — the model drives the flow by calling backend tools (search, fetch, select, book, verify, confirm, cancel); the backend owns all ids, prices, and coordinates.
- **Ride-state grounding** — each turn injects an authoritative "current ride state" summary (pickup/dropoff, status, available options) so the model doesn't re-ask or drift.
- **Live price monitoring** — the quote-service refreshes prices on an interval and pushes deltas to the UI.
- **Ride-status tracking** — after confirmation, the booking-service polls provider status and records milestones.
- **Route map** — the UI renders pickup/dropoff on a Leaflet map once a search starts.
- **Voice** — optional speech input/output in the browser.

## Configuration

All configuration is via `.env` (see `.env.example` for the full annotated list). Notable variables:

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | Chooses `gemini` or `ollama`. |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | Gemini credentials and approved model selection. |
| `OLLAMA_BASE_URL` / `OLLAMA_MODEL` | Local Ollama endpoint and model selection. |
| `LLM_MAX_TOKENS` / `LLM_TEMPERATURE` | Per-call output cap and sampling temperature. The service hard-caps output at 256 tokens. |
| `CHAT_CONTEXT_MESSAGE_LIMIT` | How many recent chat messages are replayed into each LLM turn. |
| `QUOTE_REFRESH_INTERVAL_SECONDS` | Price-monitoring cadence. |
| `RIDE_STATUS_POLL_INTERVAL_SECONDS` | Ride-status polling cadence. |
| `TEST_PICKUP_/DROPOFF_LAT/LNG` | Stand-in coordinates (no geocoding yet). |
| `CORS_ALLOW_ORIGINS` | Browser origins allowed to call the AI Service. |
| `LOG_LEVEL` | AI Service log verbosity. |

## Observability

The AI Service logs each turn — the model input, every tool/API call and its result, and the final reply. Set `TRACE_TO_FILE=true` to also tee those logs to a rotating file (default `services/ai-service/logs/ai-service.log`, bind-mounted to the host in dev) for a persistent, greppable trace. The trace contains full prompts and conversation history, so treat it as a debug artifact.

```zsh
tail -f services/ai-service/logs/ai-service.log
```

## Documentation

See `docs/` for the architecture and design notes.

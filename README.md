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

## Quick Start

```bash
docker-compose up
```

Frontend: http://localhost:3000  
AI Service: http://localhost:8001  
Quote Service: http://localhost:8002  
Booking Service: http://localhost:8003  
Mock Providers: http://localhost:8004  

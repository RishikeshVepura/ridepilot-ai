"""Application entrypoint for the Quote Service.

Wires the layered app together: creates the FastAPI instance, sets up request
logging, bootstraps the database and starts the quote-monitor background worker
in the lifespan, and mounts the quotes router. Business logic lives in the
service layer (services.quote_service); this module only assembles the pieces.
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from api.quote_routes import router as quotes_router
from db.database import database
from workers.quote_monitor import QuoteMonitor

# Logging verbosity. INFO logs every incoming request (method, path, status,
# latency), the monitoring worker's activity, and outbound provider calls. Set
# LOG_LEVEL=DEBUG for more.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logging.getLogger("quote-service").setLevel(LOG_LEVEL)

logger = logging.getLogger("quote-service.http")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create quote_sessions, quotes, and quote_events tables if they don't exist.
    await database.init_db()

    # Start the quote monitoring worker as a background asyncio task. It refreshes
    # MONITORING sessions on a configurable interval and publishes QUOTE_DELTA
    # events to the AI Service. The stop event lets us shut it down cleanly.
    stop_event = asyncio.Event()
    monitor = QuoteMonitor(database.session_factory)
    monitor_task = asyncio.create_task(monitor.run_loop(stop_event))

    try:
        yield
    finally:
        # Signal the loop to stop and wait for it to finish its current cycle.
        stop_event.set()
        await monitor_task


app = FastAPI(title="Quote Service", lifespan=lifespan)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log each incoming request and its response (status + latency).

    Health checks are skipped to keep the log focused on real API traffic.
    """
    if request.url.path == "/health":
        return await call_next(request)

    start = time.perf_counter()
    logger.info("→ %s %s", request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.exception(
            "✗ %s %s — unhandled error after %.1f ms",
            request.method,
            request.url.path,
            elapsed_ms,
        )
        raise
    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "← %s %s %s (%.1f ms)",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


app.include_router(quotes_router)


@app.get("/health")
def health():
    return {"service": "quote-service", "status": "ok"}

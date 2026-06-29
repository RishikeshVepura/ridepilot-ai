import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from db import init_db
from ride_tracker import ride_tracker_loop
from routes import router as bookings_router

# Logging verbosity. INFO logs every incoming request (method, path, status,
# latency), the ride-tracker worker's activity, and outbound provider calls. Set
# LOG_LEVEL=DEBUG for more. Configured here at import so the whole service —
# including the background worker — emits at the chosen level.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logging.getLogger("booking-service").setLevel(LOG_LEVEL)

logger = logging.getLogger("booking-service.http")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create bookings and booking_events tables if they don't exist.
    await init_db()

    # Start the ride status tracking worker as a background asyncio task. It polls
    # the provider for confirmed/active bookings on a configurable interval and
    # records ride milestones as booking_events. The stop event lets us shut it
    # down cleanly.
    stop_event = asyncio.Event()
    tracker_task = asyncio.create_task(ride_tracker_loop(stop_event))

    try:
        yield
    finally:
        # Signal the loop to stop and wait for it to finish its current cycle.
        stop_event.set()
        await tracker_task


app = FastAPI(title="Booking Service", lifespan=lifespan)


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


app.include_router(bookings_router)


@app.get("/health")
def health():
    return {"service": "booking-service", "status": "ok"}

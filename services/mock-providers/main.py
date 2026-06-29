import logging
import os
import time

from fastapi import FastAPI, Request

from routes import router

# Logging verbosity. INFO logs every incoming provider call (quotes, bookings,
# status, cancel) with its response status and latency — useful for seeing what
# the Quote and Booking services are asking the providers for.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logging.getLogger("mock-providers").setLevel(LOG_LEVEL)

logger = logging.getLogger("mock-providers.http")

app = FastAPI(title="Mock Providers")


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


app.include_router(router)


@app.get("/health")
def health():
    return {"service": "mock-providers", "status": "ok"}

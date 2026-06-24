import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from db import init_db
from monitor import monitor_loop
from routes import router as quotes_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create quote_sessions, quotes, and quote_events tables if they don't exist.
    await init_db()

    # Start the quote monitoring worker as a background asyncio task. It refreshes
    # MONITORING sessions on a configurable interval and publishes QUOTE_DELTA
    # events to the AI Service. The stop event lets us shut it down cleanly.
    stop_event = asyncio.Event()
    monitor_task = asyncio.create_task(monitor_loop(stop_event))

    try:
        yield
    finally:
        # Signal the loop to stop and wait for it to finish its current cycle.
        stop_event.set()
        await monitor_task


app = FastAPI(title="Quote Service", lifespan=lifespan)

app.include_router(quotes_router)


@app.get("/health")
def health():
    return {"service": "quote-service", "status": "ok"}

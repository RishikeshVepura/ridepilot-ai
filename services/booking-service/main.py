import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from db import init_db
from ride_tracker import ride_tracker_loop
from routes import router as bookings_router


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

app.include_router(bookings_router)


@app.get("/health")
def health():
    return {"service": "booking-service", "status": "ok"}

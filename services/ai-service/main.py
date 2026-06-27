from contextlib import asynccontextmanager

from fastapi import FastAPI

from db import init_db
from events import router as internal_events_router
from routes import router as chat_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create chat_sessions and chat_messages tables if they don't exist.
    await init_db()
    yield


app = FastAPI(title="AI Service", lifespan=lifespan)

app.include_router(chat_router)
app.include_router(internal_events_router)


@app.get("/health")
def health():
    return {"service": "ai-service", "status": "ok"}

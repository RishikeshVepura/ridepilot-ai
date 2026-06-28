import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from db import init_db
from events import router as internal_events_router
from routes import router as chat_router

# Origins allowed to call the AI Service from the browser. The frontend is the
# only browser client (design rule 1) and runs on http://localhost:3000 in dev.
# Override with a comma-separated list via CORS_ALLOW_ORIGINS for other envs.
_default_origins = "http://localhost:3000,http://127.0.0.1:3000"
CORS_ALLOW_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOW_ORIGINS", _default_origins).split(",")
    if origin.strip()
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create chat_sessions and chat_messages tables if they don't exist.
    await init_db()
    yield


app = FastAPI(title="AI Service", lifespan=lifespan)

# Allow the browser frontend to call the chat + SSE endpoints cross-origin.
# Without this the browser blocks requests from :3000 to :8001 (CORS error).
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router)
app.include_router(internal_events_router)


@app.get("/health")
def health():
    return {"service": "ai-service", "status": "ok"}

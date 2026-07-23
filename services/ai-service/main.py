"""Application entrypoint for the AI Service.

Assembles the layered app: configures logging (including the optional file
trace), creates the FastAPI instance, bootstraps the database in the lifespan,
sets up CORS, and mounts the chat and internal-event routers. Business logic
lives in the service layer (services.*); this module only wires the pieces.
"""

import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.chat_routes import router as chat_router
from api.event_routes import router as internal_events_router
from db.database import database
from services.llm_service import llm_status

# Logging verbosity for the service. INFO shows the per-turn assistant activity
# (tool/API calls, responses, final replies); set LOG_LEVEL=DEBUG for more.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s | %(message)s"
logging.basicConfig(
    level=LOG_LEVEL,
    format=_LOG_FORMAT,
)
# Ensure our own loggers honor the configured level even if a parent (e.g.
# uvicorn) already installed handlers on import.
logging.getLogger("ai-service").setLevel(LOG_LEVEL)


def _env_flag(name: str, default: bool = False) -> bool:
    """Parse a boolean-ish environment variable.

    Treats 1/true/yes/on (case-insensitive) as True; anything else as False.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --- Optional file logging (feature-flagged) --------------------------------
# When TRACE_TO_FILE is enabled, tee the same log lines that go to stdout into a
# rotating file. This gives a persistent, greppable trace of each turn — the
# model input, the tools/APIs called, and the final reply. The trace contains
# full prompts and conversation history, so it is a debug artifact.
TRACE_TO_FILE = _env_flag("TRACE_TO_FILE", True)
# Path for the trace file. Relative paths resolve against this service's dir, so
# the default lands in services/ai-service/logs/, bind-mounted to the host in dev.
TRACE_LOG_PATH = os.getenv("TRACE_LOG_PATH", "logs/ai-service.log")
# Rotation bounds so the file can't grow unbounded.
TRACE_LOG_MAX_BYTES = int(os.getenv("TRACE_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
TRACE_LOG_BACKUP_COUNT = int(os.getenv("TRACE_LOG_BACKUP_COUNT", "3"))


def _configure_file_logging() -> None:
    """Attach a rotating file handler to the root logger when TRACE_TO_FILE is on.

    No-op when the flag is off. The handler mirrors the stdout format and level
    and is added to the root logger, so every propagating logger is captured in
    one file. A failure to open the file is logged and swallowed so logging setup
    never blocks startup.
    """
    if not TRACE_TO_FILE:
        return

    path = Path(TRACE_LOG_PATH)
    if not path.is_absolute():
        path = Path(__file__).parent / path

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=TRACE_LOG_MAX_BYTES,
            backupCount=TRACE_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setLevel(LOG_LEVEL)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        logging.getLogger().addHandler(handler)
        logging.getLogger("ai-service").info(
            "File logging enabled (TRACE_TO_FILE) → %s", path
        )
    except OSError as exc:
        logging.getLogger("ai-service").warning(
            "Could not enable file logging at %s: %s", path, exc
        )


_configure_file_logging()

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
    await database.init_db()
    yield


app = FastAPI(title="AI Service", lifespan=lifespan)

# Allow the browser frontend to call the chat + SSE endpoints cross-origin.
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


@app.get("/api/llm-status")
def llm_status_endpoint():
    """Report whether the AI Service is using a live LLM or the stub.

    Confirms mode (live/stub), the model and endpoint in use, and whether an API
    key is set — without exposing the key.
    """
    return llm_status()

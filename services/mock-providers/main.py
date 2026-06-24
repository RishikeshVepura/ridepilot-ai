from fastapi import FastAPI

from routes import router

app = FastAPI(title="Mock Providers")

app.include_router(router)


@app.get("/health")
def health():
    return {"service": "mock-providers", "status": "ok"}

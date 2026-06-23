from fastapi import FastAPI

app = FastAPI(title="Mock Providers")


@app.get("/health")
def health():
    return {"service": "mock-providers", "status": "ok"}

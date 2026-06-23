from fastapi import FastAPI

app = FastAPI(title="Quote Service")


@app.get("/health")
def health():
    return {"service": "quote-service", "status": "ok"}

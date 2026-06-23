from fastapi import FastAPI

app = FastAPI(title="Booking Service")


@app.get("/health")
def health():
    return {"service": "booking-service", "status": "ok"}

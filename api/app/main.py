from fastapi import FastAPI

from app.logging import get_logger

SERVICE_NAME = "agent-api"

app = FastAPI(title=SERVICE_NAME)
log = get_logger(SERVICE_NAME)


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME}

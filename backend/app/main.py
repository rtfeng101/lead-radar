from fastapi import FastAPI

from app.api import leads, pipeline

app = FastAPI(title="Leadgen API")
app.include_router(leads.router)
app.include_router(pipeline.router)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}

"""Eclipse local server. FastAPI, localhost:8000, bind 127.0.0.1 only.

Run from the repo root:
    .venv/bin/uvicorn server.main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import audit_api, memory_api, system, tasks_api
from .core import config, db


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.migrate()
    config.get_token()  # generate + persist; never logged
    yield


app = FastAPI(
    title="Eclipse",
    version="0.1.0",
    lifespan=lifespan,
    # No unauthenticated docs: the plan says every request carries
    # X-Eclipse-Token, and /docs, /redoc and /openapi.json are routes.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    # Sprint 3 adds task routes (POST) and the plan's memory
    # forget routes (PATCH/DELETE); widen from GET/POST.
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["X-Eclipse-Token", "Content-Type"],
    allow_credentials=False,
)

app.include_router(system.router)
app.include_router(audit_api.router)
app.include_router(memory_api.router)
app.include_router(tasks_api.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)

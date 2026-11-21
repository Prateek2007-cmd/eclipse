"""Eclipse local server. FastAPI, localhost:8000, bind 127.0.0.1 only.

Run from the repo root:
    .venv/bin/uvicorn server.main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import audit_api, connectors_api, jobs_api, mail_api, memory_api, monitor_api, proof_api, rag_api, system, tasks_api, vault_api
from .core import config, db

logger = logging.getLogger("eclipse.main")


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
app.include_router(jobs_api.router)
app.include_router(vault_api.router)
app.include_router(mail_api.router)
app.include_router(rag_api.router)
app.include_router(proof_api.router)
app.include_router(connectors_api.router)
app.include_router(monitor_api.router)

# Sprint 9: the workspace (Vite + React build) is served from the
# local server at /workspace. No token is needed to load the page
# itself — it holds no data; every data call is token-gated.
# The mount is registered after the API routers so it cannot
# shadow an API route.
_WORKSPACE_DIR = Path(__file__).resolve().parents[1] / "web" / "workspace" / "dist"
if _WORKSPACE_DIR.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/workspace", StaticFiles(directory=str(_WORKSPACE_DIR), html=True), name="workspace")
else:
    # Sprint 9 fix (review M9): a missing dist/ used to fail
    # silently — /workspace just 404'd. Log it so the cause is
    # visible in the server log.
    logger.warning(
        "workspace dist/ not found at %s — /workspace is not mounted;"
        " run `npm run build` in web/workspace",
        _WORKSPACE_DIR,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)

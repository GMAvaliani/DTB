"""FastAPI application factory."""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import dtb
from dtb.api.deps import AppContext
from dtb.api.routes_interop import router as interop_router
from dtb.api.routes_model import router as model_router
from dtb.api.routes_sim import router as sim_router
from dtb.api.security import auth_enabled
from dtb.persistence import Database

logger = logging.getLogger("dtb.api")


def _frontend_dir() -> str | None:
    override = os.environ.get("DTB_FRONTEND_DIR")
    if override and os.path.isdir(override):
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (
        os.path.join(here, "..", "..", "..", "frontend"),  # repo layout
        os.path.join(here, "..", "frontend"),  # installed package layout
        os.path.join(os.getcwd(), "frontend"),  # docker / run-from-checkout
    ):
        candidate = os.path.normpath(candidate)
        if os.path.isfile(os.path.join(candidate, "index.html")):
            return candidate
    return None


def create_app(db: Database | None = None) -> FastAPI:
    app = FastAPI(
        title="DTB — Digital Twin of Business",
        version=dtb.__version__,
        description="Платформа цифрового двойника деятельности предприятия: "
        "СЭДы-агенты, полезности, НП-события, event-driven continuous-time "
        "симуляция, Монте-Карло, прогноз, оптимизация, knowledge graph.",
    )
    app.state.ctx = AppContext(db or Database())
    if not auth_enabled():
        logger.warning(
            "DTB_API_KEYS is not set — running in OPEN development mode (no auth)"
        )

    app.include_router(model_router, prefix="/api", tags=["model"])
    app.include_router(sim_router, prefix="/api", tags=["simulation"])
    app.include_router(interop_router, prefix="/api", tags=["interop"])

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "version": dtb.__version__,
            "engine_version": dtb.ENGINE_VERSION,
            "auth": "enabled" if auth_enabled() else "open-dev-mode",
        }

    frontend = _frontend_dir()
    if frontend:
        app.mount("/static", StaticFiles(directory=frontend), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(os.path.join(frontend, "index.html"))

    return app

"""FastAPI application factory for Spark Pulse."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from spark_pulse.config import config
from spark_pulse.routers import recipes, deployments, memory, cache, settings, mods, config as config_router
from spark_pulse.auth import AuthMiddleware, router as auth_router
from spark_pulse.sse import router as sse_router
from spark_pulse.tools import is_simulation
from spark_pulse.version import get_version
from spark_pulse.mcp_http import handle_mcp, MCP_PATH


# ── SPA serving ──────────────────────────────────────────────────────────────

_UI_DIR = Path(__file__).resolve().parent / "ui"
_INDEX_FILE = _UI_DIR / "index.html"


def _get_ui_dir() -> Path:
    if not _UI_DIR.is_dir():
        raise RuntimeError(f"UI directory not found at {_UI_DIR}. "
                           "Build the frontend first: cd web && npm install && npm run build")
    return _UI_DIR


def _serve_spa(filename: str | None = None) -> FileResponse:
    """Serve static file or fall back to index.html for SPA routing."""
    ui_dir = _get_ui_dir()
    if filename:
        safe_root = str(ui_dir) + os.sep
        candidate = os.path.normpath(os.path.join(str(ui_dir), filename))
        if not candidate.startswith(safe_root):
            return FileResponse(str(_INDEX_FILE))
        if os.path.isfile(candidate):
            return FileResponse(candidate)
    return FileResponse(str(_INDEX_FILE))


# ── App factory ─────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: validate spark-vllm-docker path, log mode. Shutdown: cleanup."""
    spark_path = Path(config.spark_vllm_path)
    app.state.spark_path_valid = spark_path.is_dir()

    mode = "SIMULATION" if is_simulation() else "PRODUCTION"
    print(f"Spark Pulse starting in {mode} mode "
          f"(spark-vllm-docker: {config.spark_vllm_path})")

    yield


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Spark Pulse",
        description="Web UI for spark-vllm-docker",
        version=get_version(),
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Auth middleware (only active when auth is configured)
    app.add_middleware(AuthMiddleware)

    # API routes
    app.include_router(recipes.router)
    app.include_router(deployments.router)
    app.include_router(memory.router)
    app.include_router(cache.router)
    app.include_router(settings.router)
    app.include_router(mods.router)
    app.include_router(config_router.router)
    app.include_router(auth_router)
    app.include_router(sse_router)

    # Health check
    @app.get("/health")
    def health_check():
        return {"status": "ok", "spark_vllm_path": config.spark_vllm_path}

    # Version
    @app.get("/version")
    def version():
        return {"version": get_version()}

    # MCP JSON-RPC endpoint (same port, inherits auth middleware)
    if config.mcp_enabled:
        @app.post(f"/{MCP_PATH}")
        async def mcp_endpoint(request: Request):
            try:
                body = await request.json()
            except Exception:
                return JSONResponse(
                    {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None},
                    status_code=400,
                )
            result = await handle_mcp(body)
            return JSONResponse(result)

        print(f"MCP server enabled at /{MCP_PATH}")

    # SPA catch-all
    @app.api_route("/{filename:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def serve_static(request: Request, filename: str):
        return _serve_spa(filename)

    return app


app = create_app()

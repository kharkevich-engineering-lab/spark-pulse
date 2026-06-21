"""FastAPI application factory for Spark Pulse."""

import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from spark_pulse.config import config
from spark_pulse.routers import (
    recipes,
    deployments,
    memory,
    cache,
    settings,
    mods,
    benchmarking,
    config as config_router,
    git_update as git_update_router,
    custom_recipes as custom_recipes_router,
    custom_files as custom_files_router,
    oci as oci_router,
    docker as docker_router,
    discovery as discovery_router,
    cluster as cluster_router,
)
from spark_pulse.auth import AuthMiddleware, router as auth_router
from spark_pulse.sse import router as sse_router
from spark_pulse.tools import is_simulation
from spark_pulse.tools.git_update import check_updates
from spark_pulse.tools.oci_registry import (
    start_background_updater,
    stop_background_updater,
)
from spark_pulse.version import get_version
from spark_pulse.mcp_http import handle_mcp, MCP_PATH

# ── Background git update scheduler ──────────────────────────────────────────

_git_update_task: threading.Timer | None = None
_git_update_running = False


def _git_update_loop():
    """Periodic loop that checks for git updates and emits events via SSE."""
    global _git_update_task, _git_update_running

    # Timer callbacks should do one check, then schedule the next callback.
    # A while-loop here can enqueue unbounded timers.
    if not _git_update_running:
        return

    try:
        result = check_updates(config.spark_vllm_path)
        if result.get("available"):
            print(
                f"Git update available: {result.get('local_version')} -> {result.get('remote_version')}"
            )
            # SSE broadcast happens through the SSE endpoint which clients poll
            # at their configured interval
    except Exception as e:
        print(f"Git update check failed: {e}")

    if not _git_update_running:
        return

    interval = config.git_update_check_interval_seconds
    _git_update_task = threading.Timer(interval, _git_update_loop)
    _git_update_task.daemon = True
    _git_update_task.start()


def _start_git_update_scheduler():
    """Start the periodic git update check if enabled."""
    global _git_update_task, _git_update_running

    if not config.git_update_enabled:
        return

    _git_update_running = True
    # Start the first check
    _git_update_task = threading.Timer(60, _git_update_loop)  # First check after 1 min
    _git_update_task.daemon = True
    _git_update_task.start()
    print("Git update scheduler started")


def _stop_git_update_scheduler():
    """Stop the periodic git update check."""
    global _git_update_task, _git_update_running

    _git_update_running = False
    if _git_update_task is not None:
        _git_update_task.cancel()
        _git_update_task = None
    print("Git update scheduler stopped")


# ── SPA serving ──────────────────────────────────────────────────────────────

_UI_DIR = Path(__file__).resolve().parent / "ui"
_INDEX_FILE = _UI_DIR / "index.html"


def _get_ui_dir() -> Path:
    if not _UI_DIR.is_dir():
        raise RuntimeError(
            f"UI directory not found at {_UI_DIR}. "
            "Build the frontend first: cd web && npm install && npm run build"
        )
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
    """Startup: validate spark-vllm-docker path, log mode, create symlinks, start scheduler.
    Shutdown: remove symlinks, cleanup."""
    spark_path = Path(config.spark_vllm_path)
    app.state.spark_path_valid = spark_path.is_dir()

    mode = "SIMULATION" if is_simulation() else "PRODUCTION"
    print(
        f"Spark Pulse starting in {mode} mode "
        f"(spark-vllm-docker: {config.spark_vllm_path})"
    )

    # Create symlinks for custom recipes and mods
    try:
        created = custom_files_router.create_symlinks(config.spark_vllm_path)
        n_recipes = len(created.get("recipes", []))
        n_mods = len(created.get("mods", []))
        if n_recipes or n_mods:
            print(f"Symlinks created: {n_recipes} custom recipes, {n_mods} custom mods")
    except Exception as e:
        print(f"Warning: could not create symlinks for custom files: {e}")

    # Start background git update scheduler
    _start_git_update_scheduler()

    # Start OCI background update checker
    start_background_updater()

    yield

    # Cleanup on shutdown
    _stop_git_update_scheduler()
    stop_background_updater()

    # Remove symlinks for custom recipes and mods
    try:
        removed = custom_files_router.remove_symlinks(config.spark_vllm_path)
        n_recipes = removed.get("recipes", 0)
        n_mods = removed.get("mods", 0)
        if n_recipes or n_mods:
            print(f"Symlinks removed: {n_recipes} custom recipes, {n_mods} custom mods")
    except Exception as e:
        print(f"Warning: could not remove symlinks for custom files: {e}")


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
    app.include_router(custom_files_router.router)
    app.include_router(custom_recipes_router.router)
    app.include_router(recipes.router)
    app.include_router(deployments.router)
    app.include_router(memory.router)
    app.include_router(cache.router)
    app.include_router(settings.router)
    app.include_router(mods.router)
    app.include_router(config_router.router)
    app.include_router(git_update_router.router)
    app.include_router(benchmarking.router)
    app.include_router(oci_router.router)
    app.include_router(docker_router.router)
    app.include_router(discovery_router.router)
    app.include_router(cluster_router.router)
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
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -32700, "message": "Parse error"},
                        "id": None,
                    },
                    status_code=400,
                )
            result = await handle_mcp(body)
            return JSONResponse(result)

        print(f"MCP server enabled at /{MCP_PATH}")

    # SPA catch-all
    @app.api_route(
        "/{filename:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"]
    )
    async def serve_static(request: Request, filename: str):
        return _serve_spa(filename)

    return app


app = create_app()

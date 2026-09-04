"""FastAPI application factory for Spark Pulse."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from spark_pulse.config import config
from spark_pulse.routers import (
    recipes,
    recipe_import as recipe_import_router,
    deployments,
    memory,
    cache,
    models as models_router,
    images as images_router,
    settings,
    mods,
    benchmarking,
    config as config_router,
    custom_recipes as custom_recipes_router,
    custom_files as custom_files_router,
    oci as oci_router,
    docker as docker_router,
    discovery as discovery_router,
    nodes as nodes_router,
    launch_script as launch_script_router,
    health as health_router,
    engines as engines_router,
    preflight as preflight_router,
)
from spark_pulse.auth import AuthMiddleware, router as auth_router
from spark_pulse.sse import router as sse_router
from spark_pulse import tools
from spark_pulse.tools import is_simulation
from spark_pulse.tools.atomic_json import StateFileError
from spark_pulse.tools.oci_registry import (
    start_background_updater,
    stop_background_updater,
)
from spark_pulse.tools.reconciliation import reconcile_all
from spark_pulse.tools.health import load_health_tracking, HealthMonitor
from spark_pulse.version import get_version
from spark_pulse.mcp_http import handle_mcp, MCP_PATH

# ── Health monitor singleton ─────────────────────────────────────────────────

_health_monitor: HealthMonitor | None = None


def _get_health_monitor() -> HealthMonitor:
    """Get or create the default health monitor."""
    global _health_monitor
    if _health_monitor is None:
        _health_monitor = HealthMonitor(check_interval=30.0)
    return _health_monitor


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


async def configure_thread_pool() -> int:
    """Pin the worker-thread ceiling and return it.

    Every sync endpoint and every ``run_in_threadpool`` call shares one AnyIO
    limiter whose default is 40. Nothing anywhere says so, so exhaustion — a
    handful of blocking Docker or SSH calls is enough — presents as the whole
    API going quiet with no clue why. Setting it from config and logging the
    number makes the ceiling a stated fact.

    The limiter lives in a run-scoped variable, so this only takes effect when
    called from inside the running event loop.
    """
    import anyio.to_thread

    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = config.thread_pool_size
    return int(limiter.total_tokens)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: validate spark-vllm-docker path, log mode, create symlinks, start scheduler.
    Shutdown: remove symlinks, cleanup."""
    spark_path = Path(config.spark_vllm_path)
    app.state.spark_path_valid = spark_path.is_dir()

    try:
        threads = await configure_thread_pool()
        print(f"Worker thread pool: {threads} threads")
    except Exception as e:  # pragma: no cover - defensive
        print(f"Warning: could not set the worker thread pool size: {e}")

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

    # Refuse to start on an unreadable state file. An unreadable state file is
    # not an empty cluster: coming up with an empty view while containers are
    # still running is what lets a control plane tear down live work.
    try:
        tools.deployments.check_state_file()
    except StateFileError as e:
        print(f"FATAL: cannot read deployment state file {e.path}: {e.reason}")
        if e.quarantine_path is not None:
            print(
                f"FATAL: the unreadable file was moved aside to {e.quarantine_path}. "
                "Inspect or restore it, then restart Spark Pulse."
            )
        print("FATAL: refusing to start with an empty view of running deployments.")
        raise

    # Put this machine in the node registry, then say so on the LAN.
    #
    # `register_self` is idempotent and fills blanks only, so a restart never
    # overwrites an address or an interface name an operator corrected by hand.
    # The mDNS announcement carries the *minted* node id — not the hostname and
    # not the machine-id, which DGX Sparks duplicate — so a peer that finds us
    # knows which control plane it found. Both steps are best effort: a control
    # plane that cannot announce itself still runs, and its peers can still be
    # added by address.
    try:
        this_node = tools.node_registry.register_self()
        print(f"Control node registered: {this_node.label} ({this_node.id[:8]})")
        announced = tools.discovery.announce_self(
            this_node.id, config.webui_port, get_version()
        )
        if announced:
            print(
                "Announced _spark-pulse._tcp on "
                f"{', '.join(tools.discovery.real_interface_names()) or 'no interface'}"
            )
    except Exception as e:
        print(f"Warning: could not register this node: {e}")

    # Start OCI background update checker
    start_background_updater()

    # Run startup reconciliation to recover deployment state from container
    # labels. It still counts clusters because a host upgraded from an older
    # build can be running containers the deleted orchestrator labelled, and
    # those are exactly what the orphan sweep has to find.
    try:
        result = reconcile_all()
        print(
            f"Reconciliation complete: {result.clusters_reconciled} clusters, "
            f"{result.deployments_reconciled} deployments, "
            f"{result.orphaned_containers_cleaned} orphans cleaned"
        )
        if result.errors:
            for err in result.errors:
                print(f"Reconciliation warning: {err}")
    except Exception as e:
        print(f"Warning: reconciliation failed: {e}")

    # Restore health monitoring tracking from disk
    try:
        monitor = _get_health_monitor()
        tracked = load_health_tracking()
        for dep in tracked.get("deployments", []):
            monitor.track_deployment(dep["id"], dep.get("info", {}))
        n_deps = len(tracked.get("deployments", []))
        if n_deps:
            print(f"Restored {n_deps} deployments from health tracking")
    except Exception as e:
        print(f"Warning: health tracking restore failed: {e}")

    yield

    # Cleanup on shutdown
    stop_background_updater()

    # Withdraw the mDNS record rather than letting it time out, so a peer
    # browsing right after a restart does not see a node that is not there.
    try:
        tools.discovery.stop_announcement()
    except Exception as e:  # pragma: no cover — shutdown is best effort
        print(f"Warning: could not withdraw the mDNS record: {e}")

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
    app.include_router(recipe_import_router.router)
    app.include_router(recipes.router)
    app.include_router(deployments.router)
    app.include_router(memory.router)
    app.include_router(cache.router)
    app.include_router(models_router.router)
    app.include_router(images_router.router)
    app.include_router(settings.router)
    app.include_router(mods.router)
    app.include_router(config_router.router)
    app.include_router(benchmarking.router)
    app.include_router(oci_router.router)
    app.include_router(docker_router.router)
    app.include_router(discovery_router.router)
    app.include_router(nodes_router.router)
    app.include_router(launch_script_router.router)
    app.include_router(health_router.router)
    app.include_router(engines_router.router)
    app.include_router(preflight_router.router)
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

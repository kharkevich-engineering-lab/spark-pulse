"""Benchmarking API endpoints."""

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, field_validator

from spark_pulse import tools

router = APIRouter(prefix="/api/benchmarks", tags=["benchmarking"])


class RunBenchmarkRequest(BaseModel):
    deployment_id: str
    baseline_id: str | None = None
    recipe_id: str = ""
    recipe_name: str = ""
    params: dict = {}

    @field_validator("params", mode="before")
    @classmethod
    def validate_params(cls, values):
        """Validate and sanitize benchmark params before execution."""
        if not isinstance(values, dict):
            values = {}
        params = values.get("params", values)
        if params:
            port = params.get("port")
            if port is not None:
                try:
                    port = int(port)
                except (ValueError, TypeError):
                    raise ValueError(f"Invalid port value: {port}")
                allowed_ports = {
                    8000,
                    8001,
                    8002,
                    8003,
                    8004,
                    8005,
                    8006,
                    8007,
                    8008,
                    8009,
                    8010,
                }
                if port not in allowed_ports:
                    raise ValueError(
                        f"Port {port} not allowed. Allowed ports: {sorted(allowed_ports)}"
                    )
                values["params"] = dict(params)
                values["params"]["port"] = port
        return values


class CompareRunsRequest(BaseModel):
    run_ids: list[str]


@router.get("")
def list_benchmarks():
    """Return all benchmarks sorted by date descending."""
    return tools.benchmarking.list_benchmarks()


@router.get("/latest-by-recipe")
def get_latest_by_recipe():
    """Return the latest completed benchmark for each recipe."""
    return tools.benchmarking.get_latest_by_recipe()


@router.get("/recipe/{recipe_id}")
def get_recipe_benchmarks(recipe_id: str):
    """Return all benchmarks for a specific recipe."""
    return tools.benchmarking.get_benchmarks_for_recipe(recipe_id)


@router.get("/recipe/{recipe_id}/latest")
def get_recipe_latest(recipe_id: str):
    """Return the latest completed benchmark for a recipe."""
    result = tools.benchmarking.get_recipe_latest(recipe_id)
    if result is None:
        raise HTTPException(status_code=404, detail="No benchmark data for this recipe")
    return result


@router.get("/{benchmark_id}")
def get_benchmark(benchmark_id: str):
    """Return a single benchmark by ID."""
    result = tools.benchmarking.get_benchmark(benchmark_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Benchmark not found")
    return result


@router.post("/compare")
def compare_runs(req: CompareRunsRequest):
    """Compare multiple benchmark runs against each other."""
    if len(req.run_ids) < 2:
        raise HTTPException(
            status_code=400,
            detail="At least 2 benchmark IDs required for comparison",
        )
    result = tools.benchmarking.compare_runs(req.run_ids)
    if result is None:
        raise HTTPException(status_code=404, detail="One or more benchmarks not found")
    return result


@router.post("")
def run_benchmark(req: RunBenchmarkRequest, background_tasks: BackgroundTasks):
    """Start a new benchmark. Returns immediately with status='running'."""
    record = tools.benchmarking.create_benchmark(
        deployment_id=req.deployment_id,
        baseline_id=req.baseline_id,
        params=req.params,
        recipe_id=req.recipe_id,
        recipe_name=req.recipe_name,
    )
    background_tasks.add_task(
        tools.benchmarking.execute_benchmark, record["benchmark_id"]
    )
    return record

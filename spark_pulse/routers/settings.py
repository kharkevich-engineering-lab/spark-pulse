"""Settings API."""

from fastapi import APIRouter

from spark_pulse.config import config

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
def get_settings():
    return {
        "spark_vllm_path": config.spark_vllm_path,
        "default_container": config.default_container,
        "default_gpu_mem_util": config.default_gpu_mem_util,
        "default_port_range_start": config.default_port_range_start,
        "default_port_range_end": config.default_port_range_end,
        "webui_port": config.webui_port,
    }


@router.put("")
def update_settings(req: dict):
    config.update(**{k: v for k, v in req.items() if v is not None})
    return {
        "spark_vllm_path": config.spark_vllm_path,
        "default_container": config.default_container,
        "default_gpu_mem_util": config.default_gpu_mem_util,
        "default_port_range_start": config.default_port_range_start,
        "default_port_range_end": config.default_port_range_end,
        "webui_port": config.webui_port,
    }

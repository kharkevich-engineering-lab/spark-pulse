"""Memory monitoring API — GPU, CPU, disk stats."""

from fastapi import APIRouter

from spark_pulse import tools

router = APIRouter(prefix="/api/memory", tags=["memory"])


@router.get("/gpu")
def get_gpu_stats():
    return {"gpus": tools.system.get_gpu_stats()}


@router.get("/cpu")
def get_cpu_stats():
    return tools.system.get_cpu_stats()


@router.get("/disk")
def get_disk_stats():
    return {"disks": tools.system.get_disk_stats()}


@router.get("")
def get_all_memory():
    return tools.system.get_all_memory()

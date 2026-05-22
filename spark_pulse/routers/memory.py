"""Memory monitoring API — GPU, CPU, disk stats."""

import os
import signal

from fastapi import APIRouter, HTTPException

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
    from spark_pulse.tools.deployments import list_deployments
    data = tools.system.get_all_memory()
    # Collect PIDs of tracked running/pending deployments
    running_pids: set[int] = {
        dep["pid"]
        for dep in list_deployments()
        if dep.get("pid") and dep.get("status") in ("running", "pending")
    }
    for proc in data.get("processes", []):
        proc["is_tracked"] = proc["pid"] in running_pids
    return data


@router.delete("/processes/{pid}")
def kill_gpu_process(pid: int):
    result = tools.system.kill_gpu_process(pid)
    if not result.get("killed") and result.get("error") == "Process not found":
        raise HTTPException(status_code=404, detail=f"Process {pid} not found")
    if not result.get("killed") and result.get("error") == "Permission denied":
        raise HTTPException(status_code=403, detail=f"Permission denied to kill process {pid}")
    return result

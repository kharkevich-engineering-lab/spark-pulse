"""Monitoring API — what every node is doing, not what this one is doing.

The three sub-endpoints answer for the control node, because that is what they
have always meant and something still asks them that way. ``GET /api/memory``
answers for every node: the control node's block stays at the top level so
every existing reader keeps working, and ``nodes`` carries the cluster.

Nothing here reads hardware. Each node is asked through its own agent —
including this one — so the page does not depend on which machine the control
plane happens to be installed on.
"""

from fastapi import APIRouter, HTTPException, Query

from spark_pulse import tools

router = APIRouter(prefix="/api/memory", tags=["memory"])


def _control() -> dict:
    """The control node's own block."""
    return tools.node_stats.for_node(tools.node_service.control_node())


@router.get("/gpu")
def get_gpu_stats():
    return {"gpus": _control()["gpu"]}


@router.get("/cpu")
def get_cpu_stats():
    return _control()["cpu"]


@router.get("/disk")
def get_disk_stats():
    return {"disks": _control()["disk"]}


@router.get("")
def get_all_memory():
    return tools.node_stats.collect()


@router.delete("/processes/{pid}")
def kill_gpu_process(
    pid: int,
    node: str = Query("", description="Address of the node the process is on"),
    force: bool = Query(False, description="SIGKILL rather than SIGTERM"),
):
    """End one GPU process, on whichever node holds it.

    A process inside a container this control plane started is ended by
    stopping that container — killing the process inside it would leave the
    container behind holding its ports. Anything else is signalled through the
    node's agent, which is why the button now works on a node that is not this
    one.
    """
    result = tools.node_stats.terminate(pid, node, force)
    if not result.get("killed"):
        error = str(result.get("error") or "")
        if "no such process" in error.lower():
            raise HTTPException(status_code=404, detail=f"Process {pid} not found")
        if "not permitted" in error.lower() or "permission" in error.lower():
            raise HTTPException(
                status_code=403, detail=f"Not permitted to signal process {pid}"
            )
    return result

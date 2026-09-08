"""Monitoring API — what every node is doing.

One endpoint, and it answers for the whole cluster: ``nodes`` carries one block
per registered node, control plane first. There were three sub-endpoints
answering for this machine alone; nothing called them, and an endpoint that
silently means "the control node" is the defect this page was fixed for.

Nothing here reads hardware. Each node is asked through its own agent —
including this one — so the page does not depend on which machine the control
plane happens to be installed on.
"""

from fastapi import APIRouter, HTTPException, Query

from spark_pulse import tools

router = APIRouter(prefix="/api/memory", tags=["memory"])


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

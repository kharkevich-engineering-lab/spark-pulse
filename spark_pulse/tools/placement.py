"""Which nodes, ports and memory a run occupies, and what is left for the next.

Split from :mod:`native_runtime`: these read the records, ask each node what
it is listening on and holding, and decide a port and a memory budget — none
of it is about how a container is started, and every backend places a run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from spark_pulse import tools
from spark_pulse.config import config
from spark_pulse.tools import memory_budget
from spark_pulse.tools.runtime_errors import NativeRuntimeError

if TYPE_CHECKING:
    from spark_pulse.engines import Engine


def _load_records() -> list[dict[str, Any]]:
    """The deployment records, from the one store that owns them."""
    return tools.deployment_records.load()


def allocate_port(taken: set[int] | None = None) -> int:
    """First port in the configured range that ``taken`` does not name.

    ``taken`` is the whole answer. This used to bind each candidate on
    127.0.0.1 as well, which is a statement about the control plane — and the
    API port is bound on rank zero's node, which is a peer as often as not.
    What a node is listening on is asked of that node (:func:`_node_listeners`)
    and arrives here already folded into ``taken``.
    """
    taken = taken or set()
    start, end = config.default_port_range_start, config.default_port_range_end
    for port in range(start, end + 1):
        if port not in taken:
            return port
    raise NativeRuntimeError(
        f"no free port in the configured range {start}-{end}; "
        "widen default_port_range_* or stop an existing deployment"
    )


def _node_key(address: Any) -> str:
    """One name per machine: the empty string for this one, else its address.

    A solo record names no node and a cluster record may name this machine by
    its address; both are the same ports on the same host, so they have to
    land under the same key or a run here would not see the other.
    """
    text = str(address or "").strip()
    if not text:
        return ""
    try:
        if tools.node_service.is_local_address(text):
            return ""
    except Exception:  # pragma: no cover — best effort; the address still names it
        pass
    return text


def _occupied_nodes(record: dict[str, Any]) -> set[str] | None:
    """The nodes a run still occupies, by :func:`_node_key`; ``None`` if none.

    A live run occupies every node a rank of it is on. A finished run occupies
    nothing — unless it left orphans, and then it occupies their nodes: its
    ranks there were never confirmed gone, and a port or a gigabyte released
    on "we asked it to stop" is how every orphan bug in this class began. The
    one rule both the port choice and the memory budget read, so a node cannot
    be busy for one and free for the other.
    """
    finished = record.get("status") in ("stopped", "error")
    orphans = record.get("orphans") or []
    if finished and not orphans:
        return None
    if finished:
        nodes = {_node_key(o.get("node")) for o in orphans if isinstance(o, dict)}
    elif isinstance(record.get("ranks"), list) and record["ranks"]:
        nodes = {_node_key(entry.get("node")) for entry in record["ranks"]}
    else:
        nodes = {_node_key(n) for n in (record.get("nodes") or [])}
    return nodes or {""}


def _ports_in_use(exclude: str = "") -> dict[str, dict[int, str]]:
    """Ports live deployments hold, per node — API, rendezvous *and* RPC.

    Keyed by :func:`_node_key`, valued ``{port: the run's name}`` so a plan
    that moves off a port can say who holds it. Per node because a port is a
    property of a machine: two runs on two different nodes may both serve on
    8000, and pooling them made the second node's run move for nothing.

    A launch binds its rendezvous and RPC ports exactly as surely as its API
    port, so handing the same number out twice would break a deployment that
    never mentioned it. Every port a run holds is counted on every node it
    occupies — rank zero binds the API and rendezvous ports, the workers the
    RPC one, and the pre-flight checks the API port on all of them anyway.

    A stopped deployment with outstanding orphans still holds its ports on the
    orphans' nodes. Its ranks on unreachable nodes were never confirmed gone,
    and every orphan bug in this class comes from releasing a resource on
    inference — "we asked it to stop" — rather than on evidence that it did.

    ``exclude`` is the deployment being planned: a record written ahead of
    its own plan (a scheduled deploy, a re-plan of the same id) is not
    somebody else holding the port.
    """
    held: dict[str, dict[int, str]] = {}
    for record in _load_records():
        if exclude and record.get("id") == exclude:
            continue
        nodes = _occupied_nodes(record)
        if nodes is None:
            continue
        holder = str(record.get("name") or record.get("id") or "")
        for node in nodes:
            ports = held.setdefault(node, {})
            for key in ("port", "rendezvous_port", "rpc_port"):
                value = record.get(key)
                if isinstance(value, int) and value:
                    ports.setdefault(value, holder)
    return held


def _claimed_fraction(
    engine_obj: Engine, command: str, params: dict[str, Any]
) -> float | None:
    """The fraction of a node's memory a rendered run will take, if any.

    Only an engine whose spec maps ``gpu_memory_utilization`` takes one, and
    the rendered command is read before the params because it is what the
    engine is actually handed — a v1 template may spell the flag out with a
    literal of its own. A fraction engine handed none takes its default.
    """
    flag = engine_obj.spec.runtime.param_flags.get("gpu_memory_utilization")
    if not flag:
        return None
    found = memory_budget.fraction_in_command(command, (str(flag),))
    if found is not None:
        return found
    try:
        value = float(params.get("gpu_memory_utilization"))
    except (TypeError, ValueError):
        return memory_budget.ENGINE_DEFAULT_FRACTION
    return value if 0 < value <= 1 else memory_budget.ENGINE_DEFAULT_FRACTION


def _node_labels() -> tuple[dict[str, str], str]:
    """Registered names by address, and the control node's, for the budget."""
    try:
        records = list(tools.node_registry.list_nodes())
    except Exception:  # noqa: BLE001 — a name is a courtesy, not a fact we need
        return {}, "this machine"
    control = next((r for r in records if r.is_control_plane), None)
    return (
        {r.address: r.label for r in records if r.address},
        control.label if control is not None else "this machine",
    )


def _memory_budget(
    node_list: list[str],
    fraction: float | None,
    exclude: str,
    services: Callable[[str], Any],
) -> list[dict[str, Any]]:
    """Each node's memory budget, beside the runs already on it.

    The runs are the ones :func:`_occupied_nodes` says occupy the node — the
    same rule the port choice uses. A co-tenant whose record carries a
    fraction claims that fraction of the node's total; one that does not is
    measured, by asking the node what its containers hold (``GetNodeStats``
    through the node's agent, attributed by :mod:`node_stats`). The node is
    asked only when somebody else is on it: alone on a node, a run is judged
    by the engine's own startup check exactly as before.
    """
    tenants: dict[str, list[tuple[str, str, float | None]]] = {}
    for record in _load_records():
        if exclude and record.get("id") == exclude:
            continue
        nodes = _occupied_nodes(record)
        if nodes is None:
            continue
        holder = (
            str(record.get("id") or ""),
            str(record.get("name") or record.get("id") or ""),
            memory_budget.record_fraction(record),
        )
        for node in nodes:
            tenants.setdefault(node, []).append(holder)

    labels, control_label = _node_labels()
    budgets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for address in node_list or [""]:
        key = _node_key(address)
        if key in seen:
            continue
        seen.add(key)
        label = labels.get(address) or (control_label if not key else address)
        holders = tenants.get(key, [])
        if not holders:
            budgets.append(
                memory_budget.budget_for_node(
                    address, key, label, [], None, {}, fraction
                ).to_dict()
            )
            continue
        total: int | None = None
        measured: dict[str, int | None] | None = None
        reason = ""
        try:
            total, measured = tools.node_stats.memory_by_deployment(services(address))
        except Exception as exc:  # noqa: BLE001 — an unanswered node is unknown
            reason = f"could not ask {label} for its memory: {str(exc)[:200]}"
        budgets.append(
            memory_budget.budget_for_node(
                address, key, label, holders, total, measured, fraction, reason=reason
            ).to_dict()
        )
    return budgets


def _node_listeners(
    node_list: list[str],
) -> list[tuple[str, str, set[int] | None, str]]:
    """What each node the run will occupy is listening on, asked of the node.

    ``(node key, label, ports, why not)`` per node; ``ports`` is ``None`` when
    the node could not be asked. The question is the pre-flight's own — the
    same ``ss`` command through the same host probe (``RunHostProbe`` on the
    node's agent, the control node over loopback; the simulated host under
    ``SIMULATION_MODE``), parsed by the same function — so the planner and the
    check that follows it cannot disagree about what is free.
    """
    try:
        targets = tools.preflight.targets_for({"nodes": node_list})
    except Exception as exc:  # noqa: BLE001 — an unreadable registry cannot be asked
        return [
            (_node_key(a), str(a or "this machine"), None, str(exc))
            for a in (node_list or [""])
        ]
    answers: list[tuple[str, str, set[int] | None, str]] = []
    for target in targets:
        key = "" if target.is_control_plane else _node_key(target.address)
        try:
            result = tools.preflight.probe_for(target).run(
                tools.preflight.PORTS_COMMAND
            )
        except Exception as exc:  # noqa: BLE001 — a probe that raised went unanswered
            answers.append((key, target.label, None, str(exc)))
            continue
        if result.ok:
            ports = tools.preflight.parse_listening_ports(result.stdout)
            answers.append((key, target.label, ports, ""))
        else:
            why = result.error or result.message or f"exit {result.returncode}"
            answers.append((key, target.label, None, why))
    return answers


def _choose_port(
    preferred: int | None,
    node_list: list[str],
    reserved: list[tuple[int | None, str]],
    exclude: str,
    warnings: list[str],
) -> int:
    """The API port for a run whose port is a preference, not a pin.

    Every recipe in the OCI collection says ``port: 8000``, so a second run on
    a node asked for 8000 again and the pre-flight blocked it. A recipe's port
    is what it would like: it is kept when it is free on every node the run
    occupies and moved into the configured range when it is not, with the
    holder named — a run on record, or whatever the node says is listening.

    A node that cannot be asked is not assumed free. Its ports are judged from
    the records alone and the plan says so; the pre-flight asks again before
    anything is launched, and a pinned port never comes through here at all.
    """
    in_use = _ports_in_use(exclude=exclude)
    taken: dict[int, str] = {}
    for key in {_node_key(a) for a in node_list} or {""}:
        for port, holder in in_use.get(key, {}).items():
            taken.setdefault(port, f"run {holder}")
    for _key, label, listening, why in _node_listeners(node_list):
        if listening is None:
            warnings.append(
                f"could not list the ports in use on {label} ({why}); the API "
                "port was chosen from the runs on record alone, and the "
                "pre-flight checks it again before launch"
            )
            continue
        for port in listening:
            taken.setdefault(port, f"a process on {label}")
    for port, role in reserved:
        if port:
            taken.setdefault(int(port), f"the engine's own {role} port")
    if preferred and int(preferred) not in taken:
        return int(preferred)
    port = allocate_port(set(taken))
    if preferred:
        warnings.append(
            f"port {preferred} is held by {taken[int(preferred)]}; "
            f"this run gets {port}"
        )
    return port

"""A control node its own ledger has denied re-enrols itself.

The ledger denies a node whose hardware fingerprint moved and surfaces it for
a human decision. For a peer that is right. For the control node there is
nobody to surface it to — the process exits at startup and the page that
would show the decision never loads — so the decision is made in
``start_local_agent``: said out loud, then re-enrolled under the same id.

This is what the fingerprint bug looked like in the field: a container that
had been running since before enrolment was stopped, its ``veth`` vanished,
the fingerprint changed, and the control plane restarted into a loop it could
not leave.
"""

from __future__ import annotations

import logging

import pytest

from spark_pulse.agent.local import start_local_agent
from spark_pulse.mock.docker import MockDockerClient, MockDockerService

pytestmark = pytest.mark.asyncio


async def _start(agent_server, directory, node_id=""):
    return await start_local_agent(
        agent_server,
        directory=directory,
        docker_service=MockDockerService(MockDockerClient()),
        host="127.0.0.1",
        node_id=node_id,
        wait=10.0,
    )


async def test_a_denied_control_node_re_enrols_under_the_same_id(
    agent_server, tmp_path, a_runnable_agent_binary, caplog
):
    directory = tmp_path / "local-agent"
    first = await _start(agent_server, directory)
    node_id = first.node_id
    await first.stop()
    assert agent_server.ledger.get(node_id).state == "accepted"

    agent_server.ledger.deny(
        node_id, "hardware fingerprint changed; the node may be reimaged"
    )
    assert agent_server.ledger.get(node_id).state == "denied"

    with caplog.at_level(logging.WARNING, logger="spark_pulse.agent.local"):
        second = await _start(agent_server, directory, node_id=node_id)
    try:
        assert second.node_id == node_id, "the identity is the registry's, still"
        assert agent_server.hub.is_connected(node_id)
        assert agent_server.ledger.get(node_id).state == "accepted"
        assert any(
            "denied by its own ledger" in record.getMessage()
            and "hardware fingerprint changed" in record.getMessage()
            for record in caplog.records
        ), "the decision is said out loud, with the ledger's reason"
    finally:
        await second.stop()


async def test_an_accepted_control_node_is_not_re_enrolled(
    agent_server, tmp_path, a_runnable_agent_binary
):
    """The recovery is for a denial only; a restart keeps the same certificate."""
    directory = tmp_path / "local-agent"
    first = await _start(agent_server, directory)
    node_id = first.node_id
    issued = agent_server.ledger.get(node_id).issued
    await first.stop()

    second = await _start(agent_server, directory, node_id=node_id)
    try:
        assert second.node_id == node_id
        assert agent_server.ledger.get(node_id).issued == issued, "no new certificate"
    finally:
        await second.stop()

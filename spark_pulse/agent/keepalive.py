"""Keepalive, in both directions, with the invariant that ties them together.

This is one of the three traps §3.2 names, and it is worth its own file
because the failure is invisible. If the **server's minimum ping interval is
not below the client's keepalive time**, the server decides the client is
pinging too often, kills the connection with ``ENHANCE_YOUR_CALM``, and the
client — per the HTTP/2 spec's guidance, silently — *doubles* its keepalive
interval. Detection then gets slower every time it happens, over hours, with
nothing in any log saying so. A cluster that detected a dead node in ten
seconds on Monday takes minutes on Wednesday and nobody knows why.

The numbers are §3.2's: ten seconds client, twenty server, a five second
minimum interval, and pings permitted without calls on both sides. The last
one matters because an idle agent — one holding a stream with no command on it
— is the normal state, and a keepalive that only runs during calls would never
run at all.

:func:`check_invariant` is called at import and asserted by a test, so the
relationship cannot be broken by editing one number.
"""

from __future__ import annotations

__all__ = [
    "CLIENT_KEEPALIVE_MS",
    "SERVER_KEEPALIVE_MS",
    "SERVER_MIN_PING_INTERVAL_MS",
    "KEEPALIVE_TIMEOUT_MS",
    "MAX_MESSAGE_BYTES",
    "client_options",
    "server_options",
    "check_invariant",
]

#: How often an idle agent pings the control plane.
CLIENT_KEEPALIVE_MS = 10_000

#: How often the control plane pings an idle agent.
SERVER_KEEPALIVE_MS = 20_000

#: The floor the server enforces on how often a client may ping. **Must stay
#: below** :data:`CLIENT_KEEPALIVE_MS`.
SERVER_MIN_PING_INTERVAL_MS = 5_000

#: How long either side waits for a ping to be answered before giving up on
#: the connection.
KEEPALIVE_TIMEOUT_MS = 5_000

#: 64 MiB. Commands carry scripts and configuration, not artifacts — a model
#: or an image is fetched once by the control node and served from its
#: registry (§3.4). The limit is generous enough that a large serve script or
#: a directory of mods is never the thing that fails, and small enough that a
#: mistake is caught here rather than by the node running out of memory.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024


def check_invariant() -> None:
    """Raise if the server would punish the client for its own keepalive."""
    if SERVER_MIN_PING_INTERVAL_MS >= CLIENT_KEEPALIVE_MS:
        raise ValueError(
            "the server's minimum ping interval "
            f"({SERVER_MIN_PING_INTERVAL_MS}ms) must be below the client's "
            f"keepalive time ({CLIENT_KEEPALIVE_MS}ms), or the server kills "
            "the connection with ENHANCE_YOUR_CALM and the client silently "
            "doubles its interval"
        )


def client_options() -> list[tuple[str, int]]:
    """gRPC channel options for an agent dialling the control plane."""
    check_invariant()
    return [
        ("grpc.keepalive_time_ms", CLIENT_KEEPALIVE_MS),
        ("grpc.keepalive_timeout_ms", KEEPALIVE_TIMEOUT_MS),
        # An agent with no command in flight is the normal case, so pings must
        # be permitted without calls or the keepalive never fires.
        ("grpc.keepalive_permit_without_calls", 1),
        # 0 means "no limit on pings sent without data". The default of 2 is
        # for a client that is expected to be making requests; ours is not.
        ("grpc.http2.max_pings_without_data", 0),
        ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
        ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
    ]


def server_options() -> list[tuple[str, int]]:
    """gRPC server options for the control plane's one inbound port."""
    check_invariant()
    return [
        ("grpc.keepalive_time_ms", SERVER_KEEPALIVE_MS),
        ("grpc.keepalive_timeout_ms", KEEPALIVE_TIMEOUT_MS),
        ("grpc.keepalive_permit_without_calls", 1),
        ("grpc.http2.min_ping_interval_without_data_ms", SERVER_MIN_PING_INTERVAL_MS),
        # Never send GOAWAY with ENHANCE_YOUR_CALM over ping frequency. The
        # invariant above should make this unreachable; this makes it
        # unreachable even if a future edit breaks the invariant, because the
        # failure mode is silent and slow rather than loud and fast.
        ("grpc.http2.max_ping_strikes", 0),
        ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
        ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
    ]


check_invariant()

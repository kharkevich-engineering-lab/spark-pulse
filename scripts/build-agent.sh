#!/usr/bin/env bash
# Build the node agent binary the control plane ships to nodes.
#
# The agent is one static binary. It is built for the *node's* platform, not
# the control plane's, because those are only the same machine when the
# control plane happens to be a Spark — and the whole reason the agent is not
# Python any more is that a bundle built out of the control plane's own
# environment is a bundle that only works when the two match.
#
# Built inside a container so the result does not depend on what happens to be
# installed here. musl, so the binary needs no libc on the node either.
#
# **Native only.** The container runs as the target's architecture, so the
# compile inside it is native — never emulated. Emulation would work, and it
# is refused anyway: the same build takes 1m32s on an aarch64 runner and 26
# minutes under qemu on an x86_64 one, and a build slow enough that people
# start skipping it is a build that stops being run. If the architectures do
# not match, this says so and stops rather than quietly taking half an hour.
set -euo pipefail

TARGET="${TARGET:-aarch64-unknown-linux-musl}"
ENGINE="${CONTAINER_ENGINE:-}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/spark_pulse/agent/bin"

if [[ -z "$ENGINE" ]]; then
  for candidate in podman docker; do
    if command -v "$candidate" >/dev/null 2>&1; then ENGINE="$candidate"; break; fi
  done
fi
if [[ -z "$ENGINE" ]]; then
  echo "build-agent: need podman or docker to build the agent." >&2
  echo "  Set CONTAINER_ENGINE, or build natively with:" >&2
  echo "    cd agent && cargo build --release --target $TARGET" >&2
  exit 1
fi

mkdir -p "$OUT" "$ROOT/.agent-build-cache"

# Normalise both the target's architecture and this machine's to the names a
# container platform uses, so they can be compared.
canonical() {
  case "$1" in
    aarch64|arm64) echo arm64 ;;
    x86_64|amd64) echo amd64 ;;
    *) echo "$1" ;;
  esac
}

TARGET_ARCH="$(canonical "${TARGET%%-*}")"
HOST_ARCH="$(canonical "$(uname -m)")"

if [[ "$TARGET_ARCH" != "$HOST_ARCH" ]]; then
  host_triple="${HOST_ARCH/arm64/aarch64}"
  host_triple="${host_triple/amd64/x86_64}-unknown-linux-musl"
  {
    echo "build-agent: refusing to build $TARGET on $HOST_ARCH."
    echo
    echo "  It would run under emulation, which works and takes about half an"
    echo "  hour rather than a minute and a half. Run this on a $TARGET_ARCH"
    echo "  machine or CI runner instead."
    echo
    echo "  To build for *this* machine instead:"
    echo "    TARGET=$host_triple $0"
  } >&2
  exit 1
fi

echo "build-agent: building $TARGET natively on $HOST_ARCH with $ENGINE"
"$ENGINE" run --rm --platform "linux/$TARGET_ARCH" \
  -v "$ROOT":/src:z \
  -v "$ROOT/.agent-build-cache":/target:z \
  -w /src/agent \
  -e CARGO_TARGET_DIR=/target \
  docker.io/library/rust:alpine \
  sh -euc '
    apk add --no-cache musl-dev protoc protobuf-dev >/dev/null
    cargo build --release --target '"$TARGET"'
    cp "/target/'"$TARGET"'/release/spark-pulse-agent" /src/spark_pulse/agent/bin/spark-pulse-agent-'"$TARGET"'
  '

BINARY="$OUT/spark-pulse-agent-$TARGET"
# 755 outright, not `chmod +x`: the file arrives with whatever umask the
# container had, and `+x` only adds the execute bit to that. A binary that
# ships 711 works and is unreadable to everyone but its owner, which is a
# surprise nobody needs to debug.
chmod 755 "$BINARY"
echo "build-agent: $(du -h "$BINARY" | cut -f1) -> ${BINARY#"$ROOT"/}"

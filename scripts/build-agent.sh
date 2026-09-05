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

echo "build-agent: building $TARGET with $ENGINE"
"$ENGINE" run --rm \
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
chmod +x "$BINARY"
echo "build-agent: $(du -h "$BINARY" | cut -f1) -> ${BINARY#"$ROOT"/}"

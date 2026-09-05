#!/usr/bin/env bash
# Regenerate the node-agent protocol stubs from spark_pulse/agent/agent.proto.
#
# The generated modules are committed. protoc is not a build-time dependency of
# an install, and `pip install spark-pulse` must not need grpcio-tools; only a
# developer changing the protocol runs this.
#
# It is run from the repository root with `-I.` on purpose: that makes the
# generated import read `from spark_pulse.agent import agent_pb2`, which is
# importable from inside the package. Running it from inside the package
# directory instead emits a bare `import agent_pb2`, which is not.
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
if [ -x .venv/bin/python ]; then
    PYTHON=.venv/bin/python
fi

"$PYTHON" -m grpc_tools.protoc \
    -I. \
    --python_out=. \
    --pyi_out=. \
    --grpc_python_out=. \
    spark_pulse/agent/agent.proto

echo "regenerated spark_pulse/agent/agent_pb2.py, agent_pb2.pyi, agent_pb2_grpc.py"

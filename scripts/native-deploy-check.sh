#!/usr/bin/env bash
# Manual verification of the native deploy path on a real DGX Spark.
#
# Deploys a recipe through the REST API with `runtime: native`, waits for the
# engine to answer, hits /v1/models, and tears the deployment down again. This
# is the hardware check that simulation-mode tests cannot do: it needs a real
# Docker daemon, a real GPU and the engine image pulled.
#
# Usage: ./scripts/native-deploy-check.sh <recipe-id> [options]
#
# Options:
#   --base-url URL   Spark Pulse base URL (default http://127.0.0.1:8100)
#   --engine NAME    Engine override (vllm, sglang)
#   --model ID       Model override; must already be in the local catalogue
#   --timeout SECS   How long to wait for readiness (default 900)
#   --keep           Leave the deployment running instead of tearing it down
#   --help           Show this help
#
# Prerequisites on the Spark:
#   * spark-pulse running with `runtime: native` in
#     ~/.config/spark-pulse/settings.json (or SPARK_PULSE_RUNTIME=native)
#   * the engine image pulled, and the recipe's model downloaded
#   * curl and jq on PATH
#
# Exits non-zero with the deployment's log tail on any failure.

set -euo pipefail

base_url="http://127.0.0.1:8100"
recipe=""
engine=""
model=""
timeout=900
keep=false

usage() {
    sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --base-url) base_url="$2"; shift 2 ;;
        --engine)   engine="$2";   shift 2 ;;
        --model)    model="$2";    shift 2 ;;
        --timeout)  timeout="$2";  shift 2 ;;
        --keep)     keep=true;     shift ;;
        --help|-h)  usage; exit 0 ;;
        -*)         echo "Unknown option: $1" >&2; usage; exit 2 ;;
        *)          recipe="$1";   shift ;;
    esac
done

if [[ -z "$recipe" ]]; then
    echo "error: a recipe id is required" >&2
    usage
    exit 2
fi

for tool in curl jq; do
    command -v "$tool" >/dev/null || { echo "error: $tool is required" >&2; exit 2; }
done

api() {
    local method="$1" path="$2" body="${3:-}"
    if [[ -n "$body" ]]; then
        curl -fsS -X "$method" -H 'Content-Type: application/json' -d "$body" "$base_url/api$path"
    else
        curl -fsS -X "$method" "$base_url/api$path"
    fi
}

log_tail() {
    local id="$1"
    echo "--- last 50 log lines -------------------------------------------"
    api GET "/deployments/$id/logs?lines=50" 2>/dev/null | jq -r '.logs' || echo "(no logs)"
    echo "-----------------------------------------------------------------"
}

# ── 0. The server must actually be on the native runtime ────────────────────

echo "==> Checking $base_url"
runtime=$(api GET "/config" | jq -r '.runtime')
if [[ "$runtime" != "native" ]]; then
    echo "error: server reports runtime='$runtime'; set runtime: native and restart" >&2
    exit 1
fi
echo "    runtime: native"

# ── 1. Dry run first — a bad plan should never start a container ────────────

plan_body=$(jq -nc \
    --arg recipe "$recipe" \
    --arg engine "$engine" \
    --arg model "$model" \
    '{recipe_id: $recipe, allow_missing_model: false}
     + (if $engine == "" then {} else {engine: $engine} end)
     + (if $model  == "" then {} else {model:  $model}  end)')

echo "==> Planning $recipe"
plan=$(api POST "/deployments/plan" "$plan_body")
echo "    engine:  $(jq -r '.engine + "/" + .variant' <<<"$plan")"
echo "    image:   $(jq -r '.image_ref' <<<"$plan")"
echo "    model:   $(jq -r '.model' <<<"$plan")"
echo "    port:    $(jq -r '.port' <<<"$plan")"
echo "    command: $(jq -r '.launch_command' <<<"$plan")"
jq -r '.warnings[]? | "    warning: " + .' <<<"$plan"

# ── 2. Deploy ───────────────────────────────────────────────────────────────

create_body=$(jq -nc \
    --arg recipe "$recipe" \
    --arg engine "$engine" \
    --arg model "$model" \
    '{recipe_id: $recipe, name: ($recipe + "-native-check"), params: {}}
     + (if $engine == "" then {} else {engine: $engine} end)
     + (if $model  == "" then {} else {model:  $model}  end)')

echo "==> Deploying"
deployment=$(api POST "/deployments" "$create_body")
id=$(jq -r '.id' <<<"$deployment")
port=$(jq -r '.port' <<<"$deployment")
container=$(jq -r '.container_name' <<<"$deployment")
echo "    id:        $id"
echo "    container: $container"

cleanup() {
    if [[ "$keep" == true ]]; then
        echo "==> --keep given: leaving $id running on port $port"
        return
    fi
    echo "==> Tearing down $id"
    api DELETE "/deployments/$id" >/dev/null 2>&1 || true
    api DELETE "/deployments/$id" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# ── 3. Wait for readiness ───────────────────────────────────────────────────

echo "==> Waiting up to ${timeout}s for readiness"
deadline=$(( $(date +%s) + timeout ))
while true; do
    state=$(api GET "/deployments/$id")
    status=$(jq -r '.status' <<<"$state")
    ready=$(jq -r '.ready // false' <<<"$state")

    if [[ "$ready" == "true" ]]; then
        echo "    ready after $(( timeout - (deadline - $(date +%s)) ))s"
        break
    fi
    if [[ "$status" == "error" || "$status" == "stopped" ]]; then
        echo "error: deployment ended as '$status': $(jq -r '.error_message // "no message"' <<<"$state")" >&2
        log_tail "$id"
        exit 1
    fi
    if (( $(date +%s) >= deadline )); then
        echo "error: not ready within ${timeout}s (status=$status)" >&2
        log_tail "$id"
        exit 1
    fi
    sleep 5
done

# ── 4. The engine must actually serve ───────────────────────────────────────

echo "==> GET http://127.0.0.1:$port/v1/models"
if ! models=$(curl -fsS "http://127.0.0.1:$port/v1/models"); then
    echo "error: /v1/models did not answer" >&2
    log_tail "$id"
    exit 1
fi
jq -r '.data[]? | "    served: " + .id' <<<"$models" || echo "    $models"

echo "==> OK: native deploy of '$recipe' served and is being torn down"

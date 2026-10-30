#!/usr/bin/env bash
# DeliveryOps: one entrypoint, three run/deploy targets.
#
#   deploy/run.sh [local|gcp|aws] [--apply]
#
# With no target argument it asks: "Run target? [local/gcp/aws] (default
# local):" - an invalid answer gets one re-prompt, then the script gives up.
#
#   local  Run the stack on this machine. With Docker: `docker compose up`.
#          Without: the API and worker as uv processes (in-memory or
#          DATABASE_URL-backed, per your env) in the background of this
#          script; Ctrl-C stops both.
#   gcp    terraform init + plan in deploy/terraform/gcp.
#   aws    terraform init + plan in deploy/terraform/aws.
#
# Cloud targets NEVER apply without --apply: the default is plan-and-stop.
# Backend configuration comes from the environment: either a backend.hcl
# file sitting in the module directory (passed as -backend-config=<file>),
# or TF_BACKEND_CONFIG_ARGS holding extra init arguments, word-split, e.g.
#   TF_BACKEND_CONFIG_ARGS='-backend-config=bucket=my-state -backend-config=prefix=deliveryops/prod'
# (No quotes around values inside the variable: word splitting keeps them
# literal and terraform rejects a key or value that starts with a quote.)
# Required terraform variables (gcp: project_id + image; aws: image) come
# from TF_VAR_* environment variables or a terraform.tfvars in the module
# directory - see deploy/README.md for the full per-target runbooks.
set -euo pipefail

cd "$(dirname "$0")/.."

usage() {
  echo "usage: deploy/run.sh [local|gcp|aws] [--apply]" >&2
}

TARGET=""
APPLY=0
for arg in "$@"; do
  case "$arg" in
    local | gcp | aws) TARGET="$arg" ;;
    --apply) APPLY=1 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "run.sh: unknown argument: $arg" >&2; usage; exit 2 ;;
  esac
done

if [ -z "$TARGET" ]; then
  invalid=0
  while [ -z "$TARGET" ]; do
    printf 'Run target? [local/gcp/aws] (default local): '
    read -r answer || answer=""
    answer="${answer:-local}"
    case "$answer" in
      local | gcp | aws) TARGET="$answer" ;;
      *)
        invalid=$((invalid + 1))
        if [ "$invalid" -ge 2 ]; then
          echo "run.sh: '$answer' is not a target either - aborting" >&2
          exit 2
        fi
        echo "run.sh: unknown target '$answer' (want local, gcp or aws)" >&2
        ;;
    esac
  done
fi

run_local() {
  if command -v docker >/dev/null 2>&1; then
    echo "local target, via docker compose"
    docker compose up --build -d
    echo "  api:       http://localhost:8000/health"
    echo "  dashboard: http://localhost:8000/dashboard"
    echo "  stop with: docker compose down"
    return
  fi
  command -v uv >/dev/null 2>&1 || {
    echo "run.sh: neither docker nor uv is available for the local target" >&2
    exit 1
  }
  echo "local target, via uv (no docker found)"
  PORT=8000 uv run deliveryops-api >/tmp/deliveryops-api.log 2>&1 &
  api_pid=$!
  PORT=18099 uv run deliveryops-worker >/tmp/deliveryops-worker.log 2>&1 &
  worker_pid=$!
  # Both children die with this script: Ctrl-C (or any exit) runs the trap.
  trap 'kill "$api_pid" "$worker_pid" 2>/dev/null || true' EXIT INT TERM
  for _ in $(seq 1 60); do
    curl -sf http://localhost:8000/health >/dev/null 2>&1 && break
    sleep 1
  done
  curl -sf http://localhost:8000/health >/dev/null || {
    echo "run.sh: the API did not come up - see /tmp/deliveryops-api.log" >&2
    exit 1
  }
  echo "  api:       http://localhost:8000/health"
  echo "  dashboard: http://localhost:8000/dashboard"
  echo "  worker:    http://localhost:18099/health"
  echo "  drive a gate with: uv run deliveryops-approve <run_id>"
  echo "  Ctrl-C stops the API and the worker."
  wait
}

run_cloud() {
  local dir="deploy/terraform/$TARGET"
  command -v terraform >/dev/null 2>&1 || {
    echo "run.sh: terraform is required for the $TARGET target" >&2
    exit 1
  }
  local init_args=()
  if [ -f "$dir/backend.hcl" ]; then
    init_args+=("-backend-config=$dir/backend.hcl")
  fi
  if [ -n "${TF_BACKEND_CONFIG_ARGS:-}" ]; then
    # Deliberate word splitting: the variable holds whole init arguments.
    read -ra extra <<<"$TF_BACKEND_CONFIG_ARGS"
    init_args+=("${extra[@]}")
  fi
  echo "$TARGET target: terraform init + plan in $dir"
  terraform -chdir="$dir" init "${init_args[@]}"
  terraform -chdir="$dir" plan
  if [ "$APPLY" -eq 1 ]; then
    echo "$TARGET target: --apply given, applying"
    terraform -chdir="$dir" apply
  else
    echo "plan only - nothing applied. Re-run with --apply to apply."
  fi
}

case "$TARGET" in
  local) run_local ;;
  gcp | aws) run_cloud ;;
esac

#!/usr/bin/env bash
#
# A persistent dev-task container for building and testing the harness.
#
# Harbor tears its environments down at the end of a trial, which is wrong for
# development: we want one long-lived Odoo we can iterate against. This builds the dev
# task's image with Harbor (so it is byte-identical to what the eval uses) and runs it
# directly, then waits for the scenario setup marker.
#
#   scripts/devbox.sh up      # build if needed, start, wait for /tmp/saas_setup_complete
#   scripts/devbox.sh down    # remove the container
#   scripts/devbox.sh reset   # down + up (a clean Odoo, e.g. after a destructive test)
#   scripts/devbox.sh shell   # interactive shell inside it
#
# The dev task is fixed and must never appear in configs/eval100.txt.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 2

DEV_TASK="${ERP_DEV_TASK:-2000_easy_01_buy_only_baseline}"
NAME="${ERP_DEV_CONTAINER:-erpdev}"
TASK_DIR="${ERP_TASKS_DIR:-vendor/erp-bench/tasks}/$DEV_TASK"
IMAGE="erp-harness-devbox:$DEV_TASK"
PORT="${ERP_DEV_PORT:-18069}"
MEMORY="${ERP_DEV_MEMORY:-4g}"
CPUS="${ERP_DEV_CPUS:-3}"

build() {
  if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "devbox: building $IMAGE from $TASK_DIR/environment"
    docker build -t "$IMAGE" "$TASK_DIR/environment" || exit 1
  fi
}

up() {
  build
  if [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" = "true" ]; then
    echo "devbox: $NAME already running"
  else
    # Never `docker start` a stopped one. The entrypoint re-runs against a Postgres
    # data directory it did not shut down cleanly and dies with "the database system
    # is shutting down"; worse, /tmp/saas_setup_complete survives from the previous
    # boot, so a marker-only readiness check reports success on a dead container.
    docker rm -f "$NAME" >/dev/null 2>&1
    docker run -d --name "$NAME" --memory "$MEMORY" --cpus "$CPUS" \
      -p "$PORT:8069" "$IMAGE" >/dev/null || exit 1
  fi
  echo -n "devbox: waiting for Odoo scenario setup"
  for _ in $(seq 1 180); do
    if ! docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null | grep -q true; then
      echo " — CONTAINER EXITED"
      docker logs --tail 20 "$NAME"
      return 1
    fi
    # Both conditions: the setup marker AND a live Odoo that answers RPC.
    if docker exec "$NAME" test -f /tmp/saas_setup_complete 2>/dev/null \
       && docker exec "$NAME" python3 -c "
import xmlrpc.client
c = xmlrpc.client.ServerProxy('http://127.0.0.1:8069/xmlrpc/2/common')
c.version()
" >/dev/null 2>&1; then
      echo " — ready"
      echo "devbox: $NAME  odoo http://127.0.0.1:$PORT  db bench  admin/pass"
      return 0
    fi
    echo -n "."
    sleep 2
  done
  echo " — TIMED OUT"
  docker logs --tail 20 "$NAME"
  return 1
}

case "${1:-up}" in
  up) up ;;
  down) docker rm -f "$NAME" >/dev/null 2>&1; echo "devbox: removed $NAME" ;;
  reset) docker rm -f "$NAME" >/dev/null 2>&1; up ;;
  shell) docker exec -it "$NAME" bash ;;
  *) echo "usage: $0 {up|down|reset|shell}" >&2; exit 2 ;;
esac

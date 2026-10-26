#!/usr/bin/env bash
# Full local verification against real Docker, PostgreSQL and MongoDB.
#
#   bash scripts/verify_local_stack.sh            # everything
#   SKIP_ACCEPTANCE=1 bash scripts/verify_local_stack.sh   # skip the slow R24-R27 stack
#
# Steps: preflight -> static checks -> `make dev` -> live-MongoDB tests -> full
# backend suite -> frontend tests/build -> R24-R27 acceptance (own compose
# project) -> seed demo users + workload for a manual walkthrough.
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
PYTHON=./nosql/bin/python
RESULTS=()
FAILED=0

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
record() {
  if [[ "$2" == 0 ]]; then RESULTS+=("PASS  $1"); else RESULTS+=("FAIL  $1"); FAILED=1; fi
}
run() { local name="$1"; shift; step "$name"; "$@"; record "$name" "$?"; }

step "Preflight"
command -v docker >/dev/null || { echo "docker is not on PATH" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker daemon is not reachable; start Docker Desktop" >&2; exit 1; }
[[ -x "$PYTHON" ]] || { echo "Run ./scripts/bootstrap.sh first (needs ./nosql)" >&2; exit 1; }
mkdir -p .secrets
[[ -f .secrets/control_plane_master_key ]] || "$PYTHON" scripts/generate_master_key.py --output .secrets/control_plane_master_key
[[ -f .secrets/mongodb_replica_keyfile ]] || "$PYTHON" scripts/generate_mongodb_keyfile.py --output .secrets/mongodb_replica_keyfile
export JWT_SIGNING_KEY="${JWT_SIGNING_KEY:-$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(48))')}"
export AI_PROVIDER="${AI_PROVIDER:-deterministic}"

run "ruff" "$PYTHON" -m ruff check backend scripts
run "mypy" env MYPYPATH=backend "$PYTHON" -m mypy backend/app
run "database portability boundary" "$PYTHON" scripts/check_database_portability.py
run "compose config" docker compose --profile light config -q

run "make dev (build + start stack)" make dev
step "Waiting for the API through the console proxy"
healthy=1
for _ in $(seq 1 90); do
  if curl -fsS http://127.0.0.1:${FRONTEND_HOST_PORT:-5173}/health/ready >/dev/null 2>&1; then healthy=0; break; fi
  sleep 2
done
record "stack healthy (migrate, api, worker, frontend)" "$healthy"
docker compose --profile light ps

run "live-MongoDB integration tests" "$PYTHON" -m pytest -q \
  backend/tests/metrics/test_live_workload_metrics.py \
  backend/tests/mongodb/test_capabilities_integration.py \
  backend/tests/mongodb/test_executor_permissions.py \
  backend/tests/production/test_durable_lifecycle.py \
  backend/tests/security/test_security_suite.py \
  backend/tests/workloads/test_durable_snapshot.py
run "full backend suite" "$PYTHON" -m pytest -q backend/tests
run "frontend dependencies (npm ci, lockfile)" bash -c "cd frontend && npm ci --ignore-scripts --no-audit --no-fund"
run "frontend unit tests" bash -c "cd frontend && npm test"
run "frontend production build" bash -c "cd frontend && npm run build"
step "Worker heartbeat"
docker compose --profile light logs --tail 20 worker

if [[ -z "${SKIP_ACCEPTANCE:-}" ]]; then
  run "R24-R27 real-stack acceptance" make r24r27-acceptance
fi

step "Seeding demo users and workload for a manual walkthrough"
export DATABASE_URL="postgresql+asyncpg://control_plane:control_plane_dev_only@127.0.0.1:${POSTGRES_HOST_PORT:-5432}/control_plane"
CONTROL_PLANE_PASSWORD=operator-password-123 "$PYTHON" scripts/manage_users.py create --email ops@example.com --role OPERATOR || true
CONTROL_PLANE_PASSWORD=approver-password-123 "$PYTHON" scripts/manage_users.py create --email lead@example.com --role APPROVER || true
( make dev-workload DURATION=300 >/tmp/nosql-dev-workload.log 2>&1 & )

printf '\n\033[1mResults\033[0m\n'
printf '  %s\n' "${RESULTS[@]}"
cat <<'EOF'

Manual walkthrough (http://localhost:5173), workload is running for 5 minutes:
  1. Sign in as ops@example.com / operator-password-123
  2. Targets: add orders-production (label mongo-monitored) and orders-sandbox (label mongo-evaluation)
  3. On orders-production: map the sandbox, wait for the checklist (telemetry every 30 s), allowlist commerce.orders
  4. Start a run; at the approval gate sign in as lead@example.com / approver-password-123 and approve from Approvals
  5. Watch deploy + monitoring; check Ledger shows "Chain verified"
Stop the stack with: docker compose --profile light down
EOF
exit "$FAILED"

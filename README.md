# AI-Driven Autonomous NoSQL Query Optimization Framework

A safety-first control plane that watches a MongoDB workload, proposes index
changes, **proves** them on a separate sandbox replica with paired statistics,
and deploys only what passes — behind a four-eyes approval (or a narrow
autonomous path), with an append-only hash-chained ledger, post-deployment
monitoring, and ownership-verified rollback. A local LLM (Ollama) diagnoses and
ranks, but never decides or executes.

```
telemetry ─▶ snapshot ─▶ diagnose* ─▶ candidates ─▶ rank* ─▶ calibrate ─▶ sandbox trials
          ─▶ ◆ admission (paired bootstrap) ─▶ ◆ approval ─▶ deploy ─▶ monitor ─▶ done | rollback
                                                                  (* advisory model)
```

This project targets Intel x86_64 macOS using CPython 3.12 and linux/amd64 Docker containers.

## Quick start (local stack)

```bash
./scripts/bootstrap.sh                 # first time: project venv in nosql/
source nosql/bin/activate
mkdir -p .secrets                      # first time: dev-only secrets (git-ignored)
python scripts/generate_master_key.py --output .secrets/control_plane_master_key
python scripts/generate_mongodb_keyfile.py --output .secrets/mongodb_replica_keyfile
export JWT_SIGNING_KEY=$(python -c 'import secrets; print(secrets.token_urlsafe(48))')
make dev                               # postgres, 2× mongo, migrate, api, worker, frontend
make user EMAIL=ops@example.com ROLE=OPERATOR
make user EMAIL=lead@example.com ROLE=APPROVER
make dev-workload                      # seed commerce.orders and drive slow reads for 2 min
```

Open http://localhost:5173 and sign in as the operator, then:

1. **Targets → Add a target** `orders-production` with connection label `mongo-monitored`, and a
   second target `orders-sandbox` with label `mongo-evaluation`.
2. On `orders-production`, map the sandbox and wait for the readiness checklist to go green
   (the worker collects telemetry every 30 s); allowlist `commerce.orders`.
3. **Start a run.** Watch the lifecycle rail; when it reaches the approval gate, sign in as the
   approver (a different person — self-approval is refused) and approve from **Approvals**.

Set `AI_PROVIDER=deterministic` if Ollama is not running; diagnosis then returns no findings and
candidates come only from the deterministic rules.

## How the worker reaches MongoDB

Credentials never enter the API or PostgreSQL. The worker mounts a JSON file
(`TARGET_CONNECTIONS_FILE`, dev default [`infra/dev/target_connections.json`](infra/dev/target_connections.json))
keyed by a target's `connection_label`:

| role           | used for                                                         |
| -------------- | ---------------------------------------------------------------- |
| `observer_uri` | read-only telemetry, capability probes, sandbox copy source, monitoring probes |
| `executor_uri` | `createIndex` / `dropIndex` / query settings on the monitored target only |
| `sandbox_uri`  | rewriting the disposable evaluation replica                      |

The worker publishes only label names and roles (never URIs) so the console can show what is
configured. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design.

## Operator console

- **Overview** — runs in progress, approvals waiting, stalled runs with plain-language causes.
- **Targets** — readiness checklist, sandbox mapping, namespace allowlist, capabilities.
- **Runs** — lifecycle rail, workload shapes, diagnosis, candidates with paired-trial charts and
  the full admission evidence (confidence bounds per metric), deployment ledger, monitoring,
  rollback, advisory model calls, and raw evidence.
- **Approvals** — inbox with expiry countdowns; **Ledger** — chain verification; **System** —
  worker heartbeat, AI provider, connections.

## Testing

```bash
make verify-local   # everything against real Docker: stack, live-Mongo tests, suite, frontend, R24-R27
make test           # backend (live-Mongo integration tests need `make dev` running)
make acceptance
cd frontend && npm test && npm run build
```

## Docker profiles

```bash
make dev
make replica-test-env
make paper-env
```

Do not install project packages into system Python or use a virtual environment other than `nosql/` for this project.

# Architecture

## Processes

| process    | role |
| ---------- | ---- |
| `api`      | FastAPI control plane: auth, RBAC, run/approval commands, read models. Never holds a Mongo credential. |
| `worker`   | Durable PostgreSQL job worker. Composes the optimization pipeline per run (`app/worker/composition.py`), runs the telemetry collector, publishes a non-secret heartbeat. |
| `migrate`  | One-shot `alembic upgrade head` before `api`/`worker` start. |
| `frontend` | React operator console served by nginx, same-origin proxy to the API. |
| PostgreSQL | Authoritative state: runs, jobs, immutable artifacts, ledger, settings. |
| MongoDB ×2 | The monitored target and a separate sandbox evaluation replica. |

## Run lifecycle (worker)

Each stage persists one immutable artifact before the run advances; a crashed
or restarted worker resumes from PostgreSQL.

1. **Snapshot** — freeze the last 15 minutes of completed, literal-free telemetry (≤5 minutes old).
2. **Diagnose** (advisory) — the LLM may only reference snapshot shape/evidence IDs.
3. **Candidates** — deterministic equality→sort→range `CREATE_INDEX` for allowlisted namespaces.
4. **Rank** (advisory) — the LLM orders opaque handles; it cannot add or remove candidates.
5. **Calibrate** — freeze the top three and the benchmark profile (SMOKE for approval-controlled,
   AUTONOMOUS for full-autonomous runs).
6. **Sandbox trials** (`app/evaluation/executor.py`) — copy the collection (observer → sandbox,
   bounded by `EVALUATION_COPY_MAX_DOCUMENTS`), replay the snapshot's find shapes instantiated
   from sampled sandbox documents (`app/evaluation/replay.py`), and measure deterministic AB/BA
   pairs with an interleaved A/A baseline. Result sets are compared in both arms. Read-only
   production probe windows are captured as the monitoring baseline.
7. **Admission** — the frozen paired-bootstrap engine; primary metric must improve ≥5%, p95/p99
   (and write p95 when the workload writes) must not regress, any result mismatch rejects.
8. **Authority / approval** — autonomous only for admitted, autonomy-eligible `CREATE_INDEX`
   with non-`currentOp` telemetry in full-autonomous mode; otherwise a 15-minute, evidence-bound,
   four-eyes approval. If nobody decides in time, the worker completes the run as
   `APPROVAL_EXPIRED` without touching the target (`expire_approvals`, every
   `APPROVAL_EXPIRY_INTERVAL_SECONDS`).
9. **Deploy** — target lock, durable intent, idempotent `createIndex`, ledger entries.
10. **Monitor** — live probe windows vs. the pre-deployment baseline; catastrophic or sustained
    p99 regression, new errors, or replica trouble trigger ownership-verified rollback.

A blocked stage records a sanitized code in `jobs.last_error_code` (for example
`NO_COMPLETED_TELEMETRY`); after five attempts the job fails and an operator can resume it
once the cause is fixed (`POST /api/v1/runs/{id}/resume`).

## Telemetry collector (`app/telemetry/collector.py`)

Every `TELEMETRY_INTERVAL_SECONDS` for each active, non-sandbox target with an observer
credential: capability snapshot (on change), namespace discovery, then the first provider that
yields observations — `$queryStats` (cumulative counters differenced against a cursor),
an already-enabled profiler, the structured slow-query log, then `currentOp`. It never enables
profiling. Development MongoDB runs with `--slowms 5` so collection scans are logged.

## Operator API additions

| route | purpose |
| ----- | ------- |
| `GET /api/v1/overview` | counts, stalled runs, recent runs, target readiness, worker heartbeat |
| `GET /api/v1/targets/{id}` | readiness checklist, mapping, namespaces, capability, telemetry |
| `POST /api/v1/targets/{id}/evaluation-mapping` | map a distinct sandbox target |
| `POST /api/v1/targets/{id}/namespaces/{ns}/allowlist` | allow/exclude a collection |
| `GET /api/v1/connections` | worker-published connection labels and roles (no URIs) |
| `GET /api/v1/runs/{id}` | now includes `evidence`: shapes, findings, candidates, trials, admission details, ledger, AI calls, events, job |
| `POST /api/v1/runs/{id}/resume` | re-queue a stalled run's failed job |
| `GET /api/v1/approvals/inbox` | approvals with action, expiry, and whether the caller may decide |
| `GET /api/v1/ledger/verify` | per-target hash-chain verification |

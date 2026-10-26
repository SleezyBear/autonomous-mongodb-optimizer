# Production Readiness

## Remediation baseline

This document is the authoritative readiness record for the Production-Readiness
Remediation Plan. It supersedes claims of production completion in the original
phase plan. Historical tests remain useful evidence, but passing them does not
prove that the promised system operated end-to-end against real infrastructure.

| Evidence label | Meaning |
| --- | --- |
| `UNIT_PASS` | An isolated deterministic test passed. It may use fakes, callbacks, or in-memory state. |
| `INTEGRATION_PASS` | A concrete component boundary was exercised, such as a Docker service, migration, or frontend build. |
| `SYSTEM_PASS` | A registered no-mocks acceptance test completed an actual end-to-end workflow against applicable infrastructure. |

R24 now has a registered `SYSTEM_PASS` backed by a real no-mocks lifecycle
acceptance. Historical original phases retain their recorded evidence labels.
`scripts/check_system_pass_registry.py` enforces that every `SYSTEM_PASS` row,
including remediation-phase identifiers such as `R24`, has a registered system
acceptance test. Whole-project closure is not yet complete because R28 remains
outstanding.

## Remediation status

| Remediation phase | Status | Evidence |
| --- | --- | --- |
| R0 — truthful state | COMPLETE — UNIT_PASS | Historical claims are reclassified independently of `PHASE_STATUS.md`; the system-pass registry guard is tested. |
| R1 — Python/runtime lifecycle migration | COMPLETE — INTEGRATION_PASS | uv-managed CPython 3.12.14 x86_64, unchanged pinned requirements, 196 backend tests, static checks, and rebuilt API/worker images passed; system `python3` remains 3.14.6. |
| R2 — relational control-plane schema | COMPLETE — INTEGRATION_PASS | Corrective migration 0004 defines UUID-backed models, ownership FKs, unique/check constraints, and indexes; disposable PostgreSQL upgrade/downgrade/upgrade inspection passed. |
| R3 — real persistence repositories | COMPLETE — INTEGRATION_PASS | PostgreSQL repositories now cover every durable domain; state and a pending job survived rebuilt/restarted API and worker containers and were recovered through fresh repository engines. |
| R4 — production-grade authentication | COMPLETE — INTEGRATION_PASS | PostgreSQL-backed users and refresh-token sessions enforce explicit JWT configuration, rotation/revocation, user disablement, expiry/type rejection, five-attempt lockout, and persisted authentication audit events. |
| R5 — durable distributed authorization and approvals | COMPLETE — INTEGRATION_PASS | PostgreSQL transactions persist evidence-bound approvals and decisions with four-eyes enforcement; separate repository engines and restarted API/worker containers proved visibility and continued validity before expiry. |
| R6 — distributed target locking | COMPLETE — INTEGRATION_PASS | PostgreSQL session advisory locks derived solely from target IDs excluded a competing independent worker; authority was safely recoverable after release. |
| R7 — durable production ledger | COMPLETE — INTEGRATION_PASS | PostgreSQL ledger rows contain the required state/action and hash-chain evidence; restart verification passed and privileged disposable-row tampering was detected. |
| R8 — durable crash recovery and idempotency | COMPLETE — INTEGRATION_PASS | Persisted intent and `INTENT_RECORDED`/`EXECUTION_STARTED`/`EFFECT_OBSERVED`/`EXECUTION_CONFIRMED` state support restart recovery; an observed exact effect was confirmed without duplicate mutation. |
| R9 — complete API implementation | COMPLETE — INTEGRATION_PASS | Every required `/api/v1` group now exposes a paginated persisted resource route rather than a fabricated `not_configured` response; target reads are ownership-filtered and target creation is explicit. |
| R10 — correct metric semantics | COMPLETE — UNIT_PASS | Latency percentiles and throughput use successful operations only, failures remain independently counted, and cumulative server counters are differenced into canonical utilization and per-operation values. |
| R11 — telemetry correctness and privacy | COMPLETE — UNIT_PASS | MongoDB 8-shaped query-stats, profiler, current-op, and structured diagnostic-log records are normalized without literals; strict privacy uses keyed HMAC pseudonyms for identifiers and values. |
| R12 — real CommerceBench | COMPLETE — UNIT_PASS | Exact SMOKE, STANDARD, and PUBLICATION record-count contracts, mixed read/write workload shapes, generator version, and content-derived dataset fingerprints are defined; the SMOKE dataset materializes the required 34,500 records deterministically. |
| R13 — real MongoDB benchmark executor | COMPLETE — INTEGRATION_PASS | Real MongoDB 8 CommerceBench execution restores both AB/BA arms, verifies the environment, warms up and schedules mixed operations, records monotonic successful/error/timeout timing and counter deltas, and writes typed trial evidence. |
| R14 — statistical correctness | COMPLETE — UNIT_PASS | Pair sufficiency is enforced per protected metric; higher-is-better zero behavior is explicit; a three-observation minimum applies; and a shared-resample paired max-statistic bootstrap supplies simultaneous protected-family safety evidence. |
| R15 — real sandbox cloning and evaluation | COMPLETE — INTEGRATION_PASS | A concrete MongoDB copier physically clones documents and relevant indexes into the separate evaluation target, validates dataset/version/FCV/workload facts, and fails if monitored optimizer-managed state changes during copying. |
| R16 — schema-bound AI advisory contract | COMPLETE — UNIT_PASS | Versioned v1 diagnosis/ranking prompt assets, provider-supplied JSON schemas, invocation provenance (prompt version, model, sanitized input/hash, result, latency), fabricated-reference validation, and deterministic fallback on AI failure are covered by focused tests. |
| R17 — real pgvector experience store | COMPLETE — INTEGRATION_PASS | PostgreSQL pgvector experience records persist the complete action, workload, prediction, outcome, embedding-model, and rollback evidence; retrieval is action-family scoped and survives an engine restart. |
| R18 — real post-deployment monitor | COMPLETE — INTEGRATION_PASS | The monitor computes catastrophic and sustained p99 regression, errors, timeouts, replica health, and workload comparability from fixed telemetry windows; it persists literal-free evidence to PostgreSQL and invokes only ownership-safe rollback. |
| R19B — lifecycle persistence and migration safety | COMPLETE — INTEGRATION_PASS | `make r19b-acceptance` passed: 7 focused real-PostgreSQL lifecycle/worker tests, isolated fresh and legacy migration paths, legacy `RUNNING` refusal, safe downgrade, unsafe downgrade refusal, 233 backend tests, Ruff, mypy, and `pip check`. |
| R19C — durable worker infrastructure | COMPLETE — INTEGRATION_PASS | `make r19c-acceptance` passed: 19 focused worker tests, real PostgreSQL crash/lease-expiry recovery, idle and active-job shutdown, readiness/outage recovery, Docker worker health/shutdown/restart verification, R19B regression, 250 backend tests, Ruff, mypy, and `pip check`. The worker is real and durable, but safely terminal-fails non-terminal optimization jobs with `ORCHESTRATION_UNAVAILABLE` while orchestration is absent. |
| R19E — durable snapshot evidence | COMPLETE — INTEGRATION_PASS | Durable snapshot creation, persistence, target binding, and literal-free evidence are covered by the R19E regression chain. |
| R19F — durable grounded diagnosis | COMPLETE — INTEGRATION_PASS | Persisted diagnosis is grounded in immutable snapshot evidence; real Ollama diagnosis and currentOp non-autonomy integration passed. |
| R19G–K — candidates through statistical admission | COMPLETE — INTEGRATION_PASS | `make r19gk-acceptance` passed. The dedicated real-Ollama ordered gate passed (3 tests), durable real evaluation/trial persistence and frozen statistical admission passed, as did full backend regression (269 passed, 3 skipped), Ruff, mypy (98 source files), and `pip check`. |
| R19D — durable OptimizationRun orchestrator | COMPLETE — INTEGRATION_PASS | The durable worker/orchestrator lifecycle, approval continuation, deployment, monitoring, and rollback boundaries are covered by the accepted R19L–P evidence. |
| R19L–P — production lifecycle completion | COMPLETE — INTEGRATION_PASS | Durable lifecycle/approval/deployment/monitoring/rollback acceptance passed with the real-provider and full-regression evidence recorded for R19L–P. |
| R19Q–S — final API, frontend, and system integration | COMPLETE — INTEGRATION_PASS | Authenticated typed run/approval APIs, live frontend run screens, generated API contract, and final regression acceptance passed: API 20 tests, frontend 6 tests and production build, focused durable suite 72 tests, real-Ollama gate 3 tests, and 281 backend tests (3 intentional real-provider skips), plus Ruff, mypy (103 files), pip check, and `git diff --check`. |
| R19 — real optimization-run workflow | COMPLETE — INTEGRATION_PASS | All R19 slices are complete. The final API/frontend integration audit is recorded in `docs/R19_FINAL_INTEGRATION_AUDIT.md`; development PostgreSQL/Mongo data, snapshots, jobs, and Docker volumes were not reset or deleted. |
| R20 — infrastructure/runtime hardening | COMPLETE — INTEGRATION_PASS | Fail-closed production settings and secrets, explicit hosts/CORS/body bounds, PostgreSQL-backed readiness, structured/redacted metrics/logging, non-root read-only images, loopback development ports, frontend same-origin proxy/security headers, and graceful API/worker shutdown passed in `make r20r23-acceptance`. |
| R21 — supply-chain hardening | COMPLETE — INTEGRATION_PASS | 28 direct and 74 hash-locked Python packages reconstructed under CPython 3.12.14 x86_64; Node 22 `npm ci`/test/build passed; Python and npm audits reported zero vulnerabilities; CycloneDX backend/frontend SBOMs, reviewed secret classifications (zero real secrets), and provenance were generated. |
| R22 — PostgreSQL operational readiness | COMPLETE — INTEGRATION_PASS | Central bounded pool/timeouts, advisory-locked migrations, fresh and prior-head upgrades, concurrent runners, seven operational indexes, isolated backup/restore with ledger/job/index integrity, and pause/unpause recovery passed. |
| R23 — MongoDB operational hardening | COMPLETE — INTEGRATION_PASS | Central bounded sync/async clients, production TLS and topology policy, read-only probing, trusted/untrusted CA behavior, isolated executor least privilege, replica election recovery, and outage/restart durability passed. |
| R24 — true no-mock system lifecycle | COMPLETE — SYSTEM_PASS | Eight persisted real lifecycle paths passed with real PostgreSQL, MongoDB, Ollama, API, worker, admission, authority, deployment, monitoring, rollback, and real-stack Playwright 3/3. `artifacts/generated/r24-system.json` records `SYSTEM_PASS`; R24 is registered in `docs/SYSTEM_ACCEPTANCE_REGISTRY.json`. |
| R25 — adversarial/failure qualification | COMPLETE — INTEGRATION_PASS | The adversarial/security/statistical suite passed 129 tests, and the isolated three-member replica partition/election scenario passed majority-retained, election-recovery, majority-loss fail-closed, restoration, and exactly-once reconciliation checks. |
| R26 — concurrency/load/soak qualification | COMPLETE — INTEGRATION_PASS | Concurrency qualification passed 27 tests. The 60-second soak passed with 4,444 API samples, zero failures, p95 90.83 ms, bounded PostgreSQL/MongoDB connections, and no post-cleanup asyncio/client leak. |
| R27 — autonomy readiness | COMPLETE — INTEGRATION_PASS | Explicit autonomy/production/monitoring/experience/rollback/reversion recheck passed 46/46, including predeployment evidence binding, autonomy eligibility, mandatory monitoring, ownership-safe rollback, and experience non-authority. |
| R28 — research/reproducibility closure | NOT STARTED | CommerceBench/NoSQLBench publication runs, B0–B5 ablations, raw exports, figures/tables, manifests, and final reproducibility/final-acceptance closure remain. |
| R29 — operational wiring and operator console | IMPLEMENTED — INTEGRATION_PASS (PostgreSQL + in-memory Mongo double); real-MongoDB verification pending | The Compose worker now composes every stage per run from a worker-only connection file; a read-only telemetry collector, sandbox evaluator with replay and result-correctness checks, live monitoring probes, operator onboarding/read-model APIs, migration 0024 (overlapping snapshot observations), and the rebuilt operator console. `backend/tests/worker/test_production_pipeline.py` drives API → worker → approval → deployment → monitoring/rollback end to end. |

## Qualification rule

Production qualification requires completion of the remediation plan’s required
phases and a real, no-mocks system acceptance run. It cannot be inferred from
unit-test counts, a frontend rendering, a Compose health check, or this document.

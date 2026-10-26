PYTHON := ./nosql/bin/python
PIP := ./nosql/bin/python -m pip

.PHONY: user verify-local dev-workload preflight wheel-test phase0-acceptance phase-r0-acceptance phase-r1-acceptance phase-r2-acceptance phase-r3-acceptance phase-r4-acceptance phase-r5-acceptance phase-r6-acceptance phase-r7-acceptance phase-r8-acceptance phase-r9-acceptance phase-r10-acceptance phase-r11-acceptance phase-r12-acceptance phase-r13-acceptance phase-r14-acceptance phase-r15-acceptance phase-r16-acceptance phase-r17-acceptance phase-r18-acceptance r19b-acceptance r19c-docker-worker r19c-acceptance r19d-acceptance r19e-acceptance r19f-acceptance r19gk-real-ai r19gk-acceptance r19lp-acceptance r19-acceptance r20r23-acceptance r24r27-acceptance phase15-acceptance pip-check test dev replica-test-env paper-env acceptance nosqlbench-smoke safetybench phase43-acceptance phase44-acceptance phase45-acceptance phase46-acceptance phase47-acceptance phase48-acceptance phase49-acceptance phase50-acceptance phase51-acceptance phase52-acceptance phase53-acceptance phase54-acceptance phase55-acceptance demo-reset demo-start demo-seed demo-workload publication-repro generate-exports r28-invalidate r28-check r28-acceptance final-acceptance run-orchestrator run-supervisor

preflight:
	$(PIP) check
	$(PYTHON) scripts/verify_requirements.py
	$(PYTHON) scripts/check_python_dependencies.py
	$(PYTHON) scripts/verify_environment.py

wheel-test:
	rm -rf .dependency-wheel-test
	mkdir -p .dependency-wheel-test
	$(PYTHON) -m pip download --quiet --only-binary=:all: --dest .dependency-wheel-test -r requirements.txt

phase0-acceptance: wheel-test preflight
	$(PYTHON) -c 'import sys; assert sys.version_info[:2] == (3, 12)'
	$(PYTHON) -c 'import platform; assert platform.machine() == "x86_64"'

phase-r0-acceptance:
	$(PYTHON) scripts/check_system_pass_registry.py
	$(PYTHON) -m pytest backend/tests/tooling/test_system_pass_registry.py

phase-r1-acceptance:
	$(PIP) check
	$(PYTHON) scripts/check_python_dependencies.py
	$(PYTHON) scripts/verify_requirements.py
	$(PYTHON) scripts/verify_environment.py
	$(PYTHON) -m pytest backend/tests
	$(PYTHON) -m ruff check backend
	$(PYTHON) -m mypy backend/app
	$(PYTHON) -c 'import platform, sys; assert sys.version_info[:2] == (3, 12); assert platform.machine() == "x86_64"'

phase-r2-acceptance:
	$(PYTHON) -m pytest backend/tests/schema
	$(PYTHON) scripts/check_control_plane_schema.py

phase-r3-acceptance:
	$(PYTHON) -m pytest backend/tests/repositories backend/tests/worker/test_durable.py
	$(PYTHON) scripts/check_repository_restart.py write --state-file .r3-restart-state.json
	docker compose --profile light up -d --build api worker
	docker compose --profile light restart api worker
	docker compose --profile light up -d --wait api worker
	curl --fail --silent http://localhost:8000/health/ready > /dev/null
	$(PYTHON) scripts/check_repository_restart.py verify --state-file .r3-restart-state.json

phase-r4-acceptance:
	JWT_SIGNING_KEY='r4-test-signing-key-that-is-long-enough' docker compose --profile light up -d --wait postgres
	$(PYTHON) -m alembic -c backend/alembic.ini upgrade head
	JWT_SIGNING_KEY='r4-test-signing-key-that-is-long-enough' $(PYTHON) -m pytest backend/tests/auth/test_production_auth.py

phase-r5-acceptance:
	docker compose --profile light up -d --wait postgres
	$(PYTHON) -m alembic -c backend/alembic.ini upgrade head
	$(PYTHON) -m pytest backend/tests/approvals/test_durable_distributed.py
	docker compose --profile light up -d --build api worker
	docker compose --profile light restart api worker
	docker compose --profile light up -d --wait api worker

phase-r6-acceptance:
	docker compose --profile light up -d --wait postgres
	$(PYTHON) -m pytest backend/tests/production/test_distributed_target_lock.py

phase-r7-acceptance:
	docker compose --profile light up -d --wait postgres
	$(PYTHON) -m alembic -c backend/alembic.ini upgrade head
	$(PYTHON) -m pytest backend/tests/ledger/test_postgres_ledger.py

phase-r8-acceptance:
	docker compose --profile light up -d --wait postgres
	$(PYTHON) -m alembic -c backend/alembic.ini upgrade head
	$(PYTHON) -m pytest backend/tests/recovery/test_durable_idempotency.py

phase-r9-acceptance:
	JWT_SIGNING_KEY='r9-test-signing-key-that-is-long-enough' $(PYTHON) -m pytest backend/tests/api/test_real_routes.py

phase-r10-acceptance:
	$(PYTHON) -m pytest backend/tests/metrics/test_canonical_semantics.py

phase-r11-acceptance:
	$(PYTHON) -m pytest backend/tests/telemetry backend/tests/security/test_privacy.py

phase-r12-acceptance:
	$(PYTHON) -m pytest backend/tests/commercebench
	$(PYTHON) scripts/reset_commercebench.py --profile smoke

phase-r13-acceptance:
	docker compose --profile light up -d --wait mongo-evaluation
	$(PYTHON) -m pytest backend/tests/benchmarks/test_real_mongodb_executor.py
	$(PYTHON) scripts/run_commercebench_smoke.py

phase-r14-acceptance:
	$(PYTHON) -m pytest backend/tests/unit/admission

phase-r15-acceptance:
	docker compose --profile light up -d --wait mongo-monitored mongo-evaluation
	$(PYTHON) -m pytest backend/tests/evaluation
	$(PYTHON) scripts/verify_evaluation_copy.py

phase-r16-acceptance:
	$(PYTHON) -m pytest backend/tests/ai backend/tests/pipeline backend/tests/security/test_privacy.py
	$(PYTHON) -m ruff check backend/app/ai/provider.py backend/app/pipeline/diagnosis.py backend/tests/ai/test_provider.py backend/tests/pipeline/test_diagnosis.py

phase-r17-acceptance:
	docker compose --profile light up -d --wait postgres
	$(PYTHON) -m alembic -c backend/alembic.ini upgrade head
	$(PYTHON) -m pytest backend/tests/experience

phase-r18-acceptance:
	docker compose --profile light up -d --wait postgres
	$(PYTHON) -m alembic -c backend/alembic.ini upgrade head
	$(PYTHON) -m pytest backend/tests/monitoring

r19b-acceptance:
	docker compose --profile light up -d --wait postgres
	$(PYTHON) -m alembic -c backend/alembic.ini upgrade head
	$(PYTHON) -m pytest backend/tests/runs/test_lifecycle_persistence.py backend/tests/worker/test_durable.py
	$(PYTHON) scripts/check_r19b_migrations.py
	$(PYTHON) -m pytest backend/tests
	$(PYTHON) -m ruff check backend
	MYPYPATH=backend $(PYTHON) -m mypy backend/app
	$(PIP) check

r19c-docker-worker:
	$(PYTHON) scripts/check_r19c_docker_worker.py

r19c-acceptance:
	docker compose --profile light up -d --wait postgres
	docker compose --profile light stop worker
	$(PYTHON) -m alembic -c backend/alembic.ini upgrade head
	$(PYTHON) -m pytest backend/tests/worker
	$(MAKE) r19c-docker-worker
	docker compose --profile light stop worker
	$(MAKE) r19b-acceptance
	$(PYTHON) -m pytest backend/tests
	$(PYTHON) -m ruff check backend
	MYPYPATH=backend $(PYTHON) -m mypy backend/app
	$(PIP) check

r19d-acceptance:
	docker compose --profile light up -d --wait postgres
	$(PYTHON) -m alembic -c backend/alembic.ini upgrade head
	$(PYTHON) -m pytest backend/tests/runs/test_orchestrator_created.py
	$(MAKE) r19c-acceptance
	$(MAKE) r19b-acceptance
	$(PYTHON) -m pytest backend/tests
	$(PYTHON) -m ruff check backend
	MYPYPATH=backend $(PYTHON) -m mypy backend/app
	$(PIP) check

r19e-acceptance:
	docker compose --profile light up -d --wait postgres mongo-monitored
	$(PYTHON) -m alembic -c backend/alembic.ini upgrade head
	$(PYTHON) -m pytest backend/tests/workloads/test_durable_snapshot.py backend/tests/telemetry/test_provider_selection.py backend/tests/runs/test_orchestrator_created.py
	$(MAKE) r19d-acceptance
	$(MAKE) r19c-acceptance
	$(MAKE) r19b-acceptance
	$(PYTHON) -m pytest backend/tests
	$(PYTHON) -m ruff check backend
	MYPYPATH=backend $(PYTHON) -m mypy backend/app
	$(PIP) check

r19f-acceptance:
	docker compose --profile light up -d --wait postgres mongo-monitored
	$(PYTHON) -m alembic -c backend/alembic.ini upgrade head
	R19F_REAL_OLLAMA=1 $(PYTHON) -m pytest backend/tests/diagnosis backend/tests/workloads/test_durable_snapshot.py backend/tests/runs/test_orchestrator_created.py
	$(MAKE) r19e-acceptance
	$(MAKE) r19c-acceptance
	$(MAKE) r19b-acceptance
	$(PYTHON) -m pytest backend/tests
	$(PYTHON) -m ruff check backend
	MYPYPATH=backend $(PYTHON) -m mypy backend/app
	$(PIP) check

r19gk-real-ai:
	R19F_REAL_OLLAMA=1 OLLAMA_TIMEOUT_SECONDS=600 $(PYTHON) -m pytest backend/tests/diagnosis/test_real_ollama.py::test_real_ollama_persists_grounded_diagnosis backend/tests/diagnosis/test_real_ollama.py::test_real_currentop_snapshot_to_diagnosis_remains_non_autonomous backend/tests/ranking/test_real_ollama.py::test_real_ollama_persists_candidate_ranking

r19gk-acceptance:
	docker compose --profile light up -d --wait postgres mongo-monitored mongo-evaluation
	$(PYTHON) -m alembic -c backend/alembic.ini upgrade head
	$(PYTHON) -m pytest backend/tests/candidates backend/tests/ranking/test_durable_ranking.py backend/tests/evaluation backend/tests/unit/admission backend/tests/runs backend/tests/diagnosis/test_durable_diagnosis.py
	$(MAKE) r19gk-real-ai
	$(PYTHON) -m pytest backend/tests/diagnosis/test_durable_diagnosis.py backend/tests/workloads/test_durable_snapshot.py backend/tests/runs/test_orchestrator_created.py
	$(MAKE) r19e-acceptance
	$(MAKE) r19c-acceptance
	$(MAKE) r19b-acceptance
	$(PYTHON) -m pytest backend/tests
	$(PYTHON) -m ruff check backend
	MYPYPATH=backend $(PYTHON) -m mypy backend/app
	$(PIP) check

r19lp-acceptance:
	docker compose --profile light up -d --wait postgres mongo-monitored mongo-evaluation
	$(PYTHON) -m alembic -c backend/alembic.ini upgrade head
	DATABASE_URL='postgresql+asyncpg://control_plane:control_plane_dev_only@127.0.0.1:5432/r19lp_acceptance_guard' JWT_SIGNING_KEY='r19lp-acceptance-signing-key-that-is-long-enough' $(PYTHON) -m pytest backend/tests/production backend/tests/approvals backend/tests/worker backend/tests/monitoring backend/tests/recovery backend/tests/ledger
	$(PYTHON) scripts/check_r19b_migrations.py
	$(MAKE) r19gk-real-ai
	DATABASE_URL='postgresql+asyncpg://control_plane:control_plane_dev_only@127.0.0.1:5432/r19lp_acceptance_guard' JWT_SIGNING_KEY='r19lp-acceptance-signing-key-that-is-long-enough' $(PYTHON) -m pytest backend/tests
	$(PYTHON) -m ruff check backend
	MYPYPATH=backend $(PYTHON) -m mypy backend/app
	$(PIP) check
	git diff --check

r19-acceptance:
	docker compose --profile light up -d --wait postgres mongo-monitored mongo-evaluation
	$(PYTHON) -m alembic -c backend/alembic.ini upgrade head
	JWT_SIGNING_KEY='r19-api-acceptance-signing-key-that-is-long-enough' $(PYTHON) -m pytest backend/tests/api
	$(PYTHON) scripts/generate_openapi_types.py --check
	docker run --rm --platform linux/amd64 -v "$(CURDIR)/frontend:/app" -w /app node:22.14.0-alpine npm test
	docker run --rm --platform linux/amd64 -v "$(CURDIR)/frontend:/app" -w /app node:22.14.0-alpine npm run build
	$(MAKE) r19lp-acceptance
	git diff --check

r20r23-acceptance:
	bash scripts/run_r20r23_acceptance.sh

r24r27-acceptance:
	bash scripts/run_r24r27_acceptance.sh

phase15-acceptance:
	$(PYTHON) scripts/phase15_acceptance.py

pip-check:
	$(PIP) check

test:
	$(PYTHON) -m pytest backend/tests

dev:
	docker compose --profile light up -d --build

# Create a control-plane user: make user EMAIL=ops@example.com ROLE=OPERATOR
user:
	$(PYTHON) scripts/manage_users.py create --email "$(EMAIL)" --role "$(ROLE)"

# Seed commerce.orders on the local monitored MongoDB and drive a read workload.
verify-local:
	bash scripts/verify_local_stack.sh

dev-workload:
	$(PYTHON) scripts/dev_workload.py --seed --duration $(or $(DURATION),120)

replica-test-env:
	docker compose --profile replica-test up -d

paper-env:
	docker compose -f docker-compose.yml -f docker-compose.paper.yml --profile paper up -d

acceptance:
	$(PYTHON) scripts/verify_environment.py
	$(PIP) check
	$(PYTHON) scripts/check_python_dependencies.py
	$(PYTHON) scripts/check_makefile_python.py
	$(PYTHON) -m pytest backend/tests
	$(PYTHON) -m ruff check backend benchmarks scripts
	MYPYPATH=backend $(PYTHON) -m mypy backend/app benchmarks scripts/collect_hardware_manifest.py scripts/demo_mode.py scripts/check_makefile_python.py
	$(PYTHON) -m alembic -c backend/alembic.ini upgrade head
	$(PYTHON) -m pytest backend/tests/adapters
	$(PYTHON) -m pytest backend/tests/auth backend/tests/mongodb/test_executor_permissions.py backend/tests/production
	$(PYTHON) -m pytest backend/tests/unit/admission
	$(PYTHON) -m pytest backend/tests/ledger
	$(PYTHON) -m pytest backend/tests/security
	docker run --rm --platform linux/amd64 -v "$(CURDIR)/frontend:/app" -w /app node:22.14.0-alpine npm test
	docker compose --profile light up -d --build frontend
	docker run --rm --platform linux/amd64 --add-host host.docker.internal:host-gateway -v "$(CURDIR)/frontend:/app" -w /app mcr.microsoft.com/playwright:v1.47.1-jammy npx playwright test
	$(PYTHON) -m pytest backend/tests/commercebench
	$(MAKE) nosqlbench-smoke
	$(PYTHON) -m pytest backend/tests/safetybench
	$(PYTHON) -m pytest backend/tests/rollback backend/tests/reversion
	$(PYTHON) -m pytest backend/tests/ai
	$(MAKE) phase47-acceptance
	git diff --check

nosqlbench-smoke:
	docker compose --profile bench run --rm nosqlbench-smoke

safetybench:
	$(PYTHON) -m pytest backend/tests/safetybench

phase43-acceptance:
	$(PYTHON) -m pytest backend/tests/ablations

phase44-acceptance:
	$(PYTHON) -m pytest backend/tests/api
	$(PYTHON) scripts/generate_openapi_types.py --check

phase45-acceptance:
	docker run --rm --platform linux/amd64 -v "$(CURDIR)/frontend:/app" -w /app node:22.14.0-alpine npm test

phase46-acceptance:
	$(PYTHON) -m pytest backend/tests/ai

phase47-acceptance:
	$(PYTHON) scripts/check_database_portability.py
	$(PYTHON) -m pytest backend/tests/portability

phase48-acceptance:
	$(PYTHON) -m pytest backend/tests/security

phase49-acceptance:
	$(PYTHON) -m pytest backend/tests/recovery

phase50-acceptance:
	$(PYTHON) -m pytest backend/tests/resources

phase51-acceptance:
	$(PYTHON) scripts/collect_hardware_manifest.py
	$(PYTHON) -m pytest backend/tests/benchmarks/test_hardware.py backend/tests/benchmarks/test_runner.py

phase52-acceptance:
	$(PYTHON) -m pytest backend/tests/exports

demo-reset:
	$(PYTHON) scripts/demo_mode.py reset

demo-start:
	$(PYTHON) scripts/demo_mode.py start

demo-seed:
	$(PYTHON) scripts/demo_mode.py seed

demo-workload:
	$(PYTHON) scripts/demo_mode.py workload

phase53-acceptance:
	$(PYTHON) -m pytest backend/tests/demo

phase54-acceptance:
	$(PYTHON) -m pytest backend/tests/autonomy backend/tests/production

phase55-acceptance:
	$(PYTHON) -m pytest backend/tests/reversion

publication-repro:
	$(PYTHON) scripts/publication_repro.py

generate-exports:
	$(PYTHON) scripts/generate_exports.py

r28-invalidate:
	@echo "Mark pre-fix R28 evidence INVALID_NONAUTHORITATIVE_R28_RUN (never delete)."
	$(PYTHON) scripts/invalidate_r28_evidence.py
	@echo "Mark superseded r28a PUBLICATION evidence INVALID_SUPERSEDED_RESET_IMPLEMENTATION (never delete)."
	$(PYTHON) scripts/invalidate_r28a_publication.py

r28-check:
	$(PYTHON) scripts/check_r28_authoritative_evidence.py

r28-acceptance: r28-invalidate
	@echo "R28 acceptance: fail-closed verification of authoritative evidence and tests."
	$(PYTHON) scripts/verify_r28_preflight.py
	$(PYTHON) -m pytest backend/tests/r28 -q
	$(MAKE) publication-repro
	$(MAKE) r28-check
	$(PYTHON) -m pytest backend/tests -q
	$(PYTHON) -m ruff check backend benchmarks scripts
	MYPYPATH=backend $(PYTHON) -m mypy backend/app benchmarks scripts/experiment_orchestrator.py scripts/run_commercebench_profile.py scripts/publication_repro.py scripts/check_r28_authoritative_evidence.py scripts/generate_exports.py scripts/benchmark_reset.py scripts/invalidate_r28a_publication.py
	$(PIP) check

final-acceptance:
	@echo "Final acceptance composes prior evidence without rerunning expensive system-pass lifecycles."
	$(MAKE) r28-acceptance
	$(MAKE) generate-exports
	@echo "final acceptance required"

run-orchestrator:
	@echo "Run authoritative experiment orchestrator (long-running, resumable, project Python)."
	$(PYTHON) scripts/experiment_orchestrator.py --mongo-uri "mongodb://control_plane_root:control_plane_root_dev_only@127.0.0.1:27018/?authSource=admin&directConnection=true" --run-safetybench --run-nosqlbench --run-standard --run-publication

run-supervisor:
	@echo "Start supervisor that launches follow-up orchestrator when commercebench finishes"
	nohup $(PYTHON) scripts/supervisor.py > artifacts/experiments/supervisor.log 2>&1 &

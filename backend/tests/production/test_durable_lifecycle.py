"""Isolated PostgreSQL coverage for the R19L-P durable index lifecycle."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from pymongo import AsyncMongoClient
from pymongo.errors import OperationFailure

from app.adapters.contracts import IndexSpec, Namespace
from app.adapters.fake import FakeDatabaseAdapter
from app.adapters.mongodb import MongoDBAdapter
from app.approvals.durable import DurableApprovalRequired, DurableApprovalService
from app.authority.durable import AUTONOMY_INELIGIBLE_REQUIRES_APPROVAL, DurableAuthorityService
from app.autonomy.readiness import authority_integrity_fingerprint, environment_binding
from app.metrics.collector import MetricSnapshot
from app.monitoring.durable import DurableMonitoringService
from app.monitoring.post_deployment import MonitoringWindow
from app.production.durable import DurableDeploymentError, DurableDeploymentService
from app.rollback.durable import DurableRollbackService
from app.runs.orchestrator import OptimizationRunOrchestrator
from app.worker.durable import ExecutionContext
from app.workloads.snapshots import Share


async def _prepared_run(engine: object, *, approval_controlled: bool = False, admitted: bool = False, autonomy_eligible: bool = True, database: str = "isolated") -> tuple[UUID, UUID, UUID]:
    """Create the minimum real immutable evidence chain for an approved index."""
    ids = [uuid4() for _ in range(13)]
    user, target, window, snapshot, namespace, shape, run, candidate, action, invocation, diagnosis, generation, ranking = ids
    evaluation_plan, admission, authority = uuid4(), uuid4(), uuid4()
    now = datetime.now(timezone.utc)
    async with engine.begin() as connection:  # type: ignore[union-attr]
        await connection.execute(text("INSERT INTO users (id,created_at,updated_at,email,password_hash,role,status,failed_login_count) VALUES (:id,:now,:now,:email,'hash','OPERATOR','ACTIVE',0)"), {"id": user, "now": now, "email": f"r19lp-{user.hex}@example.test"})
        mode = "APPROVAL_CONTROLLED" if approval_controlled else "FULL_AUTONOMOUS"
        run_status = "ADMITTED" if admitted else "APPROVAL_PENDING" if approval_controlled else "APPROVED"
        await connection.execute(text("INSERT INTO targets (id,created_at,updated_at,owner_user_id,name,deployment_mode,state,connection_label,is_active) VALUES (:id,:now,:now,:owner,:name,:mode,'ACTIVE','isolated',true)"), {"id": target, "now": now, "owner": user, "name": f"target-{target.hex}", "mode": mode})
        await connection.execute(text("INSERT INTO telemetry_windows (id,created_at,updated_at,target_id,started_at,ended_at,source,status) VALUES (:id,:now,:now,:target,:now,:now,'test','COMPLETE')"), {"id": window, "now": now, "target": target})
        await connection.execute(text("INSERT INTO workload_snapshots (id,created_at,updated_at,target_id,telemetry_window_id,snapshot,fingerprint,completeness) VALUES (:id,:now,:now,:target,:window,'{}',:fingerprint,CAST(:completeness AS jsonb))"), {"id": snapshot, "now": now, "target": target, "window": window, "fingerprint": f"snapshot-{snapshot.hex}", "completeness": '{"production_autonomy_eligible":true}' if autonomy_eligible else '{"production_autonomy_eligible":false}'})
        await connection.execute(text("INSERT INTO namespaces (id,created_at,updated_at,target_id,name,allowlisted) VALUES (:id,:now,:now,:target,'orders',true)"), {"id": namespace, "now": now, "target": target})
        await connection.execute(text("INSERT INTO query_shapes (id,created_at,updated_at,namespace_id,shape_hash,normalized_shape,operation) VALUES (:id,:now,:now,:namespace,:hash,'{}','find')"), {"id": shape, "now": now, "namespace": namespace, "hash": f"shape-{shape.hex}"})
        await connection.execute(text("INSERT INTO optimization_runs (id,created_at,updated_at,target_id,workload_snapshot_id,status,requested_by_user_id,deployment_mode,primary_metric_key) VALUES (:id,:now,:now,:target,:snapshot,CAST(:status AS run_status),:user,:mode,'p99')"), {"id": run, "now": now, "target": target, "snapshot": snapshot, "user": user, "status": run_status, "mode": mode})
        await connection.execute(text("INSERT INTO candidates (id,created_at,updated_at,optimization_run_id,query_shape_id,status,candidate_hash,source_snapshot_id,deterministic_fingerprint,policy_classification) VALUES (:id,:now,:now,:run,:shape,'ADMITTED',:hash,:snapshot,:fingerprint,'AUTO_ELIGIBLE_AFTER_ADMISSION')"), {"id": candidate, "now": now, "run": run, "shape": shape, "hash": f"candidate-hash-{candidate.hex}", "snapshot": snapshot, "fingerprint": f"candidate-fingerprint-{candidate.hex}"})
        await connection.execute(text("INSERT INTO candidate_actions (id,created_at,updated_at,candidate_id,action_type,action_payload,reversible) VALUES (:id,:now,:now,:candidate,'CREATE_INDEX',CAST(:payload AS jsonb),true)"), {"id": action, "now": now, "candidate": candidate, "payload": f'{{"action_type":"CREATE_INDEX","database":"{database}","collection":"orders","index_name":"optimizer_status","fields":[{{"field":"status","direction":1}}],"unique":false,"expire_after_seconds":null,"sparse":false,"partial_filter_expression":null,"text":false,"wildcard":false,"geo":false,"hashed":false}}'})
        await connection.execute(text("INSERT INTO ai_invocations (id,created_at,updated_at,optimization_run_id,stage,provider,model,prompt_version,schema_version,workload_snapshot_id,input_hash,sanitized_input,status,attempt_number) VALUES (:id,:now,:now,:run,'RANKING','test','test','v1','v1',:snapshot,:hash,'{}','COMPLETED',1)"), {"id": invocation, "now": now, "run": run, "snapshot": snapshot, "hash": f"input-{invocation.hex}"})
        await connection.execute(text("INSERT INTO diagnosis_artifacts (id,created_at,updated_at,optimization_run_id,workload_snapshot_id,target_id,ai_invocation_id,schema_version,source_snapshot_fingerprint,deterministic_evidence_hash,artifact_fingerprint) VALUES (:id,:now,:now,:run,:snapshot,:target,:invocation,'v1',:source,:evidence,:fingerprint)"), {"id": diagnosis, "now": now, "run": run, "snapshot": snapshot, "target": target, "invocation": invocation, "source": f"snapshot-{snapshot.hex}", "evidence": f"evidence-{diagnosis.hex}", "fingerprint": f"diagnosis-{diagnosis.hex}"})
        await connection.execute(text("INSERT INTO candidate_generation_artifacts (id,created_at,updated_at,optimization_run_id,workload_snapshot_id,diagnosis_artifact_id,generator_version,candidate_count,ordered_candidate_fingerprints,artifact_fingerprint,completed_at) VALUES (:id,:now,:now,:run,:snapshot,:diagnosis,'test',1,CAST(:ordered AS jsonb),:fingerprint,:now)"), {"id": generation, "now": now, "run": run, "snapshot": snapshot, "diagnosis": diagnosis, "ordered": f'["candidate-fingerprint-{candidate.hex}"]', "fingerprint": f"generation-{generation.hex}"})
        await connection.execute(text("INSERT INTO candidate_ranking_artifacts (id,created_at,updated_at,optimization_run_id,generation_artifact_id,ai_invocation_id,ordered_candidate_ids,artifact_fingerprint) VALUES (:id,:now,:now,:run,:generation,:invocation,CAST(:ordered AS jsonb),:fingerprint)"), {"id": ranking, "now": now, "run": run, "generation": generation, "invocation": invocation, "ordered": f'["{candidate}"]', "fingerprint": f"ranking-{ranking.hex}"})
        await connection.execute(text("INSERT INTO evaluation_plans (id,created_at,updated_at,optimization_run_id,ranking_artifact_id,profile,selected_candidate_ids,calibration,artifact_fingerprint) VALUES (:id,:now,:now,:run,:ranking,'SMOKE',CAST(:selected AS jsonb),'{}',:fingerprint)"), {"id": evaluation_plan, "now": now, "run": run, "ranking": ranking, "selected": f'["{candidate}"]', "fingerprint": f"plan-{evaluation_plan.hex}"})
        admission_production_eligible = not approval_controlled
        await connection.execute(text("INSERT INTO admission_artifacts (id,created_at,updated_at,optimization_run_id,evaluation_plan_id,selected_candidate_id,admitted_candidate_ids,production_eligible,artifact_fingerprint) VALUES (:id,:now,:now,:run,:plan,:candidate,CAST(:admitted AS jsonb),:production_eligible,:fingerprint)"), {"id": admission, "now": now, "run": run, "plan": evaluation_plan, "candidate": candidate, "admitted": f'["{candidate}"]', "production_eligible": admission_production_eligible, "fingerprint": f"admission-{admission.hex}"})
        if not admitted:
            authority_type = "HUMAN_REQUIRED" if approval_controlled else "AUTONOMOUS"
            reason = "AUTONOMY_INELIGIBLE_REQUIRES_APPROVAL" if approval_controlled else "AUTONOMY_ELIGIBLE"
            target_identity, capability_fingerprint, security_fingerprint = environment_binding(
                {"id": target, "connection_label": "isolated", "deployment_mode": mode, "state": "ACTIVE", "is_active": True}, None, None
            )
            authority_fingerprint = authority_integrity_fingerprint(
                run, candidate, f"candidate-fingerprint-{candidate.hex}", mode, authority_type,
                reason, autonomy_eligible, admission_production_eligible,
                target_identity, capability_fingerprint, security_fingerprint,
            )
            await connection.execute(text("INSERT INTO authority_decisions (id,created_at,updated_at,optimization_run_id,candidate_id,candidate_fingerprint,deployment_mode,authority_type,authority_reason,production_autonomy_eligible,integrity_fingerprint) VALUES (:id,:now,:now,:run,:candidate,:candidate_fingerprint,:mode,:authority_type,:reason,:eligible,:fingerprint)"), {"id": authority, "now": now, "run": run, "candidate": candidate, "candidate_fingerprint": f"candidate-fingerprint-{candidate.hex}", "fingerprint": authority_fingerprint, "mode": mode, "authority_type": authority_type, "reason": reason, "eligible": autonomy_eligible})
    return run, target, candidate


@pytest.mark.asyncio
async def test_durable_create_index_is_real_transactional_and_idempotent(disposable_worker_database: str) -> None:
    engine = create_async_engine(disposable_worker_database)
    adapter = FakeDatabaseAdapter((Namespace("orders"),))
    run, target, _candidate = await _prepared_run(engine)
    context = ExecutionContext(asyncio.Event())
    try:
        service = DurableDeploymentService(engine, adapter)
        deployed = await service.deploy_for_run(run, context)
        assert deployed["status"] == "APPLIED"
        assert [index.name for index in await adapter.list_indexes(Namespace("orders"))] == ["optimizer_status"]
        # Simulate recovery on an already applied target state; no duplicate index
        # mutation occurs and the run remains at its authoritative deployed state.
        recovered = await service.deploy_for_run(run, context)
        assert recovered["id"] == deployed["id"]
        async with engine.connect() as connection:
            assert (await connection.scalar(text("SELECT status::text FROM optimization_runs WHERE id=:id"), {"id": run})) == "DEPLOYED"
            assert (await connection.scalar(text("SELECT count(*) FROM deployment_artifacts WHERE optimization_run_id=:id"), {"id": run})) == 1
            assert (await connection.scalar(text("SELECT count(*) FROM production_ledger_entries WHERE target_id=:id"), {"id": target})) == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_durable_rollback_removes_only_owned_exact_index_and_blocks_drift(disposable_worker_database: str) -> None:
    engine = create_async_engine(disposable_worker_database)
    adapter = FakeDatabaseAdapter((Namespace("orders"),))
    run, _target, _candidate = await _prepared_run(engine)
    context = ExecutionContext(asyncio.Event())
    try:
        await DurableDeploymentService(engine, adapter).deploy_for_run(run, context)
        rollback = DurableRollbackService(engine, adapter)
        status, reason = await rollback.rollback_for_run(run)
        assert status.value == "ROLLED_BACK" and reason == "ROLLBACK_APPLIED", reason
        assert await adapter.list_indexes(Namespace("orders")) == ()
        # A recovered rollback job is an idempotent read of durable factual
        # evidence; it must not reinterpret the now-absent index as drift.
        repeated_status, repeated_reason = await rollback.rollback_for_run(run)
        assert repeated_status.value == "ROLLED_BACK" and repeated_reason == "ROLLBACK_APPLIED"
        # A second deployment sees a human replacement of its owned name with a
        # different key. That resource is not claimed and is never dropped.
        drifted_run, _second_target, _second_candidate = await _prepared_run(engine)
        await DurableDeploymentService(engine, adapter).deploy_for_run(drifted_run, context)
        await adapter.drop_index(Namespace("orders"), "optimizer_status")
        await adapter.create_index(Namespace("orders"), IndexSpec("optimizer_status", (("human", 1),)))
        status, reason = await rollback.rollback_for_run(drifted_run)
        assert status.value == "ROLLBACK_BLOCKED" and reason in {"INDEX_SPEC_MISMATCH", "RELEVANT_DRIFT_DETECTED"}
        assert [item.name for item in await adapter.list_indexes(Namespace("orders"))] == ["optimizer_status"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_deployed_run_persists_measured_monitoring_then_completes(disposable_worker_database: str) -> None:
    engine = create_async_engine(disposable_worker_database)
    adapter = FakeDatabaseAdapter((Namespace("orders"),))
    run, _target, _candidate = await _prepared_run(engine)
    context = ExecutionContext(asyncio.Event())
    try:
        deployment = DurableDeploymentService(engine, adapter)
        await deployment.deploy_for_run(run, context)

        def window() -> MonitoringWindow:
            return MonitoringWindow(
                MetricSnapshot(10, 20, 30, 100, 80, 20, 1, 2, 3, 4, 5, 0, 10, 9, 8, 7, 0, 0),
                (Share("shape", 1.0),),
            )

        async def measured(_run_id: UUID) -> tuple[tuple[MonitoringWindow, ...], tuple[MonitoringWindow, ...]]:
            return (window(),) * 10, (window(),)

        monitoring = DurableMonitoringService(engine, measured, DurableRollbackService(engine, adapter))
        orchestrator = OptimizationRunOrchestrator(engine, deployment_service=deployment, monitoring_executor=monitoring.monitor_for_run)
        # Simulate a crash after measured evidence was committed but before the
        # run's MONITORING/terminal transition. Recovery may recollect evidence
        # but must not duplicate the factual experience outcome.
        assert (await monitoring.monitor_for_run(run)).status.value == "STABLE"
        assert (await orchestrator.run(run, context)).status.value == "MONITORING"
        assert (await orchestrator.run(run, context)).status.value == "COMPLETED"
        async with engine.connect() as connection:
            assert (await connection.scalar(text("SELECT completion_reason::text FROM optimization_runs WHERE id=:id"), {"id": run})) == "DEPLOYMENT_SUCCEEDED"
            assert (await connection.scalar(text("SELECT count(*) FROM audit_events WHERE optimization_run_id=:id AND event_type='DURABLE_POST_DEPLOYMENT_MONITORING'"), {"id": run})) == 2
            assert (await connection.scalar(text("SELECT count(*) FROM experience_records WHERE candidate_id IN (SELECT candidate_id FROM deployment_artifacts WHERE optimization_run_id=:id)"), {"id": run})) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_measured_regression_triggers_owned_durable_rollback(disposable_worker_database: str) -> None:
    engine = create_async_engine(disposable_worker_database)
    adapter = FakeDatabaseAdapter((Namespace("orders"),))
    run, _target, _candidate = await _prepared_run(engine)
    context = ExecutionContext(asyncio.Event())
    try:
        deployment = DurableDeploymentService(engine, adapter)
        await deployment.deploy_for_run(run, context)

        def window(p99: float) -> MonitoringWindow:
            return MonitoringWindow(
                MetricSnapshot(10, 20, p99, 100, 80, 20, 1, 2, 3, 4, 5, 0, 10, 9, 8, 7, 0, 0),
                (Share("shape", 1.0),),
            )

        async def measured(_run_id: UUID) -> tuple[tuple[MonitoringWindow, ...], tuple[MonitoringWindow, ...]]:
            return (window(30),) * 10, (window(90),)

        monitoring = DurableMonitoringService(engine, measured, DurableRollbackService(engine, adapter))
        orchestrator = OptimizationRunOrchestrator(engine, deployment_service=deployment, monitoring_executor=monitoring.monitor_for_run)
        assert (await orchestrator.run(run, context)).status.value == "MONITORING"
        assert (await orchestrator.run(run, context)).status.value == "ROLLED_BACK"
        assert await adapter.list_indexes(Namespace("orders")) == ()
        async with engine.connect() as connection:
            assert await connection.scalar(
                text("SELECT count(*) FROM rollback_records WHERE candidate_id IN (SELECT candidate_id FROM deployment_artifacts WHERE optimization_run_id=:id)"),
                {"id": run},
            ) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_approval_grant_is_four_eyes_and_creates_exactly_one_continuation(disposable_worker_database: str) -> None:
    engine = create_async_engine(disposable_worker_database)
    run, target, candidate = await _prepared_run(engine, approval_controlled=True)
    try:
        async with engine.begin() as connection:
            requester = await connection.scalar(text("SELECT requested_by_user_id FROM optimization_runs WHERE id=:id"), {"id": run})
            action_id = await connection.scalar(text("SELECT id FROM candidate_actions WHERE candidate_id=:id"), {"id": candidate})
            approver = uuid4()
            now = datetime.now(timezone.utc)
            await connection.execute(text("INSERT INTO users (id,created_at,updated_at,email,password_hash,role,status,failed_login_count) VALUES (:id,:now,:now,:email,'hash','APPROVER','ACTIVE',0)"), {"id": approver, "now": now, "email": f"approver-{approver.hex}@example.test"})
            await connection.execute(
                text(
                    "INSERT INTO jobs "
                    "(id,created_at,updated_at,payload,status,attempts,optimization_run_id,kind,completed_at) "
                    "VALUES (:id,:now,:now,CAST(:payload AS jsonb),'COMPLETED',1,:run,'OPTIMIZATION',:now)"
                ),
                {
                    "id": uuid4(),
                    "now": now,
                    "payload": f'{{"run_id":"{run}"}}',
                    "run": run,
                },
            )
        service = DurableApprovalService(engine)
        request = await service.request(action_id=str(action_id), target_id=target, candidate_id=candidate, candidate_fingerprint=f"candidate-fingerprint-{candidate.hex}", evidence_hash="authority-bound", requester_id=requester, optimization_run_id=run)
        with pytest.raises(DurableApprovalRequired):
            await service.approve_and_enqueue_continuation(request["id"], requester)
        approved = await service.approve_and_enqueue_continuation(request["id"], approver, "p99 gain verified in sandbox")
        assert approved["status"] == "APPROVED"
        adapter = FakeDatabaseAdapter((Namespace("orders"),))
        await DurableDeploymentService(engine, adapter).deploy_for_run(run, ExecutionContext(asyncio.Event()))
        assert [item.name for item in await adapter.list_indexes(Namespace("orders"))] == ["optimizer_status"]
        async with engine.connect() as connection:
            assert (await connection.scalar(text("SELECT status::text FROM optimization_runs WHERE id=:id"), {"id": run})) == "DEPLOYED"
            assert (await connection.scalar(text("SELECT count(*) FROM jobs WHERE optimization_run_id=:id AND payload ->> 'continuation'='true'"), {"id": run})) == 1
            assert (await connection.scalar(text("SELECT count(*) FROM jobs WHERE optimization_run_id=:id"), {"id": run})) == 2
            assert (await connection.scalar(text("SELECT reason FROM approval_decisions WHERE approval_request_id=:id"), {"id": request["id"]})) == "p99 gain verified in sandbox"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_approval_rejection_completes_without_continuation_or_mutation(disposable_worker_database: str) -> None:
    engine = create_async_engine(disposable_worker_database)
    run, target, candidate = await _prepared_run(engine, approval_controlled=True)
    try:
        async with engine.begin() as connection:
            requester = await connection.scalar(text("SELECT requested_by_user_id FROM optimization_runs WHERE id=:id"), {"id": run})
            action_id = await connection.scalar(text("SELECT id FROM candidate_actions WHERE candidate_id=:id"), {"id": candidate})
            approver = uuid4()
            now = datetime.now(timezone.utc)
            await connection.execute(text("INSERT INTO users (id,created_at,updated_at,email,password_hash,role,status,failed_login_count) VALUES (:id,:now,:now,:email,'hash','APPROVER','ACTIVE',0)"), {"id": approver, "now": now, "email": f"rejector-{approver.hex}@example.test"})
        service = DurableApprovalService(engine)
        request = await service.request(action_id=str(action_id), target_id=target, candidate_id=candidate, candidate_fingerprint=f"candidate-fingerprint-{candidate.hex}", evidence_hash="authority-bound", requester_id=requester, optimization_run_id=run)
        rejected = await service.reject_and_complete_run(request["id"], approver, "not now")
        assert rejected["status"] == "REJECTED"
        async with engine.connect() as connection:
            row = (await connection.execute(text("SELECT status::text,completion_reason::text FROM optimization_runs WHERE id=:id"), {"id": run})).mappings().one()
            assert row["status"] == "COMPLETED" and row["completion_reason"] == "APPROVAL_REJECTED"
            assert (await connection.scalar(text("SELECT count(*) FROM jobs WHERE optimization_run_id=:id"), {"id": run})) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_authority_auto_path_requires_explicit_eligible_evidence(disposable_worker_database: str) -> None:
    engine = create_async_engine(disposable_worker_database)
    try:
        eligible_run, _target, _candidate = await _prepared_run(engine, admitted=True, autonomy_eligible=True)
        automatic = await DurableAuthorityService(engine).decide_for_run(eligible_run)
        assert automatic.automatic and automatic.decision["authority_type"] == "AUTONOMOUS"

        currentop_run, _target, _candidate = await _prepared_run(engine, admitted=True, autonomy_eligible=False)
        fallback = await DurableAuthorityService(engine).decide_for_run(currentop_run)
        assert not fallback.automatic
        assert fallback.decision["authority_type"] == "HUMAN_REQUIRED"
        assert fallback.decision["authority_reason"] == AUTONOMY_INELIGIBLE_REQUIRES_APPROVAL
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_isolated_mongo_create_index_and_owned_rollback(disposable_worker_database: str) -> None:
    """Use a unique test namespace, never the evaluation database or app data."""
    uri = os.environ.get(
        "R19LP_PRODUCTION_TEST_MONGODB_URI",
        "mongodb://control_plane_root:control_plane_root_dev_only@127.0.0.1:27017/admin?authSource=admin&directConnection=true",
    )
    engine = create_async_engine(disposable_worker_database)
    client: AsyncMongoClient[object] = AsyncMongoClient(uri, serverSelectionTimeoutMS=5_000)
    database = f"r19lp_production_test_{uuid4().hex}"
    collection = client.get_database(database).get_collection("orders")
    document_id = uuid4().hex
    try:
        await collection.insert_one({"_id": document_id, "status": "test"})
        # This human index predates deployment. It is present in the durable
        # before/after evidence and must survive optimizer rollback unchanged.
        await collection.create_index([("human_reference", 1)], name="human_reference")
        run, _target, _candidate = await _prepared_run(engine, database=database)
        adapter = MongoDBAdapter(client, database)
        await DurableDeploymentService(engine, adapter).deploy_for_run(run, ExecutionContext(asyncio.Event()))
        indexes = [item async for item in await collection.list_indexes()]
        optimizer = next(item for item in indexes if item["name"] == "optimizer_status")
        assert list(optimizer["key"].items()) == [("status", 1)]
        assert await collection.count_documents({"_id": document_id}) == 1
        status, reason = await DurableRollbackService(engine, adapter).rollback_for_run(run)
        assert status.value == "ROLLED_BACK" and reason == "ROLLBACK_APPLIED"
        remaining = {item["name"] for item in [item async for item in await collection.list_indexes()]}
        assert "optimizer_status" not in remaining and "human_reference" in remaining
        assert await collection.count_documents({"_id": document_id}) == 1
    finally:
        # Clean up precisely the test document and human index this test created.
        await collection.delete_one({"_id": document_id})
        try:
            await collection.drop_index("human_reference")
        except OperationFailure:
            pass
        await client.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_crash_after_external_effect_recovers_without_duplicate_mutation(disposable_worker_database: str) -> None:
    class CountingAdapter(FakeDatabaseAdapter):
        def __init__(self) -> None:
            super().__init__((Namespace("orders"),))
            self.creates = 0

        async def create_index(self, namespace: Namespace, index: IndexSpec) -> None:
            self.creates += 1
            await super().create_index(namespace, index)

    engine = create_async_engine(disposable_worker_database)
    run, _target, _candidate = await _prepared_run(engine)
    adapter = CountingAdapter()
    context = ExecutionContext(asyncio.Event())
    try:
        crashing = DurableDeploymentService(engine, adapter)

        async def crash_after_effect(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("simulated crash after MongoDB success")

        crashing._mark_applied = crash_after_effect  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="simulated crash"):
            await crashing.deploy_for_run(run, context)
        assert adapter.creates == 1
        recovered = DurableDeploymentService(engine, adapter)
        await recovered.deploy_for_run(run, context)
        assert adapter.creates == 1
        async with engine.connect() as connection:
            assert (await connection.scalar(text("SELECT status FROM deployment_artifacts WHERE optimization_run_id=:id"), {"id": run})) == "APPLIED"
            assert (await connection.scalar(text("SELECT status::text FROM optimization_runs WHERE id=:id"), {"id": run})) == "DEPLOYED"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["target", "capability", "credential"])
async def test_predeployment_environment_drift_fails_before_mutation(
    disposable_worker_database: str, drift: str
) -> None:
    engine = create_async_engine(disposable_worker_database)
    run, target, _candidate = await _prepared_run(engine)
    adapter = FakeDatabaseAdapter((Namespace("orders"),))
    now = datetime.now(timezone.utc)
    try:
        async with engine.begin() as connection:
            if drift == "target":
                await connection.execute(
                    text("UPDATE targets SET connection_label='changed' WHERE id=:id"),
                    {"id": target},
                )
            elif drift == "capability":
                await connection.execute(
                    text(
                        "INSERT INTO capability_snapshots "
                        "(id,created_at,updated_at,target_id,capabilities,fingerprint) "
                        "VALUES (:id,:now,:now,:target,CAST(:capabilities AS jsonb),:fingerprint)"
                    ),
                    {
                        "id": uuid4(),
                        "now": now,
                        "target": target,
                        "capabilities": '{"create_index":true}',
                        "fingerprint": "changed-capability",
                    },
                )
            else:
                await connection.execute(
                    text(
                        "INSERT INTO target_credentials "
                        "(id,created_at,updated_at,target_id,encrypted_payload,key_version,credential_type) "
                        "VALUES (:id,:now,:now,:target,:payload,2,'mongodb')"
                    ),
                    {"id": uuid4(), "now": now, "target": target, "payload": b"opaque"},
                )
        with pytest.raises(DurableDeploymentError, match="PREDEPLOYMENT_EVIDENCE_DRIFT"):
            await DurableDeploymentService(engine, adapter).deploy_for_run(
                run, ExecutionContext(asyncio.Event())
            )
        assert await adapter.list_indexes(Namespace("orders")) == ()
        async with engine.connect() as connection:
            assert await connection.scalar(
                text(
                    "SELECT count(*) FROM deployment_artifacts "
                    "WHERE optimization_run_id=:id"
                ),
                {"id": run},
            ) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_expired_approval_completes_run_without_mutation(disposable_worker_database: str) -> None:
    engine = create_async_engine(disposable_worker_database)
    run, target, candidate = await _prepared_run(engine, approval_controlled=True)
    try:
        async with engine.begin() as connection:
            requester = await connection.scalar(text("SELECT requested_by_user_id FROM optimization_runs WHERE id=:id"), {"id": run})
            action_id = await connection.scalar(text("SELECT id FROM candidate_actions WHERE candidate_id=:id"), {"id": candidate})
        service = DurableApprovalService(engine)
        request = await service.request(action_id=str(action_id), target_id=target, candidate_id=candidate, candidate_fingerprint=f"candidate-fingerprint-{candidate.hex}", evidence_hash="authority-bound", requester_id=requester, optimization_run_id=run)
        assert await service.expire_overdue() == []  # still inside the window
        later = request["expires_at"] + timedelta(seconds=1)
        assert await service.expire_overdue(now=later) == [run]
        assert await service.expire_overdue(now=later) == []  # idempotent
        async with engine.connect() as connection:
            assert (await connection.scalar(text("SELECT status FROM approval_requests WHERE id=:id"), {"id": request["id"]})) == "EXPIRED"
            row = (await connection.execute(text("SELECT status::text, completion_reason::text FROM optimization_runs WHERE id=:id"), {"id": run})).one()
            assert tuple(row) == ("COMPLETED", "APPROVAL_EXPIRED")
            assert (await connection.scalar(text("SELECT count(*) FROM approval_decisions WHERE approval_request_id=:id"), {"id": request["id"]})) == 0
        with pytest.raises(DurableApprovalRequired):
            await service.approve_and_enqueue_continuation(request["id"], uuid4())
    finally:
        await engine.dispose()

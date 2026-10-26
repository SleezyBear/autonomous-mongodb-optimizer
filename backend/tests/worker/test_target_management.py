"""Operators can recover from onboarding mistakes: wrong label, duplicate name, wrong sandbox."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.auth.security import JwtService, Principal, Role
from app.main import app
from app.settings_store import WORKER_SCOPE, WORKER_STATUS_KEY, upsert_setting


_KEY = "target-management-signing-key-long-enough"
_CONNECTIONS = [
    {"label": "mongo-monitored", "database": "commerce", "roles": ["observer", "executor"]},
    {"label": "mongo-evaluation", "database": "commerce", "roles": ["sandbox"]},
]


def _headers(user_id: UUID, role: Role) -> dict[str, str]:
    return {"Authorization": "Bearer " + JwtService(_KEY).issue(Principal(str(user_id), role))}


@pytest.mark.asyncio
async def test_operator_can_fix_and_retire_targets(disposable_worker_database: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", disposable_worker_database)
    monkeypatch.setenv("JWT_SIGNING_KEY", _KEY)
    engine = create_async_engine(disposable_worker_database)
    operator = uuid4()
    async with engine.begin() as connection:
        await connection.execute(text("INSERT INTO users (id,created_at,updated_at,email,password_hash,role,status,failed_login_count) VALUES (:id,now(),now(),:email,'unused','OPERATOR','ACTIVE',0)"), {"id": operator, "email": f"op-{operator.hex}@example.test"})
    await upsert_setting(engine, WORKER_SCOPE, WORKER_STATUS_KEY, {"heartbeat_at": datetime.now(timezone.utc).isoformat(), "connections": _CONNECTIONS})
    headers = _headers(operator, Role.OPERATOR)

    with TestClient(app) as client:
        unknown = client.post("/api/v1/targets", headers=headers, json={"name": "orders-production", "connection_label": "mongo-typo"})
        assert unknown.status_code == 422
        assert "mongo-monitored (production)" in unknown.json()["detail"] and "mongo-evaluation (sandbox only)" in unknown.json()["detail"]

        # The exact mistake: production created on the sandbox label.
        production = client.post("/api/v1/targets", headers=headers, json={"name": "orders-production", "connection_label": "mongo-evaluation"}).json()
        duplicate = client.post("/api/v1/targets", headers=headers, json={"name": "orders-production", "connection_label": "mongo-monitored"})
        assert duplicate.status_code == 409 and "already have a target named" in duplicate.json()["detail"]

        detail = client.get(f"/api/v1/targets/{production['id']}", headers=headers).json()
        assert detail["role"] == "SANDBOX"  # recognised as a sandbox connection, not a broken production target
        connection_check = next(check for check in detail["checks"] if check["key"] == "connection")
        assert connection_check["ok"] is False and "only has sandbox credentials" in connection_check["detail"] and "mongo-monitored" in connection_check["detail"]

        sandbox = client.post("/api/v1/targets", headers=headers, json={"name": "orders-sandbox", "connection_label": "mongo-evaluation-2"})
        assert sandbox.status_code == 422
        sandbox_id = client.post("/api/v1/targets", headers=headers, json={"name": "orders-sandbox", "connection_label": "mongo-evaluation"}).json()["id"]

        # Mapping is refused while production still uses the sandbox label.
        refused = client.post(f"/api/v1/targets/{production['id']}/evaluation-mapping", headers=headers, json={"evaluation_target_id": sandbox_id})
        assert refused.status_code == 422

        # Fix it in place.
        bad_edit = client.patch(f"/api/v1/targets/{production['id']}", headers=headers, json={"connection_label": "nope"})
        assert bad_edit.status_code == 422
        fixed = client.patch(f"/api/v1/targets/{production['id']}", headers=headers, json={"connection_label": "mongo-monitored", "deployment_mode": "FULL_AUTONOMOUS"})
        assert fixed.status_code == 200, fixed.text
        assert fixed.json()["connection_label"] == "mongo-monitored" and fixed.json()["deployment_mode"] == "FULL_AUTONOMOUS"
        detail = client.get(f"/api/v1/targets/{production['id']}", headers=headers).json()
        assert detail["role"] == "MONITORED"
        assert next(check for check in detail["checks"] if check["key"] == "connection")["ok"] is True

        # A production target cannot be used as someone's sandbox.
        other = client.post("/api/v1/targets", headers=headers, json={"name": "billing-production", "connection_label": "mongo-monitored"}).json()
        wrong_sandbox = client.post(f"/api/v1/targets/{sandbox_id}/evaluation-mapping", headers=headers, json={"evaluation_target_id": other["id"]})
        assert wrong_sandbox.status_code == 422

        mapped = client.post(f"/api/v1/targets/{production['id']}/evaluation-mapping", headers=headers, json={"evaluation_target_id": sandbox_id})
        assert mapped.status_code == 200, mapped.text
        assert client.patch(f"/api/v1/targets/{production['id']}", headers=headers, json={"name": "orders-sandbox"}).status_code == 409

        # The sandbox cannot be archived while it is in use; production can, and frees its name.
        in_use = client.post(f"/api/v1/targets/{sandbox_id}/archive", headers=headers)
        assert in_use.status_code == 409 and "orders-production" in in_use.json()["detail"]
        archived = client.post(f"/api/v1/targets/{production['id']}/archive", headers=headers)
        assert archived.status_code == 200 and archived.json()["is_active"] is False
        assert client.patch(f"/api/v1/targets/{production['id']}", headers=headers, json={"name": "x"}).status_code == 409
        assert client.post("/api/v1/runs", headers=headers, json={"target_id": production["id"]}).status_code == 409
        again = client.post("/api/v1/targets", headers=headers, json={"name": "orders-production", "connection_label": "mongo-monitored"})
        assert again.status_code == 200, again.text
        # Its sandbox is free again and can be archived.
        assert client.post(f"/api/v1/targets/{sandbox_id}/archive", headers=headers).status_code == 200

        # A label cannot change under a running run.
        async with engine.begin() as connection:
            await connection.execute(text("INSERT INTO optimization_runs (id,created_at,updated_at,target_id,status,requested_by_user_id,deployment_mode,primary_metric_key) VALUES (:id,now(),now(),:target,'EVALUATING',:user,'APPROVAL_CONTROLLED','p99_latency_ms')"), {"id": uuid4(), "target": UUID(other["id"]), "user": operator})
        busy = client.patch(f"/api/v1/targets/{other['id']}", headers=headers, json={"connection_label": "mongo-evaluation"})
        assert busy.status_code == 409 and "in progress" in busy.json()["detail"]
        assert client.patch(f"/api/v1/targets/{other['id']}", headers=headers, json={"name": "billing-prod"}).status_code == 200
    await engine.dispose()

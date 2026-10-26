"""Small upsert/read helpers over the durable ``settings`` table.

The worker uses these rows to publish *non-secret* operational facts (its
heartbeat, configured connection labels, per-target telemetry status, and
provider cursors) that the API can show to operators without ever sharing the
worker's credentials.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import models


WORKER_SCOPE = "worker"
WORKER_STATUS_KEY = "status"
TELEMETRY_STATUS_SCOPE = "telemetry_status"
TELEMETRY_CURSOR_SCOPE = "telemetry_cursor"


async def upsert_setting(engine: AsyncEngine, scope: str, key: str, value: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO settings (id, created_at, updated_at, scope, key, value) "
                "VALUES (:id, :now, :now, :scope, :key, CAST(:value AS jsonb)) "
                "ON CONFLICT (scope, key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at"
            ),
            {"id": uuid4(), "now": now, "scope": scope, "key": key, "value": json.dumps(value, default=str)},
        )


async def read_setting(engine: AsyncEngine, scope: str, key: str) -> dict[str, Any] | None:
    table = models.Setting.__table__
    async with engine.connect() as connection:
        row = (await connection.execute(select(table.c.value, table.c.updated_at).where(table.c.scope == scope, table.c.key == key))).mappings().one_or_none()
    if row is None or not isinstance(row["value"], dict):
        return None
    return {**row["value"], "_updated_at": row["updated_at"].isoformat() if row["updated_at"] else None}


async def read_scope(engine: AsyncEngine, scope: str) -> dict[str, dict[str, Any]]:
    table = models.Setting.__table__
    async with engine.connect() as connection:
        rows = (await connection.execute(select(table.c.key, table.c.value, table.c.updated_at).where(table.c.scope == scope))).mappings().all()
    return {
        str(row["key"]): {**row["value"], "_updated_at": row["updated_at"].isoformat() if row["updated_at"] else None}
        for row in rows
        if isinstance(row["value"], dict)
    }


async def delete_setting(engine: AsyncEngine, scope: str, key: str) -> None:
    table = models.Setting.__table__
    async with engine.begin() as connection:
        await connection.execute(delete(table).where(table.c.scope == scope, table.c.key == key))

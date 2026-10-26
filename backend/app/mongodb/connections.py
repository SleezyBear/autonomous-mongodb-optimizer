"""Worker-only MongoDB connection registry keyed by a target's ``connection_label``.

Connection strings are secrets.  They are never accepted by the HTTP API and are
never written to PostgreSQL: an operator provisions a JSON file that is mounted
only into the worker process, e.g.::

    {
      "mongo-monitored": {
        "observer_uri": "mongodb://optimizer_observer:...@mongo-monitored:27017/?authSource=admin",
        "executor_uri": "mongodb://optimizer_executor:...@mongo-monitored:27017/?authSource=admin",
        "database": "commerce"
      },
      "mongo-evaluation": {
        "sandbox_uri": "mongodb://sandbox:...@mongo-evaluation:27017/?authSource=admin",
        "database": "commerce"
      }
    }

``observer_uri`` is read-only (telemetry, capability probes, evaluation copy
source, monitoring probes).  ``executor_uri`` may only create/drop indexes and
manage query settings.  ``sandbox_uri`` belongs to a disposable evaluation
replica and may rewrite that copy.  The worker publishes only the *label names*
and which roles each label has (never the URIs) so the API can show operators
which targets are runnable.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pymongo import AsyncMongoClient

from app.mongodb.client import create_async_mongo_client, safe_mongo_uri


DEFAULT_CONNECTIONS_FILE = Path("/run/secrets/target_connections")
_ROLES = ("observer_uri", "executor_uri", "sandbox_uri")


class ConnectionConfigurationError(RuntimeError):
    """The worker connection file is missing, malformed, or lacks a needed role."""


@dataclass(frozen=True)
class ConnectionProfile:
    label: str
    database: str
    observer_uri: str | None
    executor_uri: str | None
    sandbox_uri: str | None

    def roles(self) -> tuple[str, ...]:
        return tuple(
            role.removesuffix("_uri")
            for role in _ROLES
            if getattr(self, role) is not None
        )

    def public(self) -> dict[str, object]:
        """Return label metadata that is safe to show to API users."""
        hosts = sorted(
            {
                safe_mongo_uri(uri).split("//", 1)[-1].split("/", 1)[0]
                for uri in (self.observer_uri, self.executor_uri, self.sandbox_uri)
                if uri is not None
            }
        )
        return {"label": self.label, "database": self.database, "roles": list(self.roles()), "hosts": hosts}


def load_connection_profiles(path: Path | None = None) -> dict[str, ConnectionProfile]:
    """Parse the worker connection file; an absent file yields no profiles."""
    location = path or Path(os.getenv("TARGET_CONNECTIONS_FILE", str(DEFAULT_CONNECTIONS_FILE)))
    if not location.exists():
        return {}
    try:
        document = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConnectionConfigurationError("target connection file is unreadable or not JSON") from error
    if not isinstance(document, dict):
        raise ConnectionConfigurationError("target connection file must be a JSON object")
    profiles: dict[str, ConnectionProfile] = {}
    for label, raw in document.items():
        if not isinstance(label, str) or not label or not isinstance(raw, dict):
            raise ConnectionConfigurationError("each connection entry must be an object keyed by label")
        database = raw.get("database")
        if not isinstance(database, str) or not database or "." in database or "$" in database:
            raise ConnectionConfigurationError(f"connection {label!r} needs a plain database name")
        values: dict[str, str | None] = {}
        for role in _ROLES:
            value = raw.get(role)
            if value is not None and (not isinstance(value, str) or not value.startswith(("mongodb://", "mongodb+srv://"))):
                raise ConnectionConfigurationError(f"connection {label!r} {role} must be a MongoDB URI")
            values[role] = value
        if not any(values.values()):
            raise ConnectionConfigurationError(f"connection {label!r} defines no URI")
        profiles[label] = ConnectionProfile(label, database, values["observer_uri"], values["executor_uri"], values["sandbox_uri"])
    return profiles


class TargetConnectionRegistry:
    """Process-wide, lazily created MongoDB clients (one per label and role)."""

    def __init__(self, profiles: dict[str, ConnectionProfile]) -> None:
        self._profiles = profiles
        self._clients: dict[tuple[str, str], AsyncMongoClient[dict[str, Any]]] = {}
        self._local = os.getenv("APP_ENV", "development").lower() != "production"

    @classmethod
    def from_environment(cls) -> TargetConnectionRegistry:
        return cls(load_connection_profiles())

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))

    def profile(self, label: str) -> ConnectionProfile:
        try:
            return self._profiles[label]
        except KeyError as error:
            raise ConnectionConfigurationError(f"CONNECTION_LABEL_NOT_CONFIGURED:{label}") from error

    def has_role(self, label: str, role: str) -> bool:
        profile = self._profiles.get(label)
        return profile is not None and getattr(profile, f"{role}_uri") is not None

    def client(self, label: str, role: str) -> AsyncMongoClient[dict[str, Any]]:
        """Return the shared client for ``role`` in {observer, executor, sandbox}."""
        if role not in {"observer", "executor", "sandbox"}:
            raise ValueError("unknown connection role")
        key = (label, role)
        existing = self._clients.get(key)
        if existing is not None:
            return existing
        uri = getattr(self.profile(label), f"{role}_uri")
        if uri is None:
            raise ConnectionConfigurationError(f"CONNECTION_ROLE_NOT_CONFIGURED:{label}:{role}")
        created = create_async_mongo_client(uri, local_development=self._local)
        self._clients[key] = created
        return created

    def public_document(self) -> list[dict[str, object]]:
        return [self._profiles[label].public() for label in self.labels]

    async def close(self) -> None:
        clients, self._clients = self._clients, {}
        for client in clients.values():
            await client.close()

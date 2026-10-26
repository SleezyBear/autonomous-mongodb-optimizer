"""A small asynchronous in-memory MongoDB double for worker pipeline tests.

It implements only the subset of the PyMongo async API the production worker
uses, and models index benefit with a deterministic cost: a find whose
equality predicate is the leading key of an index is cheap; otherwise the
"collection scan" costs time proportional to the collection size.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Iterator
from typing import Any


SCAN_SECONDS_PER_DOCUMENT = 0.000_004
INDEXED_SECONDS = 0.000_15


def _get(document: dict[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = document
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return False, None
    return True, current


def _matches(document: dict[str, Any], query: dict[str, Any]) -> bool:
    for field, condition in query.items():
        present, value = _get(document, field)
        if isinstance(condition, dict) and condition and all(str(key).startswith("$") for key in condition):
            for operator, expected in condition.items():
                if operator == "$eq" and not (present and value == expected):
                    return False
                if operator == "$in" and not (present and value in expected):
                    return False
                if operator in {"$gt", "$gte", "$lt", "$lte"}:
                    if not present:
                        return False
                    try:
                        ok = {"$gt": value > expected, "$gte": value >= expected, "$lt": value < expected, "$lte": value <= expected}[operator]
                    except TypeError:
                        return False
                    if not ok:
                        return False
        elif not (present and value == condition):
            return False
    return True


class FakeCursor:
    def __init__(self, documents: list[dict[str, Any]], delay: float = 0.0) -> None:
        self._documents = documents
        self._delay = delay

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        if self._delay:
            await asyncio.sleep(self._delay)
        return list(self._documents if length is None else self._documents[:length])

    def __aiter__(self) -> FakeCursor:
        self._iterator: Iterator[dict[str, Any]] = iter(list(self._documents))
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._iterator)
        except StopIteration as error:
            raise StopAsyncIteration from error


class FakeCollection:
    def __init__(self, name: str) -> None:
        self.name = name
        self.documents: list[dict[str, Any]] = []
        self.indexes: dict[str, list[tuple[str, int]]] = {"_id_": [("_id", 1)]}
        self.find_calls = 0

    def _cost(self, query: dict[str, Any]) -> float:
        equality = {field for field, condition in query.items() if not isinstance(condition, dict) or "$eq" in condition or "$in" in condition}
        for name, keys in self.indexes.items():
            if keys and keys[0][0] in equality and (name != "_id_" or "_id" in equality):
                return float(getattr(self, "indexed_seconds", INDEXED_SECONDS))
        return SCAN_SECONDS_PER_DOCUMENT * max(len(self.documents), 1)

    def find(self, query: dict[str, Any] | None = None, projection: dict[str, Any] | None = None, *, sort: list[tuple[str, int]] | None = None, limit: int = 0, batch_size: int | None = None) -> FakeCursor:
        del batch_size
        self.find_calls += 1
        query = query or {}
        rows = [dict(document) for document in self.documents if _matches(document, query)]
        for field, direction in reversed(sort or []):
            rows.sort(key=lambda row: (_get(row, field)[1] is None, _get(row, field)[1]), reverse=direction == -1)
        if limit:
            rows = rows[:limit]
        if projection:
            rows = [{key: value for key, value in row.items() if key in projection or key == "_id"} for row in rows]
        return FakeCursor(rows, self._cost(query))

    def aggregate(self, pipeline: list[dict[str, Any]]) -> FakeCursor:
        stage = pipeline[0] if pipeline else {}
        if "$sample" in stage:
            size = int(stage["$sample"]["size"])
            population = list(self.documents)
            return FakeCursor([dict(item) for item in random.Random(7).sample(population, min(size, len(population)))])
        return FakeCursor([])

    async def estimated_document_count(self) -> int:
        return len(self.documents)

    async def count_documents(self, query: dict[str, Any]) -> int:
        return sum(_matches(document, query) for document in self.documents)

    async def insert_one(self, document: dict[str, Any]) -> None:
        await asyncio.sleep(0.000_05 * len(self.indexes))
        self.documents.append(dict(document))

    async def insert_many(self, documents: list[dict[str, Any]], ordered: bool = True) -> None:
        del ordered
        self.documents.extend(dict(document) for document in documents)

    async def delete_many(self, query: dict[str, Any]) -> None:
        self.documents = [document for document in self.documents if not _matches(document, query)]

    async def drop(self) -> None:
        self.documents = []
        self.indexes = {"_id_": [("_id", 1)]}

    async def create_index(self, keys: list[tuple[str, int]], name: str, **options: Any) -> str:
        del options
        self.indexes[name] = [(str(field), int(direction)) for field, direction in keys]
        return name

    async def drop_index(self, name: str) -> None:
        if name == "_id_":
            raise ValueError("cannot drop _id_")
        self.indexes.pop(name)

    async def index_information(self) -> dict[str, dict[str, Any]]:
        return {name: {"key": list(keys)} for name, keys in self.indexes.items()}

    def list_indexes(self) -> FakeCursor:
        return FakeCursor([{"name": name, "key": dict(keys), "v": 2} for name, keys in self.indexes.items()])


class FakeDatabase:
    def __init__(self, server: FakeServer, name: str) -> None:
        self._server = server
        self.name = name

    def get_collection(self, name: str) -> FakeCollection:
        return self._server.collections.setdefault((self.name, name), FakeCollection(name))

    async def list_collection_names(self) -> list[str]:
        return sorted(name for (database, name), collection in self._server.collections.items() if database == self.name and (collection.documents or len(collection.indexes) > 1))

    async def command(self, command: dict[str, Any]) -> dict[str, Any]:
        name = next(iter(command))
        if name == "hello":
            return {"ok": 1, "setName": self._server.set_name, "isWritablePrimary": True}
        if name == "buildInfo":
            return {"version": "8.0.4", "versionArray": [8, 0, 4, 0]}
        if name == "getParameter":
            return {"featureCompatibilityVersion": {"version": "8.0"}}
        if name == "replSetGetStatus":
            return {"members": [{"health": 1}]}
        if name == "profile":
            return {"was": 0}
        if name == "getLog":
            return {"log": list(self._server.log_lines)}
        if name == "currentOp":
            return {"inprog": []}
        raise RuntimeError(f"unsupported fake command {name}")

    def aggregate(self, pipeline: list[dict[str, Any]]) -> FakeCursor:
        raise RuntimeError("aggregate stage not supported by fake server")


class FakeServer:
    def __init__(self, set_name: str) -> None:
        self.set_name = set_name
        self.collections: dict[tuple[str, str], FakeCollection] = {}
        self.log_lines: list[str] = []

    def client(self) -> FakeClient:
        return FakeClient(self)


class FakeClient:
    def __init__(self, server: FakeServer) -> None:
        self.server = server

    def get_database(self, name: str) -> FakeDatabase:
        return FakeDatabase(self.server, name)

    @property
    def admin(self) -> FakeDatabase:
        return self.get_database("admin")

    async def close(self) -> None:
        return None

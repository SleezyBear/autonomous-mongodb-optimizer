"""Replay literal-free find shapes against a sandbox copy or a read-only probe.

Telemetry never retains predicate literals: a persisted shape such as
``{"filter": {"customer_id": "<integer>", "created_at": {"$gte": "<date>"}}}``
records only structure.  To measure a candidate index the evaluator needs
concrete queries, so each template is *instantiated from documents sampled
from the same collection*.  Values therefore come from the evaluated data
itself, never from the monitored application's requests, and they are never
persisted: only latencies, counts, and result fingerprints leave this module.

Only conservative shapes are replayed (top-level equality, ``$eq``, ``$in``
and range operators).  Anything else (``$or``, ``$regex``, ``$elemMatch`` …)
is reported as unsupported rather than approximated.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


_EQUALITY_OPERATORS = frozenset({"$eq"})
_LOWER = frozenset({"$gt", "$gte"})
_UPPER = frozenset({"$lt", "$lte"})
_SET = frozenset({"$in"})
_SUPPORTED_OPERATORS = _EQUALITY_OPERATORS | _LOWER | _UPPER | _SET
WRITE_OPERATIONS = frozenset({"insert", "update", "delete", "findAndModify", "findandmodify"})
DEFAULT_REPLAY_LIMIT = 20


@dataclass(frozen=True)
class FieldTemplate:
    """How one top-level filter field is instantiated from a sampled document."""

    field: str
    kind: str  # EQUALITY | IN | RANGE
    lower: bool = False
    upper: bool = False


@dataclass(frozen=True)
class ReplayShape:
    """One replayable find shape with its observed workload weight."""

    query_shape_id: str
    query_shape_hash: str
    collection: str
    fields: tuple[FieldTemplate, ...]
    sort: tuple[tuple[str, int], ...]
    limit: int
    weight: float

    @property
    def label(self) -> str:
        return self.query_shape_hash[:12]


@dataclass(frozen=True)
class ReplayPlan:
    """A deterministic, weighted schedule of replayable shapes for one collection."""

    collection: str
    shapes: tuple[ReplayShape, ...]
    unsupported: tuple[str, ...] = ()
    has_writes: bool = False
    schedule: tuple[int, ...] = field(default=())

    def is_empty(self) -> bool:
        return not self.shapes

    def describe(self) -> dict[str, object]:
        """Literal-free description persisted as evidence."""
        return {
            "collection": self.collection,
            "shapes": [
                {
                    "query_shape_id": shape.query_shape_id,
                    "query_shape_hash": shape.query_shape_hash,
                    "fields": [{"field": item.field, "kind": item.kind, "lower": item.lower, "upper": item.upper} for item in shape.fields],
                    "sort": [[name, direction] for name, direction in shape.sort],
                    "limit": shape.limit,
                    "weight": round(shape.weight, 6),
                }
                for shape in self.shapes
            ],
            "unsupported_query_shape_ids": list(self.unsupported),
            "has_writes": self.has_writes,
        }


@dataclass(frozen=True)
class InstantiatedQuery:
    shape: ReplayShape
    filter: dict[str, Any]


def _command(normalized_shape: object) -> Mapping[str, Any] | None:
    if not isinstance(normalized_shape, Mapping):
        return None
    query = normalized_shape.get("query", normalized_shape)
    return query if isinstance(query, Mapping) else None


def parse_find_template(normalized_shape: object) -> tuple[tuple[FieldTemplate, ...], tuple[tuple[str, int], ...], int] | None:
    """Return field templates, sort, and limit, or ``None`` when unsupported."""
    command = _command(normalized_shape)
    if command is None:
        return None
    filter_document = command.get("filter", command.get("query", {}))
    if filter_document is None:
        filter_document = {}
    if not isinstance(filter_document, Mapping):
        return None
    templates: list[FieldTemplate] = []
    for name, value in filter_document.items():
        if not isinstance(name, str) or not name or name.startswith("$"):
            return None
        if isinstance(value, Mapping):
            operators = {str(operator) for operator in value}
            if not operators or not operators <= _SUPPORTED_OPERATORS:
                return None
            if operators & _SET:
                if operators != {"$in"}:
                    return None
                templates.append(FieldTemplate(name, "IN"))
            elif operators & (_LOWER | _UPPER):
                if operators & _EQUALITY_OPERATORS:
                    return None
                templates.append(FieldTemplate(name, "RANGE", lower=bool(operators & _LOWER), upper=bool(operators & _UPPER)))
            else:
                templates.append(FieldTemplate(name, "EQUALITY"))
        elif isinstance(value, list):
            return None
        else:
            templates.append(FieldTemplate(name, "EQUALITY"))
    sort_document = command.get("sort", {})
    sort: list[tuple[str, int]] = []
    if isinstance(sort_document, Mapping):
        for name, direction in sort_document.items():
            if not isinstance(name, str) or name.startswith("$"):
                return None
            # Telemetry canonicalization may tokenise the direction; ascending
            # order is used for the replay, which is index-direction neutral
            # for single-key sorts and conservative for compound ones.
            sort.append((name, -1 if direction == -1 else 1))
    limit_value = command.get("limit")
    limit = int(limit_value) if isinstance(limit_value, int) and not isinstance(limit_value, bool) and 0 < limit_value <= 1000 else DEFAULT_REPLAY_LIMIT
    return tuple(sorted(templates, key=lambda item: item.field)), tuple(sort), limit


def build_replay_plan(collection: str, shapes: list[Mapping[str, Any]], *, maximum_shapes: int = 8) -> ReplayPlan:
    """Build a weighted plan from snapshot shape rows of one collection.

    Each row needs ``query_shape_id``, ``query_shape_hash``, ``operation``,
    ``normalized_shape`` and ``workload_operation_share``.
    """
    replayable: list[ReplayShape] = []
    unsupported: list[str] = []
    has_writes = False
    ordered = sorted(shapes, key=lambda row: (-float(row.get("workload_operation_share") or 0.0), str(row["query_shape_hash"])))
    for row in ordered:
        operation = str(row.get("operation", ""))
        if operation in WRITE_OPERATIONS:
            has_writes = True
            continue
        if operation != "find":
            unsupported.append(str(row["query_shape_id"]))
            continue
        parsed = parse_find_template(row.get("normalized_shape"))
        if parsed is None or not parsed[0]:
            unsupported.append(str(row["query_shape_id"]))
            continue
        fields, sort, limit = parsed
        replayable.append(
            ReplayShape(
                str(row["query_shape_id"]), str(row["query_shape_hash"]), collection, fields, sort, limit,
                max(float(row.get("workload_operation_share") or 0.0), 0.0),
            )
        )
        if len(replayable) >= maximum_shapes:
            break
    total = sum(shape.weight for shape in replayable)
    schedule: list[int] = []
    if replayable:
        # Deterministic weighted schedule of 20 slots; every shape runs at least once.
        for index, shape in enumerate(replayable):
            share = shape.weight / total if total > 0 else 1 / len(replayable)
            schedule.extend([index] * max(1, round(share * 20)))
    return ReplayPlan(collection, tuple(replayable), tuple(unsupported), has_writes, tuple(schedule))


def lookup(document: Mapping[str, Any], dotted: str) -> tuple[bool, Any]:
    """Resolve a dotted path in a sampled document."""
    current: Any = document
    for part in dotted.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return False, None
    if isinstance(current, (list, dict)):
        return False, None
    return True, current


def instantiate(shape: ReplayShape, sample: Mapping[str, Any], second: Mapping[str, Any] | None = None) -> InstantiatedQuery | None:
    """Fill a template from sampled documents; ``None`` when a field is absent."""
    filter_document: dict[str, Any] = {}
    for template in shape.fields:
        present, value = lookup(sample, template.field)
        if not present:
            return None
        if template.kind == "EQUALITY":
            filter_document[template.field] = value
        elif template.kind == "IN":
            filter_document[template.field] = {"$in": [value]}
        else:
            other_present, other = lookup(second, template.field) if second is not None else (False, None)
            bounds: dict[str, Any] = {}
            low, high = value, value
            if other_present:
                try:
                    low, high = (value, other) if value <= other else (other, value)
                except TypeError:
                    low, high = value, value
            if template.lower:
                bounds["$gte"] = low
            if template.upper:
                bounds["$lte"] = high
            filter_document[template.field] = bounds
    return InstantiatedQuery(shape, filter_document)


def result_fingerprint(identifiers: list[Any]) -> str:
    """Order-insensitive fingerprint of returned document identities."""
    rendered = sorted(json.dumps(item, sort_keys=True, default=str) for item in identifiers)
    return hashlib.sha256("\n".join(rendered).encode("utf-8")).hexdigest()


def respects_sort(documents: Sequence[Mapping[str, Any]], sort: tuple[tuple[str, int], ...]) -> bool:
    """Check the returned order honours the requested sort keys."""
    if not sort or len(documents) < 2:
        return True

    def key(document: Mapping[str, Any]) -> tuple[Any, ...]:
        return tuple(lookup(document, name)[1] for name, _ in sort)

    for previous, current in zip(documents, list(documents)[1:]):
        left, right = key(previous), key(current)
        for (name, direction), a, b in zip(sort, left, right):
            del name
            try:
                if a == b:
                    continue
                ordered = a < b if direction == 1 else a > b
            except TypeError:
                return True  # mixed BSON types use server ordering; do not guess
            if not ordered:
                return False
            break
    return True

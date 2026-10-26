"""Literal-free shape replay used by the sandbox evaluator."""

from app.evaluation.replay import build_replay_plan, instantiate, parse_find_template, respects_sort, result_fingerprint


def _row(shape: dict[str, object], share: float = 0.5, operation: str = "find", identifier: str = "s1") -> dict[str, object]:
    return {"query_shape_id": identifier, "query_shape_hash": identifier * 4, "operation": operation, "normalized_shape": {"query": shape}, "workload_operation_share": share}


def test_templates_accept_only_conservative_operators() -> None:
    fields, sort, limit = parse_find_template({"query": {"find": "<string>", "filter": {"customer_id": "<integer>", "created_at": {"$gte": "<date>"}}, "sort": {"created_at": "<integer>"}, "limit": 10}}) or ((), (), 0)
    assert [(item.field, item.kind) for item in fields] == [("created_at", "RANGE"), ("customer_id", "EQUALITY")]
    assert sort == (("created_at", 1),) and limit == 10
    assert parse_find_template({"query": {"filter": {"$or": [{"a": "<integer>"}]}}}) is None
    assert parse_find_template({"query": {"filter": {"name": {"$regex": "<string>"}}}}) is None


def test_plan_weights_shapes_and_records_writes_and_unsupported() -> None:
    plan = build_replay_plan("orders", [
        _row({"filter": {"customer_id": "<integer>"}}, 0.75, identifier="a"),
        _row({"filter": {"status": "<string>"}}, 0.25, identifier="b"),
        _row({"filter": {"$where": "<string>"}}, 0.1, identifier="c"),
        _row({"update": "<string>"}, 0.1, operation="update", identifier="d"),
    ])
    assert [shape.query_shape_id for shape in plan.shapes] == ["a", "b"]
    assert plan.unsupported == ("c",) and plan.has_writes
    assert plan.schedule.count(0) == 15 and plan.schedule.count(1) == 5


def test_instantiation_uses_sampled_values_and_skips_missing_fields() -> None:
    plan = build_replay_plan("orders", [_row({"filter": {"customer_id": "<integer>", "total": {"$gte": "<integer>", "$lte": "<integer>"}}})])
    shape = plan.shapes[0]
    query = instantiate(shape, {"customer_id": 7, "total": 50}, {"customer_id": 9, "total": 20})
    assert query is not None and query.filter == {"customer_id": 7, "total": {"$gte": 20, "$lte": 50}}
    assert instantiate(shape, {"total": 1}) is None


def test_correctness_helpers_ignore_order_but_check_requested_sort() -> None:
    assert result_fingerprint([1, 2, 3]) == result_fingerprint([3, 1, 2])
    assert respects_sort([{"a": 1}, {"a": 2}], (("a", 1),))
    assert not respects_sort([{"a": 2}, {"a": 1}], (("a", 1),))

#!/usr/bin/env python3
"""Seed and exercise the local monitored MongoDB so the optimizer has real work.

Creates ``commerce.orders`` (default 120k documents, no secondary indexes) and
then issues an application-like read workload — customer order lookups sorted
by recency plus a smaller status filter — for ``--duration`` seconds.  With the
development ``--slowms 5`` setting these collection scans appear in MongoDB's
structured slow-query log, which the worker's telemetry collector turns into
literal-free workload windows.

    ./nosql/bin/python scripts/dev_workload.py --seed --duration 120

Only the owned ``commerce.orders`` development collection is written, and only
by ``--seed``/``--reset``.  While seeding, the slow-query threshold is raised so
the one-time bulk load is not mistaken for application writes.  The script refuses non-local URIs unless ``--force``.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from pymongo import MongoClient


DEFAULT_URI = "mongodb://control_plane_root:control_plane_root_dev_only@127.0.0.1:27017/?authSource=admin&directConnection=true"
STATUSES = ("pending", "paid", "shipped", "delivered", "cancelled")


def seed(collection: object, count: int, customers: int) -> None:
    rng = random.Random(42)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    batch = []
    for order_id in range(1, count + 1):
        batch.append({
            "_id": order_id,
            "customer_id": rng.randrange(1, customers + 1),
            "status": STATUSES[rng.randrange(len(STATUSES))],
            "total_cents": rng.randrange(500, 50_000),
            "created_at": start + timedelta(minutes=order_id),
            "region": rng.randrange(5),
        })
        if len(batch) == 5_000:
            collection.insert_many(batch, ordered=False)  # type: ignore[attr-defined]
            batch = []
    if batch:
        collection.insert_many(batch, ordered=False)  # type: ignore[attr-defined]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--uri", default=DEFAULT_URI)
    parser.add_argument("--database", default="commerce")
    parser.add_argument("--seed", action="store_true", help="create the orders collection if it is empty")
    parser.add_argument("--reset", action="store_true", help="drop and re-create the dev orders collection")
    parser.add_argument("--orders", type=int, default=120_000)
    parser.add_argument("--customers", type=int, default=8_000)
    parser.add_argument("--duration", type=float, default=60.0, help="seconds of read workload (0 to skip)")
    parser.add_argument("--force", action="store_true", help="allow a non-local MongoDB host")
    arguments = parser.parse_args()
    host = urlsplit(arguments.uri).hostname or ""
    if host not in {"127.0.0.1", "localhost", "mongo-monitored"} and not arguments.force:
        print("Refusing to write to a non-local MongoDB without --force.", file=sys.stderr)
        return 2
    client: MongoClient[dict[str, object]] = MongoClient(arguments.uri, serverSelectionTimeoutMS=5_000)
    orders = client[arguments.database]["orders"]
    if arguments.reset:
        orders.drop()
    if arguments.seed or arguments.reset:
        if orders.estimated_document_count() == 0:
            print(f"Seeding {arguments.orders:,} orders …", flush=True)
            # The bulk load is setup, not application traffic: keep it out of the slow-query log,
            # or the optimizer would see a write workload that never really happens.
            database = client[arguments.database]
            previous = int(database.command({"profile": -1}).get("slowms", 100))
            database.command({"profile": 0, "slowms": 1_000_000})
            try:
                seed(orders, arguments.orders, arguments.customers)
            finally:
                database.command({"profile": 0, "slowms": previous})
        print(f"orders: {orders.estimated_document_count():,} documents, indexes: {sorted(orders.index_information())}")
    if arguments.duration <= 0:
        return 0
    rng = random.Random()
    deadline = time.monotonic() + arguments.duration
    issued = 0
    print(f"Running read workload for {arguments.duration:.0f}s (Ctrl-C to stop) …", flush=True)
    try:
        while time.monotonic() < deadline:
            if rng.random() < 0.8:
                list(orders.find({"customer_id": rng.randrange(1, arguments.customers + 1)}).sort("created_at", -1).limit(10))
            else:
                list(orders.find({"status": STATUSES[rng.randrange(len(STATUSES))], "region": rng.randrange(5)}).limit(20))
            issued += 1
            if issued % 200 == 0:
                print(f"  {issued} queries", flush=True)
    except KeyboardInterrupt:
        pass
    print(f"Issued {issued} queries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

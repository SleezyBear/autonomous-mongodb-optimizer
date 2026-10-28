"""Benchmark suite behind Chapters 8 to 10 of the final report.

Run through ``run_all.sh``; each suite is also runnable alone, e.g.
``docker compose --profile client run --rm client python bench.py mongo``.

Every suite writes ``<RESULTS_DIR>/<suite>.json`` after each experiment, so a
failure later in a suite never loses earlier measurements.  Raw per-repetition
summaries and latency samples are kept for the statistics and figures.
"""

from __future__ import annotations

import itertools
import json
import os
import random
import sys
import threading
import time
import traceback
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

RESULTS = Path(os.environ.get("RESULTS_DIR", "/results"))
REPS = int(os.environ.get("REPS", "5"))
WARMUP = float(os.environ.get("WARMUP_SECONDS", "3"))
MEASURE = float(os.environ.get("MEASURE_SECONDS", "10"))
SCALES = sorted(int(value) for value in os.environ.get("SCALES", "10000,100000,1000000,10000000").split(",") if value.strip())
MAIN_SCALE = 1_000_000 if 1_000_000 in SCALES else max(scale for scale in SCALES if scale <= 1_000_000)
CLIENTS = (1, 2, 4, 8)
SEED = 42
STATUSES = ("pending", "paid", "shipped", "delivered", "cancelled")
START = datetime(2024, 1, 1, tzinfo=timezone.utc)
NEW_IDS = 10_000_000_000  # ids at or above this were inserted during a measurement
SPAN_SECONDS = 730 * 86_400
SAMPLE = 2_000
_uid = itertools.count(1)
# Fresh interpreters for client processes: no database client is ever shared across a fork.
SPAWN = multiprocessing.get_context("spawn")

# Workload mixes (operation -> share).  W5 follows the application access patterns in Section 4.3.
WORKLOADS: dict[str, dict[str, float]] = {
    "W1": {"Q1": 0.50, "Q2": 0.25, "Q3": 0.20, "Q4": 0.03, "Q5": 0.02},
    "W2": {"Q1": 0.25, "Q2": 0.15, "Q3": 0.10, "Q4": 0.30, "Q5": 0.20},
    "W3": {"Q1": 0.05, "Q3": 0.05, "Q4": 0.60, "Q5": 0.30},
    "W4": {"Q6": 1.0},
    "W5": {"Q1": 0.55, "Q2": 0.10, "Q3": 0.15, "Q7": 0.05, "Q4": 0.10, "Q5": 0.05},
}
# Operations every compared database supports natively, used for the cross-system comparison.
CORE = {"Q1": 0.70, "Q3": 0.15, "Q4": 0.15}


def log(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def save(name: str, data: Any) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{name}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=1, default=str))
    temporary.replace(path)


def guarded(results: dict[str, Any], key: str, suite: str, function: Callable[[], Any]) -> Any:
    """Run one experiment; record its error instead of stopping the suite."""
    started = time.perf_counter()
    try:
        value = function()
        results[key] = value
        return value
    except Exception as error:  # noqa: BLE001 - keep the unattended run going
        results.setdefault("errors", {})[key] = f"{type(error).__name__}: {error}"
        log(f"!! {suite}.{key} failed: {error}")
        traceback.print_exc()
        return None
    finally:
        results.setdefault("durations_s", {})[key] = round(time.perf_counter() - started, 1)
        save(suite, results)


# ---------------------------------------------------------------- data

def customer_count(n: int) -> int:
    return max(1_000, n // 10)


def skewed_customer(rng: random.Random, n: int) -> int:
    # Squaring a uniform draw gives a realistic skew: a minority of customers place most orders.
    return int(customer_count(n) * rng.random() ** 2) + 1


def order_doc(order_id: int, n: int, rng: random.Random, created_at: datetime | None = None) -> dict[str, Any]:
    items = [{"sku": rng.randrange(1, 50_001), "qty": rng.randrange(1, 5), "price_cents": rng.randrange(100, 20_000)} for _ in range(rng.randrange(1, 5))]
    return {
        "_id": order_id,
        "customer_id": skewed_customer(rng, n),
        "status": STATUSES[rng.randrange(len(STATUSES))],
        "region": rng.randrange(5),
        "total_cents": sum(item["qty"] * item["price_cents"] for item in items),
        "created_at": created_at or START + timedelta(seconds=rng.randrange(SPAN_SECONDS)),
        "items": items,
    }


def customer_doc(customer_id: int) -> dict[str, Any]:
    rng = random.Random(f"customer:{customer_id}")
    return {"_id": customer_id, "name": f"Customer {customer_id}", "segment": ("consumer", "business")[customer_id % 2], "region": customer_id % 5,
            "signup": START - timedelta(days=rng.randrange(1, 1_500))}


def batch_starts(n: int, size: int = 10_000) -> list[int]:
    return list(range(1, n + 1, size))


def batch(start: int, n: int, size: int = 10_000) -> list[dict[str, Any]]:
    rng = random.Random(f"{SEED}:{start}")
    return [order_doc(order_id, n, rng) for order_id in range(start, min(start + size, n + 1))]


def split(items: list[int], parts: int) -> list[list[int]]:
    return [items[index::parts] for index in range(parts)]


# ---------------------------------------------------------------- systems

class MongoSystem:
    INDEXES = [("ix_customer_created", [("customer_id", 1), ("created_at", -1)]), ("ix_status_region", [("status", 1), ("region", 1)]), ("ix_created", [("created_at", 1)])]

    def __init__(self, uri: str = "mongodb://mongo:27017/?directConnection=true") -> None:
        from pymongo import MongoClient
        self.uri = uri
        self.client = MongoClient(uri, serverSelectionTimeoutMS=60_000)
        self.db = self.client["bench"]

    def wait(self, timeout: float = 240) -> None:
        deadline = time.time() + timeout
        while True:
            try:
                self.client.admin.command("ping")
                return
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(2)

    def version(self) -> str:
        return str(self.client.admin.command("buildInfo")["version"])

    def reset(self) -> None:
        for name in ("orders", "customers", "orders_ref", "order_items"):
            self.db.drop_collection(name)

    def load_slice(self, n: int, starts: list[int]) -> None:
        for start in starts:
            self.db.orders.insert_many(batch(start, n), ordered=False)

    def load_customers(self, n: int) -> None:
        ids = list(range(1, customer_count(n) + 1))
        for index in range(0, len(ids), 10_000):
            self.db.customers.insert_many([customer_doc(customer_id) for customer_id in ids[index:index + 10_000]], ordered=False)

    def create_indexes(self, which: list[str] | None = None) -> dict[str, float]:
        timings = {}
        for name, keys in self.INDEXES:
            if which is None or name in which:
                started = time.perf_counter()
                self.db.orders.create_index(keys, name=name)
                timings[name] = time.perf_counter() - started
        return timings

    def drop_indexes(self) -> None:
        self.db.orders.drop_indexes()

    def cleanup(self) -> None:
        self.db.orders.delete_many({"_id": {"$gte": NEW_IDS}})

    def coll_bytes(self, name: str) -> int:
        stats = list(self.db[name].aggregate([{"$collStats": {"storageStats": {}}}]))
        return sum(int(entry["storageStats"].get("storageSize", 0)) + int(entry["storageStats"].get("totalIndexSize", 0)) for entry in stats)

    def storage(self) -> dict[str, Any]:
        shards = list(self.db.orders.aggregate([{"$collStats": {"storageStats": {}}}]))
        out = {"shards": {}}
        totals = {"count": 0, "data_bytes": 0, "storage_bytes": 0, "index_bytes": 0}
        index_sizes: dict[str, int] = {}
        for entry in shards:
            stats = entry["storageStats"]
            out["shards"][entry.get("shard", "single")] = int(stats.get("count", 0))
            totals["count"] += int(stats.get("count", 0))
            totals["data_bytes"] += int(stats.get("size", 0))
            totals["storage_bytes"] += int(stats.get("storageSize", 0))
            totals["index_bytes"] += int(stats.get("totalIndexSize", 0))
            for name, size in stats.get("indexSizes", {}).items():
                index_sizes[name] = index_sizes.get(name, 0) + int(size)
        customers = self.db.command("dbStats")
        out.update(totals)
        out["index_sizes"] = index_sizes
        out["db_storage_bytes"] = int(customers.get("storageSize", 0)) + int(customers.get("indexSize", 0))
        return out

    def run(self, op: str, rng: random.Random, n: int, uid: int, counter: list[int]) -> None:
        orders = self.db.orders
        if op == "Q1":
            list(orders.find({"customer_id": skewed_customer(rng, n)}, {"items": 0}).sort("created_at", -1).limit(10))
        elif op == "Q2":
            list(orders.find({"status": STATUSES[rng.randrange(5)], "region": rng.randrange(5)}, {"items": 0}).limit(20))
        elif op == "Q3":
            orders.find_one({"_id": rng.randrange(1, n + 1)})
        elif op == "Q4":
            counter[0] += 1
            orders.insert_one(order_doc(NEW_IDS + uid * 10_000_000 + counter[0], n, rng, datetime.now(timezone.utc)))
        elif op == "Q5":
            orders.update_one({"_id": rng.randrange(1, n + 1)}, {"$set": {"status": STATUSES[rng.randrange(5)]}})
        elif op == "Q6":
            low = START + timedelta(seconds=rng.randrange(SPAN_SECONDS - 7 * 86_400))
            list(orders.aggregate([{"$match": {"created_at": {"$gte": low, "$lt": low + timedelta(days=7)}}},
                                   {"$group": {"_id": "$region", "revenue": {"$sum": "$total_cents"}, "orders": {"$sum": 1}}}]))
        elif op == "Q7":
            order = orders.find_one({"_id": rng.randrange(1, n + 1)})
            if order:
                self.db.customers.find_one({"_id": order["customer_id"]})
        elif op == "Q7R":
            found = list(self.db.orders_ref.aggregate([{"$match": {"_id": rng.randrange(1, n + 1)}},
                                                        {"$lookup": {"from": "order_items", "localField": "_id", "foreignField": "order_id", "as": "items"}}]))
            if found:
                self.db.customers.find_one({"_id": found[0]["customer_id"]})
        else:
            raise ValueError(op)


class PostgresSystem:
    DSN = "host=postgres dbname=bench user=postgres password=bench"
    INDEXES = [("ix_customer_created", "CREATE INDEX ix_customer_created ON orders (customer_id, created_at DESC)"),
               ("ix_status_region", "CREATE INDEX ix_status_region ON orders (status, region)"),
               ("ix_created", "CREATE INDEX ix_created ON orders (created_at)")]

    def __init__(self) -> None:
        import psycopg
        self.psycopg = psycopg
        self.conn = psycopg.connect(self.DSN, autocommit=True)

    @staticmethod
    def wait(timeout: float = 240) -> None:
        import psycopg
        deadline = time.time() + timeout
        while True:
            try:
                psycopg.connect(PostgresSystem.DSN, connect_timeout=5).close()
                return
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(2)

    def version(self) -> str:
        return str(self.conn.execute("SHOW server_version").fetchone()[0])

    def reset(self) -> None:
        self.conn.execute("DROP TABLE IF EXISTS order_items, orders, customers CASCADE")
        self.conn.execute("CREATE TABLE customers (id integer PRIMARY KEY, name text NOT NULL, segment text NOT NULL, region smallint NOT NULL, signup timestamptz NOT NULL)")
        self.conn.execute("CREATE TABLE orders (id bigint PRIMARY KEY, customer_id integer NOT NULL, status text NOT NULL, region smallint NOT NULL, total_cents integer NOT NULL, created_at timestamptz NOT NULL)")
        self.conn.execute("CREATE TABLE order_items (order_id bigint NOT NULL, line smallint NOT NULL, sku integer NOT NULL, qty smallint NOT NULL, price_cents integer NOT NULL, PRIMARY KEY (order_id, line))")

    def load_slice(self, n: int, starts: list[int]) -> None:
        with self.conn.cursor() as cursor:
            for start in starts:
                docs = batch(start, n)
                with cursor.copy("COPY orders (id, customer_id, status, region, total_cents, created_at) FROM STDIN") as copy:
                    for doc in docs:
                        copy.write_row((doc["_id"], doc["customer_id"], doc["status"], doc["region"], doc["total_cents"], doc["created_at"]))
                with cursor.copy("COPY order_items (order_id, line, sku, qty, price_cents) FROM STDIN") as copy:
                    for doc in docs:
                        for line, item in enumerate(doc["items"]):
                            copy.write_row((doc["_id"], line, item["sku"], item["qty"], item["price_cents"]))

    def load_customers(self, n: int) -> None:
        with self.conn.cursor() as cursor, cursor.copy("COPY customers (id, name, segment, region, signup) FROM STDIN") as copy:
            for customer_id in range(1, customer_count(n) + 1):
                doc = customer_doc(customer_id)
                copy.write_row((doc["_id"], doc["name"], doc["segment"], doc["region"], doc["signup"]))

    def create_indexes(self, which: list[str] | None = None) -> dict[str, float]:
        timings = {}
        for name, statement in self.INDEXES:
            if which is None or name in which:
                started = time.perf_counter()
                self.conn.execute(statement)
                timings[name] = time.perf_counter() - started
        if which is None:
            started = time.perf_counter()
            self.conn.execute("ALTER TABLE order_items ADD CONSTRAINT fk_items_order FOREIGN KEY (order_id) REFERENCES orders (id)")
            timings["fk_items_order"] = time.perf_counter() - started
        self.conn.execute("ANALYZE")
        return timings

    def drop_indexes(self) -> None:
        for name, _ in self.INDEXES:
            self.conn.execute(f"DROP INDEX IF EXISTS {name}")
        self.conn.execute("ANALYZE orders")

    def cleanup(self) -> None:
        self.conn.execute("DELETE FROM order_items WHERE order_id >= %s", (NEW_IDS,))
        self.conn.execute("DELETE FROM orders WHERE id >= %s", (NEW_IDS,))

    def storage(self) -> dict[str, Any]:
        rows = self.conn.execute("SELECT relname, pg_table_size(oid), pg_indexes_size(oid) FROM pg_class WHERE relname IN ('orders','order_items','customers')").fetchall()
        out: dict[str, Any] = {"tables": {name: {"table_bytes": table, "index_bytes": index} for name, table, index in rows}}
        out["data_bytes"] = sum(table for _, table, _ in rows)
        out["index_bytes"] = sum(index for _, _, index in rows)
        out["storage_bytes"] = out["data_bytes"]
        out["count"] = self.conn.execute("SELECT count(*) FROM orders").fetchone()[0]
        out["index_sizes"] = {name: size for name, size in self.conn.execute("SELECT indexrelname, pg_relation_size(indexrelid) FROM pg_stat_user_indexes").fetchall()}
        return out

    def run(self, op: str, rng: random.Random, n: int, uid: int, counter: list[int]) -> None:
        execute = self.conn.execute
        if op == "Q1":
            execute("SELECT id, customer_id, status, region, total_cents, created_at FROM orders WHERE customer_id = %s ORDER BY created_at DESC LIMIT 10", (skewed_customer(rng, n),)).fetchall()
        elif op == "Q2":
            execute("SELECT id, customer_id, status, region, total_cents, created_at FROM orders WHERE status = %s AND region = %s LIMIT 20", (STATUSES[rng.randrange(5)], rng.randrange(5))).fetchall()
        elif op == "Q3":
            order_id = rng.randrange(1, n + 1)
            execute("SELECT * FROM orders WHERE id = %s", (order_id,)).fetchall()
            execute("SELECT * FROM order_items WHERE order_id = %s", (order_id,)).fetchall()
        elif op == "Q4":
            counter[0] += 1
            doc = order_doc(NEW_IDS + uid * 10_000_000 + counter[0], n, rng, datetime.now(timezone.utc))
            with self.conn.transaction():
                execute("INSERT INTO orders (id, customer_id, status, region, total_cents, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                        (doc["_id"], doc["customer_id"], doc["status"], doc["region"], doc["total_cents"], doc["created_at"]))
                with self.conn.cursor() as cursor:
                    cursor.executemany("INSERT INTO order_items (order_id, line, sku, qty, price_cents) VALUES (%s,%s,%s,%s,%s)",
                                       [(doc["_id"], line, item["sku"], item["qty"], item["price_cents"]) for line, item in enumerate(doc["items"])])
        elif op == "Q5":
            execute("UPDATE orders SET status = %s WHERE id = %s", (STATUSES[rng.randrange(5)], rng.randrange(1, n + 1)))
        elif op == "Q6":
            low = START + timedelta(seconds=rng.randrange(SPAN_SECONDS - 7 * 86_400))
            execute("SELECT region, sum(total_cents), count(*) FROM orders WHERE created_at >= %s AND created_at < %s GROUP BY region", (low, low + timedelta(days=7))).fetchall()
        elif op == "Q7":
            execute("SELECT o.*, c.name, c.segment, i.line, i.sku, i.qty, i.price_cents FROM orders o JOIN customers c ON c.id = o.customer_id "
                    "JOIN order_items i ON i.order_id = o.id WHERE o.id = %s", (rng.randrange(1, n + 1),)).fetchall()
        else:
            raise ValueError(op)


class CassandraSystem:
    def __init__(self) -> None:
        from cassandra.cluster import Cluster
        from cassandra.query import BatchStatement, BatchType
        self.BatchStatement, self.BatchType = BatchStatement, BatchType
        self.cluster = Cluster(["cassandra"], connect_timeout=30)
        self.session = self.cluster.connect()
        self._prepared = False

    @staticmethod
    def wait(timeout: float = 420) -> None:
        from cassandra.cluster import Cluster
        deadline = time.time() + timeout
        while True:
            try:
                Cluster(["cassandra"], connect_timeout=10).connect().shutdown()
                return
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(5)

    def version(self) -> str:
        return str(self.session.execute("SELECT release_version FROM system.local").one()[0])

    def reset(self) -> None:
        self.session.execute("DROP KEYSPACE IF EXISTS bench", timeout=120)
        self.session.execute("CREATE KEYSPACE bench WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1}")
        self.session.execute("CREATE TABLE bench.orders_by_id (order_id bigint PRIMARY KEY, customer_id int, status text, region int, total_cents int, created_at timestamp, items text)")
        self.session.execute("CREATE TABLE bench.orders_by_customer (customer_id int, created_at timestamp, order_id bigint, status text, total_cents int, "
                             "PRIMARY KEY ((customer_id), created_at, order_id)) WITH CLUSTERING ORDER BY (created_at DESC, order_id ASC)")

    def _prepare(self) -> None:
        if not self._prepared:
            self.session.set_keyspace("bench")
            self.ins_id = self.session.prepare("INSERT INTO orders_by_id (order_id, customer_id, status, region, total_cents, created_at, items) VALUES (?,?,?,?,?,?,?)")
            self.ins_cust = self.session.prepare("INSERT INTO orders_by_customer (customer_id, created_at, order_id, status, total_cents) VALUES (?,?,?,?,?)")
            self.q1 = self.session.prepare("SELECT order_id, created_at, status, total_cents FROM orders_by_customer WHERE customer_id = ? LIMIT 10")
            self.q3 = self.session.prepare("SELECT * FROM orders_by_id WHERE order_id = ?")
            self._prepared = True

    def load_slice(self, n: int, starts: list[int]) -> None:
        from cassandra.concurrent import execute_concurrent_with_args
        self._prepare()
        for start in starts:
            docs = batch(start, n)
            execute_concurrent_with_args(self.session, self.ins_id, [(d["_id"], d["customer_id"], d["status"], d["region"], d["total_cents"], d["created_at"], json.dumps(d["items"])) for d in docs], concurrency=64)
            execute_concurrent_with_args(self.session, self.ins_cust, [(d["customer_id"], d["created_at"], d["_id"], d["status"], d["total_cents"]) for d in docs], concurrency=64)

    def load_customers(self, n: int) -> None:  # customers are denormalised into the order tables
        return None

    def storage(self) -> dict[str, Any]:
        container = compose_container("cassandra")
        container.exec_run("nodetool flush bench")
        output = container.exec_run(["sh", "-c", "du -sb /var/lib/cassandra/data/bench | cut -f1"]).output.decode().strip()
        return {"storage_bytes": int(output or 0), "data_bytes": int(output or 0), "index_bytes": 0}

    def run(self, op: str, rng: random.Random, n: int, uid: int, counter: list[int]) -> None:
        self._prepare()
        if op == "Q1":
            list(self.session.execute(self.q1, (skewed_customer(rng, n),)))
        elif op == "Q3":
            self.session.execute(self.q3, (rng.randrange(1, n + 1),)).one()
        elif op == "Q4":
            counter[0] += 1
            d = order_doc(NEW_IDS + uid * 10_000_000 + counter[0], n, rng, datetime.now(timezone.utc))
            statement = self.BatchStatement(batch_type=self.BatchType.LOGGED)
            statement.add(self.ins_id, (d["_id"], d["customer_id"], d["status"], d["region"], d["total_cents"], d["created_at"], json.dumps(d["items"])))
            statement.add(self.ins_cust, (d["customer_id"], d["created_at"], d["_id"], d["status"], d["total_cents"]))
            self.session.execute(statement)
        else:
            raise ValueError(op)


class Neo4jSystem:
    def __init__(self) -> None:
        from neo4j import GraphDatabase
        self.driver = GraphDatabase.driver("bolt://neo4j:7687", auth=("neo4j", "benchpassword"))

    @staticmethod
    def wait(timeout: float = 300) -> None:
        from neo4j import GraphDatabase
        deadline = time.time() + timeout
        while True:
            try:
                driver = GraphDatabase.driver("bolt://neo4j:7687", auth=("neo4j", "benchpassword"))
                driver.verify_connectivity()
                driver.close()
                return
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(3)

    def version(self) -> str:
        records, _, _ = self.driver.execute_query("CALL dbms.components() YIELD versions RETURN versions[0] AS v")
        return str(records[0]["v"])

    def reset(self) -> None:
        with self.driver.session() as session:  # CALL ... IN TRANSACTIONS needs an implicit transaction
            session.run("MATCH (n) CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS").consume()
        self.driver.execute_query("CREATE CONSTRAINT customer_id IF NOT EXISTS FOR (c:Customer) REQUIRE c.id IS UNIQUE")
        self.driver.execute_query("CREATE CONSTRAINT order_id IF NOT EXISTS FOR (o:Order) REQUIRE o.id IS UNIQUE")

    def load_customers(self, n: int) -> None:
        ids = list(range(1, customer_count(n) + 1))
        for index in range(0, len(ids), 10_000):
            rows = [{"id": doc["_id"], "name": doc["name"], "segment": doc["segment"], "region": doc["region"]} for doc in map(customer_doc, ids[index:index + 10_000])]
            self.driver.execute_query("UNWIND $rows AS r CREATE (:Customer {id: r.id, name: r.name, segment: r.segment, region: r.region})", rows=rows)

    def load_slice(self, n: int, starts: list[int]) -> None:
        for start in starts:
            rows = [{"id": d["_id"], "customer_id": d["customer_id"], "status": d["status"], "region": d["region"], "total_cents": d["total_cents"],
                     "created_at": int(d["created_at"].timestamp() * 1000), "items": json.dumps(d["items"])} for d in batch(start, n)]
            self.driver.execute_query("UNWIND $rows AS r MATCH (c:Customer {id: r.customer_id}) "
                                      "CREATE (c)-[:PLACED]->(:Order {id: r.id, status: r.status, region: r.region, total_cents: r.total_cents, created_at: r.created_at, items: r.items})", rows=rows)

    def cleanup(self) -> None:
        self.driver.execute_query("MATCH (o:Order) WHERE o.id >= $low DETACH DELETE o", low=NEW_IDS)

    def storage(self) -> dict[str, Any]:
        output = compose_container("neo4j").exec_run(["sh", "-c", "du -sb /data/databases/neo4j | cut -f1"]).output.decode().strip()
        return {"storage_bytes": int(output or 0), "data_bytes": int(output or 0), "index_bytes": 0}

    def run(self, op: str, rng: random.Random, n: int, uid: int, counter: list[int]) -> None:
        if op == "Q1":
            self.driver.execute_query("MATCH (c:Customer {id: $c})-[:PLACED]->(o:Order) RETURN o.id, o.created_at, o.status ORDER BY o.created_at DESC LIMIT 10", c=skewed_customer(rng, n))
        elif op == "Q3":
            self.driver.execute_query("MATCH (o:Order {id: $id}) RETURN o", id=rng.randrange(1, n + 1))
        elif op == "Q4":
            counter[0] += 1
            d = order_doc(NEW_IDS + uid * 10_000_000 + counter[0], n, rng, datetime.now(timezone.utc))
            self.driver.execute_query("MATCH (c:Customer {id: $c}) CREATE (c)-[:PLACED]->(:Order {id: $id, status: $s, region: $r, total_cents: $t, created_at: $at, items: $items})",
                                      c=d["customer_id"], id=d["_id"], s=d["status"], r=d["region"], t=d["total_cents"], at=int(d["created_at"].timestamp() * 1000), items=json.dumps(d["items"]))
        else:
            raise ValueError(op)


class RedisSystem:
    def __init__(self) -> None:
        import redis
        self.redis = redis.Redis(host="redis", port=6379)

    @staticmethod
    def wait(timeout: float = 120) -> None:
        import redis
        deadline = time.time() + timeout
        while True:
            try:
                redis.Redis(host="redis").ping()
                return
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(2)

    def version(self) -> str:
        return str(self.redis.info("server")["redis_version"])

    def reset(self) -> None:
        self.redis.flushall()

    def load_customers(self, n: int) -> None:
        return None

    def _write(self, pipe: Any, d: dict[str, Any]) -> None:
        pipe.hset(f"order:{d['_id']}", mapping={"customer_id": d["customer_id"], "status": d["status"], "region": d["region"], "total_cents": d["total_cents"],
                                                 "created_at": d["created_at"].isoformat(), "items": json.dumps(d["items"])})
        pipe.zadd(f"cust:{d['customer_id']}:orders", {d["_id"]: d["created_at"].timestamp()})

    def load_slice(self, n: int, starts: list[int]) -> None:
        for start in starts:
            pipe = self.redis.pipeline(transaction=False)
            for d in batch(start, n):
                self._write(pipe, d)
            pipe.execute()

    def storage(self) -> dict[str, Any]:
        used = int(self.redis.info("memory")["used_memory"])
        return {"storage_bytes": used, "data_bytes": used, "index_bytes": 0, "note": "in-memory (persistence disabled)"}

    def run(self, op: str, rng: random.Random, n: int, uid: int, counter: list[int]) -> None:
        if op == "Q1":
            ids = self.redis.zrevrange(f"cust:{skewed_customer(rng, n)}:orders", 0, 9)
            pipe = self.redis.pipeline(transaction=False)
            for order_id in ids:
                pipe.hgetall(f"order:{order_id.decode()}")
            pipe.execute()
        elif op == "Q3":
            self.redis.hgetall(f"order:{rng.randrange(1, n + 1)}")
        elif op == "Q4":
            counter[0] += 1
            d = order_doc(NEW_IDS + uid * 10_000_000 + counter[0], n, rng, datetime.now(timezone.utc))
            pipe = self.redis.pipeline(transaction=True)
            self._write(pipe, d)
            pipe.execute()
        else:
            raise ValueError(op)


def make_system(name: str, params: dict[str, Any] | None = None) -> Any:
    params = params or {}
    if name == "mongo":
        return MongoSystem(**params)
    return {"postgres": PostgresSystem, "cassandra": CassandraSystem, "neo4j": Neo4jSystem, "redis": RedisSystem}[name]()


# ---------------------------------------------------------------- docker helpers (via the mounted socket)

def compose_container(service: str) -> Any:
    import docker
    client = docker.from_env()
    found = client.containers.list(all=True, filters={"label": ["com.docker.compose.project=nosqlreport", f"com.docker.compose.service={service}"]})
    if not found:
        raise RuntimeError(f"container for service {service} not found")
    return found[0]


class StatsSampler:
    """Samples CPU and memory of one container about once per second."""

    def __init__(self, service: str) -> None:
        self.container = compose_container(service)
        self.samples: list[dict[str, float]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        started = time.time()
        for stats in self.container.stats(stream=True, decode=True):
            if self._stop.is_set():
                break
            try:
                cpu = stats["cpu_stats"]
                pre = stats["precpu_stats"]
                cpu_delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
                system_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
                cpus = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or [1])
                cpu_percent = (cpu_delta / system_delta) * cpus * 100 if system_delta > 0 else 0.0
                memory = stats["memory_stats"]
                used = memory.get("usage", 0) - memory.get("stats", {}).get("inactive_file", 0)
                self.samples.append({"t": round(time.time() - started, 2), "cpu_percent": round(cpu_percent, 2), "mem_bytes": used, "mem_limit": memory.get("limit", 0), "cpus": cpus})
            except (KeyError, TypeError, ZeroDivisionError):
                continue

    def __enter__(self) -> StatsSampler:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()


# ---------------------------------------------------------------- measurement

def _worker(args: tuple[Any, ...]) -> dict[str, Any]:
    system_name, params, n, mix, duration, warmup, seed, uid = args
    system = make_system(system_name, params)
    rng = random.Random(seed)
    ops, weights = list(mix), list(mix.values())
    counter = [0]
    latencies: dict[str, list[float]] = {op: [] for op in ops}
    errors = 0
    error_kinds: dict[str, int] = {}
    warm_end = time.perf_counter() + warmup
    while time.perf_counter() < warm_end:
        op = rng.choices(ops, weights)[0]
        try:
            system.run(op, rng, n, uid, counter)
        except Exception:
            pass
    started = time.perf_counter()
    end = started + duration
    while True:
        now = time.perf_counter()
        if now >= end:
            break
        op = rng.choices(ops, weights)[0]
        try:
            system.run(op, rng, n, uid, counter)
            latencies[op].append((time.perf_counter() - now) * 1000)
        except Exception as error:  # noqa: BLE001
            errors += 1
            error_kinds[type(error).__name__] = error_kinds.get(type(error).__name__, 0) + 1
    return {"latencies": latencies, "errors": errors, "error_kinds": error_kinds, "elapsed": time.perf_counter() - started}


def _stats(values: np.ndarray, rng: np.random.Generator) -> dict[str, Any]:
    if values.size == 0:
        return {"count": 0}
    sample = values if values.size <= SAMPLE else rng.choice(values, SAMPLE, replace=False)
    return {"count": int(values.size), "mean": float(values.mean()), "p50": float(np.percentile(values, 50)), "p95": float(np.percentile(values, 95)),
            "p99": float(np.percentile(values, 99)), "max": float(values.max()), "sample": [round(float(v), 4) for v in sample]}


def measure(system: str, n: int, mix: dict[str, float], clients: int = 1, reps: int = REPS, duration: float = MEASURE, warmup: float = WARMUP,
            params: dict[str, Any] | None = None, label: str = "") -> dict[str, Any]:
    log(f"   measure {system} {label or ''} mix={'+'.join(mix)} clients={clients} reps={reps}")
    rng = np.random.default_rng(SEED)
    rows = []
    for rep in range(reps):
        args = [(system, params or {}, n, mix, duration, warmup, f"{label}:{clients}:{rep}:{worker}", next(_uid)) for worker in range(clients)]
        with ProcessPoolExecutor(max_workers=clients, mp_context=SPAWN) as pool:
            parts = list(pool.map(_worker, args))
        elapsed = max(part["elapsed"] for part in parts)
        per_op = {op: np.array([v for part in parts for v in part["latencies"][op]], dtype=float) for op in mix}
        overall = np.concatenate(list(per_op.values())) if per_op else np.array([])
        errors = sum(part["errors"] for part in parts)
        kinds: dict[str, int] = {}
        for part in parts:
            for kind, count in part["error_kinds"].items():
                kinds[kind] = kinds.get(kind, 0) + count
        row = {"rep": rep, "elapsed_s": elapsed, "ops": int(overall.size), "errors": errors, "error_kinds": kinds,
               "throughput": overall.size / elapsed if elapsed else 0.0, "error_rate": errors / max(errors + overall.size, 1),
               "overall": _stats(overall, rng), "per_op": {op: _stats(values, rng) for op, values in per_op.items()}}
        rows.append(row)
        # Remove documents inserted during this repetition so every repetition sees the same data set.
        cleaner = getattr(make_system(system, params), "cleanup", None)
        if cleaner and any(op == "Q4" for op in mix):
            cleaner()
    return {"system": system, "records": n, "mix": mix, "clients": clients, "reps": rows, "measure_s": duration, "warmup_s": warmup}


def load(system_name: str, n: int, params: dict[str, Any] | None = None, processes: int = 4) -> dict[str, Any]:
    system = make_system(system_name, params)
    system.reset()
    started = time.perf_counter()
    system.load_customers(n)
    customers_s = time.perf_counter() - started
    slices = split(batch_starts(n), processes)
    started = time.perf_counter()
    with ProcessPoolExecutor(max_workers=processes, mp_context=SPAWN) as pool:
        list(pool.map(_load_slice, [(system_name, params or {}, n, part) for part in slices]))
    orders_s = time.perf_counter() - started
    log(f"   loaded {n:,} orders into {system_name} in {orders_s:.1f}s")
    return {"records": n, "customers": customer_count(n), "load_orders_s": orders_s, "load_customers_s": customers_s}


def _load_slice(args: tuple[Any, ...]) -> None:
    system_name, params, n, starts = args
    make_system(system_name, params).load_slice(n, starts)


# ---------------------------------------------------------------- suites

def scale_series(name: str, system_name: str, results: dict[str, Any], main: Callable[[], None]) -> None:
    results.setdefault("scale", {})
    for n in SCALES:
        log(f"== {system_name} at {n:,} records")
        entry: dict[str, Any] = {}
        results["scale"][str(n)] = entry

        def step() -> None:
            entry["load"] = load(system_name, n)
            system = make_system(system_name)
            entry["index_build_s"] = system.create_indexes()
            entry["storage"] = system.storage()
            entry["read_Q1"] = measure(system_name, n, {"Q1": 1.0}, label=f"scale{n}")
            entry["write_Q4"] = measure(system_name, n, {"Q4": 1.0}, label=f"scale{n}")
        guarded(results, f"scale_{n}", name, step)
        if n == MAIN_SCALE:
            main()


def suite_mongo() -> None:
    name = "mongo"
    results: dict[str, Any] = {"main_scale": MAIN_SCALE, "scales": SCALES, "started": datetime.now(timezone.utc)}
    mongo = MongoSystem()
    mongo.wait()
    results["version"] = mongo.version()
    n = MAIN_SCALE

    def main() -> None:
        log(f"== mongo main experiments at {n:,}")
        guarded(results, "queries", name, lambda: {op: measure("mongo", n, {op: 1.0}, label="query") for op in ("Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7")})

        def indexing() -> dict[str, Any]:
            out: dict[str, Any] = {"index_sizes": mongo.storage()["index_sizes"]}
            mongo.drop_indexes()
            out["no_index"] = {op: measure("mongo", n, {op: 1.0}, label="noindex") for op in ("Q1", "Q2", "Q6", "Q4")}
            out["rebuild_s"] = mongo.create_indexes()
            out["with_index_Q4"] = measure("mongo", n, {"Q4": 1.0}, label="withindex")
            return out
        guarded(results, "indexing", name, indexing)

        def compound_vs_single() -> dict[str, Any]:
            mongo.db.orders.drop_index("ix_customer_created")
            mongo.db.orders.create_index([("customer_id", 1)], name="ix_customer_only")
            single = measure("mongo", n, {"Q1": 1.0}, label="single")
            w5 = measure("mongo", n, WORKLOADS["W5"], clients=8, label="single-w5")
            mongo.db.orders.drop_index("ix_customer_only")
            mongo.create_indexes(["ix_customer_created"])
            return {"single_field_Q1": single, "single_field_W5_8": w5}
        guarded(results, "ablation_compound", name, compound_vs_single)

        def embedding_vs_referencing() -> dict[str, Any]:
            db = mongo.db
            db.orders.aggregate([{"$match": {"_id": {"$lte": n}}}, {"$project": {"items": 0}}, {"$out": "orders_ref"}])
            db.orders.aggregate([{"$match": {"_id": {"$lte": n}}}, {"$unwind": {"path": "$items", "includeArrayIndex": "line"}},
                                 {"$project": {"_id": 0, "order_id": "$_id", "line": 1, "sku": "$items.sku", "qty": "$items.qty", "price_cents": "$items.price_cents"}},
                                 {"$out": "order_items"}])
            db.order_items.create_index([("order_id", 1)])
            embedded = measure("mongo", n, {"Q7": 1.0}, label="embedded")
            referenced = measure("mongo", n, {"Q7R": 1.0}, label="referenced")
            out = {"embedded_Q7": embedded, "referenced_Q7": referenced,
                   "referenced_storage_bytes": mongo.coll_bytes("orders_ref") + mongo.coll_bytes("order_items"),
                   "embedded_storage_bytes": mongo.coll_bytes("orders")}
            db.drop_collection("orders_ref")
            db.drop_collection("order_items")
            return out
        guarded(results, "ablation_embedding", name, embedding_vs_referencing)

        guarded(results, "workloads", name, lambda: {w: {str(c): measure("mongo", n, mix, clients=c, label=w) for c in CLIENTS} for w, mix in WORKLOADS.items()})
        guarded(results, "core", name, lambda: {str(c): measure("mongo", n, CORE, clients=c, label="core") for c in (1, 8)})

        def resources() -> dict[str, Any]:
            with StatsSampler("mongo") as sampler:
                run = measure("mongo", n, WORKLOADS["W5"], clients=8, reps=1, duration=60, warmup=5, label="resources")
                time.sleep(1)
            return {"samples": sampler.samples, "run": run}
        guarded(results, "resources", name, resources)

        def schema_change() -> dict[str, Any]:
            started = time.perf_counter()
            mongo.db.orders.update_many({}, {"$set": {"loyalty_points": 0}})
            backfill = time.perf_counter() - started
            return {"add_field_ddl_s": 0.0, "backfill_s": backfill, "note": "no DDL needed; backfill optional"}
        guarded(results, "schema_change", name, schema_change)

        def cold_vs_warm() -> dict[str, Any]:
            out = {}
            for op in ("Q1", "Q3"):
                compose_container("mongo").restart(timeout=30)
                fresh = MongoSystem()
                fresh.wait()
                rng = random.Random(f"cold:{op}")
                cold = []
                for _ in range(300):
                    started = time.perf_counter()
                    fresh.run(op, rng, n, 0, [0])
                    cold.append((time.perf_counter() - started) * 1000)
                warm = measure("mongo", n, {op: 1.0}, label=f"warm-{op}")
                out[op] = {"cold_first_300_ms": cold, "warm": warm}
            return out
        guarded(results, "cold_warm", name, cold_vs_warm)

    scale_series(name, "mongo", results, main)
    results["finished"] = datetime.now(timezone.utc)
    save(name, results)


def suite_postgres() -> None:
    name = "postgres"
    PostgresSystem.wait()
    pg = PostgresSystem()
    results: dict[str, Any] = {"main_scale": MAIN_SCALE, "scales": SCALES, "version": pg.version(), "started": datetime.now(timezone.utc)}
    n = MAIN_SCALE

    def main() -> None:
        log(f"== postgres main experiments at {n:,}")
        guarded(results, "queries", name, lambda: {op: measure("postgres", n, {op: 1.0}, label="query") for op in ("Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7")})

        def indexing() -> dict[str, Any]:
            local = PostgresSystem()
            out: dict[str, Any] = {"index_sizes": local.storage()["index_sizes"]}
            local.drop_indexes()
            out["no_index"] = {op: measure("postgres", n, {op: 1.0}, label="noindex") for op in ("Q1", "Q2", "Q6", "Q4")}
            out["rebuild_s"] = local.create_indexes(["ix_customer_created", "ix_status_region", "ix_created"])
            return out
        guarded(results, "indexing", name, indexing)
        guarded(results, "workloads", name, lambda: {w: {str(c): measure("postgres", n, WORKLOADS[w], clients=c, label=w) for c in (1, 8)} for w in ("W1", "W3", "W5")})
        guarded(results, "core", name, lambda: {str(c): measure("postgres", n, CORE, clients=c, label="core") for c in (1, 8)})

        def schema_change() -> dict[str, Any]:
            local = PostgresSystem()
            started = time.perf_counter()
            local.conn.execute("ALTER TABLE orders ADD COLUMN loyalty_points integer")
            ddl = time.perf_counter() - started
            started = time.perf_counter()
            local.conn.execute("UPDATE orders SET loyalty_points = 0")
            backfill = time.perf_counter() - started
            local.conn.execute("ALTER TABLE orders DROP COLUMN loyalty_points")
            return {"add_field_ddl_s": ddl, "backfill_s": backfill}
        guarded(results, "schema_change", name, schema_change)

    scale_series(name, "postgres", results, main)
    results["finished"] = datetime.now(timezone.utc)
    save(name, results)


def suite_other(system_name: str) -> None:
    """Cassandra, Neo4j and Redis on the common core workload at the main scale."""
    name = system_name
    {"cassandra": CassandraSystem, "neo4j": Neo4jSystem, "redis": RedisSystem}[system_name].wait()
    system = make_system(system_name)
    results: dict[str, Any] = {"records": MAIN_SCALE, "version": system.version(), "started": datetime.now(timezone.utc)}
    n = MAIN_SCALE
    guarded(results, "load", name, lambda: load(system_name, n, processes=1 if system_name == "neo4j" else 4))
    guarded(results, "storage", name, lambda: make_system(system_name).storage())
    guarded(results, "core", name, lambda: {str(c): measure(system_name, n, CORE, clients=c, label="core") for c in (1, 8)})
    results["finished"] = datetime.now(timezone.utc)
    save(name, results)


def suite_replica_set() -> None:
    """Write concern, replication lag, stale reads and failover on a three-member replica set."""
    from pymongo import MongoClient, ReadPreference
    from pymongo.read_concern import ReadConcern
    from pymongo.write_concern import WriteConcern
    name = "replica_set"
    results: dict[str, Any] = {"members": 3, "started": datetime.now(timezone.utc)}
    for host in ("rs1", "rs2", "rs3"):
        MongoSystem(f"mongodb://{host}:27017/?directConnection=true").wait()
    seed = MongoClient("mongodb://rs1:27017/?directConnection=true")
    try:
        seed.admin.command("replSetInitiate", {"_id": "rsb", "members": [{"_id": 0, "host": "rs1:27017"}, {"_id": 1, "host": "rs2:27017"}, {"_id": 2, "host": "rs3:27017"}]})
    except Exception as error:  # already initiated on a rerun
        log(f"   replSetInitiate: {error}")
    uri = "mongodb://rs1:27017,rs2:27017,rs3:27017/?replicaSet=rsb"

    def primary_ready(timeout: float = 120) -> MongoClient:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                client = MongoClient(uri, serverSelectionTimeoutMS=5_000)
                status = client.admin.command("replSetGetStatus")
                states = [member["stateStr"] for member in status["members"]]
                if states.count("PRIMARY") == 1 and states.count("SECONDARY") == 2:
                    return client
            except Exception:
                pass
            time.sleep(2)
        raise RuntimeError("replica set did not become healthy")

    client = primary_ready()
    results["version"] = client.admin.command("buildInfo")["version"]
    n = 100_000
    guarded(results, "load", name, lambda: load("mongo", n, params={"uri": uri}))
    settings = {"weak": WriteConcern(w=1), "majority": WriteConcern(w="majority")}

    def write_latency() -> dict[str, Any]:
        out = {}
        for label, concern in settings.items():
            coll = client["bench"].get_collection("rs_probe", write_concern=concern)
            latencies = []
            for rep in range(REPS):
                end = time.perf_counter() + MEASURE
                rep_lat = []
                while time.perf_counter() < end:
                    started = time.perf_counter()
                    coll.insert_one({"rep": rep, "at": datetime.now(timezone.utc)})
                    rep_lat.append((time.perf_counter() - started) * 1000)
                latencies.append({"p50": float(np.percentile(rep_lat, 50)), "p95": float(np.percentile(rep_lat, 95)), "mean": float(np.mean(rep_lat)), "count": len(rep_lat)})
            out[label] = latencies
        return out
    guarded(results, "write_latency", name, write_latency)

    def lag_and_stale() -> dict[str, Any]:
        status = client.admin.command("replSetGetStatus")
        secondaries = [member["name"] for member in status["members"] if member["stateStr"] == "SECONDARY"]
        readers = [MongoClient(f"mongodb://{host}/?directConnection=true", read_preference=ReadPreference.SECONDARY_PREFERRED) for host in secondaries]
        out: dict[str, Any] = {"secondaries": secondaries}
        for label, concern in settings.items():
            coll = client["bench"].get_collection("rs_lag", write_concern=concern)
            read_concern = ReadConcern("majority") if label == "majority" else ReadConcern("local")
            reader_colls = [reader["bench"].get_collection("rs_lag", read_concern=read_concern) for reader in readers]
            lags, stale = [], 0
            for index in range(300):
                key = f"{label}-{index}-{time.time_ns()}"
                coll.insert_one({"_id": key})
                acked = time.perf_counter()
                if reader_colls[0].find_one({"_id": key}) is None:
                    stale += 1
                pending = set(range(len(reader_colls)))
                while pending and time.perf_counter() - acked < 5:
                    for position in list(pending):
                        if reader_colls[position].find_one({"_id": key}) is not None:
                            pending.discard(position)
                lags.append((time.perf_counter() - acked) * 1000)
            out[label] = {"replication_lag_ms": lags, "stale_reads": stale, "reads": 300}
        # Read-your-writes with a causally consistent session, for comparison.
        with client.start_session(causal_consistency=True) as session:
            coll = client["bench"].get_collection("rs_lag", write_concern=WriteConcern(w="majority"), read_concern=ReadConcern("majority"),
                                                   read_preference=ReadPreference.SECONDARY)
            stale = 0
            for index in range(300):
                key = f"causal-{index}-{time.time_ns()}"
                coll.insert_one({"_id": key}, session=session)
                if coll.find_one({"_id": key}, session=session) is None:
                    stale += 1
            out["majority_causal"] = {"stale_reads": stale, "reads": 300}
        return out
    guarded(results, "lag_stale", name, lag_and_stale)

    def healthy_primary(timeout: float) -> str | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                return str(MongoClient(uri, serverSelectionTimeoutMS=5_000).admin.command("hello")["primary"]).split(":")[0]
            except Exception:
                time.sleep(1)
        return None

    def failover() -> dict[str, Any]:
        out: dict[str, Any] = {"election_timeout_ms": client.admin.command("replSetGetConfig")["config"]["settings"].get("electionTimeoutMillis")}
        for label, concern in settings.items():
            trials = []
            for trial in range(3):
                healthy = primary_ready(180)
                primary = healthy.admin.command("hello")["primary"].split(":")[0]
                writer = MongoClient(uri, serverSelectionTimeoutMS=2_000, retryWrites=True)
                coll = writer["bench"].get_collection("rs_failover", write_concern=concern)
                events: list[tuple[float, float, bool]] = []  # (started, finished, acknowledged)
                stop = threading.Event()

                def loop() -> None:
                    while not stop.is_set():
                        started = time.perf_counter()
                        try:
                            coll.insert_one({"t": started})
                            events.append((started, time.perf_counter(), True))
                        except Exception:
                            events.append((started, time.perf_counter(), False))
                        time.sleep(0.01)
                thread = threading.Thread(target=loop, daemon=True)
                thread.start()
                time.sleep(5)
                killed_at = time.perf_counter()
                compose_container(primary).kill()
                time.sleep(40)
                stop.set()
                thread.join(10)
                # Recovery: until a write that was issued after the kill is acknowledged by the new primary.
                first_ok = next((end_ for start_, end_, ok in events if ok and start_ >= killed_at), None)
                post = [(start_, end_, ok) for start_, end_, ok in events if start_ >= killed_at - 1]
                new_primary = healthy_primary(30)
                trials.append({"killed": primary, "new_primary": new_primary, "recovery_s": (first_ok - killed_at) if first_ok else None,
                               "failed_attempts": sum(1 for _, _, ok in post if not ok), "acknowledged_after": sum(1 for _, _, ok in post if ok),
                               "timeline": [[round(start_ - killed_at, 3), round(end_ - killed_at, 3), ok] for start_, end_, ok in post if end_ - killed_at <= 20]})
                compose_container(primary).start()
                writer.close()
            out[label] = trials
            save(name, results)
        return out
    guarded(results, "failover", name, failover)
    results["finished"] = datetime.now(timezone.utc)
    save(name, results)


def suite_shard(shards: int) -> None:
    from pymongo import MongoClient
    name = f"shard_{shards}"
    results: dict[str, Any] = {"shards": shards, "started": datetime.now(timezone.utc)}

    def init(host: str, config: dict[str, Any]) -> None:
        MongoSystem(f"mongodb://{host}:27017/?directConnection=true").wait()
        client = MongoClient(f"mongodb://{host}:27017/?directConnection=true")
        try:
            client.admin.command("replSetInitiate", config)
        except Exception as error:
            log(f"   {host}: {error}")
        deadline = time.time() + 120
        while time.time() < deadline:
            if client.admin.command("hello").get("isWritablePrimary"):
                return
            time.sleep(1)
        raise RuntimeError(f"{host} did not become primary")

    init("cfg", {"_id": "cfg", "configsvr": True, "members": [{"_id": 0, "host": "cfg:27017"}]})
    for index in range(1, shards + 1):
        init(f"sh{index}", {"_id": f"sh{index}", "members": [{"_id": 0, "host": f"sh{index}:27017"}]})
    router = MongoSystem("mongodb://mongos:27017/")
    router.wait(300)
    for index in range(1, shards + 1):
        router.client.admin.command("addShard", f"sh{index}/sh{index}:27017")
    router.client.admin.command("enableSharding", "bench")
    router.client.admin.command("shardCollection", "bench.orders", key={"customer_id": "hashed"})
    results["version"] = router.version()
    n = MAIN_SCALE
    params = {"uri": "mongodb://mongos:27017/"}

    def run_load() -> dict[str, Any]:
        # load() resets collections, so keep the sharded orders collection and only refresh data.
        router.db.orders.delete_many({})
        router.db.customers.drop()
        started = time.perf_counter()
        router.load_customers(n)
        slices = split(batch_starts(n), 4)
        with ProcessPoolExecutor(max_workers=4, mp_context=SPAWN) as pool:
            list(pool.map(_load_slice, [("mongo", params, n, part) for part in slices]))
        elapsed = time.perf_counter() - started
        return {"records": n, "load_s": elapsed, "index_build_s": router.create_indexes()}
    guarded(results, "load", name, run_load)
    guarded(results, "distribution", name, lambda: router.storage())
    guarded(results, "W5", name, lambda: {str(c): measure("mongo", n, WORKLOADS["W5"], clients=c, params=params, label="shard-w5") for c in (1, 8)})
    guarded(results, "core", name, lambda: {str(c): measure("mongo", n, CORE, clients=c, params=params, label="shard-core") for c in (1, 8)})
    results["finished"] = datetime.now(timezone.utc)
    save(name, results)


def environment() -> None:
    import docker
    info = docker.from_env().info()
    save("environment", {"docker": {key: info.get(key) for key in ("ServerVersion", "OperatingSystem", "KernelVersion", "Architecture", "NCPU", "MemTotal")},
                         "python": sys.version, "reps": REPS, "warmup_s": WARMUP, "measure_s": MEASURE, "scales": SCALES, "main_scale": MAIN_SCALE,
                         "workloads": WORKLOADS, "core": CORE, "seed": SEED, "recorded": datetime.now(timezone.utc)})


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    log(f"suite {command} starting (reps={REPS}, measure={MEASURE}s, scales={SCALES})")
    if command == "environment":
        environment()
    elif command == "mongo":
        suite_mongo()
    elif command == "postgres":
        suite_postgres()
    elif command in ("cassandra", "neo4j", "redis"):
        suite_other(command)
    elif command == "rs":
        suite_replica_set()
    elif command == "shard":
        suite_shard(int(sys.argv[2]))
    else:
        raise SystemExit("usage: bench.py environment|mongo|postgres|cassandra|neo4j|redis|rs|shard N")
    log(f"suite {command} finished")

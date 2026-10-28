# Report benchmarks

Measurements behind Chapters 8–10 of the final report: query latency, indexing,
scalability (10K–10M records), concurrency, resource use, replica-set consistency
and failover, sharding, a PostgreSQL baseline and a comparison with Cassandra,
Neo4j and Redis.

```bash
docker compose --profile light down      # free memory used by the dev stack
bash benchmarks/report/run_all.sh        # about 3 hours, unattended
```

- Databases run one at a time in the `nosqlreport` Compose project, which publishes no
  host ports, so it never clashes with the application stack.
- The client runs in a container (`python:3.11-slim`), so nothing is installed on the host.
- Results are written to `artifacts/report-bench/` as each experiment finishes,
  along with `host.txt` (hardware) and `run.log`.
- Rerunning skips suites that already finished. To run some suites again, delete their
  `.json` file, or use `ONLY="postgres neo4j" bash benchmarks/report/run_all.sh`.
- Defaults: 5 repetitions per measurement, 3 s warm-up, 10 s measurement window
  (`REPS`, `WARMUP_SECONDS`, `MEASURE_SECONDS`). The 10M scale is skipped automatically
  when Docker has less than 6 GB of memory.

| Suite | What it measures |
|---|---|
| `mongo` | Q1–Q7 latency, index effect, compound vs single-field index, embedding vs referencing, W1–W5 at 1/2/4/8 clients, CPU/memory, schema change, cold vs warm cache, 10K–10M scaling |
| `postgres` | the same queries on a normalised schema, the index effect, workloads, schema change, scaling |
| `cassandra`, `neo4j`, `redis` | load time, storage, and the common core workload (Q1 70 %, Q3 15 %, Q4 15 %) at 1 and 8 clients |
| `replica_set` | write latency with w:1 vs w:majority, replication lag, stale reads, failover recovery |
| `shard_1`, `shard_2`, `shard_4` | W5 and core workloads through mongos with 1, 2 and 4 shards |

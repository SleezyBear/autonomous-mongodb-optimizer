#!/usr/bin/env bash
# Runs every benchmark behind the report, one database at a time, unattended.
# Results: artifacts/report-bench/*.json, log: artifacts/report-bench/run.log
# Re-running skips suites that already finished; delete a suite's .json to redo it.
#   ONLY="postgres redis" bash benchmarks/report/run_all.sh   -> run just those suites
set -u
cd "$(dirname "$0")"
OUT=../../artifacts/report-bench
mkdir -p "$OUT"
LOG="$OUT/run.log"
exec > >(tee -a "$LOG") 2>&1
DC="docker compose"

# Keep the Mac awake for the whole run.
if command -v caffeinate >/dev/null 2>&1; then caffeinate -dimsu -w $$ & fi

echo "=== benchmark run started $(date)"
{
  echo "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "cpu: $(sysctl -n machdep.cpu.brand_string 2>/dev/null || grep -m1 'model name' /proc/cpuinfo | cut -d: -f2)"
  echo "cores: $(sysctl -n hw.ncpu 2>/dev/null || nproc)"
  echo "perf_cores: $(sysctl -n hw.perflevel0.physicalcpu 2>/dev/null || echo n/a)"
  echo "memory_bytes: $(sysctl -n hw.memsize 2>/dev/null || grep MemTotal /proc/meminfo)"
  echo "os: $(sw_vers -productName 2>/dev/null) $(sw_vers -productVersion 2>/dev/null || uname -sr)"
  echo "model: $(sysctl -n hw.model 2>/dev/null || echo n/a)"
  echo "disk: $(df -h . | tail -1)"
  echo "docker: $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
  echo "docker_cpus: $(docker info --format '{{.NCPU}}' 2>/dev/null)"
  echo "docker_memory_bytes: $(docker info --format '{{.MemTotal}}' 2>/dev/null)"
} > "$OUT/host.txt"
cat "$OUT/host.txt"

DOCKER_MEM=$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)
if [ -z "${SCALES:-}" ]; then
  if [ "$DOCKER_MEM" -lt 6000000000 ]; then
    export SCALES="10000,100000,1000000"
    echo "Docker has less than 6 GB of memory: skipping the 10M-record scale."
  else
    export SCALES="10000,100000,1000000,10000000"
  fi
fi

$DC down -v --remove-orphans >/dev/null 2>&1
echo "=== building the benchmark client"
$DC --profile client build client || { echo "client build failed"; exit 1; }

wanted() { [ -z "${ONLY:-}" ] && return 0; case " $ONLY " in *" $1 "*) return 0;; esac; return 1; }
finished() { [ -f "$OUT/$1.json" ] && grep -q '"finished"' "$OUT/$1.json"; }

# run_suite <result-name> <profile> "<services>" <bench.py args...>
run_suite() {
  local name=$1 profile=$2 services=$3; shift 3
  wanted "$name" || return 0
  if finished "$name"; then echo "=== $name already finished, skipping"; return 0; fi
  echo "=== $name: starting $services ($(date +%H:%M))"
  local started=$SECONDS
  $DC --profile "$profile" up -d $services
  $DC --profile client run --rm client python bench.py "$@"
  local rc=$?
  echo "=== $name: done in $(( (SECONDS - started) / 60 )) min (exit $rc)"
  $DC --profile "$profile" down -v --remove-orphans >/dev/null 2>&1
}

wanted environment && $DC --profile client run --rm client python bench.py environment
run_suite mongo     mongo     "mongo"                       mongo
run_suite postgres  postgres  "postgres"                    postgres
run_suite redis     redis     "redis"                       redis
run_suite cassandra cassandra "cassandra"                   cassandra
run_suite neo4j     neo4j     "neo4j"                       neo4j
run_suite replica_set rs      "rs1 rs2 rs3"                 rs
run_suite shard_1   shard     "cfg sh1 mongos"              shard 1
run_suite shard_2   shard     "cfg sh1 sh2 mongos"          shard 2
run_suite shard_4   shard     "cfg sh1 sh2 sh3 sh4 mongos"  shard 4

$DC down -v --remove-orphans >/dev/null 2>&1
echo
echo "=== benchmark run finished $(date)"
echo "Results in artifacts/report-bench:"
for f in "$OUT"/*.json; do
  [ -f "$f" ] || continue
  if grep -q '"errors": {' "$f"; then status="finished with some errors (see file)";
  elif grep -q '"finished"' "$f"; then status="ok"; else status="incomplete"; fi
  printf '  %-22s %s\n' "$(basename "$f")" "$status"
done

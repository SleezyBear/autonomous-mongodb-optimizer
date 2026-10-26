#!/usr/bin/env sh
set -eu

mongo_host="$1"
replica_set="$2"
admin_uri="mongodb://control_plane_root:control_plane_root_dev_only@${mongo_host}:27017/admin?authSource=admin&directConnection=true"

mongosh "$admin_uri" --quiet --eval "
  try {
    rs.status();
  } catch (error) {
    rs.initiate({_id: '$replica_set', members: [{_id: 0, host: '$mongo_host:27017'}]});
  }
"

if [ "$mongo_host" = "mongo-monitored" ]; then
  mongosh "$admin_uri" --quiet --eval '
    const admin = db.getSiblingDB("admin");
    const commerce = db.getSiblingDB("commerce");

    // The declared role definitions are authoritative for both new and existing
    // databases: create when absent, reconcile (updateRole) when present so a
    // retained volume converges to the same privileges as a fresh environment.
    // Read-only telemetry: slow-query log, in-progress ops, $queryStats, and an
    // already-enabled profiler.  The observer can never write application data.
    const observerPrivileges = [
      {resource: {cluster: true}, actions: ["serverStatus", "replSetGetStatus", "getParameter", "listDatabases", "getLog", "inprog", "queryStatsRead"]},
      {resource: {db: "commerce", collection: ""}, actions: ["find", "listCollections", "listIndexes", "collStats", "dbStats"]},
      {resource: {db: "commerce", collection: "system.profile"}, actions: ["find"]},
      {resource: {db: "admin", collection: ""}, actions: ["enableProfiler"]}
    ];
    const executorPrivileges = [
      {resource: {cluster: true}, actions: ["serverStatus", "replSetGetStatus", "getParameter", "listDatabases", "querySettings"]},
      {resource: {db: "commerce", collection: ""}, actions: ["find", "listCollections", "listIndexes", "createIndex", "dropIndex", "collStats", "dbStats"]}
    ];

    if (admin.getRole("optimizerObserver") === null) {
      admin.createRole({
        role: "optimizerObserver",
        privileges: observerPrivileges,
        roles: []
      });
    } else {
      admin.updateRole("optimizerObserver", {
        privileges: observerPrivileges,
        roles: []
      });
    }
    if (admin.getRole("optimizerExecutor") === null) {
      admin.createRole({
        role: "optimizerExecutor",
        privileges: executorPrivileges,
        roles: []
      });
    } else {
      admin.updateRole("optimizerExecutor", {
        privileges: executorPrivileges,
        roles: []
      });
    }
    if (admin.getUser("optimizer_observer") === null) {
      admin.createUser({user: "optimizer_observer", pwd: "observer_dev_only", roles: [{role: "optimizerObserver", db: "admin"}]});
    }
    if (admin.getUser("optimizer_executor") === null) {
      admin.createUser({user: "optimizer_executor", pwd: "executor_dev_only", roles: [{role: "optimizerExecutor", db: "admin"}]});
    }
    if (!commerce.getCollectionNames().includes("permission_probe")) {
      commerce.createCollection("permission_probe");
    }
  '
fi

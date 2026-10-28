#!/usr/bin/env bash
# Brings the local stack up for the report screenshots: users, workload, and a mongosh CRUD demo.
set -u
cd "$(dirname "$0")/.."
export JWT_SIGNING_KEY="$(cat .secrets/jwt_signing_key)"
echo "== starting the stack (this rebuilds the images; a few minutes)"
make dev || exit 1
echo "== waiting for the API"
for i in $(seq 1 90); do curl -fsS http://localhost:5173/api/v1/health/ready >/dev/null 2>&1 && break; curl -fsS http://localhost:8000/health/ready >/dev/null 2>&1 && break; sleep 2; done
export CONTROL_PLANE_PASSWORD="$(cat .secrets/screenshot_password)"
./nosql/bin/python scripts/manage_users.py create --email report-ops@example.com --role OPERATOR || true
./nosql/bin/python scripts/manage_users.py create --email report-lead@example.com --role APPROVER || true
unset CONTROL_PLANE_PASSWORD
echo "== driving the workload for 5 minutes (leave this window open)"
./nosql/bin/python scripts/dev_workload.py --reset --seed --duration 300
clear
echo "== CRUD on commerce.orders (mongosh)"
docker compose exec -T mongo-monitored mongosh --quiet -u control_plane_root -p control_plane_root_dev_only --authenticationDatabase admin commerce --eval '
print("> db.orders.insertOne({ _id: 10000001, customer_id: 1407, status: \"pending\", region: 3, total_cents: 7449, created_at: new Date(), items: [ { sku: 18342, qty: 1, price_cents: 7449 } ] })");
printjson(db.orders.insertOne({ _id: 10000001, customer_id: 1407, status: "pending", region: 3, total_cents: 7449, created_at: new Date(), items: [ { sku: 18342, qty: 1, price_cents: 7449 } ] }));
print("\n> db.orders.find({ customer_id: 1407 }, { items: 0 }).sort({ created_at: -1 }).limit(3)");
printjson(db.orders.find({ customer_id: 1407 }, { items: 0 }).sort({ created_at: -1 }).limit(3).toArray());
print("\n> db.orders.find({ customer_id: 1407 }).sort({ created_at: -1 }).limit(10).explain().queryPlanner.winningPlan");
const p = db.orders.find({ customer_id: 1407 }).sort({ created_at: -1 }).limit(10).explain().queryPlanner.winningPlan;
print(JSON.stringify(p, (k, v) => (k === "indexBounds" || k === "keyPattern" || k === "stage" || k === "inputStage" || k === "indexName" || k === "limitAmount" || /^\d+$/.test(k) || k === "") ? v : undefined, 1));
print("\n> db.orders.updateOne({ _id: 10000001 }, { $set: { status: \"paid\" } })");
printjson(db.orders.updateOne({ _id: 10000001 }, { $set: { status: "paid" } }));
print("\n> db.orders.deleteOne({ _id: 10000001 })");
printjson(db.orders.deleteOne({ _id: 10000001 }));
'
echo
echo "== done. Leave this window as it is; Claude will take the screenshots."

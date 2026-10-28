#!/bin/bash
# mongosh CRUD demo on commerce.orders for the report screenshot; output is also saved for the report.
cd "$(dirname "$0")/.."
export JWT_SIGNING_KEY="$(cat .secrets/jwt_signing_key)"
clear
{
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
} 2>&1 | tee artifacts/report-bench/crud_demo.txt

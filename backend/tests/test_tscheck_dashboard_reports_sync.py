"""Criterion: Dashboard and reports sync and persistence."""

from helpers import act_as


def test_overview_reflects_offline_approval_after_refresh_and_reports_columns(client, fresh_customer):
    act_as(client, "u-ops")
    before = client.get("/dashboard/overview").json()["order_summary"]["awaiting_approval"]

    act_as(client, "u-sales")
    order = client.post("/orders", json={
        "customer_id": fresh_customer["id"], "product": "tscheck-sync-part", "quantity": 6,
        "unit_price": 15.0, "expected_delivery": "2027-01-01", "source": "offline", "payment_terms": "credit",
    }).json()

    # "refresh" = re-fetch the overview endpoint fresh; count rose by at least our own order
    # (other tscheck tests may run concurrently under xdist and also move the shared counter)
    act_as(client, "u-ops")
    after = client.get("/dashboard/overview").json()["order_summary"]["awaiting_approval"]
    assert after >= before + 1

    act_as(client, "u-ops")
    decided = client.post(f"/orders/{order['id']}/decision", json={"decision": "approve", "reason": ""}).json()
    assert decided["status"] == "confirmed"

    # the order itself persists its confirmed state on a fresh fetch (simulates page refresh)
    refetched = client.get(f"/orders/{order['id']}").json()
    assert refetched["status"] == "confirmed"

    after2 = client.get("/dashboard/overview").json()["order_summary"]
    assert after2["confirmed"] >= 1

    # Reports -> Orders includes the expected columns and the fixture order's data
    r = client.get("/reports/orders")
    assert r.status_code == 200, r.text
    report = r.json()
    col_keys = {c["key"].lower() for c in report["columns"]}
    for expected in ("source", "value", "paid", "balance", "produced"):
        assert any(expected in k for k in col_keys), (expected, col_keys)

    rows = report["rows"]
    match = next((row for row in rows if row.get("order_no") == order["order_no"]
                  or row.get("Order No") == order["order_no"]), None)
    assert match is not None, (order["order_no"], rows[:3] if rows else rows)

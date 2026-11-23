"""Criterion: Offline order submission creates an approval request."""

from helpers import act_as


def test_offline_order_awaiting_approval(client, fresh_customer):
    act_as(client, "u-ops")

    # dashboard count before
    before = client.get("/dashboard/overview").json()
    awaiting_before = before["order_summary"]["awaiting_approval"]

    act_as(client, "u-sales")

    r = client.post("/orders", json={
        "customer_id": fresh_customer["id"], "product": "tscheck-widget", "quantity": 5,
        "unit_price": 50.0, "expected_delivery": "2027-01-01", "source": "offline",
        "payment_terms": "credit",
    })
    assert r.status_code == 201, r.text
    order = r.json()
    assert order["status"] == "awaiting_approval"
    assert order["submitted_by"]["name"] == "Emily Ross"

    # notifications feed for manager (u-ops) lists the order
    act_as(client, "u-ops")
    notif = client.get("/notifications").json()
    titles = [n["title"] for n in notif["items"]]
    assert any(order["order_no"] in t and "awaiting approval" in t for t in titles), titles

    # dashboard overview awaiting_approval count increments by at least 1 (parallel tscheck
    # tests under xdist may also touch the shared counter concurrently)
    after = client.get("/dashboard/overview").json()
    assert after["order_summary"]["awaiting_approval"] >= awaiting_before + 1

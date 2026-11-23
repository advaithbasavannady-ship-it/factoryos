"""Criterion: Audit trail - the order timeline lists every step chronologically with
user name, role and timestamp."""

from helpers import act_as, idempotency_headers, new_idempotency_key


def test_timeline_chronological_with_user_role_timestamp(client, fresh_customer):
    act_as(client, "u-sales")
    order = client.post("/orders", json={
        "customer_id": fresh_customer["id"], "product": "tscheck-audit-part", "quantity": 4,
        "unit_price": 10.0, "expected_delivery": "2027-01-01", "source": "offline", "payment_terms": "credit",
    }).json()

    act_as(client, "u-ops")
    order = client.post(f"/orders/{order['id']}/decision", json={"decision": "approve", "reason": ""}).json()

    act_as(client, "u-pm")
    order = client.post(f"/orders/{order['id']}/assign", json={"departments": ["Assembly", "Packaging"],
                                                                "route_name": "Assembly only"}).json()

    act_as(client, "u-sup-assembly")
    order = client.post(f"/orders/{order['id']}/stages/0/log", json={"produced": 4, "rejected": 0}).json()
    order = client.post(f"/orders/{order['id']}/stages/0/transfer", json={"quantity": 4},
                        headers=idempotency_headers(new_idempotency_key())).json()

    r = client.get(f"/orders/{order['id']}")
    assert r.status_code == 200
    timeline = r.json()["timeline"]
    assert len(timeline) >= 5

    stages_seen = [e["stage"] for e in timeline]
    assert "Order received" in stages_seen
    assert "Submitted for approval" in stages_seen
    assert "Approved" in stages_seen
    assert "Assigned to production" in stages_seen
    assert "Production update" in stages_seen
    assert "Units transferred" in stages_seen

    # chronological order (ISO timestamps are lexically sortable)
    timestamps = [e["at"] for e in timeline]
    assert timestamps == sorted(timestamps)

    # every entry carries user name + role + timestamp
    for e in timeline:
        assert e.get("user"), e
        assert e.get("role"), e
        assert e.get("at"), e

    # specific entries carry the right actor
    approve_entry = next(e for e in timeline if e["stage"] == "Approved")
    assert approve_entry["user"] == "Sarah Mitchell"
    assert approve_entry["role"] == "manager"

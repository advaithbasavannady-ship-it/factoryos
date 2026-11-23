"""Criterion: Role-based approval and rejection rules."""

from helpers import act_as


def _submit_offline(client, fresh_customer):
    act_as(client, "u-sales")
    r = client.post("/orders", json={
        "customer_id": fresh_customer["id"], "product": "tscheck-gadget", "quantity": 3,
        "unit_price": 40.0, "expected_delivery": "2027-01-01", "source": "offline",
        "payment_terms": "credit",
    })
    assert r.status_code == 201, r.text
    return r.json()


def test_sales_cannot_approve(client, fresh_customer):
    order = _submit_offline(client, fresh_customer)
    act_as(client, "u-sales")
    r = client.post(f"/orders/{order['id']}/decision", json={"decision": "approve", "reason": ""})
    assert r.status_code == 403, r.text


def test_reject_requires_reason(client, fresh_customer):
    order = _submit_offline(client, fresh_customer)
    act_as(client, "u-ops")
    r = client.post(f"/orders/{order['id']}/decision", json={"decision": "reject", "reason": ""})
    assert r.status_code == 422, r.text


def test_request_correction_sets_status(client, fresh_customer):
    order = _submit_offline(client, fresh_customer)
    act_as(client, "u-ops")
    r = client.post(f"/orders/{order['id']}/decision",
                    json={"decision": "request_correction", "reason": "fix quantity please"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "correction_requested"


def test_approve_confirms_with_decided_by(client, fresh_customer):
    order = _submit_offline(client, fresh_customer)
    act_as(client, "u-ops")
    r = client.post(f"/orders/{order['id']}/decision", json={"decision": "approve", "reason": ""})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "confirmed"
    assert body["decided_by"]["name"] == "Sarah Mitchell"
    assert body["decided_at"]


def test_awaiting_approval_cannot_be_assigned(client, fresh_customer):
    order = _submit_offline(client, fresh_customer)
    act_as(client, "u-pm")
    r = client.post(f"/orders/{order['id']}/assign", json={"departments": ["Machining"]})
    assert r.status_code == 409, r.text

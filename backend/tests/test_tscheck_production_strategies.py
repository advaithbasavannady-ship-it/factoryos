"""Criterion: Department-wise multi-stage production."""

from helpers import act_as, idempotency_headers, new_idempotency_key


def _confirmed_order(client, fresh_customer, qty=20):
    act_as(client, "u-sales")
    r = client.post("/orders", json={
        "customer_id": fresh_customer["id"], "product": "tscheck-part", "quantity": qty,
        "unit_price": 10.0, "expected_delivery": "2027-01-01", "source": "offline", "payment_terms": "credit",
    })
    assert r.status_code == 201, r.text
    order = r.json()
    act_as(client, "u-ops")
    r2 = client.post(f"/orders/{order['id']}/decision", json={"decision": "approve", "reason": ""})
    assert r2.status_code == 200, r2.text
    return r2.json()


def test_assign_machined_parts_route_and_stage_cards(client, fresh_customer):
    order = _confirmed_order(client, fresh_customer, qty=20)
    act_as(client, "u-pm")
    r = client.post(f"/orders/{order['id']}/assign", json={
        "departments": ["Machining", "Finishing", "Packaging"], "route_name": "Machined parts"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "in_production"
    assert len(body["stages"]) == 3
    s0 = body["stages"][0]
    for field in ("supervisor", "received", "produced", "rejected", "transferred"):
        assert field in s0
    assert s0["received"] == 20


def test_supervisor_cannot_log_other_department(client, fresh_customer):
    order = _confirmed_order(client, fresh_customer, qty=20)
    act_as(client, "u-pm")
    client.post(f"/orders/{order['id']}/assign", json={
        "departments": ["Machining", "Finishing", "Packaging"], "route_name": "Machined parts"})
    act_as(client, "u-sup-finishing")
    r = client.post(f"/orders/{order['id']}/stages/0/log", json={"produced": 5, "rejected": 0})
    assert r.status_code == 403, r.text


def test_overproducing_beyond_demand_rejected(client, fresh_customer):
    order = _confirmed_order(client, fresh_customer, qty=20)
    act_as(client, "u-pm")
    client.post(f"/orders/{order['id']}/assign", json={
        "departments": ["Machining", "Finishing", "Packaging"], "route_name": "Machined parts"})
    act_as(client, "u-sup-machining")
    r = client.post(f"/orders/{order['id']}/stages/0/log", json={"produced": 25, "rejected": 0})
    assert r.status_code == 422, r.text


def test_later_stage_cannot_process_more_than_received(client, fresh_customer):
    order = _confirmed_order(client, fresh_customer, qty=20)
    act_as(client, "u-pm")
    client.post(f"/orders/{order['id']}/assign", json={
        "departments": ["Machining", "Finishing", "Packaging"], "route_name": "Machined parts"})
    act_as(client, "u-sup-machining")
    r = client.post(f"/orders/{order['id']}/stages/0/log", json={"produced": 20, "rejected": 0})
    assert r.status_code == 200, r.text
    # transfer only 5 to Finishing
    r2 = client.post(f"/orders/{order['id']}/stages/0/transfer", json={"quantity": 5},
                     headers=idempotency_headers(new_idempotency_key()))
    assert r2.status_code == 200, r2.text
    act_as(client, "u-sup-finishing")
    r3 = client.post(f"/orders/{order['id']}/stages/1/log", json={"produced": 10, "rejected": 0})
    assert r3.status_code == 422, r3.text


def test_transfer_and_final_stage_moves_to_quality_control(client, fresh_customer):
    order = _confirmed_order(client, fresh_customer, qty=5)
    act_as(client, "u-pm")
    client.post(f"/orders/{order['id']}/assign", json={
        "departments": ["Machining", "Finishing", "Packaging"], "route_name": "Machined parts"})
    act_as(client, "u-sup-machining")
    assert client.post(f"/orders/{order['id']}/stages/0/log", json={"produced": 5, "rejected": 0}).status_code == 200
    assert client.post(f"/orders/{order['id']}/stages/0/transfer", json={"quantity": 5},
                       headers=idempotency_headers(new_idempotency_key())).status_code == 200
    act_as(client, "u-sup-finishing")
    assert client.post(f"/orders/{order['id']}/stages/1/log", json={"produced": 5, "rejected": 0}).status_code == 200
    assert client.post(f"/orders/{order['id']}/stages/1/transfer", json={"quantity": 5},
                       headers=idempotency_headers(new_idempotency_key())).status_code == 200
    act_as(client, "u-sup-packaging")
    r = client.post(f"/orders/{order['id']}/stages/2/log", json={"produced": 5, "rejected": 0})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "quality_check"

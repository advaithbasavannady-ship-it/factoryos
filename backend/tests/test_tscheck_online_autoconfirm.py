"""Criterion: Online orders and auto-confirmation rules."""

from helpers import REPEAT_CUSTOMER_ID, act_as, idempotency_headers, new_idempotency_key


def test_online_autoconfirm_small_repeat_order(client):
    act_as(client, "u-sales")
    r = client.post("/orders", json={
        "customer_id": REPEAT_CUSTOMER_ID, "product": "Precision Drive Shaft PS-50", "quantity": 5,
        "unit_price": 50.0, "expected_delivery": "2027-01-01", "source": "online",
        "payment_terms": "advance", "advance_paid": 100.0,  # value=250, 100 >= 30% of 250 (75)
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "confirmed", body


def test_online_advance_no_payment_pending_review_with_payment_hold(client):
    act_as(client, "u-sales")
    r = client.post("/orders", json={
        "customer_id": REPEAT_CUSTOMER_ID, "product": "Precision Drive Shaft PS-50", "quantity": 5,
        "unit_price": 50.0, "expected_delivery": "2027-01-01", "source": "online",
        "payment_terms": "advance", "advance_paid": 0.0,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending_review", body
    holds = {h["key"]: h for h in body["holds"]}
    assert "payment" in holds and holds["payment"]["cleared"] is False

    # recording a payment >= 30% clears the hold and confirms
    act_as(client, "u-ops")
    r2 = client.post(f"/orders/{body['id']}/payments", json={"amount": 100.0, "method": "Bank transfer"},
                     headers=idempotency_headers(new_idempotency_key()))
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    cleared_hold = next(h for h in body2["holds"] if h["key"] == "payment")
    assert cleared_hold["cleared"] is True
    assert body2["status"] == "confirmed", body2


def test_order_value_shown_separately_from_received_balance(client):
    act_as(client, "u-sales")
    r = client.post("/orders", json={
        "customer_id": REPEAT_CUSTOMER_ID, "product": "Precision Drive Shaft PS-50", "quantity": 10,
        "unit_price": 20.0, "expected_delivery": "2027-01-01", "source": "online",
        "payment_terms": "advance", "advance_paid": 60.0,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    value = body["quantity"] * body["unit_price"]
    assert value == 200.0
    assert body["amount_paid"] == 60.0
    balance = value - body["amount_paid"]
    assert balance == 140.0

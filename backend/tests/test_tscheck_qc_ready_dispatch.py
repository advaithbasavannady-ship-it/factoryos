"""Criterion: QC -> Ready -> Dispatch lifecycle with roles."""

import os
from concurrent.futures import ThreadPoolExecutor

import httpx

from helpers import act_as, auth_headers, idempotency_headers, new_idempotency_key


def _order_at_qc(client, fresh_customer, qty=5, product="tscheck-qc-part"):
    act_as(client, "u-sales")
    r = client.post("/orders", json={
        "customer_id": fresh_customer["id"], "product": product, "quantity": qty,
        "unit_price": 10.0, "expected_delivery": "2027-01-01", "source": "offline", "payment_terms": "credit",
    })
    order = r.json()
    act_as(client, "u-ops")
    order = client.post(f"/orders/{order['id']}/decision", json={"decision": "approve", "reason": ""}).json()
    act_as(client, "u-pm")
    order = client.post(f"/orders/{order['id']}/assign", json={"departments": ["Assembly", "Packaging"],
                                                                "route_name": "Assembly only"}).json()
    act_as(client, "u-sup-assembly")
    client.post(f"/orders/{order['id']}/stages/0/log", json={"produced": qty, "rejected": 0})
    client.post(f"/orders/{order['id']}/stages/0/transfer", json={"quantity": qty},
                headers=idempotency_headers(new_idempotency_key()))
    act_as(client, "u-sup-packaging")
    order = client.post(f"/orders/{order['id']}/stages/1/log", json={"produced": qty, "rejected": 0}).json()
    assert order["status"] == "quality_check", order
    return order


def test_qc_partial_reject_sends_rework_then_full_pass_ready_dispatch(client, fresh_customer):
    order = _order_at_qc(client, fresh_customer, qty=5)
    act_as(client, "u-qc")
    # reject 2 units -> rework, back to in_production
    r = client.post(f"/orders/{order['id']}/inspect", json={"passed": 3, "rejected": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "in_production", body
    assert body["qc_status"] == "rework"

    # wrong-stage action: cannot mark ready while in_production
    act_as(client, "u-dispatch")
    r_bad = client.post(f"/orders/{order['id']}/ready", headers=idempotency_headers(new_idempotency_key()))
    assert r_bad.status_code == 409, r_bad.text

    # redo the last stage for the reworked 2 units
    act_as(client, "u-sup-packaging")
    client.post(f"/orders/{order['id']}/stages/1/log", json={"produced": 2, "rejected": 0})

    act_as(client, "u-qc")
    r2 = client.post(f"/orders/{order['id']}/inspect", json={"passed": 2, "rejected": 0})
    assert r2.status_code == 200, r2.text
    assert r2.json()["qc_status"] == "passed"

    act_as(client, "u-dispatch")
    r3 = client.post(f"/orders/{order['id']}/ready", headers=idempotency_headers(new_idempotency_key()))
    assert r3.status_code == 200, r3.text
    assert r3.json()["status"] == "ready_for_dispatch"

    act_as(client, "u-dispatch")
    dispatch_key = new_idempotency_key()
    dispatch_body = {"carrier": "tscheck-carrier"}
    r4 = client.post(f"/orders/{order['id']}/dispatch", json=dispatch_body,
                     headers=idempotency_headers(dispatch_key))
    assert r4.status_code == 200, r4.text
    assert r4.json()["status"] == "dispatched"

    # wrong-stage: dispatching an already-dispatched order fails
    r5 = client.post(f"/orders/{order['id']}/dispatch", json=dispatch_body,
                     headers=idempotency_headers(dispatch_key))
    assert r5.status_code == 200, r5.text
    r6 = client.post(f"/orders/{order['id']}/dispatch", json=dispatch_body,
                     headers=idempotency_headers(new_idempotency_key()))
    assert r6.status_code == 409, r6.text


def test_concurrent_ready_requests_move_finished_stock_once(client, fresh_customer):
    order = _order_at_qc(client, fresh_customer, qty=4, product="tscheck-concurrent-fg")
    act_as(client, "u-qc")
    passed = client.post(f"/orders/{order['id']}/inspect", json={"passed": 4, "rejected": 0})
    assert passed.status_code == 200, passed.text

    act_as(client, "u-dispatch")
    credentials = __import__("json").loads(open("/run/secrets/test-role-credentials").read())
    response = client.post("/auth/login", json={
        "email": credentials["u-dispatch"]["email"],
        "password": credentials["u-dispatch"]["password"],
        "company_id": credentials["u-dispatch"]["company_id"],
    })
    assert response.status_code == 200
    headers = auth_headers(response.json()["access_token"])
    ready_key = new_idempotency_key()
    ready_headers = {**headers, **idempotency_headers(ready_key)}
    with httpx.Client(base_url=str(client.base_url), headers=ready_headers, timeout=30) as first, \
            httpx.Client(base_url=str(client.base_url), headers=ready_headers, timeout=30) as second:
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(
                lambda pair: pair[0].post(f"/orders/{order['id']}/ready"),
                [(first,), (second,)],
            ))
    assert sorted(reply.status_code for reply in replies) == [200, 200]
    client.headers.update(auth_headers(os.environ["TEST_ACCESS_TOKEN"]))
    stock = client.get("/inventory/items").json()
    item = next(row for row in stock if row["id"] == "phase12-finished-qc-part")
    assert item["quantity"] == 4

    dispatch_headers = {**auth_headers(response.json()["access_token"]),
                        **idempotency_headers(new_idempotency_key())}
    with httpx.Client(base_url=str(client.base_url), headers=dispatch_headers, timeout=30) as first, \
            httpx.Client(base_url=str(client.base_url), headers=dispatch_headers, timeout=30) as second:
        with ThreadPoolExecutor(max_workers=2) as pool:
            dispatch_replies = list(pool.map(
                lambda pair: pair[0].post(
                    f"/orders/{order['id']}/dispatch", json={"carrier": "phase12-concurrency-check"},
                ),
                [(first,), (second,)],
            ))
    assert sorted(reply.status_code for reply in dispatch_replies) == [200, 200]
    client.headers.update(auth_headers(os.environ["TEST_ACCESS_TOKEN"]))
    stock_after_dispatch = client.get("/inventory/items").json()
    item_after_dispatch = next(row for row in stock_after_dispatch if row["id"] == "phase12-finished-qc-part")
    assert item_after_dispatch["quantity"] == 0

"""MongoDB-backed concurrent order payment integrity regression."""

import os
from concurrent.futures import ThreadPoolExecutor

import httpx

from helpers import auth_headers, idempotency_headers, new_idempotency_key


def test_concurrent_payments_preserve_each_payment_and_balance(client, fresh_customer):
    headers = auth_headers(os.environ["TEST_ACCESS_TOKEN"])
    client.headers.update(headers)
    created = client.post("/orders", json={
        "customer_id": fresh_customer["id"], "product": "phase12-payment-race",
        "quantity": 32, "unit_price": 1.0, "expected_delivery": "2027-01-01",
        "source": "offline", "payment_terms": "credit",
    })
    assert created.status_code == 201, created.text
    order_id = created.json()["id"]

    attempts = 16
    base_url = str(client.base_url).rstrip("/")

    keys = [new_idempotency_key() for _ in range(attempts)]

    def pay_once(_index, key):
        mutation_headers = {**headers, **idempotency_headers(key)}
        with httpx.Client(base_url=base_url, headers=mutation_headers, timeout=30) as worker:
            return worker.post(
                f"/orders/{order_id}/payments",
                json={"amount": 1.0, "method": "test transfer", "reference": "concurrent regression"},
            )

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        replies = list(pool.map(pay_once, range(attempts), keys))
    assert all(reply.status_code == 200 for reply in replies), [reply.status_code for reply in replies]
    stored = client.get(f"/orders/{order_id}")
    assert stored.status_code == 200, stored.text
    order = stored.json()
    assert order["amount_paid"] == attempts
    assert len(order["payments"]) == attempts

    replay_headers = {**headers, **idempotency_headers(keys[0])}
    with httpx.Client(base_url=base_url, headers=replay_headers, timeout=30) as retry_client:
        replay = retry_client.post(
            f"/orders/{order_id}/payments",
            json={"amount": 1.0, "method": "test transfer", "reference": "concurrent regression"},
        )
    assert replay.status_code == 200, replay.text
    assert replay.json() == replies[0].json()
    still_stored = client.get(f"/orders/{order_id}").json()
    assert still_stored["amount_paid"] == attempts
    conflict_headers = {**headers, **idempotency_headers(keys[0])}
    with httpx.Client(base_url=base_url, headers=conflict_headers, timeout=30) as retry_client:
        conflict = retry_client.post(
            f"/orders/{order_id}/payments", json={"amount": 2.0, "method": "test transfer"},
        )
    assert conflict.status_code == 409

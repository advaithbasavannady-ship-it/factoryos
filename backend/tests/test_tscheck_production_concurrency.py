"""MongoDB-backed concurrent production transfer integrity regression."""

from concurrent.futures import ThreadPoolExecutor

import httpx

from helpers import act_as, idempotency_headers, new_idempotency_key


def test_concurrent_partial_transfers_preserve_quantity_lineage(client, fresh_customer):
    act_as(client, "u-sales")
    created = client.post("/orders", json={
        "customer_id": fresh_customer["id"], "product": "phase12-transfer-race",
        "quantity": 20, "unit_price": 1.0, "expected_delivery": "2027-01-01",
        "source": "offline", "payment_terms": "credit",
    })
    assert created.status_code == 201, created.text
    order_id = created.json()["id"]
    act_as(client, "u-ops")
    decided = client.post(f"/orders/{order_id}/decision", json={"decision": "approve", "reason": "test"})
    assert decided.status_code == 200, decided.text
    act_as(client, "u-pm")
    assigned = client.post(f"/orders/{order_id}/assign", json={
        "departments": ["Machining", "Finishing"], "route_name": "Phase 1.2 transfer race",
    })
    assert assigned.status_code == 200, assigned.text
    act_as(client, "u-sup-machining")
    logged = client.post(f"/orders/{order_id}/stages/0/log", json={"produced": 20, "rejected": 0})
    assert logged.status_code == 200, logged.text

    attempts = 16
    base_url = str(client.base_url).rstrip("/")
    headers = dict(client.headers)

    keys = [new_idempotency_key() for _ in range(attempts)]

    def transfer_once(_index, key):
        mutation_headers = {**headers, **idempotency_headers(key)}
        with httpx.Client(base_url=base_url, headers=mutation_headers, timeout=30) as worker:
            return worker.post(f"/orders/{order_id}/stages/0/transfer", json={"quantity": 1})

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        replies = list(pool.map(transfer_once, range(attempts), keys))
    assert all(reply.status_code == 200 for reply in replies), [reply.status_code for reply in replies]
    stored = client.get(f"/orders/{order_id}")
    assert stored.status_code == 200, stored.text
    stages = stored.json()["stages"]
    assert stages[0]["transferred"] == attempts
    assert stages[1]["received"] == attempts

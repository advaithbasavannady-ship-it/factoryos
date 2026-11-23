"""MongoDB-backed inventory and purchasing lifecycle regression."""

import os
from concurrent.futures import ThreadPoolExecutor

import httpx

from helpers import act_as, auth_headers, idempotency_headers, new_idempotency_key


def test_inventory_adjust_and_purchase_receipt_are_tenant_scoped(client):
    # Seeded only in factoryos_security_test by the isolated suite bootstrap.
    item_id = "phase12-raw-copper"

    act_as(client, "u-sales")
    denied = client.post(f"/inventory/items/{item_id}/adjust", json={"delta": 1, "reason": "test"},
                         headers=idempotency_headers(new_idempotency_key()))
    assert denied.status_code == 403, denied.text

    # The runner's synthetic owner token is scoped to the same isolated test company.
    client.headers.update(auth_headers(os.environ["TEST_ACCESS_TOKEN"]))
    before = client.get("/inventory/items").json()
    raw_item = next(row for row in before if row["id"] == item_id)
    assert raw_item["quantity"] == 10

    adjust_key = new_idempotency_key()
    adjust_body = {"delta": 2, "reason": "integration check"}
    adjusted = client.post(f"/inventory/items/{item_id}/adjust", json=adjust_body,
                           headers=idempotency_headers(adjust_key))
    assert adjusted.status_code == 200, adjusted.text
    assert adjusted.json()["quantity"] == 12
    replayed_adjustment = client.post(f"/inventory/items/{item_id}/adjust", json=adjust_body,
                                      headers=idempotency_headers(adjust_key))
    assert replayed_adjustment.status_code == 200
    assert replayed_adjustment.json()["quantity"] == 12
    mismatched_adjustment = client.post(
        f"/inventory/items/{item_id}/adjust", json={"delta": 3, "reason": "integration check"},
        headers=idempotency_headers(adjust_key),
    )
    assert mismatched_adjustment.status_code == 409

    create_purchase_key = new_idempotency_key()
    purchase = client.post("/purchases", json={
        "item_id": item_id, "supplier_id": "phase12-synthetic-supplier", "quantity": 7,
        "unit_cost": 2.5, "expected_date": "2027-01-01",
    }, headers=idempotency_headers(create_purchase_key))
    assert purchase.status_code == 201, purchase.text
    replayed_create = client.post("/purchases", json={
        "item_id": item_id, "supplier_id": "phase12-synthetic-supplier", "quantity": 7,
        "unit_cost": 2.5, "expected_date": "2027-01-01",
    }, headers=idempotency_headers(create_purchase_key))
    assert replayed_create.status_code == 201
    assert replayed_create.json()["id"] == purchase.json()["id"]
    receipt_key = new_idempotency_key()
    received = client.patch(f"/purchases/{purchase.json()['id']}", json={"status": "received"},
                            headers=idempotency_headers(receipt_key))
    assert received.status_code == 200, received.text
    assert received.json()["status"] == "received"

    after = client.get("/inventory/items").json()
    raw_item_after = next(row for row in after if row["id"] == item_id)
    assert raw_item_after["quantity"] == 19
    duplicate_receipt = client.patch(f"/purchases/{purchase.json()['id']}", json={"status": "received"},
                                     headers=idempotency_headers(receipt_key))
    assert duplicate_receipt.status_code == 200, duplicate_receipt.text

    concurrent_purchase = client.post("/purchases", json={
        "item_id": item_id, "supplier_id": "phase12-synthetic-supplier", "quantity": 3,
        "unit_cost": 2.5, "expected_date": "2027-01-01",
    }, headers=idempotency_headers(new_idempotency_key()))
    assert concurrent_purchase.status_code == 201, concurrent_purchase.text
    quantity_before = raw_item_after["quantity"]
    purchase_id = concurrent_purchase.json()["id"]
    headers = auth_headers(os.environ["TEST_ACCESS_TOKEN"])
    concurrent_receipt_key = new_idempotency_key()
    receipt_headers = {**headers, **idempotency_headers(concurrent_receipt_key)}
    with httpx.Client(base_url=str(client.base_url), headers=receipt_headers, timeout=30) as first, \
            httpx.Client(base_url=str(client.base_url), headers=receipt_headers, timeout=30) as second:
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(
                lambda pair: pair[0].patch(f"/purchases/{purchase_id}", json={"status": "received"}),
                [(first,), (second,)],
            ))
    assert sorted(reply.status_code for reply in replies) == [200, 200]
    after_concurrent = client.get("/inventory/items").json()
    raw_item_concurrent = next(row for row in after_concurrent if row["id"] == item_id)
    assert raw_item_concurrent["quantity"] == quantity_before + 3


def test_concurrent_inventory_adjustments_preserve_every_successful_delta(client):
    item_id = "phase12-raw-copper"
    client.headers.update(auth_headers(os.environ["TEST_ACCESS_TOKEN"]))
    before = client.get("/inventory/items").json()
    initial = next(row["quantity"] for row in before if row["id"] == item_id)
    count = 16
    headers = auth_headers(os.environ["TEST_ACCESS_TOKEN"])
    base_url = str(client.base_url).rstrip("/")

    def adjust_once(_index, delta, key):
        mutation_headers = {**headers, **idempotency_headers(key)}
        with httpx.Client(base_url=base_url, headers=mutation_headers, timeout=30) as worker:
            return worker.post(
                f"/inventory/items/{item_id}/adjust",
                json={"delta": delta, "reason": "concurrent integrity regression"},
            )

    with ThreadPoolExecutor(max_workers=count) as pool:
        replies = list(pool.map(adjust_once, range(count), [1] * count,
                                [new_idempotency_key() for _ in range(count)]))
    assert all(reply.status_code == 200 for reply in replies), [reply.status_code for reply in replies]
    after = client.get("/inventory/items").json()
    final = next(row["quantity"] for row in after if row["id"] == item_id)
    assert final == initial + count

    with ThreadPoolExecutor(max_workers=count) as pool:
        negative_replies = list(pool.map(adjust_once, range(count), [-1] * count,
                                         [new_idempotency_key() for _ in range(count)]))
    assert all(reply.status_code == 200 for reply in negative_replies), [
        reply.status_code for reply in negative_replies
    ]
    after_consumption = client.get("/inventory/items").json()
    final_after_consumption = next(row["quantity"] for row in after_consumption if row["id"] == item_id)
    assert final_after_consumption == initial
    consumption = client.get("/inventory/consumption?days=1").json()
    consumed_item = next(row for row in consumption["items"] if row["item_id"] == item_id)
    assert consumed_item["total"] == count

    rejected = client.post(
        f"/inventory/items/{item_id}/adjust", json={"delta": -(initial + 1), "reason": "below-zero check"},
        headers=idempotency_headers(new_idempotency_key()),
    )
    assert rejected.status_code == 422
    final_item = next(row for row in client.get("/inventory/items").json() if row["id"] == item_id)
    assert final_item["quantity"] == initial


def test_concurrent_duplicate_purchase_receipts_credit_stock_once(client):
    item_id = "phase12-raw-copper"
    client.headers.update(auth_headers(os.environ["TEST_ACCESS_TOKEN"]))
    before = client.get("/inventory/items").json()
    initial = next(row["quantity"] for row in before if row["id"] == item_id)
    purchase = client.post("/purchases", json={
        "item_id": item_id, "supplier_id": "phase12-synthetic-supplier", "quantity": 1,
        "unit_cost": 2.5, "expected_date": "2027-01-01",
    }, headers=idempotency_headers(new_idempotency_key()))
    assert purchase.status_code == 201, purchase.text

    headers = auth_headers(os.environ["TEST_ACCESS_TOKEN"])
    base_url = str(client.base_url).rstrip("/")
    purchase_id = purchase.json()["id"]

    receipt_key = new_idempotency_key()
    receipt_headers = {**headers, **idempotency_headers(receipt_key)}

    def receive_once(_index):
        with httpx.Client(base_url=base_url, headers=receipt_headers, timeout=30) as worker:
            return worker.patch(f"/purchases/{purchase_id}", json={"status": "received"})

    attempts = 16
    with ThreadPoolExecutor(max_workers=attempts) as pool:
        replies = list(pool.map(receive_once, range(attempts)))
    assert sum(reply.status_code == 200 for reply in replies) == attempts
    after = client.get("/inventory/items").json()
    final = next(row["quantity"] for row in after if row["id"] == item_id)
    assert final == initial + 1

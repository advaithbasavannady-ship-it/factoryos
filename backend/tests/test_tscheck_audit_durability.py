"""Mongo integration tests for required audit persistence on critical mutations."""

import hashlib
import os

from helpers import auth_headers, idempotency_headers, new_idempotency_key


def test_critical_inventory_mutation_aborts_when_audit_insert_fails(client):
    client.headers.update(auth_headers(os.environ["TEST_ACCESS_TOKEN"]))
    test_url = lambda path: str(client.base_url.copy_with(path=path))
    identity = client.get(test_url("/__test__/identity")).json()
    run_header = {"X-Test-Run-ID": identity["run_id"]}
    session = client.get("/auth/session")
    assert session.status_code == 200, session.text
    company_id = session.json()["user"]["company_id"]

    key = new_idempotency_key()
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    event_id = hashlib.sha256(f"factoryos-audit:{company_id}:{key_hash}".encode()).hexdigest()
    sentinel = client.post(test_url("/__test__/audit-sentinel"), headers=run_header, json={"event_id": event_id})
    assert sentinel.status_code == 200, sentinel.text

    before = next(item for item in client.get("/inventory/items").json() if item["id"] == "phase12-audit-copper")
    body = {"delta": 1, "reason": "audit failure atomicity test"}
    failed = client.post("/inventory/items/phase12-audit-copper/adjust", json=body,
                         headers=idempotency_headers(key))
    assert failed.status_code == 503
    after_failure = next(item for item in client.get("/inventory/items").json()
                         if item["id"] == "phase12-audit-copper")
    assert after_failure["quantity"] == before["quantity"]

    removed = client.delete(test_url(f"/__test__/audit-sentinel/{event_id}"), headers=run_header)
    assert removed.status_code == 200 and removed.json()["deleted"] is True

    succeeded = client.post("/inventory/items/phase12-audit-copper/adjust", json=body,
                            headers=idempotency_headers(key))
    assert succeeded.status_code == 200, succeeded.text
    assert succeeded.json()["quantity"] == before["quantity"] + 1
    replay = client.post("/inventory/items/phase12-audit-copper/adjust", json=body,
                         headers=idempotency_headers(key))
    assert replay.status_code == 200
    assert replay.json()["quantity"] == succeeded.json()["quantity"]

    audit = client.get(test_url(f"/__test__/audit-event/{event_id}"), headers=run_header)
    assert audit.status_code == 200
    event = audit.json()["event"]
    assert event and event["action"] == "inventory.stock_adjusted"
    assert event["tenant_id"] == company_id
    assert event["user_id"]

"""MongoDB-backed cross-company operational read/write isolation checks."""

import json
from pathlib import Path

from helpers import auth_headers, idempotency_headers, new_idempotency_key


def test_multicompany_user_cannot_read_or_mutate_other_tenant_records(client):
    role_credentials = json.loads(Path("/run/secrets/test-role-credentials").read_text())
    primary_company = role_credentials["u-sales"]["company_id"]
    login = client.post("/auth/login", json={
        "email": "multi@phase12.test", "password": "phase12-multi-company-password-42",
        "company_id": primary_company,
    })
    assert login.status_code == 200, login.text
    client.headers.update(auth_headers(login.json()["access_token"]))

    # The requested IDs exist in a second synthetic company, but all queries
    # must use the company bound to the active login session.
    assert client.get("/orders/phase12-secondary-order").status_code == 404
    foreign_payment = client.post("/orders/phase12-secondary-order/payments", json={
        "amount": 1, "method": "Test",
    }, headers=idempotency_headers(new_idempotency_key()))
    assert foreign_payment.status_code == 404
    foreign_stock = client.post("/inventory/items/phase12-secondary-inventory/adjust", json={
        "delta": 1, "reason": "tenant isolation test",
    }, headers=idempotency_headers(new_idempotency_key()))
    assert foreign_stock.status_code == 404

    switched = client.post("/auth/switch-company", json={"company_id": "phase12-secondary-company"})
    assert switched.status_code == 200, switched.text
    client.headers.update(auth_headers(switched.json()["access_token"]))
    session = client.get("/auth/session")
    assert session.status_code == 200, session.text
    assert session.json()["user"]["role"] == "sales_staff"
    assert session.json()["user"]["company_id"] == "phase12-secondary-company"
    assert client.get("/orders/phase12-secondary-order").status_code == 200

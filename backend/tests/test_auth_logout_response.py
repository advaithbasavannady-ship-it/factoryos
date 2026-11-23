"""Regression coverage for the isolated auth router's logout response."""

import uuid


def test_logout_without_session_returns_204_and_clears_refresh_cookie(client):
    response = client.post("/auth/logout")

    assert response.status_code == 204
    cookie = response.headers.get("set-cookie", "").lower()
    assert "factoryos_refresh=" in cookie
    assert "max-age=0" in cookie
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=strict" in cookie
    assert "path=/api/auth" in cookie


def test_password_reset_confirm_returns_204(client):
    response = client.post("/auth/password-reset/confirm", json={
        "token": "phase12-preprovisioned-password-reset-token",
        "new_password": "replacement-password-for-test",
    })

    assert response.status_code == 204


def test_real_mongo_login_rate_limit_blocks_after_email_threshold(client):
    email = f"limit-{uuid.uuid4().hex}@example.test"
    statuses = [
        client.post("/auth/login", json={"email": email, "password": "wrong-password"}).status_code
        for _ in range(11)
    ]
    assert statuses[:10] == [401] * 10
    assert statuses[10] == 429


def test_synthetic_invitation_acceptance_activates_membership(client):
    token = "phase12-preprovisioned-invitation-token"
    accepted = client.post("/auth/invitations/accept", json={
        "token": token, "new_password": "synthetic-invitee-password-42",
    })
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "active"

    login = client.post("/auth/login", json={
        "email": "invitee@phase12.test", "password": "synthetic-invitee-password-42",
        "company_id": accepted.json()["company_id"],
    })
    assert login.status_code == 200, login.text
    client.headers.update({"Authorization": f"Bearer {login.json()['access_token']}"})
    session = client.get("/auth/session")
    assert session.status_code == 200, session.text
    assert session.json()["user"]["role"] == "sales_staff"

    replay = client.post("/auth/invitations/accept", json={
        "token": token, "new_password": "synthetic-invitee-password-42",
    })
    assert replay.status_code == 400

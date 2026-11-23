"""Small shared helpers for backend security tests."""

import json
import os
import uuid
from pathlib import Path

import httpx

REPEAT_CUSTOMER_ID = "3c4607fb-98f3-4f95-8b2f-fd45f206b003"  # Orion Robotics - has a dispatched order


def auth_headers(access_token: str) -> dict[str, str]:
    """Build bearer-token auth headers for authenticated requests."""
    return {"Authorization": f"Bearer {access_token}"}


def idempotency_headers(key: str) -> dict[str, str]:
    """Build the required replay header for a synthetic operational mutation."""
    return {"Idempotency-Key": key}


def new_idempotency_key() -> str:
    return "factoryos-test-" + uuid.uuid4().hex


_role_tokens: dict[tuple[str, str, str], str] = {}


def act_as(client: httpx.Client, actor: str) -> None:
    """Log in as a provisioned synthetic role and set this client's bearer token."""
    credentials_override = os.environ.get("TEST_ROLE_CREDENTIALS_FILE", "").strip()
    if credentials_override:
        credentials_path = Path(credentials_override)
    else:
        credentials_path = Path("/run/secrets/test-role-credentials")
    cache_key = (str(credentials_path), str(client.base_url), actor)
    token = _role_tokens.get(cache_key)
    if token is None:
        try:
            credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
            if not isinstance(credentials, dict):
                raise TypeError("credentials payload must be a JSON object")
            actor_credentials = credentials[actor]
            if not isinstance(actor_credentials, dict):
                raise TypeError(f"actor credentials for {actor} must be a dictionary")
            email = actor_credentials["email"]
            password = actor_credentials["password"]
            company_id = actor_credentials["company_id"]
            if (
                not isinstance(email, str)
                or not email.strip()
                or not isinstance(password, str)
                or not password
                or not isinstance(company_id, str)
                or not company_id.strip()
            ):
                raise TypeError(f"invalid credential field values for actor {actor}")
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            raise RuntimeError(f"missing isolated test credentials for actor {actor}") from None

        response = client.post("/auth/login", json={
            "email": email.strip(),
            "password": password,
            "company_id": company_id.strip(),
        })
        if response.status_code != 200:
            raise RuntimeError(f"test actor login failed for {actor}: status={response.status_code}")
        token = response.json()["access_token"]
        _role_tokens[cache_key] = token

    client.headers.update(auth_headers(token))


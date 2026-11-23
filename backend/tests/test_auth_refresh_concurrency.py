"""Real Mongo-backed refresh rotation, duplicate request and replay regressions."""

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from helpers import auth_headers


REFRESH_COOKIE = "factoryos_refresh"


def _login(client, actor: str) -> tuple[str, str]:
    credentials = json.loads(Path("/run/secrets/test-role-credentials").read_text())[actor]
    response = client.post("/auth/login", json={
        "email": credentials["email"], "password": credentials["password"],
        "company_id": credentials["company_id"],
    })
    assert response.status_code == 200, response.text
    return response.json()["access_token"], client.cookies.get(REFRESH_COOKIE)


def _refresh(client, cookie: str, request_id: str):
    return client.post("/auth/refresh", headers={
        "Cookie": f"{REFRESH_COOKIE}={cookie}", "X-Refresh-Request-ID": request_id,
    })


def test_simultaneous_refresh_and_network_retry_return_one_rotation(client):
    _, old_cookie = _login(client, "u-sales")
    request_id = str(uuid.uuid4())
    base_url = str(client.base_url)

    def send_duplicate(_):
        with httpx.Client(base_url=base_url, timeout=30) as tab:
            return _refresh(tab, old_cookie, request_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(send_duplicate, range(2)))

    assert first.status_code == second.status_code == 200, (first.status_code, second.status_code)
    assert first.json() == second.json()
    first_cookie = first.cookies.get(REFRESH_COOKIE)
    second_cookie = second.cookies.get(REFRESH_COOKIE)
    assert first_cookie and first_cookie == second_cookie and first_cookie != old_cookie

    # A retry after the browser accepted Set-Cookie still returns the same result.
    retry = _refresh(client, first_cookie, request_id)
    assert retry.status_code == 200, retry.text
    assert retry.json() == first.json()
    assert retry.cookies.get(REFRESH_COOKIE) == first_cookie


def test_concurrent_old_token_with_different_request_ids_revokes_family(client):
    access, old_cookie = _login(client, "u-pm")
    base_url = str(client.base_url)
    request_ids = [str(uuid.uuid4()), str(uuid.uuid4())]

    def send_replay(request_id):
        with httpx.Client(base_url=base_url, timeout=30) as tab:
            return _refresh(tab, old_cookie, request_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send_replay, request_ids))

    assert sorted(response.status_code for response in responses) == [200, 401], [
        (response.status_code, response.text) for response in responses
    ]
    winner = next(response for response in responses if response.status_code == 200)
    successor = winner.cookies.get(REFRESH_COOKIE)
    assert successor and successor != old_cookie

    # Distinct IDs cannot claim one logical retry. The losing request is treated
    # as replay, so the winner's token and access session are revoked as well.
    assert client.get("/auth/session", headers=auth_headers(access)).status_code == 401
    replay_after_race = _refresh(client, successor, str(uuid.uuid4()))
    assert replay_after_race.status_code == 401


def test_request_id_cannot_be_reused_for_another_session(client):
    first_access, first_cookie = _login(client, "u-sales")
    request_id = str(uuid.uuid4())
    first = _refresh(client, first_cookie, request_id)
    assert first.status_code == 200, first.text

    other = httpx.Client(base_url=str(client.base_url), timeout=30)
    try:
        second_access, second_cookie = _login(other, "u-ops")
        collision = _refresh(other, second_cookie, request_id)
        assert collision.status_code == 409, collision.text
        assert other.get("/auth/session", headers=auth_headers(second_access)).status_code == 200
        independent = _refresh(other, second_cookie, str(uuid.uuid4()))
        assert independent.status_code == 200, independent.text
        assert client.get("/auth/session", headers=auth_headers(first_access)).status_code == 200
    finally:
        other.close()


def test_replay_with_a_different_request_id_revokes_the_session_family(client):
    _, old_cookie = _login(client, "u-pm")
    rotated = _refresh(client, old_cookie, str(uuid.uuid4()))
    assert rotated.status_code == 200, rotated.text
    successor = rotated.cookies.get(REFRESH_COOKIE)
    assert successor and successor != old_cookie

    stolen_replay = _refresh(client, old_cookie, str(uuid.uuid4()))
    assert stolen_replay.status_code == 401
    restored = _refresh(client, successor, str(uuid.uuid4()))
    assert restored.status_code == 401
    assert client.get("/auth/session", headers=auth_headers(rotated.json()["access_token"])).status_code == 401


def test_revoked_session_cannot_be_restored_by_a_cached_refresh_result(client):
    access, old_cookie = _login(client, "u-dispatch")
    request_id = str(uuid.uuid4())
    rotated = _refresh(client, old_cookie, request_id)
    assert rotated.status_code == 200, rotated.text
    successor = rotated.cookies.get(REFRESH_COOKIE)
    assert successor

    client.cookies.set(REFRESH_COOKIE, successor)
    logout = client.post("/auth/logout", headers=auth_headers(access))
    assert logout.status_code == 204, logout.text
    duplicate = _refresh(client, successor, request_id)
    assert duplicate.status_code == 401
    assert client.get("/auth/session", headers=auth_headers(rotated.json()["access_token"])).status_code == 401

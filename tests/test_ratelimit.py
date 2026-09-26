"""Inbound rate limiting (P3): fixed-window math, both backends, the 429
contract and the per-endpoint rules."""

from datetime import UTC, datetime

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from alphamarket import auth, ratelimit
from alphamarket.config import get_settings
from alphamarket.ratelimit import (
    MemoryLimiter,
    PostgresLimiter,
    prune_counters,
    rate_limit,
    window_bounds,
)

SIGNUP = {"email": "rl@example.com", "password": "hunter2hunter2", "display_name": "Rl"}


def _at(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def test_window_bounds_hand_computed():
    # 60 s window at t = 1_000_015: floor(1_000_015 / 60) = 16_666 -> start 999_960,
    # the window ends at 1_000_020, so 5 s remain.
    assert window_bounds(_at(1_000_015), 60) == (999_960, 5)
    # Exactly on a boundary: start = t, the whole window remains.
    assert window_bounds(_at(999_960), 60) == (999_960, 60)
    # 0.5 s before the end rounds up to 1 s (Retry-After is never 0).
    assert window_bounds(_at(1_000_019.5), 60) == (999_960, 1)
    # 1 h window at t = 7_300: floor(7_300 / 3_600) = 2 -> start 7_200, 3_500 s left.
    assert window_bounds(_at(7_300), 3600) == (7_200, 3_500)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_fixed_window_counts_and_resets(backend, session_factory):
    limiter = MemoryLimiter() if backend == "memory" else PostgresLimiter(session_factory)
    t = 1_000_015
    decisions = [limiter.hit("k", 3, 60, _at(t + i)) for i in range(4)]
    assert [d.count for d in decisions] == [1, 2, 3, 4]
    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert decisions[-1].retry_after == 2  # t + 3 = 1_000_018; window ends 1_000_020
    # Other keys are independent.
    assert limiter.hit("other", 3, 60, _at(t)).count == 1
    # The next window starts over.
    fresh = limiter.hit("k", 3, 60, _at(1_000_020))
    assert fresh.count == 1 and fresh.allowed


def test_postgres_limiter_prunes_expired_windows(session_factory):
    limiter = PostgresLimiter(session_factory)
    limiter.hit("a", 5, 60, _at(1_000_015))  # window ends 1_000_020
    limiter.hit("b", 5, 3600, _at(1_000_015))  # 1-hour window ends 1_000_800
    with session_factory() as db:
        assert prune_counters(db, _at(1_000_100)) == 1  # only "a" has ended


def _mini_app(rule: str) -> TestClient:
    app = FastAPI()
    app.state.rate_limiter = MemoryLimiter()

    @app.get("/x", dependencies=[Depends(rate_limit(rule))])
    def x() -> dict:
        return {"ok": True}

    return TestClient(app)


def test_dependency_raises_429_with_retry_after():
    client = _mini_app("api_ip")  # 60 per minute
    for _ in range(60):
        assert client.get("/x").status_code == 200
    resp = client.get("/x")
    assert resp.status_code == 429
    assert 1 <= int(resp.headers["Retry-After"]) <= 60


def test_client_ip_honours_proxy_headers_only_when_trusted(monkeypatch):
    settings = get_settings()
    app = FastAPI()

    @app.get("/ip")
    def ip(request: Request) -> dict:
        return {"ip": ratelimit.client_ip(request)}

    client = TestClient(app)
    headers = {"CF-Connecting-IP": "203.0.113.9", "Fly-Client-IP": "198.51.100.7"}
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    assert client.get("/ip", headers=headers).json()["ip"] == "testclient"
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    assert client.get("/ip", headers=headers).json()["ip"] == "203.0.113.9"
    assert client.get("/ip", headers={"Fly-Client-IP": "198.51.100.7"}).json()["ip"] == (
        "198.51.100.7"
    )


def test_login_limited_per_ip_before_bcrypt(client, monkeypatch):
    calls = []
    real = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda p, h: calls.append(1) or real(p, h))
    for i in range(10):
        resp = client.post("/auth/login", json={"email": f"x{i}@example.com", "password": "nope"})
        assert resp.status_code == 401
    assert len(calls) == 10
    resp = client.post("/auth/login", json={"email": "y@example.com", "password": "nope"})
    assert resp.status_code == 429 and "Retry-After" in resp.headers
    assert len(calls) == 10  # the 11th attempt never reached bcrypt


def test_login_limited_per_email_across_ips(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    for i in range(20):  # 20 per hour per email, each from a different IP
        resp = client.post(
            "/auth/login",
            json={"email": "victim@example.com", "password": "guess"},
            headers={"CF-Connecting-IP": f"203.0.113.{i}"},
        )
        assert resp.status_code == 401
    resp = client.post(
        "/auth/login",
        json={"email": "victim@example.com", "password": "guess"},
        headers={"CF-Connecting-IP": "203.0.113.99"},
    )
    assert resp.status_code == 429


def test_signup_limited_per_ip(client):
    for i in range(5):
        body = dict(SIGNUP, email=f"s{i}@example.com")
        assert client.post("/auth/signup", json=body).status_code == 201
    resp = client.post("/auth/signup", json=dict(SIGNUP, email="s9@example.com"))
    assert resp.status_code == 429


def test_login_form_shows_limit_message(client):
    for _ in range(10):
        client.post("/login-form", data={"email": "a@example.com", "password": "wrong"})
    resp = client.post("/login-form", data={"email": "a@example.com", "password": "wrong"})
    assert resp.status_code == 429 and "Too many attempts" in resp.text


def test_public_api_limited_per_ip(client):
    for _ in range(60):
        assert client.get("/api/strategies/nope").status_code == 404
    resp = client.get("/api/strategies/nope")
    assert resp.status_code == 429 and resp.headers["Retry-After"]
    assert client.get("/health").status_code == 200  # non-API routes are untouched


def test_follow_limited_per_user(client):
    client.post("/auth/signup", json=SIGNUP)
    for _ in range(60):
        assert client.post("/follows/nope").status_code == 404
    assert client.post("/follows/nope").status_code == 429


def test_build_limiter_rejects_unknown_kind():
    with pytest.raises(ValueError):
        ratelimit.build_limiter("redis")
    assert isinstance(ratelimit.build_limiter("memory"), MemoryLimiter)

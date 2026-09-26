"""csrf.py: Origin/Referer check on cookie-authenticated unsafe methods
(implementation_plan P3 item 5)."""

from starlette.testclient import TestClient

from alphamarket.config import get_settings

SIGNUP = {"email": "csrf@example.com", "password": "hunter2hunter2", "display_name": "CSRF Test"}


def _signup(c, **headers):
    return c.post("/auth/signup", json=SIGNUP, headers=headers or None)


def test_cookieless_request_passes_even_cross_origin(client):
    """No session cookie yet (first signup of this client) -> nothing to
    forge into, so a mismatched Origin is allowed through."""
    resp = _signup(client, origin="https://evil.example")
    assert resp.status_code == 201
    # The signup response DID set a session cookie now.
    assert "session" in client.cookies


def test_cross_origin_request_with_session_cookie_is_rejected(client):
    _signup(client)  # same-origin: default header from the client fixture
    assert "session" in client.cookies

    resp = client.post("/auth/logout", headers={"origin": "https://evil.example"})
    assert resp.status_code == 403
    assert "CSRF" in resp.text


def test_missing_origin_with_valid_referer_passes(client):
    _signup(client)
    settings = get_settings()

    # A fresh client sharing the same app and cookies, but with NO default
    # Origin header at all (simulating a browser that omits Origin on some
    # same-site requests) and a Referer that matches public_base_url.
    referer_only = TestClient(
        client.app,
        cookies=dict(client.cookies),
        headers={"referer": f"{settings.public_base_url}/dashboard"},
    )
    resp = referer_only.post("/auth/logout")
    assert resp.status_code == 204


def test_missing_origin_and_missing_referer_is_rejected(client):
    _signup(client)
    settings = get_settings()
    bare = TestClient(client.app, cookies=dict(client.cookies), headers={})
    resp = bare.post("/auth/logout")
    assert resp.status_code == 403
    assert settings.public_base_url  # sanity: setting exists and is truthy


def test_mismatched_referer_is_rejected(client):
    _signup(client)
    no_origin = TestClient(
        client.app,
        cookies=dict(client.cookies),
        headers={"referer": "https://evil.example/dashboard"},
    )
    resp = no_origin.post("/auth/logout")
    assert resp.status_code == 403


def test_get_requests_are_never_checked(client):
    """Safe methods are never CSRF-checked, cookie or not."""
    _signup(client)
    resp = client.get("/auth/me", headers={"origin": "https://evil.example"})
    assert resp.status_code == 200


def test_csrf_allowed_origins_setting_is_honoured(client):
    _signup(client)
    settings = get_settings()
    original = settings.csrf_allowed_origins
    settings.csrf_allowed_origins = "https://staging.example"
    try:
        resp = client.post("/auth/logout", headers={"origin": "https://staging.example"})
        assert resp.status_code == 204
    finally:
        settings.csrf_allowed_origins = original

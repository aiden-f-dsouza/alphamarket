"""OAuth2 authorization-code + PKCE client and the Alpaca connect flow.
respx only -- invariant #6."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from sqlalchemy import select

from alphamarket import oauth
from alphamarket.config import get_settings
from alphamarket.models import VenueAccount, VenueAccountStatus
from alphamarket.oauth import OAuthProvider
from alphamarket.vault import decrypt_secret

PAPER = "https://paper-api.alpaca.markets"
TOKEN_URL = "https://api.alpaca.markets/oauth/token"
FIXTURES = Path(__file__).parent / "fixtures" / "alpaca"
ACCOUNT_ID = "e6fe16f3-64a4-4921-8928-cadf02f92f98"


def _fx(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def alpaca_app():
    s = get_settings()
    saved = (s.alpaca_client_id, s.alpaca_client_secret, s.alpaca_env, s.public_base_url)
    s.alpaca_client_id, s.alpaca_client_secret = "koine-client", "koine-client-secret"
    s.alpaca_env, s.public_base_url = "paper", "http://testserver"
    yield
    s.alpaca_client_id, s.alpaca_client_secret, s.alpaca_env, s.public_base_url = saved


@pytest.fixture()
def user(client):
    resp = client.post(
        "/auth/signup",
        json={"email": "o@example.com", "password": "hunter2hunter2", "display_name": "Eq"},
    )
    assert resp.status_code == 201
    return resp.json()


def _provider(**kw) -> OAuthProvider:
    base = dict(
        venue="test",
        authorize_url="https://venue.example/authorize",
        token_url="https://venue.example/token",
        revoke_url="https://venue.example/revoke",
        client_id="cid",
        client_secret="csecret",
        forbidden_scopes=frozenset({"trading"}),
    )
    return OAuthProvider(**{**base, **kw})


# ---- PKCE and the authorize URL ---------------------------------------------------


def test_code_challenge_matches_rfc7636_appendix_b():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert oauth.code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_verifier_length_is_within_rfc_bounds():
    v = oauth.new_code_verifier()
    assert 43 <= len(v) <= 128


def test_authorize_url_requests_no_scope_and_pins_env():
    from alphamarket.connectors.registry import get_oauth_provider

    url = oauth.authorize_url(get_oauth_provider("alpaca"), "http://cb", "st4te", "v" * 50)
    parts = urlsplit(url)
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        "https://app.alpaca.markets/oauth/authorize"
    )
    assert q["response_type"] == "code" and q["client_id"] == "koine-client"
    assert q["state"] == "st4te" and q["redirect_uri"] == "http://cb"
    assert q["env"] == "paper"
    assert q["code_challenge"] == oauth.code_challenge("v" * 50)
    assert q["code_challenge_method"] == "S256"
    assert "scope" not in q  # Alpaca's read-only default


def test_state_cookie_is_signed_and_expires():
    raw = oauth.dump_state({"s": "abc", "u": 1})
    assert oauth.load_state(raw) == {"s": "abc", "u": 1}
    assert oauth.load_state(raw + "x") is None  # tampered
    assert oauth.load_state(raw, max_age=-1) is None  # expired
    assert oauth.load_state(None) is None


# ---- token endpoint ---------------------------------------------------------------


@respx.mock
def test_exchange_sends_verifier_and_client_auth():
    route = respx.post("https://venue.example/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "at",
                "refresh_token": "rt",
                "expires_in": 3600,
                "token_type": "bearer",
                "scope": "read",
            },
        )
    )
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    rec = oauth.exchange_code(_provider(), "code1", "http://cb", "verif", now=now)
    form = parse_qs(route.calls.last.request.content.decode())
    assert form["grant_type"] == ["authorization_code"]
    assert form["code_verifier"] == ["verif"]
    assert form["client_secret"] == ["csecret"]
    # expires_in 3600 s from 12:00 -> 13:00 UTC
    assert rec == {
        "access_token": "at",
        "token_type": "bearer",
        "scope": "read",
        "refresh_token": "rt",
        "expires_at": "2026-09-26T13:00:00+00:00",
    }


@respx.mock
def test_exchange_refusal_raises_without_echoing_secrets():
    respx.post("https://venue.example/token").mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    with pytest.raises(oauth.OAuthError) as exc:
        oauth.exchange_code(_provider(), "bad", "http://cb", "v")
    assert "invalid_grant" in str(exc.value) and "csecret" not in str(exc.value)


def test_refresh_due_margin_and_jitter():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

    def rec(minutes):
        return {"refresh_token": "rt", "expires_at": (now + timedelta(minutes=minutes)).isoformat()}

    # deadline = expires_at - 5 min margin - jitter
    assert oauth.refresh_due(rec(4), now, jitter_seconds=0)  # 12:04 - 5m = 11:59 <= now
    assert not oauth.refresh_due(rec(10), now, jitter_seconds=0)  # 12:05 > now
    assert oauth.refresh_due(rec(6), now, jitter_seconds=120)  # 12:06 - 5m - 2m = 11:59
    assert not oauth.refresh_due(rec(6), now, jitter_seconds=30)  # 12:00:30 > now
    assert not oauth.refresh_due({"expires_at": rec(1)["expires_at"]}, now)  # no refresh token
    assert not oauth.refresh_due({"refresh_token": "rt", "expires_at": None}, now)  # Alpaca


@respx.mock
def test_refresh_keeps_the_old_refresh_token_when_none_is_returned():
    respx.post("https://venue.example/token").mock(
        return_value=httpx.Response(200, json={"access_token": "at2", "expires_in": 60})
    )
    fresh = oauth.refresh_token(_provider(), {"access_token": "at", "refresh_token": "rt"})
    assert fresh["access_token"] == "at2" and fresh["refresh_token"] == "rt"


@respx.mock
def test_refresh_that_grants_a_write_scope_is_refused():
    respx.post("https://venue.example/token").mock(
        return_value=httpx.Response(200, json={"access_token": "at2", "scope": "trading"})
    )
    with pytest.raises(oauth.OAuthError):
        oauth.refresh_token(_provider(), {"access_token": "at", "refresh_token": "rt"})


def test_ensure_fresh_is_a_no_op_for_alpaca_tokens():
    secret = json.dumps({"access_token": "at", "scope": "", "expires_at": None})
    assert oauth.ensure_fresh("alpaca", secret) is None
    assert oauth.ensure_fresh("kalshi", "pem") is None


@respx.mock
def test_revoke_rfc7009_prefers_the_refresh_token():
    route = respx.post("https://venue.example/revoke").mock(return_value=httpx.Response(200))
    assert oauth.revoke(_provider(), json.dumps({"access_token": "at", "refresh_token": "rt"}))
    form = parse_qs(route.calls.last.request.content.decode())
    assert form["token"] == ["rt"] and form["token_type_hint"] == ["refresh_token"]


@respx.mock
def test_revoke_never_raises():
    respx.post("https://venue.example/revoke").mock(side_effect=httpx.ConnectError("down"))
    assert oauth.revoke(_provider(), "at") is False
    assert oauth.revoke(_provider(revoke_url=None), "at") is False


# ---- routes -----------------------------------------------------------------------


def test_start_requires_login(client):
    resp = client.get("/oauth/alpaca/start?strategy_name=Growth", follow_redirects=False)
    assert resp.status_code == 401


def test_unknown_or_unconfigured_venue(client, user):
    assert client.get("/oauth/kalshi/start?strategy_name=xx").status_code == 404
    get_settings().alpaca_client_id = ""
    assert client.get("/oauth/alpaca/start?strategy_name=xx").status_code == 503


def _start(client) -> str:
    resp = client.get("/oauth/alpaca/start?strategy_name=Dividend%20growth", follow_redirects=False)
    assert resp.status_code == 302
    q = parse_qs(urlsplit(resp.headers["location"]).query)
    assert q["redirect_uri"] == ["http://testserver/oauth/alpaca/callback"]
    cookie = resp.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Path=/oauth/alpaca" in cookie
    return q["state"][0]


def _mock_alpaca(scope="", probe_status=403):
    token = {"access_token": "user-token-not-real", "token_type": "bearer", "scope": scope}
    token_route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token))
    respx.get(f"{PAPER}/v2/account").mock(
        return_value=httpx.Response(200, json=_fx("account.json"))
    )
    respx.get(f"{PAPER}/v2/positions").mock(
        return_value=httpx.Response(200, json=_fx("positions.json"))
    )
    probe = respx.post(f"{PAPER}/v2/orders").mock(
        return_value=httpx.Response(probe_status, json={"message": "forbidden"})
    )
    return token_route, probe


@respx.mock
def test_callback_connects_a_read_only_grant(client, user, session_factory):
    token_route, probe = _mock_alpaca()
    state = _start(client)
    resp = client.get(f"/oauth/alpaca/callback?code=c0de&state={state}", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/dashboard"
    form = parse_qs(token_route.calls.last.request.content.decode())
    assert form["code"] == ["c0de"] and "code_verifier" in form
    assert probe.called  # paper: the impossible-order probe ran and was refused
    with session_factory() as db:
        account = db.scalar(select(VenueAccount))
        assert account.venue == "alpaca" and account.venue_env == "paper"
        assert account.key_id == ACCOUNT_ID
        assert account.status == VenueAccountStatus.ACTIVE
        assert account.scope_checked_at is not None
        stored = json.loads(decrypt_secret(account.encrypted_private_key))
        assert stored["access_token"] == "user-token-not-real" and stored["scope"] == ""
        # M7 anchor = broker equity 103820.56 -> 103_820_560_000 micros
        assert account.anchor_balance_micros == 103_820_560_000
    listed = client.get("/venues").json()
    assert "user-token-not-real" not in json.dumps(listed)


@respx.mock
def test_callback_rejects_state_mismatch(client, user):
    _start(client)
    resp = client.get("/oauth/alpaca/callback?code=c&state=forged", follow_redirects=False)
    assert resp.status_code == 400


def test_callback_without_start_cookie_is_rejected(client, user):
    resp = client.get("/oauth/alpaca/callback?code=c&state=x", follow_redirects=False)
    assert resp.status_code == 400


@respx.mock
def test_callback_refuses_a_trading_grant(client, user, session_factory):
    _mock_alpaca(scope="account:write trading")
    state = _start(client)
    resp = client.get(f"/oauth/alpaca/callback?code=c&state={state}", follow_redirects=False)
    assert resp.status_code == 422
    with session_factory() as db:
        assert db.scalar(select(VenueAccount)) is None


@respx.mock
def test_callback_refuses_when_the_probe_shows_write_access(client, user, session_factory):
    _mock_alpaca(probe_status=422)
    state = _start(client)
    resp = client.get(f"/oauth/alpaca/callback?code=c&state={state}", follow_redirects=False)
    assert resp.status_code == 422
    with session_factory() as db:
        assert db.scalar(select(VenueAccount)) is None


def test_user_declining_on_alpaca(client, user):
    state = _start(client)
    resp = client.get(
        f"/oauth/alpaca/callback?error=access_denied&state={state}", follow_redirects=False
    )
    assert resp.status_code == 400


def test_pasting_a_token_into_post_venues_is_refused(client, user):
    resp = client.post(
        "/venues",
        json={"venue": "alpaca", "key_id": ACCOUNT_ID, "secret": "tok", "strategy_name": "Eq"},
    )
    assert resp.status_code == 422
    assert "OAuth" in resp.json()["detail"]


@respx.mock
def test_disconnect_erases_the_alpaca_token(client, user, session_factory):
    _mock_alpaca()
    state = _start(client)
    client.get(f"/oauth/alpaca/callback?code=c&state={state}", follow_redirects=False)
    account_id = client.get("/venues").json()[0]["id"]
    assert client.delete(f"/venues/{account_id}").status_code == 204
    with session_factory() as db:
        account = db.get(VenueAccount, account_id)
        assert account.encrypted_private_key is None
        assert account.status == VenueAccountStatus.DISCONNECTED


def test_connect_page_offers_the_oauth_button_not_a_secret_field(client, user):
    html = client.get("/dashboard/connect?venue=alpaca").text
    assert 'action="/oauth/alpaca/start"' in html
    assert 'name="secret"' not in html


def test_token_record_keeps_a_missing_scope_unknown():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    # Venue omitted `scope`: recorded as None ("unknown"), never "" ("none granted").
    assert oauth.token_record({"access_token": "at"}, now)["scope"] is None
    assert oauth.token_record({"access_token": "at", "scope": ""}, now)["scope"] == ""
    # A refresh that omits scope keeps the original grant's (RFC 6749 section 6).
    prev = {"access_token": "old", "scope": "read", "refresh_token": "rt"}
    assert oauth.token_record({"access_token": "at2"}, now, previous=prev)["scope"] == "read"
    assert oauth.granted_scopes({"scope": None}) == set()


def _record_revokes(monkeypatch) -> list[str]:
    revoked: list[str] = []

    def fake_revoke(provider, secret, client=None):
        revoked.append(secret)
        return False

    monkeypatch.setattr(oauth, "revoke", fake_revoke)
    return revoked


@respx.mock
def test_callback_revokes_a_grant_the_connect_step_refused(client, user, monkeypatch):
    revoked = _record_revokes(monkeypatch)
    _mock_alpaca(probe_status=422)  # the probe shows write access: 422, nothing stored
    state = _start(client)
    resp = client.get(f"/oauth/alpaca/callback?code=c&state={state}", follow_redirects=False)
    assert resp.status_code == 422
    assert len(revoked) == 1
    assert json.loads(revoked[0])["access_token"] == "user-token-not-real"


@respx.mock
def test_callback_revokes_when_the_account_cannot_be_read(client, user, monkeypatch):
    revoked = _record_revokes(monkeypatch)
    token = {"access_token": "user-token-not-real", "token_type": "bearer", "scope": ""}
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token))
    respx.get(f"{PAPER}/v2/account").mock(return_value=httpx.Response(500, json={}))
    state = _start(client)
    resp = client.get(f"/oauth/alpaca/callback?code=c&state={state}", follow_redirects=False)
    assert resp.status_code == 502
    assert len(revoked) == 1

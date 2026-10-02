"""wave2 w2-p8-connect exit tests (D18 consent, D20 SnapTrade pending
connects, D21 signed claims; invariants 9, 10, 11). Venue traffic is
respx-mocked (invariant 6); signatures come from a throwaway eth_account
test key, never a real wallet."""

import dataclasses
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import HTTPException
from sqlalchemy import func, select

from alphamarket import consent as consent_mod
from alphamarket import credentials, pending
from alphamarket.config import get_settings
from alphamarket.connectors.base import CredentialKind
from alphamarket.connectors.hyperliquid import INFO_URL, claim_message
from alphamarket.connectors.registry import PROFILES
from alphamarket.connectors.snaptrade import BASE_URL as SNAP
from alphamarket.models import (
    AuditLog,
    ConsentText,
    Strategy,
    User,
    VenueAccount,
    VenueAccountStatus,
    VenueConsent,
    VenueCredential,
)
from alphamarket.venues import ConnectIn, assemble_secret
from authutil import consent, secure_account
from test_security_headers import _assert_headers, _assert_no_inline_script_or_handlers
from test_venues import _connect_polymarket, _mock_polymarket, _mock_venue, _pem

SNAP_FIXTURES = Path(__file__).parent / "fixtures" / "snaptrade"
SNAP_ACCOUNT_REF = "917c8734-8470-4a3e-a18f-57c3f2ee6631"
SNAP_AUTH_ID = "87b24961-b51e-4db8-9226-f198f6518a89"

# Throwaway secp256k1 keys for signing tests (never funded, never on a venue).
WALLET = Account.from_key("0x" + "11" * 32)
OTHER_WALLET = Account.from_key("0x" + "22" * 32)


def _snap_fixture(name: str):
    return json.loads((SNAP_FIXTURES / name).read_text())


@pytest.fixture()
def user(client):
    resp = client.post(
        "/auth/signup",
        json={"email": "w2@example.com", "password": "hunter2hunter2", "display_name": "Wave"},
    )
    assert resp.status_code == 201
    secure_account(client, "w2@example.com")
    return resp.json()


def _kalshi_json(client, **extra):
    body = {
        "venue": "kalshi",
        "key_id": "demo-key-id",
        "private_key_pem": _pem(),
        "strategy_name": "Consent test",
        **extra,
    }
    return client.post("/venues", json=body)


def _count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


# ---- consent (D18) -----------------------------------------------------------------


def test_consent_text_is_versioned_and_hashed_over_its_exact_body():
    screen = consent_mod.consent_text("hyperliquid")
    assert screen.sha256 == hashlib.sha256(screen.body.encode("utf-8")).hexdigest()
    assert screen.version.endswith(screen.sha256[:8])
    # Built from the venue's terms doc (docs/venues/hyperliquid.md) and the profile.
    assert "never ask for a private key" in screen.body
    assert "sign a Koine message" in screen.body
    assert "How to revoke" in screen.body and "What becomes public" in screen.body
    # Kalshi has no terms doc: the profile-built paragraphs alone.
    kalshi = consent_mod.consent_text("kalshi")
    assert kalshi.body.startswith("Connect Kalshi to Koine")
    assert kalshi.sha256 != screen.sha256


@respx.mock
def test_json_connect_without_consent_is_422_and_creates_nothing(client, user, session_factory):
    _mock_venue()
    resp = _kalshi_json(client)
    assert resp.status_code == 422
    assert "consent" in resp.json()["detail"].lower()
    resp = _kalshi_json(client, consent_sha256="0" * 64)  # stale or forged
    assert resp.status_code == 422
    with session_factory() as db:
        assert db.scalars(select(VenueAccount)).all() == []
        assert db.scalars(select(VenueConsent)).all() == []
    # Nothing reached the venue: consent is checked before any venue call.
    assert not any("/portfolio/balance" in str(c.request.url) for c in respx.calls)


@respx.mock
def test_form_connect_with_stale_consent_rerenders_the_screen(client, user, session_factory):
    _mock_venue()
    resp = client.post(
        "/dashboard/connect",
        data={
            "key_id": "k",
            "secret": _pem(),
            "strategy_name": "Stale",
            "consent_sha256": "f" * 64,
        },
    )
    assert resp.status_code == 422
    assert consent("kalshi") in resp.text  # the page carries the CURRENT hash
    with session_factory() as db:
        assert db.scalars(select(VenueAccount)).all() == []


@respx.mock
def test_connect_records_one_consent_row_matching_the_stored_text(client, user, session_factory):
    _mock_venue()
    resp = _kalshi_json(client, consent_sha256=consent("kalshi"))
    assert resp.status_code == 201, resp.text
    with session_factory() as db:
        account = db.scalars(select(VenueAccount)).one()
        (row,) = db.scalars(select(VenueConsent)).all()
        text = db.get(ConsentText, row.text_sha256)
        assert row.venue_account_id == account.id and row.user_id == user["id"]
        assert row.venue == "kalshi"
        assert hashlib.sha256(text.body.encode("utf-8")).hexdigest() == row.text_sha256
        assert text.body == consent_mod.consent_text("kalshi").body
        assert row.text_version == text.version
        assert row.ip == "testclient" and row.user_agent == "testclient"
        (audit_row,) = db.scalars(select(AuditLog).where(AuditLog.action == "venue.consent")).all()
        assert audit_row.target_id == str(account.id)
        assert audit_row.details["consent_id"] == row.id
        assert audit_row.details["text_version"] == row.text_version
    # A second connect reuses the stored text (one consent_texts row per body).
    _mock_venue()
    assert _kalshi_json(client, consent_sha256=consent("kalshi")).status_code == 201
    with session_factory() as db:
        assert _count(db, ConsentText) == 1
        assert _count(db, VenueConsent) == 2


def _race(factory, monkeypatch) -> None:
    """Two requests store the same text: both looked and saw no row, the
    first INSERT wins, the second conflicts on the primary key and must be
    ignored, never an error (ON CONFLICT DO NOTHING)."""
    screen = consent_mod.consent_text("polymarket")
    with factory() as a, factory() as b:
        user = User(email="r@example.com", password_hash="x", display_name="R")
        a.add(user)
        a.commit()
        consent_mod.require_consent(a, user, "polymarket", screen.sha256)
        # b's existence check ran before a committed: it saw nothing.
        monkeypatch.setattr(b, "get", lambda *args, **kwargs: None)
        assert consent_mod.require_consent(b, user, "polymarket", screen.sha256) == screen
    with factory() as db:
        rows = db.scalars(select(ConsentText)).all()
        assert [r.sha256 for r in rows] == [screen.sha256]
        assert rows[0].body == screen.body


def test_require_consent_tolerates_a_concurrent_first_insert(session_factory, monkeypatch):
    _race(session_factory, monkeypatch)


@pytest.mark.pg
def test_require_consent_race_on_postgres(pg_session_factory, monkeypatch):
    _race(pg_session_factory, monkeypatch)


def test_require_consent_rejects_missing_and_stale(session_factory):
    with session_factory() as db:
        user = User(email="s@example.com", password_hash="x", display_name="S")
        db.add(user)
        db.commit()
        for bad in (None, "", "abc", consent("kalshi")):
            with pytest.raises(HTTPException) as exc:
                consent_mod.require_consent(db, user, "polymarket", bad)
            assert exc.value.status_code == 422
        assert _count(db, ConsentText) == 0


# ---- every credential kind renders -------------------------------------------------


def test_every_credential_kind_form_renders(client, user, monkeypatch):
    seen = set()
    # No registered venue uses KEY_SECRET_PASSPHRASE yet: render it on a
    # stand-in profile so the branch is exercised.
    stand_in = dataclasses.replace(
        PROFILES["kraken"], credential=CredentialKind.KEY_SECRET_PASSPHRASE
    )
    monkeypatch.setitem(PROFILES, "kraken", stand_in)
    for venue, profile in PROFILES.items():
        resp = client.get(f"/dashboard/connect?venue={venue}")
        assert resp.status_code == 200, venue
        html = resp.text
        _assert_headers(resp)
        _assert_no_inline_script_or_handlers(html, venue)
        screen = consent_mod.consent_text(venue)
        assert f'name="consent_sha256" value="{screen.sha256}"' in html, venue
        kind = str(profile.credential)
        seen.add(kind)
        if kind == "oauth2":
            assert f'action="/oauth/{venue}/start"' in html and 'name="secret"' not in html
        elif kind == "aggregator":
            assert f'action="/dashboard/connect/{venue}/start"' in html
            assert 'name="secret"' not in html and 'name="key_id"' not in html
        elif kind == "address":
            assert 'name="secret"' not in html and 'placeholder="0x…"' in html
            if str(profile.ownership) == "signed_message":
                assert "Sign a Koine message with your wallet" in html
            else:
                assert "bio" in html
        else:
            assert 'action="/dashboard/connect"' in html and 'name="secret"' in html
            assert ("contents of the .pem file" in html) == (kind == "rsa_pem")
            assert ('name="passphrase"' in html) == (kind == "key_secret_passphrase")
            assert ('name="account_ref"' in html) == (kind == "report_token")
    assert seen == {str(k) for k in CredentialKind}


def test_passphrase_secret_is_assembled_server_side():
    profile = dataclasses.replace(
        PROFILES["kraken"], credential=CredentialKind.KEY_SECRET_PASSPHRASE
    )
    base = {"venue": "kraken", "key_id": "k", "strategy_name": "Pp"}
    secret = assemble_secret(profile, ConnectIn(**base, secret="s3", passphrase="p@ss"))
    assert json.loads(secret) == {"secret": "s3", "passphrase": "p@ss"}
    # An API caller may send the JSON itself.
    raw = json.dumps({"secret": "s3", "passphrase": "p@ss"})
    assert assemble_secret(profile, ConnectIn(**base, secret=raw)) == raw
    with pytest.raises(HTTPException) as exc:
        assemble_secret(profile, ConnectIn(**base, secret="s3"))
    assert exc.value.status_code == 422 and "s3" not in exc.value.detail
    # Other kinds pass the secret through untouched.
    assert assemble_secret(PROFILES["kraken"], ConnectIn(**base, secret="s3")) == "s3"


def test_form_validation_error_never_echoes_the_secret(client, user):
    resp = client.post(
        "/dashboard/connect",
        data={
            "venue": "kalshi",
            "key_id": "k",
            "secret": "pasted-secret-topsecret",
            "strategy_name": "x",  # too short
            "consent_sha256": consent("kalshi"),
        },
    )
    assert resp.status_code == 422
    assert "strategy name" in resp.text and "topsecret" not in resp.text


# ---- Hyperliquid signed claim (D21) ------------------------------------------------


def _seed_hyperliquid(session_factory, user_id: int, address: str) -> tuple[int, str]:
    with session_factory() as db:
        account = VenueAccount(
            user_id=user_id,
            venue="hyperliquid",
            venue_env="prod",
            key_id=address.lower(),
            status=VenueAccountStatus.ACTIVE,
            claim_code="koine-1a2b3c4d",
        )
        db.add(account)
        db.flush()
        db.add(Strategy(user_id=user_id, venue_account_id=account.id, name="Perps", slug="p-1"))
        db.commit()
        return account.id, account.claim_code


def _sign(wallet, text: str) -> str:
    signed = Account.sign_message(encode_defunct(text=text), private_key=wallet.key)
    return "0x" + bytes(signed.signature).hex()


@respx.mock
def test_hyperliquid_claim_with_a_signature_over_the_stored_code(client, user, session_factory):
    account_id, code = _seed_hyperliquid(session_factory, user["id"], WALLET.address)
    # A signer that is not the address makes the connector ask Hyperliquid
    # whether it controls the address (vault leader / sub-account master).
    role = respx.post(INFO_URL).mock(
        return_value=httpx.Response(200, json={"role": "user", "data": None})
    )
    # No signature: refused with the signed-message wording.
    resp = client.post(f"/venues/{account_id}/claim")
    assert resp.status_code == 422 and "Sign the Koine message" in resp.json()["detail"]
    # A valid signature, but over ANOTHER code (e.g. one from another
    # account): the server rebuilds the message from the stored code -> 422.
    replay = _sign(WALLET, claim_message(WALLET.address, "koine-deadbeef"))
    resp = client.post(f"/venues/{account_id}/claim", json={"signature": replay})
    assert resp.status_code == 422 and code in resp.json()["detail"]
    # Another wallet signing the right message: not this address -> 422.
    stranger = _sign(OTHER_WALLET, claim_message(WALLET.address, code))
    assert (
        client.post(f"/venues/{account_id}/claim", json={"signature": stranger}).status_code == 422
    )
    assert role.called
    with session_factory() as db:
        assert db.get(VenueAccount, account_id).claimed_at is None
    # The right wallet over claim_text(stored code): claimed.
    good = _sign(WALLET, claim_message(WALLET.address, code))
    resp = client.post(f"/venues/{account_id}/claim", json={"signature": good})
    assert resp.status_code == 200, resp.text
    assert resp.json()["claimed"] is True and resp.json()["claim_code"] is None
    with session_factory() as db:
        assert db.get(VenueAccount, account_id).claimed_at is not None


@respx.mock
def test_hyperliquid_claim_page_and_pasted_signature(client, user, session_factory):
    account_id, code = _seed_hyperliquid(session_factory, user["id"], WALLET.address)
    message = claim_message(WALLET.address, code)
    resp = client.get(f"/dashboard/connect/claim/{account_id}")
    assert resp.status_code == 200
    html = resp.text
    _assert_headers(resp)
    _assert_no_inline_script_or_handlers(html, "claim")
    assert '<script src="/static/claim.js" defer></script>' in html
    assert "data-claim-form" in html and "data-claim-sign" in html
    assert 'name="signature"' in html  # the no-JS paste path
    assert message.split("\n")[2] in html  # "Address: 0x..." line of the exact text
    # The dashboard's bare Claim POST sends Hyperliquid accounts to this page.
    resp = client.post(f"/dashboard/accounts/{account_id}/claim", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/dashboard/connect/claim/{account_id}"
    # A bad paste re-renders the page with the error.
    respx.post(INFO_URL).mock(return_value=httpx.Response(200, json={"role": "user"}))
    bad = client.post(
        f"/dashboard/connect/claim/{account_id}",
        data={"signature": _sign(OTHER_WALLET, message)},
    )
    assert bad.status_code == 422 and "does not match" in bad.text
    # Pasting the right signature (no JS) claims it.
    ok = client.post(
        f"/dashboard/connect/claim/{account_id}",
        data={"signature": _sign(WALLET, message)},
        follow_redirects=False,
    )
    assert ok.status_code == 303 and ok.headers["location"] == "/dashboard"
    with session_factory() as db:
        assert db.get(VenueAccount, account_id).claimed
    # Claimed: the page has nothing left to do.
    again = client.get(f"/dashboard/connect/claim/{account_id}", follow_redirects=False)
    assert again.status_code == 303


def test_claim_js_uses_data_hooks_and_personal_sign():
    js = (Path(__file__).parents[1] / "src/alphamarket/static/claim.js").read_text()
    assert "eth_requestAccounts" in js and "personal_sign" in js
    assert "data-claim-message" in js and "[data-claim-form]" in js
    assert "TextEncoder" in js  # UTF-8 bytes, hex encoded
    assert "fetch(" not in js and "innerHTML" not in js


@respx.mock
def test_polymarket_form_connect_goes_to_the_bio_claim_page(client, user, session_factory):
    _mock_polymarket()
    resp = client.post(
        "/dashboard/connect",
        data={
            "venue": "polymarket",
            "key_id": "0x124628E686dB22da39b6Ee43CA786C8A074BD6F2",
            "strategy_name": "Weather fade",
            "consent_sha256": consent("polymarket"),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with session_factory() as db:
        account = db.scalars(select(VenueAccount)).one()
    assert resp.headers["location"] == f"/dashboard/connect/claim/{account.id}"
    html = client.get(resp.headers["location"]).text
    assert account.claim_code in html and "profile bio" in html
    assert "claim.js" not in html  # no wallet signing for a bio claim
    # The JSON path (existing helper) still works with consent.
    assert _connect_polymarket(client).status_code == 201


# ---- SnapTrade pending connect (D20) -----------------------------------------------


@pytest.fixture()
def snaptrade_keys():
    settings = get_settings()
    saved = (settings.snaptrade_client_id, settings.snaptrade_consumer_key)
    settings.snaptrade_client_id = "KOINE-TEST"
    settings.snaptrade_consumer_key = "consumer-secret-key"
    yield
    settings.snaptrade_client_id, settings.snaptrade_consumer_key = saved


def _mock_snaptrade(auth="snaptrade_authorization_read.json") -> dict[str, respx.Route]:
    routes = {
        "register": respx.post(f"{SNAP}/snapTrade/registerUser").mock(
            side_effect=lambda request: httpx.Response(
                200,
                json={
                    "userId": json.loads(request.content)["userId"],
                    "userSecret": "snap-user-secret-not-real",
                },
            )
        ),
        "login": respx.post(f"{SNAP}/snapTrade/login").mock(
            return_value=httpx.Response(
                200, json={"redirectURI": "https://app.snaptrade.com/connect?token=t"}
            )
        ),
        "accounts": respx.get(f"{SNAP}/accounts").mock(
            return_value=httpx.Response(200, json=_snap_fixture("snaptrade_accounts.json"))
        ),
        "balances": respx.get(f"{SNAP}/accounts/{SNAP_ACCOUNT_REF}/balances").mock(
            return_value=httpx.Response(200, json=_snap_fixture("snaptrade_balances.json"))
        ),
        "holdings": respx.get(f"{SNAP}/accounts/{SNAP_ACCOUNT_REF}/holdings").mock(
            return_value=httpx.Response(200, json=_snap_fixture("snaptrade_holdings.json"))
        ),
        "activities": respx.get(f"{SNAP}/accounts/{SNAP_ACCOUNT_REF}/activities").mock(
            return_value=httpx.Response(200, json={"data": [], "pagination": {"total": 0}})
        ),
        "auth": respx.get(f"{SNAP}/authorizations/{SNAP_AUTH_ID}").mock(
            return_value=httpx.Response(200, json=_snap_fixture(auth))
        ),
        "delete": respx.delete(f"{SNAP}/snapTrade/deleteUser").mock(
            return_value=httpx.Response(200, json={"status": "deleted"})
        ),
    }
    return routes


def _start_snaptrade(client) -> int:
    resp = client.post(
        "/dashboard/connect/snaptrade/start",
        data={"consent_sha256": consent("snaptrade")},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    assert location.startswith("/dashboard/connect/snaptrade/")
    return int(location.rsplit("/", 1)[1])


@respx.mock
def test_snaptrade_pending_picker_finish_and_disconnect(
    client, user, session_factory, snaptrade_keys
):
    routes = _mock_snaptrade()
    account_id = _start_snaptrade(client)
    with session_factory() as db:
        account = db.get(VenueAccount, account_id)
        assert account.status == VenueAccountStatus.PENDING
        assert account.key_id.startswith("koine-") and account.anchor_at is None
        cred = credentials.active_credential(db, account)
        assert cred.kind == "aggregator" and cred.account_ref is None
        # The user secret is only in venue_credentials, sealed.
        assert b"snap-user-secret-not-real" not in (cred.ciphertext or b"")
        (row,) = db.scalars(select(VenueConsent)).all()
        assert row.venue_account_id == account_id
        assert row.text_sha256 == consent("snaptrade")
        assert db.scalars(select(Strategy)).all() == []
        user_id = account.key_id
    registered = json.loads(routes["register"].calls.last.request.content)
    assert registered["userId"] == user_id  # one fresh SnapTrade user per connection

    # The picker: a portal link (connectionType=read, back to this page) and
    # the linked accounts, numbers masked.
    resp = client.get(f"/dashboard/connect/snaptrade/{account_id}")
    assert resp.status_code == 200
    _assert_headers(resp)
    _assert_no_inline_script_or_handlers(resp.text, "picker")
    login = json.loads(routes["login"].calls.last.request.content)
    assert login["connectionType"] == "read"
    assert login["customRedirect"].endswith(f"/dashboard/connect/snaptrade/{account_id}")
    assert 'href="https://app.snaptrade.com/connect?token=t"' in resp.text
    assert SNAP_ACCOUNT_REF in resp.text and "Robinhood" in resp.text
    assert "ending 8443" in resp.text and "Q6542138443" not in resp.text
    assert "snap-user-secret-not-real" not in resp.text

    # Finish: ACTIVE, account_ref stored, strategy created, no second consent row.
    resp = client.post(
        f"/dashboard/connect/snaptrade/{account_id}",
        data={"account_ref": SNAP_ACCOUNT_REF, "strategy_name": "Robinhood swing"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/dashboard"
    assert routes["auth"].called  # read-only proof ran (connectionType read)
    with session_factory() as db:
        account = db.get(VenueAccount, account_id)
        assert account.status == VenueAccountStatus.ACTIVE
        assert account.claimed and account.anchor_at is not None
        assert account.anchor_balance_micros == 300_710_000  # cash 300.71 USD
        cred = credentials.active_credential(db, account)
        assert cred.account_ref == SNAP_ACCOUNT_REF
        assert credentials.open_credential(db, account, purpose="test").secret == (
            "snap-user-secret-not-real"
        )
        assert db.scalars(select(Strategy)).one().name == "Robinhood swing"
        assert _count(db, VenueConsent) == 1
    # Finished: the picker sends you back to the dashboard.
    assert (
        client.get(f"/dashboard/connect/snaptrade/{account_id}", follow_redirects=False).status_code
        == 303
    )

    # Disconnect deletes the SnapTrade user (by id; no secret needed).
    assert client.delete(f"/venues/{account_id}").status_code == 204
    assert routes["delete"].called
    q = parse_qs(urlsplit(str(routes["delete"].calls.last.request.url)).query)
    assert q["userId"] == [user_id] and "userSecret" not in q
    with session_factory() as db:
        account = db.get(VenueAccount, account_id)
        assert account.status == VenueAccountStatus.DISCONNECTED
        assert credentials.active_credential(db, account) is None


@respx.mock
def test_snaptrade_trade_connection_is_refused_and_stays_pending(
    client, user, session_factory, snaptrade_keys
):
    _mock_snaptrade(auth="snaptrade_authorization_trade.json")
    account_id = _start_snaptrade(client)
    resp = client.post(
        f"/dashboard/connect/snaptrade/{account_id}",
        data={"account_ref": SNAP_ACCOUNT_REF, "strategy_name": "Writable"},
    )
    assert resp.status_code == 422 and "read-only" in resp.text
    with session_factory() as db:
        assert db.get(VenueAccount, account_id).status == VenueAccountStatus.PENDING
        assert db.scalars(select(Strategy)).all() == []


@respx.mock
def test_snaptrade_start_needs_consent(client, user, session_factory, snaptrade_keys):
    routes = _mock_snaptrade()
    resp = client.post("/dashboard/connect/snaptrade/start", data={})
    assert resp.status_code == 422
    assert not routes["register"].called  # no billable user without consent
    with session_factory() as db:
        assert db.scalars(select(VenueAccount)).all() == []


def test_picker_needs_the_connect_gate_to_finish(client, session_factory, snaptrade_keys):
    client.post(
        "/auth/signup",
        json={"email": "nog@example.com", "password": "hunter2hunter2", "display_name": "Nog"},
    )
    resp = client.post(
        "/dashboard/connect/snaptrade/start",
        data={"consent_sha256": consent("snaptrade")},
        follow_redirects=False,
    )
    assert resp.status_code == 403, (resp.status_code, resp.headers.get("location"))
    with session_factory() as db:
        assert db.scalars(select(VenueAccount)).all() == []


# ---- abandon_stale ------------------------------------------------------------------


@respx.mock
def test_abandon_stale_drops_pending_older_than_the_ttl(session_factory, snaptrade_keys):
    delete = respx.delete(f"{SNAP}/snapTrade/deleteUser").mock(
        return_value=httpx.Response(200, json={"status": "deleted"})
    )
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    with session_factory() as db:
        owner = User(email="p@example.com", password_hash="x", display_name="P")
        db.add(owner)
        db.flush()
        ids = {}
        for name, age in (("stale", timedelta(hours=25)), ("fresh", timedelta(hours=1))):
            account = VenueAccount(
                user_id=owner.id,
                venue="snaptrade",
                venue_env="prod",
                key_id=f"koine-{name}",
                status=VenueAccountStatus.PENDING,
                created_at=now - age,
            )
            db.add(account)
            db.flush()
            credentials.store(db, account, f"secret-{name}", kind="aggregator")
            ids[name] = account.id
        # An ACTIVE account of the same age is never touched.
        active = VenueAccount(
            user_id=owner.id,
            venue="snaptrade",
            venue_env="prod",
            key_id="koine-active",
            status=VenueAccountStatus.ACTIVE,
            created_at=now - timedelta(days=3),
        )
        db.add(active)
        db.commit()

        assert pending.abandon_stale(db, now) == 1
        stale = db.get(VenueAccount, ids["stale"])
        fresh = db.get(VenueAccount, ids["fresh"])
        assert stale.status == VenueAccountStatus.DISCONNECTED
        assert credentials.active_credential(db, stale) is None
        assert fresh.status == VenueAccountStatus.PENDING
        assert credentials.active_credential(db, fresh) is not None
        assert db.get(VenueAccount, active.id).status == VenueAccountStatus.ACTIVE
        (row,) = db.scalars(
            select(AuditLog).where(
                AuditLog.action == "credential.disconnect",
                AuditLog.target_id == str(ids["stale"]),
            )
        ).all()
        assert row.details["reason"] == "pending_abandoned"
        erased = db.scalars(
            select(VenueCredential).where(VenueCredential.venue_account_id == ids["stale"])
        ).one()
        assert erased.ciphertext is None and erased.revoked_at is not None
        # Idempotent: nothing left to abandon.
        assert pending.abandon_stale(db, now) == 0
    assert delete.call_count == 1
    q = parse_qs(urlsplit(str(delete.calls.last.request.url)).query)
    assert q["userId"] == ["koine-stale"]


# ---- partner gate and OAuth consent ------------------------------------------------


@pytest.fixture()
def closed(client, user):
    settings = get_settings()
    settings.enable_unapproved_venues = False
    yield client
    settings.enable_unapproved_venues = True


@respx.mock
def test_partner_gate_still_refuses_every_new_path(closed, session_factory, snaptrade_keys):
    routes = _mock_snaptrade()
    resp = closed.post(
        "/dashboard/connect/snaptrade/start", data={"consent_sha256": consent("snaptrade")}
    )
    assert resp.status_code == 403 and "written approval" in resp.text
    assert not routes["register"].called
    resp = closed.post(
        "/venues",
        json={
            "venue": "kraken",
            "key_id": "k",
            "secret": "s",
            "strategy_name": "Kr",
            "consent_sha256": consent("kraken"),
        },
    )
    assert resp.status_code == 403
    resp = closed.get(
        "/oauth/alpaca/start",
        params={"strategy_name": "Eq", "consent_sha256": consent("alpaca")},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    html = closed.get("/dashboard/connect?venue=snaptrade").text
    assert 'type="submit" disabled' in html
    with session_factory() as db:
        assert db.scalars(select(VenueAccount)).all() == []


def test_oauth_start_requires_consent_and_carries_it_in_state(client, user):
    get_settings().alpaca_client_id = get_settings().alpaca_client_id or "cid"
    get_settings().alpaca_client_secret = get_settings().alpaca_client_secret or "csecret"
    resp = client.get("/oauth/alpaca/start?strategy_name=Growth", follow_redirects=False)
    assert resp.status_code == 422
    resp = client.get(
        "/oauth/alpaca/start",
        params={"strategy_name": "Growth", "consent_sha256": consent("alpaca")},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    from alphamarket import oauth

    raw = resp.cookies.get(oauth.cookie_name("alpaca")) or client.cookies.get(
        oauth.cookie_name("alpaca")
    )
    assert oauth.load_state(raw)["c"] == consent("alpaca")

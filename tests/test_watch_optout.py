"""Watchlist opt-out (watchlist.perform_optout, watch_pages /watch/optout):
the address owner proves the address (EIP-191 signature for Hyperliquid, a
code in the profile bio for Polymarket), then one transaction records the
append-only opt-out, deletes every Watch on the address, stops every active
tail of it and audits. Venue traffic is respx-mocked (invariant 6); signatures
come from a throwaway eth_account key, never a real wallet."""

import re

import httpx
import pytest
import respx
from eth_account import Account
from eth_account.messages import encode_defunct
from sqlalchemy import select

from alphamarket.connectors.base import RateBudget, RateScope
from alphamarket.connectors.hyperliquid import INFO_URL, claim_message
from alphamarket.models import (
    Allocation,
    AllocationStatus,
    AppendOnlyViolation,
    AuditLog,
    User,
    Watch,
    WatchOptout,
)
from alphamarket.venue_limits import MemoryBuckets, VenueLimiter
from test_venues import POLY_ADDRESS, POLY_GAMMA
from test_watchlist import FakeInfo, add, login, signup, watched_id

WALLET = Account.from_key("0x" + "11" * 32)
OTHER_WALLET = Account.from_key("0x" + "22" * 32)
ADDR = WALLET.address.lower()
CODE_RE = re.compile(r"koine-optout-[0-9a-f]{12}")


@pytest.fixture()
def info():
    """Hyperliquid /info from the recorded fixtures (test_watchlist.FakeInfo)."""
    with respx.mock(assert_all_called=False) as router:
        fake = FakeInfo()
        router.post(INFO_URL).mock(side_effect=fake)
        yield fake


def sign(wallet, text: str) -> str:
    signed = Account.sign_message(encode_defunct(text=text), private_key=wallet.key)
    return "0x" + bytes(signed.signature).hex()


def start(client, venue="hyperliquid", address=ADDR):
    return client.post("/watch/optout", data={"venue": venue, "address": address})


def verify(client, venue="hyperliquid", address=ADDR, **extra):
    return client.post("/watch/optout/verify", data={"venue": venue, "address": address, **extra})


def issued_code(resp) -> str:
    """The code the proof step shows (the page is the only place it is issued)."""
    assert resp.status_code == 200, resp.text
    [code] = set(CODE_RE.findall(resp.text))
    return code


def tail(client, sid: int):
    resp = client.post(
        f"/watch/{sid}/tail",
        data={"allocation": "2000", "drawdown_limit_bps": "1500", "confirm": "on"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


def optouts(session_factory) -> list[WatchOptout]:
    with session_factory() as db:
        return list(db.scalars(select(WatchOptout)).all())


# ---- Hyperliquid: the whole flow -----------------------------------------------------


def test_signed_optout_removes_watches_of_every_user_stops_tails_and_audits(
    client, session_factory, info
):
    signup(client, "a@example.com")
    sid = watched_id(add(client, ADDR, label="mine"))
    tail(client, sid)
    signup(client, "b@example.com")
    assert watched_id(add(client, ADDR)) == sid  # the shared strategy
    tail(client, sid)
    signup(client, "owner@example.com")  # the address owner holds no watch

    proof = start(client)
    code = issued_code(proof)
    message = claim_message(ADDR, code)
    assert "Koine ownership claim" in proof.text and f"Address: {ADDR}" in proof.text
    assert 'name="signature"' in proof.text and "data-claim-form" in proof.text
    assert '<script src="/static/claim.js" defer></script>' in proof.text
    assert optouts(session_factory) == []  # nothing happens before the proof

    done = verify(client, signature=sign(WALLET, message))
    assert done.status_code == 200 and "Opted out." in done.text
    assert "2 watchlists" in done.text and "2 paper tails" in done.text

    with session_factory() as db:
        owner = db.scalar(select(User).where(User.email == "owner@example.com"))
        [row] = db.scalars(select(WatchOptout)).all()
        assert (row.venue, row.address, row.proved_by_user_id) == ("hyperliquid", ADDR, owner.id)
        assert db.scalars(select(Watch)).all() == []  # both users' watches
        allocations = db.scalars(select(Allocation)).all()
        assert len(allocations) == 2
        assert all(a.status == AllocationStatus.STOPPED for a in allocations)
        assert {a.stop_reason for a in allocations} == {"optout"}
        [audit] = db.scalars(select(AuditLog).where(AuditLog.action == "watch.optout")).all()
        assert audit.actor_user_id == owner.id
        assert (audit.target_type, audit.target_id) == ("watch_optout", str(row.id))
        assert audit.details == {"venue": "hyperliquid", "watches_removed": 2, "tails_stopped": 2}
        assert ADDR not in str(audit.details)

    # Both watchers lost the private page; the address cannot be added again.
    for email in ("a@example.com", "b@example.com"):
        login(client, email)
        assert client.get(f"/watch/{sid}").status_code == 404
        resp = add(client, ADDR)
        assert resp.status_code == 422 and "asked not to be tracked" in resp.text
        assert (
            client.post("/api/watch", json={"venue": "hyperliquid", "address": ADDR}).status_code
            == 422
        )


def test_a_wrong_signature_or_a_replayed_code_opts_nothing_out(client, session_factory, info):
    signup(client, "a@example.com")
    sid = watched_id(add(client, ADDR))
    signup(client, "owner@example.com")
    code = issued_code(start(client))
    message = claim_message(ADDR, code)

    # No signature at all.
    resp = verify(client)
    assert resp.status_code == 422 and "Sign the Koine message" in resp.text
    assert code in resp.text  # back on the proof step, same server-issued code
    # Another wallet signing the right message: not this address.
    resp = verify(client, signature=sign(OTHER_WALLET, message))
    assert resp.status_code == 422 and "does not match" in resp.text
    # The right wallet over a code the SERVER did not issue (replay of a
    # signature made for another user or address), also smuggled in as a
    # `code` form field the server must ignore.
    forged = claim_message(ADDR, "koine-optout-deadbeef0000")
    resp = verify(client, signature=sign(WALLET, forged), code="koine-optout-deadbeef0000")
    assert resp.status_code == 422 and "does not match" in resp.text
    # Garbage.
    assert verify(client, signature="0xnothex").status_code == 422

    assert optouts(session_factory) == []
    with session_factory() as db:
        assert len(db.scalars(select(Watch)).all()) == 1
        assert db.scalars(select(AuditLog).where(AuditLog.action == "watch.optout")).all() == []
    login(client, "a@example.com")
    assert client.get(f"/watch/{sid}").status_code == 200


def test_the_code_is_per_user_and_per_address(client, session_factory, info):
    signup(client, "a@example.com")
    code_a = issued_code(start(client))
    assert issued_code(start(client)) == code_a  # stable for one user + address
    assert issued_code(start(client, address="0x" + "cd" * 20)) != code_a
    signup(client, "b@example.com")
    assert issued_code(start(client)) != code_a
    # A's signature over A's code does not opt the address out for B's attempt
    # (B's server-issued code differs), though it is a valid signature.
    resp = verify(client, signature=sign(WALLET, claim_message(ADDR, code_a)))
    assert resp.status_code == 422
    assert optouts(session_factory) == []


def test_a_busy_venue_says_busy_and_changes_nothing(client, session_factory, info):
    signup(client, "a@example.com")
    watched_id(add(client, ADDR))
    signup(client, "owner@example.com")
    code = issued_code(start(client))
    drained = VenueLimiter(
        MemoryBuckets(),
        budgets_for=lambda venue: (RateBudget(RateScope.IP, 1, 1),),  # 1 token a minute
    )
    drained.acquire("hyperliquid", None, "info")
    client.app.state.venue_limiter = drained
    calls = info.calls

    # An unrelated wallet, so the connector would have to ask the venue.
    resp = verify(client, signature=sign(OTHER_WALLET, claim_message(ADDR, code)))
    assert resp.status_code == 503 and "Hyperliquid is busy" in resp.text
    assert "does not match" not in resp.text  # busy, not "failed"
    assert int(resp.headers["Retry-After"]) >= 1
    assert info.calls == calls  # nothing was sent
    assert optouts(session_factory) == []
    with session_factory() as db:
        assert len(db.scalars(select(Watch)).all()) == 1

    # The same proof goes through once the limiter has room.
    client.app.state.venue_limiter = VenueLimiter(MemoryBuckets(), budgets_for=lambda venue: ())
    assert "Opted out." in verify(client, signature=sign(WALLET, claim_message(ADDR, code))).text


def test_opting_out_twice_is_a_no_op(client, session_factory, info):
    signup(client, "owner@example.com")
    code = issued_code(start(client))
    assert "Opted out." in verify(client, signature=sign(WALLET, claim_message(ADDR, code))).text
    assert "Already opted out." in start(client).text  # no proof asked
    assert "Already opted out." in verify(client).text  # no signature, no venue call
    assert len(optouts(session_factory)) == 1
    with session_factory() as db:
        assert len(db.scalars(select(AuditLog).where(AuditLog.action == "watch.optout")).all()) == 1


def test_an_address_nobody_watches_can_still_opt_out(client, session_factory, info):
    signup(client, "owner@example.com")
    code = issued_code(start(client))
    done = verify(client, signature=sign(WALLET, claim_message(ADDR, code)))
    assert "0 watchlists" in done.text and "0 paper tails" in done.text
    assert len(optouts(session_factory)) == 1
    signup(client, "a@example.com")
    assert add(client, ADDR).status_code == 422


# ---- Polymarket: bio code (ships although Polymarket watches are closed) ----------------


@respx.mock
def test_polymarket_optout_with_the_code_in_the_bio(client, session_factory):
    signup(client, "owner@example.com")
    proof = start(client, "polymarket", POLY_ADDRESS)
    code = issued_code(proof)
    assert "bio" in proof.text and 'name="signature"' not in proof.text
    address = POLY_ADDRESS.lower()

    # The code is not in the bio yet.
    respx.get(f"{POLY_GAMMA}/public-profile").mock(
        return_value=httpx.Response(200, json={"bio": "getting adversely selected"})
    )
    resp = verify(client, "polymarket", POLY_ADDRESS)
    assert resp.status_code == 422 and f"Code {code} was not found" in resp.text
    assert optouts(session_factory) == []

    respx.get(f"{POLY_GAMMA}/public-profile").mock(
        return_value=httpx.Response(200, json={"bio": f"hello {code} bye"})
    )
    done = verify(client, "polymarket", POLY_ADDRESS)
    assert done.status_code == 200 and "Opted out." in done.text
    [row] = optouts(session_factory)
    assert (row.venue, row.address) == ("polymarket", address)


@respx.mock
def test_polymarket_profile_unreachable_is_not_a_proof(client, session_factory):
    signup(client, "owner@example.com")
    start(client, "polymarket", POLY_ADDRESS)
    respx.get(f"{POLY_GAMMA}/public-profile").mock(return_value=httpx.Response(500))
    resp = verify(client, "polymarket", POLY_ADDRESS)
    assert resp.status_code == 502 and "Could not reach Polymarket" in resp.text
    assert optouts(session_factory) == []


# ---- gates -------------------------------------------------------------------------------


def test_an_unverified_email_is_refused(client, session_factory, info):
    signup(client, "a@example.com", verified=False)
    page = client.get("/watch/optout")
    assert page.status_code == 200 and "Verify your email address first" in page.text
    assert 'name="address"' not in page.text
    resp = start(client)
    assert resp.status_code == 403 and "Verify your email" in resp.text
    resp = verify(client, signature=sign(WALLET, "anything"))
    assert resp.status_code == 403 and "Verify your email" in resp.text
    assert optouts(session_factory) == []
    assert info.calls == 0


def test_login_is_required_and_the_form_validates(client, session_factory, info):
    resp = client.get("/watch/optout", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/login")
    for path in ("/watch/optout", "/watch/optout/verify"):
        resp = client.post(
            path, data={"venue": "hyperliquid", "address": ADDR}, follow_redirects=False
        )
        assert resp.status_code == 303 and resp.headers["location"].startswith("/login")
    signup(client, "a@example.com")
    page = client.get("/watch/optout")
    assert page.status_code == 200 and "Ask not to be watched." in page.text
    assert "Cache-Control" in page.headers and "no-store" in page.headers["Cache-Control"]
    assert start(client, address="0x1234").status_code == 422
    assert start(client, venue="kalshi").status_code == 422
    assert optouts(session_factory) == []


def test_optout_posts_are_rate_limited(client, session_factory, info):
    from alphamarket.ratelimit import RULES

    signup(client, "a@example.com")
    limit = RULES["watch_optout_user"].limit
    for _ in range(limit):
        assert start(client).status_code == 200
    assert start(client).status_code == 429
    assert verify(client).status_code == 429


# ---- the record and the links ------------------------------------------------------------


def test_the_optout_row_is_append_only(client, session_factory, info):
    signup(client, "owner@example.com")
    code = issued_code(start(client))
    verify(client, signature=sign(WALLET, claim_message(ADDR, code)))
    with session_factory() as db:
        row = db.scalars(select(WatchOptout)).one()
        row.address = "0x" + "00" * 20
        with pytest.raises(AppendOnlyViolation):
            db.flush()
        db.rollback()
    with session_factory() as db:
        row = db.scalars(select(WatchOptout)).one()
        db.delete(row)
        with pytest.raises(AppendOnlyViolation):
            db.flush()


def test_the_footer_and_the_security_page_link_to_the_optout(client):
    for path in ("/", "/marketplace", "/security"):
        footer = client.get(path).text.split("<footer")[1]
        assert 'href="/watch/optout"' in footer, path
    assert 'href="/watch/optout"' in client.get("/security").text.split("<footer")[0]  # the page

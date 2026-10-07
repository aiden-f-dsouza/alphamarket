"""Private watchlist (watchlist.py, watch_pages.py): adding a public
Hyperliquid address through the real connector (respx, recorded public
fixtures; invariant 6), sharing one tracked account between watchers, the
private page and tail, the limits, and removal. Visibility on public surfaces
is pinned separately in test_watch_visibility.py."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from alphamarket.config import get_settings
from alphamarket.connectors.hyperliquid import INFO_URL
from alphamarket.models import (
    Allocation,
    AllocationStatus,
    Job,
    Strategy,
    StrategyKind,
    User,
    VenueAccount,
    Watch,
    WatchOptout,
)
from authutil import verify_email

FIXTURES = Path(__file__).parent / "fixtures" / "hyperliquid"
ADDRESS = "0x1a13afd75418fd72dfa80da33cbca489e231caec"  # public leaderboard trader
PASSWORD = "hunter2hunter2"
BANNER = "Only you can see this page."


def fx(name: str):
    return json.loads((FIXTURES / f"{name}_2026-09-26.json").read_text())


class FakeInfo:
    """POST /info by request `type`, from the recorded fixtures; anything not
    recorded answers an empty list (no fills, no ledger updates)."""

    def __init__(self) -> None:
        self.answers = {
            "userRole": fx("userRole_user"),
            "clearinghouseState": fx("clearinghouseState"),
            "spotClearinghouseState": fx("spotClearinghouseState"),
            "meta": fx("meta"),
            "spotMeta": fx("spotMeta"),
            "perpDexs": fx("perpDexs"),
            "userAbstraction": fx("userAbstraction"),
            "allMids": fx("allMids"),
        }
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        body = json.loads(request.content)
        if body.get("dex") and body["type"] in ("meta", "clearinghouseState"):
            return httpx.Response(200, json=fx(f"{body['type']}_xyz"))
        return httpx.Response(200, json=self.answers.get(body["type"], []))


@pytest.fixture()
def info():
    with respx.mock(assert_all_called=False) as router:
        fake = FakeInfo()
        router.post(INFO_URL).mock(side_effect=fake)
        yield fake


def signup(client, email: str, *, verified: bool = True) -> None:
    resp = client.post(
        "/signup-form",
        data={"email": email, "password": PASSWORD, "display_name": "User " + email.split("@")[0]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    if verified:
        verify_email(client, email)


def login(client, email: str) -> None:
    client.post("/logout-form", follow_redirects=False)
    resp = client.post(
        "/login-form", data={"email": email, "password": PASSWORD}, follow_redirects=False
    )
    assert resp.status_code == 303


def add(client, address: str = ADDRESS, venue: str = "hyperliquid", label: str = ""):
    return client.post(
        "/watch",
        data={"venue": venue, "address": address, "label": label},
        follow_redirects=False,
    )


def watched_id(resp) -> int:
    assert resp.status_code == 303, resp.text
    return int(resp.headers["location"].split("/")[2].split("?")[0])


def test_add_creates_a_private_shared_account(client, session_factory, info):
    signup(client, "a@example.com")
    sid = watched_id(add(client, ADDRESS.upper().replace("0X", "0x"), label="Big perp guy"))
    with session_factory() as db:
        strategy = db.get(Strategy, sid)
        account = db.get(VenueAccount, strategy.venue_account_id)
        owner = db.get(User, strategy.user_id)
        assert strategy.kind == StrategyKind.WATCH and strategy.public is False
        assert strategy.slug == f"watch-{sid}" and ADDRESS not in strategy.slug
        assert account.is_watch and account.key_id == ADDRESS and account.claimed_at is None
        assert account.claim_code is None and account.anchor_at is not None
        assert owner.is_system and owner.password_hash == "!"
        [job] = db.scalars(select(Job).where(Job.venue_account_id == account.id)).all()
        assert job.kind == "backfill"
        [watch] = db.scalars(select(Watch)).all()
        assert watch.label == "Big perp guy"

    page = client.get(f"/watch/{sid}")
    assert page.status_code == 200
    assert BANNER in page.text and "Big perp guy" in page.text and ADDRESS in page.text
    assert 'name="robots" content="noindex' in page.text
    assert "ownership proven" not in page.text  # nobody claimed the address
    assert page.headers["cache-control"] == "private, no-store"
    listing = client.get("/watch")
    assert "Big perp guy" in listing.text and f'href="/watch/{sid}"' in listing.text
    assert client.get(f"/s/watch-{sid}").status_code == 404


def test_two_watchers_share_one_account_and_one_ingestion(client, session_factory, info):
    signup(client, "a@example.com")
    first = watched_id(add(client))
    calls = info.calls
    signup(client, "b@example.com")
    second = watched_id(add(client, label="mine"))
    assert first == second
    assert info.calls == calls  # the second watcher made no venue call
    with session_factory() as db:
        assert len(db.scalars(select(VenueAccount).where(VenueAccount.is_watch)).all()) == 1
        assert len(db.scalars(select(Watch)).all()) == 2
        assert len(db.scalars(select(Job)).all()) == 1


def test_a_watch_page_is_404_for_everyone_else(client, session_factory, info):
    signup(client, "a@example.com")
    sid = watched_id(add(client, label="secret label"))
    signup(client, "b@example.com")
    for path in (f"/watch/{sid}", f"/watch/{sid}/tail"):
        assert client.get(path).status_code == 404, path
    assert client.post(f"/watch/{sid}/remove").status_code == 404
    assert client.post(f"/watch/{sid}/label", data={"label": "x"}).status_code == 404
    assert "secret label" not in client.get("/watch").text
    client.post("/logout-form", follow_redirects=False)
    assert client.get(f"/watch/{sid}").status_code == 404
    resp = client.get("/watch", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/login")


def test_refusals(client, session_factory, info, monkeypatch):
    signup(client, "u@example.com", verified=False)
    resp = add(client)
    assert resp.status_code == 403 and "Verify your email" in resp.text
    verify_email(client, "u@example.com")

    assert add(client, "0x1234").status_code == 422
    resp = add(client, venue="polymarket")  # closed until counsel answers
    assert resp.status_code == 422 and "Only Hyperliquid" in resp.text
    assert add(client, venue="kalshi").status_code == 422

    with session_factory() as db:
        other = User(email="owner@example.com", password_hash="x", display_name="Owner")
        db.add(other)
        db.flush()
        optout = "0x" + "ab" * 20
        db.add(WatchOptout(venue="hyperliquid", address=optout, proved_by_user_id=other.id))
        db.commit()
    resp = add(client, optout)
    assert resp.status_code == 422 and "asked not to be tracked" in resp.text

    monkeypatch.setattr(get_settings(), "watch_max_per_user", 1)
    watched_id(add(client))
    resp = add(client, "0x" + "cd" * 20)
    assert resp.status_code == 422 and "up to 1 addresses" in resp.text
    assert add(client).status_code == 409  # already on the list
    assert info.calls > 0


def test_global_cap(client, info, monkeypatch):
    monkeypatch.setattr(get_settings(), "watch_max_tracked", 0)
    signup(client, "u@example.com")
    assert add(client).status_code == 503
    assert info.calls == 0


def test_a_listed_creator_address_links_to_its_public_page(client, session_factory, info):
    with session_factory() as db:
        creator = User(email="c@example.com", password_hash="x", display_name="Creator")
        db.add(creator)
        db.flush()
        account = VenueAccount(
            user_id=creator.id,
            venue="hyperliquid",
            venue_env="prod",
            key_id=ADDRESS,
            claimed_at=datetime.now(UTC),
        )
        db.add(account)
        db.flush()
        db.add(Strategy(user_id=creator.id, venue_account_id=account.id, name="Pub", slug="pub-1"))
        db.commit()
    signup(client, "u@example.com")
    resp = add(client)
    assert resp.status_code == 409 and 'href="/s/pub-1"' in resp.text
    assert info.calls == 0


def test_paper_tail_of_a_watch_then_remove(client, session_factory, info):
    signup(client, "a@example.com")
    sid = watched_id(add(client, label="Perp whale"))
    page = client.get(f"/watch/{sid}/tail")
    assert page.status_code == 200 and BANNER in page.text
    assert f'action="/watch/{sid}/tail"' in page.text
    resp = client.post(
        f"/watch/{sid}/tail",
        data={"allocation": "2000", "drawdown_limit_bps": "1500", "confirm": "on"},
        follow_redirects=False,
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/portfolio?notice=started"
    portfolio = client.get("/portfolio").text
    assert f'href="/watch/{sid}"' in portfolio and "Perp whale" in portfolio
    assert "Private watch" in portfolio and "Koine watchlist" not in portfolio

    # Another user cannot tail it through the JSON route either.
    signup(client, "b@example.com")
    resp = client.post("/allocations", json={"strategy_slug": f"watch-{sid}", "allocation": "500"})
    assert resp.status_code == 404

    login(client, "a@example.com")
    assert client.post(f"/watch/{sid}/remove", follow_redirects=False).status_code == 303
    with session_factory() as db:
        [alloc] = db.scalars(select(Allocation)).all()
        assert alloc.status == AllocationStatus.STOPPED
        assert db.scalars(select(Watch)).all() == []
    portfolio = client.get("/portfolio").text
    assert f'href="/watch/{sid}"' not in portfolio and "Perp whale" not in portfolio
    assert client.get(f"/watch/{sid}").status_code == 404


def test_json_api(client, info):
    signup(client, "a@example.com")
    resp = client.post("/api/watch", json={"venue": "hyperliquid", "address": ADDRESS})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["address"] == ADDRESS and body["slug"].startswith("watch-")
    sid = body["strategy_id"]
    resp = client.patch(f"/api/watch/{sid}", json={"label": "  two   words "})
    assert resp.json()["label"] == "two words"
    listed = client.get("/api/watch").json()
    assert [w["strategy_id"] for w in listed["watches"]] == [sid] and listed["limit"] == 10
    assert client.delete(f"/api/watch/{sid}").status_code == 204
    assert client.get("/api/watch").json()["watches"] == []
    assert client.delete(f"/api/watch/{sid}").status_code == 404

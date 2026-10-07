"""Workstream A: "Run by Koine" house strategies.

A house user (users.is_house) owns Koine-run accounts. The badge is a label on
every surface that shows a strategy; it never changes visibility, ranking or
the security rules (a house key that turns writable is suspended like anyone's).
"""

import importlib.util
import re
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from alphamarket import auth, scopecheck
from alphamarket.config import get_settings
from alphamarket.models import AuditLog, Follow, Strategy, User, VenueAccount
from alphamarket.public import list_visible, operator_of
from test_pages_v2 import _asset_signal, _stats, _strategy, _user, v2_extras
from test_scopecheck import BASE, _account
from test_tail_ui import _signup, _start

BADGE = "Run by Koine"

_spec = importlib.util.spec_from_file_location(
    "create_house_user", Path(__file__).parent.parent / "scripts" / "create_house_user.py"
)
create_house_user = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(create_house_user)


def _listed(db, slug, *, house: bool, total_return: float, venue="kraken") -> Strategy:
    """A public Kraken record with v2 stats (100 verified days: past incubation)."""
    owner = _user(db, f"{slug}@example.com", name="Koine" if house else f"Creator {slug}")
    owner.is_house = house
    strategy = _strategy(db, venue, slug, f"Strategy {slug}", user=owner)
    _stats(db, strategy, v2_extras(), total_return=total_return)
    db.commit()
    return strategy


@pytest.fixture()
def pair(session_factory):
    """A house strategy and a creator strategy; the creator one ranks higher."""
    with session_factory() as db:
        _listed(db, "house-eth", house=True, total_return=0.10)
        _listed(db, "crea-eth", house=False, total_return=0.20)


@pytest.fixture()
def viewer(client, session_factory) -> int:
    _signup(client)
    with session_factory() as db:
        return db.scalar(select(User).where(User.email == "inv@example.com")).id


@pytest.fixture()
def demo_mode():
    settings = get_settings()
    settings.demo_mode = True
    yield
    settings.demo_mode = False


# ---- operator_of and the payloads -------------------------------------------------------------


def test_operator_of(session_factory):
    assert operator_of(None) == "creator"
    assert operator_of(User(email="a", password_hash="x", display_name="A")) == "creator"
    assert operator_of(User(email="b", password_hash="x", display_name="B", is_house=True)) == (
        "koine"
    )


def test_json_api_and_rows_carry_the_operator(client, session_factory, pair):
    assert client.get("/api/strategies/house-eth").json()["operator"] == "koine"
    assert client.get("/api/strategies/crea-eth").json()["operator"] == "creator"
    with session_factory() as db:
        assert {r["slug"]: r["operator"] for r in list_visible(db)} == {
            "house-eth": "koine",
            "crea-eth": "creator",
        }


# ---- the badge on every surface ---------------------------------------------------------------


def test_badge_on_marketplace_cards_and_table(client, pair):
    cards = client.get("/marketplace").text
    assert cards.count(BADGE) == 1  # the house card only
    assert 'class="operator-badge"' in cards
    table = client.get("/marketplace?view=table").text
    assert table.count(BADGE) == 1
    # the chips are controls, not badges
    assert "Only Koine" in cards and "Exclude Koine" in cards


def test_badge_on_the_landing_featured_card(client, session_factory):
    with session_factory() as db:
        _listed(db, "house-eth", house=True, total_return=0.10)
    assert BADGE in client.get("/").text
    with session_factory() as db:
        db.get(User, db.scalar(select(Strategy.user_id))).is_house = False
        db.commit()
    assert BADGE not in client.get("/").text


def test_strategy_page_byline_and_badge(client, pair):
    house = client.get("/s/house-eth").text
    assert house.count('class="operator-badge"') == 1
    assert 'href="/verification#run-by-koine"' in house
    assert ">Run by Koine</a>" in house  # the byline replaces "by <creator>"
    assert ">by Koine<" not in house
    creator = client.get("/s/crea-eth").text
    assert BADGE not in creator and "by Creator crea-eth" in creator


def test_tail_page_badge(client, pair, viewer):
    assert BADGE in client.get("/s/house-eth/tail").text
    assert BADGE not in client.get("/s/crea-eth/tail").text


def test_portfolio_row_badge_and_api(client, session_factory, pair, viewer):
    assert _start(client, slug="house-eth").status_code == 303
    assert _start(client, slug="crea-eth").status_code == 303
    html = client.get("/portfolio").text
    assert html.count(BADGE) == 1
    ops = {
        a["strategy_slug"]: a["operator"]
        for a in client.get("/api/portfolio").json()["allocations"]
    }
    assert ops == {"house-eth": "koine", "crea-eth": "creator"}


def test_feed_badge_and_api(client, session_factory, pair, viewer, demo_mode):
    with session_factory() as db:
        for slug in ("house-eth", "crea-eth"):
            strategy = db.scalar(select(Strategy).where(Strategy.slug == slug))
            db.add(Follow(user_id=viewer, strategy_id=strategy.id))
        # Instrument/fill ids are unique per venue account: one signal each.
        _asset_signal(db, db.scalar(select(Strategy).where(Strategy.slug == "house-eth")))
        db.commit()
    items = client.get("/api/feed").json()["items"]
    assert items and {i["operator"] for i in items} == {"koine"}
    # the HTML feed is the demo community page: real posts need a seeded database
    with session_factory() as db:
        owner = _user(db, "seeded@example.com", name="Seeded")
        owner.is_demo = True
        _strategy(db, "kraken", "seeded-eth", user=owner)
        db.commit()
    html = client.get("/feed").text
    assert html.count(BADGE) == 1


# ---- no ranking advantage ---------------------------------------------------------------------


def _order(client, query=""):
    html = client.get(f"/marketplace?view=table{query}").text
    return re.findall(r'href="/s/([a-z-]+)"', html)


def test_marketplace_order_ignores_is_house(client, session_factory, pair):
    with session_factory() as db:
        before = [r["slug"] for r in list_visible(db)]
    page_before = _order(client)
    assert before == ["crea-eth", "house-eth"]
    # Flip: the house strategy now has the lower return; swap the flag as well.
    with session_factory() as db:
        for slug, house in (("house-eth", False), ("crea-eth", True)):
            s = db.scalar(select(Strategy).where(Strategy.slug == slug))
            db.get(User, s.user_id).is_house = house
        db.commit()
        after = [r["slug"] for r in list_visible(db)]
    assert after == before  # same data, flag moved: same order
    assert _order(client) == page_before


def test_operator_filter(client, session_factory, pair):
    with session_factory() as db:
        assert [r["slug"] for r in list_visible(db, operator="koine")] == ["house-eth"]
        assert [r["slug"] for r in list_visible(db, operator="creator")] == ["crea-eth"]
        assert len(list_visible(db, operator="other")) == 2
        assert len(list_visible(db, operator=None)) == 2
    assert _order(client, "&operator=koine") == ["house-eth"]
    assert _order(client, "&operator=creator") == ["crea-eth"]
    assert _order(client, "&operator=nonsense") == ["crea-eth", "house-eth"]
    page = client.get("/marketplace?operator=koine").text
    assert 'class="chip on" href="/marketplace?operator=koine"' in page
    # other controls keep the filter
    assert "operator=koine" in page and 'name="operator" value="koine"' in page


# ---- security: a house key that turns writable is suspended ----------------------------------


@respx.mock
def test_house_account_with_a_writable_key_is_suspended(session_factory):
    respx.get(f"{BASE}/api_keys").mock(
        return_value=httpx.Response(
            200, json={"api_keys": [{"api_key_id": "demo-key-id", "scopes": ["read", "write"]}]}
        )
    )
    with session_factory() as db:
        account = _account(db)
        db.get(User, account.user_id).is_house = True
        db.commit()
        evidence = scopecheck.recheck_account(db, account)
        assert evidence.read_only is False
        assert account.status == "suspended"
        actions = list(db.scalars(select(AuditLog.action).order_by(AuditLog.id)).all())
        assert actions[-3:] == ["scope.check", "credential.disconnect", "account.suspend"]
        assert db.get(VenueAccount, account.id).status == "suspended"


# ---- scripts/create_house_user.py -------------------------------------------------------------


def test_create_house_user_is_no_shortcut(session_factory):
    with session_factory() as db:
        make = create_house_user.create_house_user
        user = make(db, "Ops@Koine.example", "Koine", "a-long-pass-1")
        assert user.is_house and user.email == "ops@koine.example"
        # not verified, no TOTP, not a demo or system user: connecting still needs all of it
        assert user.email_verified_at is None
        assert not user.is_demo and not user.is_system
        assert auth.verify_password("a-long-pass-1", user.password_hash)
        with pytest.raises(ValueError, match="already registered"):
            make(db, "ops@koine.example", "Koine", "a-long-pass-1")
        with pytest.raises(ValueError, match="at least"):
            make(db, "x@koine.example", "Koine", "short")
        assert db.scalars(select(AuditLog.action)).all() == ["house.user_created"]

"""Invariant 7 for seeded demo data (w3-seed): rows owned by an `is_demo`
user are public only while DEMO_MODE is on, always under the "Sample data"
banner; with them in the database the static demo.py padding steps aside;
preflight refuses a database that holds any."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from alphamarket import demo
from alphamarket.config import get_settings
from alphamarket.db import Base
from alphamarket.follows import feed_items
from alphamarket.models import (
    Follow,
    Settlement,
    Strategy,
    StrategyStats,
    User,
    VenueAccount,
)
from alphamarket.public import list_visible, recent_public_settlements

_spec = importlib.util.spec_from_file_location(
    "preflight", Path(__file__).parent.parent / "scripts" / "preflight.py"
)
preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preflight)

M = 1_000_000
NOW = datetime.now(UTC)
BANNER = "Sample data"


@pytest.fixture()
def demo_on():
    settings = get_settings()
    settings.demo_mode = True
    yield
    settings.demo_mode = False


def _record(db, *, slug: str, name: str, is_demo: bool) -> Strategy:
    """A listed Kalshi record with 120 verified days of stats and one settlement."""
    user = User(email=f"{slug}@example.com", password_hash="x", display_name=slug, is_demo=is_demo)
    db.add(user)
    db.flush()
    anchor = NOW - timedelta(days=120)
    account = VenueAccount(
        user_id=user.id,
        venue="kalshi",
        venue_env="prod",
        key_id=f"k-{slug}",
        anchor_balance_micros=1_000 * M,
        anchor_at=anchor,
        claimed_at=anchor,
    )
    db.add(account)
    db.flush()
    strategy = Strategy(
        user_id=user.id, venue_account_id=account.id, name=name, slug=slug, style_tags="weather"
    )
    db.add(strategy)
    db.flush()
    days = [(anchor + timedelta(days=n)).date().isoformat() for n in range(120)]
    db.add(
        StrategyStats(
            strategy_id=strategy.id,
            as_of=NOW,
            track_days=120,
            n_fills=4,
            n_settlements=1,
            total_return=0.12,
            max_drawdown=0.05,
            risk_score=3,
            equity_micros=1_120 * M,
            realized_pnl_micros=120 * M,
            flags=[],
            extras={"daily_equity": [[d, (1_000 + n) * M] for n, d in enumerate(days)]},
        )
    )
    db.add(
        Settlement(
            strategy_id=strategy.id,
            venue_settlement_id=f"set-{slug}",
            ticker=f"KXHIGHNY-{slug.upper()}",
            market_result="yes",
            yes_count_micros=10 * M,
            revenue_micros=10 * M,
            settled_at=NOW - timedelta(days=1),
        )
    )
    db.commit()
    return strategy


@pytest.fixture()
def records(session_factory):
    with session_factory() as db:
        real = _record(db, slug="real-weather", name="Real weather book", is_demo=False)
        seeded = _record(db, slug="seeded-weather", name="Seeded weather book", is_demo=True)
        return real, seeded


def test_demo_rows_are_hidden_without_demo_mode(client, session_factory, records):
    real, seeded = records
    with session_factory() as db:
        assert [r["slug"] for r in list_visible(db)] == ["real-weather"]
        assert [s["strategy"] for s in recent_public_settlements(db)] == ["Real weather book"]
        items = feed_items(db, [db.get(Strategy, real.id), db.get(Strategy, seeded.id)])
        assert {i["strategy_slug"] for i in items} == {"real-weather"}
    assert client.get("/s/seeded-weather").status_code == 404
    assert client.get("/api/strategies/seeded-weather").status_code == 404
    assert client.get("/api/strategies/seeded-weather/signals").status_code == 404
    resp = client.get("/s/real-weather")
    assert resp.status_code == 200 and BANNER not in resp.text
    assert client.get("/api/strategies/real-weather").json()["sample_data"] is False
    for path in ("/", "/marketplace", "/transparency"):
        html = client.get(path).text
        assert "Seeded weather book" not in html and BANNER not in html, path


def test_demo_rows_show_under_the_banner_in_demo_mode(client, session_factory, records, demo_on):
    with session_factory() as db:
        rows = {r["slug"]: r for r in list_visible(db)}
    assert rows["seeded-weather"]["demo"] is True and rows["real-weather"]["demo"] is False
    html = client.get("/s/seeded-weather").text
    assert "Seeded weather book" in html and BANNER in html
    assert client.get("/api/strategies/seeded-weather").json()["sample_data"] is True
    assert BANNER not in client.get("/s/real-weather").text  # a real record is never sample

    html = client.get("/marketplace").text
    assert "Seeded weather book" in html and BANNER in html
    # Seeded rows replace the static padding: nothing is listed twice.
    for static in demo.marketplace_rows():
        assert f'href="/s/{static["slug"]}"' not in html, static["slug"]
    html = client.get("/transparency").text
    assert BANNER in html
    assert BANNER in client.get("/").text


def test_empty_database_in_demo_mode_keeps_the_static_padding(client, demo_on):
    html = client.get("/marketplace").text
    static = demo.marketplace_rows()
    assert static and f'href="/s/{static[0]["slug"]}"' in html and BANNER in html


def test_a_demo_investors_portfolio_carries_the_banner(client, session_factory, demo_on):
    resp = client.post(
        "/signup-form",
        data={"email": "inv@example.com", "password": "hunter2hunter2", "display_name": "Inv"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert BANNER not in client.get("/portfolio").text  # a real user's own book
    with session_factory() as db:
        user = db.query(User).filter_by(email="inv@example.com").one()
        user.is_demo = True
        db.commit()
    assert BANNER in client.get("/portfolio").text


def test_preflight_refuses_a_database_with_demo_users():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    with engine.connect() as conn:
        assert preflight.demo_problems(conn) == []
    with sf() as db:
        db.add(User(email="a@example.com", password_hash="x", display_name="A"))
        db.commit()
    with engine.connect() as conn:
        assert preflight.demo_problems(conn) == []  # real users only: fine
    with sf() as db:
        db.add(User(email="d@example.com", password_hash="x", display_name="D", is_demo=True))
        db.commit()
    with engine.connect() as conn:
        [problem] = preflight.demo_problems(conn)
    assert "1 demo user" in problem
    engine.dispose()


def test_seeded_feed_and_creator_links(client, session_factory, records, demo_on):
    """A logged-in visitor of a seeded demo sees the real fills and settlements
    of what they follow on /feed; a seeded creator's handle leads to their page."""
    _, seeded = records
    client.post(
        "/signup-form",
        data={"email": "fan@example.com", "password": "hunter2hunter2", "display_name": "Fan"},
        follow_redirects=False,
    )
    with session_factory() as db:
        fan = db.query(User).filter_by(email="fan@example.com").one()
        db.add(Follow(user_id=fan.id, strategy_id=seeded.id))
        db.commit()
    html = client.get("/feed").text
    assert "Seeded weather book: KXHIGHNY-SEEDED-WEATHER resolved YES" in html
    assert "+12.0% · −5.0% DD verified" in html and BANNER in html
    resp = client.get("/u/seeded-weather", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/s/seeded-weather"
    assert client.get("/u/real-weather").status_code == 404  # only demo users' handles

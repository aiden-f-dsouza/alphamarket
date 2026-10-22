# ruff: noqa: F811
"""The real /feed (DEMO_MODE off): per-user, platform numbers only."""

from sqlalchemy import select

from alphamarket.models import Allocation, Follow, StrategyKind, User
from test_pages_v2 import _asset_signal, _stats, _strategy, v2_extras
from test_tail_ui import _signup, demo_mode, eth, viewer  # noqa: F401


def _uid(session_factory) -> int:
    with session_factory() as db:
        return db.scalar(select(User).where(User.email == "inv@example.com")).id


def _tail(session_factory, uid, strategy_id):
    with session_factory() as db:
        db.add(
            Allocation(
                user_id=uid,
                strategy_id=strategy_id,
                allocation_micros=1_000_000,
                drawdown_limit_bps=1500,
                high_water_micros=1_000_000,
            )
        )
        db.commit()


def _with_signal(session_factory, slug, name):
    with session_factory() as db:
        s = _strategy(db, "kraken", slug, name)
        _stats(db, s, v2_extras())
        _asset_signal(db, s)
        db.commit()
        return s.id


def test_logged_out_redirects_to_login(client):
    resp = client.get("/feed", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login?next=/feed"


def test_empty_state_links_to_the_marketplace(client, viewer):
    resp = client.get("/feed")
    assert resp.status_code == 200
    assert "not following a strategy yet" in resp.text and 'href="/marketplace"' in resp.text
    assert "noindex" in resp.text
    assert resp.headers["cache-control"] == "private, no-store"
    assert "likes" not in resp.text.lower() and "Rooms" not in resp.text


def test_followed_strategy_without_trades_is_listed(client, eth, viewer):
    client.post(f"/s/{eth}/follow")
    html = client.get("/feed").text
    assert "No trades have been recorded since you started following" in html
    assert 'href="/s/eth-swing"' in html and 'action="/s/eth-swing/unfollow"' in html


def test_followed_trades_show_with_links(client, session_factory, viewer):
    _with_signal(session_factory, "seeded", "Seeded ETH")
    client.post("/s/seeded/follow")
    html = client.get("/feed").text
    assert "Seeded ETH: buy 0.5 ETH/USD at 3,100.5" in html
    assert 'href="/s/seeded"' in html and "paper-tailing" not in html
    assert "See your feed" in client.get("/s/seeded").text


def test_tailed_but_not_followed_appears_with_marker(client, session_factory, viewer):
    sid = _with_signal(session_factory, "tailed", "Tailed ETH")
    _tail(session_factory, _uid(session_factory), sid)
    html = client.get("/feed").text
    assert "Tailed ETH: buy 0.5 ETH/USD" in html and "paper-tailing" in html


def test_private_watch_never_appears(client, session_factory, viewer):
    sid = _with_signal(session_factory, "watched", "Secret Whale")
    with session_factory() as db:
        from alphamarket.models import Strategy

        db.get(Strategy, sid).kind = StrategyKind.WATCH
        db.add(Follow(user_id=_uid(session_factory), strategy_id=sid))
        db.commit()
    _tail(session_factory, _uid(session_factory), sid)
    html = client.get("/feed").text
    assert "Secret Whale" not in html and "not following a strategy yet" in html


def test_nav_shows_feed_for_logged_in_user_only(client, eth):
    assert 'href="/feed"' not in client.get("/marketplace").text
    _signup(client)
    assert 'href="/feed"' in client.get("/marketplace").text


def test_demo_mode_feed_unchanged(client, demo_mode):
    html = client.get("/feed").text
    assert "Sample data" in html and "cache-control" not in client.get("/feed").headers

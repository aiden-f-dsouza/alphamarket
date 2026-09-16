"""Follow, feed, paper allocation and portfolio endpoints (M6)."""

from datetime import UTC, datetime, timedelta

import pytest

from alphamarket import signals
from alphamarket.models import Fill, Settlement, Strategy, User, VenueAccount
from alphamarket.money import to_micros

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
H = timedelta(hours=1)


@pytest.fixture()
def creator_strategy(session_factory):
    with session_factory() as db:
        user = User(email="creator@example.com", password_hash="x", display_name="Creator")
        db.add(user)
        db.flush()
        acct = VenueAccount(
            user_id=user.id,
            venue="kalshi",
            venue_env="prod",
            key_id="k",
            anchor_balance_micros=to_micros("1000.00"),
            anchor_at=T0,
            claimed_at=T0,
        )
        db.add(acct)
        db.flush()
        s = Strategy(user_id=user.id, venue_account_id=acct.id, name="Fed path", slug="fed-path")
        db.add(s)
        db.flush()
        db.add(
            Fill(
                strategy_id=s.id,
                venue_fill_id="a",
                ticker="KXFED-26SEP-C",
                side="no",
                action="buy",
                count_micros=20_000_000,
                price_micros=250_000,
                fee_micros=262_500,
                filled_at=T0 + H,
            )
        )
        db.add(
            Settlement(
                strategy_id=s.id,
                venue_settlement_id="KXOLD:1",
                ticker="KXOLD-1",
                market_result="yes",
                yes_count_micros=1_000_000,
                revenue_micros=1_000_000,
                settled_at=T0 + 2 * H,
            )
        )
        db.commit()
        signals.emit_signals(db, s)
        return s.slug


@pytest.fixture()
def follower(client):
    resp = client.post(
        "/auth/signup",
        json={"email": "f@example.com", "password": "hunter2hunter2", "display_name": "Fan"},
    )
    assert resp.status_code == 201
    return resp.json()


def test_public_signals_show_proportions_not_dollars(client, creator_strategy):
    body = client.get(f"/api/strategies/{creator_strategy}/signals").json()
    (sig,) = body["signals"]
    assert sig["ticker"] == "KXFED-26SEP-C" and sig["side"] == "no" and sig["action"] == "buy"
    # 20 × 0.25 = $5 of a $1000 book
    assert sig["stake_fraction"] == pytest.approx(0.005)
    assert client.get("/api/strategies/nope/signals").status_code == 404


def test_follow_unfollow_and_feed(client, creator_strategy, follower):
    assert client.get("/follows").json() == []
    assert client.get("/api/feed").json() == {"items": [], "n_following": 0}
    resp = client.post(f"/follows/{creator_strategy}")
    assert resp.status_code == 201 and resp.json()["strategy_slug"] == creator_strategy
    assert client.post(f"/follows/{creator_strategy}").status_code == 201  # idempotent
    assert len(client.get("/follows").json()) == 1
    feed = client.get("/api/feed").json()
    assert feed["n_following"] == 1
    assert [i["kind"] for i in feed["items"]] == ["settlement", "fill"]  # newest first
    assert client.delete(f"/follows/{creator_strategy}").status_code == 204
    assert client.get("/follows").json() == []
    assert client.post("/follows/nope").status_code == 404


def test_endpoints_need_login(client, creator_strategy):
    assert client.get("/follows").status_code == 401
    assert client.get("/api/portfolio").status_code == 401
    body = {"strategy_slug": "x", "allocation": "1"}
    assert client.post("/allocations", json=body).status_code == 401


def test_paper_allocation_lifecycle(client, creator_strategy, follower):
    bad = client.post("/allocations", json={"strategy_slug": creator_strategy, "allocation": "50"})
    assert bad.status_code == 422
    live = client.post(
        "/allocations",
        json={"strategy_slug": creator_strategy, "allocation": "500", "mode": "live"},
    )
    assert live.status_code == 403 and "licensed" in live.json()["detail"]
    assert (
        client.post(
            "/allocations", json={"strategy_slug": creator_strategy, "allocation": "abc"}
        ).status_code
        == 422
    )

    resp = client.post(
        "/allocations",
        json={
            "strategy_slug": creator_strategy,
            "allocation": "500.00",
            "drawdown_limit_bps": 2000,
        },
    )
    assert resp.status_code == 201, resp.text
    a = resp.json()
    assert a["mode"] == "paper" and a["paper"] and a["status"] == "active"
    assert a["allocation"] == 500.0 and a["equity"] == 500.0 and a["drawdown_limit"] == 0.2
    assert len(a["curve"]) == 1  # the curve starts at the allocation
    # A second active tail of the same strategy is refused.
    assert (
        client.post(
            "/allocations", json={"strategy_slug": creator_strategy, "allocation": "500.00"}
        ).status_code
        == 409
    )

    # Signals emitted before the allocation started are not replayed.
    run = client.post(f"/allocations/{a['id']}/run").json()
    assert run["mirrored"] == 0 and run["n_fills"] == 0

    p = client.get("/api/portfolio").json()
    assert p["paper"] and p["n_active"] == 1 and p["allocated"] == 500.0 and p["value"] == 500.0
    assert client.get("/allocations/999").status_code == 404

    stopped = client.post(f"/allocations/{a['id']}/stop").json()
    assert stopped["status"] == "stopped" and stopped["stop_reason"] == "user"
    assert client.get("/api/portfolio").json()["n_active"] == 0
    # The feed includes tailed strategies even without an explicit follow.
    assert client.get("/api/feed").json()["n_following"] == 1


def test_creator_cannot_tail_own_strategy(client, creator_strategy, session_factory):
    resp = client.post(
        "/auth/signup",
        json={"email": "creator@example.com", "password": "hunter2hunter2", "display_name": "Cr"},
    )
    assert resp.status_code == 409  # already exists as a bare row; log in instead
    with session_factory() as db:
        from alphamarket.auth import hash_password

        u = db.query(User).filter_by(email="creator@example.com").one()
        u.password_hash = hash_password("hunter2hunter2")
        db.commit()
    assert (
        client.post(
            "/auth/login", json={"email": "creator@example.com", "password": "hunter2hunter2"}
        ).status_code
        == 200
    )
    resp = client.post(
        "/allocations", json={"strategy_slug": creator_strategy, "allocation": "500"}
    )
    assert resp.status_code == 422 and "own" in resp.json()["detail"]

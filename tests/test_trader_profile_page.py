"""The "How this trader trades" section of /watch/{id} (copy strategies B5):
the DB bridge (trader_profile_db) for a Hyperliquid and a Polymarket watch
account, the section on the owner's page only, the empty state, the private
help page, and the static demo's sample built by the real functions."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from alphamarket import demo
from alphamarket.config import get_settings
from alphamarket.connectors.base import InstrumentType
from alphamarket.models import (
    AssetFill,
    Fill,
    Instrument,
    LedgerEntry,
    Settlement,
    Strategy,
    StrategyKind,
    StrategyStats,
    StrategyStatus,
    User,
    VenueAccount,
    Watch,
)
from alphamarket.trader_profile_db import trader_profile, trader_profile_inputs
from authutil import verify_email

PASSWORD = "hunter2hunter2"
ADDRESS = "0x1a13afd75418fd72dfa80da33cbca489e231caec"
HEADING = "How this trader trades"
T0 = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)  # a Monday
M = 1_000_000


def signup(client, email: str) -> None:
    resp = client.post(
        "/signup-form",
        data={"email": email, "password": PASSWORD, "display_name": "User " + email.split("@")[0]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    verify_email(client, email)


def make_watch(db, user: User | None, venue: str = "hyperliquid", n: int = 0) -> Strategy:
    system = db.scalar(select(User).where(User.is_system.is_(True)))
    if system is None:
        system = User(
            email="system@koine.invalid", password_hash="!", display_name="K", is_system=True
        )
        db.add(system)
        db.flush()
    account = VenueAccount(
        user_id=system.id,
        venue=venue,
        venue_env="prod",
        key_id=ADDRESS[:-4] + f"{n:04x}",
        anchor_balance_micros=0,
        anchor_at=T0 - timedelta(days=30),
        is_watch=True,
    )
    db.add(account)
    db.flush()
    strategy = Strategy(
        user_id=system.id,
        venue_account_id=account.id,
        name="Hyperliquid 0x1a13…caec",
        slug=f"watch-{account.id}",
        kind=StrategyKind.WATCH,
        public=False,
        status=StrategyStatus.LIVE,
    )
    db.add(strategy)
    db.flush()
    if user is not None:
        db.add(Watch(user_id=user.id, strategy_id=strategy.id, label="Desk"))
    db.commit()
    return strategy


def add_perp_fills(db, strategy: Strategy, n: int) -> None:
    """n BTC-PERP round trips of 1 BTC (buy at 100, sell an hour later at 110)."""
    inst = Instrument(
        venue="hyperliquid",
        symbol="BTC",
        type=InstrumentType.PERP,
        base_asset="BTC-PERP",
        quote_asset="USDC",
        qty_decimals=5,
    )
    db.add(inst)
    db.flush()
    for i in range(n):
        at = T0 + timedelta(days=i)
        for k, (action, price) in enumerate((("buy", "100.5"), ("sell", "110"))):
            db.add(
                AssetFill(
                    venue_account_id=strategy.venue_account_id,
                    instrument_id=inst.id,
                    venue_fill_id=f"f{i}-{k}",
                    order_id=f"o{i}-{k}",
                    action=action,
                    qty_atoms=100_000,
                    qty_decimals=5,
                    price=price,
                    filled_at=at + timedelta(hours=k),
                )
            )
    db.commit()


def test_bridge_reads_asset_fills_funding_and_equity(session_factory):
    with session_factory() as db:
        strategy = make_watch(db, None)
        add_perp_fills(db, strategy, 2)
        account_id = strategy.venue_account_id
        for ref, kind, asset, atoms in (
            ("fund-1", "funding", "USDC", -1_500_000),  # paid 1.50
            ("fund-2", "funding", "USDC", 250_000),  # received 0.25
            ("fund-3", "funding", "BTC", 7),  # not cash: skipped
            ("fee-1", "fee", "USDC", -99),  # not funding: not read
        ):
            db.add(
                LedgerEntry(
                    venue_account_id=account_id,
                    asset=asset,
                    delta_atoms=atoms,
                    decimals=6,
                    kind=kind,
                    venue_ref=ref,
                    group_ref=ref,
                    occurred_at=T0 + timedelta(hours=3),
                )
            )
        db.add(
            StrategyStats(
                strategy_id=strategy.id,
                as_of=T0 + timedelta(days=5),
                track_days=5,
                n_fills=4,
                n_settlements=0,
                max_drawdown=0.0,
                risk_score=5,
                equity_micros=1_000 * M,
                realized_pnl_micros=0,
                extras={"daily_equity": [["2026-09-07", 1_000 * M], ["2026-09-08", 2_000 * M]]},
            )
        )
        db.commit()
        inputs = trader_profile_inputs(db, strategy)
        assert [f.instrument for f in inputs.fills] == ["BTC-PERP"] * 4
        assert inputs.fills[0].notional_micros == 100_500_000  # 1 x 100.5, exact
        assert inputs.fills[0].kind is InstrumentType.PERP
        assert [e.equity_micros for e in inputs.equity] == [1_000 * M, 2_000 * M]
        assert sorted(f.amount_micros for f in inputs.funding) == [-1_500_000, 250_000]
        assert inputs.copy_delay_seconds == get_settings().ingest_interval_seconds == 300

        p = trader_profile(db, strategy)
        assert p.n_fills == 4 and not p.enough_history
        # Funding paid = minus the signed sum: -(-1.50 + 0.25) = 1.25 USD.
        assert p.perps.funding_paid_micros == 1_250_000
        # Fill 3 (Sep 8) is the first with a prior day's equity (Sep 7: 1000);
        # fill 1 and 2 are on Sep 7 itself, before the series' first complete day.
        assert p.perps.max_leverage == pytest.approx(100.5 / 1000)
        assert p.holding.n_round_trips == 2 and p.holding.median_seconds == 3600


def test_bridge_without_funding_or_stats_is_none_not_zero(session_factory):
    with session_factory() as db:
        strategy = make_watch(db, None)
        add_perp_fills(db, strategy, 1)
        inputs = trader_profile_inputs(db, strategy)
        assert inputs.funding is None and inputs.equity == []
        assert trader_profile(db, strategy).perps.funding_paid_micros is None


def test_bridge_reads_polymarket_fills_and_settlements(session_factory):
    with session_factory() as db:
        strategy = make_watch(db, None, venue="polymarket")
        rows = [
            ("a", "buy", "yes", 10 * M, 150_000, 9),
            ("b", "buy", "no", 4 * M, 850_000, 10),
            ("c", "sell", "yes", 5 * M, 900_000, 11),
        ]
        for ref, action, side, count, price, hour in rows:
            db.add(
                Fill(
                    strategy_id=strategy.id,
                    venue_fill_id=ref,
                    ticker="MKT",
                    side=side,
                    action=action,
                    count_micros=count,
                    price_micros=price,
                    filled_at=T0.replace(hour=hour),
                )
            )
        db.add(
            Settlement(
                strategy_id=strategy.id,
                venue_settlement_id="s1",
                ticker="MKT",
                market_result="YES",
                revenue_micros=5 * M,
                settled_at=T0 + timedelta(days=1),
            )
        )
        db.commit()
        inputs = trader_profile_inputs(db, strategy)
        assert [f.price for f in inputs.fills] == ["0.15", "0.85", "0.9"]
        assert inputs.fills[0].notional_micros == 1_500_000 and inputs.fills[0].outcome == "yes"
        assert inputs.settlements[0].result == "yes"  # lower-cased
        b = trader_profile(db, strategy).binary
        by = {x.label: x for x in b.buckets}
        # 10 yes @ 0.15: 5 sold early, 5 held and won. 4 no @ 0.85 held, lost.
        assert (by["0-20"].held_share, by["0-20"].win_rate) == (0.5, 1.0)
        assert (by["80-100"].held_share, by["80-100"].win_rate) == (1.0, 0.0)
        assert b.held_to_settlement == pytest.approx(9 / 14)


def test_section_renders_for_the_owner_only(client, session_factory):
    signup(client, "a@example.com")
    with session_factory() as db:
        owner = db.scalar(select(User).where(User.email == "a@example.com"))
        strategy = make_watch(db, owner)
        add_perp_fills(db, strategy, 6)  # 12 fills >= the 10 the section needs
        sid, slug = strategy.id, strategy.slug
    page = client.get(f"/watch/{sid}")
    assert page.status_code == 200
    html = page.text
    assert HEADING in html and 'id="how-they-trade"' in html
    assert "Holding periods" in html and "When they trade (UTC)" in html
    assert "What they trade" in html and "BTC-PERP" in html and "Perpetual futures" in html
    assert "Copy feasibility" in html and 'href="/watch/help"' in html
    assert "Round trips shorter than the copy delay (5 min)" in html
    assert "0% of closed notional (0 of 6)" in html  # six one-hour round trips
    assert "cannot be copied" in html and "Within " not in html
    assert "Prediction markets: entry price" not in html  # no binary fills: hidden
    assert "<script" not in html.split('id="how-they-trade"')[1].split("</section>")[0]
    # Never on a public page, never for anyone else.
    assert client.get(f"/s/{slug}").status_code == 404
    client.post("/logout-form", follow_redirects=False)
    assert client.get(f"/watch/{sid}").status_code == 404
    signup(client, "b@example.com")
    other = client.get(f"/watch/{sid}")
    assert other.status_code == 404 and HEADING not in other.text


def test_short_history_shows_the_empty_state(client, session_factory):
    signup(client, "a@example.com")
    with session_factory() as db:
        owner = db.scalar(select(User).where(User.email == "a@example.com"))
        strategy = make_watch(db, owner)
        add_perp_fills(db, strategy, 2)  # 4 fills
        sid = strategy.id
    html = client.get(f"/watch/{sid}").text
    assert HEADING in html and "Not enough history yet" in html and "4 fills so far" in html
    assert "Holding periods" not in html and "When they trade" not in html
    # No fills at all.
    with session_factory() as db:
        owner = db.scalar(select(User).where(User.email == "a@example.com"))
        empty = make_watch(db, owner, n=1)
        eid = empty.id
    html = client.get(f"/watch/{eid}").text
    assert "No fills have been read yet" in html


def test_binary_watch_shows_the_price_bucket_table(client, session_factory):
    signup(client, "a@example.com")
    with session_factory() as db:
        owner = db.scalar(select(User).where(User.email == "a@example.com"))
        strategy = make_watch(db, owner, venue="polymarket")
        for i in range(12):
            db.add(
                Fill(
                    strategy_id=strategy.id,
                    venue_fill_id=f"f{i}",
                    ticker=f"M{i}",
                    side="yes",
                    action="buy",
                    count_micros=2 * M,
                    price_micros=300_000,
                    filled_at=T0 + timedelta(hours=i),
                )
            )
        db.commit()
        sid = strategy.id
    html = client.get(f"/watch/{sid}").text
    assert "Prediction markets: entry price" in html and "20-40" in html
    assert "Perpetual futures" not in html


def test_help_page_is_private(client):
    resp = client.get("/watch/help", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/login")
    signup(client, "a@example.com")
    resp = client.get("/watch/help")
    assert resp.status_code == 200
    assert "Holding periods" in resp.text and "Herfindahl" in resp.text
    assert 'name="robots" content="noindex' in resp.text
    assert resp.headers["cache-control"] == "private, no-store"
    assert 'aria-label="Learn"' not in resp.text  # not a Learn tab


@pytest.fixture()
def demo_on():
    settings = get_settings()
    settings.demo_mode = True
    yield
    settings.demo_mode = False


def test_demo_sample_page_renders_the_computed_profile(client, demo_on):
    p = demo.watch_sample_profile()
    assert p.enough_history and p.perps and p.holding and p.copy and p.sizing
    resp = client.get("/watch/sample-1")
    assert resp.status_code == 200
    html = resp.text
    assert "Sample data" in html and HEADING in html
    assert f"{p.n_fills} fills" in html
    assert "Holding periods" in html and "When they trade (UTC)" in html
    assert f"{p.holding.n_round_trips} closed round trip" in html
    c = p.copy
    assert c.n_too_fast >= 2
    assert f"({c.n_too_fast} of {c.n_round_trips})" in html
    assert "Prediction markets: entry price" not in html  # a perps sample
    assert 'href="#"' not in html
    assert client.get("/watch/help").status_code == 200  # the link target works in the demo


def test_demo_profile_is_deterministic_and_matches_the_sample_fills():
    a, b = demo.watch_sample_profile(), demo.watch_sample_profile()
    assert a == b
    assert a.n_fills == len(demo._WATCH_SAMPLE_FILLS)
    assert {i.instrument for i in a.instruments.top} == {"BTC-PERP", "ETH-PERP"}
    assert a.first_at.date() >= date(2026, 6, 1)


def test_public_sample_strategy_page_never_has_the_section(client, demo_on):
    resp = client.get(f"/s/{demo.WATCH_BASE_SLUG}")
    assert resp.status_code == 200 and HEADING not in resp.text

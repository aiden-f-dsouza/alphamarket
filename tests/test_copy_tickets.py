"""Copy tickets (copy_tickets.py, copy_pages.py; methodology "Copy tickets").

Every expected number is hand-computed in the comments (M = 1,000,000).
Koine never places a ticket: these tests only cover issuing, expiring,
matching the user's own real fills, the pages and the settings."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from alphamarket import copy_tickets, email, signals
from alphamarket.config import get_settings
from alphamarket.connectors.registry import get_profile
from alphamarket.jobs import JobContext
from alphamarket.mirror import process_allocation
from alphamarket.models import (
    Allocation,
    AssetFill,
    AuditLog,
    CopyTicket,
    CopyTicketStatus,
    Fill,
    Job,
    MirrorAssetFill,
    MirrorFill,
    Signal,
    Strategy,
    User,
    VenueAccount,
    VenueAccountStatus,
)
from alphamarket.money import to_atoms, to_micros
from test_paper_engine import START, T0, World, fills_of

M = 1_000_000
PASSWORD = "hunter2hunter2"


@pytest.fixture()
def db(session_factory):
    with session_factory() as session:
        yield session


@pytest.fixture()
def copy_on():
    settings = get_settings()
    before = (settings.copy_tickets_enabled, settings.copy_tickets_sized)
    settings.copy_tickets_enabled = True
    settings.copy_tickets_sized = True
    yield settings
    settings.copy_tickets_enabled, settings.copy_tickets_sized = before


@pytest.fixture()
def outbox():
    sender = email.ConsoleEmailSender()
    email.set_sender(sender)
    yield sender.outbox
    email.set_sender(None)


@pytest.fixture()
def kraken(db):
    w = CopyWorld(db, "kraken")
    w.btc = w.instrument("XXBTZUSD", "BTC", min_qty_atoms=10_000, min_notional="0.5")
    w.eth = w.instrument("XETHZUSD", "ETH", min_qty_atoms=10_000, min_notional="0.5")
    return w


def _follower(db, alloc) -> User:
    return db.get(User, alloc.user_id)


def _own_account(db, user, venue="kraken", **kw) -> VenueAccount:
    account = VenueAccount(user_id=user.id, venue=venue, venue_env="prod", key_id="own", **kw)
    db.add(account)
    db.commit()
    return account


class CopyWorld(World):
    """World whose asset signals carry notional_micros (qty x price), which
    the stake share needs; World.signal leaves it NULL."""

    def signal(self, inst, action, qty, price, **kw):
        notional = to_micros(str(Decimal(qty) * Decimal(price)))
        real_add = self.db.add

        def add(obj):
            if isinstance(obj, Signal):
                obj.notional_micros = notional
            real_add(obj)

        self.db.add = add
        try:
            return super().signal(inst, action, qty, price, **kw)
        finally:
            self.db.add = real_add


def _btc_buy(w, filled_at=T0, before="0", after="0.1", qty="0.1"):
    # Creator ($10,000) buys 0.1 BTC at 60,000 (notional $6,000); quote ask 60,030.
    return w.signal(
        w.btc,
        "buy",
        qty,
        "60000",
        before=before,
        after=after,
        bid="59990",
        ask="60030",
        filled_at=filled_at,
    )


def _tickets(db, alloc=None) -> list[CopyTicket]:
    q = select(CopyTicket).order_by(CopyTicket.id)
    if alloc is not None:
        q = q.where(CopyTicket.allocation_id == alloc.id)
    return list(db.scalars(q).all())


def _enable(db, alloc, now=START, email_on=False, account_id=None):
    return copy_tickets.set_copy_settings(
        db, _follower(db, alloc), alloc, True, email_on, account_id, now=now
    )


# ---- pure formulas -----------------------------------------------------------------------


def test_slippage_bps_hand_computed():
    # buy: (0.41 - 0.40) / 0.40 x 10,000 = 250 bps worse
    assert copy_tickets.slippage_bps("buy", "0.40", "0.41") == 250
    # buy cheaper than paper: (0.39 - 0.40) / 0.40 = -250 bps (better)
    assert copy_tickets.slippage_bps("buy", "0.40", "0.39") == -250
    # buy: (60,060.015 - 60,030) / 60,030 = 30.015 / 60,030 = 0.0005 -> 5 bps
    assert copy_tickets.slippage_bps("buy", "60030", "60060.015") == 5
    # sell: (100 - 99.5) / 100 = 50 bps worse; selling higher is better
    assert copy_tickets.slippage_bps("sell", "100", "99.5") == 50
    assert copy_tickets.slippage_bps("sell", "100", "101") == -100
    assert copy_tickets.slippage_bps("buy", "0", "1") is None


def test_median_and_summary():
    assert copy_tickets.median([Fraction(3), Fraction(1), Fraction(2)]) == 2
    assert copy_tickets.median([Fraction(1), Fraction(2), Fraction(3), Fraction(10)]) == Fraction(
        5, 2
    )
    assert copy_tickets.median([]) is None
    rows = [
        CopyTicket(action="buy", paper_price="0.40", matched_price="0.41", status="matched"),
        CopyTicket(action="sell", paper_price="100", matched_price="99.5", status="matched"),
        CopyTicket(action="buy", paper_price="1", status="skipped"),
        CopyTicket(action="buy", paper_price="1", status="open"),
    ]
    out = copy_tickets.summarize(rows)
    # matched slippages 250 and 50 -> median (250 + 50) / 2 = 150
    assert out["issued"] == 4 and out["matched"] == 2 and out["skipped"] == 1
    assert out["open"] == 1 and out["done"] == 0 and out["expired"] == 0
    assert out["median_slippage_bps"] == 150


def test_take_lots_sums_oldest_first_and_counts_only_what_is_needed():
    at = T0
    lots = [
        copy_tickets.Lot("a", Fraction("0.004"), Fraction(60000), at),
        copy_tickets.Lot("b", Fraction("0.004"), Fraction(60100), at),
        copy_tickets.Lot("c", Fraction("0.004"), Fraction(60200), at),
        copy_tickets.Lot("d", Fraction("0.004"), Fraction(1), at),
    ]
    taken = copy_tickets.take_lots(Fraction("0.01"), lots)
    # 0.004 + 0.004 + 0.002 of c (d untouched):
    # (0.004 x 60,000 + 0.004 x 60,100 + 0.002 x 60,200) / 0.01
    # = (240 + 240.4 + 120.4) / 0.01 = 60,080
    assert taken.refs == ("a", "b", "c")
    assert taken.qty == Fraction("0.01") and taken.vwap == 60080
    partial = copy_tickets.take_lots(Fraction(1), lots[:1])
    assert partial.qty == Fraction("0.004") and partial.vwap == 60000


def test_stake_bps():
    # binary: 10 x 0.40 = $4 of $1,000 equity = 0.4% = 40 bps
    s = Signal(fill_id=1, count_micros=10 * M, price_micros=400_000, creator_equity_micros=1000 * M)
    assert copy_tickets.stake_bps(s) == 40
    # asset: $6,000 of $10,000 = 6,000 bps
    a = Signal(asset_fill_id=1, notional_micros=6_000 * M, creator_equity_micros=10_000 * M)
    assert copy_tickets.stake_bps(a) == 6_000
    # $1 of $30,000 = 0.333 bps -> 0 (half-up); $5 of $20,000 = 2.5 -> 3
    assert (
        copy_tickets.stake_bps(
            Signal(asset_fill_id=1, notional_micros=M, creator_equity_micros=30_000 * M)
        )
        == 0
    )
    assert (
        copy_tickets.stake_bps(
            Signal(asset_fill_id=1, notional_micros=5 * M, creator_equity_micros=20_000 * M)
        )
        == 3
    )
    assert (
        copy_tickets.stake_bps(
            Signal(asset_fill_id=1, notional_micros=None, creator_equity_micros=M)
        )
        is None
    )
    assert (
        copy_tickets.stake_bps(Signal(asset_fill_id=1, notional_micros=M, creator_equity_micros=0))
        is None
    )


def test_place_urls_come_from_the_profile(db, kraken):
    url = copy_tickets.place_url(get_profile("kraken"), "XXBTZUSD", kraken.btc)
    assert url == "https://pro.kraken.com/app/trade/btc-usd"
    # Kalshi has no confirmed per-market page: the markets home
    assert copy_tickets.place_url(get_profile("kalshi"), "KX-1", None) == (
        "https://kalshi.com/markets"
    )
    # A template that needs base/quote without an instrument falls back too
    assert copy_tickets.place_url(get_profile("kraken"), "X", None) == (
        "https://pro.kraken.com/app/trade"
    )
    assert copy_tickets.place_url(get_profile("snaptrade"), "AAPL", None) is None


# ---- issuing: asset ------------------------------------------------------------------------


def test_asset_ticket_is_the_paper_fill(db, kraken, copy_on):
    alloc = kraken.allocation("1000")
    _enable(db, alloc)
    sig = _btc_buy(kraken)
    assert process_allocation(db, alloc) == (1, 0)
    [paper_fill] = fills_of(db, alloc)
    issued = copy_tickets.issue_tickets(db, alloc, T0 + timedelta(seconds=40))
    [t] = issued
    # Paper: 0.1 x 1,000 / 10,000 = 0.01 BTC at the ask 60,030
    assert (t.mirror_asset_fill_id, t.mirror_fill_id, t.signal_id) == (paper_fill.id, None, sig.id)
    assert (t.venue, t.ticker, t.label, t.side, t.action) == (
        "kraken",
        "XXBTZUSD",
        "BTC/USD",
        "long",
        "buy",
    )
    assert (t.qty_atoms, t.qty_decimals) == (1_000_000, 8)
    assert (t.paper_price, t.creator_price) == ("60030", "60000")
    assert t.stake_bps == 6_000  # $6,000 of the creator's $10,000
    assert t.status == "open" and t.user_id == alloc.user_id
    assert t.expires_at.replace(tzinfo=UTC) == T0 + timedelta(hours=1)  # creator fill + TTL
    # idempotent: a second pass issues nothing
    assert copy_tickets.issue_tickets(db, alloc, T0 + timedelta(seconds=50)) == []
    assert len(_tickets(db)) == 1


def test_already_expired_ticket_is_stored_as_expired(db, kraken, copy_on, outbox):
    alloc = kraken.allocation("1000")
    _enable(db, alloc, email_on=True)
    _btc_buy(kraken)
    process_allocation(db, alloc)
    now = T0 + timedelta(hours=2)
    [t] = copy_tickets.issue_tickets(db, alloc, now)
    assert t.status == "expired" and t.status_at.replace(tzinfo=UTC) == now
    # an expired ticket is history, not news: no email
    assert copy_tickets.notify_new_tickets(db, alloc, [t]) is False
    assert not outbox


def test_liquidations_and_stop_closes_issue_no_ticket(db, kraken, copy_on):
    alloc = kraken.allocation("1000")
    _enable(db, alloc)
    for reason in ("liquidation", "stop_close"):
        db.add(
            MirrorAssetFill(
                allocation_id=alloc.id,
                signal_id=None,
                reason=reason,
                mode="paper",
                instrument_id=kraken.btc.id,
                action="sell",
                qty_atoms=1_000,
                qty_decimals=8,
                price="60000",
                notional_micros=600_000,
                price_basis="mark",
                filled_at=T0,
            )
        )
    db.commit()
    assert copy_tickets.issue_tickets(db, alloc, T0) == []


def test_nothing_from_before_copy_enabled_at(db, kraken, copy_on):
    alloc = kraken.allocation("1000")
    _btc_buy(kraken)
    process_allocation(db, alloc)  # tickets off: this paper fill stays ticketless
    _enable(db, alloc, now=datetime.now(UTC))
    _btc_buy(kraken, filled_at=T0 + timedelta(minutes=1), before="0.1", after="0.2")
    process_allocation(db, alloc)
    first, second = fills_of(db, alloc)
    [t] = copy_tickets.issue_tickets(db, alloc, T0 + timedelta(minutes=2))
    assert t.mirror_asset_fill_id == second.id
    # off -> on again stamps a fresh copy_enabled_at
    stamped = alloc.copy_enabled_at
    copy_tickets.set_copy_settings(db, _follower(db, alloc), alloc, False, False, None)
    assert copy_tickets.issue_tickets(db, alloc, T0) == []  # off: nothing
    copy_tickets.set_copy_settings(db, _follower(db, alloc), alloc, True, False, None)
    assert alloc.copy_enabled_at > stamped


def test_feature_off_issues_nothing(db, kraken, copy_on):
    alloc = kraken.allocation("1000")
    _enable(db, alloc)
    _btc_buy(kraken)
    process_allocation(db, alloc)
    copy_on.copy_tickets_enabled = False
    assert copy_tickets.issue_tickets(db, alloc, T0) == []
    copy_on.copy_tickets_enabled = True
    assert len(copy_tickets.issue_tickets(db, alloc, T0)) == 1


# ---- issuing: binary -------------------------------------------------------------------------


@pytest.fixture()
def kalshi(db):
    creator = User(email="ck@example.com", password_hash="x", display_name="C")
    db.add(creator)
    db.flush()
    acct = VenueAccount(
        user_id=creator.id,
        venue="kalshi",
        venue_env="prod",
        key_id="k",
        anchor_balance_micros=1000 * M,
        anchor_at=T0,
        claimed_at=T0,
    )
    db.add(acct)
    db.flush()
    strategy = Strategy(user_id=creator.id, venue_account_id=acct.id, name="Kx", slug="kx")
    db.add(strategy)
    db.commit()
    return strategy


TICKER = "KXTEST-26AUG01-A"


def _binary_fill(strategy, fid, at, action, count, price, side="yes"):
    return Fill(
        strategy_id=strategy.id,
        venue_fill_id=fid,
        ticker=TICKER,
        side=side,
        action=action,
        count_micros=to_micros(count),
        price_micros=to_micros(price),
        fee_micros=0,
        filled_at=at,
    )


def _binary_alloc(db, strategy, email_on=False):
    follower = User(email="fk@example.com", password_hash="x", display_name="Fk")
    db.add(follower)
    db.flush()
    alloc = Allocation(
        user_id=follower.id,
        strategy_id=strategy.id,
        allocation_micros=100 * M,
        drawdown_limit_bps=1500,
        high_water_micros=100 * M,
        started_at=T0 - timedelta(hours=1),
    )
    db.add(alloc)
    db.commit()
    _enable(db, alloc, now=T0 - timedelta(hours=1), email_on=email_on)
    return alloc


def test_binary_ticket_and_match_with_slippage(db, kalshi, copy_on):
    alloc = _binary_alloc(db, kalshi)
    # Creator ($1,000) buys 10 YES at 0.40 -> follower $100: 10 x 100 / 1,000 = 1 contract
    db.add(_binary_fill(kalshi, "a", T0 + timedelta(minutes=5), "buy", "10", "0.40"))
    db.commit()
    signals.emit_signals(db, kalshi)
    process_allocation(db, alloc)
    [t] = copy_tickets.issue_tickets(db, alloc, T0 + timedelta(minutes=6))
    assert (t.ticker, t.label, t.side, t.action) == (TICKER, TICKER, "yes", "buy")
    assert (t.qty_atoms, t.qty_decimals, t.paper_price, t.creator_price) == (M, 6, "0.4", "0.4")
    assert t.stake_bps == 40  # $4 of $1,000
    assert t.instrument_id is None and t.mirror_fill_id is not None

    # The follower's own Kalshi account: binary fills are keyed by its strategy.
    follower = _follower(db, alloc)
    own = _own_account(db, follower, venue="kalshi")
    own_strategy = Strategy(user_id=follower.id, venue_account_id=own.id, name="Mine", slug="m")
    db.add(own_strategy)
    db.commit()
    copy_tickets.set_copy_settings(db, follower, alloc, True, False, own.id)
    db.add(
        _binary_fill(
            own_strategy, "own-no", T0 + timedelta(minutes=7), "buy", "1", "0.60", side="no"
        )
    )  # wrong side: ignored
    db.add(_binary_fill(own_strategy, "own-1", T0 + timedelta(minutes=8), "buy", "1", "0.41"))
    db.commit()
    assert copy_tickets.match_tickets(db, own, T0 + timedelta(minutes=10)) == 1
    db.refresh(t)
    assert t.status == "matched" and t.matched_qty_atoms == M and t.matched_price == "0.41"
    [ref] = t.matched_refs
    assert ref.startswith("fill:")
    # (0.41 - 0.40) / 0.40 = 250 bps worse than paper
    assert copy_tickets.ticket_view(t, True)["slippage_bps"] == 250.0
    assert copy_tickets.copy_stats(db, alloc)["median_slippage_bps"] == 250.0


# ---- matching: asset -------------------------------------------------------------------------


def _user_fill(db, account, inst, fid, action, qty, price, at):
    db.add(
        AssetFill(
            venue_account_id=account.id,
            instrument_id=inst.id,
            venue_fill_id=fid,
            action=action,
            qty_atoms=to_atoms(qty, inst.qty_decimals),
            qty_decimals=inst.qty_decimals,
            price=price,
            filled_at=at,
        )
    )
    db.commit()


@pytest.fixture()
def matched_world(db, kraken, copy_on):
    alloc = kraken.allocation("1000")
    follower = _follower(db, alloc)
    own = _own_account(db, follower)
    _enable(db, alloc, account_id=own.id)
    _btc_buy(kraken)
    process_allocation(db, alloc)
    [t] = copy_tickets.issue_tickets(db, alloc, T0 + timedelta(seconds=40))
    return kraken, alloc, own, t


def test_vwap_of_partial_fills_with_wrong_action_and_instrument_ignored(db, matched_world):
    kraken, alloc, own, t = matched_world
    at = T0 + timedelta(minutes=2)
    _user_fill(db, own, kraken.btc, "early", "buy", "0.01", "1", T0 - timedelta(minutes=1))
    _user_fill(db, own, kraken.btc, "u1", "buy", "0.004", "60000", at)
    _user_fill(db, own, kraken.btc, "sell", "sell", "0.01", "1", at)  # wrong action
    _user_fill(db, own, kraken.eth, "eth", "buy", "0.01", "1", at)  # wrong instrument
    _user_fill(db, own, kraken.btc, "u2", "buy", "0.004", "60100", at + timedelta(seconds=1))
    _user_fill(db, own, kraken.btc, "u3", "buy", "0.004", "60200", at + timedelta(seconds=2))
    assert copy_tickets.match_tickets(db, own, T0 + timedelta(minutes=5)) == 1
    db.refresh(t)
    # 0.004 @ 60,000 + 0.004 @ 60,100 + 0.002 of u3 @ 60,200 -> VWAP 60,080
    assert t.status == "matched" and t.matched_qty_atoms == 1_000_000
    assert t.matched_price == "60080" and len(t.matched_refs) == 3
    # (60,080 - 60,030) / 60,030 x 10,000 = 8.329... bps
    assert copy_tickets.ticket_view(t, True)["slippage_bps"] == 8.3
    assert t.matched_at.replace(tzinfo=UTC) == at + timedelta(seconds=2)
    # a rerun changes nothing
    assert copy_tickets.match_tickets(db, own, T0 + timedelta(minutes=6)) == 0


def test_exact_match_has_zero_slippage(db, matched_world):
    kraken, alloc, own, t = matched_world
    _user_fill(db, own, kraken.btc, "u1", "buy", "0.01", "60030", T0 + timedelta(minutes=1))
    copy_tickets.match_tickets(db, own, T0 + timedelta(minutes=5))
    db.refresh(t)
    assert t.matched_price == "60030"
    assert copy_tickets.slippage_bps(t.action, t.paper_price, t.matched_price) == 0


def test_a_fill_matches_one_ticket_and_a_partial_keeps_filling(db, matched_world):
    kraken, alloc, own, t1 = matched_world
    _btc_buy(kraken, filled_at=T0 + timedelta(minutes=1), before="0.1", after="0.2")
    process_allocation(db, alloc)
    [t2] = copy_tickets.issue_tickets(db, alloc, T0 + timedelta(minutes=2))
    # One real fill of 0.02 BTC: the older ticket takes it, all of it.
    _user_fill(db, own, kraken.btc, "big", "buy", "0.02", "60000", T0 + timedelta(minutes=3))
    assert copy_tickets.match_tickets(db, own, T0 + timedelta(minutes=4)) == 1
    db.refresh(t1)
    db.refresh(t2)
    assert t1.status == "matched" and t1.matched_qty_atoms == 1_000_000
    assert t2.status == "open" and t2.matched_refs is None
    # Later 0.005 then 0.005 more: t2 matches partially, then whole.
    _user_fill(db, own, kraken.btc, "p1", "buy", "0.005", "60000", T0 + timedelta(minutes=5))
    copy_tickets.match_tickets(db, own, T0 + timedelta(minutes=6))
    db.refresh(t2)
    assert t2.status == "matched" and t2.matched_qty_atoms == 500_000
    assert copy_tickets.ticket_view(t2, True)["partial"] is True
    _user_fill(db, own, kraken.btc, "p2", "buy", "0.005", "60100", T0 + timedelta(minutes=7))
    assert copy_tickets.match_tickets(db, own, T0 + timedelta(minutes=8)) == 1
    db.refresh(t2)
    # t2 is smaller than 0.01 (the follower's cash after the first buy caps
    # it), so p2 counts only for the rest: q2 - 0.005 at 60,100.
    q2 = Fraction(t2.qty_atoms, 10**8)
    assert Fraction("0.005") < q2 < Fraction("0.01")
    vwap = (Fraction("0.005") * 60000 + (q2 - Fraction("0.005")) * 60100) / q2
    assert t2.matched_qty_atoms == t2.qty_atoms
    assert t2.matched_price == copy_tickets.decimal_str(vwap)
    assert len(t2.matched_refs) == 2


def test_fills_outside_the_window_or_on_a_skipped_ticket_do_not_match(db, matched_world):
    kraken, alloc, own, t = matched_world
    # issued at T0+40s: the window ends 24 h later
    _user_fill(db, own, kraken.btc, "late", "buy", "0.01", "60000", T0 + timedelta(hours=25))
    assert copy_tickets.match_tickets(db, own, T0 + timedelta(hours=26)) == 0
    t.status = CopyTicketStatus.SKIPPED
    db.commit()
    _user_fill(db, own, kraken.btc, "ok", "buy", "0.01", "60000", T0 + timedelta(minutes=1))
    assert copy_tickets.match_tickets(db, own, T0 + timedelta(hours=26)) == 0


def test_expired_and_done_tickets_still_match(db, matched_world):
    kraken, alloc, own, t = matched_world
    assert copy_tickets.expire_tickets(db, T0 + timedelta(hours=2)) == 1
    db.refresh(t)
    assert t.status == "expired"
    _user_fill(db, own, kraken.btc, "late", "buy", "0.01", "60000", T0 + timedelta(hours=3))
    assert copy_tickets.match_tickets(db, own, T0 + timedelta(hours=4)) == 1


# ---- settings ----------------------------------------------------------------------------------


def test_settings_accept_only_the_users_own_live_account_on_that_venue(db, kraken, copy_on):
    alloc = kraken.allocation("1000")
    follower = _follower(db, alloc)
    stranger = User(email="s@example.com", password_hash="x", display_name="S")
    db.add(stranger)
    db.commit()
    theirs = _own_account(db, stranger)
    other_venue = _own_account(db, follower, venue="coinbase")
    watch = _own_account(db, follower, is_watch=True)
    suspended = _own_account(db, follower, status=VenueAccountStatus.SUSPENDED)
    for bad in (theirs, other_venue, watch, suspended):
        with pytest.raises(HTTPException) as exc:
            copy_tickets.set_copy_settings(db, follower, alloc, True, False, bad.id)
        assert exc.value.status_code == 422
    db.rollback()
    mine = _own_account(db, follower)
    copy_tickets.set_copy_settings(db, follower, alloc, True, True, mine.id)
    assert (alloc.copy_tickets, alloc.copy_email, alloc.copy_account_id) == (True, True, mine.id)
    ctx = copy_tickets.allocation_copy_context(db, follower, alloc)
    assert [a["id"] for a in ctx["accounts"]] == [mine.id]
    assert "own" not in str(ctx["accounts"])  # never the key id
    assert ctx["action"] == f"/portfolio/allocations/{alloc.id}/copy"
    assert ctx["stats"]["issued"] == 0
    audit = db.scalars(select(AuditLog).where(AuditLog.action == "copy.settings")).all()
    assert audit and audit[-1].details["account_id"] == mine.id
    with pytest.raises(HTTPException) as exc:  # someone else's allocation
        copy_tickets.set_copy_settings(db, stranger, alloc, True, False, None)
    assert exc.value.status_code == 404


# ---- worker: issue + email in the mirror pass, match in the signals job ---------------------


def _ctx(db, payload=None, account=None, kind="mirror"):
    job = Job(kind=kind, dedupe_key="t", payload=payload or {}, priority=10)
    return JobContext(
        db=db, job=job, account=account, worker_id="t", settings=get_settings(), limiter=None
    )


def test_mirror_pass_issues_and_emails_once(db, kraken, copy_on, outbox):
    from alphamarket import worker

    alloc = kraken.allocation("1000")
    _enable(db, alloc, email_on=True)
    now = datetime.now(UTC)
    _btc_buy(kraken, filled_at=now - timedelta(minutes=5))
    worker.handle_mirror(_ctx(db, {"strategy_id": kraken.strategy.id}))
    assert len(_tickets(db, alloc)) == 1
    [msg] = list(outbox)
    follower = _follower(db, alloc)
    assert msg.to == follower.email and "1 new copy ticket" in msg.subject
    assert "BUY LONG 0.01 BTC/USD at 60030" in msg.body
    assert "/copy" in msg.body and copy_tickets.DISCLAIMER in msg.body
    worker.handle_mirror(_ctx(db, {"strategy_id": kraken.strategy.id}))  # nothing new
    assert len(outbox) == 1 and len(_tickets(db, alloc)) == 1


def test_email_off_or_unsized_email_has_no_quantity(db, kraken, copy_on, outbox):
    from alphamarket import worker

    alloc = kraken.allocation("1000")
    _enable(db, alloc, email_on=False)
    now = datetime.now(UTC)
    _btc_buy(kraken, filled_at=now - timedelta(minutes=5))
    worker.handle_mirror(_ctx(db))  # the global sweep also expires
    assert len(_tickets(db, alloc)) == 1 and not outbox
    copy_on.copy_tickets_sized = False
    [t] = _tickets(db, alloc)
    alloc.copy_email = True
    db.commit()
    assert copy_tickets.notify_new_tickets(db, alloc, [t])
    body = outbox[-1].body
    assert "0.01" not in body and "60.00% of the creator's equity" in body
    assert copy_tickets.DISCLAIMER_UNSIZED in body


def test_signals_job_matches_copies(db, matched_world):
    from alphamarket import worker

    kraken, alloc, own, t = matched_world
    # the ticket was issued in the past here: re-date it inside the lookback
    t.created_at = datetime.now(UTC) - timedelta(minutes=10)
    db.commit()
    _user_fill(db, own, kraken.btc, "u", "buy", "0.01", "60030", T0 + timedelta(minutes=1))
    worker.handle_signals(_ctx(db, account=own, kind="signals"))
    db.refresh(t)
    assert t.status == "matched"


# ---- pages and JSON -------------------------------------------------------------------------


def _signup(client, email_addr):
    resp = client.post(
        "/signup-form",
        data={"email": email_addr, "password": PASSWORD, "display_name": "Inv"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


@pytest.fixture()
def viewer_world(client, session_factory, copy_on):
    """A logged-in viewer with a Kraken paper tail with one open ticket."""
    _signup(client, "inv@example.com")
    with session_factory() as db:
        viewer = db.scalar(select(User).where(User.email == "inv@example.com"))
        w = CopyWorld(db, "kraken")
        w.btc = w.instrument("XXBTZUSD", "BTC", min_qty_atoms=10_000, min_notional="0.5")
        alloc = Allocation(
            user_id=viewer.id,
            strategy_id=w.strategy.id,
            allocation_micros=1000 * M,
            drawdown_limit_bps=1500,
            high_water_micros=1000 * M,
            started_at=START,
        )
        db.add(alloc)
        db.commit()
        _enable(db, alloc)
        _btc_buy(w, filled_at=datetime.now(UTC) - timedelta(minutes=5))
        process_allocation(db, alloc)
        [t] = copy_tickets.issue_tickets(db, alloc)
        return {"viewer": viewer.id, "alloc": alloc.id, "ticket": t.id}


def test_copy_page_lists_the_open_ticket(client, viewer_world):
    resp = client.get("/copy")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "private, no-store"
    html = resp.text
    assert 'name="robots" content="noindex' in html
    assert "BTC/USD" in html and "0.01" in html and "60.00%" in html
    assert 'href="https://pro.kraken.com/app/trade/btc-usd"' in html
    assert 'rel="noopener noreferrer"' in html and 'target="_blank"' in html
    assert f'action="/copy/{viewer_world["ticket"]}/done"' in html
    assert "Koine does not place orders" in html


def test_copy_page_unsized_shows_no_quantity(client, viewer_world, copy_on):
    copy_on.copy_tickets_sized = False
    html = client.get("/copy").text
    assert "BTC/USD" in html and "60.00%" in html
    assert "0.01 BTC" not in html and ">0.01<" not in html
    data = client.get("/api/copy-tickets").json()
    [t] = data["open"]
    assert "qty" not in t and "matched_qty" not in t and t["stake_pct"] == 0.6
    view = copy_tickets.ticket_view(CopyTicket(**_ticket_fields()), False)
    assert "qty" not in view


def _ticket_fields():
    now = datetime.now(UTC)
    return {
        "id": 1,
        "allocation_id": 1,
        "venue": "kraken",
        "ticker": "X",
        "label": "X",
        "side": "long",
        "action": "buy",
        "qty_atoms": 5,
        "qty_decimals": 0,
        "paper_price": "1",
        "creator_price": "1",
        "stake_bps": 10,
        "created_at": now,
        "expires_at": now + timedelta(hours=1),
        "status": "open",
    }


def test_copy_routes_404_when_the_feature_is_off(client, viewer_world, copy_on):
    copy_on.copy_tickets_enabled = False
    assert client.get("/copy", follow_redirects=False).status_code == 404
    assert client.get("/api/copy-tickets").status_code == 404
    tid = viewer_world["ticket"]
    assert client.post(f"/copy/{tid}/skip", follow_redirects=False).status_code == 404


def test_logged_out_copy_page_goes_to_login(client, copy_on):
    resp = client.get("/copy", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login?next=/copy"


def test_done_and_skip_only_for_the_owner(client, session_factory, viewer_world):
    tid = viewer_world["ticket"]
    client.post("/logout-form")
    _signup(client, "other@example.com")
    assert client.post(f"/copy/{tid}/skip").status_code == 404
    assert client.post(f"/copy/{tid}/done").status_code == 404
    client.post("/logout-form")
    client.post(
        "/login-form",
        data={"email": "inv@example.com", "password": PASSWORD},
        follow_redirects=False,
    )
    resp = client.post(f"/copy/{tid}/skip", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/copy?notice=skipped"
    # skipped is final for done/skip: the second click changes nothing
    resp = client.post(f"/copy/{tid}/done", follow_redirects=False)
    assert resp.headers["location"] == "/copy"
    with session_factory() as db:
        t = db.get(CopyTicket, tid)
        assert t.status == "skipped"
        n = db.scalar(select(func.count()).where(AuditLog.action == "copy.ticket_skip"))
        assert n == 1
        # done is allowed from expired
        t.status = "expired"
        db.commit()
    resp = client.post(f"/copy/{tid}/done", follow_redirects=False)
    assert resp.headers["location"] == "/copy?notice=done"
    with session_factory() as db:
        assert db.get(CopyTicket, tid).status == "done"


def test_done_from_expired_only_inside_the_match_window(db, kraken, copy_on):
    # Issued at T; expired tickets may be marked done until T + 24h (MATCH_WINDOW).
    t = CopyTicket(created_at=START, status=CopyTicketStatus.EXPIRED)
    assert copy_tickets.can_mark_done(t, "expired", START + timedelta(hours=24))
    assert not copy_tickets.can_mark_done(t, "expired", START + timedelta(hours=24, seconds=1))
    assert copy_tickets.can_mark_done(t, "open", START + timedelta(days=30))
    assert not copy_tickets.can_mark_done(t, "skipped", START)
    assert not copy_tickets.can_mark_done(t, "matched", START)


def test_settings_form_and_json(client, session_factory, viewer_world):
    aid = viewer_world["alloc"]
    resp = client.post(
        f"/portfolio/allocations/{aid}/copy",
        data={"enabled": "on", "email": "on", "account_id": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/portfolio?notice=copy_saved"
    with session_factory() as db:
        a = db.get(Allocation, aid)
        assert a.copy_tickets and a.copy_email and a.copy_account_id is None
        stranger = User(email="z@example.com", password_hash="x", display_name="Z")
        db.add(stranger)
        db.commit()
        theirs = VenueAccount(user_id=stranger.id, venue="kraken", venue_env="prod", key_id="z")
        db.add(theirs)
        db.commit()
        theirs_id = theirs.id
    resp = client.post(
        f"/portfolio/allocations/{aid}/copy",
        data={"enabled": "on", "account_id": str(theirs_id)},
        follow_redirects=False,
    )
    assert resp.status_code == 422 and "your own connected Kraken" in resp.text
    resp = client.post(f"/allocations/{aid}/copy", json={"enabled": False, "email": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False and body["stats"]["issued"] == 1
    resp = client.post(f"/allocations/{aid}/copy", json={"enabled": True, "account_id": theirs_id})
    assert resp.status_code == 422
    data = client.get("/api/copy-tickets").json()
    assert data["sized"] is True and data["disclaimer"] == copy_tickets.DISCLAIMER
    [t] = data["open"]
    assert t["qty"] == "0.01" and t["place_url"].endswith("/btc-usd")
    assert [a["allocation_id"] for a in data["allocations"]] == [aid]
    # someone else's allocation is a 404
    client.post("/logout-form")
    _signup(client, "other2@example.com")
    assert client.post(f"/allocations/{aid}/copy", json={"enabled": True}).status_code == 404
    assert client.post(f"/portfolio/allocations/{aid}/copy", data={}).status_code == 404


# ---- concurrency: two passes issuing the same ticket ----------------------------------------


def _race(session_factory, monkeypatch, make_world) -> int:
    """Pass B read the ticketless paper fills before pass A committed its
    ticket; B's insert then hits the unique source id, is rolled back alone,
    and B issues nothing. Returns how many tickets exist."""
    with session_factory() as a, session_factory() as b:
        alloc_id = make_world(a)
        alloc_b = b.get(Allocation, alloc_id)
        stale = copy_tickets._unticketed_sources(b, alloc_b, START)
        assert len(stale) == 1
        assert len(copy_tickets.issue_tickets(a, a.get(Allocation, alloc_id), T0)) == 1
        monkeypatch.setattr(copy_tickets, "_unticketed_sources", lambda *_: stale)
        assert copy_tickets.issue_tickets(b, alloc_b, T0) == []
        return b.scalar(
            select(func.count(CopyTicket.id)).where(CopyTicket.allocation_id == alloc_id)
        )


def _binary_world(db, tag: str) -> int:
    creator = User(email=f"rc-{tag}@example.com", password_hash="x", display_name="C")
    follower = User(email=f"rf-{tag}@example.com", password_hash="x", display_name="F")
    db.add_all([creator, follower])
    db.flush()
    acct = VenueAccount(user_id=creator.id, venue="kalshi", venue_env="prod", key_id=tag)
    db.add(acct)
    db.flush()
    strategy = Strategy(user_id=creator.id, venue_account_id=acct.id, name=tag, slug=f"r-{tag}")
    db.add(strategy)
    db.flush()
    fill = _binary_fill(strategy, f"r-{tag}", T0, "buy", "10", "0.40")
    db.add(fill)
    db.flush()
    sig = Signal(
        strategy_id=strategy.id,
        fill_id=fill.id,
        ticker=TICKER,
        side="yes",
        action="buy",
        count_micros=10 * M,
        price_micros=400_000,
        creator_equity_micros=1000 * M,
        position_after_micros=10 * M,
        filled_at=T0,
    )
    alloc = Allocation(
        user_id=follower.id,
        strategy_id=strategy.id,
        allocation_micros=100 * M,
        drawdown_limit_bps=1500,
        high_water_micros=100 * M,
        started_at=START,
        copy_tickets=True,
        copy_enabled_at=START,
    )
    db.add_all([sig, alloc])
    db.flush()
    db.add(
        MirrorFill(
            allocation_id=alloc.id,
            signal_id=sig.id,
            mode="paper",
            ticker=TICKER,
            side="yes",
            action="buy",
            count_micros=M,
            price_micros=400_000,
            filled_at=T0,
        )
    )
    db.commit()
    return alloc.id


def test_a_concurrent_pass_cannot_issue_a_ticket_twice(session_factory, monkeypatch, copy_on):
    assert _race(session_factory, monkeypatch, lambda db: _binary_world(db, "sqlite")) == 1


@pytest.mark.pg
def test_a_concurrent_pass_cannot_issue_a_ticket_twice_pg(pg_session_factory, monkeypatch, copy_on):
    import uuid

    tag = uuid.uuid4().hex[:8]
    assert _race(pg_session_factory, monkeypatch, lambda db: _binary_world(db, tag)) == 1

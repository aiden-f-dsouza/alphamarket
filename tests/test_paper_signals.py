"""Signals v2 (paper_signals.emit_asset_signals) and the feed's asset item.

Hand-computed expectations (USD micros; 1 USD = M). The creator is anchored
at D-1 00:00 UTC with $5,000 + 1 ETH; the only complete close before D is
ETH 3,000 on D-1 (D's 3,100 close is not complete at any instant of D).
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from alphamarket import paper_signals, prices
from alphamarket.connectors.base import BudgetExhausted, InstrumentType, LegKind
from alphamarket.follows import feed_items, signal_item
from alphamarket.models import (
    AnchorBalance,
    AssetFill,
    AssetQuote,
    Instrument,
    LedgerEntry,
    PriceDaily,
    Signal,
    Strategy,
    User,
    VenueAccount,
)
from alphamarket.money import to_atoms
from alphamarket.signals import emit_signals

M = 1_000_000
D = date(2026, 9, 10)
DM1 = D - timedelta(days=1)
ETH = 10**8  # 1 ETH at 8 dp


def at(day: date, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


@pytest.fixture()
def db(session_factory):
    with session_factory() as session:
        yield session


class QuoteDouble:
    """Stands in for prices.capture_quote (a stub until w2-market-data lands)."""

    def __init__(self, raises: Exception | None = None):
        self.calls: list[str] = []
        self.raises = raises

    def __call__(self, db, venue, venue_env, instrument, limiter):
        self.calls.append(instrument.symbol)
        if self.raises is not None:
            raise self.raises
        return AssetQuote(
            instrument_id=instrument.id,
            bid="3099.5",
            ask="3100.5",
            last="3100",
            as_of=at(D, 12, 2),
            source=venue,
        )


@pytest.fixture()
def quotes(monkeypatch):
    double = QuoteDouble()
    monkeypatch.setattr(prices, "capture_quote", double)
    return double


def _account(db, venue="kraken", slug="k", anchor=(("USD", 5_000 * M, 6), ("ETH", ETH, 8))):
    user = User(email=f"{slug}@example.com", password_hash="x", display_name=slug)
    db.add(user)
    db.flush()
    acct = VenueAccount(user_id=user.id, venue=venue, venue_env="prod", key_id=slug)
    db.add(acct)
    db.flush()
    for asset, atoms, decimals in anchor:
        db.add(
            AnchorBalance(
                venue_account_id=acct.id,
                asset=asset,
                atoms=atoms,
                decimals=decimals,
                as_of=at(DM1, 0),
            )
        )
    strategy = Strategy(user_id=user.id, venue_account_id=acct.id, name=slug, slug=slug)
    db.add(strategy)
    db.commit()
    return acct, strategy


def _instrument(db, venue, symbol, base, quote="USD", type_=InstrumentType.SPOT, decimals=8):
    inst = Instrument(
        venue=venue,
        symbol=symbol,
        type=type_,
        base_asset=base,
        quote_asset=quote,
        qty_decimals=decimals,
    )
    db.add(inst)
    db.flush()
    return inst


def _close(db, inst, day, usd):
    db.add(
        PriceDaily(
            instrument_id=inst.id,
            day=day,
            close=str(usd),
            usd_micros_per_unit=usd * M,
            source=inst.venue,
        )
    )


def _leg(db, acct, asset, delta, decimals, kind, ref, when, instrument_id=None):
    db.add(
        LedgerEntry(
            venue_account_id=acct.id,
            instrument_id=instrument_id,
            asset=asset,
            delta_atoms=delta,
            decimals=decimals,
            kind=kind,
            venue_ref=ref,
            group_ref=f"fill:{ref}",
            occurred_at=when,
        )
    )


def _fill(
    db, acct, inst, fid, action, qty, price, when, ingested=None, quote_legs=None, base_leg=True
):
    """An AssetFill plus its base and quote legs (quote_legs: (asset, delta, dp))."""
    q = to_atoms(qty, inst.qty_decimals)
    db.add(
        AssetFill(
            venue_account_id=acct.id,
            instrument_id=inst.id,
            venue_fill_id=fid,
            action=action,
            qty_atoms=q,
            qty_decimals=inst.qty_decimals,
            price=price,
            filled_at=when,
            ingested_at=ingested or when + timedelta(minutes=1),
        )
    )
    sign = 1 if action == "buy" else -1
    if base_leg:
        _leg(db, acct, inst.base_asset, sign * q, inst.qty_decimals, LegKind.FILL_BASE, fid, when)
    for asset, delta, decimals in quote_legs or ():
        kind = LegKind.FEE if asset.startswith("fee:") else LegKind.FILL_QUOTE
        _leg(db, acct, asset.removeprefix("fee:"), delta, decimals, kind, fid, when)


@pytest.fixture()
def kraken(db):
    acct, strategy = _account(db)
    eth = _instrument(db, "kraken", "XETHZUSD", "ETH")
    _close(db, eth, DM1, 3_000)
    _close(db, eth, D, 3_100)  # not complete at any instant of D: never used on D
    db.commit()
    return acct, strategy, eth


def _signals(db, strategy):
    return db.scalars(
        select(Signal).where(Signal.strategy_id == strategy.id).order_by(Signal.id)
    ).all()


# ---- the plan's exit test --------------------------------------------------------------------


def test_buy_signal_carries_equity_position_notional_and_quote(db, kraken, quotes):
    acct, strategy, eth = kraken
    # Buy 0.5 ETH at 3,100 on D 12:00: cash -1,550.
    _fill(db, acct, eth, "f1", "buy", "0.5", "3100", at(D, 12), quote_legs=[("USD", -1_550 * M, 6)])
    # A deposit is a ledger event, never a signal.
    _leg(db, acct, "USD", 1_000 * M, 6, LegKind.DEPOSIT, "dep1", at(D, 13))
    db.commit()

    assert emit_signals(db, strategy) == 1  # dispatches to paper_signals for kraken
    [s] = _signals(db, strategy)
    # E(t-) = 5,000 + 1 ETH x 3,000 (D-1 close) = 8,000.
    assert s.creator_equity_micros == 8_000_000_000
    assert s.equity_basis_day == DM1
    assert (s.position_before_atoms, s.position_after_atoms) == (1 * ETH, 150_000_000)
    assert s.notional_micros == 1_550_000_000  # 0.5 x 3,100
    assert (s.ticker, s.side, s.action) == ("XETHZUSD", "long", "buy")
    assert (s.qty_atoms, s.qty_decimals, s.price) == (50_000_000, 8, "3100")
    assert s.instrument_id == eth.id
    assert s.fill_id is None and s.count_micros is None and s.price_micros is None
    assert s.position_after_micros is None
    fill = db.scalars(select(AssetFill)).one()
    assert s.asset_fill_id == fill.id
    assert s.observed_at.replace(tzinfo=UTC) == at(D, 12, 1)
    # The quote is copied from what capture_quote returned.
    assert (s.quote_bid, s.quote_ask, s.quote_last, s.quote_source) == (
        "3099.5",
        "3100.5",
        "3100",
        "kraken",
    )
    assert s.quote_at.replace(tzinfo=UTC) == at(D, 12, 2)
    assert quotes.calls == ["XETHZUSD"]

    # A second run adds nothing and captures no quote.
    assert emit_signals(db, strategy) == 0
    assert len(_signals(db, strategy)) == 1
    assert quotes.calls == ["XETHZUSD"]


def test_same_instant_fills_chain_positions_and_share_equity(db, kraken, quotes):
    acct, strategy, eth = kraken
    _fill(db, acct, eth, "f1", "buy", "0.5", "3100", at(D, 12), quote_legs=[("USD", -1_550 * M, 6)])
    # One order, two fills at 14:00: sell 0.2 then 0.3 at 3,150.
    _fill(db, acct, eth, "f2", "sell", "0.2", "3150", at(D, 14), quote_legs=[("USD", 630 * M, 6)])
    _fill(db, acct, eth, "f3", "sell", "0.3", "3150", at(D, 14), quote_legs=[("USD", 945 * M, 6)])
    db.commit()

    assert paper_signals.emit_asset_signals(db, strategy) == 3
    s1, s2, s3 = _signals(db, strategy)
    assert s1.creator_equity_micros == 8_000 * M
    # E(14:00-) = cash 5,000 - 1,550 + 1.5 ETH x 3,000 = 3,450 + 4,500 = 7,950,
    # shared by both same-instant fills (the 14:00 legs are not "before").
    assert s2.creator_equity_micros == s3.creator_equity_micros == 7_950 * M
    assert s2.equity_basis_day == s3.equity_basis_day == DM1
    # Running position: 1.5 -> 1.3 -> 1.0 ETH.
    assert (s2.position_before_atoms, s2.position_after_atoms) == (150_000_000, 130_000_000)
    assert (s3.position_before_atoms, s3.position_after_atoms) == (130_000_000, 100_000_000)
    assert s2.side == s3.side == "long" and s2.action == s3.action == "sell"
    assert (s2.notional_micros, s3.notional_micros) == (630 * M, 945 * M)
    assert quotes.calls == ["XETHZUSD"]  # once per instrument per pass


def test_same_instant_fill_arriving_in_a_later_sync_continues_the_chain(db, kraken, quotes):
    # One order filled twice at 14:00; the second fill is imported a sync later.
    acct, strategy, eth = kraken
    _fill(db, acct, eth, "f2", "sell", "0.2", "3150", at(D, 14), quote_legs=[("USD", 630 * M, 6)])
    db.commit()
    assert emit_signals(db, strategy) == 1
    _fill(db, acct, eth, "f3", "sell", "0.3", "3150", at(D, 14), quote_legs=[("USD", 945 * M, 6)])
    db.commit()
    assert emit_signals(db, strategy) == 1
    s2, s3 = _signals(db, strategy)
    # 1.0 -> 0.8 -> 0.5 ETH; both priced against E = 5,000 + 1 x 3,000 = 8,000.
    assert (s2.position_before_atoms, s2.position_after_atoms) == (ETH, 80_000_000)
    assert (s3.position_before_atoms, s3.position_after_atoms) == (80_000_000, 50_000_000)
    assert s2.creator_equity_micros == s3.creator_equity_micros == 8_000 * M


def test_a_fee_charged_in_the_base_asset_shows_in_the_next_before(db, kraken, quotes):
    # Positions are the ledger's holdings, not a sum of fill sizes: buy 1 ETH
    # paying a 0.001 ETH fee at 12:00, so the 14:00 sell starts from
    # 1 + 1 - 0.001 = 1.999 ETH (the 12:00 signal's "after" was 2.0).
    acct, strategy, eth = kraken
    legs = [("USD", -3_100 * M, 6), ("fee:ETH", -100_000, 8)]
    _fill(db, acct, eth, "f1", "buy", "1", "3100", at(D, 12), quote_legs=legs)
    _fill(db, acct, eth, "f2", "sell", "0.5", "3150", at(D, 14), quote_legs=[("USD", 1_575 * M, 6)])
    db.commit()
    assert emit_signals(db, strategy) == 2
    s1, s2 = _signals(db, strategy)
    assert (s1.position_before_atoms, s1.position_after_atoms) == (ETH, 2 * ETH)
    assert (s2.position_before_atoms, s2.position_after_atoms) == (199_900_000, 149_900_000)
    # E(14:00-) = 5,000 - 3,100 + 1.999 x 3,000 = 1,900 + 5,997 = 7,897.
    assert s2.creator_equity_micros == 7_897 * M


def test_fills_at_or_before_the_anchor_never_become_signals(db, kraken, quotes):
    acct, strategy, eth = kraken
    _fill(db, acct, eth, "old", "buy", "0.1", "2900", at(DM1, 0) - timedelta(hours=1))
    _fill(db, acct, eth, "edge", "buy", "0.1", "2900", at(DM1, 0))
    db.commit()
    assert emit_signals(db, strategy) == 0
    assert _signals(db, strategy) == []
    assert quotes.calls == []


def test_an_unanchored_account_emits_nothing(db, quotes):
    acct, strategy = _account(db, anchor=())
    eth = _instrument(db, "kraken", "XETHZUSD", "ETH")
    _fill(db, acct, eth, "f1", "buy", "0.5", "3100", at(D, 12))
    db.commit()
    assert emit_signals(db, strategy) == 0


# ---- perps --------------------------------------------------------------------------------


def test_perp_fills_use_the_signed_perp_book(db, quotes):
    # Hyperliquid-shaped: symbol "BTC", base "BTC-PERP", USDC collateral.
    acct, strategy = _account(db, venue="hyperliquid", slug="h", anchor=(("USDC", 1_000 * M, 6),))
    perp = _instrument(db, "hyperliquid", "BTC", "BTC-PERP", "USDC", InstrumentType.PERP, 5)
    _close(db, perp, DM1, 60_000)
    _close(db, perp, D, 60_500)
    # D 10:00 buy 0.1 @ 60,000, fee 3 USDC.
    fee = [("fee:USDC", -3 * M, 6)]
    _fill(db, acct, perp, "p1", "buy", "0.1", "60000", at(D, 10), quote_legs=fee)
    # D+1 10:00 sell 0.3 @ 61,000: closes 0.1 (realized +100), opens 0.2 short.
    _fill(db, acct, perp, "p2", "sell", "0.3", "61000", at(D + timedelta(days=1), 10))
    _leg(db, acct, "USDC", 100 * M, 6, LegKind.REALIZED_PNL, "p2", at(D + timedelta(days=1), 10))
    db.commit()

    assert emit_signals(db, strategy) == 2
    s1, s2 = _signals(db, strategy)
    # Opening: flat -> +0.1 (10,000 atoms at 5 dp), equity = 1,000 USDC cash only.
    assert (s1.position_before_atoms, s1.position_after_atoms, s1.side) == (0, 10_000, "long")
    assert s1.creator_equity_micros == 1_000 * M and s1.equity_basis_day is None
    assert s1.notional_micros == 6_000 * M  # 0.1 x 60,000, USDC at par
    # Flip: +0.1 -> -0.2, short. E = 1,000 - 3 + 0.1 x (60,500 - 60,000) = 1,047 at D's close.
    assert (s2.position_before_atoms, s2.position_after_atoms) == (10_000, -20_000)
    assert s2.side == "short"
    assert s2.creator_equity_micros == 1_047 * M and s2.equity_basis_day == D
    assert s2.notional_micros == 18_300 * M  # 0.3 x 61,000
    assert (s1.ticker, s2.ticker, s2.action) == ("BTC", "BTC", "sell")


def test_perp_position_comes_from_perp_fills_not_ledger_legs(db, quotes):
    # The perp book is built from asset_fills: here the venue booked no
    # FILL_BASE legs, so the ledger alone would say "flat" before the second fill.
    acct, strategy = _account(db, venue="hyperliquid", slug="h2", anchor=(("USDC", 1_000 * M, 6),))
    perp = _instrument(db, "hyperliquid", "ETH", "ETH-PERP", "USDC", InstrumentType.PERP, 4)
    _fill(db, acct, perp, "e1", "sell", "0.5", "3000", at(D, 10), base_leg=False)
    _fill(db, acct, perp, "e2", "buy", "0.2", "2990", at(D, 11), base_leg=False)
    db.commit()
    assert emit_signals(db, strategy) == 2
    s1, s2 = _signals(db, strategy)
    assert (s1.position_before_atoms, s1.position_after_atoms, s1.side) == (0, -5_000, "short")
    assert (s2.position_before_atoms, s2.position_after_atoms, s2.side) == (-5_000, -3_000, "short")


# ---- non-USD quotes, precision -------------------------------------------------------------


def test_btc_quoted_pair_has_no_usd_notional(db, quotes):
    # ETH held at 12 dp in the anchor; the pair reports 8 dp. The holding
    # 1.000000000005 ETH truncates to 1.00000000 at the fill's precision.
    acct, strategy = _account(
        db, slug="b", anchor=(("USD", 5_000 * M, 6), ("ETH", 1_000_000_000_005, 12))
    )
    pair = _instrument(db, "kraken", "XETHXXBT", "ETH", quote="BTC")
    usd = _instrument(db, "kraken", "XETHZUSD", "ETH")
    _close(db, usd, DM1, 3_000)
    _fill(db, acct, pair, "x1", "buy", "0.1", "0.05", at(D, 12), quote_legs=[("BTC", -500_000, 8)])
    db.commit()

    assert emit_signals(db, strategy) == 1
    [s] = _signals(db, strategy)
    assert s.notional_micros is None
    assert (s.position_before_atoms, s.position_after_atoms) == (100_000_000, 110_000_000)
    # 5,000 + 1.000000000005 x 3,000 = 8,000.000000015 -> truncated to 8,000.000000.
    assert s.creator_equity_micros == 8_000 * M
    item = signal_item(strategy, s)
    assert item["stake_fraction"] is None and item["notional"] is None


# ---- quote capture --------------------------------------------------------------------------


def test_budget_exhausted_quote_still_stores_the_signal(db, kraken, monkeypatch):
    acct, strategy, eth = kraken
    double = QuoteDouble(raises=BudgetExhausted(30.0, "ticker"))
    monkeypatch.setattr(prices, "capture_quote", double)
    _fill(db, acct, eth, "f1", "buy", "0.5", "3100", at(D, 12), quote_legs=[("USD", -1_550 * M, 6)])
    db.commit()
    assert emit_signals(db, strategy) == 1
    [s] = _signals(db, strategy)
    assert double.calls == ["XETHZUSD"]
    assert (s.quote_bid, s.quote_ask, s.quote_last, s.quote_at, s.quote_source) == (None,) * 5
    assert s.creator_equity_micros == 8_000 * M


def test_a_failing_quote_source_still_stores_the_signal(db, kraken, monkeypatch):
    acct, strategy, eth = kraken
    double = QuoteDouble(raises=RuntimeError("source down"))
    monkeypatch.setattr(prices, "capture_quote", double)
    _fill(db, acct, eth, "f1", "buy", "0.5", "3100", at(D, 12), quote_legs=[("USD", -1_550 * M, 6)])
    db.commit()
    assert emit_signals(db, strategy) == 1
    [s] = _signals(db, strategy)
    assert (s.quote_bid, s.quote_ask, s.quote_last, s.quote_at, s.quote_source) == (None,) * 5


def test_a_database_error_in_quote_capture_is_not_swallowed(db, kraken, monkeypatch):
    # capture_quote writes asset_quotes in the same session: a failed flush
    # leaves the transaction unusable, so the pass must fail, not carry on.
    acct, strategy, eth = kraken
    error = IntegrityError("INSERT INTO asset_quotes", {}, Exception("boom"))
    monkeypatch.setattr(prices, "capture_quote", QuoteDouble(raises=error))
    _fill(db, acct, eth, "f1", "buy", "0.5", "3100", at(D, 12), quote_legs=[("USD", -1_550 * M, 6)])
    db.commit()
    with pytest.raises(IntegrityError):
        emit_signals(db, strategy)
    db.rollback()
    assert _signals(db, strategy) == []


def test_a_signal_stale_when_observed_gets_no_quote(db, kraken, quotes):
    acct, strategy, eth = kraken
    # Kraken's copy-lag limit is 6 h; Koine first saw this fill 7 h after it happened.
    _fill(db, acct, eth, "f1", "buy", "0.5", "3100", at(D, 1), ingested=at(D, 8))
    db.commit()
    assert emit_signals(db, strategy) == 1
    [s] = _signals(db, strategy)
    assert quotes.calls == []
    assert s.quote_last is None and s.observed_at.replace(tzinfo=UTC) == at(D, 8)


def test_notional_formula():
    assert paper_signals.notional_usd_micros(50_000_000, 8, "3100", "USD") == 1_550 * M
    assert paper_signals.notional_usd_micros(1, 8, "60990.5", "USDT") == 609  # 609.905 -> down
    assert paper_signals.notional_usd_micros(10**7, 8, "0.05", "BTC") is None


# ---- the feed -----------------------------------------------------------------------------


def test_signal_item_renders_an_asset_signal(db, kraken, quotes):
    acct, strategy, eth = kraken
    _fill(db, acct, eth, "f1", "buy", "0.5", "3100", at(D, 12), quote_legs=[("USD", -1_550 * M, 6)])
    db.commit()
    emit_signals(db, strategy)
    [s] = _signals(db, strategy)
    item = signal_item(strategy, s)
    assert item["kind"] == "fill" and item["asset"] is True
    assert (item["ticker"], item["side"], item["action"]) == ("XETHZUSD", "long", "buy")
    assert item["qty"] == "0.50000000" and item["count"] == 0.5
    assert item["price"] == 3100.0 and item["price_str"] == "3100"
    assert item["notional"] == 1550.0
    # 1,550 of an 8,000 book.
    assert item["stake_fraction"] == pytest.approx(1_550 / 8_000)
    assert item["copy_lag_seconds"] == 60
    assert item["at"] == at(D, 12).isoformat()

    items = feed_items(db, [strategy])
    assert [i["ticker"] for i in items] == ["XETHZUSD"]


def test_unknown_equity_gives_no_stake_fraction(db, kraken):
    acct, strategy, eth = kraken
    sig = Signal(
        strategy_id=strategy.id,
        asset_fill_id=1,
        ticker="XETHZUSD",
        side="long",
        action="buy",
        qty_atoms=1,
        qty_decimals=8,
        price="1",
        notional_micros=1,
        creator_equity_micros=0,
        filled_at=at(D),
    )
    assert signal_item(strategy, sig)["stake_fraction"] is None
    assert signal_item(strategy, sig)["copy_lag_seconds"] is None


# ---- Postgres (migration 0013 columns, CHECK, the outer-join idempotency) -------------------


@pytest.mark.pg
def test_exit_numbers_on_postgres(pg_session_factory, quotes):
    # Own asset code and slug: the migrated database is shared by the session.
    with pg_session_factory() as db:
        acct, strategy = _account(
            db, slug="pg-sig", anchor=(("USD", 5_000 * M, 6), ("PGETH", ETH, 8))
        )
        inst = _instrument(db, "kraken", "PGETHUSD", "PGETH")
        _close(db, inst, DM1, 3_000)
        cash = [("USD", -1_550 * M, 6)]
        _fill(db, acct, inst, "pg1", "buy", "0.5", "3100", at(D, 12), quote_legs=cash)
        db.commit()
        assert emit_signals(db, strategy) == 1
        assert emit_signals(db, strategy) == 0
        [s] = _signals(db, strategy)
        assert (s.creator_equity_micros, s.notional_micros) == (8_000 * M, 1_550 * M)
        assert (s.position_before_atoms, s.position_after_atoms) == (ETH, 150_000_000)
        assert s.equity_basis_day == DM1 and s.quote_ask == "3100.5"

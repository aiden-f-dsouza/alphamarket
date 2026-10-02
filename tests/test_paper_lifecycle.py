"""Paper copying v2 lifecycle: perp funding, cross-margin liquidation, splits
and cash dividends (paper_lifecycle.py; methodology "Paper copying v2:
funding, liquidation, corporate actions").

Expected values are worked by hand in the comments (USD micros; 1 USD = M).
Rows are seeded straight through the ORM: funding_rates, corporate_actions,
asset_quotes and the follower's opening paper fill. prices.latest_mark is a
stub until the market-data stream lands, so these tests swap in
`_contract_latest_mark`, which implements its documented contract over
asset_quotes (newest row no older than quote_max_age_seconds; mid of bid/ask
when both, else last)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest
from sqlalchemy import select

from alphamarket import paper_book, paper_lifecycle, prices
from alphamarket.config import get_settings
from alphamarket.connectors.base import CorporateActionKind, InstrumentType, LegKind
from alphamarket.jobs import aware
from alphamarket.models import (
    Allocation,
    AllocationStatus,
    AssetQuote,
    CorporateActionRow,
    FundingRateRow,
    Instrument,
    MirrorAssetFill,
    MirrorLedgerEntry,
    PriceDaily,
    Strategy,
    User,
    VenueAccount,
)
from alphamarket.money import to_atoms, to_micros
from alphamarket.paper_book import LegSpec, cash_leg
from alphamarket.paper_lifecycle import (
    apply_lifecycle,
    dividend_micros,
    funding_payment_micros,
    maintenance_margin_rate,
    margin_check,
    split_delta_atoms,
)
from alphamarket.prices import Mark

M = 1_000_000
D = date(2026, 9, 10)
BTC_TIERS = [{"lower_bound": "0", "max_leverage": 40}]


def at(day: date, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def _contract_latest_mark(db, instrument_id, now):
    """prices.latest_mark as its docstring specifies, over asset_quotes."""
    oldest = now - timedelta(seconds=get_settings().quote_max_age_seconds)
    row = db.scalars(
        select(AssetQuote)
        .where(
            AssetQuote.instrument_id == instrument_id,
            AssetQuote.as_of <= now,
            AssetQuote.as_of >= oldest,
        )
        .order_by(AssetQuote.as_of.desc(), AssetQuote.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    if row.bid is not None and row.ask is not None:
        mid = (Decimal(row.bid) + Decimal(row.ask)) / 2
        return Mark(instrument_id, str(mid), aware(row.as_of), row.source, "mid")
    return Mark(instrument_id, row.last, aware(row.as_of), row.source, "last")


@pytest.fixture(autouse=True)
def _marks(monkeypatch):
    monkeypatch.setattr(prices, "latest_mark", _contract_latest_mark)


@pytest.fixture()
def db(session_factory):
    with session_factory() as session:
        yield session


def _user(db, email) -> User:
    user = User(email=email, password_hash="x", display_name=email[0])
    db.add(user)
    db.flush()
    return user


def _allocation(db, venue: str, cash_micros: int, tag: str = "") -> Allocation:
    name = f"{venue}{tag}"
    creator = _user(db, f"c-{name}@example.com")
    acct = VenueAccount(user_id=creator.id, venue=venue, venue_env="prod", key_id=f"k-{name}")
    db.add(acct)
    db.flush()
    strategy = Strategy(user_id=creator.id, venue_account_id=acct.id, name=name, slug=name)
    db.add(strategy)
    db.flush()
    follower = _user(db, f"f-{name}@example.com")
    alloc = Allocation(
        user_id=follower.id,
        strategy_id=strategy.id,
        allocation_micros=cash_micros,
        drawdown_limit_bps=9000,
        high_water_micros=cash_micros,
        started_at=at(D, 0),
    )
    db.add(alloc)
    db.commit()
    return alloc


def _instrument(db, venue, symbol, base, type_, decimals, tiers=None) -> Instrument:
    inst = Instrument(
        venue=venue,
        symbol=symbol,
        type=type_,
        base_asset=base,
        quote_asset="USD",
        qty_decimals=decimals,
        margin_tiers=tiers,
    )
    db.add(inst)
    db.flush()
    return inst


# ---- pure formulas ------------------------------------------------------------------------


def test_funding_payment_signs_and_rounding():
    q = Fraction(9_998, 10**5)  # 0.09998 BTC long
    # 0.09998 x 60,000 x 0.0000125 = 0.074985 exactly: the long pays.
    assert funding_payment_micros(q, Fraction(60_000), Fraction("0.0000125")) == -74_985
    # 0.09998 x 59,000 x 0.00001 = 0.0589882: the long receives, floored to 0.058988.
    assert funding_payment_micros(q, Fraction(59_000), Fraction("-0.00001")) == 58_988
    # A short of the same size pays the negative rate: -0.0589882 -> -0.058989 (up).
    assert funding_payment_micros(-q, Fraction(59_000), Fraction("-0.00001")) == -58_989


def test_maintenance_margin_tier_selection():
    tiers = [
        {"lower_bound": "0", "max_leverage": 40},
        {"lower_bound": "150000000", "max_leverage": 20},
    ]
    # Largest lower_bound <= notional; mmr = 1 / (2 x max_leverage).
    assert maintenance_margin_rate(tiers, Fraction(5_098_98, 100)) == Fraction(1, 80)
    assert maintenance_margin_rate(tiers, Fraction(149_999_999)) == Fraction(1, 80)
    assert maintenance_margin_rate(tiers, Fraction(150_000_000)) == Fraction(1, 40)
    assert maintenance_margin_rate(None, Fraction(1)) is None
    assert maintenance_margin_rate([], Fraction(1)) is None
    # No tier starts at or below the notional: no rate, never a guess.
    assert maintenance_margin_rate([{"lower_bound": "10", "max_leverage": 5}], Fraction(9)) is None


def test_margin_check_hand_computed():
    q, entry = Fraction(9_998, 10**5), Fraction(60_010)
    cash = to_micros("996.984103")
    # mark 51,000: AV = 996.984103 + 0.09998 x (51,000 - 60,010) = 996.984103 - 900.8198
    #   = 96.164303; MM = 0.09998 x 51,000 / 80 = 5,098.98 / 80 = 63.73725.
    ok = margin_check(cash, [(q, entry, Fraction(51_000), BTC_TIERS)])
    assert ok.account_value == Fraction("96.164303")
    assert ok.maintenance_margin == Fraction("63.73725")
    assert not ok.liquidate
    # mark 50,500: AV = 996.984103 - 950.8098 = 46.174303; MM = 5,048.99 / 80 = 63.112375.
    bad = margin_check(cash, [(q, entry, Fraction(50_500), BTC_TIERS)])
    assert bad.account_value == Fraction("46.174303")
    assert bad.maintenance_margin == Fraction("63.112375")
    assert bad.liquidate
    # Without tiers there is nothing to compare against: no check.
    assert margin_check(cash, [(q, entry, Fraction(50_500), None)]) is None


def test_split_and_dividend_formulas():
    assert split_delta_atoms(10 * 10**9, 2, 1) == 10 * 10**9  # 10 -> 20 shares
    assert split_delta_atoms(7, 1, 3) == -5  # 7 whole shares, 1:3 -> floor(2.33) = 2
    assert split_delta_atoms(10, 3, 2) == 5  # 10 -> 15
    assert dividend_micros(Fraction(20), Fraction("0.25")) == 5 * M
    # 3.333333333 shares x 0.37 = 1.23333333321 -> floored to 1.233333.
    assert dividend_micros(Fraction(3_333_333_333, 10**9), Fraction("0.37")) == 1_233_333
    with pytest.raises(ValueError):
        split_delta_atoms(10, 0, 1)


# ---- perps: funding and liquidation ------------------------------------------------------


def _seed_hl(db, tag: str = ""):
    """A Hyperliquid follower: $1,000, bought 0.09998 BTC-PERP at 60,010 on D
    at 12:00 with a 5 bps fee: 5,999.7998 x 0.0005 = 2.9999899 -> 2.9999
    rounded per the profile (the engine's job; seeded here as given), so
    cash 997.0001."""
    alloc = _allocation(db, "hyperliquid", 1_000 * M, tag)
    perp = _instrument(
        db, "hyperliquid", f"BTC{tag}", "BTC-PERP", InstrumentType.PERP, 5, BTC_TIERS
    )
    db.add(
        MirrorAssetFill(
            allocation_id=alloc.id,
            reason="signal",
            mode="paper",
            instrument_id=perp.id,
            action="buy",
            qty_atoms=9_998,
            qty_decimals=5,
            price="60010",
            notional_micros=to_micros("5999.7998"),
            fee_micros=to_micros("2.9999"),
            price_basis="ask",
            filled_at=at(D),
        )
    )
    paper_book.append_group(
        db,
        alloc,
        ref="sig:1",
        occurred_at=at(D),
        legs=[
            LegSpec("BTC-PERP", 9_998, 5, LegKind.FILL_BASE, perp.id),
            cash_leg(-to_micros("2.9999"), LegKind.FEE),
        ],
    )
    db.commit()
    assert paper_book.load_book(db, alloc).cash_micros == to_micros("997.0001")
    return alloc, perp


@pytest.fixture()
def hl(db):
    return _seed_hl(db)


def _funding(db, perp, when, rate, mark=None):
    db.add(
        FundingRateRow(
            instrument_id=perp.id, at=when, rate=rate, mark_price=mark, source="hyperliquid"
        )
    )
    db.commit()


def _quote(db, inst, when, last=None, bid=None, ask=None):
    db.add(AssetQuote(instrument_id=inst.id, bid=bid, ask=ask, last=last, as_of=when, source="hl"))
    db.commit()


def _cash(db, alloc) -> int:
    return paper_book.load_book(db, alloc).cash_micros


def test_hyperliquid_funding_then_liquidation_exit_test(db, hl):
    alloc, perp = hl
    # Before the open (ignored), and in the future at the time of the pass (not yet due).
    _funding(db, perp, at(D, 11), "0.01", "60000")
    _funding(db, perp, at(D, 13), "0.0000125", "60000")
    _funding(db, perp, at(D, 14), "-0.00001", "59000")
    _funding(db, perp, at(D, 16), "0.5", "59000")

    # 997.0001 - 0.074985 = 996.925115; + 0.058988 = 996.984103.
    assert apply_lifecycle(db, alloc, at(D, 15)) == 2
    db.commit()
    assert _cash(db, alloc) == to_micros("996.984103")
    legs = db.scalars(
        select(MirrorLedgerEntry)
        .where(MirrorLedgerEntry.kind == LegKind.FUNDING)
        .order_by(MirrorLedgerEntry.occurred_at)
    ).all()
    assert [(leg.delta_atoms, leg.ref) for leg in legs] == [
        (-74_985, f"fund:{perp.id}:{int(at(D, 13).timestamp())}"),
        (58_988, f"fund:{perp.id}:{int(at(D, 14).timestamp())}"),
    ]
    assert all(leg.instrument_id == perp.id for leg in legs)
    # A second pass applies nothing.
    assert apply_lifecycle(db, alloc, at(D, 15)) == 0

    # Fresh mark 51,000: AV 96.164303 >= MM 63.73725, no liquidation.
    _quote(db, perp, at(D, 15, 1), last="51000")
    assert apply_lifecycle(db, alloc, at(D, 15, 5)) == 0
    assert alloc.status == AllocationStatus.ACTIVE

    # Fresh mark 50,500 (mid of 50,499 / 50,501): AV 46.174303 < MM 63.112375.
    _quote(db, perp, at(D, 15, 10), bid="50499", ask="50501")
    now = at(D, 15, 12)
    assert apply_lifecycle(db, alloc, now) == 1
    db.commit()

    liq = db.scalars(select(MirrorLedgerEntry).where(MirrorLedgerEntry.ref.like("liq:%"))).all()
    by_kind = {leg.kind: leg for leg in liq}
    # Realized at the mark: 0.09998 x (50,500 - 60,010) = -950.8098.
    assert by_kind[LegKind.LIQUIDATION].delta_atoms == -950_809_800
    assert by_kind[LegKind.FILL_BASE].delta_atoms == -9_998
    assert {leg.ref for leg in liq} == {f"liq:{perp.id}:{int(now.timestamp())}"}
    book = paper_book.load_book(db, alloc)
    assert book.cash_micros == to_micros("46.174303")
    assert book.perps == {} and "BTC-PERP" not in book.holdings
    fill = db.scalars(select(MirrorAssetFill).where(MirrorAssetFill.reason == "liquidation")).one()
    assert (fill.action, fill.qty_atoms, fill.qty_decimals, fill.price, fill.price_basis) == (
        "sell",
        9_998,
        5,
        "50500",
        "mark",
    )
    assert fill.signal_id is None and fill.fee_micros == 0
    assert fill.notional_micros == to_micros("5048.99")
    assert aware(fill.filled_at) == now and aware(fill.quote_at) == at(D, 15, 10)
    assert alloc.status == AllocationStatus.STOPPED and alloc.stop_reason == "liquidated"

    # Idempotent: nothing left to fund (the D 16:00 row is due now, but the
    # position closed at 15:12) and nothing left to liquidate.
    assert apply_lifecycle(db, alloc, at(D, 17)) == 0
    assert _cash(db, alloc) == to_micros("46.174303")


def _fund_to_exit_state(db, alloc, perp):
    _funding(db, perp, at(D, 13), "0.0000125", "60000")
    _funding(db, perp, at(D, 14), "-0.00001", "59000")
    assert apply_lifecycle(db, alloc, at(D, 14, 30)) == 2
    db.commit()
    assert _cash(db, alloc) == to_micros("996.984103")


def test_stale_mark_means_no_check(db, hl, monkeypatch):
    alloc, perp = hl
    _fund_to_exit_state(db, alloc, perp)
    # 16 minutes old (> 15 min): latest_mark returns nothing, no check.
    _quote(db, perp, at(D, 15, 0), last="50500")
    assert apply_lifecycle(db, alloc, at(D, 15, 16)) == 0
    assert alloc.status == AllocationStatus.ACTIVE
    # Even if a mark source hands back a stale mark, the age is re-checked.
    stale = Mark(perp.id, "50500", at(D, 15, 0), "hl", "last")
    monkeypatch.setattr(prices, "latest_mark", lambda db, i, now: stale)
    assert apply_lifecycle(db, alloc, at(D, 15, 16)) == 0
    assert apply_lifecycle(db, alloc, at(D, 15, 15)) == 1  # exactly 15 min old: fresh
    assert alloc.stop_reason == "liquidated"


def test_null_margin_tiers_means_no_check(db, hl):
    alloc, perp = hl
    perp.margin_tiers = None
    db.commit()
    _fund_to_exit_state(db, alloc, perp)
    _quote(db, perp, at(D, 15, 10), last="50500")
    assert apply_lifecycle(db, alloc, at(D, 15, 12)) == 0
    assert alloc.status == AllocationStatus.ACTIVE
    assert paper_book.load_book(db, alloc).perps["BTC-PERP"].qty.atoms == 9_998


def test_a_stopped_allocation_keeps_its_reason_when_liquidated(db, hl):
    alloc, perp = hl
    alloc.status, alloc.stop_reason, alloc.stopped_at = AllocationStatus.STOPPED, "drawdown", at(D)
    db.commit()
    _fund_to_exit_state(db, alloc, perp)  # stopped allocations keep accruing funding
    _quote(db, perp, at(D, 15, 10), last="50500")
    assert apply_lifecycle(db, alloc, at(D, 15, 12)) == 1
    assert alloc.stop_reason == "drawdown"
    assert paper_book.load_book(db, alloc).perps == {}


def test_funding_mark_falls_back_to_the_quote_then_the_close(db, hl):
    alloc, perp = hl
    # 13:00 has no mark on the row: the quote at 12:55 (59,000) is used:
    # -0.09998 x 59,000 x 0.0001 = -0.589882.
    _quote(db, perp, at(D, 12, 55), last="59000")
    _funding(db, perp, at(D, 13), "0.0001")
    assert apply_lifecycle(db, alloc, at(D, 13, 30)) == 1
    db.commit()
    assert _cash(db, alloc) == to_micros("997.0001") - 589_882
    # Next day, no quote near 01:00: the last complete close (D, 58,000) is used:
    # -0.09998 x 58,000 x 0.0001 = -0.579884. (The D+1 close is not complete yet.)
    nxt = D + timedelta(days=1)
    for day, close in ((D, 58_000), (nxt, 1)):
        db.add(
            PriceDaily(
                instrument_id=perp.id,
                day=day,
                close=str(close),
                usd_micros_per_unit=close * M,
                source="hyperliquid",
            )
        )
    _funding(db, perp, at(nxt, 1), "0.0001")
    assert apply_lifecycle(db, alloc, at(nxt, 2)) == 1
    db.commit()
    assert _cash(db, alloc) == to_micros("997.0001") - 589_882 - 579_884


def test_funding_without_any_mark_is_skipped_not_guessed(db, hl):
    alloc, perp = hl
    _funding(db, perp, at(D, 13), "0.0001")
    assert apply_lifecycle(db, alloc, at(D, 14)) == 0
    assert _cash(db, alloc) == to_micros("997.0001")


def test_funding_watermark_is_the_latest_funding_leg(db, hl):
    alloc, perp = hl
    _funding(db, perp, at(D, 14), "0.0000125", "60000")
    assert apply_lifecycle(db, alloc, at(D, 15)) == 1
    db.commit()
    # A row that lands later but is dated before the watermark is not applied.
    _funding(db, perp, at(D, 13), "0.5", "60000")
    assert apply_lifecycle(db, alloc, at(D, 15)) == 0
    assert _cash(db, alloc) == to_micros("996.925115")


def test_a_short_receives_positive_funding(db):
    alloc = _allocation(db, "hyperliquid", 1_000 * M)
    eth = _instrument(db, "hyperliquid", "ETH", "ETH-PERP", InstrumentType.PERP, 4, BTC_TIERS)
    db.add(
        MirrorAssetFill(
            allocation_id=alloc.id,
            reason="signal",
            mode="paper",
            instrument_id=eth.id,
            action="sell",
            qty_atoms=5_000,
            qty_decimals=4,
            price="3000",
            notional_micros=1_500 * M,
            price_basis="bid",
            filled_at=at(D),
        )
    )
    paper_book.append_group(
        db,
        alloc,
        ref="sig:9",
        occurred_at=at(D),
        legs=[LegSpec("ETH-PERP", -5_000, 4, LegKind.FILL_BASE, eth.id)],
    )
    db.commit()
    # q = -0.5: -(-0.5) x 3,000 x 0.0000125 = +0.01875.
    _funding(db, eth, at(D, 13), "0.0000125", "3000")
    assert apply_lifecycle(db, alloc, at(D, 14)) == 1
    db.commit()
    assert _cash(db, alloc) == 1_000 * M + 18_750
    # Short above entry, mark 4,000: AV = 1,000.01875 - 500 = 500.01875 >= MM 25: no check fires.
    _quote(db, eth, at(D, 14), last="4000")
    assert apply_lifecycle(db, alloc, at(D, 14, 1)) == 0
    # Mark 4,990: AV = 1,000.01875 - 0.5 x 1,990 = 5.01875 < MM 0.5 x 4,990 / 80 = 31.1875.
    _quote(db, eth, at(D, 14, 2), last="4990")
    assert apply_lifecycle(db, alloc, at(D, 14, 3)) == 1
    db.commit()
    fill = db.scalars(select(MirrorAssetFill).where(MirrorAssetFill.reason == "liquidation")).one()
    assert (fill.action, fill.qty_atoms) == ("buy", 5_000)
    assert _cash(db, alloc) == to_micros("5.01875")


# ---- equities: splits and cash dividends ------------------------------------------------


@pytest.fixture()
def aapl(db):
    """An Alpaca follower with $10,000 that bought 10 AAPL for $1,500 on D."""
    alloc = _allocation(db, "alpaca", 10_000 * M)
    inst = _instrument(db, "alpaca", "AAPL", "AAPL", InstrumentType.EQUITY, 9)
    paper_book.append_group(
        db,
        alloc,
        ref="sig:1",
        occurred_at=at(D, 14),
        legs=[
            LegSpec("AAPL", to_atoms("10", 9), 9, LegKind.FILL_BASE, inst.id),
            cash_leg(-1_500 * M, LegKind.FILL_QUOTE),
        ],
    )
    db.commit()
    return alloc, inst


def _action(db, kind, ex, source="alpaca", symbol="AAPL", **kw) -> CorporateActionRow:
    row = CorporateActionRow(source=source, symbol=symbol, kind=kind, ex_date=ex, **kw)
    db.add(row)
    db.commit()
    return row


def test_split_then_dividend_exit_test(db, aapl):
    alloc, inst = aapl
    split = _action(db, CorporateActionKind.SPLIT, D + timedelta(days=5), ratio_new=2, ratio_old=1)
    # The same split reported by a second source is applied once.
    _action(db, CorporateActionKind.SPLIT, D + timedelta(days=5), "ibkr", ratio_new=2, ratio_old=1)
    div = _action(
        db,
        CorporateActionKind.CASH_DIVIDEND,
        D + timedelta(days=10),
        pay_date=D + timedelta(days=20),
        cash_per_share="0.25",
    )
    _action(db, CorporateActionKind.SYMBOL_CHANGE, D + timedelta(days=6), new_symbol="APLE")

    assert apply_lifecycle(db, alloc, at(D + timedelta(days=4), 23)) == 0  # nothing due
    assert apply_lifecycle(db, alloc, at(D + timedelta(days=5), 0)) == 1  # the split, at ex 00:00
    db.commit()
    book = paper_book.load_book(db, alloc)
    assert book.holdings["AAPL"].atoms == to_atoms("20", 9)  # 2:1 -> 20 shares
    assert book.cash_micros == 8_500 * M  # cost unchanged: a split moves no cash
    [leg] = db.scalars(select(MirrorLedgerEntry).where(MirrorLedgerEntry.ref == f"ca:{split.id}"))
    assert (leg.kind, leg.delta_atoms, aware(leg.occurred_at)) == (
        LegKind.SPLIT,
        to_atoms("10", 9),
        at(D + timedelta(days=5), 0),
    )

    # Bought on the ex-date itself: not entitled to the dividend.
    paper_book.append_group(
        db,
        alloc,
        ref="sig:2",
        occurred_at=at(D + timedelta(days=10), 15),
        legs=[
            LegSpec("AAPL", to_atoms("4", 9), 9, LegKind.FILL_BASE, inst.id),
            cash_leg(-300 * M, LegKind.FILL_QUOTE),
        ],
    )
    db.commit()
    # Past the ex-date, before the pay date: not credited yet.
    assert apply_lifecycle(db, alloc, at(D + timedelta(days=19), 23)) == 0
    assert _cash(db, alloc) == 8_200 * M
    # Pay date: 20 shares held at the end of ex - 1 x $0.25 = $5.00.
    assert apply_lifecycle(db, alloc, at(D + timedelta(days=20), 9)) == 1
    db.commit()
    assert _cash(db, alloc) == 8_205 * M
    [leg] = db.scalars(select(MirrorLedgerEntry).where(MirrorLedgerEntry.ref == f"ca:{div.id}"))
    assert (leg.kind, leg.delta_atoms, aware(leg.occurred_at)) == (
        LegKind.DIVIDEND,
        5 * M,
        at(D + timedelta(days=20), 0),
    )
    # A second pass applies nothing.
    assert apply_lifecycle(db, alloc, at(D + timedelta(days=30))) == 0
    assert _cash(db, alloc) == 8_205 * M
    assert paper_book.load_book(db, alloc).holdings["AAPL"].atoms == to_atoms("24", 9)


def test_reverse_split_drops_the_fraction_and_unheld_symbols_are_ignored(db):
    alloc = _allocation(db, "ibkr", 1_000 * M)
    inst = _instrument(db, "ibkr", "XYZ", "XYZ", InstrumentType.EQUITY, 0)
    paper_book.append_group(
        db,
        alloc,
        ref="sig:1",
        occurred_at=at(D, 14),
        legs=[
            LegSpec("XYZ", 7, 0, LegKind.FILL_BASE, inst.id),
            cash_leg(-70 * M, LegKind.FILL_QUOTE),
        ],
    )
    db.commit()
    # 7 shares, 1-for-3: floor(7 / 3) = 2 shares, the 1/3 share is dropped (no cash).
    _action(
        db, CorporateActionKind.SPLIT, D + timedelta(days=2), symbol="XYZ", ratio_new=1, ratio_old=3
    )
    _action(
        db,
        CorporateActionKind.SPLIT,
        D + timedelta(days=2),
        symbol="MSFT",
        ratio_new=2,
        ratio_old=1,
    )
    assert apply_lifecycle(db, alloc, at(D + timedelta(days=3))) == 1
    db.commit()
    book = paper_book.load_book(db, alloc)
    assert book.holdings["XYZ"].atoms == 2
    assert book.cash_micros == 930 * M


def test_dividend_on_a_position_sold_before_ex_date_pays_nothing(db, aapl):
    alloc, inst = aapl
    paper_book.append_group(
        db,
        alloc,
        ref="sig:2",
        occurred_at=at(D + timedelta(days=1), 15),
        legs=[
            LegSpec("AAPL", -to_atoms("10", 9), 9, LegKind.FILL_BASE, inst.id),
            cash_leg(1_510 * M, LegKind.FILL_QUOTE),
        ],
    )
    db.commit()
    _action(
        db,
        CorporateActionKind.CASH_DIVIDEND,
        D + timedelta(days=3),
        pay_date=D + timedelta(days=4),
        cash_per_share="0.25",
    )
    assert apply_lifecycle(db, alloc, at(D + timedelta(days=5))) == 0
    assert _cash(db, alloc) == 10_010 * M


def test_dividend_without_a_pay_date_waits(db, aapl):
    alloc, _ = aapl
    _action(db, CorporateActionKind.CASH_DIVIDEND, D + timedelta(days=3), cash_per_share="0.25")
    assert apply_lifecycle(db, alloc, at(D + timedelta(days=30))) == 0


def test_now_must_be_timezone_aware(db, aapl):
    alloc, _ = aapl
    with pytest.raises(ValueError):
        apply_lifecycle(db, alloc, datetime(2026, 9, 10, 12))


def test_no_events_for_an_empty_allocation(db):
    alloc = _allocation(db, "kraken", 1_000 * M)
    assert apply_lifecycle(db, alloc, at(D)) == 0
    assert paper_lifecycle.LIQUIDATED == "liquidated"


@pytest.mark.pg
def test_funding_and_liquidation_on_postgres(pg_session_factory):
    # The same exit numbers through Postgres (timestamptz, triggers, LIKE refs).
    with pg_session_factory() as db:
        alloc, perp = _seed_hl(db, "-lifecycle-pg")
        _fund_to_exit_state(db, alloc, perp)
        assert apply_lifecycle(db, alloc, at(D, 14, 30)) == 0
        _quote(db, perp, at(D, 15, 10), last="50500")
        assert apply_lifecycle(db, alloc, at(D, 15, 12)) == 1
        db.commit()
        assert _cash(db, alloc) == to_micros("46.174303")
        assert db.get(Allocation, alloc.id).stop_reason == "liquidated"
        assert apply_lifecycle(db, alloc, at(D, 16)) == 0

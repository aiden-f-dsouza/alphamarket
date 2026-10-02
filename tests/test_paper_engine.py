"""Paper copy v2 engine (paper.py): sizing, fill price, fees, skips and
follower valuation for ASSET strategies. Every expected number is worked out
by hand in the comments (USD; M = 1,000,000 micros). Methodology section:
"Paper copying v2: sizing, fill price, fees"."""

from datetime import UTC, date, datetime, timedelta
from fractions import Fraction

import pytest
from sqlalchemy import select

from alphamarket import paper, portfolio, prices
from alphamarket.connectors.base import Action, InstrumentType, LegKind, RegulatoryFees
from alphamarket.connectors.registry import US_EQUITY_REGULATORY_FEES, get_profile
from alphamarket.execution import (
    AssetPaperExecutor,
    LiveMirroringUnavailable,
    get_asset_executor,
)
from alphamarket.mirror import process_allocation
from alphamarket.models import (
    Allocation,
    AllocationStatus,
    AssetFill,
    Instrument,
    MirrorAssetFill,
    MirrorLedgerEntry,
    MirrorSkip,
    PriceDaily,
    Signal,
    Strategy,
    User,
    VenueAccount,
)
from alphamarket.money import to_atoms, to_micros

M = 1_000_000
D = date(2026, 9, 10)
T0 = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
START = datetime(2026, 9, 9, 0, 0, tzinfo=UTC)


# ---- fixtures ------------------------------------------------------------------------------------


@pytest.fixture()
def db(session_factory):
    with session_factory() as session:
        yield session


class World:
    """One creator account + strategy on `venue`, and helpers to add an
    instrument, signals and follower allocations."""

    def __init__(self, db, venue: str):
        self.db = db
        self.venue = venue
        self.creator = User(email=f"c-{venue}@example.com", password_hash="x", display_name="C")
        db.add(self.creator)
        db.flush()
        self.account = VenueAccount(
            user_id=self.creator.id, venue=venue, venue_env="prod", key_id="k"
        )
        db.add(self.account)
        db.flush()
        self.strategy = Strategy(
            user_id=self.creator.id, venue_account_id=self.account.id, name=venue, slug=venue
        )
        db.add(self.strategy)
        db.flush()
        self._n = 0
        self._followers = 0

    def instrument(self, symbol, base, type_=InstrumentType.SPOT, decimals=8, quote="USD", **kw):
        inst = Instrument(
            venue=self.venue,
            symbol=symbol,
            type=type_,
            base_asset=base,
            quote_asset=quote,
            qty_decimals=decimals,
            **kw,
        )
        self.db.add(inst)
        self.db.flush()
        return inst

    def allocation(self, dollars: str, drawdown_limit_bps: int = 1500) -> Allocation:
        self._followers += 1
        user = User(
            email=f"f{self._followers}-{self.venue}@example.com",
            password_hash="x",
            display_name="F",
        )
        self.db.add(user)
        self.db.flush()
        alloc = Allocation(
            user_id=user.id,
            strategy_id=self.strategy.id,
            allocation_micros=to_micros(dollars),
            drawdown_limit_bps=drawdown_limit_bps,
            high_water_micros=to_micros(dollars),
            started_at=START,
        )
        self.db.add(alloc)
        self.db.commit()
        return alloc

    def signal(
        self,
        inst: Instrument,
        action: str,
        qty: str,
        price: str,
        *,
        before: str | None,
        after: str,
        equity: str = "10000",
        bid: str | None = None,
        ask: str | None = None,
        filled_at: datetime = T0,
        lag: timedelta = timedelta(seconds=30),
    ) -> Signal:
        self._n += 1
        dec = inst.qty_decimals
        fill = AssetFill(
            venue_account_id=self.account.id,
            instrument_id=inst.id,
            venue_fill_id=f"f{self._n}",
            action=action,
            qty_atoms=to_atoms(qty, dec),
            qty_decimals=dec,
            price=price,
            filled_at=filled_at,
            ingested_at=filled_at + lag,
        )
        self.db.add(fill)
        self.db.flush()
        position_after = to_atoms(after, dec)
        sig = Signal(
            strategy_id=self.strategy.id,
            asset_fill_id=fill.id,
            instrument_id=inst.id,
            ticker=inst.symbol,
            side="long" if position_after > 0 or before not in (None, "0") else "short",
            action=action,
            qty_atoms=to_atoms(qty, dec),
            qty_decimals=dec,
            price=price,
            creator_equity_micros=to_micros(equity),
            position_before_atoms=to_atoms(before, dec) if before is not None else None,
            position_after_atoms=position_after,
            filled_at=filled_at,
            observed_at=filled_at + lag,
            quote_bid=bid,
            quote_ask=ask,
            quote_at=filled_at + lag if (bid or ask) else None,
            quote_source=self.venue if (bid or ask) else None,
        )
        self.db.add(sig)
        self.db.commit()
        return sig


def fills_of(db, alloc) -> list[MirrorAssetFill]:
    return db.scalars(
        select(MirrorAssetFill)
        .where(MirrorAssetFill.allocation_id == alloc.id)
        .order_by(MirrorAssetFill.id)
    ).all()


def skips_of(db, alloc) -> dict[int, str]:
    rows = db.scalars(select(MirrorSkip).where(MirrorSkip.allocation_id == alloc.id)).all()
    return {r.signal_id: r.reason for r in rows}


def legs_of(db, alloc, ref) -> dict[str, int]:
    rows = db.scalars(
        select(MirrorLedgerEntry).where(
            MirrorLedgerEntry.allocation_id == alloc.id, MirrorLedgerEntry.ref == ref
        )
    ).all()
    return {f"{r.kind}:{r.asset}": r.delta_atoms for r in rows}


def book(db, alloc):
    from alphamarket.paper_book import load_book

    return load_book(db, alloc)


@pytest.fixture()
def kraken(db):
    w = World(db, "kraken")
    # Kraken XBT/USD: lot_decimals 8, ordermin 0.0001 BTC = 10,000 atoms.
    w.btc = w.instrument("XXBTZUSD", "BTC", min_qty_atoms=10_000, min_notional="0.5")
    return w


@pytest.fixture()
def hyperliquid(db):
    w = World(db, "hyperliquid")
    # Hyperliquid BTC perp: szDecimals 5, $10 minimum order value, USDC margin.
    w.perp = w.instrument(
        "BTC", "BTC-PERP", InstrumentType.PERP, decimals=5, quote="USDC", min_notional="10"
    )
    return w


@pytest.fixture()
def alpaca(db):
    w = World(db, "alpaca")
    # Fractionable US equity: 9 dp, $1 minimum notional.
    w.aapl = w.instrument(
        "AAPL", "AAPL", InstrumentType.EQUITY, decimals=9, min_qty_atoms=1, min_notional="1"
    )
    return w


# ---- exit tests: Kraken spot (80 bps, fees to $0.0001) -------------------------------------------


def test_kraken_spot_buy_then_partial_sell(db, kraken):
    alloc = kraken.allocation("1000")
    # Creator (equity $10,000) buys 0.1 BTC at 60,000; quote at observed_at
    # bid 59,990 / ask 60,030. Follower $1,000:
    #   q = 0.1 x 1,000 / 10,000 = 0.01 BTC at the ASK 60,030
    #   notional = 0.01 x 60,030 = 600.30; fee = 0.8% = 4.8024 (to $0.0001)
    #   cash = 1,000 - 600.30 - 4.8024 = 394.8976
    s1 = kraken.signal(
        kraken.btc, "buy", "0.1", "60000", before="0", after="0.1", bid="59990", ask="60030"
    )
    assert process_allocation(db, alloc) == (1, 0)
    [f1] = fills_of(db, alloc)
    assert (f1.action, f1.qty_atoms, f1.qty_decimals, f1.price, f1.price_basis) == (
        "buy",
        1_000_000,
        8,
        "60030",
        "ask",
    )
    assert f1.notional_micros == 600_300_000
    assert f1.fee_micros == 4_802_400
    assert f1.signal_id == s1.id and f1.mode == "paper" and f1.reason == "signal"
    assert f1.penalty_bps is None
    assert f1.filled_at.replace(tzinfo=UTC) == T0 + timedelta(seconds=30)  # observed_at (E5)
    assert legs_of(db, alloc, f"sig:{s1.id}") == {
        "fill_base:BTC": 1_000_000,
        "fill_quote:USD": -600_300_000,
        "fee:USD": -4_802_400,
    }
    assert book(db, alloc).cash_micros == to_micros("394.8976")
    assert alloc.last_signal_id == s1.id

    # Creator sells 0.04 of its 0.1 (fraction 0.4); bid 60,990.5:
    #   q = 0.01 x 0.04 / 0.1 = 0.004 BTC at the BID
    #   proceeds = 0.004 x 60,990.5 = 243.962; fee 0.8% = 1.951696 -> 1.9517
    #   cash = 394.8976 + 243.962 - 1.9517 = 636.9079; position 0.006 BTC
    s2 = kraken.signal(
        kraken.btc,
        "sell",
        "0.04",
        "61000",
        before="0.1",
        after="0.06",
        bid="60990.5",
        ask="61010",
        filled_at=T0 + timedelta(hours=1),
    )
    assert process_allocation(db, alloc) == (1, 0)
    f2 = fills_of(db, alloc)[1]
    assert (f2.action, f2.qty_atoms, f2.price, f2.price_basis) == (
        "sell",
        400_000,
        "60990.5",
        "bid",
    )
    assert f2.notional_micros == 243_962_000
    assert f2.fee_micros == 1_951_700
    b = book(db, alloc)
    assert b.cash_micros == to_micros("636.9079")
    assert b.holdings["BTC"].atoms == 600_000
    assert alloc.last_signal_id == s2.id


def test_tiny_follower_is_skipped_below_minimum_size(db, kraken):
    # $100 follower of a $1,000,000 creator buying 0.1 BTC:
    #   q = 0.1 x 100 / 1,000,000 = 0.00001 BTC = 1,000 atoms < ordermin 10,000
    alloc = kraken.allocation("100")
    s = kraken.signal(
        kraken.btc, "buy", "0.1", "60000", before="0", after="0.1", equity="1000000", ask="60030"
    )
    assert process_allocation(db, alloc) == (0, 0)
    assert fills_of(db, alloc) == []
    assert skips_of(db, alloc) == {s.id: "below_minimum_size"}
    assert alloc.last_signal_id == s.id
    assert book(db, alloc).cash_micros == 100 * M


def test_no_quote_falls_back_to_creator_price_with_penalty(db, kraken):
    alloc = kraken.allocation("1000")
    # No quote: buy at 60,000 x (1 + 50/10,000) = 60,300.
    #   q = 0.01; cost = 603.00; fee 0.8% = 4.824 -> cash 1,000 - 607.824 = 392.176
    kraken.signal(kraken.btc, "buy", "0.1", "60000", before="0", after="0.1")
    # Creator sells everything at 61,000, still no quote: 61,000 x 0.995 = 60,695.
    #   q = all 0.01; proceeds 606.95; fee 4.8556 -> cash 392.176 + 606.95 - 4.8556 = 994.2704
    kraken.signal(
        kraken.btc,
        "sell",
        "0.1",
        "61000",
        before="0.1",
        after="0",
        filled_at=T0 + timedelta(hours=1),
    )
    assert process_allocation(db, alloc) == (2, 0)
    buy, sell = fills_of(db, alloc)
    assert (buy.price, buy.price_basis, buy.penalty_bps, buy.quote_at) == (
        "60300",
        "creator_price_penalty",
        50,
        None,
    )
    assert (buy.notional_micros, buy.fee_micros) == (603 * M, 4_824_000)
    assert (sell.price, sell.price_basis, sell.qty_atoms) == (
        "60695",
        "creator_price_penalty",
        10**6,
    )
    assert (sell.notional_micros, sell.fee_micros) == (606_950_000, 4_855_600)
    b = book(db, alloc)
    assert b.cash_micros == to_micros("994.2704")
    assert "BTC" not in b.holdings


def test_paper_price_uses_the_quote_side_or_the_penalty():
    profile = get_profile("kraken")
    sig = Signal(price="100", quote_bid="99", quote_ask=None, quote_at=T0)
    # A sell takes the bid; a buy with no ask falls back: 100 x 1.005 = 100.5.
    assert paper.paper_price(sig, Action.SELL, profile).price == "99"
    assert paper.paper_price(sig, Action.BUY, profile) == paper.PaperPrice(
        "100.5", "creator_price_penalty", 50, None
    )
    # Sell fallback: 0.3 x 0.995 = 0.2985 (exact decimal string).
    assert paper.paper_price(Signal(price="0.3"), Action.SELL, profile).price == "0.2985"


# ---- exit test: US equity regulatory fees --------------------------------------------------------


def test_us_equity_sell_pays_regulatory_fees_from_the_profile(db, alpaca):
    alloc = alpaca.allocation("2000")
    # Creator ($10,000) buys 100 AAPL at 80; follower $2,000: q = 100 x 0.2 = 20
    # shares at the ask 80.00; cost 1,600.00; buys pay no fee -> cash 400.00.
    alpaca.signal(
        alpaca.aapl, "buy", "100", "80", before="0", after="100", bid="79.99", ask="80.00"
    )
    # Creator sells all 100 at the bid 80.00; the follower sells all 20:
    #   proceeds 1,600.00
    #   SEC  = ceil_cent(1,600 x 0.00 / 1e6) = 0.00   (profile rate $0.00 per million)
    #   TAF  = ceil_cent(min(20 x 0.000166, 8.30)) = ceil_cent(0.00332) = 0.01
    #   cash = 400 + 1,600 - 0.01 = 1,999.99
    alpaca.signal(
        alpaca.aapl,
        "sell",
        "100",
        "80",
        before="100",
        after="0",
        bid="80.00",
        ask="80.02",
        filled_at=T0 + timedelta(hours=1),
    )
    assert process_allocation(db, alloc) == (2, 0)
    buy, sell = fills_of(db, alloc)
    assert (buy.qty_atoms, buy.notional_micros, buy.fee_micros) == (20 * 10**9, 1_600 * M, 0)
    assert (sell.qty_atoms, sell.notional_micros, sell.fee_micros) == (
        20 * 10**9,
        1_600 * M,
        10_000,
    )
    assert book(db, alloc).cash_micros == to_micros("1999.99")
    assert get_profile("alpaca").regulatory_fees is US_EQUITY_REGULATORY_FEES


def test_regulatory_fee_formula_rounds_each_fee_up_to_the_cent_and_caps_taf():
    fees = RegulatoryFees(
        sec_fee_per_million="27.80", taf_per_share="0.000166", taf_max="8.30", effective="t"
    )
    shares = 10**9  # 9 dp
    # 20 @ 80: SEC 1,600 x 27.80 / 1e6 = 0.04448 -> 0.05; TAF 0.00332 -> 0.01; total 0.06.
    assert paper.regulatory_fee_micros(fees, Action.SELL, 20 * shares, 9, "80") == 60_000
    # 100,000 @ 1: SEC 2.78 exactly; TAF 16.60 capped at 8.30; total 11.08.
    assert paper.regulatory_fee_micros(fees, Action.SELL, 100_000 * shares, 9, "1") == 11_080_000
    # Buys pay neither.
    assert paper.regulatory_fee_micros(fees, Action.BUY, 20 * shares, 9, "80") == 0


# ---- exit test: perps (Hyperliquid 5 bps, fees to the micro) -------------------------------------


def test_perp_open_then_flip(db, hyperliquid):
    alloc = hyperliquid.allocation("1000")
    # Open: creator ($10,000) buys 1 BTC-PERP at 60,000 (notional 60,000 = 6x).
    # The follower mirrors the NOTIONAL: 60,000 x 1,000 / 10,000 = 6,000 at the
    # ask 60,010 -> q = 6,000 / 60,010 = 0.0999833 -> 0.09998 (5 dp).
    #   notional 0.09998 x 60,010 = 5,999.7998 (margin, not spent)
    #   fee 5 bps = 2.9998999 -> 2.999900; cash = 1,000 - 2.9999 = 997.0001
    s1 = hyperliquid.signal(
        hyperliquid.perp, "buy", "1", "60000", before="0", after="1", bid="59990", ask="60010"
    )
    assert process_allocation(db, alloc) == (1, 0)
    [f1] = fills_of(db, alloc)
    assert (f1.action, f1.qty_atoms, f1.price, f1.notional_micros, f1.fee_micros) == (
        "buy",
        9_998,
        "60010",
        5_999_799_800,
        2_999_900,
    )
    assert legs_of(db, alloc, f"sig:{s1.id}") == {
        "fill_base:BTC-PERP": 9_998,
        "fee:USD": -2_999_900,
    }
    b = book(db, alloc)
    assert b.cash_micros == to_micros("997.0001")
    assert b.perps["BTC-PERP"].qty.atoms == 9_998 and b.perps["BTC-PERP"].entry == 60_010

    # Flip: creator sells 3 at 61,000 (1 long -> 2 short), equity $10,000;
    # quote bid 60,990. Follower equity F = 997.0001 (no mark: the perp adds 0).
    #   close all 0.09998 (the creator closed all of its long)
    #   open  2 x 61,000 x 997.0001 / 10,000 / 60,990 = 0.1994327 -> 0.19943
    #   one sell of 0.29941 at 60,990: notional 18,261.0159
    #   realized = 0.09998 x (60,990 - 60,010) = 97.9804
    #   fee = 18,261.0159 x 5 bps = 9.13050795 -> 9.130508
    #   cash = 997.0001 + 97.9804 - 9.130508 = 1,085.849992
    s2 = hyperliquid.signal(
        hyperliquid.perp,
        "sell",
        "3",
        "61000",
        before="1",
        after="-2",
        bid="60990",
        ask="61010",
        filled_at=T0 + timedelta(hours=1),
    )
    assert process_allocation(db, alloc) == (1, 0)
    f2 = fills_of(db, alloc)[1]
    assert (f2.action, f2.qty_atoms, f2.price, f2.notional_micros, f2.fee_micros) == (
        "sell",
        29_941,
        "60990",
        18_261_015_900,
        9_130_508,
    )
    assert legs_of(db, alloc, f"sig:{s2.id}") == {
        "fill_base:BTC-PERP": -29_941,  # ONE leg: the net signed delta
        "realized_pnl:USD": 97_980_400,
        "fee:USD": -9_130_508,
    }
    b = book(db, alloc)
    assert b.cash_micros == to_micros("1085.849992")
    position = b.perps["BTC-PERP"]
    assert position.qty.atoms == -19_943 and position.entry == 60_990  # entry resets on a flip
    assert b.holdings["BTC-PERP"].atoms == -19_943  # legs agree with the fills


def test_perp_partial_reduce_realizes_a_loss_rounded_against_the_follower(db, hyperliquid):
    alloc = hyperliquid.allocation("1000")
    hyperliquid.signal(hyperliquid.perp, "buy", "1", "60000", before="0", after="1", ask="60010")
    # Creator sells 0.5 of 1 at a loss; bid 59,000.12345:
    #   q = 0.09998 x 0.5 = 0.04999
    #   realized = 0.04999 x (59,000.12345 - 60,010) = -50.4837287345 -> floor -50.483729
    #   notional 0.04999 x 59,000.12345 = 2,949.4161712655 -> down 2,949.416171
    #   fee 5 bps of 2,949.4161712655 = 1.47470808... -> 1.474708
    #   cash = 997.0001 - 50.483729 - 1.474708 = 945.041663
    s2 = hyperliquid.signal(
        hyperliquid.perp,
        "sell",
        "0.5",
        "59000",
        before="1",
        after="0.5",
        bid="59000.12345",
        filled_at=T0 + timedelta(hours=1),
    )
    process_allocation(db, alloc)
    assert legs_of(db, alloc, f"sig:{s2.id}") == {
        "fill_base:BTC-PERP": -4_999,
        "realized_pnl:USD": -50_483_729,
        "fee:USD": -1_474_708,
    }
    b = book(db, alloc)
    assert b.cash_micros == to_micros("945.041663")
    assert b.perps["BTC-PERP"].qty.atoms == 4_999 and b.perps["BTC-PERP"].entry == 60_010


def test_perp_order_against_a_diverged_follower_position_realizes_pnl(db, hyperliquid):
    # The follower opened long 0.09998 @ 60,010 (cash 997.0001); the creator's
    # close was observed too late and skipped, so the follower is still long
    # while the creator is flat. The creator then opens SHORT 0.5 at 61,000
    # (before 0), bid 60,990. F = 997.0001 (the perp is unpriced):
    #   q = 0.5 x 61,000 x 997.0001 / 10,000 / 60,990 = 0.0498581... -> 0.04985
    # That sell runs against the follower's long, so it CLOSES 0.04985:
    #   realized = 0.04985 x (60,990 - 60,010) = 48.853
    #   fee = 0.04985 x 60,990 x 5 bps = 1.52017575 -> 1.520176
    #   cash = 997.0001 + 48.853 - 1.520176 = 1,044.332924; long 0.05013 left
    alloc = hyperliquid.allocation("1000")
    hyperliquid.signal(hyperliquid.perp, "buy", "1", "60000", before="0", after="1", ask="60010")
    stale = hyperliquid.signal(
        hyperliquid.perp, "sell", "1", "61000", before="1", after="0", bid="60990",
        filled_at=T0 + timedelta(hours=1), lag=timedelta(hours=6, seconds=1),
    )  # fmt: skip
    s3 = hyperliquid.signal(
        hyperliquid.perp, "sell", "0.5", "61000", before="0", after="-0.5", bid="60990",
        filled_at=T0 + timedelta(hours=2),
    )  # fmt: skip
    assert process_allocation(db, alloc) == (2, 0)
    assert skips_of(db, alloc) == {stale.id: "stale_signal"}
    assert legs_of(db, alloc, f"sig:{s3.id}") == {
        "fill_base:BTC-PERP": -4_985,
        "realized_pnl:USD": 48_853_000,
        "fee:USD": -1_520_176,
    }
    b = book(db, alloc)
    assert b.cash_micros == to_micros("1044.332924")
    assert b.perps["BTC-PERP"].qty.atoms == 5_013 and b.perps["BTC-PERP"].entry == 60_010


def test_spot_partial_sell_below_minimum_is_skipped_but_a_full_exit_is_not(db, kraken):
    # $15 follower of a $10,000 creator buying 0.1 BTC at the ask 60,030:
    #   q = 0.1 x 15 / 10,000 = 0.00015 BTC (15,000 atoms >= ordermin 10,000)
    #   cost 9.0045; fee 0.8% = 0.072036 -> 0.0720; cash 15 - 9.0765 = 5.9235
    alloc = kraken.allocation("15")
    kraken.signal(kraken.btc, "buy", "0.1", "60000", before="0", after="0.1", ask="60030")
    # Creator sells 40%: the follower would sell 6,000 atoms < 10,000 -> skipped.
    partial = kraken.signal(
        kraken.btc, "sell", "0.04", "61000", before="0.1", after="0.06", bid="60990.5",
        filled_at=T0 + timedelta(hours=1),
    )  # fmt: skip
    # Creator sells the rest (flat): the follower exits all 0.00015 at 60,990.5:
    #   proceeds 9.148575; fee 0.8% = 0.0731886 -> 0.0732
    #   cash = 5.9235 + 9.148575 - 0.0732 = 14.998875
    full = kraken.signal(
        kraken.btc, "sell", "0.06", "61000", before="0.06", after="0", bid="60990.5",
        filled_at=T0 + timedelta(hours=2),
    )  # fmt: skip
    assert process_allocation(db, alloc) == (2, 0)
    assert skips_of(db, alloc) == {partial.id: "below_minimum_size"}
    sell = fills_of(db, alloc)[1]
    assert (sell.signal_id, sell.qty_atoms, sell.notional_micros, sell.fee_micros) == (
        full.id,
        15_000,
        9_148_575,
        73_200,
    )
    b = book(db, alloc)
    assert b.cash_micros == to_micros("14.998875")
    assert "BTC" not in b.holdings


def test_perp_skip_reasons(db, hyperliquid):
    alloc = hyperliquid.allocation("1000")
    # A perp signal without the creator's position: open vs close is unknown.
    unknown = hyperliquid.signal(
        hyperliquid.perp, "buy", "1", "60000", before=None, after="1", ask="60010"
    )
    # A creator reducing a position the follower never had.
    reduce = hyperliquid.signal(
        hyperliquid.perp, "sell", "0.5", "60000", before="1", after="0.5", bid="59990"
    )
    # A $1,000,000 creator opening 0.001 BTC: 0.001 x 60,000 x 1,000 / 1,000,000
    # = $0.06 of notional -> 0 atoms at 5 dp, under the $10 minimum.
    tiny = hyperliquid.signal(
        hyperliquid.perp, "buy", "0.001", "60000", before="1", after="1.001",
        equity="1000000", ask="60010",
    )  # fmt: skip
    assert process_allocation(db, alloc) == (0, 0)
    assert skips_of(db, alloc) == {
        unknown.id: "creator_position_unknown",
        reduce.id: "no_position",
        tiny.id: "below_minimum_size",
    }


# ---- exit tests: stale signals, live executor, drawdown on a pure buy ----------------------------


def test_stale_signal_is_skipped(db, kraken):
    alloc = kraken.allocation("1000")
    six_hours = timedelta(hours=6)
    late = kraken.signal(
        kraken.btc, "buy", "0.1", "60000", before="0", after="0.1", ask="60030",
        lag=six_hours + timedelta(seconds=1),
    )  # fmt: skip
    on_time = kraken.signal(
        kraken.btc, "buy", "0.1", "60000", before="0.1", after="0.2", ask="60030",
        filled_at=T0 + timedelta(minutes=1), lag=six_hours,
    )  # fmt: skip
    assert process_allocation(db, alloc) == (1, 0)
    assert skips_of(db, alloc) == {late.id: "stale_signal"}
    [fill] = fills_of(db, alloc)
    assert fill.signal_id == on_time.id


def test_daily_venues_allow_three_days_of_lag():
    assert get_profile("ibkr").max_copy_lag_seconds == 3 * 24 * 3600
    assert get_profile("kraken").max_copy_lag_seconds == 6 * 3600


def test_live_asset_executor_is_refused():
    assert isinstance(get_asset_executor("paper"), AssetPaperExecutor)
    with pytest.raises(LiveMirroringUnavailable):
        get_asset_executor("live")


def test_live_allocation_mirrors_nothing(db, kraken):
    alloc = kraken.allocation("1000")
    kraken.signal(kraken.btc, "buy", "0.1", "60000", before="0", after="0.1", ask="60030")
    alloc.mode = "live"  # cannot be created through the API; refused here anyway
    db.commit()
    with pytest.raises(LiveMirroringUnavailable):
        process_allocation(db, alloc)
    assert fills_of(db, alloc) == []


def test_snapshot_after_a_pure_buy_does_not_trip_the_drawdown_stop(db, kraken):
    # 1% limit. After the buy: cash 394.8976 + BTC at cost 600.30 = 995.1976
    # (no quote, no close): drawdown 0.48% (the fee) < 1%.
    alloc = kraken.allocation("1000", drawdown_limit_bps=100)
    kraken.signal(kraken.btc, "buy", "0.1", "60000", before="0", after="0.1", ask="60030")
    process_allocation(db, alloc)
    snap = portfolio.snapshot_allocation(db, alloc)
    assert snap.equity_micros == to_micros("995.1976")
    assert snap.open_cost_micros == snap.open_value_micros == 600_300_000
    assert snap.unmarked_cost_micros == 600_300_000
    assert alloc.status == AllocationStatus.ACTIVE


# ---- sizing branches -----------------------------------------------------------------------------


def test_buy_is_capped_by_cash_including_the_fee(db, kraken):
    # Creator equity $1,000 buys 1 BTC; follower $1,000 wants 1 BTC at 60,030.
    # Largest q (8 dp) with cost_up(q) + fee(q) <= 1,000; about
    # 1,000 / (60,030 x 1.008) = 0.0165261287 BTC:
    #   q = 0.01652613: cost 992.0635839 -> 992.063584; fee 0.8% = 7.93650867
    #       -> 7.9365; total 1,000.000084 > 1,000      (does not fit)
    #   q = 0.01652612: cost 992.0629836 -> 992.062984; fee 7.93650387
    #       -> 7.9365; total 999.999484 <= 1,000       (fits) -> cash 0.000516
    alloc = kraken.allocation("1000")
    kraken.signal(
        kraken.btc, "buy", "1", "60000", before="0", after="1", equity="1000", ask="60030"
    )
    process_allocation(db, alloc)
    [fill] = fills_of(db, alloc)
    assert fill.qty_atoms == 1_652_612
    assert (fill.notional_micros, fill.fee_micros) == (992_062_984, 7_936_500)
    assert book(db, alloc).cash_micros == 516
    profile = get_profile("kraken")
    assert paper.max_affordable_atoms(1_652_613, 1_000 * M, kraken.btc, "60030", profile) == (
        1_652_612
    )


def test_skip_reasons(db, kraken):
    alloc = kraken.allocation("5")
    usd_btc = kraken.btc
    eth_btc = kraken.instrument("XETHXXBT", "ETH", quote="BTC")
    signals = {
        # $5 vs a $100 creator buying 0.1 BTC: wants 0.005 BTC ($300); cash buys
        # 5 / (60,030 x 1.008) = 0.0000826 BTC < ordermin 0.0001.
        "insufficient_cash": kraken.signal(
            usd_btc, "buy", "0.1", "60000", before="0", after="0.1", equity="100", ask="60030"
        ),
        "non_usd_quote": kraken.signal(
            eth_btc, "buy", "1", "0.05", before="0", after="1", ask="0.05"
        ),
        "creator_equity_unknown": kraken.signal(
            usd_btc, "buy", "0.1", "60000", before="0", after="0.1", equity="0", ask="60030"
        ),
        "no_position": kraken.signal(
            usd_btc, "sell", "0.1", "60000", before="0.1", after="0", bid="59990"
        ),
    }
    # A sell emitted without the creator's position: the fraction is unknown.
    unknown = kraken.signal(usd_btc, "sell", "0.1", "60000", before=None, after="0", bid="1")
    assert process_allocation(db, alloc) == (0, 0)
    expected = {s.id: reason for reason, s in signals.items()}
    expected[unknown.id] = "creator_position_unknown"
    assert skips_of(db, alloc) == expected
    view = portfolio.allocation_view(db, alloc)
    assert view["skipped"] == {
        "creator_equity_unknown": 1,
        "creator_position_unknown": 1,
        "insufficient_cash": 1,
        "no_position": 1,
        "non_usd_quote": 1,
    }


def test_buy_sizes_by_follower_equity_at_the_mark(db, kraken, monkeypatch):
    alloc = kraken.allocation("1000")
    kraken.signal(kraken.btc, "buy", "0.1", "60000", before="0", after="0.1", ask="60030")
    process_allocation(db, alloc)  # cash 394.8976, 0.01 BTC
    # BTC is now quoted at 70,000: F = 394.8976 + 0.01 x 70,000 = 1,094.8976.
    monkeypatch.setattr(
        prices, "latest_mark", lambda db, iid, now: prices.Mark(iid, "70000", T0, "kraken", "mid")
    )
    # Creator ($10,000) buys 0.02 more at the ask 70,000:
    #   q = 0.02 x 1,094.8976 / 10,000 = 0.0021897952 -> 0.00218979
    #   cost 0.00218979 x 70,000 = 153.2853; fee 0.8% = 1.2262824 -> 1.2263
    #   cash = 394.8976 - 153.2853 - 1.2263 = 240.386
    kraken.signal(
        kraken.btc,
        "buy",
        "0.02",
        "70000",
        before="0.1",
        after="0.12",
        ask="70000",
        filled_at=T0 + timedelta(hours=1),
    )
    process_allocation(db, alloc)
    assert fills_of(db, alloc)[1].qty_atoms == 218_979
    assert book(db, alloc).cash_micros == to_micros("240.386")


def test_signals_before_the_start_are_passed_over_silently_and_reruns_add_nothing(db, kraken):
    alloc = kraken.allocation("1000")
    early = kraken.signal(
        kraken.btc,
        "buy",
        "0.1",
        "60000",
        before="0",
        after="0.1",
        ask="60030",
        filled_at=START - timedelta(seconds=1),
    )
    s = kraken.signal(kraken.btc, "buy", "0.1", "60000", before="0.1", after="0.2", ask="60030")
    assert process_allocation(db, alloc) == (1, 0)
    assert process_allocation(db, alloc) == (0, 0)
    assert [f.signal_id for f in fills_of(db, alloc)] == [s.id]
    assert skips_of(db, alloc) == {}
    assert alloc.last_signal_id == s.id > early.id


def test_stopped_allocation_copies_nothing_but_runs_the_lifecycle(db, kraken, monkeypatch):
    calls = []

    def lifecycle(db, allocation, now):
        calls.append(allocation.id)
        return 2

    monkeypatch.setattr("alphamarket.paper_lifecycle.apply_lifecycle", lifecycle)
    alloc = kraken.allocation("1000")
    alloc.status = AllocationStatus.STOPPED
    db.commit()
    kraken.signal(kraken.btc, "buy", "0.1", "60000", before="0", after="0.1", ask="60030")
    assert process_allocation(db, alloc) == (0, 2)
    assert calls == [alloc.id]
    assert fills_of(db, alloc) == [] and alloc.last_signal_id == 0


# ---- valuation and the portfolio view ------------------------------------------------------------


def test_valuation_prefers_a_fresh_quote_then_the_last_close_then_cost(db, kraken, monkeypatch):
    alloc = kraken.allocation("1000")
    kraken.signal(kraken.btc, "buy", "0.1", "60000", before="0", after="0.1", ask="60030")
    process_allocation(db, alloc)  # cash 394.8976, 0.01 BTC at cost 600.30

    # 1. No quote, no close: at cost -> equity 995.1976.
    v = paper.value_asset_allocation(db, alloc, now=T0 + timedelta(hours=2))
    assert (v.equity_micros, v.unmarked_cost_micros) == (995_197_600, 600_300_000)
    [p] = v.asset_positions
    assert (p["kind"], p["symbol"], p["side"], p["qty"], p["mark_source"], p["mark"]) == (
        "spot",
        "XXBTZUSD",
        "long",
        "0.01",
        "cost",
        None,
    )
    assert p["value"] == p["cost"] == 600.3 and p["unrealized"] == 0.0

    # 2. Last daily close 60,500 (D): 0.01 x 60,500 = 605.00 -> equity 999.8976.
    db.add(
        PriceDaily(
            instrument_id=kraken.btc.id,
            day=D,
            close="60500",
            usd_micros_per_unit=60_500 * M,
            source="kraken",
        )
    )
    db.commit()
    v = paper.value_asset_allocation(db, alloc, now=T0 + timedelta(days=1))
    assert (v.equity_micros, v.unmarked_cost_micros) == (999_897_600, 0)
    [p] = v.asset_positions
    assert (p["mark"], p["mark_source"], p["mark_as_of"], p["unrealized"]) == (
        "60500",
        "close",
        "2026-09-10",
        4.7,
    )

    # 3. A fresh quote mid 61,000.5: 0.01 x 61,000.5 = 610.005 -> equity 1,004.9026.
    monkeypatch.setattr(
        prices,
        "latest_mark",
        lambda db, iid, now: prices.Mark(iid, "61000.5", T0, "kraken", "mid"),
    )
    v = paper.value_asset_allocation(db, alloc)
    assert v.equity_micros == 1_004_902_600
    assert v.asset_positions[0]["mark_source"] == "quote"


def test_perp_valued_at_the_mark_and_shown_in_the_portfolio(db, hyperliquid, monkeypatch):
    alloc = hyperliquid.allocation("1000")
    hyperliquid.signal(
        hyperliquid.perp, "buy", "1", "60000", before="0", after="1", ask="60010",
        lag=timedelta(seconds=20),
    )  # fmt: skip
    hyperliquid.signal(
        hyperliquid.perp, "sell", "3", "61000", before="1", after="-2", bid="60990",
        filled_at=T0 + timedelta(hours=1), lag=timedelta(seconds=50),
    )  # fmt: skip
    process_allocation(db, alloc)  # cash 1,085.849992, short 0.19943 @ 60,990
    monkeypatch.setattr(
        prices, "latest_mark", lambda db, iid, now: prices.Mark(iid, "60000", T0, "hl", "mid")
    )
    # unrealized = -0.19943 x (60,000 - 60,990) = 197.4357
    # equity = 1,085.849992 + 197.4357 = 1,283.285692
    v = paper.value_asset_allocation(db, alloc)
    assert v.equity_micros == to_micros("1283.285692")
    assert (v.open_cost_micros, v.unmarked_cost_micros) == (0, 0)
    view = portfolio.allocation_view(db, alloc)
    [p] = view["positions"]
    assert p == {
        "kind": "perp",
        "symbol": "BTC",
        "side": "short",
        "qty": "0.19943",
        "cost": 12163.2357,  # 0.19943 x 60,990 at entry
        "mark": "60000",
        "mark_source": "quote",
        "mark_as_of": T0.isoformat(),
        "value": 197.4357,
        "unrealized": 197.4357,
        "entry": "60990",
    }
    assert view["venue"] == "hyperliquid" and view["asset_class"] == "crypto"
    assert view["copy_lag_median_seconds"] == 35  # (20 + 50) / 2
    assert view["skipped"] == {}
    assert view["n_fills"] == 2
    assert view["equity"] == pytest.approx(1283.285692)


def test_perp_without_any_price_contributes_nothing(db, hyperliquid):
    alloc = hyperliquid.allocation("1000")
    hyperliquid.signal(hyperliquid.perp, "buy", "1", "60000", before="0", after="1", ask="60010")
    process_allocation(db, alloc)
    v = paper.value_asset_allocation(db, alloc)
    assert v.equity_micros == to_micros("997.0001")
    assert v.asset_positions[0]["mark_source"] == "cost"
    # A close for the underlying coin values the perp: 0.09998 x (61,010 - 60,010) = 99.98.
    coin = hyperliquid.instrument("BTC/USDC", "BTC", quote="USDC")
    db.add(
        PriceDaily(
            instrument_id=coin.id, day=D, close="61010", usd_micros_per_unit=61_010 * M, source="hl"
        )
    )
    db.commit()
    v = paper.value_asset_allocation(db, alloc, now=T0 + timedelta(days=1))
    assert v.equity_micros == to_micros("1096.9801")


def test_cost_basis_is_average_cost_through_a_partial_sell(db, kraken):
    alloc = kraken.allocation("1000")
    kraken.signal(kraken.btc, "buy", "0.1", "60000", before="0", after="0.1", ask="60030")
    kraken.signal(
        kraken.btc,
        "sell",
        "0.04",
        "61000",
        before="0.1",
        after="0.06",
        bid="60990.5",
        filled_at=T0 + timedelta(hours=1),
    )
    process_allocation(db, alloc)
    # 600.30 paid for 0.01; 0.004 sold removes 40% of the cost -> 360.18 for 0.006.
    assert paper.cost_basis_micros(db, alloc) == {"BTC": 360_180_000}


# ---- pure helpers --------------------------------------------------------------------------------


def test_rounding_helpers():
    # Values are in micros: 1,951,696 micros to $0.0001 (100 micros) -> 1,951,700.
    assert paper.round_half_up(Fraction(1_951_696), 100) == 1_951_700
    assert paper.round_half_up(Fraction(1_951_650), 100) == 1_951_700  # a half goes up
    assert paper.round_half_up(Fraction(1_951_649), 100) == 1_951_600
    assert paper.ceil_to(Fraction(3_320), 10_000) == 10_000
    assert paper.ceil_to(Fraction(20_000), 10_000) == 20_000
    assert paper.floor_micros(Fraction(-1, 3)) == -1  # a debit rounds up in magnitude
    assert paper.floor_micros(Fraction(5, 3)) == 1
    assert paper.decimal_str(Fraction(609_905, 10)) == "60990.5"
    assert paper.decimal_str(Fraction(60_300)) == "60300"
    assert paper.decimal_str(Fraction(1, 3), 4) == "0.3333"


def test_bps_fee_uses_the_profile_rate_and_precision():
    kraken = get_profile("kraken")
    # 0.004 BTC x 60,990.5 = 243.962; 0.8% = 1.951696 -> 1.9517 ($0.0001).
    assert paper.asset_fee_micros(kraken, Action.SELL, 400_000, 8, "60990.5") == 1_951_700
    with pytest.raises(ValueError):
        paper.asset_fee_micros(get_profile("kalshi"), Action.BUY, 1, 0, "0.5")


def test_mirror_ledger_rows_are_append_only_after_a_copy(db, kraken):
    from alphamarket.models import AppendOnlyViolation

    alloc = kraken.allocation("1000")
    kraken.signal(kraken.btc, "buy", "0.1", "60000", before="0", after="0.1", ask="60030")
    process_allocation(db, alloc)
    [fill] = fills_of(db, alloc)
    fill.fee_micros = 0
    with pytest.raises(AppendOnlyViolation):
        db.flush()
    db.rollback()
    leg = db.scalars(
        select(MirrorLedgerEntry).where(MirrorLedgerEntry.kind == LegKind.FILL_BASE)
    ).one()
    db.delete(leg)
    with pytest.raises(AppendOnlyViolation):
        db.flush()

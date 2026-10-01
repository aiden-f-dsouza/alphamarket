"""Wave 2 scaffold: creator equity before a fill, the valuation DB adapters,
the paper follower book, and the schema rules every workstream relies on.
Expected values are worked out in the comments (USD micros; 1 USD = M)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from alphamarket import paper_book, valuation_db
from alphamarket.connectors.base import InstrumentType, LegKind
from alphamarket.models import (
    APPEND_ONLY_MODELS_W2,
    Allocation,
    AnchorBalance,
    AppendOnlyViolation,
    AssetFill,
    ConsentText,
    Instrument,
    LedgerEntry,
    MirrorAssetFill,
    MirrorLedgerEntry,
    PriceDaily,
    Signal,
    Strategy,
    User,
    VenueAccount,
    VenueActivityFlag,
)
from alphamarket.money import to_atoms, to_micros
from alphamarket.paper_book import LegSpec, cash_leg
from alphamarket.valuation import (
    AnchorIn,
    LegIn,
    PriceIn,
    basis_day,
    equity_before,
)

M = 1_000_000
D = date(2026, 9, 10)
DM1 = D - timedelta(days=1)


def at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


# ---- valuation.equity_before ------------------------------------------------------------


def test_equity_before_uses_previous_close_and_legs_strictly_before():
    # Anchor: $5,000 + 1 ETH. Closes: D-1 3,000; D 3,100 (not complete yet at t).
    # A $1,000 deposit at 09:00 on D counts; the fill's own leg at t does not.
    # E(t-) = 5,000 + 1,000 + 1 x 3,000 = 9,000.
    anchor = [AnchorIn("USD", 5_000 * M, 6), AnchorIn("ETH", to_atoms("1", 8), 8)]
    legs = [
        LegIn("USD", 1_000 * M, 6, LegKind.DEPOSIT, at(D, 9)),
        LegIn("ETH", to_atoms("0.5", 8), 8, LegKind.FILL_BASE, at(D, 12)),
        LegIn("USD", -1_550 * M, 6, LegKind.FILL_QUOTE, at(D, 12)),
    ]
    prices = [PriceIn("ETH", DM1, 3_000 * M), PriceIn("ETH", D, 3_100 * M)]
    v = equity_before(anchor, legs, prices, [], at(D, 12))
    assert v.equity_micros == 9_000 * M
    assert basis_day(v) == DM1


def test_equity_before_excludes_an_unpriced_asset():
    # No ETH close before D: ETH is excluded, equity is the $5,000 cash.
    anchor = [AnchorIn("USD", 5_000 * M, 6), AnchorIn("ETH", to_atoms("1", 8), 8)]
    v = equity_before(anchor, [], [PriceIn("ETH", D, 3_100 * M)], [], at(D, 12))
    assert v.equity_micros == 5_000 * M
    assert basis_day(v) is None


# ---- DB fixtures --------------------------------------------------------------------------


@pytest.fixture()
def db(session_factory):
    with session_factory() as session:
        yield session


def _user(db, email="a@example.com") -> User:
    user = User(email=email, password_hash="x", display_name="A")
    db.add(user)
    db.flush()
    return user


def _instrument(db, venue, symbol, base, type_=InstrumentType.SPOT, decimals=8) -> Instrument:
    inst = Instrument(
        venue=venue,
        symbol=symbol,
        type=type_,
        base_asset=base,
        quote_asset="USD",
        qty_decimals=decimals,
    )
    db.add(inst)
    db.flush()
    return inst


@pytest.fixture()
def kraken_account(db):
    user = _user(db)
    acct = VenueAccount(
        user_id=user.id,
        venue="kraken",
        venue_env="prod",
        key_id="k",
        anchor_balance_micros=5_000 * M,
        anchor_at=at(DM1, 0),
    )
    db.add(acct)
    db.flush()
    db.add(Strategy(user_id=user.id, venue_account_id=acct.id, name="K", slug="k"))
    db.flush()
    return acct


# ---- valuation_db --------------------------------------------------------------------------


def test_account_inputs_reads_anchor_vector_legs_after_it_and_perp_fills(db, kraken_account):
    anchor_at = at(DM1, 0)
    db.add_all(
        [
            AnchorBalance(
                venue_account_id=kraken_account.id,
                asset="USD",
                atoms=5_000 * M,
                decimals=6,
                as_of=anchor_at,
            ),
            AnchorBalance(
                venue_account_id=kraken_account.id,
                asset="ETH",
                atoms=to_atoms("1", 8),
                decimals=8,
                as_of=anchor_at,
            ),
        ]
    )
    perp = _instrument(db, "kraken", "ETH-PERP", "ETH-PERP", InstrumentType.PERP, 4)
    for n, (when, delta) in enumerate([(anchor_at - timedelta(hours=1), 7), (at(D, 9), 11)]):
        db.add(
            LedgerEntry(
                venue_account_id=kraken_account.id,
                asset="USD",
                delta_atoms=delta,
                decimals=6,
                kind=LegKind.DEPOSIT,
                venue_ref=f"d{n}",
                group_ref=f"event:deposit:d{n}",
                occurred_at=when,
            )
        )
    db.add(
        AssetFill(
            venue_account_id=kraken_account.id,
            instrument_id=perp.id,
            venue_fill_id="f1",
            action="sell",
            qty_atoms=25,
            qty_decimals=4,
            price="3010.5",
            filled_at=at(D, 10),
        )
    )
    db.commit()

    inputs = valuation_db.account_inputs(db, kraken_account)
    assert inputs is not None
    assert inputs.anchor_at == anchor_at
    assert {(a.asset, a.atoms) for a in inputs.anchor} == {("USD", 5_000 * M), ("ETH", 10**8)}
    assert [leg.delta_atoms for leg in inputs.legs] == [11]  # the pre-anchor deposit is out
    [fill] = inputs.perp_fills
    assert (fill.asset, fill.qty_delta_atoms, fill.decimals, fill.price) == (
        "ETH-PERP",
        -25,
        4,
        "3010.5",
    )


def test_account_inputs_falls_back_to_the_cash_anchor(db, kraken_account):
    inputs = valuation_db.account_inputs(db, kraken_account)
    assert [(a.asset, a.atoms, a.decimals) for a in inputs.anchor] == [("USD", 5_000 * M, 6)]


def test_price_inputs_prefer_the_pricing_venue_then_the_fallback(db):
    k = _instrument(db, "kraken", "XETHZUSD", "ETH")
    cg = _instrument(db, "coingecko", "ethereum", "ETH")
    other = _instrument(db, "binanceus", "ETHUSD", "ETH")
    db.add_all(
        [
            PriceDaily(
                instrument_id=k.id,
                day=DM1,
                close="3000",
                usd_micros_per_unit=3_000 * M,
                source="kraken",
            ),
            PriceDaily(
                instrument_id=cg.id,
                day=DM1,
                close="2990",
                usd_micros_per_unit=2_990 * M,
                source="coingecko",
            ),
            PriceDaily(
                instrument_id=cg.id,
                day=D,
                close="3100",
                usd_micros_per_unit=3_100 * M,
                source="coingecko",
            ),
            # Not a preferred venue: ignored even though it has the only D+1 close.
            PriceDaily(
                instrument_id=other.id,
                day=D + timedelta(days=1),
                close="1",
                usd_micros_per_unit=1 * M,
                source="binanceus",
            ),
            # No USD value (a BTC-quoted close): never a PriceIn.
            PriceDaily(
                instrument_id=k.id, day=D, close="0.05", usd_micros_per_unit=None, source="kraken"
            ),
        ]
    )
    db.commit()
    prices = valuation_db.price_inputs(db, {"ETH"}, None, None, ("kraken",))
    assert [(p.day, p.usd_micros_per_unit) for p in prices] == [(DM1, 3_000 * M), (D, 3_100 * M)]
    assert valuation_db.price_inputs(db, {"ETH"}, D, D, ("kraken",)) == [
        PriceIn("ETH", D, 3_100 * M)
    ]


# ---- paper_book -----------------------------------------------------------------------------


@pytest.fixture()
def allocation(db, kraken_account):
    follower = _user(db, "f@example.com")
    strategy = db.scalars(select(Strategy)).one()
    alloc = Allocation(
        user_id=follower.id,
        strategy_id=strategy.id,
        allocation_micros=1_000 * M,
        drawdown_limit_bps=1500,
        high_water_micros=1_000 * M,
        started_at=at(DM1, 0),
    )
    db.add(alloc)
    db.commit()
    return alloc


def test_rounding_is_against_the_follower():
    # 0.004 BTC x 60,990.5 = 243.962 exactly; 0.0000001 BTC x 60,990.5 = 0.00609905.
    assert paper_book.cost_micros_up(400_000, 8, "60990.5") == 243_962_000
    assert paper_book.cost_micros_up(10, 8, "60990.5") == 6_100  # 6,099.05 -> up
    assert paper_book.proceeds_micros_down(10, 8, "60990.5") == 6_099  # -> down


def test_book_spot_buy_and_idempotent_groups(db, allocation):
    btc = _instrument(db, "kraken", "XXBTZUSD", "BTC")
    # Buy 0.01 BTC at 60,030: cost 600.30, fee 0.8% = 4.8024 -> cash 394.8976.
    cost = paper_book.cost_micros_up(to_atoms("0.01", 8), 8, "60030")
    legs = [
        LegSpec("BTC", to_atoms("0.01", 8), 8, LegKind.FILL_BASE, btc.id),
        cash_leg(-cost, LegKind.FILL_QUOTE),
        cash_leg(-to_micros("4.8024"), LegKind.FEE),
    ]
    rows = paper_book.append_group(db, allocation, ref="sig:1", legs=legs, occurred_at=at(D))
    assert len(rows) == 3
    assert paper_book.append_group(db, allocation, ref="sig:1", legs=legs, occurred_at=at(D)) == []
    db.commit()

    book = paper_book.load_book(db, allocation)
    assert book.cash_micros == to_micros("394.8976")
    assert book.holdings["BTC"].atoms == 1_000_000
    assert book.refs == {"sig:1"}
    before = paper_book.load_book(db, allocation, at=at(D, 11))
    assert before.cash_micros == 1_000 * M and before.holdings == {}


def test_book_perp_entry_comes_from_mirror_fills(db, allocation):
    perp = _instrument(db, "hyperliquid", "BTC", "BTC-PERP", InstrumentType.PERP, 5)
    db.add(
        MirrorAssetFill(
            allocation_id=allocation.id,
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
        allocation,
        ref="sig:2",
        occurred_at=at(D),
        legs=[
            LegSpec("BTC-PERP", 9_998, 5, LegKind.FILL_BASE, perp.id),
            cash_leg(-to_micros("2.9999"), LegKind.FEE),
        ],
    )
    db.commit()
    book = paper_book.load_book(db, allocation)
    assert book.cash_micros == to_micros("997.0001")
    position = book.perps["BTC-PERP"]
    assert position.qty.atoms == 9_998 and position.entry == 60_010


# ---- schema rules ---------------------------------------------------------------------------


def test_signal_needs_exactly_one_source(db, kraken_account):
    strategy = db.scalars(select(Strategy)).one()
    db.add(
        Signal(
            strategy_id=strategy.id,
            ticker="X",
            side="long",
            action="buy",
            creator_equity_micros=1,
            filled_at=at(D),
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_wave2_append_only_tables_refuse_updates_and_deletes(db, kraken_account):
    db.add(ConsentText(sha256="a" * 64, venue="kraken", version="1", body="text"))
    flag = VenueActivityFlag(
        venue_account_id=kraken_account.id,
        venue_ref="r1",
        kind="unsupported_activity",
        activity_type="SSO",
        reason="short_sale",
        occurred_at=at(D),
    )
    db.add(flag)
    db.commit()
    flag.reason = "other"
    with pytest.raises(AppendOnlyViolation):
        db.flush()
    db.rollback()
    assert MirrorLedgerEntry in APPEND_ONLY_MODELS_W2
    db.delete(db.get(ConsentText, "a" * 64))
    with pytest.raises(AppendOnlyViolation):
        db.flush()

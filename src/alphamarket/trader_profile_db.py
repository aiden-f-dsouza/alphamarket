"""DB adapter for trader_profile.py: a watched strategy's rows in, the pure
module's inputs (and the profile) out. trader_profile never imports models;
this is its only bridge, the way valuation_db is valuation's.

Reads, per account kind:

* asset venues (Hyperliquid): `asset_fills` joined to `instruments` for the
  type and name; funding legs through `ingest.account_entries(kinds=FUNDING)`
  (cash assets only); the equity series from the newest `strategy_stats` row
  (`extras["daily_equity"]`, which analytics_v2 builds with valuation.py).
* binary venues (Polymarket): `fills` and `settlements` of the strategy; the
  same stored equity series.

No venue is called and nothing is written. The profile is computed on demand
for the page (an account's fills are a few thousand rows; cache in
`strategy_stats.extras` only if that ever shows up slow).
"""

from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket.analytics import latest_stats
from alphamarket.config import get_settings
from alphamarket.connectors.base import InstrumentType, LegKind
from alphamarket.connectors.registry import CASH_AT_PAR
from alphamarket.ingest import account_entries, is_asset_venue
from alphamarket.ledger import aware, fill_notional
from alphamarket.models import AssetFill, Fill, Instrument, Settlement, Strategy, VenueAccount
from alphamarket.money import MICROS_PER_UNIT, from_micros
from alphamarket.trader_profile import (
    EquityIn,
    FillIn,
    FundingIn,
    SettlementIn,
    TraderProfile,
    build_profile,
)
from alphamarket.valuation import parse_price

BINARY_DECIMALS = 6  # contract quantities share the 1e6 scale (invariant 1)


@dataclass(frozen=True)
class ProfileInputs:
    fills: list[FillIn]
    settlements: list[SettlementIn]
    equity: list[EquityIn]
    funding: list[FundingIn] | None  # None: no funding payments recorded
    copy_delay_seconds: int  # a follower's poll interval, settings.ingest_interval_seconds


def _asset_fills(db: Session, account: VenueAccount) -> list[FillIn]:
    rows = db.execute(
        select(AssetFill, Instrument)
        .join(Instrument, Instrument.id == AssetFill.instrument_id)
        .where(AssetFill.venue_account_id == account.id)
        .order_by(AssetFill.filled_at, AssetFill.id)
    ).all()
    out = []
    for fill, inst in rows:
        if fill.qty_atoms <= 0:
            continue
        # Gross value in USD micros, truncated toward zero like every valuation figure.
        notional = Fraction(fill.qty_atoms, 10**fill.qty_decimals) * parse_price(fill.price)
        out.append(
            FillIn(
                at=aware(fill.filled_at),
                instrument=inst.label,
                kind=InstrumentType(inst.type),
                action=fill.action,
                qty_atoms=fill.qty_atoms,
                qty_decimals=fill.qty_decimals,
                price=fill.price,
                notional_micros=int(notional * MICROS_PER_UNIT),
                order_id=fill.order_id,
            )
        )
    return out


def _funding(db: Session, account: VenueAccount) -> list[FundingIn] | None:
    legs = account_entries(db, account.id, kinds=[LegKind.FUNDING])
    paid = [
        FundingIn(
            at=aware(leg.occurred_at),
            amount_micros=int(Fraction(leg.delta_atoms * MICROS_PER_UNIT, 10**leg.decimals)),
        )
        for leg in legs
        if leg.asset in CASH_AT_PAR
    ]
    return paid or None  # nothing recorded: unknown, not zero


def _binary_fills(db: Session, strategy: Strategy) -> list[FillIn]:
    rows = db.scalars(
        select(Fill).where(Fill.strategy_id == strategy.id).order_by(Fill.filled_at, Fill.id)
    ).all()
    return [
        FillIn(
            at=aware(f.filled_at),
            instrument=f.ticker,
            kind=InstrumentType.BINARY,
            action=f.action,
            qty_atoms=f.count_micros,
            qty_decimals=BINARY_DECIMALS,
            price=str(from_micros(f.price_micros)),
            notional_micros=fill_notional(f),
            outcome=f.side,
        )
        for f in rows
        if f.count_micros > 0
    ]


def _settlements(db: Session, strategy: Strategy) -> list[SettlementIn]:
    rows = db.scalars(
        select(Settlement)
        .where(Settlement.strategy_id == strategy.id)
        .order_by(Settlement.settled_at, Settlement.id)
    ).all()
    return [SettlementIn(aware(s.settled_at), s.ticker, s.market_result.lower()) for s in rows]


def _equity(db: Session, strategy: Strategy) -> list[EquityIn]:
    stats = latest_stats(db, strategy.id)
    if stats is None:
        return []
    return [EquityIn(date.fromisoformat(day), int(v)) for day, v in stats.extras["daily_equity"]]


def trader_profile_inputs(db: Session, strategy: Strategy) -> ProfileInputs | None:
    """The pure module's inputs for a watched strategy; None when its account
    is gone."""
    account = db.get(VenueAccount, strategy.venue_account_id)
    if account is None:
        return None
    delay = get_settings().ingest_interval_seconds
    if is_asset_venue(account.venue):
        return ProfileInputs(
            _asset_fills(db, account),
            [],
            _equity(db, strategy),
            _funding(db, account),
            delay,
        )
    return ProfileInputs(
        _binary_fills(db, strategy),
        _settlements(db, strategy),
        _equity(db, strategy),
        None,
        delay,
    )


def trader_profile(db: Session, strategy: Strategy) -> TraderProfile | None:
    inputs = trader_profile_inputs(db, strategy)
    if inputs is None:
        return None
    return build_profile(
        inputs.fills,
        inputs.settlements,
        inputs.equity,
        inputs.funding,
        copy_delay_seconds=inputs.copy_delay_seconds,
    )

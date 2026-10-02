"""DB adapters for valuation.py (wave2): turn ledger v2 rows into the pure
module's inputs. valuation.py never imports the models; this module is the
only bridge, shared by analytics_v2 (creator stats), paper_signals (creator
equity at a fill) and the paper engine (follower valuation).

Rows in, dataclasses out; no arithmetic beyond sign and selection.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket.connectors.base import InstrumentType, LegKind
from alphamarket.connectors.registry import get_profile, price_asset
from alphamarket.ingest import account_entries
from alphamarket.jobs import aware
from alphamarket.models import AssetFill, Instrument, PriceDaily, VenueAccount
from alphamarket.reconcile import latest_anchor_balances
from alphamarket.valuation import AnchorIn, LegIn, PerpFillIn, PriceIn

FALLBACK_PRICE_SOURCE = "coingecko"
"""Synthetic `instruments.venue` of fallback prices (connectors/coingecko.py)."""

CASH_DECIMALS = 6  # an anchor_balance_micros fallback is the quote asset at 6 dp


@dataclass(frozen=True)
class ValuationInputs:
    """Everything valuation.py needs for one account, from its anchor on."""

    anchor: tuple[AnchorIn, ...]
    anchor_at: datetime
    legs: tuple[LegIn, ...]  # strictly after anchor_at, in ingestion order
    perp_fills: tuple[PerpFillIn, ...]  # strictly after anchor_at

    @property
    def assets(self) -> set[str]:
        return {a.asset for a in self.anchor} | {leg.asset for leg in self.legs}


def account_inputs(db: Session, account: VenueAccount) -> ValuationInputs | None:
    """The anchor vector (newest anchor_balances set; for an account anchored
    before ledger v2, its cash anchor in the profile's quote asset), every
    ledger leg after it, and every perp fill after it as a signed PerpFillIn
    (buy +, sell -). None when the account was never anchored."""
    rows = latest_anchor_balances(db, account)
    if rows:
        anchor_at = max(aware(r.as_of) for r in rows)
        anchor = tuple(
            AnchorIn(r.asset, r.atoms, r.decimals, entry_price=r.entry_price) for r in rows
        )
    elif account.anchor_at is not None and account.anchor_balance_micros is not None:
        anchor_at = aware(account.anchor_at)
        quote = get_profile(account.venue).quote_asset
        anchor = (AnchorIn(quote, account.anchor_balance_micros, CASH_DECIMALS),)
    else:
        return None
    legs = tuple(
        LegIn(
            asset=e.asset,
            delta_atoms=e.delta_atoms,
            decimals=e.decimals,
            kind=LegKind(e.kind),
            occurred_at=aware(e.occurred_at),
        )
        for e in account_entries(db, account.id, after=anchor_at)
    )
    fills = db.execute(
        select(AssetFill, Instrument)
        .join(Instrument, Instrument.id == AssetFill.instrument_id)
        .where(AssetFill.venue_account_id == account.id, Instrument.type == InstrumentType.PERP)
        .order_by(AssetFill.filled_at, AssetFill.id)
    ).all()
    perp_fills = tuple(
        PerpFillIn(
            asset=inst.base_asset,
            occurred_at=aware(fill.filled_at),
            qty_delta_atoms=fill.qty_atoms if fill.action == "buy" else -fill.qty_atoms,
            decimals=fill.qty_decimals,
            price=fill.price,
        )
        for fill, inst in fills
        if aware(fill.filled_at) > anchor_at
    )
    return ValuationInputs(anchor, anchor_at, legs, perp_fills)


def price_inputs(
    db: Session,
    assets: Iterable[str],
    start: date | None,
    end: date | None,
    prefer_venues: Sequence[str],
) -> list[PriceIn]:
    """One PriceIn per (asset, day) in [start, end] (None = unbounded) from
    prices_daily rows that carry a USD value. For each asset and day the
    first venue in `prefer_venues` with a close wins, then the CoinGecko
    fallback. Instruments are matched on base_asset, so a perp ("BTC-PERP")
    gets its own closes and a coin its spot closes. A venue variant priced as
    another asset (registry.price_asset: Kraken "ETH2.S" -> "ETH") gets that
    asset's closes, returned under the held asset's own code."""
    order = [*dict.fromkeys([*prefer_venues, FALLBACK_PRICE_SOURCE])]
    held = set(assets)
    if not held:
        return []
    home = prefer_venues[0] if prefer_venues else None
    priced_as: dict[str, set[str]] = {}
    for asset in held:
        target = price_asset(home, asset) if home else asset
        priced_as.setdefault(target, set()).add(asset)
    wanted = set(priced_as)
    query = (
        select(
            Instrument.base_asset,
            Instrument.venue,
            PriceDaily.day,
            PriceDaily.usd_micros_per_unit,
            PriceDaily.close,
        )
        .join(PriceDaily, PriceDaily.instrument_id == Instrument.id)
        .where(
            Instrument.base_asset.in_(wanted),
            Instrument.venue.in_(order),
            PriceDaily.usd_micros_per_unit.is_not(None),
        )
    )
    if start is not None:
        query = query.where(PriceDaily.day >= start)
    if end is not None:
        query = query.where(PriceDaily.day <= end)
    rank = {venue: n for n, venue in enumerate(order)}
    best: dict[tuple[str, date], tuple[int, int, str | None]] = {}
    for target, venue, day, usd, close in db.execute(query).all():
        for asset in priced_as[target]:
            key = (asset, day)
            candidate = (rank[venue], usd, close)
            if key not in best or candidate[0] < best[key][0]:
                best[key] = candidate
    return [
        _price_in(asset, day, usd, close) for (asset, day), (_, usd, close) in sorted(best.items())
    ]


def _price_in(asset: str, day: date, usd: int, close: str | None) -> PriceIn:
    """D14: carry the exact close when it agrees with the stored micros (it
    always does for a USD/at-par close written by prices.py); a row that
    disagrees is valued at its micros rather than failing the whole job."""
    try:
        return PriceIn(asset, day, usd, close=close)
    except (ValueError, TypeError, ArithmeticError):
        return PriceIn(asset, day, usd)


def pricing_venues(account: VenueAccount) -> tuple[str, ...]:
    """Where an account's assets are priced: the profile's pricing_venue
    (IBKR, SnapTrade -> alpaca) or the venue itself."""
    profile = get_profile(account.venue)
    return (profile.pricing_venue or account.venue,)

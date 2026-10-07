"""DB adapters for valuation.py (wave2): turn ledger v2 rows into the pure
module's inputs. valuation.py never imports the models; this module is the
only bridge, shared by analytics_v2 (creator stats), paper_signals (creator
equity at a fill) and the paper engine (follower valuation).

Rows in, dataclasses out; no arithmetic beyond sign and selection.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alphamarket.connectors.base import InstrumentType, LegKind
from alphamarket.connectors.registry import get_profile, price_asset
from alphamarket.jobs import aware
from alphamarket.models import AssetFill, Instrument, LedgerEntry, PriceDaily, VenueAccount
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


def account_anchor(
    db: Session, account: VenueAccount
) -> tuple[tuple[AnchorIn, ...], datetime] | None:
    """(anchor vector, anchor_at): the newest anchor_balances set, or for an
    account anchored before ledger v2 its cash anchor in the profile's quote
    asset. None when the account was never anchored. Reads no legs."""
    rows = latest_anchor_balances(db, account)
    if rows:
        anchor_at = max(aware(r.as_of) for r in rows)
        anchor = tuple(
            AnchorIn(r.asset, r.atoms, r.decimals, entry_price=r.entry_price) for r in rows
        )
        return anchor, anchor_at
    if account.anchor_at is not None and account.anchor_balance_micros is not None:
        quote = get_profile(account.venue).quote_asset
        return (AnchorIn(quote, account.anchor_balance_micros, CASH_DECIMALS),), aware(
            account.anchor_at
        )
    return None


def account_inputs(
    db: Session, account: VenueAccount, *, valued_from: datetime | None = None
) -> ValuationInputs | None:
    """The anchor vector (account_anchor), every ledger leg after it, and every
    perp fill after it as a signed PerpFillIn (buy +, sell -). None when the
    account was never anchored.

    valued_from (w3-perf, for paper_signals): the caller values the account
    only at instants >= valued_from, with LedgerSweep's holdings_before /
    perp_positions_before / equity_before. Then the legs before midnight UTC
    of valued_from's day are summed in SQL into one ADJUSTMENT leg per
    (asset, decimals), dated a microsecond before that midnight; later legs
    come one by one. Those answers are unchanged, to the atom: holdings are
    sums, every (asset, decimals) pair keeps a leg (zero sums too) so each
    asset's scale is the same max, and equity_before counts a flow only on
    the instant's own UTC day, which no folded leg falls on. Perp fills are
    all kept (the perp book depends on their order). NOT for daily series
    or anything that reads a leg's kind or time before that midnight."""
    anchored = account_anchor(db, account)
    if anchored is None:
        return None
    anchor, anchor_at = anchored
    cut = None
    if valued_from is not None:
        cut = datetime.combine(aware(valued_from).astimezone(UTC).date(), time(0), tzinfo=UTC)
    legs = _legs_after(db, account, anchor_at, fold_before=cut)
    fills = db.execute(
        select(
            AssetFill.action,
            AssetFill.qty_atoms,
            AssetFill.qty_decimals,
            AssetFill.price,
            AssetFill.filled_at,
            Instrument.base_asset,
        )
        .join(Instrument, Instrument.id == AssetFill.instrument_id)
        .where(AssetFill.venue_account_id == account.id, Instrument.type == InstrumentType.PERP)
        .order_by(AssetFill.filled_at, AssetFill.id)
    ).all()
    perp_fills = tuple(
        PerpFillIn(
            asset=row.base_asset,
            occurred_at=aware(row.filled_at),
            qty_delta_atoms=row.qty_atoms if row.action == "buy" else -row.qty_atoms,
            decimals=row.qty_decimals,
            price=row.price,
        )
        for row in fills
        if aware(row.filled_at) > anchor_at
    )
    return ValuationInputs(anchor, anchor_at, legs, perp_fills)


def _legs_after(
    db: Session, account: VenueAccount, after: datetime, *, fold_before: datetime | None = None
) -> tuple[LegIn, ...]:
    """ingest.account_entries(db, account.id, after=after) as LegIn, in the same
    (ingestion, id) order: the columns valuation needs, not ORM rows, with the
    time filter in SQL and re-checked aware (SQLite stores naive values).
    fold_before: see account_inputs(valued_from=...)."""
    after = aware(after)
    folded: list[LegIn] = []
    query = select(
        LedgerEntry.asset,
        LedgerEntry.delta_atoms,
        LedgerEntry.decimals,
        LedgerEntry.kind,
        LedgerEntry.occurred_at,
    ).where(LedgerEntry.venue_account_id == account.id, LedgerEntry.occurred_at > after)
    if fold_before is not None and fold_before > after:
        sums = db.execute(
            select(LedgerEntry.asset, LedgerEntry.decimals, func.sum(LedgerEntry.delta_atoms))
            .where(
                LedgerEntry.venue_account_id == account.id,
                LedgerEntry.occurred_at > after,
                LedgerEntry.occurred_at < fold_before,
            )
            .group_by(LedgerEntry.asset, LedgerEntry.decimals)
            .order_by(LedgerEntry.asset, LedgerEntry.decimals)
        ).all()
        at = fold_before - timedelta(microseconds=1)
        # SUM(bigint) is NUMERIC on Postgres (an exact Decimal): int() is exact.
        folded = [
            LegIn(asset, int(total), decimals, LegKind.ADJUSTMENT, at)
            for asset, decimals, total in sums
        ]
        query = query.where(LedgerEntry.occurred_at >= fold_before)
    rows = db.execute(query.order_by(LedgerEntry.id)).all()
    return (
        *folded,
        *(
            LegIn(
                asset=asset,
                delta_atoms=delta,
                decimals=decimals,
                kind=LegKind(kind),
                occurred_at=aware(occurred),
            )
            for asset, delta, decimals, kind, occurred in rows
            if aware(occurred) > after
        ),
    )


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

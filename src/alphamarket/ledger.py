"""Ledger math (M2): cash accounting from venue fills and settlements.

Every formula here is documented on the public methodology page
(docs/methodology.md) and pinned by a hand-computed unit test.

Model (verified to the micro-dollar against the live demo account on
2026-09-01: pre-trade balance minus one fill's notional-plus-fee equalled the
post-settlement balance exactly):

    cash(t) = anchor_balance
            + sum(fill_cash_flow for fills after anchor_at, filled_at <= t)
            + sum(settlement revenue for settlements after anchor_at, settled_at <= t)

    fill_cash_flow(buy)  = -(count * price + fee)
    fill_cash_flow(sell) = +(count * price - fee)
    settlement flow      = +revenue          (fees are charged at fill time
                                              only; the settlement payload's
                                              fee field is a summary, not a
                                              second charge)

The anchor is the venue balance observed at connect time. Reconciliation
therefore never depends on how much history the venue returns: any fill
before the anchor is already reflected in the anchor balance, and any
settlement after it (even of a pre-anchor position) is real cash in.

Contract quantities and prices both carry the 1e6 scale, so count * price
carries 1e12 and money.notional_micros brings it back exactly. A venue that
reports an average price finer than 1e-6 (Polymarket) also reports the gross
cash of the fill; that figure is stored as `notional_micros` and used in
place of the product, so cash still reproduces the venue to the micro.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from alphamarket.connectors.base import Action
from alphamarket.money import notional_micros


class FillLike(Protocol):
    ticker: str
    action: str
    count_micros: int
    price_micros: int
    fee_micros: int
    filled_at: datetime


class SettlementLike(Protocol):
    ticker: str
    revenue_micros: int
    settled_at: datetime


@dataclass(frozen=True)
class LedgerEvent:
    at: datetime
    kind: str  # "fill" | "settlement"
    ticker: str
    cash_flow_micros: int
    cash_after_micros: int


def fill_notional(fill: FillLike) -> int:
    """Gross value of a fill before fees: the venue-reported figure when the
    fill carries one, otherwise price x count computed exactly."""
    reported = getattr(fill, "notional_micros", None)
    if reported is not None:
        return reported
    return notional_micros(fill.price_micros, fill.count_micros)


def fill_cash_flow(fill: FillLike) -> int:
    """Signed cash movement of one fill, in micros."""
    notional = fill_notional(fill)
    if Action(fill.action) is Action.BUY:
        return -(notional + fill.fee_micros)
    return notional - fill.fee_micros


def settlement_cash_flow(settlement: SettlementLike) -> int:
    return settlement.revenue_micros


def aware(ts: datetime) -> datetime:
    """SQLite drops tzinfo; stored values are UTC by invariant #2."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def build_cash_curve(
    anchor_balance_micros: int,
    anchor_at: datetime,
    fills: list[FillLike],
    settlements: list[SettlementLike],
) -> list[LedgerEvent]:
    """Chronological cash curve starting from the anchor. Events at or
    before anchor_at are excluded: the anchor balance already contains them."""
    anchor_at = aware(anchor_at)
    pending: list[tuple[datetime, int, str, str, int]] = []
    for f in fills:
        at = aware(f.filled_at)
        if at > anchor_at:
            pending.append((at, 0, "fill", f.ticker, fill_cash_flow(f)))
    for s in settlements:
        at = aware(s.settled_at)
        if at > anchor_at:
            pending.append((at, 1, "settlement", s.ticker, settlement_cash_flow(s)))
    pending.sort(key=lambda e: (e[0], e[1]))

    curve: list[LedgerEvent] = []
    cash = anchor_balance_micros
    for at, _, kind, ticker, flow in pending:
        cash += flow
        curve.append(LedgerEvent(at, kind, ticker, flow, cash))
    return curve


def cash_at(
    anchor_balance_micros: int,
    anchor_at: datetime,
    fills: list[FillLike],
    settlements: list[SettlementLike],
    as_of: datetime | None = None,
) -> int:
    """Cash balance implied by the ledger at `as_of` (default: after all events)."""
    curve = build_cash_curve(anchor_balance_micros, anchor_at, fills, settlements)
    if as_of is not None:
        as_of = aware(as_of)
        curve = [e for e in curve if e.at <= as_of]
    return curve[-1].cash_after_micros if curve else anchor_balance_micros

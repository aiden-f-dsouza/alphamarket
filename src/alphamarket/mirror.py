"""Paper mirroring engine (M6): turn creator signals into follower fills, and
creator settlements into follower settlements.

Every formula here is on the methodology page (docs/methodology.md, "Paper
tailing") and pinned by a hand-computed test in tests/test_mirror.py.

Sizing
------
    buy:  follower_count = signal_count × follower_equity / creator_equity_before
    sell: follower_count = follower_position × signal_count
                           / (signal_count + creator_position_after)
          (the follower sells the same FRACTION of its position that the
          creator sold; a creator selling out sells the follower out too)

    counts are floored to 0.01 contract (the venue's own granularity);
    a buy is capped at what the follower's cash can pay for including fee.

Fee (Kalshi's published formula, verified to the micro against the one
recorded live fill: 101.89 × 0.98 × 0.02 × 0.07 = 0.13979 -> "0.139800"):
    fee = 0.07 × count × price × (1 - price), rounded half-up to $0.0001

Settlement
----------
A follower position settles when the creator's settlement for that ticker is
ingested (same result, same time), paying $1 per contract on the winning side.
If the creator had already sold out and never gets a settlement row, the
public market's finalized result settles the follower instead.

Paper fills are modelled, not real: the creator's price is assumed available
to the follower. That assumption is stated on every paper figure.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket.connectors.base import Action, Side, VenueProfile
from alphamarket.connectors.registry import get_profile
from alphamarket.execution import get_executor
from alphamarket.ledger import aware
from alphamarket.marketdata import latest_prices
from alphamarket.models import (
    Allocation,
    AllocationStatus,
    MirrorFill,
    MirrorSettlement,
    Settlement,
    Signal,
    Strategy,
    VenueAccount,
)
from alphamarket.money import MICROS_PER_UNIT, notional_micros
from alphamarket.stats import OpenPosition, Replay, replay

logger = logging.getLogger(__name__)

CONTRACT_STEP_MICROS = 10_000  # 0.01 contract (Kalshi default; venues override)
FEE_RATE_BPS = 700  # Kalshi: 7% of count × price × (1 - price); venues override
FEE_ROUNDING_MICROS = 100  # venue reports fees to $0.0001


def paper_fee_micros(
    count_micros: int,
    price_micros: int,
    fee_rate_bps: int = FEE_RATE_BPS,
    rounding_micros: int = FEE_ROUNDING_MICROS,
) -> int:
    """rate × count × price × (1 − price), rounded half-up to the venue's fee
    precision. Rate and precision are the venue's (Kalshi 7% to $0.0001,
    Polymarket 5% to $0.000001); see registry.PROFILES."""
    # count(1e6) × price(1e6) × (1-price)(1e6) × rate/10_000 -> scale 1e18 × 1e-4
    numerator = count_micros * price_micros * (MICROS_PER_UNIT - price_micros) * fee_rate_bps
    denominator = 10_000 * MICROS_PER_UNIT * MICROS_PER_UNIT
    raw, rem = divmod(numerator, denominator)
    if 2 * rem >= denominator:
        raw += 1
    q, r = divmod(raw, rounding_micros)
    if 2 * r >= rounding_micros:
        q += 1
    return q * rounding_micros


def floor_to_step(count_micros: int, step_micros: int = CONTRACT_STEP_MICROS) -> int:
    return count_micros // step_micros * step_micros


def round_to_tick(price_micros: int, tick_micros: int) -> int:
    """Nearest venue tick, half-up. Paper fills happen on the venue's price
    grid so that price × count is exact in micros for any stepped count."""
    q, r = divmod(price_micros, tick_micros)
    if 2 * r >= tick_micros:
        q += 1
    return q * tick_micros


def affordable_count(
    cash_micros: int,
    price_micros: int,
    count_micros: int,
    fee_rate_bps: int = FEE_RATE_BPS,
    step_micros: int = CONTRACT_STEP_MICROS,
    rounding_micros: int = FEE_ROUNDING_MICROS,
) -> int:
    """Largest count (stepped) not above `count_micros` whose notional plus fee
    fits in `cash_micros`. Notional grows linearly and the fee is monotone in
    count, so stepping down from the notional-only bound terminates fast."""
    if cash_micros <= 0 or price_micros <= 0:
        return 0
    count = min(
        count_micros, floor_to_step(cash_micros * MICROS_PER_UNIT // price_micros, step_micros)
    )
    while count > 0:
        cost = notional_micros(price_micros, count) + paper_fee_micros(
            count, price_micros, fee_rate_bps, rounding_micros
        )
        if cost <= cash_micros:
            return count
        count -= step_micros
    return 0


def profile_for(db: Session, allocation: Allocation) -> VenueProfile:
    """The venue profile of the strategy an allocation tails."""
    strategy = db.get(Strategy, allocation.strategy_id)
    account = db.get(VenueAccount, strategy.venue_account_id) if strategy else None
    return get_profile(account.venue if account else "kalshi")


# ---- follower ledger ------------------------------------------------------------


def allocation_ledger(db: Session, allocation: Allocation) -> tuple[list, list]:
    fills = db.scalars(
        select(MirrorFill)
        .where(MirrorFill.allocation_id == allocation.id)
        .order_by(MirrorFill.filled_at, MirrorFill.id)
    ).all()
    settlements = db.scalars(
        select(MirrorSettlement)
        .where(MirrorSettlement.allocation_id == allocation.id)
        .order_by(MirrorSettlement.settled_at, MirrorSettlement.id)
    ).all()
    return fills, settlements


def allocation_anchor_at(allocation: Allocation):
    """Replay excludes events at or before the anchor; a mirror fill can carry
    the exact start instant, so anchor one second earlier."""
    return aware(allocation.started_at) - timedelta(seconds=1)


def replay_allocation(db: Session, allocation: Allocation) -> Replay:
    fills, settlements = allocation_ledger(db, allocation)
    return replay(
        allocation.allocation_micros, allocation_anchor_at(allocation), fills, settlements
    )


def _cash_now(rep: Replay, allocation: Allocation) -> int:
    return rep.points[-1].cash_micros if rep.points else allocation.allocation_micros


def _open_cost_now(rep: Replay) -> int:
    return rep.points[-1].open_cost_micros if rep.points else 0


def _position(rep: Replay, ticker: str, side: str) -> OpenPosition | None:
    return next((p for p in rep.open_positions if p.ticker == ticker and p.side == side), None)


# ---- sizing ------------------------------------------------------------------


@dataclass(frozen=True)
class Sized:
    count_micros: int
    reason: str  # "ok" | why it was skipped


def size_buy(
    signal: Signal,
    follower_equity_micros: int,
    follower_cash_micros: int,
    profile: VenueProfile | None = None,
) -> Sized:
    step = profile.contract_step_micros if profile else CONTRACT_STEP_MICROS
    rate = profile.fee_rate_bps if profile else FEE_RATE_BPS
    price = (
        round_to_tick(signal.price_micros, profile.tick_micros) if profile else signal.price_micros
    )
    if signal.creator_equity_micros <= 0:
        return Sized(0, "creator_equity_unknown")
    if follower_equity_micros <= 0:
        return Sized(0, "follower_equity_zero")
    proportional = floor_to_step(
        signal.count_micros * follower_equity_micros // signal.creator_equity_micros, step
    )
    rounding = profile.fee_rounding_micros if profile else FEE_ROUNDING_MICROS
    count = affordable_count(follower_cash_micros, price, proportional, rate, step, rounding)
    if count <= 0:
        return Sized(0, "below_minimum_size" if proportional <= 0 else "insufficient_cash")
    return Sized(count, "ok")


def size_sell(
    signal: Signal, follower_position_micros: int, profile: VenueProfile | None = None
) -> Sized:
    step = profile.contract_step_micros if profile else CONTRACT_STEP_MICROS
    if follower_position_micros <= 0:
        return Sized(0, "no_position")
    creator_before = signal.count_micros + signal.position_after_micros
    if creator_before <= 0:
        return Sized(0, "creator_position_unknown")
    if signal.position_after_micros == 0:
        count = follower_position_micros  # creator sold out: so does the follower
    else:
        count = floor_to_step(
            follower_position_micros * signal.count_micros // creator_before, step
        )
    if count <= 0:
        return Sized(0, "below_minimum_size")
    return Sized(min(count, follower_position_micros), "ok")


# ---- mirroring ----------------------------------------------------------------


def mirror_signal(db: Session, allocation: Allocation, signal: Signal) -> MirrorFill | None:
    """Apply one signal to one allocation. Returns the mirror fill, or None
    if the signal was skipped. Never raises for a sizing outcome."""
    rep = replay_allocation(db, allocation)
    profile = profile_for(db, allocation)
    cash = _cash_now(rep, allocation)
    equity = cash + _open_cost_now(rep)
    if Action(signal.action) is Action.BUY:
        sized = size_buy(signal, equity, cash, profile)
    else:
        pos = _position(rep, signal.ticker, signal.side)
        sized = size_sell(signal, pos.count_micros if pos else 0, profile)
    if sized.count_micros <= 0:
        logger.info("allocation %s skipped signal %s: %s", allocation.id, signal.id, sized.reason)
        return None
    executed = get_executor(allocation.mode).execute(
        allocation, signal, sized.count_micros, profile
    )
    row = MirrorFill(
        allocation_id=allocation.id,
        signal_id=signal.id,
        mode=allocation.mode,
        ticker=signal.ticker,
        side=signal.side,
        action=signal.action,
        count_micros=executed.count_micros,
        price_micros=executed.price_micros,
        fee_micros=executed.fee_micros,
        venue_order_id=executed.venue_order_id,
        filled_at=executed.filled_at,
    )
    db.add(row)
    return row


def mirror_pending(db: Session, allocation: Allocation) -> int:
    """Apply every signal the allocation has not yet considered. Signals filled
    before the allocation started are skipped: a follower joins at the
    creator's current book going forward, it does not inherit past entries."""
    if allocation.status != AllocationStatus.ACTIVE:
        return 0
    signals = db.scalars(
        select(Signal)
        .where(Signal.strategy_id == allocation.strategy_id, Signal.id > allocation.last_signal_id)
        .order_by(Signal.id)
    ).all()
    started = aware(allocation.started_at)
    mirrored = 0
    for signal in signals:
        if aware(signal.filled_at) >= started and mirror_signal(db, allocation, signal):
            mirrored += 1
            db.flush()  # the next replay must see this fill
        allocation.last_signal_id = signal.id
    db.commit()
    return mirrored


# ---- settlement --------------------------------------------------------------


def _settle(
    db: Session, allocation: Allocation, pos_yes: int, pos_no: int, ticker: str, result: str, at
) -> MirrorSettlement:
    if result == "yes":
        revenue = notional_micros(MICROS_PER_UNIT, pos_yes)
    elif result == "no":
        revenue = notional_micros(MICROS_PER_UNIT, pos_no)
    else:
        revenue = 0  # void/other: the venue refunds cost in reality; modelled as a loss
    row = MirrorSettlement(
        allocation_id=allocation.id,
        ticker=ticker,
        market_result=result,
        yes_count_micros=pos_yes,
        no_count_micros=pos_no,
        revenue_micros=revenue,
        settled_at=at,
    )
    db.add(row)
    return row


def settle_pending(db: Session, allocation: Allocation) -> int:
    """Settle every open follower position whose market has resolved, using
    the creator's settlement row when there is one and the public market's
    finalized result otherwise. Idempotent: one settlement per ticker."""
    rep = replay_allocation(db, allocation)
    if not rep.open_positions:
        return 0
    already = set(
        db.scalars(
            select(MirrorSettlement.ticker).where(MirrorSettlement.allocation_id == allocation.id)
        ).all()
    )
    open_by_ticker: dict[str, dict[str, int]] = {}
    for p in rep.open_positions:
        if p.ticker in already:
            continue
        open_by_ticker.setdefault(p.ticker, {Side.YES: 0, Side.NO: 0})[p.side] = p.count_micros
    if not open_by_ticker:
        return 0

    creator_settlements = {
        s.ticker: s
        for s in db.scalars(
            select(Settlement).where(
                Settlement.strategy_id == allocation.strategy_id,
                Settlement.ticker.in_(list(open_by_ticker)),
            )
        ).all()
    }
    strategy = db.get(Strategy, allocation.strategy_id)
    account = db.get(VenueAccount, strategy.venue_account_id) if strategy else None
    prices = latest_prices(db, account.venue, account.venue_env, open_by_ticker) if account else {}
    settled = 0
    for ticker, sides in open_by_ticker.items():
        creator = creator_settlements.get(ticker)
        if creator is not None:
            _settle(
                db,
                allocation,
                sides[Side.YES],
                sides[Side.NO],
                ticker,
                creator.market_result,
                creator.settled_at,
            )
            settled += 1
            continue
        price = prices.get(ticker)
        if price is not None and price.result in ("yes", "no"):
            _settle(
                db, allocation, sides[Side.YES], sides[Side.NO], ticker, price.result, price.as_of
            )
            settled += 1
    db.commit()
    return settled


def process_allocation(db: Session, allocation: Allocation) -> tuple[int, int]:
    """One engine pass: mirror new signals, then settle resolved markets.
    wave2: allocations of asset strategies go to paper.py."""
    if is_asset_allocation(db, allocation):
        from alphamarket.paper import process_asset_allocation

        return process_asset_allocation(db, allocation)
    mirrored = mirror_pending(db, allocation)
    settled = settle_pending(db, allocation)
    return mirrored, settled


def is_asset_allocation(db: Session, allocation: Allocation) -> bool:
    """wave2: does this allocation tail an ASSET strategy (paper.py)?"""
    from alphamarket.ingest import is_asset_venue
    from alphamarket.models import Strategy, VenueAccount

    strategy = db.get(Strategy, allocation.strategy_id)
    account = db.get(VenueAccount, strategy.venue_account_id) if strategy else None
    return account is not None and is_asset_venue(account.venue)

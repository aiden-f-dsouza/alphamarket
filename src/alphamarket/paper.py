"""Paper copy v2 engine for ASSET strategies (wave2, workstream w2-p6-engine).

mirror.process_allocation and portfolio.value_allocation / allocation_view
dispatch here for asset venues. The book itself lives in paper_book.py;
lifecycle events (funding, liquidation, splits, dividends) in
paper_lifecycle.py. Every formula is on the methodology page (docs/
methodology.md, "Paper copying v2: sizing, fill price, fees") and pinned by
a hand-computed test in tests/test_paper_engine.py.

Contract:

process_asset_allocation(db, allocation) -> tuple[int, int]
    Mirror signals with id > allocation.last_signal_id (ACTIVE allocations
    only), then ALWAYS call paper_lifecycle.apply_lifecycle(db, allocation,
    now) (funding, liquidation, splits, dividends; stopped allocations too,
    so dividends and funding keep accruing on what is still held). Returns
    (signals mirrored, lifecycle events applied). COMMIT (or roll back)
    before returning, even when nothing was done: worker.handle_mirror holds
    a FOR UPDATE SKIP LOCKED lock on the allocation row until then. Per
    signal: skip with a
    mirror_skips row (filled_at < started_at is skipped silently, as in M6;
    stale_signal, creator_equity_unknown, non_usd_quote, below_minimum_size,
    insufficient_cash, no_position, follower_equity_zero), else size (plan
    D8), price (D2/D4: ask for buys, bid for sells from the signal's quote,
    else creator price +/- profile.paper_fallback_penalty_bps), fee (D10 by
    profile.paper_fee_model), and write one mirror_asset_fills row plus its
    paper_book group (ref "sig:<signal_id>") in one commit. Advance
    last_signal_id past every signal considered. A perp FLIP through zero
    is ONE mirror_asset_fills row and ONE FILL_BASE leg of the net signed
    delta, with REALIZED_PNL for the closed part only (the unique keys
    allow one row per (allocation, signal) and one leg per (ref, kind,
    asset)); valuation's _PerpBook already resets the entry on a flip.
    (Also: creator_position_unknown, when a sell or a perp signal carries
    no position_before_atoms, so the fraction to copy is unknown.)

value_asset_allocation(db, allocation, marks=None, *, now=None) -> portfolio.AllocationValue
    cash_micros = paper_book cash; open_value = holdings at the freshest
    mark (prices.latest_mark mid, else the last prices_daily close, else
    cost), perps at qty x (mark - entry); asset_positions = one dict per
    position in the shape asset_view_extras documents. An unpriced holding
    is valued AT COST, never lower: snapshot_allocation's drawdown stop
    reads this equity nightly. `marks` (binary ticker marks) is ignored.
    `now` (default: the clock) is the instant marks are looked up at.

asset_view_extras(db, allocation, value) -> dict
    Merged into portfolio.allocation_view for asset allocations. Keys:
    "positions" (replaces the binary list; each {kind spot|equity|perp,
    symbol, side long|short, qty str, cost float, mark str|None,
    mark_source quote|close|cost, mark_as_of iso|None, value float,
    unrealized float, entry str|None}), "venue", "asset_class",
    "copy_lag_median_seconds" (int|None), "skipped" ({reason: count}).
    Per kind: spot/equity cost = average cost of the holding (cash paid,
    fees excluded), value = qty x mark (or cost), unrealized = value - cost,
    entry None. Perps: cost = |qty| x entry (the notional at entry), value =
    unrealized = qty x (mark - entry), the position's contribution to
    equity (margin is cash); 0 while unpriced. So for every allocation
    equity = cash + sum of position values.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from fractions import Fraction

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alphamarket import paper_book, prices, valuation_db
from alphamarket.connectors.base import (
    Action,
    InstrumentType,
    LegKind,
    PaperFeeModel,
    RegulatoryFees,
    VenueProfile,
)
from alphamarket.connectors.registry import get_profile
from alphamarket.jobs import aware
from alphamarket.models import (
    Allocation,
    AllocationStatus,
    Instrument,
    MirrorAssetFill,
    MirrorLedgerEntry,
    MirrorSkip,
    Signal,
    Strategy,
    VenueAccount,
    utcnow,
)
from alphamarket.money import MICROS_PER_UNIT
from alphamarket.paper_book import (
    CASH_ASSET,
    FollowerBook,
    LegSpec,
    cash_leg,
    cost_micros_up,
)
from alphamarket.valuation import CASH_AT_PAR, PERP_SUFFIX, parse_price

logger = logging.getLogger(__name__)

# ---- skip reasons (mirror_skips.reason) ---------------------------------------------

STALE_SIGNAL = "stale_signal"
CREATOR_EQUITY_UNKNOWN = "creator_equity_unknown"
CREATOR_POSITION_UNKNOWN = "creator_position_unknown"
NON_USD_QUOTE = "non_usd_quote"
BELOW_MINIMUM_SIZE = "below_minimum_size"
INSUFFICIENT_CASH = "insufficient_cash"
NO_POSITION = "no_position"
FOLLOWER_EQUITY_ZERO = "follower_equity_zero"
INSTRUMENT_UNKNOWN = "instrument_unknown"  # a signal without an instrument (never emitted)

CENT_MICROS = 10_000


# ---- exact arithmetic helpers ---------------------------------------------------------


def decimal_str(value: Fraction, max_places: int = 12) -> str:
    """An exact decimal string for a terminating fraction (every price this
    module makes: quote strings and creator price x (10,000 +/- bps) / 10,000).
    A non-terminating value (an average entry) is truncated toward zero at
    `max_places`; such strings are for display only."""
    for places in range(max_places + 1):
        scaled = value * 10**places
        if scaled.denominator == 1:
            break
    digits = int(scaled)  # exact when it terminated; truncation otherwise
    return format(Decimal(digits).scaleb(-places), "f")


def round_half_up(value_micros: Fraction, unit_micros: int) -> int:
    """Nearest multiple of `unit_micros`, halves up (fees are non-negative)."""
    q = value_micros / unit_micros
    return ((2 * q.numerator + q.denominator) // (2 * q.denominator)) * unit_micros


def ceil_to(value_micros: Fraction, unit_micros: int) -> int:
    """Smallest multiple of `unit_micros` >= value (regulatory fees: up to the cent)."""
    q = value_micros / unit_micros
    return -((-q.numerator) // q.denominator) * unit_micros


def floor_micros(value_micros: Fraction) -> int:
    """Floor to the micro: a credit rounds down, a debit rounds up (away from
    the follower in both cases; paper_book's rounding rule)."""
    return value_micros.numerator // value_micros.denominator


def trunc_micros(value_micros: Fraction) -> int:
    """valuation.py's rule for a position value: truncate toward zero."""
    return int(value_micros)


def _units(atoms: int, decimals: int) -> Fraction:
    return Fraction(atoms, 10**decimals)


def _notional(qty_atoms: int, decimals: int, price: str) -> Fraction:
    """Exact qty x price in USD micros."""
    return _units(qty_atoms, decimals) * parse_price(price) * MICROS_PER_UNIT


def _sign(n: int | Fraction) -> int:
    return (n > 0) - (n < 0)


# ---- price (plan D2/D4, decision E5) ------------------------------------------------------


@dataclass(frozen=True)
class PaperPrice:
    """The price a paper fill takes and why."""

    price: str  # decimal string, quote asset per unit
    basis: str  # "ask" | "bid" | "creator_price_penalty"
    penalty_bps: int | None = None
    quote_at: datetime | None = None


def paper_price(signal: Signal, action: Action, profile: VenueProfile) -> PaperPrice:
    """Buys fill at the ask, sells at the bid, of the public quote Koine
    captured when it observed the fill (copied onto the signal). Without
    that side of the quote: the creator's own price moved AGAINST the
    follower by profile.paper_fallback_penalty_bps,

        buy:  p = creator_price x (10,000 + bps) / 10,000
        sell: p = creator_price x (10,000 - bps) / 10,000
    """
    buy = action is Action.BUY
    side = signal.quote_ask if buy else signal.quote_bid
    if side is not None and parse_price(side) > 0:
        return PaperPrice(side, "ask" if buy else "bid", None, aware(signal.quote_at))
    bps = profile.paper_fallback_penalty_bps
    factor = Fraction(10_000 + bps if buy else 10_000 - bps, 10_000)
    return PaperPrice(
        decimal_str(parse_price(signal.price) * factor), "creator_price_penalty", bps, None
    )


# ---- fees (plan D10) ---------------------------------------------------------------------


def regulatory_fee_micros(
    fees: RegulatoryFees, action: Action, qty_atoms: int, decimals: int, price: str
) -> int:
    """US equity sells: SEC Section 31 fee + FINRA TAF, each rounded UP to
    the cent; buys pay nothing (commission-free).

        sec = ceil_cent(notional x sec_fee_per_million / 1,000,000)
        taf = ceil_cent(min(shares x taf_per_share, taf_max))
    """
    if action is Action.BUY:
        return 0
    notional = _notional(qty_atoms, decimals, price)
    sec = ceil_to(notional * parse_price(fees.sec_fee_per_million) / 1_000_000, CENT_MICROS)
    taf_usd = min(
        _units(qty_atoms, decimals) * parse_price(fees.taf_per_share),
        parse_price(fees.taf_max),
    )
    taf = ceil_to(taf_usd * MICROS_PER_UNIT, CENT_MICROS)
    return sec + taf


def asset_fee_micros(
    profile: VenueProfile, action: Action, qty_atoms: int, decimals: int, price: str
) -> int:
    """The paper fee of one fill under the venue's PaperFeeModel:

    bps_of_notional:      round_half_up(qty x price x fee_rate_bps / 10,000,
                                        to fee_rounding_micros)
    us_equity_regulatory: regulatory_fee_micros (profile.regulatory_fees)
    """
    model = profile.paper_fee_model
    if model is PaperFeeModel.BPS_OF_NOTIONAL:
        raw = _notional(qty_atoms, decimals, price) * profile.fee_rate_bps / 10_000
        return round_half_up(raw, profile.fee_rounding_micros)
    if model is PaperFeeModel.US_EQUITY_REGULATORY:
        if profile.regulatory_fees is None:
            raise ValueError(f"{profile.venue}: us_equity_regulatory needs regulatory_fees")
        return regulatory_fee_micros(profile.regulatory_fees, action, qty_atoms, decimals, price)
    raise ValueError(f"{profile.venue}: {model} is not an asset paper fee model")


# ---- sizing (plan D8/D9, decision E6) -----------------------------------------------------


@dataclass(frozen=True)
class Sized:
    """A follower order: `qty_atoms` (unsigned, at the instrument's decimals)
    of which `close_atoms` reduce the follower's existing position and the
    rest open or add. `reason` is "ok" or why the signal is skipped."""

    qty_atoms: int
    close_atoms: int
    reason: str

    @property
    def open_atoms(self) -> int:
        return self.qty_atoms - self.close_atoms


def _meets_minimums(qty_atoms: int, instrument: Instrument, price: str) -> bool:
    if qty_atoms <= 0 or qty_atoms < (instrument.min_qty_atoms or 0):
        return False
    if instrument.min_notional is None:
        return True
    minimum = parse_price(instrument.min_notional) * MICROS_PER_UNIT
    return _notional(qty_atoms, instrument.qty_decimals, price) >= minimum


def _floor_step(atoms: int, step: int) -> int:
    step = max(1, step or 1)
    return atoms // step * step


def max_affordable_atoms(
    qty_atoms: int,
    cash_micros: int,
    instrument: Instrument,
    price: str,
    profile: VenueProfile,
) -> int:
    """The largest stepped quantity <= qty_atoms whose cost (rounded up) plus
    fee fits in cash. Cost and fee are monotone in quantity, so a binary
    search over steps finds it exactly."""
    dec, step = instrument.qty_decimals, max(1, instrument.qty_step_atoms or 1)

    def total(atoms: int) -> int:
        return cost_micros_up(atoms, dec, price) + asset_fee_micros(
            profile, Action.BUY, atoms, dec, price
        )

    if cash_micros <= 0:
        return 0
    if total(qty_atoms) <= cash_micros:
        return qty_atoms
    lo, hi = 0, qty_atoms // step
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if total(mid * step) <= cash_micros:
            lo = mid
        else:
            hi = mid - 1
    return lo * step


def creator_split(signal: Signal) -> tuple[Fraction, int] | None:
    """How the creator's fill splits into closing and opening, from the
    signed positions around it (at qty_decimals):

        closed = min(qty, |before|) when the fill is against the position
        fraction = closed / |before|   (1 when the creator went flat or flipped)
        opening = qty - closed         (atoms that open or add)

    None when position_before_atoms is unknown."""
    before = signal.position_before_atoms
    if before is None:
        return None
    qty = signal.qty_atoms or 0
    delta_sign = 1 if Action(signal.action) is Action.BUY else -1
    if before != 0 and _sign(before) != delta_sign:
        closed = min(qty, abs(before))
        return Fraction(closed, abs(before)), qty - closed
    return Fraction(0), qty


def size_asset_order(
    signal: Signal,
    instrument: Instrument,
    profile: VenueProfile,
    *,
    price: str,
    position_atoms: int,
    follower_equity: Callable[[], int],
    cash_micros: int,
) -> Sized:
    """Size one follower order (pure; see the methodology section).

    Closing part (the creator reduced, closed or flipped its position): the
    follower closes the same FRACTION of its own position in the same
    direction, floored to the step; all of it when the creator went flat or
    flipped. Opening part, with F = follower equity, C = creator equity
    before the fill:

        spot/equity buy: q = creator_qty x F / C
        perp:            q = creator_qty x creator_price x F / C / fill_price
                         (the notional scales, so leverage is mirrored)

    floored to instrument.qty_step_atoms, skipped below min_qty_atoms or
    min_notional, and (spot/equity buys) capped by cash including the fee.
    A partial close below the minimums is skipped too; a full close never is.
    Spot is never sold short: a spot sell only closes. close_atoms is the
    part of the order that runs against the follower's OWN position (which
    can differ from the creator's after a skip), so realized P&L is booked
    whenever a follower position shrinks."""
    perp = instrument.type == InstrumentType.PERP
    dec, step = instrument.qty_decimals, instrument.qty_step_atoms or 1
    action = Action(signal.action)
    split = creator_split(signal)
    if split is None:
        if perp or action is Action.SELL:
            return Sized(0, 0, CREATOR_POSITION_UNKNOWN)
        split = (Fraction(0), signal.qty_atoms or 0)
    fraction, opening_creator = split
    if not perp and action is Action.SELL:
        opening_creator = 0  # no paper short sales of spot or equities
    reasons: list[str] = []

    # closing part
    close = 0
    if fraction > 0:
        same_way = position_atoms != 0 and _sign(position_atoms) == _sign(
            signal.position_before_atoms
        )
        if not same_way:
            reasons.append(NO_POSITION)
        elif fraction >= 1:
            close = abs(position_atoms)
        else:
            close = min(_floor_step(int(abs(position_atoms) * fraction), step), abs(position_atoms))
            if close < abs(position_atoms) and not _meets_minimums(close, instrument, price):
                close = 0
                reasons.append(BELOW_MINIMUM_SIZE)

    # opening part
    opening = 0
    if opening_creator > 0:
        creator_equity = signal.creator_equity_micros or 0
        f_equity = follower_equity() if creator_equity > 0 else 0
        if creator_equity <= 0:
            reasons.append(CREATOR_EQUITY_UNKNOWN)
        elif f_equity <= 0:
            reasons.append(FOLLOWER_EQUITY_ZERO)
        else:
            units = _units(opening_creator, signal.qty_decimals) * Fraction(
                f_equity, creator_equity
            )
            if perp:
                units = units * parse_price(signal.price) / parse_price(price)
            opening = _floor_step(int(units * 10**dec), step)
            if not _meets_minimums(opening, instrument, price):
                opening = 0
                reasons.append(BELOW_MINIMUM_SIZE)
            elif not perp:
                affordable = max_affordable_atoms(opening, cash_micros, instrument, price, profile)
                if affordable < opening and not _meets_minimums(affordable, instrument, price):
                    affordable = 0
                    reasons.append(INSUFFICIENT_CASH)
                opening = affordable

    total = close + opening
    if total <= 0:
        return Sized(0, 0, reasons[0] if reasons else NO_POSITION)
    order_sign = 1 if action is Action.BUY else -1
    if position_atoms != 0 and _sign(position_atoms) != order_sign:
        # The follower's book can differ from the creator's (a stale close
        # was skipped, a liquidation): whatever part of this order runs
        # against the follower's own position closes it and realizes P&L.
        close = min(total, abs(position_atoms))
    return Sized(total, close, "ok")


# ---- mirroring ------------------------------------------------------------------------------


def _account_of(db: Session, allocation: Allocation) -> VenueAccount | None:
    strategy = db.get(Strategy, allocation.strategy_id)
    return db.get(VenueAccount, strategy.venue_account_id) if strategy else None


def _profile_of(db: Session, allocation: Allocation) -> VenueProfile:
    account = _account_of(db, allocation)
    if account is None:
        raise ValueError(f"allocation {allocation.id}: strategy has no venue account")
    return get_profile(account.venue)


def _skip(db: Session, allocation: Allocation, signal: Signal, reason: str) -> None:
    logger.info("allocation %s skipped signal %s: %s", allocation.id, signal.id, reason)
    exists = db.scalar(
        select(MirrorSkip.id).where(
            MirrorSkip.allocation_id == allocation.id, MirrorSkip.signal_id == signal.id
        )
    )
    if exists is None:
        db.add(MirrorSkip(allocation_id=allocation.id, signal_id=signal.id, reason=reason))
        db.flush()


def _precheck(signal: Signal, instrument: Instrument | None, profile: VenueProfile) -> str | None:
    observed = aware(signal.observed_at) or aware(signal.filled_at)
    lag = (observed - aware(signal.filled_at)).total_seconds()
    if lag > profile.max_copy_lag_seconds:
        return STALE_SIGNAL
    if instrument is None:
        return INSTRUMENT_UNKNOWN
    if instrument.quote_asset not in CASH_AT_PAR:
        return NON_USD_QUOTE
    return None


def _position_atoms(book: FollowerBook, instrument: Instrument) -> int:
    """The follower's signed position in the instrument at its qty_decimals
    (floored toward zero if the book holds finer atoms)."""
    asset = instrument.base_asset
    if instrument.type == InstrumentType.PERP:
        held = book.perps.get(asset)
        qty = held.qty if held is not None else None
    else:
        qty = book.holdings.get(asset)
    if qty is None:
        return 0
    return int(qty.units * 10**instrument.qty_decimals)


def _legs(
    instrument: Instrument,
    action: Action,
    qty_atoms: int,
    notional_micros: int,
    fee_micros: int,
    realized_micros: int,
) -> list[LegSpec]:
    sign = 1 if action is Action.BUY else -1
    base = LegSpec(
        instrument.base_asset,
        sign * qty_atoms,
        instrument.qty_decimals,
        LegKind.FILL_BASE,
        instrument.id,
    )
    if instrument.type == InstrumentType.PERP:
        return [
            base,
            cash_leg(realized_micros, LegKind.REALIZED_PNL),
            cash_leg(-fee_micros, LegKind.FEE),
        ]
    return [
        base,
        cash_leg(-sign * notional_micros, LegKind.FILL_QUOTE),
        cash_leg(-fee_micros, LegKind.FEE),
    ]


def realized_pnl_micros(book: FollowerBook, instrument: Instrument, close_atoms: int, price: str):
    """Perp P&L realized by closing `close_atoms` of the follower's position at
    `price`: close_qty x (price - entry) x side, floored to the micro (a gain
    rounds down, a loss rounds up)."""
    if close_atoms <= 0:
        return 0
    position = book.perps[instrument.base_asset]
    if position.entry is None:
        raise ValueError(f"{instrument.base_asset}: follower entry unknown")
    side = _sign(position.qty.atoms)
    exact = (
        _units(close_atoms, instrument.qty_decimals)
        * (parse_price(price) - position.entry)
        * side
        * MICROS_PER_UNIT
    )
    return floor_micros(exact)


def mirror_asset_signal(
    db: Session,
    allocation: Allocation,
    signal: Signal,
    profile: VenueProfile,
    now: datetime | None = None,
) -> MirrorAssetFill | None:
    """Apply one asset signal to one allocation: a mirror_asset_fills row and
    its ledger group, or a mirror_skips row. Flushes; the caller commits."""
    from alphamarket.execution import get_asset_executor

    instrument = db.get(Instrument, signal.instrument_id) if signal.instrument_id else None
    reason = _precheck(signal, instrument, profile)
    if reason is not None:
        _skip(db, allocation, signal, reason)
        return None
    ref = f"sig:{signal.id}"
    if paper_book.has_group(db, allocation, ref):
        return None  # already applied (a pass that died before advancing last_signal_id)
    executor = get_asset_executor(allocation.mode)
    action = Action(signal.action)
    price = executor.quote(signal, action, profile)
    book = paper_book.load_book(db, allocation)
    sized = size_asset_order(
        signal,
        instrument,
        profile,
        price=price.price,
        position_atoms=_position_atoms(book, instrument),
        follower_equity=lambda: value_asset_allocation(db, allocation, now=now).equity_micros,
        cash_micros=book.cash_micros,
    )
    perp = instrument.type == InstrumentType.PERP
    realized = 0
    if sized.reason == "ok" and perp:
        realized = realized_pnl_micros(book, instrument, sized.close_atoms, price.price)
        fee = asset_fee_micros(
            profile, action, sized.qty_atoms, instrument.qty_decimals, price.price
        )
        if sized.open_atoms and book.cash_micros + realized - fee < 0:
            # Cannot pay the fee on the opening part: copy only the close.
            if sized.close_atoms:
                sized = Sized(sized.close_atoms, sized.close_atoms, "ok")
                realized = realized_pnl_micros(book, instrument, sized.close_atoms, price.price)
            else:
                sized = Sized(0, 0, INSUFFICIENT_CASH)
    if sized.reason != "ok":
        _skip(db, allocation, signal, sized.reason)
        return None

    executed = executor.execute(
        allocation, signal, instrument, action, sized.qty_atoms, price, profile
    )
    row = MirrorAssetFill(
        allocation_id=allocation.id,
        signal_id=signal.id,
        reason="signal",
        mode=allocation.mode,
        instrument_id=instrument.id,
        action=action.value,
        qty_atoms=executed.qty_atoms,
        qty_decimals=executed.qty_decimals,
        price=executed.price,
        notional_micros=executed.notional_micros,
        fee_micros=executed.fee_micros,
        price_basis=executed.price_basis,
        penalty_bps=executed.penalty_bps,
        quote_at=executed.quote_at,
        venue_order_id=executed.venue_order_id,
        filled_at=executed.filled_at,
    )
    db.add(row)
    paper_book.append_group(
        db,
        allocation,
        ref=ref,
        occurred_at=executed.filled_at,
        legs=_legs(
            instrument,
            action,
            executed.qty_atoms,
            executed.notional_micros,
            executed.fee_micros,
            realized,
        ),
    )
    return row


def mirror_asset_pending(db: Session, allocation: Allocation, now: datetime | None = None) -> int:
    """Apply every signal the allocation has not considered yet, in id order.
    Signals filled before the allocation started are passed over silently: a
    follower joins the creator's book going forward."""
    if allocation.status != AllocationStatus.ACTIVE:
        return 0
    signals = db.scalars(
        select(Signal)
        .where(Signal.strategy_id == allocation.strategy_id, Signal.id > allocation.last_signal_id)
        .order_by(Signal.id)
    ).all()
    if not signals:
        return 0
    from alphamarket.execution import get_asset_executor

    get_asset_executor(allocation.mode)  # a live allocation raises before writing anything
    profile = _profile_of(db, allocation)
    started = aware(allocation.started_at)
    mirrored = 0
    for signal in signals:
        copyable = signal.is_asset and aware(signal.filled_at) >= started
        if copyable and mirror_asset_signal(db, allocation, signal, profile, now) is not None:
            mirrored += 1
        allocation.last_signal_id = signal.id
    db.flush()
    return mirrored


def process_asset_allocation(
    db: Session, allocation: Allocation, now: datetime | None = None
) -> tuple[int, int]:
    from alphamarket.paper_lifecycle import apply_lifecycle

    now = now or utcnow()
    try:
        mirrored = mirror_asset_pending(db, allocation, now)
        applied = apply_lifecycle(db, allocation, now)
        db.commit()  # releases worker.handle_mirror's row lock
    except Exception:
        db.rollback()
        raise
    return mirrored, applied


# ---- valuation ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Marked:
    price: Fraction  # USD per unit
    text: str
    source: str  # "quote" | "close"
    as_of: str | None


def _instruments_by_asset(db: Session, allocation: Allocation) -> dict[str, Instrument]:
    rows = db.execute(
        select(MirrorLedgerEntry.asset, Instrument)
        .join(Instrument, Instrument.id == MirrorLedgerEntry.instrument_id)
        .where(MirrorLedgerEntry.allocation_id == allocation.id)
        .order_by(MirrorLedgerEntry.id)
    ).all()
    return {asset: inst for asset, inst in rows}


def _mark(
    db: Session,
    instrument: Instrument | None,
    asset: str,
    venues: tuple[str, ...],
    now: datetime,
) -> _Marked | None:
    """The freshest mark: a fresh public quote (prices.latest_mark), else the
    last daily close on or before today (a perp falls back to its coin's)."""
    if instrument is not None and instrument.quote_asset in CASH_AT_PAR:
        mark = prices.latest_mark(db, instrument.id, now)
        if mark is not None and parse_price(mark.price) > 0:
            return _Marked(
                parse_price(mark.price), mark.price, "quote", aware(mark.as_of).isoformat()
            )
    candidates = [asset]
    if asset.endswith(PERP_SUFFIX):
        candidates.append(asset.removesuffix(PERP_SUFFIX))
    for candidate in candidates:
        closes = valuation_db.price_inputs(db, {candidate}, None, aware(now).date(), venues)
        if closes:
            last = closes[-1]
            unit = Fraction(last.usd_micros_per_unit, MICROS_PER_UNIT)
            return _Marked(unit, decimal_str(unit), "close", last.day.isoformat())
    return None


def cost_basis_micros(db: Session, allocation: Allocation) -> dict[str, int]:
    """Average cost (cash paid, fees excluded) of each spot/equity holding:
    a buy adds its cash paid; a sell removes cost x sold / held; a split
    changes the quantity, never the cost. Truncated to the micro."""
    entries = db.scalars(
        select(MirrorLedgerEntry)
        .where(MirrorLedgerEntry.allocation_id == allocation.id)
        .order_by(MirrorLedgerEntry.occurred_at, MirrorLedgerEntry.id)
    ).all()
    groups: dict[str, list[MirrorLedgerEntry]] = {}
    for e in entries:
        groups.setdefault(e.ref, []).append(e)
    qty: dict[str, Fraction] = {}
    cost: dict[str, Fraction] = {}
    for legs in groups.values():
        paid = -sum(e.delta_atoms for e in legs if e.kind == LegKind.FILL_QUOTE)
        for e in legs:
            if e.asset == CASH_ASSET or e.asset.endswith(PERP_SUFFIX):
                continue
            delta = _units(e.delta_atoms, e.decimals)
            held = qty.get(e.asset, Fraction(0))
            if e.kind == LegKind.FILL_BASE and delta > 0:
                cost[e.asset] = cost.get(e.asset, Fraction(0)) + max(paid, 0)
            elif e.kind == LegKind.FILL_BASE and delta < 0 and held > 0:
                cost[e.asset] = cost.get(e.asset, Fraction(0)) * (held + delta) / held
            qty[e.asset] = held + delta
            if qty[e.asset] <= 0:
                cost[e.asset] = Fraction(0)
    return {asset: int(c) for asset, c in cost.items()}


def _pricing_venues(db: Session, allocation: Allocation) -> tuple[str, ...]:
    account = _account_of(db, allocation)
    return valuation_db.pricing_venues(account) if account is not None else ()


def _kind(instrument: Instrument | None) -> str:
    if instrument is None:
        return "spot"
    return {InstrumentType.EQUITY: "equity", InstrumentType.PERP: "perp"}.get(
        instrument.type, "spot"
    )


def value_asset_allocation(
    db: Session, allocation: Allocation, marks=None, *, now: datetime | None = None
):
    """See the module docstring. Spot/equity holdings: qty x mark, truncated
    toward zero (valuation.py's rule), else their cost. Perps: qty x (mark -
    entry), truncated toward zero, else 0."""
    from alphamarket.portfolio import AllocationValue
    from alphamarket.stats import micros_to_float

    now = now or utcnow()
    book = paper_book.load_book(db, allocation)
    instruments = _instruments_by_asset(db, allocation)
    venues = _pricing_venues(db, allocation)
    costs = cost_basis_micros(db, allocation) if book.holdings else {}
    positions: list[dict] = []
    open_cost = open_value = unmarked = 0
    for asset, qty in sorted(book.holdings.items()):
        if asset.endswith(PERP_SUFFIX):
            continue
        instrument = instruments.get(asset)
        cost = costs.get(asset, 0)
        marked = _mark(db, instrument, asset, venues, now)
        if marked is None:
            value = cost
            unmarked += cost
        else:
            value = trunc_micros(qty.units * marked.price * MICROS_PER_UNIT)
        open_cost += cost
        open_value += value
        positions.append(
            {
                "kind": _kind(instrument),
                "symbol": instrument.symbol if instrument is not None else asset,
                "side": "long" if qty.atoms > 0 else "short",
                "qty": decimal_str(abs(qty.units)),
                "cost": micros_to_float(cost),
                "mark": marked.text if marked else None,
                "mark_source": marked.source if marked else "cost",
                "mark_as_of": marked.as_of if marked else None,
                "value": micros_to_float(value),
                "unrealized": micros_to_float(value - cost),
                "entry": None,
            }
        )
    for asset, position in sorted(book.perps.items()):
        instrument = instruments.get(asset)
        marked = _mark(db, instrument, asset, venues, now)
        entry = position.entry
        if marked is None or entry is None:
            unrealized = 0
        else:
            unrealized = trunc_micros(position.qty.units * (marked.price - entry) * MICROS_PER_UNIT)
        open_value += unrealized
        entry_notional = (
            trunc_micros(abs(position.qty.units) * entry * MICROS_PER_UNIT) if entry else 0
        )
        positions.append(
            {
                "kind": "perp",
                "symbol": instrument.symbol if instrument is not None else asset,
                "side": "long" if position.qty.atoms > 0 else "short",
                "qty": decimal_str(abs(position.qty.units)),
                "cost": micros_to_float(entry_notional),
                "mark": marked.text if marked else None,
                "mark_source": marked.source if marked else "cost",
                "mark_as_of": marked.as_of if marked else None,
                "value": micros_to_float(unrealized),
                "unrealized": micros_to_float(unrealized),
                "entry": decimal_str(entry, 8) if entry is not None else None,
            }
        )
    n_fills = db.scalar(
        select(func.count(MirrorAssetFill.id)).where(MirrorAssetFill.allocation_id == allocation.id)
    )
    return AllocationValue(
        cash_micros=book.cash_micros,
        open_cost_micros=open_cost,
        open_value_micros=open_value,
        unmarked_cost_micros=unmarked,
        positions=[],
        n_fills=n_fills or 0,
        n_settlements=0,
        asset_positions=tuple(positions),
    )


# ---- read side ----------------------------------------------------------------------------


def copy_lag_median_seconds(db: Session, allocation: Allocation) -> int | None:
    """Median of observed_at - filled_at over the signals this allocation
    copied, whole seconds (the lower middle's and upper middle's mean,
    floored, for an even count). None before the first copy."""
    rows = db.execute(
        select(Signal.observed_at, Signal.filled_at)
        .join(MirrorAssetFill, MirrorAssetFill.signal_id == Signal.id)
        .where(MirrorAssetFill.allocation_id == allocation.id, Signal.observed_at.is_not(None))
    ).all()
    lags = sorted(int((aware(obs) - aware(filled)).total_seconds()) for obs, filled in rows)
    if not lags:
        return None
    mid = len(lags) // 2
    if len(lags) % 2:
        return lags[mid]
    return (lags[mid - 1] + lags[mid]) // 2


def skipped_counts(db: Session, allocation: Allocation) -> dict[str, int]:
    rows = db.execute(
        select(MirrorSkip.reason, func.count(MirrorSkip.id))
        .where(MirrorSkip.allocation_id == allocation.id)
        .group_by(MirrorSkip.reason)
    ).all()
    return {reason: count for reason, count in sorted(rows)}


def asset_view_extras(db: Session, allocation: Allocation, value) -> dict:
    account = _account_of(db, allocation)
    profile = get_profile(account.venue) if account is not None else None
    return {
        "positions": list(value.asset_positions),
        "venue": account.venue if account is not None else None,
        "asset_class": profile.asset_classes[0] if profile and profile.asset_classes else None,
        "copy_lag_median_seconds": copy_lag_median_seconds(db, allocation),
        "skipped": skipped_counts(db, allocation),
    }

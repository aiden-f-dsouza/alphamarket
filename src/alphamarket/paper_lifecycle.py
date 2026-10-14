"""Lifecycle events for paper ASSET allocations (wave2, w2-p6-lifecycle).

Contract:

apply_lifecycle(db, allocation, now) -> int
    Apply, idempotently (paper_book refs), every event due up to `now`:
    - perp funding from funding_rates for open perps (ref
      "fund:<instrument_id>:<epoch>"): payment = -q x mark x rate, mark from
      the rate row, else the quote, else the close; credits floor, debits
      ceil (paper_book rounding rule);
    - liquidation (Hyperliquid cross margin, plan D11) when a fresh mark
      (settings.quote_max_age_seconds) shows account value < maintenance
      margin. Per open perp: notional = |q| x mark; tier = the entry of
      Instrument.margin_tiers (ascending [{"lower_bound": str,
      "max_leverage": int}]) with the largest lower_bound <= notional;
      mmr = 1 / (2 x max_leverage); MM = sum notional x mmr; AV =
      cash_micros + sum of qty x (mark - entry). margin_tiers NULL -> no
      check for that position (never guess a leverage). Exit test: BTC
      max_leverage 40, q 0.09998, entry 60,010: mark 51,000 no liquidation
      (AV 96.164303 >= MM 63.73725); mark 50,500 liquidates (AV 46.174303 <
      MM 63.112375). On liquidation close every perp at the mark (mirror_asset_fills reason
      "liquidation" + FILL_BASE and LIQUIDATION legs, ref
      "liq:<instrument_id>:<epoch>"), then portfolio.stop_allocation(...,
      "liquidated");
    - corporate_actions for held equities (ref "ca:<id>"): split ->
      SPLIT leg floor(q x new/old) - q (fraction dropped); cash dividend ->
      holding at end of ex-date minus one day x cash_per_share, credited on
      pay_date (floor); symbol_change -> not applied in v2; the follower
      sees it on /portfolio (paper.symbol_change_notices).
    - the stop close (decisions 8 and 9; close_stopped), last: a STOPPED
      allocation whose book is not flat closes every crypto/stock position
      at a fresh quote. Spot and equity holdings sell at the bid; a long
      perp sells at the bid, a short perp buys at the ask. The side comes
      from the freshest asset_quotes row that has it, no older than
      settings.quote_max_age_seconds (prices.fresh_side); with none, that
      instrument waits for a later pass (never a stale mark, never the
      last price) while the others close. One mirror_asset_fills row
      (reason "stop_close", price_basis "bid"/"ask", signal_id None) and
      one paper_book group (ref "stop:<instrument_id>:<allocation_id>")
      per instrument, priced, charged and booked by the same code as a
      copied signal (execution.AssetPaperExecutor.execute_at,
      paper.fill_amounts, paper.realized_pnl_micros, paper.record_asset_fill);
      the close is never skipped for minimums or cash. Dated at `now`, or
      the newest event already in the book if later. A flat book, an
      ACTIVE allocation, or an instrument whose ref is booked adds nothing,
      so a liquidation (which already flattened the perps) followed by a
      stop never closes twice. Binary allocations never reach this module.
    - a split after the stop close (correct_late_splits): a split whose
      corporate action arrives after the stop close but whose ex-date is at
      or before it is booked on the pre-close holding, while the close sold
      the pre-split quantity at a post-split quote, so the book holds the
      split's delta again. Per such SPLIT leg (ref "ca:<id>", booked after
      the close, dated at or before it), one group
      "stop:<instrument_id>:<allocation_id>:ca:<id>" dated at the close and
      priced at the close's own price and basis (the quote that was fresh
      at that instant; never a new or stale one) sells the delta (a reverse
      split buys it back). Its cash and fee legs are the DIFFERENCE between
      one close of the corrected quantity (paper.fill_amounts) and what the
      close and earlier corrections booked, so the stop close's groups add
      up to "split, then close" to the micro (regulatory fees are rounded
      up to the cent per fill and FINRA's TAF is capped, so they do not
      add up share by share; a reverse split's correction refunds fee).
      Run right after the split is booked (before a later dividend is
      computed on the phantom shares) and again by close_stopped.
    closing_waits(db, allocation, now) lists the positions a closing tail
    still waits on (no fresh bid/ask): the tail page names them.
    Returns the number of events applied.

close_now(db, allocation, now=None) -> int
    close_stopped inline, right after a stop (tails.stop_by_user,
    portfolio.snapshot_allocation's drawdown stop): database only, no venue
    call. Takes the allocation's row lock (FOR UPDATE: it waits for a mirror
    pass holding it, so the two never close twice), runs the stage, writes
    the book checkpoint under that lock like process_asset_allocation, and
    COMMITS. A non-asset or non-stopped allocation is left alone (0).

Implementation notes (methodology "Paper copying v2: funding, liquidation,
corporate actions"; tests/test_paper_lifecycle.py pins every number):

- Order within a pass: funding, then corporate actions, then the
  liquidation check, so the check sees the cash funding just moved, then
  the stop close, so funding and splits due before the close are booked on
  the position they applied to, and a perp the liquidation just closed is
  flat (not closed twice).
- Every USD amount is computed exactly (Fraction) and then FLOORED to the
  micro: a credit rounds down, a debit rounds up in magnitude (against the
  follower, paper_book's rule).
- Funding: per perp instrument the allocation ever traded, every rate row
  from the first paper fill of it to `now` that is not booked yet (its ref
  is the idempotency key). Once a payment is booked, rows older than both
  the newest booked payment and now - FUNDING_RETRY_WINDOW (7 days) are
  not looked at again; while the position is flat, rows after its last
  fill are not either (nothing is owed). The position is the paper
  position at `at` (inclusive). A row with no position is skipped; a row
  with no mark yet is left unbooked and retried on later passes (a close
  or a quote can arrive later), never guessed, and given up once it leaves
  the window. The quote is used only when within quote_max_age of `at`;
  the close is the last complete UTC daily close before `at`'s day.
  (w3-perf: a window whose rows are all booked is checked on the rows'
  times alone; the positions at the unbooked rows come from one forward
  pass over the perp fills, _PositionsAt; the book itself from
  paper_book.book_state, which equals load_book.)
- Each stage (funding, corporate actions, liquidation, stop close) runs in its own
  savepoint: a stage that raises is rolled back and logged, and the next
  stage still runs, so a funding bug never stops the liquidation check.
- Liquidation is checked only when EVERY open perp has a fresh mark
  (prices.latest_mark, and its age re-checked here) and a known entry, and
  at least one has a margin tier. Positions without a tier add their P&L to
  AV and nothing to MM. Legs and the mirror_asset_fills row are dated
  `now` (the check), priced at the mark. The allocation is stopped with
  reason "liquidated" unless it was already stopped (its first reason is
  kept). No liquidation fee is charged [VERIFY Hyperliquid].
- Corporate actions match an EQUITY instrument's asset code (base_asset,
  the ticker) on corporate_actions.symbol. The same action reported by two
  sources (same symbol, kind, ex_date) is applied once: the lowest id
  among the rows complete enough to apply (ratios; pay date and amount)
  wins, and a group with any applied id is never applied again. A split applies
  at 00:00 UTC of the ex-date to the holding just before it; a dividend is
  computed on that same holding (end of ex-date minus one day) and
  credited at 00:00 UTC of the pay date, not before. A dividend without a
  pay date waits. Symbol changes are logged and skipped here; the
  portfolio page tells the follower (paper.symbol_change_notices).
- A source row that cannot be parsed (funding rate or mark, margin tiers,
  cash per share) is skipped and logged, never guessed, and never allowed
  to fail the pass (the caller commits signals and lifecycle together).

apply_lifecycle flushes, never commits: the caller
(paper.process_asset_allocation) commits with the rest of the pass.
close_now is the one entry point here that commits.
"""

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from fractions import Fraction

from sqlalchemy import func, or_, select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.orm import Session

from alphamarket import paper_book, prices
from alphamarket.config import get_settings
from alphamarket.connectors.base import Action, CorporateActionKind, InstrumentType, LegKind
from alphamarket.jobs import aware
from alphamarket.models import (
    Allocation,
    AllocationStatus,
    CorporateActionRow,
    FundingRateRow,
    Instrument,
    MirrorAssetFill,
    MirrorLedgerEntry,
    utcnow,
)
from alphamarket.paper_book import CASH_ASSET, CASH_DECIMALS, LegSpec
from alphamarket.valuation import CASH_AT_PAR, PERP_SUFFIX, LedgerFold, PerpPosition, parse_price

logger = logging.getLogger(__name__)

MICROS = 1_000_000
LIQUIDATED = "liquidated"
STOP_CLOSE = "stop_close"  # mirror_asset_fills.reason of a stop close
_EPSILON = timedelta(microseconds=1)
# How long a funding row that has no mark yet is retried (a close for D-1, or
# a quote near `at`, can arrive later) before it is given up.
FUNDING_RETRY_WINDOW = timedelta(days=7)


def apply_lifecycle(db: Session, allocation: Allocation, now: datetime) -> int:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware (invariant 2)")
    now = now.astimezone(UTC)
    applied = 0
    for stage in (apply_funding, apply_corporate_actions, check_liquidation, close_stopped):
        applied += _stage_isolated(db, allocation, stage, now)
    return applied


def _stage_isolated(db: Session, allocation: Allocation, stage, now: datetime) -> int:
    """One stage inside a savepoint: a stage that raises is rolled back and
    logged, and the next stage still runs (a funding bug must never stop the
    liquidation check). Every stage is idempotent, so the next pass retries
    it. A lost database connection propagates (the whole pass retries)."""
    try:
        with db.begin_nested():
            return stage(db, allocation, now)
    except (OperationalError, InterfaceError):
        raise
    except Exception:
        logger.exception("allocation %s: lifecycle stage %s raised", allocation.id, stage.__name__)
        return 0


# ---- shared helpers ----------------------------------------------------------------


def floor_micros(usd: Fraction) -> int:
    """An exact USD amount floored to whole micros: credits round down,
    debits round up in magnitude (always against the follower)."""
    return math.floor(usd * MICROS)


def _epoch(ts: datetime) -> int:
    return int(aware(ts).timestamp())


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=UTC)


def _paper_instruments(db: Session, allocation: Allocation, type_: InstrumentType):
    """Instruments of `type_` this allocation ever booked a FILL_BASE leg on."""
    ids = (
        select(MirrorLedgerEntry.instrument_id)
        .where(
            MirrorLedgerEntry.allocation_id == allocation.id,
            MirrorLedgerEntry.kind == LegKind.FILL_BASE,
            MirrorLedgerEntry.instrument_id.is_not(None),
        )
        .distinct()
    )
    return db.scalars(
        select(Instrument).where(Instrument.id.in_(ids), Instrument.type == type_)
    ).all()


def _perp_instruments(db: Session, allocation: Allocation) -> list[Instrument]:
    """Perp instruments with a paper fill (mirror_asset_fills is the perp book)."""
    ids = (
        select(MirrorAssetFill.instrument_id)
        .where(MirrorAssetFill.allocation_id == allocation.id)
        .distinct()
    )
    return list(
        db.scalars(
            select(Instrument)
            .where(Instrument.id.in_(ids), Instrument.type == InstrumentType.PERP)
            .order_by(Instrument.id)
        ).all()
    )


def _max_age() -> timedelta:
    return timedelta(seconds=get_settings().quote_max_age_seconds)


# ---- funding -----------------------------------------------------------------------


def funding_payment_micros(qty: Fraction, mark: Fraction, rate: Fraction) -> int:
    """payment = -q x mark x rate (USD), floored to the micro. A long pays a
    positive rate; a short receives it."""
    return floor_micros(-qty * mark * rate)


def _funding_watermark(db: Session, allocation: Allocation, inst: Instrument) -> datetime | None:
    return aware(
        db.scalar(
            select(func.max(MirrorLedgerEntry.occurred_at)).where(
                MirrorLedgerEntry.allocation_id == allocation.id,
                MirrorLedgerEntry.instrument_id == inst.id,
                MirrorLedgerEntry.kind == LegKind.FUNDING,
            )
        )
    )


def _first_fill_at(db: Session, allocation: Allocation, inst: Instrument) -> datetime | None:
    return aware(
        db.scalar(
            select(func.min(MirrorAssetFill.filled_at)).where(
                MirrorAssetFill.allocation_id == allocation.id,
                MirrorAssetFill.instrument_id == inst.id,
            )
        )
    )


def _close_before(db: Session, inst: Instrument, at: datetime) -> Fraction | None:
    """The last complete UTC daily close before `at`'s day: the perp's own
    USD close, else its underlying coin's, through the pricing venue."""
    from alphamarket import valuation_db
    from alphamarket.connectors.registry import get_profile

    try:
        venues: tuple[str, ...] = (get_profile(inst.venue).pricing_venue or inst.venue,)
    except ValueError:
        venues = (inst.venue,)
    end = at.astimezone(UTC).date() - timedelta(days=1)
    underlying = inst.base_asset.removesuffix(PERP_SUFFIX)
    rows = valuation_db.price_inputs(db, {inst.base_asset, underlying}, None, end, venues)
    for asset in (inst.base_asset, underlying):
        closes = [p for p in rows if p.asset == asset]
        if closes:
            return Fraction(closes[-1].usd_micros_per_unit, MICROS)
    return None


def _funding_mark(db: Session, inst: Instrument, row: FundingRateRow) -> Fraction | None:
    if row.mark_price:
        return parse_price(row.mark_price)
    at = aware(row.at)
    mark = prices.latest_mark(db, inst.id, at)
    if mark is not None and abs(at - aware(mark.as_of)) <= _max_age():
        return parse_price(mark.price)
    return _close_before(db, inst, at)


def _last_fill_at(db: Session, allocation: Allocation, inst: Instrument) -> datetime | None:
    return aware(
        db.scalar(
            select(func.max(MirrorAssetFill.filled_at)).where(
                MirrorAssetFill.allocation_id == allocation.id,
                MirrorAssetFill.instrument_id == inst.id,
            )
        )
    )


def _booked_funding_refs(
    db: Session, allocation: Allocation, inst: Instrument, since: datetime
) -> set[str]:
    return set(
        db.scalars(
            select(MirrorLedgerEntry.ref)
            .where(
                MirrorLedgerEntry.allocation_id == allocation.id,
                MirrorLedgerEntry.instrument_id == inst.id,
                MirrorLedgerEntry.kind == LegKind.FUNDING,
                MirrorLedgerEntry.occurred_at >= since,
            )
            .distinct()
        ).all()
    )


def _funding_window(
    db: Session, allocation: Allocation, inst: Instrument, book, now: datetime
) -> tuple[datetime, datetime] | None:
    """[since, until] of the funding rows to look at for one perp, or None.

    since: the first paper fill of the perp, then (once funding is booked)
    the earlier of the newest booked payment and now - FUNDING_RETRY_WINDOW,
    so a row that had no mark is retried for that long before it is given
    up. until: now, or the last fill of the perp while the position is flat
    (no row after it can owe anything)."""
    first = _first_fill_at(db, allocation, inst)
    if first is None:
        return None
    since = first
    watermark = _funding_watermark(db, allocation, inst)
    if watermark is not None:
        since = max(first, min(watermark, now - FUNDING_RETRY_WINDOW))
    until = now
    if inst.base_asset not in book.perps:
        last = _last_fill_at(db, allocation, inst)
        until = min(now, last) if last is not None else now
    return (since, until) if since <= until else None


class _PositionsAt:
    """The open perp positions at each instant of a non-decreasing series, in
    one forward pass over the fills (valuation.LedgerFold): position.qty.units
    and the open/flat answer equal perp_positions_at(anchor, fills, t)'s. The
    Qty scale can be coarser before a finer fill arrives (perp_positions_at
    uses the max over every fill given); funding reads only the units."""

    def __init__(self, anchor, fills: Sequence) -> None:
        self._fold = LedgerFold(anchor)
        self._fills = fills  # in (filled_at, id) order: paper_book.load_perp_fills
        self._next = 0

    def at(self, t: datetime) -> dict[str, PerpPosition]:
        while self._next < len(self._fills) and self._fills[self._next].occurred_at <= t:
            self._fold.add_fill(self._fills[self._next])
            self._next += 1
        return self._fold.perp_positions()


def apply_funding(db: Session, allocation: Allocation, now: datetime) -> int:
    applied = 0
    instruments = _perp_instruments(db, allocation)
    if not instruments:
        return 0
    book = paper_book.book_state(db, allocation)
    anchor = paper_book.anchor_for(allocation)
    perp_fills: tuple | None = None  # loaded on the first row that is not booked yet
    for inst in instruments:
        window = _funding_window(db, allocation, inst, book, now)
        if window is None:
            continue
        since, until = window
        booked = _booked_funding_refs(db, allocation, inst, since)
        window_rows = (
            select(FundingRateRow)
            .where(
                FundingRateRow.instrument_id == inst.id,
                FundingRateRow.at >= since,
                FundingRateRow.at <= until,
            )
            .order_by(FundingRateRow.at, FundingRateRow.id)
        )
        # w3-perf: most passes find every row of the window booked; check that
        # on the timestamps alone before loading the rows.
        times = db.scalars(window_rows.with_only_columns(FundingRateRow.at)).all()
        if all(f"fund:{inst.id}:{_epoch(aware(t))}" in booked for t in times):
            continue
        unpriced: list[datetime] = []
        if perp_fills is None:
            perp_fills = paper_book.load_perp_fills(db, allocation)
        positions = _PositionsAt(anchor, perp_fills)
        for row in db.scalars(window_rows).all():
            at = aware(row.at)
            ref = f"fund:{inst.id}:{_epoch(at)}"
            if ref in booked:
                continue
            position = positions.at(at).get(inst.base_asset)
            if position is None:
                continue
            try:
                rate = parse_price(row.rate)
                mark = _funding_mark(db, inst, row)
            except (ArithmeticError, TypeError, ValueError):
                # A malformed source row must not wedge every pass (the
                # caller commits signals and lifecycle together): skip it.
                logger.warning(
                    "allocation %s: unparseable funding row %s for %s; skipped",
                    allocation.id,
                    row.id,
                    inst.base_asset,
                )
                continue
            if mark is None:
                unpriced.append(at)  # retried next pass, until it leaves the window
                continue
            payment = funding_payment_micros(position.qty.units, mark, rate)
            rows = paper_book.append_group(
                db,
                allocation,
                ref=ref,
                legs=[LegSpec(CASH_ASSET, payment, CASH_DECIMALS, LegKind.FUNDING, inst.id)],
                occurred_at=at,
            )
            if rows:
                applied += 1
                booked.add(ref)
        if unpriced:
            logger.warning(
                "allocation %s: %d %s funding payments have no mark yet (oldest %s); "
                "each is retried for %s, then given up",
                allocation.id,
                len(unpriced),
                inst.base_asset,
                unpriced[0].isoformat(),
                FUNDING_RETRY_WINDOW,
            )
    return applied


# ---- liquidation -------------------------------------------------------------------


def maintenance_margin_rate(tiers: Sequence[dict] | None, notional: Fraction) -> Fraction | None:
    """mmr = 1 / (2 x max_leverage) of the tier with the largest lower_bound
    <= notional. None when there are no tiers or none applies (never guess)."""
    if not tiers:
        return None
    best: tuple[Fraction, int] | None = None
    for tier in tiers:
        lower = parse_price(str(tier["lower_bound"]))
        leverage = int(tier["max_leverage"])
        if leverage <= 0:
            raise ValueError(f"max_leverage must be positive, got {leverage}")
        if lower <= notional and (best is None or lower > best[0]):
            best = (lower, leverage)
    if best is None:
        return None
    return Fraction(1, 2 * best[1])


@dataclass(frozen=True)
class MarginCheck:
    """One cross-margin check, exact USD: liquidate when account_value <
    maintenance_margin."""

    account_value: Fraction
    maintenance_margin: Fraction

    @property
    def liquidate(self) -> bool:
        return self.account_value < self.maintenance_margin


@dataclass(frozen=True)
class _Open:
    inst: Instrument
    position: PerpPosition
    mark: Fraction
    mark_str: str
    mark_as_of: datetime


def margin_check(cash_micros: int, positions: Sequence[tuple]) -> MarginCheck | None:
    """positions: (qty, entry, mark, margin_tiers) with exact Fractions in
    USD. AV = cash + sum q x (mark - entry); MM = sum |q| x mark x mmr over
    positions that have a tier. None when no position has a tier."""
    av = Fraction(cash_micros, MICROS)
    mm = Fraction(0)
    tiered = False
    for qty, entry, mark, tiers in positions:
        av += qty * (mark - entry)
        notional = abs(qty) * mark
        mmr = maintenance_margin_rate(tiers, notional)
        if mmr is not None:
            tiered = True
            mm += notional * mmr
    if not tiered:
        return None
    return MarginCheck(av, mm)


def check_liquidation(db: Session, allocation: Allocation, now: datetime) -> int:
    book = paper_book.book_state(db, allocation, at=now)
    if not book.perps:
        return 0
    by_asset: dict[str, Instrument] = {}
    for inst in _perp_instruments(db, allocation):
        if inst.base_asset in by_asset:
            logger.warning("allocation %s: two instruments for %s", allocation.id, inst.base_asset)
            return 0
        by_asset[inst.base_asset] = inst
    max_age = _max_age()
    opens: list[_Open] = []
    for asset, position in sorted(book.perps.items()):
        inst = by_asset.get(asset)
        if inst is None or position.entry is None:
            return 0
        mark = prices.latest_mark(db, inst.id, now)
        if mark is None or now - aware(mark.as_of) > max_age:
            return 0  # no fresh mark: no check at all (AV would be a guess)
        opens.append(_Open(inst, position, parse_price(mark.price), mark.price, aware(mark.as_of)))
    try:
        check = margin_check(
            book.cash_micros,
            [(o.position.qty.units, o.position.entry, o.mark, o.inst.margin_tiers) for o in opens],
        )
    except (ArithmeticError, KeyError, TypeError, ValueError):
        # Malformed margin_tiers (or mark): never guess a leverage, and never
        # let one bad instrument row wedge the pass. No check.
        logger.warning("allocation %s: unusable margin tiers or mark; no check", allocation.id)
        return 0
    if check is None or not check.liquidate:
        return 0
    applied = 0
    for o in opens:
        qty = o.position.qty
        pnl = floor_micros(qty.units * (o.mark - o.position.entry))
        rows = paper_book.append_group(
            db,
            allocation,
            ref=f"liq:{o.inst.id}:{_epoch(now)}",
            legs=[
                LegSpec(o.inst.base_asset, -qty.atoms, qty.decimals, LegKind.FILL_BASE, o.inst.id),
                LegSpec(CASH_ASSET, pnl, CASH_DECIMALS, LegKind.LIQUIDATION, o.inst.id),
            ],
            occurred_at=now,
        )
        if not rows:
            continue
        db.add(
            MirrorAssetFill(
                allocation_id=allocation.id,
                signal_id=None,
                reason="liquidation",
                mode=allocation.mode,
                instrument_id=o.inst.id,
                action="sell" if qty.atoms > 0 else "buy",
                qty_atoms=abs(qty.atoms),
                qty_decimals=qty.decimals,
                price=o.mark_str,
                notional_micros=(
                    paper_book.proceeds_micros_down if qty.atoms > 0 else paper_book.cost_micros_up
                )(abs(qty.atoms), qty.decimals, o.mark_str),
                fee_micros=0,
                price_basis="mark",
                quote_at=o.mark_as_of,
                filled_at=now,
            )
        )
        applied += 1
    db.flush()
    if applied:
        logger.info(
            "allocation %s liquidated: AV %s < MM %s",
            allocation.id,
            float(check.account_value),
            float(check.maintenance_margin),
        )
        if allocation.status != AllocationStatus.STOPPED:
            from alphamarket.portfolio import stop_allocation

            stop_allocation(db, allocation, LIQUIDATED, commit=False)
            db.flush()
    return applied


# ---- corporate actions -------------------------------------------------------------


def split_delta_atoms(held_atoms: int, ratio_new: int, ratio_old: int) -> int:
    """SPLIT leg: floor(q x new / old) - q, in atoms. The fraction of an atom
    a split would create is dropped (no cash in lieu)."""
    if ratio_new <= 0 or ratio_old <= 0:
        raise ValueError(f"bad split ratio {ratio_new}:{ratio_old}")
    return (held_atoms * ratio_new) // ratio_old - held_atoms


def dividend_micros(held: Fraction, cash_per_share: Fraction) -> int:
    """DIVIDEND leg: shares held at the end of ex-date minus one day x cash
    per share, floored to the micro."""
    return floor_micros(held * cash_per_share)


_KIND_ORDER = {CorporateActionKind.SPLIT: 0, CorporateActionKind.CASH_DIVIDEND: 1}


def _applied_ca_ids(db: Session, allocation: Allocation) -> set[int]:
    refs = db.scalars(
        select(MirrorLedgerEntry.ref)
        .where(
            MirrorLedgerEntry.allocation_id == allocation.id,
            MirrorLedgerEntry.ref.like("ca:%"),
        )
        .distinct()
    ).all()
    return {int(ref.removeprefix("ca:")) for ref in refs}


def apply_corporate_actions(db: Session, allocation: Allocation, now: datetime) -> int:
    equities = {i.base_asset: i for i in _paper_instruments(db, allocation, InstrumentType.EQUITY)}
    if not equities:
        return 0
    start = aware(allocation.started_at).date() if allocation.started_at else None
    query = select(CorporateActionRow).where(CorporateActionRow.symbol.in_(equities))
    if start is not None:
        query = query.where(CorporateActionRow.ex_date >= start)
    rows = db.scalars(query.order_by(CorporateActionRow.id)).all()
    applied_ids = _applied_ca_ids(db, allocation)
    # One action per (symbol, kind, ex_date), whichever source reported it.
    groups: dict[tuple[str, str, date], list[CorporateActionRow]] = {}
    for row in rows:
        groups.setdefault((row.symbol, row.kind, row.ex_date), []).append(row)
    pending: list[CorporateActionRow] = []
    for (symbol, kind, ex_date), group in groups.items():
        if kind == CorporateActionKind.SYMBOL_CHANGE:
            logger.info(
                "allocation %s: symbol change %s on %s not applied (v2)",
                allocation.id,
                symbol,
                ex_date,
            )
            continue
        if kind not in _KIND_ORDER:
            continue
        if any(r.id in applied_ids for r in group):
            continue
        # The lowest id among the rows complete enough to apply, so one
        # source missing a pay date or ratio does not hold back another's.
        complete = [r for r in group if _is_complete(r)]
        if complete:
            pending.append(complete[0])
    pending.sort(key=lambda r: (r.ex_date, _KIND_ORDER[r.kind], r.id))
    applied = 0
    for row in pending:
        if _apply_action(db, allocation, equities[row.symbol], row, now):
            applied += 1
            if row.kind == CorporateActionKind.SPLIT:
                # A split dated before the stop close, booked after it: close
                # its shares now, before a later dividend is computed on them.
                applied += correct_late_splits(db, allocation, equities[row.symbol])
    return applied


def _is_complete(row: CorporateActionRow) -> bool:
    if row.kind == CorporateActionKind.SPLIT:
        return bool(row.ratio_new and row.ratio_old and row.ratio_new > 0 and row.ratio_old > 0)
    return row.pay_date is not None and bool(row.cash_per_share)


def _apply_action(
    db: Session, allocation: Allocation, inst: Instrument, row: CorporateActionRow, now: datetime
) -> bool:
    ex_at = _midnight(row.ex_date)
    if row.kind == CorporateActionKind.SPLIT:
        if now < ex_at or not row.ratio_new or not row.ratio_old:
            return False
        held = paper_book.holding_at(db, allocation, inst.base_asset, ex_at - _EPSILON)
        if held is None or held.atoms <= 0:
            return False
        delta = split_delta_atoms(held.atoms, row.ratio_new, row.ratio_old)
        legs = [LegSpec(inst.base_asset, delta, held.decimals, LegKind.SPLIT, inst.id)]
        occurred_at = ex_at
    else:  # cash dividend
        if row.pay_date is None or not row.cash_per_share:
            return False
        pay_at = _midnight(row.pay_date)
        if now < pay_at:
            return False
        try:
            per_share = parse_price(row.cash_per_share)
        except (ArithmeticError, TypeError, ValueError):
            logger.warning("corporate action %s: bad cash_per_share; skipped", row.id)
            return False
        held = paper_book.holding_at(db, allocation, inst.base_asset, ex_at - _EPSILON)
        if held is None or held.atoms <= 0 or per_share <= 0:
            return False
        amount = dividend_micros(held.units, per_share)
        legs = [LegSpec(CASH_ASSET, amount, CASH_DECIMALS, LegKind.DIVIDEND, inst.id)]
        occurred_at = pay_at
    rows = paper_book.append_group(
        db, allocation, ref=f"ca:{row.id}", legs=legs, occurred_at=occurred_at
    )
    return bool(rows)


# ---- stop close (decisions 8 and 9) ------------------------------------------------


def stop_ref(instrument_id: int, allocation_id: int) -> str:
    """The stop close's paper_book ref: one close per instrument per allocation."""
    return f"stop:{instrument_id}:{allocation_id}"


def is_flat(book) -> bool:
    """No non-cash holding left (spot, equity or perp): a paper_book state."""
    return not book.holdings


def is_closing(db: Session, allocation: Allocation) -> bool:
    """Stopped and not flat yet: the read side's "Closing" (derived, no
    status of its own). Binary allocations are never closing."""
    from alphamarket.mirror import is_asset_allocation

    if allocation.status != AllocationStatus.STOPPED or not is_asset_allocation(db, allocation):
        return False
    return not is_flat(paper_book.book_state(db, allocation))


def _close_qty(allocation: Allocation, inst: Instrument, held, book):
    """The signed quantity the stop close trades back to zero, or None (logged)
    when it cannot be closed safely: perps from the average-cost perp book
    (like liquidation), spot and equity from the holding."""
    if inst.type == InstrumentType.PERP:
        position = book.perps.get(inst.base_asset)
        if position is None or position.entry is None:
            logger.warning(
                "allocation %s: %s has no perp entry; stop close skipped",
                allocation.id,
                inst.base_asset,
            )
            return None
        return position.qty
    if held.atoms < 0:
        logger.warning(
            "allocation %s: negative %s holding; stop close skipped", allocation.id, inst.base_asset
        )
        return None
    return held


def _close_side(qty) -> tuple[Action, str]:
    """A long closes with a sell at the bid, a short with a buy at the ask."""
    return (Action.SELL, "bid") if qty.atoms > 0 else (Action.BUY, "ask")


@dataclass(frozen=True)
class CloseWait:
    """One position of a closing tail that waits for a fresh quote."""

    asset: str
    label: str  # how the instrument reads on a page
    side: str  # "bid" | "ask": the side its close takes
    last_quote_at: datetime | None  # newest quote with that side, any age (None: none kept)


def closing_waits(db: Session, allocation: Allocation, now: datetime) -> list[CloseWait]:
    """The positions a stopped tail still holds that wait for a fresh quote
    (no bid, or no ask for a short, within quote_max_age_seconds at `now`):
    what the "Closing" notice names. Read-only and quiet (no logging); empty
    when the tail is not closing or every position can close on the next
    pass."""
    if allocation.status != AllocationStatus.STOPPED:
        return []
    book = paper_book.book_state(db, allocation)
    if is_flat(book):
        return []
    from alphamarket import paper

    instruments = paper._instruments_by_asset(db, allocation)
    waits: list[CloseWait] = []
    for asset, held in sorted(book.holdings.items()):
        inst = instruments.get(asset)
        if inst is None:
            continue
        if inst.type == InstrumentType.PERP:
            position = book.perps.get(asset)
            if position is None:
                continue
            held = position.qty
        _, side = _close_side(held)
        if prices.fresh_side(db, inst.id, now, side) is not None:
            continue
        waits.append(CloseWait(asset, inst.label, side, prices.last_side_at(db, inst.id, side)))
    return waits


def close_stopped(db: Session, allocation: Allocation, now: datetime) -> int:
    """See the module docstring (the stop close). Returns instruments closed."""
    if allocation.status != AllocationStatus.STOPPED:
        return 0
    book = paper_book.book_state(db, allocation)
    if is_flat(book):
        return 0
    from alphamarket import paper
    from alphamarket.execution import get_asset_executor

    executor = get_asset_executor(allocation.mode)  # paper only: live raises
    profile = paper._profile_of(db, allocation)
    instruments = paper._instruments_by_asset(db, allocation)
    booked = paper_book.booked_through(db, allocation)
    filled_at = max(now, booked) if booked is not None else now
    applied = 0
    for asset, held in sorted(book.holdings.items()):
        inst = instruments.get(asset)
        if inst is None or inst.quote_asset not in CASH_AT_PAR:
            logger.warning(
                "allocation %s: %s has no USD instrument; stop close skipped", allocation.id, asset
            )
            continue
        qty = _close_qty(allocation, inst, held, book)
        if qty is None:
            continue
        ref = stop_ref(inst.id, allocation.id)
        if paper_book.has_group(db, allocation, ref):
            # A split booked after the close but dated before it (the close
            # sold the pre-split quantity): closed with the stop close.
            fixed = correct_late_splits(db, allocation, inst)
            if fixed:
                applied += fixed
                continue
            logger.warning(
                "allocation %s: %s closed at the stop but still held (%s); left as is",
                allocation.id,
                asset,
                qty.units,
            )
            continue
        action, side = _close_side(qty)
        quote = prices.fresh_side(db, inst.id, now, side)
        if quote is None:
            logger.info(
                "allocation %s: no fresh %s for %s; the stop close waits",
                allocation.id,
                side,
                asset,
            )
            continue
        price = paper.PaperPrice(quote.price, side, None, quote.as_of)
        size = abs(qty.atoms)
        realized = 0
        if inst.type == InstrumentType.PERP:
            realized = paper.realized_pnl_micros(book, inst, size, quote.price, qty.decimals)
        executed = executor.execute_at(inst, action, size, qty.decimals, price, profile, filled_at)
        row = paper.record_asset_fill(
            db,
            allocation,
            inst,
            action,
            executed,
            ref=ref,
            realized_micros=realized,
            signal_id=None,
            reason=STOP_CLOSE,
        )
        if row is not None:
            applied += 1
    if applied:
        logger.info("allocation %s: stop closed %d instruments", allocation.id, applied)
    return applied


def _stop_close_legs(db: Session, allocation: Allocation, ref: str) -> list[MirrorLedgerEntry]:
    """Every leg of one instrument's stop close: the close (`ref`) and its
    late-split corrections (`ref`:ca:<id>)."""
    return list(
        db.scalars(
            select(MirrorLedgerEntry)
            .where(
                MirrorLedgerEntry.allocation_id == allocation.id,
                or_(MirrorLedgerEntry.ref == ref, MirrorLedgerEntry.ref.like(f"{ref}:%")),
            )
            .order_by(MirrorLedgerEntry.id)
        ).all()
    )


def correct_late_splits(db: Session, allocation: Allocation, inst: Instrument) -> int:
    """See the module docstring (a split after the stop close). Returns the
    corrections booked."""
    if allocation.status != AllocationStatus.STOPPED or inst.type != InstrumentType.EQUITY:
        return 0
    ref = stop_ref(inst.id, allocation.id)
    close = db.scalars(
        select(MirrorAssetFill)
        .where(
            MirrorAssetFill.allocation_id == allocation.id,
            MirrorAssetFill.instrument_id == inst.id,
            MirrorAssetFill.reason == STOP_CLOSE,
        )
        .order_by(MirrorAssetFill.id)
    ).first()
    close_ids = [leg.id for leg in _stop_close_legs(db, allocation, ref) if leg.ref == ref]
    if close is None or not close_ids:
        return 0
    closed_at = aware(close.filled_at)
    late = [
        leg
        for leg in db.scalars(
            select(MirrorLedgerEntry)
            .where(
                MirrorLedgerEntry.allocation_id == allocation.id,
                MirrorLedgerEntry.asset == inst.base_asset,
                MirrorLedgerEntry.kind == LegKind.SPLIT,
                MirrorLedgerEntry.ref.like("ca:%"),
                MirrorLedgerEntry.id > max(close_ids),
            )
            .order_by(MirrorLedgerEntry.id)
        ).all()
        if aware(leg.occurred_at) <= closed_at
    ]
    if not late:
        return 0
    from alphamarket import paper
    from alphamarket.execution import ExecutedAssetFill, get_asset_executor

    get_asset_executor(allocation.mode)  # paper only: live raises
    profile = paper._profile_of(db, allocation)
    applied = 0
    for split in late:
        fix_ref = f"{ref}:{split.ref}"
        if paper_book.has_group(db, allocation, fix_ref):
            continue
        # What the close and earlier corrections booked, at one common scale.
        legs = _stop_close_legs(db, allocation, ref)
        scale = max([split.decimals] + [leg.decimals for leg in legs if leg.asset != CASH_ASSET])
        sold = -sum(
            leg.delta_atoms * 10 ** (scale - leg.decimals)
            for leg in legs
            if leg.kind == LegKind.FILL_BASE
        )
        received = sum(leg.delta_atoms for leg in legs if leg.kind == LegKind.FILL_QUOTE)
        charged = -sum(leg.delta_atoms for leg in legs if leg.kind == LegKind.FEE)
        delta = split.delta_atoms * 10 ** (scale - split.decimals)
        if sold + delta < 0:
            logger.warning(
                "allocation %s: split %s of %s exceeds the stop close; left as is",
                allocation.id,
                split.ref,
                inst.base_asset,
            )
            continue
        # One close of the corrected quantity at the close's own price.
        proceeds, fee = paper.fill_amounts(profile, Action.SELL, sold + delta, scale, close.price)
        action = Action.SELL if delta > 0 else Action.BUY
        cash = proceeds - received  # + receives more (sell), - pays back (buy)
        executed = ExecutedAssetFill(
            qty_atoms=abs(delta),
            qty_decimals=scale,
            price=close.price,
            price_basis=close.price_basis,
            notional_micros=cash if action is Action.SELL else -cash,
            fee_micros=fee - charged,
            filled_at=closed_at,
            quote_at=aware(close.quote_at),
        )
        row = paper.record_asset_fill(
            db,
            allocation,
            inst,
            action,
            executed,
            ref=fix_ref,
            realized_micros=0,
            signal_id=None,
            reason=STOP_CLOSE,
        )
        if row is not None:
            applied += 1
            logger.info(
                "allocation %s: %s split %s booked after the stop close; closed at %s",
                allocation.id,
                inst.base_asset,
                split.ref,
                close.price,
            )
    return applied


def close_now(db: Session, allocation: Allocation, now: datetime | None = None) -> int:
    """See the module docstring. Commits."""
    from alphamarket.mirror import is_asset_allocation

    if allocation.status != AllocationStatus.STOPPED or not is_asset_allocation(db, allocation):
        return 0
    now = aware(now) if now is not None else utcnow()
    try:
        # The mirror pass's lock (worker.handle_mirror), but waiting for it:
        # once we hold it, a pass that already closed is visible, and a new
        # one skips this allocation until we commit.
        locked = db.scalars(
            select(Allocation)
            .where(Allocation.id == allocation.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        applied = 0
        if locked is not None and locked.status == AllocationStatus.STOPPED:
            applied = _stage_isolated(db, locked, close_stopped, now)
            paper_book.save_checkpoint_safely(db, locked)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return applied

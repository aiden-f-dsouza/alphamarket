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
      pay_date (floor); symbol_change -> skipped in v2 (flagged).
    Returns the number of events applied.

Implementation notes (methodology "Paper copying v2: funding, liquidation,
corporate actions"; tests/test_paper_lifecycle.py pins every number):

- Order within a pass: funding, then corporate actions, then the
  liquidation check, so the check sees the cash funding just moved.
- Every USD amount is computed exactly (Fraction) and then FLOORED to the
  micro: a credit rounds down, a debit rounds up in magnitude (against the
  follower, paper_book's rule).
- Funding: per perp instrument the allocation ever traded, rate rows with
  `at` after the watermark (the latest FUNDING leg's occurred_at for that
  instrument; before any, from the first paper fill of it) and `at <= now`.
  The position is the paper position at `at` (inclusive). A row with no
  position is skipped; a row with no mark at all is skipped and logged
  (never guessed). The quote is used only when within quote_max_age of
  `at`; the close is the last complete UTC daily close before `at`'s day.
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
  pay date waits. Symbol changes are logged and skipped.
- A source row that cannot be parsed (funding rate or mark, margin tiers,
  cash per share) is skipped and logged, never guessed, and never allowed
  to fail the pass (the caller commits signals and lifecycle together).

Flushes, never commits: the caller (paper.process_asset_allocation)
commits with the rest of the pass.
"""

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from fractions import Fraction

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alphamarket import paper_book, prices
from alphamarket.config import get_settings
from alphamarket.connectors.base import CorporateActionKind, InstrumentType, LegKind
from alphamarket.jobs import aware
from alphamarket.models import (
    Allocation,
    AllocationStatus,
    CorporateActionRow,
    FundingRateRow,
    Instrument,
    MirrorAssetFill,
    MirrorLedgerEntry,
)
from alphamarket.paper_book import CASH_ASSET, CASH_DECIMALS, LegSpec
from alphamarket.valuation import PERP_SUFFIX, PerpPosition, parse_price, perp_positions_at

logger = logging.getLogger(__name__)

MICROS = 1_000_000
LIQUIDATED = "liquidated"
_EPSILON = timedelta(microseconds=1)


def apply_lifecycle(db: Session, allocation: Allocation, now: datetime) -> int:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware (invariant 2)")
    now = now.astimezone(UTC)
    applied = apply_funding(db, allocation, now)
    applied += apply_corporate_actions(db, allocation, now)
    applied += check_liquidation(db, allocation, now)
    return applied


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


def apply_funding(db: Session, allocation: Allocation, now: datetime) -> int:
    applied = 0
    instruments = _perp_instruments(db, allocation)
    if not instruments:
        return 0
    book = paper_book.load_book(db, allocation)
    for inst in instruments:
        watermark = _funding_watermark(db, allocation, inst)
        query = select(FundingRateRow).where(
            FundingRateRow.instrument_id == inst.id, FundingRateRow.at <= now
        )
        if watermark is not None:
            query = query.where(FundingRateRow.at > watermark)
        else:
            first = _first_fill_at(db, allocation, inst)
            if first is None:
                continue
            query = query.where(FundingRateRow.at >= first)
        for row in db.scalars(query.order_by(FundingRateRow.at, FundingRateRow.id)).all():
            at = aware(row.at)
            position = perp_positions_at(book.anchor, book.perp_fills, at).get(inst.base_asset)
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
                logger.warning(
                    "allocation %s: no mark for %s funding at %s; skipped",
                    allocation.id,
                    inst.base_asset,
                    at.isoformat(),
                )
                continue
            payment = funding_payment_micros(position.qty.units, mark, rate)
            rows = paper_book.append_group(
                db,
                allocation,
                ref=f"fund:{inst.id}:{_epoch(at)}",
                legs=[LegSpec(CASH_ASSET, payment, CASH_DECIMALS, LegKind.FUNDING, inst.id)],
                occurred_at=at,
            )
            if rows:
                applied += 1
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
    book = paper_book.load_book(db, allocation, at=now)
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
        held = paper_book.load_book(db, allocation, at=ex_at - _EPSILON).holdings.get(
            inst.base_asset
        )
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
        held = paper_book.load_book(db, allocation, at=ex_at - _EPSILON).holdings.get(
            inst.base_asset
        )
        if held is None or held.atoms <= 0 or per_share <= 0:
            return False
        amount = dividend_micros(held.units, per_share)
        legs = [LegSpec(CASH_ASSET, amount, CASH_DECIMALS, LegKind.DIVIDEND, inst.id)]
        occurred_at = pay_at
    rows = paper_book.append_group(
        db, allocation, ref=f"ca:{row.id}", legs=legs, occurred_at=occurred_at
    )
    return bool(rows)

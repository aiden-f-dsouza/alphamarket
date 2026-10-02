"""Signals v2 for ASSET strategies (wave2, workstream w2-p6-signals).

signals.emit_signals dispatches here for asset venues. Contract:

emit_asset_signals(db, strategy, *, limiter=None) -> int
    One Signal per post-anchor AssetFill of the strategy's account that has
    none (unique asset_fill_id), in (filled_at, id) order. Fills at or before
    the anchor never become signals; transfers and other events never do.
    Same-instant fills (one order, several fills with one filled_at) are
    common: position_before/after are a RUNNING position accumulated over
    the account's asset fills in (filled_at, id) order (before = running,
    after = before +/- qty), so fill n's after == fill n+1's before.
    equity_before is evaluated once per distinct filled_at and reused for
    ties (same-instant fills share creator_equity_micros). The exit test
    includes two fills at one instant.
    Columns (models.Signal docstring): ticker = instrument symbol, side by
    the sign of the creator's position, action buy|sell, qty_atoms /
    qty_decimals / price as the fill reported, notional_micros (USD or
    stablecoin quote only, else NULL), position_before/after_atoms (signed,
    rescaled to qty_decimals; spot holdings or the perp book),
    creator_equity_micros + equity_basis_day from valuation.equity_before
    over valuation_db.account_inputs / price_inputs (0 = unknown),
    observed_at = AssetFill.ingested_at, and the quote captured ONCE per
    instrument per pass with prices.capture_quote(...) copied into
    quote_bid/ask/last/at/source (NULL when none). Idempotent; returns rows
    added. Never raises for a budget-exhausted quote: the signal is stored
    without a quote (the engine then uses the fallback price).

Implementation notes (methodology "Signals v2 and copy lag"):
- The running position at the first fill of an instant starts from the
  ledger: spot/equity holdings of the instrument's base asset over every leg
  strictly before the instant (valuation.holdings_at), or the perp book over
  every perp fill strictly before it (valuation.perp_positions_at). Within
  the instant it chains fill by fill, including fills of that instant that
  were signalled in an earlier pass. So after_n == before_n+1 always holds
  inside an instant, and across instants whenever nothing but the fill
  moved the asset in between (a fee charged in the base asset, a coin
  deposit or a split shows up in the next "before"). A holding kept at more decimals than
  the fill reports is truncated toward zero to the fill's decimals (dust
  below the venue's own quantity precision).
- creator_equity_micros is 0 (unknown) when the valuation is not positive;
  unpriced assets are excluded, exactly as in the daily equity series.
- notional_micros = qty x price, truncated to the micro, only when the
  instrument's quote asset is USD or a stablecoin valued at par.
- A quote is captured only for instruments with at least one new signal
  inside the venue's copy-lag window (observed_at - filled_at <=
  profile.max_copy_lag_seconds), and copied only onto those signals: a
  signal that was already stale when Koine first saw it is never copied
  (the engine skips it as stale_signal), so it carries no quote and spends
  no data budget.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from fractions import Fraction
from itertools import groupby

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from alphamarket import prices, valuation_db
from alphamarket.connectors.base import BudgetExhausted, InstrumentType, RateLimited
from alphamarket.connectors.registry import get_profile
from alphamarket.jobs import aware
from alphamarket.models import AssetFill, AssetQuote, Instrument, Signal, Strategy, VenueAccount
from alphamarket.valuation import (
    CASH_AT_PAR,
    PERP_SUFFIX,
    basis_day,
    equity_before,
    holdings_at,
    parse_price,
    perp_positions_at,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Quote:
    bid: str | None
    ask: str | None
    last: str | None
    at: datetime | None
    source: str | None


_NO_QUOTE = _Quote(None, None, None, None, None)


def notional_usd_micros(
    qty_atoms: int, qty_decimals: int, price: str, quote_asset: str
) -> int | None:
    """qty x price in USD micros, truncated toward zero, when the quote asset
    is USD or a stablecoin at par (valuation.CASH_AT_PAR); None otherwise."""
    if quote_asset not in CASH_AT_PAR:
        return None
    return int(Fraction(qty_atoms, 10**qty_decimals) * parse_price(price) * 1_000_000)


def _to_atoms_trunc(units: Fraction, decimals: int) -> int:
    """Whole atoms at `decimals`, truncated toward zero (int() of a Fraction)."""
    return int(units * 10**decimals)


def _side(before: int, after: int, action: str) -> str:
    """long | short by the sign of the position after the fill; before it when
    the fill flattened it; the action when both are flat."""
    ref = after if after != 0 else before
    if ref == 0:
        return "long" if action == "buy" else "short"
    return "long" if ref > 0 else "short"


def _capture(db: Session, account: VenueAccount, instrument: Instrument, limiter) -> _Quote:
    """One quote for `instrument` through prices.capture_quote. A spent budget,
    a 429 or any source error leaves the signal without a quote (the engine
    then prices with the fallback penalty); it never blocks the signal.
    A database error is re-raised: capture_quote writes asset_quotes in this
    session, and a failed flush leaves the transaction unusable, so carrying
    on would only fail later at commit with the real cause hidden."""
    try:
        row: AssetQuote | None = prices.capture_quote(
            db, account.venue, account.venue_env, instrument, limiter
        )
    except (BudgetExhausted, RateLimited) as exc:
        logger.info("no quote for %s/%s: %s", account.venue, instrument.symbol, exc)
        return _NO_QUOTE
    except SQLAlchemyError:
        raise
    except Exception as exc:  # noqa: BLE001  (a quote is best-effort, the signal is not)
        logger.warning(
            "quote capture failed for %s/%s: %s",
            account.venue,
            instrument.symbol,
            type(exc).__name__,
        )
        return _NO_QUOTE
    if row is None:
        return _NO_QUOTE
    return _Quote(row.bid, row.ask, row.last, aware(row.as_of), row.source)


def _price_assets(inputs: valuation_db.ValuationInputs) -> set[str]:
    """Every asset the creator may hold, plus the underlying coin of each perp
    (a perp without closes of its own is valued at the coin's)."""
    assets = inputs.assets | {f.asset for f in inputs.perp_fills}
    return assets | {a.removesuffix(PERP_SUFFIX) for a in assets if a.endswith(PERP_SUFFIX)}


def emit_asset_signals(db: Session, strategy: Strategy, *, limiter=None) -> int:
    account = db.get(VenueAccount, strategy.venue_account_id)
    if account is None:
        return 0
    inputs = valuation_db.account_inputs(db, account)
    if inputs is None:  # never anchored: nothing is part of the verified record yet
        return 0
    pending = _unsignalled_fills(db, account.id, inputs.anchor_at)
    if not pending:
        return 0
    # Fills that already have a signal still move the running position of a
    # later fill at the SAME instant (one order whose fills arrived in two
    # syncs), so every fill from the first pending instant on is walked.
    walk = _fills_from(db, account.id, aware(pending[0][0].filled_at))
    pending_ids = {f.id for f, _ in pending}

    max_lag = timedelta(seconds=get_profile(account.venue).max_copy_lag_seconds)

    def fresh(fill: AssetFill) -> bool:
        return aware(fill.ingested_at) - aware(fill.filled_at) <= max_lag

    quotes: dict[int, _Quote] = {}
    for fill, inst in pending:
        if inst.id not in quotes and fresh(fill):
            quotes[inst.id] = _capture(db, account, inst, limiter)

    last_day = max(aware(f.filled_at) for f, _ in pending).date()
    price_rows = valuation_db.price_inputs(
        db, _price_assets(inputs), None, last_day, valuation_db.pricing_venues(account)
    )

    added = 0
    for t, grouped in groupby(walk, key=lambda row: aware(row[0].filled_at)):
        group = list(grouped)
        if not any(f.id in pending_ids for f, _ in group):
            continue
        equity_micros, basis = _equity_before(inputs, price_rows, t)
        legs_before = [leg for leg in inputs.legs if leg.occurred_at < t]
        fills_before = [f for f in inputs.perp_fills if f.occurred_at < t]
        spot = holdings_at(inputs.anchor, legs_before, t)
        perps = perp_positions_at(inputs.anchor, fills_before, t)
        running: dict[str, tuple[int, int]] = {}  # base asset -> (atoms, decimals)
        for fill, inst in group:
            base = inst.base_asset
            d = fill.qty_decimals
            if base in running:
                atoms, scale = running[base]
                before = _to_atoms_trunc(Fraction(atoms, 10**scale), d)
            elif inst.type == InstrumentType.PERP:
                pos = perps.get(base)
                before = _to_atoms_trunc(pos.qty.units, d) if pos is not None else 0
            else:
                held = spot.get(base)
                before = _to_atoms_trunc(held.units, d) if held is not None else 0
            after = before + (fill.qty_atoms if fill.action == "buy" else -fill.qty_atoms)
            running[base] = (after, d)
            if fill.id not in pending_ids:
                continue
            quote = quotes.get(inst.id, _NO_QUOTE) if fresh(fill) else _NO_QUOTE
            db.add(
                Signal(
                    strategy_id=strategy.id,
                    asset_fill_id=fill.id,
                    instrument_id=inst.id,
                    ticker=inst.symbol,
                    side=_side(before, after, fill.action),
                    action=fill.action,
                    qty_atoms=fill.qty_atoms,
                    qty_decimals=d,
                    price=fill.price,
                    notional_micros=notional_usd_micros(
                        fill.qty_atoms, d, fill.price, inst.quote_asset
                    ),
                    position_before_atoms=before,
                    position_after_atoms=after,
                    creator_equity_micros=equity_micros,
                    equity_basis_day=basis,
                    filled_at=t,
                    observed_at=aware(fill.ingested_at),
                    quote_bid=quote.bid,
                    quote_ask=quote.ask,
                    quote_last=quote.last,
                    quote_at=quote.at,
                    quote_source=quote.source,
                )
            )
            added += 1
    db.commit()
    if added:
        logger.info("emitted %d asset signals for strategy %s", added, strategy.id)
    return added


def _unsignalled_fills(db: Session, account_id: int, anchor_at: datetime):
    """(AssetFill, Instrument) pairs strictly after the anchor with no signal,
    in (filled_at, id) order."""
    rows = db.execute(
        select(AssetFill, Instrument)
        .join(Instrument, Instrument.id == AssetFill.instrument_id)
        .outerjoin(Signal, Signal.asset_fill_id == AssetFill.id)
        .where(
            AssetFill.venue_account_id == account_id,
            AssetFill.filled_at > anchor_at,
            Signal.id.is_(None),
        )
        .order_by(AssetFill.filled_at, AssetFill.id)
    ).all()
    return [(f, inst) for f, inst in rows if aware(f.filled_at) > anchor_at]


def _fills_from(db: Session, account_id: int, start: datetime):
    """Every (AssetFill, Instrument) of the account at or after `start`, in
    (filled_at, id) order, signalled or not."""
    return db.execute(
        select(AssetFill, Instrument)
        .join(Instrument, Instrument.id == AssetFill.instrument_id)
        .where(AssetFill.venue_account_id == account_id, AssetFill.filled_at >= start)
        .order_by(AssetFill.filled_at, AssetFill.id)
    ).all()


def _equity_before(
    inputs: valuation_db.ValuationInputs, price_rows, t: datetime
) -> tuple[int, date | None]:
    """(creator_equity_micros, equity_basis_day) just before `t`; (0, None)
    when the valuation is not positive (unknown: followers skip it)."""
    v = equity_before(inputs.anchor, inputs.legs, price_rows, inputs.perp_fills, t)
    if v.equity_micros <= 0:
        return 0, None
    return v.equity_micros, basis_day(v)

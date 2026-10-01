"""Signals v2 for ASSET strategies (wave2, workstream w2-p6-signals).

STUB: the scaffold dispatches here from signals.emit_signals for asset
venues; the workstream implements it. Contract:

emit_asset_signals(db, strategy, *, limiter=None) -> int
    One Signal per post-anchor AssetFill of the strategy's account that has
    none (unique asset_fill_id), in (filled_at, id) order. Fills at or before
    the anchor never become signals; transfers and other events never do.
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
"""

import logging

from sqlalchemy.orm import Session

from alphamarket.models import Strategy

logger = logging.getLogger(__name__)


def emit_asset_signals(db: Session, strategy: Strategy, *, limiter=None) -> int:
    return 0

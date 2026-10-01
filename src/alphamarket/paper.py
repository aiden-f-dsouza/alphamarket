"""Paper copy v2 engine for ASSET strategies (wave2, workstream w2-p6-engine).

STUB: mirror.process_allocation and portfolio.value_allocation /
allocation_view dispatch here for asset venues. The book itself lives in
paper_book.py (implemented); lifecycle events in paper_lifecycle.py.

Contract:

process_asset_allocation(db, allocation) -> tuple[int, int]
    Mirror signals with id > allocation.last_signal_id (ACTIVE allocations
    only), then ALWAYS call paper_lifecycle.apply_lifecycle(db, allocation,
    now) (funding, liquidation, splits, dividends; stopped allocations too,
    so dividends and funding keep accruing on what is still held). Returns
    (signals mirrored, lifecycle events applied). Per signal: skip with a
    mirror_skips row (filled_at < started_at is skipped silently, as in M6;
    stale_signal, creator_equity_unknown, non_usd_quote, below_minimum_size,
    insufficient_cash, no_position, follower_equity_zero), else size (plan
    D8), price (D2/D4: ask for buys, bid for sells from the signal's quote,
    else creator price +/- profile.paper_fallback_penalty_bps), fee (D10 by
    profile.paper_fee_model), and write one mirror_asset_fills row plus its
    paper_book group (ref "sig:<signal_id>") in one commit. Advance
    last_signal_id past every signal considered.

value_asset_allocation(db, allocation, marks=None) -> portfolio.AllocationValue
    cash_micros = paper_book cash; open_value = holdings at the freshest
    mark (prices.latest_mark mid, else the last prices_daily close, else
    cost), perps at qty x (mark - entry); asset_positions = one dict per
    position in the shape asset_view_extras documents.

asset_view_extras(db, allocation, value) -> dict
    Merged into portfolio.allocation_view for asset allocations. Keys:
    "positions" (replaces the binary list; each {kind spot|equity|perp,
    symbol, side long|short, qty str, cost float, mark str|None,
    mark_source quote|close|cost, mark_as_of iso|None, value float,
    unrealized float, entry str|None}), "venue", "asset_class",
    "copy_lag_median_seconds" (int|None), "skipped" ({reason: count}).
"""

from datetime import datetime

from sqlalchemy.orm import Session

from alphamarket import paper_book
from alphamarket.models import Allocation, utcnow


def process_asset_allocation(
    db: Session, allocation: Allocation, now: datetime | None = None
) -> tuple[int, int]:
    from alphamarket.paper_lifecycle import apply_lifecycle

    mirrored = 0
    applied = apply_lifecycle(db, allocation, now or utcnow())
    return mirrored, applied


def value_asset_allocation(db: Session, allocation: Allocation, marks=None):
    from alphamarket.portfolio import AllocationValue

    book = paper_book.load_book(db, allocation)
    return AllocationValue(
        cash_micros=book.cash_micros,
        open_cost_micros=0,
        open_value_micros=0,
        unmarked_cost_micros=0,
        positions=[],
        n_fills=0,
        n_settlements=0,
    )


def asset_view_extras(db: Session, allocation: Allocation, value) -> dict:
    return {
        "positions": list(value.asset_positions),
        "venue": None,
        "asset_class": None,
        "copy_lag_median_seconds": None,
        "skipped": {},
    }

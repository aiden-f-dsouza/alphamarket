"""Lifecycle events for paper ASSET allocations (wave2, w2-p6-lifecycle).

STUB. Contract:

apply_lifecycle(db, allocation, now) -> int
    Apply, idempotently (paper_book refs), every event due up to `now`:
    - perp funding from funding_rates for open perps (ref
      "fund:<instrument_id>:<epoch>"): payment = -q x mark x rate, mark from
      the rate row, else the quote, else the close; credits floor, debits
      ceil (paper_book rounding rule);
    - liquidation (Hyperliquid cross margin, plan D11) when a fresh mark
      (settings.quote_max_age_seconds) shows account value < maintenance
      margin: close every perp at the mark (mirror_asset_fills reason
      "liquidation" + FILL_BASE and LIQUIDATION legs, ref
      "liq:<instrument_id>:<epoch>"), then portfolio.stop_allocation(...,
      "liquidated");
    - corporate_actions for held equities (ref "ca:<id>"): split ->
      SPLIT leg floor(q x new/old) - q (fraction dropped); cash dividend ->
      holding at end of ex-date minus one day x cash_per_share, credited on
      pay_date (floor); symbol_change -> skipped in v2 (flagged).
    Returns the number of events applied.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from alphamarket.models import Allocation


def apply_lifecycle(db: Session, allocation: Allocation, now: datetime) -> int:
    return 0

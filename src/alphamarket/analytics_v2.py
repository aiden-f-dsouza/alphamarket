"""Statistics for ASSET strategies from ledger v2 (wave2, w2-stats-v2).

STUB: analytics.compute_strategy_stats dispatches here when the strategy's
venue profile is InstrumentKind.ASSET; until implemented it computes the
binary path (today's behaviour). The binary path itself is never changed.

Contract: compute_asset_stats(db, strategy, as_of=None) -> StrategyStats
    Daily series from the anchor day to the last complete UTC day via
    valuation.daily_equity_series over valuation_db inputs; total_return =
    flow-adjusted TWR; max_drawdown, sharpe, sortino on the TWR index (so a
    deposit is never a gain); win_rate None; risk_score via
    stats.risk_score with the exposure top share; realized_pnl_micros =
    sum of REALIZED_PNL + LIQUIDATION legs. extras (marked "valuation": "v2"):

        daily_equity [[iso, micros]]   raw E_d (the chart reads it)
        daily_flows [[iso, micros]]
        twr_index [[iso, float]]       growth of 1.0; drawdown + monthly read it
        twr, modified_dietz, net_flows_micros, pnl_micros
        holdings [{asset, kind cash|spot|equity|perp, qty str, close_micros,
                   price_day iso, value_micros, notional_micros, entry str|None}]
        exposure {day, equity_micros, gross_micros, by_underlying_micros,
                  top_asset, top_share, perp_gross_leverage}
        asset_classes [..], price_sources [..]
        valuation_flags {unpriced [], carried_forward_days int,
                         perp_entry_unknown [], perp_qty_mismatch []}
        broker_equity {as_of, venue_micros, computed_micros,
                       divergence_micros, tolerance_micros, ok} | None
        marked_equity_micros, n_open_positions
"""

from datetime import datetime

from sqlalchemy.orm import Session

from alphamarket.models import Strategy, StrategyStats


def compute_asset_stats(
    db: Session, strategy: Strategy, as_of: datetime | None = None
) -> StrategyStats:
    from alphamarket.analytics import compute_binary_stats

    return compute_binary_stats(db, strategy, as_of)

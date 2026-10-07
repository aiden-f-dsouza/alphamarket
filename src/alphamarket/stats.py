"""Stats and risk engine (M3). Pure functions over the ledger.

Every formula here is documented in docs/methodology.md and pinned by a
hand-computed test in tests/test_stats.py. Money stays integer micros until
the final ratio; ratios are floats because they are dimensionless.

Equity model
------------
    cash(t)      = anchor + post-anchor fill/settlement flows   (ledger.py)
    open_cost(t) = sum of cost basis of unsettled contracts
    equity(t)    = cash(t) + open_cost(t)

Open positions are carried AT COST, not marked to market (MVP limitation,
stated on the methodology page). P&L is therefore realized at settlement or
on sells, never on price moves of an open position.

Positions open at connect time are seeded from the venue's own positions
report (captured with the anchor) so they carry their real cost basis. Fills
before the anchor are otherwise ignored: the anchor balance already reflects
their cash, and tracking them would let a position whose settlement fell
outside the venue's returned history sit on the books forever, inflating
equity.
"""

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from alphamarket.connectors.base import Action, Side
from alphamarket.ledger import FillLike, SettlementLike, aware, fill_cash_flow, fill_notional
from alphamarket.money import MICROS_PER_UNIT, notional_micros

ANNUALIZATION_DAYS = 365  # prediction markets settle every day of the year


@dataclass
class _Position:
    count_micros: int = 0
    cost_micros: int = 0


@dataclass(frozen=True)
class EquityPoint:
    at: datetime
    cash_micros: int
    open_cost_micros: int
    equity_micros: int


@dataclass(frozen=True)
class RealizedTrade:
    ticker: str
    settled_at: datetime
    cost_micros: int
    revenue_micros: int

    @property
    def pnl_micros(self) -> int:
        return self.revenue_micros - self.cost_micros


@dataclass(frozen=True)
class OpenPosition:
    ticker: str
    side: str  # "yes" | "no"
    count_micros: int
    cost_micros: int


@dataclass
class Replay:
    points: list[EquityPoint]
    realized: list[RealizedTrade]
    buy_notionals: list[tuple[datetime, str, int]]  # (at, ticker, notional) per buy fill
    equity_at_anchor_micros: int
    open_positions: list[OpenPosition] = field(default_factory=list)  # at the end of the replay
    # Equity just before each fill and the (ticker, side) count just after it,
    # keyed by the fill's venue id: what signals.py needs for proportional sizing.
    fill_context: dict[str, tuple[int, int]] = field(default_factory=dict)


def _ticker_key(ticker: str, side: Side) -> tuple[str, str]:
    return (ticker, side.value)


def replay(
    anchor_balance_micros: int,
    anchor_at: datetime,
    fills: list[FillLike],
    settlements: list[SettlementLike],
    anchor_positions: list[dict] | None = None,
) -> Replay:
    """Walk post-anchor fills and settlements in time order, tracking cash
    (from the anchor) and open positions at cost (seeded from the anchor
    positions snapshot)."""
    anchor_at = aware(anchor_at)
    events: list[tuple[datetime, int, object]] = []
    for f in fills:
        if aware(f.filled_at) > anchor_at:
            events.append((aware(f.filled_at), 0, f))
    for s in settlements:
        if aware(s.settled_at) > anchor_at:
            events.append((aware(s.settled_at), 1, s))
    events.sort(key=lambda e: (e[0], e[1]))

    positions: dict[tuple[str, str], _Position] = defaultdict(_Position)
    open_cost = 0
    for ap in anchor_positions or []:
        side = Side.YES if ap["position_micros"] > 0 else Side.NO
        pos = positions[_ticker_key(ap["ticker"], side)]
        pos.count_micros += abs(ap["position_micros"])
        pos.cost_micros += ap["cost_micros"]
        open_cost += ap["cost_micros"]

    cash = anchor_balance_micros
    equity_at_anchor = cash + open_cost
    points: list[EquityPoint] = []
    realized: list[RealizedTrade] = []
    buys: list[tuple[datetime, str, int]] = []

    def snapshot(at: datetime) -> None:
        points.append(EquityPoint(at, cash, open_cost, cash + open_cost))

    fill_context: dict[str, tuple[int, int]] = {}
    for at, kind, obj in events:
        if kind == 0:
            f = obj
            pos = positions[_ticker_key(f.ticker, Side(f.side))]
            notional = fill_notional(f)
            equity_before = cash + open_cost
            if Action(f.action) is Action.BUY:
                pos.count_micros += f.count_micros
                pos.cost_micros += notional
                open_cost += notional
                buys.append((at, f.ticker, notional))
            else:
                # Sell reduces the position at average cost.
                if pos.count_micros > 0:
                    sold = min(f.count_micros, pos.count_micros)
                    released = pos.cost_micros * sold // pos.count_micros
                    pos.count_micros -= sold
                    pos.cost_micros -= released
                    open_cost -= released
                    realized.append(RealizedTrade(f.ticker, at, released, notional - f.fee_micros))
            cash += fill_cash_flow(f)
            venue_id = getattr(f, "venue_fill_id", None)
            if venue_id is not None:
                fill_context[venue_id] = (equity_before, pos.count_micros)
        else:
            s = obj
            cost = 0
            for side in (Side.YES, Side.NO):
                pos = positions.pop(_ticker_key(s.ticker, side), None)
                if pos:
                    cost += pos.cost_micros
            open_cost -= cost
            realized.append(RealizedTrade(s.ticker, at, cost, s.revenue_micros))
            cash += s.revenue_micros
        snapshot(at)

    open_positions = [
        OpenPosition(ticker, side, pos.count_micros, pos.cost_micros)
        for (ticker, side), pos in positions.items()
        if pos.count_micros > 0
    ]
    return Replay(points, realized, buys, equity_at_anchor, open_positions, fill_context)


# ---- mark-to-market (M6) -----------------------------------------------------


@dataclass(frozen=True)
class Marked:
    """Open positions valued at public market marks. Positions without a
    usable mark are carried at cost and their cost is reported separately so
    the reader can see how much of the book is unmarked."""

    open_cost_micros: int
    open_value_micros: int
    unmarked_cost_micros: int

    @property
    def unrealized_pnl_micros(self) -> int:
        return self.open_value_micros - self.open_cost_micros


def position_value_micros(pos: OpenPosition, yes_mark_micros: int) -> int:
    """A YES contract is worth the YES mark; a NO contract is worth $1 minus it."""
    per_contract = yes_mark_micros if pos.side == Side.YES else MICROS_PER_UNIT - yes_mark_micros
    return notional_micros(per_contract, pos.count_micros)


def mark_positions(positions: list[OpenPosition], marks: dict[str, int | None]) -> Marked:
    """`marks` maps ticker -> YES mark in micros (None or missing = no mark)."""
    cost = value = unmarked = 0
    for pos in positions:
        cost += pos.cost_micros
        mark = marks.get(pos.ticker)
        if mark is None:
            value += pos.cost_micros
            unmarked += pos.cost_micros
        else:
            value += position_value_micros(pos, mark)
    return Marked(cost, value, unmarked)


def daily_equity(rep: Replay, anchor_at: datetime, as_of: datetime) -> list[tuple[date, int]]:
    """End-of-day (UTC) equity for every day from the anchor day to as_of."""
    anchor_at = aware(anchor_at)
    as_of = aware(as_of)
    day = anchor_at.date()
    last_day = as_of.date()
    post = [p for p in rep.points if p.at > anchor_at]
    series: list[tuple[date, int]] = []
    equity = rep.equity_at_anchor_micros
    i = 0
    while day <= last_day:
        end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
        while i < len(post) and post[i].at < end:
            equity = post[i].equity_micros
            i += 1
        series.append((day, equity))
        day += timedelta(days=1)
    return series


def daily_returns(series: list[tuple[date, int]]) -> list[float]:
    out = []
    for (_, prev), (_, cur) in zip(series, series[1:], strict=False):
        if prev > 0:
            out.append(cur / prev - 1.0)
    return out


def total_return(series: list[tuple[date, int]]) -> float | None:
    if not series or series[0][1] <= 0:
        return None
    return series[-1][1] / series[0][1] - 1.0


def peak_invested(rep: Replay, anchor_balance_micros: int) -> int:
    """Most cash the ledger ever needed beyond what it can account for:
    max over time of (-cash), floored at zero. An account with an observed
    balance never goes negative, so this is 0 there; a fills-only venue
    anchored at 0 sees cash go negative on the first buy, and the deepest
    point is the peak capital the trader had at risk, the denominator of
    return_on_peak_invested. `anchor_balance_micros` is the cash before any
    post-anchor event."""
    worst = max(0, -anchor_balance_micros)
    for point in rep.points:
        worst = max(worst, -point.cash_micros)
    return worst


def return_on_peak_invested(rep: Replay, series: list[tuple[date, int]], peak: int) -> float | None:
    """P&L since the anchor over the peak capital invested. Only defined when
    total_return is not (anchor equity 0) and some capital was invested."""
    if peak <= 0 or not series:
        return None
    return (series[-1][1] - rep.equity_at_anchor_micros) / peak


def max_drawdown(series: list[tuple[date, int]]) -> float:
    """Largest peak-to-trough decline as a fraction of the peak."""
    peak = 0
    worst = 0.0
    for _, equity in series:
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def sharpe(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    sd = statistics.stdev(returns)
    if sd == 0:
        return None
    return statistics.mean(returns) / sd * math.sqrt(ANNUALIZATION_DAYS)


def sortino(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in returns) / len(returns))
    if downside == 0:
        return None
    return statistics.mean(returns) / downside * math.sqrt(ANNUALIZATION_DAYS)


def win_rate(realized: list[RealizedTrade]) -> float | None:
    if not realized:
        return None
    return sum(1 for t in realized if t.pnl_micros > 0) / len(realized)


def series_of(ticker: str) -> str:
    """Kalshi tickers are SERIES-EVENT-STRIKE; the series is the risk unit."""
    return ticker.split("-", 1)[0]


def concentration(buys: list[tuple[datetime, str, int]]) -> tuple[float, float]:
    """(top ticker share, top series share) of total buy notional."""
    if not buys:
        return 0.0, 0.0
    total = sum(n for _, _, n in buys)
    by_ticker: dict[str, int] = defaultdict(int)
    by_series: dict[str, int] = defaultdict(int)
    for _, ticker, n in buys:
        by_ticker[ticker] += n
        by_series[series_of(ticker)] += n
    return max(by_ticker.values()) / total, max(by_series.values()) / total


def max_stake_fraction(rep: Replay) -> float:
    """Largest single buy as a fraction of equity just before it."""
    worst = 0.0
    equity = rep.equity_at_anchor_micros
    j = 0
    for at, _, notional in rep.buy_notionals:
        while j < len(rep.points) and rep.points[j].at < at:
            equity = rep.points[j].equity_micros
            j += 1
        if equity > 0:
            worst = max(worst, notional / equity)
    return worst


# ---- fraud / risk heuristics v0 (flag, never auto-delist) ------------------

MARTINGALE_RATIO = 1.8
MARTINGALE_STREAK = 3
STAKE_SPIKE_MULTIPLE = 5.0
STAKE_SPIKE_MIN_FILLS = 10
CONCENTRATION_LIMIT = 0.5
CONCENTRATION_MIN_FILLS = 5


def martingale_flag(rep: Replay) -> bool:
    """Three consecutive losses each followed by a next buy at least 1.8x the
    lost stake: the classic double-down signature."""
    losses = sorted((t for t in rep.realized if t.pnl_micros < 0), key=lambda t: t.settled_at)
    streak = 0
    for loss in losses:
        nxt = next((n for at, _, n in rep.buy_notionals if at > loss.settled_at), None)
        if nxt is not None and loss.cost_micros > 0 and nxt >= MARTINGALE_RATIO * loss.cost_micros:
            streak += 1
            if streak >= MARTINGALE_STREAK:
                return True
        else:
            streak = 0
    return False


def stake_spike_flag(rep: Replay) -> bool:
    notionals = [n for _, _, n in rep.buy_notionals]
    if len(notionals) < STAKE_SPIKE_MIN_FILLS:
        return False
    return max(notionals) > STAKE_SPIKE_MULTIPLE * statistics.median(notionals)


def concentration_flag(rep: Replay) -> bool:
    if len(rep.buy_notionals) < CONCENTRATION_MIN_FILLS:
        return False
    _, top_series = concentration(rep.buy_notionals)
    return top_series > CONCENTRATION_LIMIT


# ---- risk score --------------------------------------------------------------


def risk_score(
    max_dd: float, downside_dev_annual: float | None, top_series_share: float, stake_frac: float
) -> int:
    """1 (calm) to 10 (wild), from realized behaviour only (AI-1):
    1 + 3*min(max_dd/0.30, 1) + 3*min(downside_dev/0.50, 1)
      + 1.5*top_series_share + 1.5*min(stake_frac/0.25, 1), rounded."""
    dd_pts = 3.0 * min(max_dd / 0.30, 1.0)
    vol_pts = 3.0 * min((downside_dev_annual or 0.0) / 0.50, 1.0)
    conc_pts = 1.5 * top_series_share
    stake_pts = 1.5 * min(stake_frac / 0.25, 1.0)
    return max(1, min(10, round(1 + dd_pts + vol_pts + conc_pts + stake_pts)))


def downside_deviation_annual(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    return math.sqrt(sum(min(r, 0.0) ** 2 for r in returns) / len(returns)) * math.sqrt(
        ANNUALIZATION_DAYS
    )


# ---- everything together -----------------------------------------------------


@dataclass
class StrategyStatsResult:
    as_of: datetime
    track_days: int
    n_fills: int
    n_settlements: int
    total_return: float | None
    max_drawdown: float
    sharpe: float | None
    sortino: float | None
    win_rate: float | None
    risk_score: int
    equity_micros: int
    realized_pnl_micros: int
    top_ticker_share: float
    top_series_share: float
    max_stake_fraction: float
    flags: list[str] = field(default_factory=list)
    daily_equity: list[tuple[date, int]] = field(default_factory=list)
    cash_micros: int = 0
    open_positions: list[OpenPosition] = field(default_factory=list)
    peak_invested_micros: int = 0
    return_on_peak_invested: float | None = None


def compute_stats(
    anchor_balance_micros: int,
    anchor_at: datetime,
    fills: list[FillLike],
    settlements: list[SettlementLike],
    as_of: datetime | None = None,
    anchor_positions: list[dict] | None = None,
) -> StrategyStatsResult:
    as_of = aware(as_of or datetime.now(UTC))
    rep = replay(anchor_balance_micros, anchor_at, fills, settlements, anchor_positions)
    series = daily_equity(rep, anchor_at, as_of)
    peak = peak_invested(rep, anchor_balance_micros)
    on_peak = return_on_peak_invested(rep, series, peak)  # on the raw P&L curve
    if anchor_balance_micros == 0 and peak > 0:
        # Fills-only venue (no observable balance, anchored at zero): the raw
        # curve is P&L, which has no meaningful ratio. Shift it by the peak
        # capital the trader had at risk so every return-based statistic
        # reads as growth of that capital; total_return then equals
        # return_on_peak_invested. See methodology "Verification tiers".
        series = [(day, equity + peak) for day, equity in series]
    rets = daily_returns(series)
    top_ticker, top_series = concentration(rep.buy_notionals)
    stake = max_stake_fraction(rep)
    dd = max_drawdown(series)
    flags = []
    if martingale_flag(rep):
        flags.append("martingale_sizing")
    if stake_spike_flag(rep):
        flags.append("stake_spike")
    if concentration_flag(rep):
        flags.append("single_series_concentration")
    post_anchor_realized = rep.realized  # replay only walks post-anchor events
    return StrategyStatsResult(
        as_of=as_of,
        track_days=(as_of.date() - aware(anchor_at).date()).days,
        n_fills=len(fills),
        n_settlements=len(settlements),
        total_return=total_return(series),
        max_drawdown=dd,
        sharpe=sharpe(rets),
        sortino=sortino(rets),
        win_rate=win_rate(post_anchor_realized),
        risk_score=risk_score(dd, downside_deviation_annual(rets), top_series, stake),
        equity_micros=series[-1][1] if series else rep.equity_at_anchor_micros,
        realized_pnl_micros=sum(t.pnl_micros for t in post_anchor_realized),
        top_ticker_share=top_ticker,
        top_series_share=top_series,
        max_stake_fraction=stake,
        flags=flags,
        daily_equity=series,
        cash_micros=rep.points[-1].cash_micros if rep.points else anchor_balance_micros,
        open_positions=rep.open_positions,
        peak_invested_micros=peak,
        return_on_peak_invested=on_peak,
    )


def drawdown_series(series: list[tuple[date, int]]) -> list[tuple[date, float]]:
    """Per-day drawdown from the running peak, as a fraction (0 = at peak)."""
    out = []
    peak = 0
    for day, equity in series:
        peak = max(peak, equity)
        out.append((day, (peak - equity) / peak if peak > 0 else 0.0))
    return out


def monthly_returns(series: list[tuple[date, int]]) -> list[tuple[int, int, float]]:
    """(year, month, return) using month-end equity over prior month-end
    equity; the first month is measured from the series' first point."""
    if not series:
        return []
    month_end: dict[tuple[int, int], int] = {}
    for day, equity in series:
        month_end[(day.year, day.month)] = equity  # last write wins = month end
    out = []
    prev = series[0][1]
    for key in sorted(month_end):
        cur = month_end[key]
        out.append((key[0], key[1], cur / prev - 1.0 if prev > 0 else 0.0))
        prev = cur
    return out


def micros_to_float(micros: int) -> float:
    """Display only. Never feed the result back into storage."""
    return micros / MICROS_PER_UNIT


def worst_periods(
    drawdowns: list[tuple[date, float]], n: int = 2
) -> list[tuple[date, date, float, date | None]]:
    """The n deepest drawdown episodes of a per-day drawdown series (as
    drawdown_series returns: 0 at a peak, positive below it). An episode is a
    run of days below the running peak: it starts at the last day at the peak
    before the run, bottoms at its deepest day, and recovers on the first day
    back at a peak (None while still below). Returns (peak_day, trough_day,
    depth, recovered_day), deepest first; equal depths keep the earlier one."""
    episodes = []
    peak_day = None
    trough_day, depth = None, 0.0
    for day, dd in drawdowns:
        if dd <= 0:
            if trough_day is not None:
                episodes.append((peak_day, trough_day, depth, day))
                trough_day, depth = None, 0.0
            peak_day = day
        elif dd > depth:
            trough_day, depth = day, dd
    if trough_day is not None:
        episodes.append((peak_day, trough_day, depth, None))
    episodes = [e for e in episodes if e[0] is not None]
    return sorted(episodes, key=lambda e: -e[2])[:n]

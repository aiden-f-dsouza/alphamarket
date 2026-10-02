"""Statistics for ASSET strategies from ledger v2 (wave2, w2-stats-v2).

analytics.compute_strategy_stats dispatches here when the strategy's venue
profile is InstrumentKind.ASSET. The binary path (analytics.compute_binary_stats)
is never called from here and never changed.

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

    Extensions (additive, same extras dict): max_perp_gross_leverage (float |
    None, the series maximum the high_leverage flag reads).

Flags: unpriced_assets, carried_forward_prices, single_asset_concentration
(top share > 50% of gross exposure), high_leverage (perp gross leverage > 5x
on any day), broker_equity_divergence (newest broker equity check failed;
reconcile never quarantines for it). Formulas: docs/methodology.md "Asset
strategy statistics"; pinned by tests/test_analytics_v2.py.

marked_valuation() is shared with reconcile (the broker equity check): the
account's holdings NOW, valued at prices.latest_mark when one is fresh, else
the last daily close.
"""

import logging
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alphamarket import prices as prices_mod
from alphamarket import stats, valuation, valuation_db
from alphamarket.connectors.base import InstrumentType, LegKind
from alphamarket.connectors.registry import get_profile
from alphamarket.ledger import aware
from alphamarket.models import (
    AssetFill,
    EquitySnapshot,
    Instrument,
    PriceDaily,
    Strategy,
    StrategyStats,
    VenueAccount,
)
from alphamarket.money import MICROS_PER_UNIT, format_atoms
from alphamarket.valuation import (
    CASH_AT_PAR,
    PERP_SUFFIX,
    DayValuation,
    FlagReason,
    PositionKind,
    PriceIn,
)

logger = logging.getLogger(__name__)

CONCENTRATION_LIMIT = Fraction(1, 2)  # single_asset_concentration: top share > 50%
LEVERAGE_LIMIT = 5  # high_leverage: perp gross notional > 5x equity
EQUITY_CARRY_ALLOWANCE_DAYS = 4  # weekends and market holidays are not stale prices
REALIZED_KINDS = frozenset({LegKind.REALIZED_PNL, LegKind.LIQUIDATION})


# ---- pure helpers (pinned by tests/test_analytics_v2.py) ---------------------------


def twr_index(series: Sequence[valuation.EquityPointLike]) -> list[tuple[date, Fraction]]:
    """Growth of 1.0 along the flow-adjusted daily returns:

        I_first = 1,   I_d = I_(d-1) x (1 + r_d)      (I_d = I_(d-1) when r_d is undefined)

    so I_last - 1 is exactly valuation.time_weighted_return(series)."""
    out: list[tuple[date, Fraction]] = []
    level = Fraction(1)
    for n, daily in enumerate(valuation.daily_returns(series)):
        if n and daily.r is not None:
            level *= 1 + daily.r
        out.append((daily.day, level))
    return out


def index_drawdown(index: Sequence[tuple[date, Fraction]]) -> Fraction:
    """max over d of (peak_d - I_d) / peak_d on the TWR index (0 for a flat or rising one)."""
    peak = Fraction(0)
    worst = Fraction(0)
    for _, level in index:
        peak = max(peak, level)
        if peak > 0:
            worst = max(worst, (peak - level) / peak)
    return worst


def index_returns(series: Sequence[valuation.EquityPointLike]) -> list[float]:
    """The defined daily returns r_d after the first point (= I_d / I_(d-1) - 1),
    as floats for the dimensionless ratios (Sharpe, Sortino, downside deviation).
    Days with no capital at risk are skipped, as in the binary path."""
    return [float(d.r) for d in valuation.daily_returns(series)[1:] if d.r is not None]


def equity_tolerance_micros(venue_equity_micros: int) -> int:
    """Broker equity check tolerance (D16): max($1, 0.5% of the broker's equity)."""
    return max(abs(venue_equity_micros) * 50 // 10_000, MICROS_PER_UNIT)


def fraction_str(value: Fraction, places: int = 12) -> str:
    """Display form of an exact entry price: at most `places` decimals, truncated,
    trailing zeros dropped. Never stored as money."""
    with localcontext() as ctx:
        ctx.prec = 60
        scaled = int(value * 10**places)  # toward zero
        text = f"{Decimal(scaled).scaleb(-places):.{places}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _float(value: Fraction | None) -> float | None:
    return None if value is None else float(value)


# ---- DB: inputs, prices, marks -------------------------------------------------------


def _price_assets(assets: set[str]) -> set[str]:
    """Assets to look up closes for: everything held except cash at par, plus
    each perp's underlying coin (a perp falls back to the coin's close)."""
    wanted = {a for a in assets if a not in CASH_AT_PAR}
    wanted |= {a.removesuffix(PERP_SUFFIX) for a in wanted if a.endswith(PERP_SUFFIX)}
    return wanted


def _pricing_instruments(db: Session, account: VenueAccount, assets: set[str]) -> dict[str, int]:
    """asset -> the instrument whose quotes mark it: the first pricing venue's
    instrument with that base asset quoted in USD or a stablecoin at par."""
    if not assets:
        return {}
    order = valuation_db.pricing_venues(account)
    rows = db.execute(
        select(Instrument.id, Instrument.base_asset, Instrument.venue)
        .where(
            Instrument.base_asset.in_(assets),
            Instrument.venue.in_(order),
            Instrument.quote_asset.in_(sorted(CASH_AT_PAR)),
        )
        .order_by(Instrument.id)
    ).all()
    rank = {venue: n for n, venue in enumerate(order)}
    best: dict[str, tuple[int, int]] = {}
    for inst_id, asset, venue in rows:
        if asset not in best or rank[venue] < best[asset][0]:
            best[asset] = (rank[venue], inst_id)
    return {asset: inst_id for asset, (_, inst_id) in best.items()}


def _mark_prices(
    db: Session, account: VenueAccount, assets: set[str], now: datetime
) -> list[PriceIn]:
    """A PriceIn on now's UTC day for every asset with a fresh mark
    (prices.latest_mark; None while no quote is fresh)."""
    out: list[PriceIn] = []
    for asset, inst_id in sorted(_pricing_instruments(db, account, assets).items()):
        mark = prices_mod.latest_mark(db, inst_id, now)
        if mark is None:
            continue
        exact = valuation.parse_price(mark.price)
        if exact < 0:
            continue
        out.append(PriceIn(asset, now.date(), int(exact * MICROS_PER_UNIT), close=mark.price))
    return out


def capture_marks(db: Session, account: VenueAccount, limiter) -> int:
    """Capture one quote now for every non-cash asset the account holds
    (prices.capture_quote, throttled by `limiter`), so marked_valuation finds
    fresh marks. Returns quotes stored; a missing quote just leaves that asset
    at its last close."""
    inputs = valuation_db.account_inputs(db, account)
    if inputs is None:
        return 0
    assets = _price_assets(inputs.assets | {f.asset for f in inputs.perp_fills})
    stored = 0
    for _, inst_id in sorted(_pricing_instruments(db, account, assets).items()):
        instrument = db.get(Instrument, inst_id)
        if instrument is None:
            continue
        quote = prices_mod.capture_quote(db, account.venue, account.venue_env, instrument, limiter)
        stored += quote is not None
    return stored


def marked_valuation(db: Session, account: VenueAccount, now: datetime) -> DayValuation | None:
    """The account's holdings at `now` (anchor + every leg and perp fill at or
    before it), valued at the freshest mark per asset (prices.latest_mark),
    else the last close of a COMPLETE UTC day before now's (carried forward):
    now's own day has no close yet, so a stored one is never looked ahead to.
    None when the account was never anchored."""
    now = aware(now).astimezone(UTC)
    inputs = valuation_db.account_inputs(db, account)
    if inputs is None:
        return None
    legs = [leg for leg in inputs.legs if leg.occurred_at <= now]
    fills = [f for f in inputs.perp_fills if f.occurred_at <= now]
    assets = _price_assets(inputs.assets | {f.asset for f in fills})
    today = now.date()
    closes = valuation_db.price_inputs(
        db, assets, None, today - timedelta(days=1), valuation_db.pricing_venues(account)
    )
    marks = _mark_prices(db, account, assets, now)  # a perp may mark via its coin
    return valuation.valuation_on(inputs.anchor, legs, [*closes, *marks], fills, today)


def _price_sources(db: Session, account: VenueAccount, assets: set[str], end: date) -> list[str]:
    """Venues (in preference order) that have a USD close for a held asset."""
    order = [*dict.fromkeys([*valuation_db.pricing_venues(account), "coingecko"])]
    if not assets:
        return []
    found = set(
        db.scalars(
            select(Instrument.venue)
            .join(PriceDaily, PriceDaily.instrument_id == Instrument.id)
            .where(
                Instrument.base_asset.in_(assets),
                Instrument.venue.in_(order),
                PriceDaily.usd_micros_per_unit.is_not(None),
                PriceDaily.day <= end,
            )
            .distinct()
        ).all()
    )
    return [v for v in order if v in found]


def _asset_kinds(db: Session, account: VenueAccount, assets: set[str]) -> dict[str, str]:
    """spot | equity per non-perp asset, from the instruments it trades or is
    priced as; the profile's asset classes decide when no instrument is known."""
    venues = [account.venue, *valuation_db.pricing_venues(account)]
    types: dict[str, set[str]] = {}
    if assets:
        for asset, type_ in db.execute(
            select(Instrument.base_asset, Instrument.type).where(
                Instrument.base_asset.in_(assets), Instrument.venue.in_(venues)
            )
        ).all():
            types.setdefault(asset, set()).add(type_)
    classes = get_profile(account.venue).asset_classes
    default = "equity" if "equities" in classes and "crypto" not in classes else "spot"
    out = {}
    for asset in assets:
        seen = types.get(asset, set())
        if InstrumentType.EQUITY in seen:
            out[asset] = "equity"
        elif InstrumentType.SPOT in seen:
            out[asset] = "spot"
        else:
            out[asset] = default
    return out


def _latest_broker_check(db: Session, strategy: Strategy, as_of: datetime) -> dict | None:
    snap = db.scalar(
        select(EquitySnapshot)
        .where(
            EquitySnapshot.strategy_id == strategy.id,
            EquitySnapshot.equity_reconciled.is_not(None),
            EquitySnapshot.as_of <= as_of,
        )
        .order_by(EquitySnapshot.as_of.desc(), EquitySnapshot.id.desc())
        .limit(1)
    )
    if snap is None:
        return None
    return {
        "as_of": aware(snap.as_of).isoformat(),
        "venue_micros": snap.venue_equity_micros,
        "computed_micros": snap.computed_equity_micros,
        "divergence_micros": snap.venue_equity_micros - snap.computed_equity_micros,
        "tolerance_micros": snap.equity_tolerance_micros,
        "ok": bool(snap.equity_reconciled),
    }


# ---- realized P&L --------------------------------------------------------------------


@dataclass(frozen=True)
class _Closes:
    by_asset: dict[str, tuple[list[date], list[Fraction]]]

    @classmethod
    def of(cls, closes: Sequence[PriceIn]) -> "_Closes":
        rows: dict[str, list[tuple[date, Fraction]]] = {}
        for p in closes:
            rows.setdefault(p.asset, []).append((p.day, p.exact_micros))
        return cls(
            {a: ([d for d, _ in sorted(r)], [c for _, c in sorted(r)]) for a, r in rows.items()}
        )

    def on_or_before(self, asset: str, day: date) -> Fraction | None:
        if asset in CASH_AT_PAR:
            return Fraction(MICROS_PER_UNIT)
        days, values = self.by_asset.get(asset, ([], []))
        i = bisect_right(days, day)
        return values[i - 1] if i else None


def realized_pnl_micros(legs: Sequence[valuation.LegIn], closes: Sequence[PriceIn]) -> int:
    """Sum of the venue's REALIZED_PNL and LIQUIDATION legs in USD micros: a
    stablecoin leg at 1.00, any other asset at its close on (or last before)
    the leg's UTC day; a leg with no close is left out. Exact, truncated once."""
    index = _Closes.of(closes)
    total = Fraction(0)
    for leg in legs:
        if leg.kind not in REALIZED_KINDS:
            continue
        close = index.on_or_before(leg.asset, leg.occurred_at.astimezone(UTC).date())
        if close is not None:
            total += Fraction(leg.delta_atoms, 10**leg.decimals) * close
    return int(total)


# ---- the stats row -------------------------------------------------------------------


def _holding_row(p: valuation.PositionValue, kinds: dict[str, str]) -> dict:
    if p.kind is PositionKind.CASH:
        kind = "cash"
    elif p.kind is PositionKind.PERP:
        kind = "perp"
    else:
        kind = kinds.get(p.asset, "spot")
    return {
        "asset": p.asset,
        "kind": kind,
        "qty": format_atoms(p.qty.atoms, p.qty.decimals),
        "close_micros": p.close_micros,
        "price_day": p.price_day.isoformat() if p.price_day else None,
        "value_micros": p.value_micros,
        "notional_micros": p.notional_micros,
        "entry": fraction_str(p.entry) if p.entry is not None else None,
    }


def _exposure_dict(e: valuation.Exposure) -> dict:
    return {
        "day": e.day.isoformat(),
        "equity_micros": e.equity_micros,
        "gross_micros": e.gross_micros,
        "by_underlying_micros": dict(sorted(e.by_underlying_micros.items())),
        "top_asset": e.top_asset,
        "top_share": _float(e.top_share),
        "perp_gross_leverage": _float(e.perp_gross_leverage),
    }


def _valuation_flags(
    series: Sequence[DayValuation], carry_allowance_days: int
) -> tuple[dict, bool]:
    """The extras' valuation_flags over every day of the series, and whether
    any carried-forward close is older than the allowance (the stale flag)."""
    unpriced: set[str] = set()
    entry_unknown: set[str] = set()
    mismatch: set[str] = set()
    carried_days = 0
    stale = False
    for v in series:
        carried = False
        for f in v.flags:
            if f.reason is FlagReason.UNPRICED:
                unpriced.add(f.asset)
            elif f.reason is FlagReason.PERP_ENTRY_UNKNOWN:
                entry_unknown.add(f.asset)
            elif f.reason is FlagReason.PERP_QTY_MISMATCH:
                mismatch.add(f.asset)
            elif f.reason is FlagReason.CARRIED_FORWARD:
                carried = True
                if f.price_day is None or (v.day - f.price_day).days > carry_allowance_days:
                    stale = True
        carried_days += carried
    return (
        {
            "unpriced": sorted(unpriced),
            "carried_forward_days": carried_days,
            "perp_entry_unknown": sorted(entry_unknown),
            "perp_qty_mismatch": sorted(mismatch),
        },
        stale,
    )


def _open_positions(v: DayValuation) -> int:
    held = {p.asset for p in v.positions if p.kind is not PositionKind.CASH}
    held |= {
        f.asset for f in v.flags if f.reason in (FlagReason.UNPRICED, FlagReason.PERP_ENTRY_UNKNOWN)
    }
    return len(held)


def compute_asset_stats(
    db: Session, strategy: Strategy, as_of: datetime | None = None
) -> StrategyStats:
    account = db.get(VenueAccount, strategy.venue_account_id)
    inputs = valuation_db.account_inputs(db, account) if account is not None else None
    if account is None or inputs is None:
        raise ValueError(f"strategy {strategy.id} has no anchored venue account")
    as_of = aware(as_of or datetime.now(UTC)).astimezone(UTC)
    anchor_day = inputs.anchor_at.astimezone(UTC).date()
    last_day = as_of.date() - timedelta(days=1)  # the last COMPLETE UTC day
    days = [anchor_day + timedelta(days=n) for n in range((last_day - anchor_day).days + 1)]

    legs = [leg for leg in inputs.legs if leg.occurred_at <= as_of]
    fills = [f for f in inputs.perp_fills if f.occurred_at <= as_of]
    held_assets = inputs.assets | {f.asset for f in fills}
    price_assets = _price_assets(held_assets)
    # Closes of complete days only: as_of's own day has no close yet, so none is
    # looked ahead to (by the series, the realized P&L or a connected-today view).
    closes = valuation_db.price_inputs(
        db, price_assets, None, last_day, valuation_db.pricing_venues(account)
    )
    series = valuation.daily_equity_series(inputs.anchor, legs, closes, fills, days)
    # Connected today: no complete day yet; holdings and exposure come from the
    # anchor day's valuation at the previous day's closes, and every return
    # statistic is undefined.
    latest = (
        series[-1]
        if series
        else valuation.valuation_on(inputs.anchor, legs, closes, fills, anchor_day)
    )

    index = twr_index(series)
    twr = valuation.time_weighted_return(series)
    md = valuation.modified_dietz_return(series)
    rets = index_returns(series)
    dd = index_drawdown(index)
    net_flows = sum(p.flow_micros for p in series[1:])
    pnl = series[-1].equity_micros - series[0].equity_micros - net_flows if series else 0

    current = valuation.exposure(latest)
    leverages = [
        e.perp_gross_leverage
        for e in (valuation.exposure(v) for v in series or [latest])
        if e.perp_gross_leverage is not None
    ]
    max_leverage = max(leverages) if leverages else None
    top_share = current.top_share or Fraction(0)

    profile = get_profile(account.venue)
    allowance = EQUITY_CARRY_ALLOWANCE_DAYS if "equities" in profile.asset_classes else 0
    # Connected today: the day being valued is today, whose close cannot exist yet,
    # so carrying the previous day's close is expected (one more day allowed).
    vflags, stale = _valuation_flags(series or [latest], allowance if series else allowance + 1)
    broker = _latest_broker_check(db, strategy, as_of)
    marked = marked_valuation(db, account, as_of)

    flags: list[str] = []
    if vflags["unpriced"]:
        flags.append("unpriced_assets")
    if stale:
        flags.append("carried_forward_prices")
    if top_share > CONCENTRATION_LIMIT:
        flags.append("single_asset_concentration")
    if max_leverage is not None and max_leverage > LEVERAGE_LIMIT:
        flags.append("high_leverage")
    if broker is not None and not broker["ok"]:
        flags.append("broker_equity_divergence")

    n_fills = db.scalar(
        select(func.count(AssetFill.id)).where(AssetFill.venue_account_id == account.id)
    )
    kinds = _asset_kinds(
        db, account, {p.asset for p in latest.positions if p.kind is PositionKind.ASSET}
    )
    row = StrategyStats(
        strategy_id=strategy.id,
        as_of=as_of,
        track_days=(as_of.date() - anchor_day).days,
        n_fills=n_fills or 0,
        n_settlements=0,
        total_return=_float(twr),
        max_drawdown=float(dd),
        sharpe=stats.sharpe(rets),
        sortino=stats.sortino(rets),
        win_rate=None,
        risk_score=stats.risk_score(
            float(dd), stats.downside_deviation_annual(rets), float(top_share), 0.0
        ),
        equity_micros=latest.equity_micros,
        realized_pnl_micros=realized_pnl_micros(legs, closes),
        flags=flags,
        extras={
            "valuation": "v2",
            "daily_equity": [[v.day.isoformat(), v.equity_micros] for v in series],
            "daily_flows": [[v.day.isoformat(), v.flow_micros] for v in series],
            "twr_index": [[d.isoformat(), float(level)] for d, level in index],
            "twr": _float(twr),
            "modified_dietz": _float(md),
            "net_flows_micros": net_flows,
            "pnl_micros": pnl,
            "holdings": [_holding_row(p, kinds) for p in latest.positions],
            "exposure": _exposure_dict(current),
            "asset_classes": list(profile.asset_classes),
            "price_sources": _price_sources(db, account, price_assets, last_day),
            "valuation_flags": vflags,
            "broker_equity": broker,
            "marked_equity_micros": marked.equity_micros if marked else latest.equity_micros,
            "n_open_positions": _open_positions(latest),
            "max_perp_gross_leverage": _float(max_leverage),
        },
    )
    db.add(row)
    db.commit()
    logger.info("asset stats for strategy %s: risk %d flags %s", strategy.id, row.risk_score, flags)
    return row

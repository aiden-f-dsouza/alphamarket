"""Public strategy page data (M4.2).

Only platform-computed numbers from venue data appear here (invariant #5,
PRD TR-1). The trader's description is passed through labeled unverified.
Privacy default: the page shows the display name, never the email.

wave2 (w2-p8-pages): asset strategies carry `extras["valuation"] == "v2"`
(analytics_v2). `page_data` branches on it: the equity chart is the
flow-adjusted growth of the first day's equity (a deposit is never a gain),
drawdown and monthly returns come from the time-weighted return index, and
the page gets a holdings/exposure panel, venue-aware labels and a
verification text per proof method and tier. Binary (Kalshi, Polymarket)
pages keep their M4 shape.
"""

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket import activity_flags
from alphamarket.analytics import latest_stats
from alphamarket.charts import polyline, window_x
from alphamarket.config import get_settings
from alphamarket.connectors.base import (
    CredentialKind,
    InstrumentKind,
    OwnershipProof,
    ReadOnlyProof,
    VenueProfile,
)
from alphamarket.connectors.registry import PROFILES
from alphamarket.db import get_db
from alphamarket.models import (
    EquitySnapshot,
    Instrument,
    Settlement,
    Strategy,
    StrategyKind,
    StrategyStatus,
    User,
    VenueAccount,
)
from alphamarket.stats import drawdown_series, micros_to_float, monthly_returns

INCUBATION_DAYS = 90
RECENT_SETTLEMENTS = 5
RECENT_SIGNALS = 8
SPARKLINE_DAYS = 60
TICKER_ITEMS = 12
INDEX_SCALE = 1_000_000  # twr_index floats -> integer ppm, display math only

ASSET_CLASS_LABELS: dict[str, str] = {
    "prediction": "Prediction markets",
    "equities": "Stocks",
    "crypto": "Crypto",
}
ASSET_CLASS_ORDER = ("prediction", "equities", "crypto")

router = APIRouter(prefix="/api/strategies", tags=["public"])


def is_visible(strategy: Strategy, account: VenueAccount | None = None) -> bool:
    """Public when the creator wants it, it is not quarantined or delisted,
    and ownership of the venue account is proven (M7: address-keyed venues
    need a profile-code claim; an unclaimed account could be anyone's). A
    watched address (kind "watch") is never public, whatever its other fields
    say: it is reached only through the watcher's own Watch row."""
    if strategy.kind == StrategyKind.WATCH:
        return False
    if not strategy.public or strategy.status in (
        StrategyStatus.QUARANTINED,
        StrategyStatus.DELISTED,
    ):
        return False
    return account is None or account.claimed


def is_demo_owned(db: Session, strategy: Strategy) -> bool:
    """True when the strategy belongs to a demo user (scripts/seed_demo.py):
    sample data, whatever engine computed its numbers (invariant 7)."""
    owner = db.get(User, strategy.user_id)
    return bool(owner is not None and owner.is_demo)


def operator_of(owner: User | None) -> str:
    """Who runs the strategy: "koine" for a house user's account (a Koine-owned
    account connected read-only like any other), else "creator". A label only:
    it never changes visibility, ranking or incubation."""
    return "koine" if owner is not None and owner.is_house else "creator"


def publicly_visible(db: Session, strategy: Strategy, account: VenueAccount | None = None) -> bool:
    """is_visible, and invariant 7: a demo user's strategy is public only while
    DEMO_MODE is on (and is then shown under the "Sample data" banner). Every
    public query (marketplace, strategy page and JSON, feed, landing) goes
    through this."""
    if not is_visible(strategy, account):
        return False
    return get_settings().demo_mode or not is_demo_owned(db, strategy)


def verification_tier(account: VenueAccount | None) -> str | None:
    """venues.verification_tier (the one definition), None without an account."""
    if account is None:
        return None
    from alphamarket.venues import verification_tier as _tier  # venues imports public

    return _tier(account)


def asset_classes_of(venue: str | None) -> tuple[str, ...]:
    profile = PROFILES.get(venue) if venue else None
    return profile.asset_classes if profile else ()


def asset_class_label(venue: str | None) -> str | None:
    classes = asset_classes_of(venue)
    return ASSET_CLASS_LABELS.get(classes[0]) if classes else None


def is_asset_venue(venue: str | None) -> bool:
    profile = PROFILES.get(venue) if venue else None
    return profile is not None and profile.instrument is InstrumentKind.ASSET


def display_name_of(venue: str | None) -> str | None:
    profile = PROFILES.get(venue) if venue else None
    return profile.display_name if profile else (venue.capitalize() if venue else None)


# ---- verification text ----------------------------------------------------------------


def _proof_line(profile: VenueProfile) -> str:
    """What proves the credential cannot move money, per credential kind."""
    name = profile.display_name
    if profile.credential is CredentialKind.ADDRESS:
        if profile.ownership is OwnershipProof.SIGNED_MESSAGE:
            return (
                "Public wallet address, no key held; ownership proven by a message "
                "signed with the wallet"
            )
        return (
            "Public wallet address, no key held; ownership proven by a Koine code "
            f"in the {name} profile"
        )
    if profile.credential is CredentialKind.OAUTH2:
        return f"Read-only OAuth grant approved inside the account at {name}, re-checked nightly"
    if profile.credential is CredentialKind.REPORT_TOKEN:
        return "Reporting-only token: this token type cannot place orders or move money"
    if profile.credential is CredentialKind.AGGREGATOR:
        return (
            f"Connected through {name}'s read-only connection; the aggregator offers no "
            "independent read-only proof, so this rests on its connection type"
        )
    if profile.readonly_proof is ReadOnlyProof.ORDER_PROBE:
        return (
            "Read-only key, proven by a refused order or withdrawal probe, re-checked every night"
        )
    return "Read-only key, scope re-checked every sync"


TIER_LABELS = {
    "reconciled": "reconciled nightly",
    "daily_verified": "daily verified",
    "fills_only": "fills verified · balance not reconciled",
    "unclaimed": "ownership unclaimed",
}


def verification_view(profile: VenueProfile | None, tier: str | None) -> dict | None:
    """The proof line and tier note shown in the "How this record is verified"
    panel. Pure: the text depends only on the venue profile and the tier."""
    if profile is None:
        return None
    note = None
    if tier == "daily_verified":
        note = (
            f"{profile.display_name} reports end of day only, so this record is verified at "
            "day resolution: balances are checked against the latest daily statement, "
            "not intraday."
        )
    return {
        "proof": _proof_line(profile),
        "tier": tier,
        "tier_label": TIER_LABELS.get(tier or "", ""),
        "note": note,
    }


# ---- asset strategies (extras "valuation": "v2") -----------------------------------------


def _f(value) -> float | None:
    """A number out of a JSON extras value (float, int or decimal string)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _usd_label(micros: int) -> str:
    """USD from micros, 2 places from a dollar up, 6 below (sub-cent coins
    would otherwise read $0.00)."""
    value = Decimal(micros) / INDEX_SCALE
    places = 2 if abs(value) >= 1 else 6
    return f"{'−' if value < 0 else ''}${abs(value):,.{places}f}"


def is_v2(stats) -> bool:
    return stats is not None and (stats.extras or {}).get("valuation") == "v2"


def holdings_view(extras: dict) -> list[dict]:
    """Rows for the holdings panel, largest first. Perp rows show notional and
    the entry price (their `value` is unrealized P&L, not a holding's worth),
    and get no share. Share = value / sum of the cash, spot and equity values
    (methodology "Reading an asset strategy's page"), so the shares add up to
    100% and a losing perp cannot push them past it."""
    total = sum(
        int(h.get("value_micros", 0))
        for h in extras.get("holdings", [])
        if h.get("kind", "spot") != "perp"
    )
    out = []
    for h in extras.get("holdings", []):
        kind = h.get("kind", "spot")
        value = int(h.get("value_micros", 0))
        close = h.get("close_micros")
        notional = h.get("notional_micros")
        notional = abs(int(notional)) if notional is not None else abs(value)
        out.append(
            {
                "asset": h.get("asset"),
                "kind": kind,
                "qty": h.get("qty"),
                "price_label": None if kind == "cash" or close is None else _usd_label(int(close)),
                "price_day": h.get("price_day"),
                "value": micros_to_float(value),
                "value_label": _usd_label(value),
                "notional_label": _usd_label(notional),
                "entry": h.get("entry"),
                "weight": value / total if kind != "perp" and total > 0 else None,
                "_order": notional,
            }
        )
    out.sort(key=lambda r: (-r["_order"], r["asset"] or ""))
    for r in out:
        del r["_order"]
    return out


def exposure_view(extras: dict) -> dict | None:
    e = extras.get("exposure")
    if not e:
        return None
    gross = int(e.get("gross_micros", 0))
    by = e.get("by_underlying_micros") or {}
    return {
        "day": e.get("day"),
        "gross_label": _usd_label(gross),
        "top_asset": e.get("top_asset"),
        "top_share": _f(e.get("top_share")),
        "perp_leverage": _f(e.get("perp_gross_leverage")),
        "by_underlying": [
            {"asset": a, "share": (int(v) / gross) if gross else 0.0, "label": _usd_label(int(v))}
            for a, v in sorted(by.items(), key=lambda kv: (-int(kv[1]), kv[0]))
        ],
    }


def growth_curve(extras: dict) -> list[tuple[date, float]]:
    """Growth of the first funded day's equity with external flows removed:
    E_f / index(f) x index(d), dollars, where f is the first day of the index
    with positive equity (index(f) is 1.0 unless the account was emptied and
    refunded). The chart plots this, never raw equity, so a deposit cannot
    look like a return; an account that opens empty and is funded on day 2
    is not drawn as growth of $0."""
    index = extras.get("twr_index") or []
    if not index:
        return []
    equity = {d: int(e) for d, e in extras.get("daily_equity") or []}
    base = 1.0
    for d, v in index:
        if equity.get(d, 0) > 0 and float(v) > 0:
            base = micros_to_float(equity[d]) / float(v)
            break
    return [(date.fromisoformat(d), base * float(v)) for d, v in index]


def index_series(extras: dict) -> list[tuple[date, int]]:
    """twr_index as (day, integer ppm) so stats.drawdown_series and
    stats.monthly_returns (integer series) apply to it unchanged. Display math
    only; the index itself is stored as computed."""
    return [(date.fromisoformat(d), round(float(v) * INDEX_SCALE)) for d, v in extras["twr_index"]]


def v2_stats_view(extras: dict) -> dict:
    series = index_series(extras) if extras.get("twr_index") else []
    flags = extras.get("valuation_flags") or {}
    broker = extras.get("broker_equity")
    growth = growth_curve(extras)
    return {
        "valuation": "v2",
        "holdings": holdings_view(extras),
        "exposure": exposure_view(extras),
        "chart_curve": [[d.isoformat(), round(v, 2)] for d, v in growth],
        "drawdown_curve": [[d.isoformat(), dd] for d, dd in drawdown_series(series)],
        "monthly_returns": [[y, m, r] for y, m, r in monthly_returns(series)],
        "twr": _f(extras.get("twr")),
        "n_open_positions": extras.get("n_open_positions"),
        "asset_classes": list(extras.get("asset_classes") or []),
        "price_sources": list(extras.get("price_sources") or []),
        "unpriced": list(flags.get("unpriced") or []),
        "carried_forward_days": int(flags.get("carried_forward_days") or 0),
        "perp_entry_unknown": list(flags.get("perp_entry_unknown") or []),
        "broker_equity": (
            {
                "ok": bool(broker.get("ok")),
                "venue": _usd_label(int(broker["venue_micros"])),
                "computed": _usd_label(int(broker["computed_micros"])),
                "as_of": broker.get("as_of"),
            }
            if broker
            else None
        ),
    }


# ---- signals (both shapes) -----------------------------------------------------------------


def instrument_label(inst: Instrument | None, fallback: str = "?") -> str:
    """Instrument.label, or `fallback` when the instrument is unknown."""
    return inst.label if inst is not None else fallback


def instrument_labels(db: Session, signals) -> dict[int, str]:
    """instrument_label for every asset signal's instrument, keyed by id."""
    ids = {s.instrument_id for s in signals if s.instrument_id is not None}
    if not ids:
        return {}
    rows = db.scalars(select(Instrument).where(Instrument.id.in_(ids))).all()
    return {i.id: instrument_label(i) for i in rows}


def signal_items(db: Session, strategy: Strategy, limit: int = 20) -> list[dict]:
    """Recent signals of a strategy, binary and asset rows alike (one
    renderer for the page, the JSON API and the feed: follows.signal_item)."""
    from alphamarket.follows import signal_item
    from alphamarket.signals import recent_signals

    signals = recent_signals(db, strategy.id, limit)
    labels = instrument_labels(db, signals)
    return [signal_item(strategy, s, labels) for s in signals]


# ---- strategy page ---------------------------------------------------------------------------


def page_data(db: Session, strategy: Strategy) -> dict:
    account = db.get(VenueAccount, strategy.venue_account_id)
    owner = db.get(User, strategy.user_id)
    stats = latest_stats(db, strategy.id)
    last_recon = db.scalar(
        select(EquitySnapshot)
        .where(EquitySnapshot.strategy_id == strategy.id)
        .order_by(EquitySnapshot.as_of.desc())
        .limit(1)
    )
    venue = account.venue if account else None
    profile = PROFILES.get(venue) if venue else None
    tier = verification_tier(account)
    asset_venue = is_asset_venue(venue)

    data = {
        "slug": strategy.slug,
        "name": strategy.name,
        "display_name": owner.display_name if owner else "",
        "operator": operator_of(owner),
        # invariant 7: a seeded demo record; the page shows the sample banner.
        "sample_data": bool(owner is not None and owner.is_demo),
        "venue": venue,
        "venue_label": display_name_of(venue),
        "venue_env": account.venue_env if account else None,
        "asset_class": asset_classes_of(venue)[0] if asset_classes_of(venue) else None,
        "asset_class_label": asset_class_label(venue),
        "is_asset": asset_venue,
        "status": strategy.status,
        "style_tags": [t.strip() for t in strategy.style_tags.split(",") if t.strip()],
        "description_unverified": strategy.description,
        "verified_since": account.anchor_at.isoformat() if account and account.anchor_at else None,
        "last_reconciled": last_recon.as_of.isoformat() if last_recon else None,
        "reconciled": last_recon.reconciled if last_recon else None,
        "verification_tier": tier,
        "verification": verification_view(profile, tier),
        "activity_flags": activity_flags.summary_for(db, account.id) if account else None,
        "stats": None,
    }
    if stats is None:
        return data

    daily = [(date.fromisoformat(d), int(e)) for d, e in stats.extras.get("daily_equity", [])]
    v2 = is_v2(stats)
    data["incubating"] = stats.track_days < INCUBATION_DAYS
    data["stats"] = {
        "as_of": stats.as_of.isoformat(),
        "track_days": stats.track_days,
        "n_fills": stats.n_fills,
        "n_settlements": stats.n_settlements,
        "total_return": stats.total_return,
        "max_drawdown": stats.max_drawdown,
        "sharpe": stats.sharpe,
        "sortino": stats.sortino,
        "win_rate": stats.win_rate,
        "risk_score": stats.risk_score,
        "equity": micros_to_float(stats.equity_micros),
        "realized_pnl": micros_to_float(stats.realized_pnl_micros),
        # Asset strategies: last equity - first equity - net external flows
        # (analytics_v2). Spot venues report no realized-P&L entries, so the
        # realized figure would read $0 there.
        "net_pnl": (
            micros_to_float(stats.extras["pnl_micros"])
            if "pnl_micros" in (stats.extras or {})
            else None
        ),
        "flags": list(stats.flags),
        "top_series_share": stats.extras.get("top_series_share"),
        "peak_invested": micros_to_float(stats.extras.get("peak_invested_micros") or 0),
        "return_on_peak_invested": stats.extras.get("return_on_peak_invested"),
        "equity_curve": [[d.isoformat(), micros_to_float(e)] for d, e in daily],
        "drawdown_curve": [[d.isoformat(), dd] for d, dd in drawdown_series(daily)],
        "monthly_returns": [[y, m, r] for y, m, r in monthly_returns(daily)],
    }
    if v2:
        data["stats"].update(v2_stats_view(stats.extras))
        data["recent_signals"] = signal_items(db, strategy, RECENT_SIGNALS)
    else:
        data["recent_settlements"] = recent_settlements(db, strategy.id)
    return data


def recent_settlements(db: Session, strategy_id: int, limit: int = RECENT_SETTLEMENTS) -> list:
    """Newest settlements as the venue reported them: ticker, which side the
    trader held (contract counts), the market result and the payout. Payout is
    the venue's revenue figure, not a per-trade P&L (cost basis lives in fills)."""
    rows = db.scalars(
        select(Settlement)
        .where(Settlement.strategy_id == strategy_id)
        .order_by(Settlement.settled_at.desc(), Settlement.id.desc())
        .limit(limit)
    ).all()
    return [
        {
            "ticker": s.ticker,
            "result": s.market_result,
            "side": "yes" if s.yes_count_micros >= s.no_count_micros else "no",
            "count": micros_to_float(max(s.yes_count_micros, s.no_count_micros)),
            "payout": micros_to_float(s.revenue_micros),
            "settled_at": s.settled_at.date().isoformat(),
        }
        for s in rows
    ]


def recent_public_settlements(db: Session, limit: int = TICKER_ITEMS) -> list:
    """Newest settlements across every visible strategy, for the landing
    ticker. Same visibility rule as the marketplace: an unlisted, quarantined
    or unclaimed account's trades never appear."""
    rows = db.scalars(
        select(Settlement).order_by(Settlement.settled_at.desc(), Settlement.id.desc()).limit(200)
    ).all()
    seen: dict[int, bool] = {}
    names: dict[int, str] = {}
    out = []
    for s in rows:
        if s.strategy_id not in seen:
            strategy = db.get(Strategy, s.strategy_id)
            account = db.get(VenueAccount, strategy.venue_account_id) if strategy else None
            seen[s.strategy_id] = bool(strategy) and publicly_visible(db, strategy, account)
            if strategy:
                names[s.strategy_id] = strategy.name
        if not seen[s.strategy_id]:
            continue
        side = "yes" if s.yes_count_micros >= s.no_count_micros else "no"
        out.append(
            {
                "strategy": names[s.strategy_id],
                "ticker": s.ticker,
                "side": side,
                "won": s.market_result == side,
                "payout": micros_to_float(s.revenue_micros),
                "settled_at": s.settled_at.date().isoformat(),
            }
        )
        if len(out) >= limit:
            break
    return out


def list_visible(
    db: Session,
    include_incubating: bool = False,
    tag: str | None = None,
    venue: str | None = None,
    asset: str | None = None,
    operator: str | None = None,
) -> list:
    """Rows for the explore page: every visible strategy that has stats, newest
    stats row each, sorted by total return. Incubating strategies (< 90 verified
    days) are hidden unless asked for; quarantined/delisted/unlisted never show.
    `venue` keeps one venue's strategies, `asset` one asset class
    ("prediction" | "crypto" | "equities", from the venue profile). `operator`
    "koine" keeps only Koine-run strategies, "creator" drops them, anything
    else keeps all; the sort never looks at it (no ranking advantage)."""
    rows = []
    for strategy in db.scalars(select(Strategy).order_by(Strategy.id)).all():
        account = db.get(VenueAccount, strategy.venue_account_id)
        if not publicly_visible(db, strategy, account):
            continue
        if venue and (account is None or account.venue != venue):
            continue
        classes = asset_classes_of(account.venue if account else None)
        if asset and asset not in classes:
            continue
        tags = [t.strip() for t in strategy.style_tags.split(",") if t.strip()]
        if tag and tag not in tags:
            continue
        stats = latest_stats(db, strategy.id)
        if stats is None:
            continue
        incubating = stats.track_days < INCUBATION_DAYS
        if incubating and not include_incubating:
            continue
        owner = db.get(User, strategy.user_id)
        op = operator_of(owner)
        if operator in ("koine", "creator") and op != operator:
            continue
        last_recon = db.scalar(
            select(EquitySnapshot)
            .where(EquitySnapshot.strategy_id == strategy.id)
            .order_by(EquitySnapshot.as_of.desc())
            .limit(1)
        )
        if is_v2(stats) and stats.extras.get("twr_index"):
            # flow-adjusted: a deposit must not bend the sparkline upward
            daily = [float(v) for _, v in stats.extras["twr_index"][-SPARKLINE_DAYS:]]
        else:
            daily = [float(e) for _, e in stats.extras.get("daily_equity", [])[-SPARKLINE_DAYS:]]
        rows.append(
            {
                "slug": strategy.slug,
                "name": strategy.name,
                "display_name": owner.display_name if owner else "",
                "operator": op,
                # invariant 7: the row's `demo` key drives the sample banner.
                "demo": bool(owner is not None and owner.is_demo),
                "tags": tags,
                "incubating": incubating,
                "venue": account.venue if account else None,
                "venue_label": display_name_of(account.venue if account else None),
                "asset_classes": list(classes),
                "asset_class_label": asset_class_label(account.venue if account else None),
                "verification_tier": verification_tier(account),
                "total_return": stats.total_return
                if stats.total_return is not None
                else (stats.extras.get("return_on_peak_invested") or 0.0),
                "max_drawdown": stats.max_drawdown,
                "sharpe": stats.sharpe,
                "risk_score": stats.risk_score,
                "track_days": stats.track_days,
                "reconciled": last_recon.reconciled if last_recon else None,
                "spark": polyline(daily, 120, 32),
                "spark_dd": window_x(daily, 120),
            }
        )
    rows.sort(key=lambda r: r["total_return"], reverse=True)
    return rows


def all_tags(rows: list) -> list[str]:
    return sorted({t for r in rows for t in r["tags"]})


def venues_in(rows: list) -> list[tuple[str, str]]:
    """(venue, display name) of the venues the rows come from, by name."""
    seen = {r["venue"]: r.get("venue_label") or r["venue"] for r in rows if r.get("venue")}
    return sorted(seen.items(), key=lambda kv: kv[1])


def visible_strategy_or_404(db: Session, slug: str) -> Strategy:
    strategy = db.scalar(select(Strategy).where(Strategy.slug == slug))
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    account = db.get(VenueAccount, strategy.venue_account_id)
    if not publicly_visible(db, strategy, account):
        raise HTTPException(status_code=404, detail="Strategy not found")
    return strategy


@router.get("/{slug}")
def strategy_json(slug: str, db: Session = Depends(get_db)) -> dict:
    return page_data(db, visible_strategy_or_404(db, slug))


@router.get("/{slug}/signals")
def strategy_signals(slug: str, db: Session = Depends(get_db)) -> dict:
    """Recent fills of a visible strategy as signals (M6). Sizes are shown as
    a fraction of the creator's equity, not in dollars. Binary and asset
    signals (wave2) share the endpoint."""
    strategy = visible_strategy_or_404(db, slug)
    return {"slug": strategy.slug, "signals": signal_items(db, strategy)}

"""/watch/discover: browse venues' public leaderboards and add an address to
the private watchlist (copy strategies step 7; docs/copy_strategies_plan.md
"Later: leaderboard browsing").

The worker refreshes one `LeaderboardSnapshot` per (venue, period) from the
venue's public market data (`refresh`, job kind `watch_discover`); the page
only reads snapshots (`discover_view`), so a page view never calls a venue.

Rules:
- Private and logged in only, like every /watch page; never indexed.
- Addresses only. The venue's display names, usernames and avatars are
  dropped by the connector (`LeaderboardEntry` has no field for them) and
  never stored; no identity lookups of any kind.
- An opted-out address (`watch_optouts`) is never shown.
- Figures are the venue's own for the period, labelled as such; Koine's
  record of an address starts when someone watches it.
- Gated by `settings.watch_discover_enabled` (vault it by turning that off)
  and limited to venues open for watching (`watchlist.open_venues`).
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alphamarket.config import get_settings
from alphamarket.connectors.base import (
    BudgetExhausted,
    LeaderboardDetails,
    LeaderboardEntry,
    LeaderboardPeriod,
    RateLimited,
)
from alphamarket.connectors.registry import build_market_data, get_profile
from alphamarket.jobs import JobContext, aware
from alphamarket.models import (
    LeaderboardSnapshot,
    Strategy,
    User,
    VenueAccount,
    Watch,
    WatchOptout,
)
from alphamarket.money import MICROS_PER_UNIT, to_micros
from alphamarket.public import publicly_visible
from alphamarket.venue_limits import is_venue_outage, rate_limit_from_http
from alphamarket.watchlist import open_venues, short_address, watch_count

logger = logging.getLogger(__name__)

SORTS = ("pnl", "roi")
PERIOD_ORDER = tuple(p.value for p in LeaderboardPeriod)

# Every column a venue's `VenueProfile.leaderboard_columns` may name, with its
# header. Venue overrides say what is different about that venue's figure.
COLUMN_LABELS = {
    "pnl": "PnL",
    "roi": "ROI",
    "volume": "Volume",
    "account_value": "Account value",
    "positions_value": "Positions value",
    "markets_traded": "Markets",
    "biggest_win": "Biggest win",
    "active_since": "Since",
}
VENUE_COLUMN_LABELS = {"polymarket": {"volume": "Volume (UTC days)"}}


class RowState:
    ADD = "add"  # can be added to the user's watchlist
    WATCHING = "watching"  # already on the user's watchlist (watch_strategy_id)
    LISTED = "listed"  # a claimed, public Koine strategy (listed_slug)
    FULL = "full"  # the user's watchlist is at watch_max_per_user


@dataclass(frozen=True)
class DiscoverRow:
    venue: str
    address: str  # lowercased
    short: str  # watchlist.short_address
    rank: int  # the venue's rank for the period
    pnl_micros: int
    roi: float | None  # fraction, 0.1234 = 12.34%; display only, never money
    volume_micros: int | None
    account_value_micros: int | None
    state: str  # RowState
    watch_strategy_id: int | None = None  # state WATCHING: /watch/{id}
    listed_slug: str | None = None  # state LISTED: /s/{slug}
    # Venue wallet stats (LeaderboardDetails); None = the venue has no such column or no value
    positions_value_micros: int | None = None
    markets_traded: int | None = None
    biggest_win_micros: int | None = None
    active_since: int | None = None  # epoch seconds, UTC

    @property
    def since(self) -> str:
        """active_since as month and year (UTC), "" when unknown."""
        if self.active_since is None:
            return ""
        return datetime.fromtimestamp(self.active_since, UTC).strftime("%b %Y")


@dataclass(frozen=True)
class DiscoverView:
    venue: str | None  # None: no venue can be browsed (see venues)
    period: str
    sort: str
    venues: tuple[str, ...]  # browsable venues, in watchlist order
    periods: tuple[str, ...]  # periods `venue` offers
    fetched_at: datetime | None  # None: no snapshot yet for (venue, period)
    rows: list[DiscoverRow]
    watch_count: int
    watch_max: int
    columns: tuple[str, ...] = ("pnl", "roi", "volume", "account_value")  # venue's, in order
    labels: dict[str, str] | None = None  # column -> header; None = COLUMN_LABELS
    note: str = ""  # the venue's one-line reading guide

    @property
    def sorts(self) -> tuple[str, ...]:
        """The sort toggles this venue offers: roi only when it has a roi column."""
        return tuple(s for s in SORTS if s == "pnl" or s in self.columns)

    def label(self, column: str) -> str:
        return (self.labels or COLUMN_LABELS).get(column, COLUMN_LABELS[column])


def columns_for(venue: str) -> tuple[str, ...]:
    return get_profile(venue).leaderboard_columns


def labels_for(venue: str) -> dict[str, str]:
    return {**COLUMN_LABELS, **VENUE_COLUMN_LABELS.get(venue, {})}


def row_json(view: "DiscoverView", r: DiscoverRow) -> dict:
    """One row of /api/watch/discover: the venue's columns only (integer
    micros for money, epoch seconds for active_since)."""
    out: dict = {"address": r.address, "rank": r.rank, "pnl_micros": r.pnl_micros}
    keys = {
        "roi": ("roi", r.roi),
        "volume": ("volume_micros", r.volume_micros),
        "account_value": ("account_value_micros", r.account_value_micros),
        "positions_value": ("positions_value_micros", r.positions_value_micros),
        "markets_traded": ("markets_traded", r.markets_traded),
        "biggest_win": ("biggest_win_micros", r.biggest_win_micros),
        "active_since": ("active_since", r.active_since),
    }
    for column in view.columns:
        if column in keys:
            key, value = keys[column]
            out[key] = value
    out["state"] = r.state
    return out


def enabled() -> bool:
    return get_settings().watch_discover_enabled


def discover_venues() -> tuple[str, ...]:
    """Venues open for watching whose profile publishes a leaderboard."""
    return tuple(v for v in open_venues() if get_profile(v).leaderboard_periods)


def periods_for(venue: str) -> tuple[str, ...]:
    offered = set(get_profile(venue).leaderboard_periods)
    return tuple(p for p in PERIOD_ORDER if p in offered)


def _micros_or_none(value: str | None) -> int | None:
    return None if value is None else to_micros(value)


def snapshot_row(entry: LeaderboardEntry) -> dict:
    """The stored form of one leaderboard row: integer micros (invariant 1),
    ROI as its decimal string. The one place a LeaderboardEntry is converted,
    shared by refresh and the seeded demo."""
    return {
        "address": entry.address.lower(),
        "rank": entry.rank,
        "pnl_micros": to_micros(entry.pnl),
        "roi": entry.roi,
        "volume_micros": _micros_or_none(entry.volume),
        "account_value_micros": _micros_or_none(entry.account_value),
    }


def store_snapshot(
    db: Session, venue: str, period: str, rows: list[dict], fetched_at: datetime
) -> LeaderboardSnapshot:
    """Insert or replace the (venue, period) snapshot. Caller commits."""
    snap = db.scalar(
        select(LeaderboardSnapshot).where(
            LeaderboardSnapshot.venue == venue, LeaderboardSnapshot.period == period
        )
    )
    if snap is None:
        snap = LeaderboardSnapshot(venue=venue, period=period)
        db.add(snap)
    snap.rows = rows
    snap.fetched_at = fetched_at
    return snap


def refresh(ctx: JobContext) -> dict:
    """Job body for JobKind.WATCH_DISCOVER: fetch every discover venue's
    leaderboards through its market data (spending the venue's outbound
    budget), filter (min account value, opt-outs), keep the top
    watch_discover_rows per period and store them. Returns counts per
    (venue, period) for the job log.

    Like the prices job, a venue whose circuit is open is skipped, and one
    that is out of budget or answers 429 is left for the next tick (no
    early re-run: a refresh has no cursor, and the snapshot it would replace
    is still there). Any failure of one venue leaves the others to run; its
    previous snapshots stay. Does nothing while the feature is off."""
    if not enabled():
        return {}
    db, limiter = ctx.db, ctx.limiter
    counts: dict[tuple[str, str], int] = {}
    for venue in discover_venues():
        details_source = None  # built lazily, shared by the venue's periods (its cache)
        if limiter is not None and limiter.breaker_open_until(venue) is not None:
            logger.info("watch discover: %s circuit open, skipped until the next tick", venue)
            continue
        periods = tuple(LeaderboardPeriod(p) for p in periods_for(venue))
        try:
            boards = _fetch(venue, periods, limiter)
            if boards is None:
                continue
            blocked = set(db.scalars(select(WatchOptout.address).where(WatchOptout.venue == venue)))
            fetched_at = ctx.clock()
            for period in periods:
                if period not in boards:  # the venue sent nothing: keep the old one
                    continue
                rows = select_rows(boards[period], blocked)
                if details_source is None:
                    details_source = _details_source(venue, limiter)
                beat = getattr(ctx, "heartbeat", None)
                if beat is not None:  # enrichment can take minutes: keep the job's lease
                    beat()
                _enrich(venue, details_source, period, rows, fetched_at, limiter)
                store_snapshot(db, venue, period.value, rows, fetched_at)
                counts[(venue, period.value)] = len(rows)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("watch discover refresh failed for %s", venue)
    return counts


def _fetch(
    venue: str, periods: tuple[LeaderboardPeriod, ...], limiter
) -> dict[LeaderboardPeriod, list[LeaderboardEntry]] | None:
    """One venue's boards through its throttled public market data, feeding
    its circuit breaker. None when the venue is busy (budget, 429): it is
    left for the next tick. Other failures raise."""
    try:
        source = build_market_data(venue, get_settings().venue_env(venue))
        if limiter is not None:
            source.set_throttle(limiter.throttle(venue, None))
        boards = source.fetch_leaderboards(periods)
    except Exception as exc:
        limited = exc if isinstance(exc, RateLimited) else rate_limit_from_http(exc)
        if limited is not None and limiter is not None:
            limiter.on_rate_limited(venue, None, limited.retry_after_seconds, limited.endpoint)
        if limited is not None or isinstance(exc, BudgetExhausted):
            logger.info("watch discover: %s busy until the next tick: %s", venue, exc)
            return None
        if limiter is not None and is_venue_outage(exc):
            limiter.record_failure(venue)
        raise
    if limiter is not None:
        limiter.record_success(venue)
    return boards


def _details_source(venue: str, limiter):
    source = build_market_data(venue, get_settings().venue_env(venue))
    if limiter is not None:
        source.set_throttle(limiter.throttle(venue, None))
    return source


def _enrich(venue, source, period, rows: list[dict], now: datetime, limiter) -> None:
    """Merge the venue's per-wallet details into the KEPT rows (after opt-outs
    and the top-N cut, so the calls scale with the snapshot, not the board).
    Best effort: any failure is logged and leaves the fields unknown, and a
    429 tells the limiter; the board is stored either way. Money goes through
    to_micros like the board's own figures (invariant 1)."""
    if not rows:
        return
    try:
        details = source.leaderboard_details(period, [r["address"] for r in rows], now)
    except Exception:
        logger.exception("watch discover: %s %s enrichment failed, board kept", venue, period.value)
        return
    limited = source.enrichment_rate_limited
    if limited is not None and limiter is not None:
        limiter.on_rate_limited(venue, None, limited.retry_after_seconds, limited.endpoint)
    for row in rows:
        d = details.get(row["address"])
        if d is not None:
            merge_details(row, d)


def merge_details(row: dict, d: LeaderboardDetails) -> None:
    """Put one address's LeaderboardDetails into its stored row. A venue's
    period volume replaces the board's only when it has one."""
    if d.volume is not None:
        row["volume_micros"] = to_micros(d.volume)
    row["positions_value_micros"] = _micros_or_none(d.positions_value)
    row["markets_traded"] = d.markets_traded
    row["biggest_win_micros"] = _micros_or_none(d.biggest_win)
    row["active_since"] = d.active_since


def select_rows(entries: list[LeaderboardEntry], blocked: set[str]) -> list[dict]:
    """The snapshot rows of one (venue, period): drop rows whose reported
    account value is under watch_discover_min_account_value (kept when the
    venue reports none), rows with a reported zero volume for the period
    (protocol accounts, not traders) and opted-out addresses, then keep the union of the
    top watch_discover_rows by pnl and the top watch_discover_rows by roi
    (rows without roi never qualify by roi), in pnl order. Ties keep the
    venue's rank order."""
    settings = get_settings()
    floor = settings.watch_discover_min_account_value * MICROS_PER_UNIT
    n = settings.watch_discover_rows
    rows = []
    for entry in sorted(entries, key=lambda e: e.rank):
        row = snapshot_row(entry)
        if row["address"] in blocked:
            continue
        value = row["account_value_micros"]
        if value is not None and value < floor:
            continue
        if row["volume_micros"] == 0:  # reported, and nothing traded in the period
            continue
        rows.append(row)
    by_pnl = sorted(rows, key=lambda r: r["pnl_micros"], reverse=True)  # stable: rank order
    by_roi = sorted((r for r in rows if r["roi"] is not None), key=_roi_key, reverse=True)
    keep = {id(r) for r in by_pnl[:n]} | {id(r) for r in by_roi[:n]}
    return [r for r in by_pnl if id(r) in keep]


def _roi_key(row: dict) -> Decimal:
    return Decimal(row["roi"]) if row.get("roi") is not None else Decimal("-Infinity")


def discover_view(
    db: Session,
    user: User,
    venue: str | None = None,
    period: str | None = None,
    sort: str = "pnl",
    limit: int = 100,
) -> DiscoverView:
    """What /watch/discover shows `user`. Unknown venue/period/sort fall back
    to the first offered (a bad query string never 404s or 500s)."""
    settings = get_settings()
    venues = discover_venues()
    sort = sort if sort in SORTS else "pnl"
    count = watch_count(db, user.id)
    if not venues:
        return DiscoverView(None, "", sort, (), (), None, [], count, settings.watch_max_per_user)
    venue = venue if venue in venues else venues[0]
    columns = columns_for(venue)
    if sort not in ("pnl", *columns):  # e.g. ?sort=roi on a venue without roi
        sort = "pnl"
    periods = periods_for(venue)
    period = period if period in periods else periods[0]

    snap = db.scalar(
        select(LeaderboardSnapshot).where(
            LeaderboardSnapshot.venue == venue, LeaderboardSnapshot.period == period
        )
    )
    raw = list(snap.rows) if snap is not None else []
    blocked = set(db.scalars(select(WatchOptout.address).where(WatchOptout.venue == venue)))
    raw = [r for r in raw if r["address"] not in blocked]
    if sort == "roi":
        raw.sort(key=_roi_key, reverse=True)
    else:
        raw.sort(key=lambda r: r["pnl_micros"], reverse=True)
    raw = raw[:limit]

    addresses = [r["address"] for r in raw]
    watching = _watched_strategy_ids(db, user, venue, addresses)
    listed = _listed_slugs(db, venue, addresses)
    full = count >= settings.watch_max_per_user

    rows = []
    for r in raw:
        address = r["address"]
        if address in watching:
            state = RowState.WATCHING
        elif address in listed:
            state = RowState.LISTED
        elif full:
            state = RowState.FULL
        else:
            state = RowState.ADD
        rows.append(
            DiscoverRow(
                venue=venue,
                address=address,
                short=short_address(address),
                rank=r["rank"],
                pnl_micros=r["pnl_micros"],
                roi=float(r["roi"]) if r.get("roi") is not None else None,
                volume_micros=r.get("volume_micros"),
                account_value_micros=r.get("account_value_micros"),
                state=state,
                watch_strategy_id=watching.get(address),
                listed_slug=listed.get(address),
                positions_value_micros=r.get("positions_value_micros"),
                markets_traded=r.get("markets_traded"),
                biggest_win_micros=r.get("biggest_win_micros"),
                active_since=r.get("active_since"),
            )
        )
    return DiscoverView(
        venue=venue,
        period=period,
        sort=sort,
        venues=venues,
        periods=periods,
        fetched_at=aware(snap.fetched_at) if snap is not None else None,
        rows=rows,
        watch_count=count,
        watch_max=settings.watch_max_per_user,
        columns=columns,
        labels=labels_for(venue),
        note=get_profile(venue).leaderboard_note,
    )


def _watched_strategy_ids(
    db: Session, user: User, venue: str, addresses: list[str]
) -> dict[str, int]:
    if not addresses:
        return {}
    rows = db.execute(
        select(VenueAccount.key_id, Strategy.id)
        .join(Strategy, Strategy.venue_account_id == VenueAccount.id)
        .join(Watch, Watch.strategy_id == Strategy.id)
        .where(
            Watch.user_id == user.id,
            VenueAccount.is_watch.is_(True),
            VenueAccount.venue == venue,
            VenueAccount.key_id.in_(addresses),
        )
    ).all()
    return {key_id: strategy_id for key_id, strategy_id in rows}


def _listed_slugs(db: Session, venue: str, addresses: list[str]) -> dict[str, str]:
    """Addresses that are a claimed creator's public strategy (the same test
    as watchlist.listed_strategy, batched)."""
    if not addresses:
        return {}
    rows = db.execute(
        select(Strategy, VenueAccount)
        .join(VenueAccount, VenueAccount.id == Strategy.venue_account_id)
        .where(
            VenueAccount.venue == venue,
            func.lower(VenueAccount.key_id).in_(addresses),
            VenueAccount.is_watch.is_(False),
            VenueAccount.claimed_at.is_not(None),
        )
    ).all()
    out: dict[str, str] = {}
    for strategy, account in rows:
        address = account.key_id.lower()
        if address not in out and publicly_visible(db, strategy, account):
            out[address] = strategy.slug
    return out

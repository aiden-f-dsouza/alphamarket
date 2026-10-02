"""Asset market data persistence (wave2, workstream w2-market-data).

The worker calls run_daily (PRICES_DAILY job, nightly per pricing venue) and
refresh_asset_marks (end of every PRICES job). The signals job calls
capture_quote. Contract:

run_daily(ctx) -> None
    ctx.job.payload["venue"] is a pricing venue. For every asset held by an
    account priced by that venue (anchors, creator legs, follower legs),
    resolve MarketDataSource.pricing_symbol, else the CoinGecko fallback
    (venue "coingecko"); upsert instruments (margin_tiers for perps via
    fetch_margin_tiers); insert MISSING prices_daily days up to yesterday
    UTC (usd_micros_per_unit via usd_micros_per_unit()); corporate actions
    for equity symbols held by followers. BudgetExhausted/RateLimited ->
    jobs.Defer (the missing days are the cursor).

refresh_asset_marks(db, limiter) -> int
    asset_quotes (and funding_rates for perps) for instruments in open
    follower books. Returns quotes written.

capture_quote(db, venue, venue_env, instrument, limiter) -> AssetQuote | None
    One quote now, through the instrument's pricing venue
    (profile.pricing_venue), stored in asset_quotes and returned. None when
    the source has no quote or the budget is spent (never raises for those).

latest_mark(db, instrument_id, now) -> Mark | None
    Newest asset_quotes row no older than settings.quote_max_age_seconds:
    mid of bid/ask when both, else last.

usd_micros_per_unit(close, quote_asset) -> int | None
    close (decimal string) x 1e6 truncated toward zero when the quote asset
    is USD or a stablecoin at par; None otherwise.

How a rerun costs nothing. Resolution of "which instrument prices this
asset" first looks at what is already stored (instruments that already have
closes, same selection rule on the stored list), and the day ranges already
asked for live in market_data_coverage (migration 0014), so a second
run_daily the same day makes no HTTP request, and a weekend (no daily bar)
is not asked for again. Only a missing range or a new asset spends budget.

Which day a close belongs to is the source's business (venue connectors
return UTC days; CoinGecko reads 00:00 UTC of the next day). Closes are
requested from the day BEFORE an account's anchor (or an allocation's start)
because the creator's equity at a fill is taken at the last complete close
(valuation.equity_before): a fill on the anchor day needs the previous close.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from alphamarket import valuation_db
from alphamarket.config import get_settings
from alphamarket.connectors.base import (
    BudgetExhausted,
    InstrumentKind,
    InstrumentType,
    MarketDataSource,
    NormalizedInstrument,
    RateLimited,
)
from alphamarket.connectors.registry import build_market_data, get_profile
from alphamarket.ingest import upsert_instrument
from alphamarket.jobs import INACTIVE_ACCOUNT_STATUSES, Defer, aware
from alphamarket.models import (
    Allocation,
    AllocationStatus,
    AssetQuote,
    CorporateActionRow,
    FundingRateRow,
    Instrument,
    MarketDataCoverage,
    MirrorAssetFill,
    MirrorLedgerEntry,
    PriceDaily,
    Strategy,
    VenueAccount,
    utcnow,
)

logger = logging.getLogger(__name__)

FALLBACK_SOURCE = valuation_db.FALLBACK_PRICE_SOURCE  # "coingecko"
CASH_ASSET = "USD"
PERP_SUFFIX = "-PERP"
KIND_CLOSES = "closes"
KIND_CORPORATE_ACTIONS = "corporate_actions"
KIND_UNPRICED = "unpriced"  # an asset no source prices, checked on fetched_on

# Corporate actions are fetched for [fetched_on - REFETCH_DAYS, today +
# LOOKAHEAD_DAYS]: announced actions are stored before their ex-date, and the
# overlap re-reads recent days a missed night would have skipped. The venue is
# asked in slices of at most ACTION_WINDOW_DAYS [VERIFY the documented limit].
CORPORATE_ACTION_LOOKAHEAD_DAYS = 30
CORPORATE_ACTION_REFETCH_DAYS = 14
CORPORATE_ACTION_WINDOW_DAYS = 90
CORPORATE_ACTION_SYMBOLS_PER_CALL = 50
MIN_DEFER_SECONDS = 5.0
FUNDING_LOOKBACK = timedelta(days=1)  # first funding fetch when no fill is known


@dataclass(frozen=True)
class Mark:
    instrument_id: int
    price: str  # decimal string, quote asset per unit
    as_of: datetime
    source: str
    basis: str  # "mid" | "last"


class PricesDailyError(Exception):
    """run_daily finished everything it could but some assets failed for
    reasons other than a budget stop; the job retries (coverage makes the
    repeat cheap)."""


# ---- pure helpers -------------------------------------------------------------------------


def usd_micros_per_unit(close: str, quote_asset: str) -> int | None:
    """USD micros one unit is worth: `close` x 1e6 truncated toward zero when
    the quote asset is USD or a stablecoin held at par (USDC, USDT), else None
    (a BTC-quoted close is not a USD value; it needs a second price).
    Exact: the decimal string is never passed through a float."""
    if quote_asset not in MarketDataSource.STABLECOINS_AT_PAR:
        return None
    return int(Fraction(close.strip()) * 1_000_000)


def _market_env(venue: str) -> str:
    """The environment whose public market data prices `venue` (data sources
    that are not venues, like coingecko, have a single environment)."""
    try:
        envs = get_profile(venue).envs
    except ValueError:
        return "prod"
    for preferred in ("prod", "live"):
        if preferred in envs:
            return preferred
    return envs[0]


def _normalized(inst: Instrument) -> NormalizedInstrument:
    return NormalizedInstrument(
        symbol=inst.symbol,
        type=InstrumentType(inst.type),
        base_asset=inst.base_asset,
        quote_asset=inst.quote_asset,
        qty_decimals=inst.qty_decimals,
        qty_step_atoms=inst.qty_step_atoms,
        min_qty_atoms=inst.min_qty_atoms,
        min_notional=inst.min_notional,
        multiplier=inst.multiplier,
        expiry=inst.expiry,
    )


def _positive(value: str | None) -> bool:
    if value is None or not str(value).strip():
        return False
    try:
        return Fraction(str(value).strip()) > 0
    except (ValueError, ZeroDivisionError):
        return False


# ---- sources ------------------------------------------------------------------------------


class _Sources:
    """Throttled market data sources for one run, and the venues' full
    instrument lists (one request each, at most once per run)."""

    def __init__(self, limiter) -> None:
        self.limiter = limiter
        self._sources: dict[str, MarketDataSource] = {}
        self._listed: dict[str, list[NormalizedInstrument]] = {}

    def source(self, venue: str) -> MarketDataSource:
        if venue not in self._sources:
            if self.limiter is not None:
                until = self.limiter.breaker_open_until(venue)
                if until is not None:
                    wait = max(MIN_DEFER_SECONDS, (until - utcnow()).total_seconds())
                    raise BudgetExhausted(wait, f"{venue} circuit open")
            source = build_market_data(venue, _market_env(venue))
            if self.limiter is not None:
                source.set_throttle(self.limiter.throttle(venue, None))
            self._sources[venue] = source
        return self._sources[venue]

    def listed(self, venue: str) -> list[NormalizedInstrument]:
        if venue not in self._listed:
            self._listed[venue] = self.call(venue, self.source(venue).fetch_instruments)
        return self._listed[venue]

    def call(self, venue: str, fn, *args, **kwargs):
        """Run one source call, feeding the venue's circuit breaker."""
        try:
            result = fn(*args, **kwargs)
        except RateLimited as exc:
            self.on_rate_limited(venue, exc)
            raise
        except httpx.TransportError:
            self._failure(venue)
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                self._failure(venue)
            raise
        if self.limiter is not None:
            self.limiter.record_success(venue)
        return result

    def _failure(self, venue: str) -> None:
        if self.limiter is not None:
            self.limiter.record_failure(venue)

    def on_rate_limited(self, venue: str, exc: RateLimited) -> None:
        if self.limiter is not None:
            self.limiter.on_rate_limited(venue, None, exc.retry_after_seconds, exc.endpoint)


# ---- demand: which assets need prices, from when ------------------------------------------


@dataclass
class _Demand:
    assets: dict[str, date] = field(default_factory=dict)  # asset -> first day needed
    equity_symbols: dict[str, date] = field(default_factory=dict)  # follower-held, -> first day

    def want(self, asset: str, first_day: date) -> None:
        if asset == CASH_ASSET:
            return
        if asset not in self.assets or first_day < self.assets[asset]:
            self.assets[asset] = first_day


def _pricing_venue_of(venue: str):
    """The profile of `venue` and the venue that prices it; None for an
    unregistered venue or a binary one (prediction markets price themselves)."""
    try:
        profile = get_profile(venue)
    except ValueError:
        return None
    if profile.instrument is InstrumentKind.BINARY:
        return None
    return profile, profile.pricing_venue or venue


def _demand(db: Session, venue: str) -> _Demand:
    """Every asset held (now or ever since its anchor) by an account whose
    assets `venue` prices, and by followers of those accounts, with the first
    day a price is needed (the day before the anchor / allocation start)."""
    demand = _Demand()
    for account in db.scalars(
        select(VenueAccount).where(VenueAccount.status.not_in(INACTIVE_ACCOUNT_STATUSES))
    ):
        found = _pricing_venue_of(account.venue)
        if found is None or found[1] != venue:
            continue
        inputs = valuation_db.account_inputs(db, account)
        if inputs is None:
            continue
        first = aware(inputs.anchor_at).astimezone(UTC).date() - timedelta(days=1)
        for asset in inputs.assets:
            demand.want(asset, first)

    rows = db.execute(
        select(Allocation, VenueAccount.venue)
        .join(Strategy, Strategy.id == Allocation.strategy_id)
        .join(VenueAccount, VenueAccount.id == Strategy.venue_account_id)
    ).all()
    for allocation, account_venue in rows:
        found = _pricing_venue_of(account_venue)
        if found is None or found[1] != venue:
            continue
        profile = found[0]
        first = aware(allocation.started_at).astimezone(UTC).date() - timedelta(days=1)
        for (asset,) in db.execute(
            select(MirrorLedgerEntry.asset)
            .where(MirrorLedgerEntry.allocation_id == allocation.id)
            .distinct()
        ):
            demand.want(asset, first)
            equity = asset != CASH_ASSET and "equities" in profile.asset_classes
            if equity and (
                asset not in demand.equity_symbols or first < demand.equity_symbols[asset]
            ):
                demand.equity_symbols[asset] = first
    return demand


# ---- coverage -----------------------------------------------------------------------------


def _coverage_row(db: Session, source: str, kind: str, scope: str) -> MarketDataCoverage | None:
    return db.scalar(
        select(MarketDataCoverage).where(
            MarketDataCoverage.source == source,
            MarketDataCoverage.kind == kind,
            MarketDataCoverage.scope == scope,
        )
    )


def _price_hull(db: Session, instrument_id: int) -> tuple[date, date] | None:
    low, high = db.execute(
        select(func.min(PriceDaily.day), func.max(PriceDaily.day)).where(
            PriceDaily.instrument_id == instrument_id
        )
    ).one()
    return (low, high) if low is not None else None


def _closes_coverage(db: Session, inst: Instrument) -> tuple[date, date] | None:
    """The day range already fetched for `inst`: what the coverage row says,
    widened by any stored closes (rows seeded or imported another way)."""
    spans: list[tuple[date, date]] = []
    row = _coverage_row(db, inst.venue, KIND_CLOSES, inst.symbol)
    if row is not None and row.covered_from is not None and row.covered_through is not None:
        spans.append((row.covered_from, row.covered_through))
    hull = _price_hull(db, inst.id)
    if hull is not None:
        spans.append(hull)
    if not spans:
        return None
    return min(s[0] for s in spans), max(s[1] for s in spans)


def _record_closes_coverage(
    db: Session, inst: Instrument, low: date, high: date, today: date, now: datetime
) -> None:
    row = _coverage_row(db, inst.venue, KIND_CLOSES, inst.symbol)
    if row is None:
        db.add(
            MarketDataCoverage(
                source=inst.venue,
                kind=KIND_CLOSES,
                scope=inst.symbol,
                covered_from=low,
                covered_through=high,
                fetched_on=today,
                updated_at=now,
            )
        )
        return
    row.covered_from = min(row.covered_from, low) if row.covered_from else low
    row.covered_through = max(row.covered_through, high) if row.covered_through else high
    row.fetched_on = today
    row.updated_at = now


# ---- resolution: asset -> the instrument that prices it -----------------------------------


def _has_closes(db: Session, inst: Instrument) -> bool:
    return _closes_coverage(db, inst) is not None


def _resolve(db: Session, sources: _Sources, venue: str, asset: str, now: datetime):
    """The instrument whose closes value one unit of `asset`, or None.

    Order (each step stops the search): an instrument of `venue` the venue's
    own pricing rule picks from what is stored AND that already has closes; a
    stored CoinGecko instrument with closes; the venue's full list (one
    request) through the same rule; the curated CoinGecko map. Perps ("X-PERP")
    are priced only by the venue's own perp instrument: their price is the
    perp's, not the spot coin's. A stablecoin needs no price (par)."""
    today = now.astimezone(UTC).date()
    source = sources.source(venue)
    if asset in source.STABLECOINS_AT_PAR:
        return None
    stored = db.scalars(select(Instrument).where(Instrument.venue == venue)).all()

    if asset.endswith(PERP_SUFFIX):
        for inst in stored:
            if (
                inst.type == InstrumentType.PERP
                and inst.base_asset == asset
                and _has_closes(db, inst)
            ):
                return inst
        if _asked_unpriced(db, venue, asset, today):
            return None
        for listed in sources.listed(venue):
            if listed.type == InstrumentType.PERP and listed.base_asset == asset:
                return upsert_instrument(db, venue, listed, now)
        return _unpriced(db, venue, asset, today, now)

    symbol = source.pricing_symbol(asset, [_normalized(i) for i in stored])
    for inst in stored:
        if inst.symbol == symbol and _has_closes(db, inst):
            return inst
    fallback = db.scalars(
        select(Instrument).where(
            Instrument.venue == FALLBACK_SOURCE,
            Instrument.base_asset == asset,
            Instrument.type == InstrumentType.SPOT,
        )
    ).all()
    for inst in fallback:
        if _has_closes(db, inst):
            return inst

    if _asked_unpriced(db, venue, asset, today):
        return None
    full = sources.listed(venue)
    symbol = source.pricing_symbol(asset, full)
    if symbol is not None:
        listed = next((i for i in full if i.symbol == symbol), None)
        if listed is not None:
            return upsert_instrument(db, venue, listed, now)

    cg = sources.source(FALLBACK_SOURCE)
    if cg.pricing_symbol(asset) is None:
        return _unpriced(db, venue, asset, today, now)  # not in the curated map: no request
    synthetic = next((i for i in cg.fetch_instruments() if i.base_asset == asset), None)
    if synthetic is None:
        return _unpriced(db, venue, asset, today, now)
    return upsert_instrument(db, FALLBACK_SOURCE, synthetic, now)


def _asked_unpriced(db: Session, venue: str, asset: str, today: date) -> bool:
    """True when `asset` was already found to have no price source today (so a
    rerun does not list the venue's instruments again for it)."""
    row = _coverage_row(db, venue, KIND_UNPRICED, asset)
    return row is not None and row.fetched_on >= today


def _unpriced(db: Session, venue: str, asset: str, today: date, now: datetime) -> None:
    row = _coverage_row(db, venue, KIND_UNPRICED, asset)
    if row is None:
        db.add(
            MarketDataCoverage(
                source=venue, kind=KIND_UNPRICED, scope=asset, fetched_on=today, updated_at=now
            )
        )
    else:
        row.fetched_on = today
        row.updated_at = now
    return None


# ---- daily closes -------------------------------------------------------------------------


def _fetch_window(
    coverage: tuple[date, date] | None, first_day: date, yesterday: date
) -> tuple[date, date] | None:
    """The one day range to ask for: what is missing before the covered range
    (an older first_day) and after it (days up to yesterday). Both missing, or
    nothing covered yet: the whole span. None when nothing is missing."""
    if first_day > yesterday:
        return None
    if coverage is None:
        return first_day, yesterday
    cov_from, cov_through = coverage
    head = first_day < cov_from
    tail = cov_through < yesterday
    if head and tail:
        return first_day, yesterday
    if head:
        return first_day, cov_from - timedelta(days=1)
    if tail:
        return cov_through + timedelta(days=1), yesterday
    return None


def _fill_closes(
    db: Session,
    sources: _Sources,
    inst: Instrument,
    first_day: date,
    today: date,
    now: datetime,
) -> int:
    """Insert the closes of `inst` that are missing. Returns rows added."""
    yesterday = today - timedelta(days=1)
    window = _fetch_window(_closes_coverage(db, inst), first_day, yesterday)
    if window is None:
        return 0
    low, high = window
    source = sources.source(inst.venue)
    closes = sources.call(inst.venue, source.fetch_daily_closes, inst.symbol, low, high)
    have = set(
        db.scalars(
            select(PriceDaily.day).where(
                PriceDaily.instrument_id == inst.id, PriceDaily.day >= low, PriceDaily.day <= high
            )
        )
    )
    added = 0
    for close in sorted(closes, key=lambda c: c.day):
        if not low <= close.day <= high or close.day in have:
            continue
        try:
            usd = usd_micros_per_unit(close.close, inst.quote_asset)
        except (ValueError, ZeroDivisionError):
            logger.warning(
                "prices_daily: malformed close %r for %s %s", close.close, inst.venue, inst.symbol
            )
            continue
        if not _positive(close.close):
            continue
        db.add(
            PriceDaily(
                instrument_id=inst.id,
                day=close.day,
                close=close.close.strip(),
                usd_micros_per_unit=usd,
                source=inst.venue,
            )
        )
        have.add(close.day)
        added += 1
    _record_closes_coverage(db, inst, low, high, today, now)
    return added


def _refresh_margin_tiers(
    db: Session, sources: _Sources, inst: Instrument, today: date, now: datetime
) -> None:
    """Perp maintenance-margin tiers, at most once per UTC day. An empty
    answer is stored as [] ("asked, none published")."""
    if inst.type != InstrumentType.PERP:
        return
    if inst.margin_tiers_at is not None and aware(inst.margin_tiers_at).date() >= today:
        return
    tiers = sources.call(inst.venue, sources.source(inst.venue).fetch_margin_tiers, inst.symbol)
    inst.margin_tiers = [
        {"lower_bound": t.lower_bound, "max_leverage": t.max_leverage} for t in tiers
    ]
    inst.margin_tiers_at = now


# ---- corporate actions --------------------------------------------------------------------


def _slices(low: date, high: date, days: int) -> Iterable[tuple[date, date]]:
    start = low
    while start <= high:
        end = min(high, start + timedelta(days=days - 1))
        yield start, end
        start = end + timedelta(days=1)


def _corporate_actions(
    db: Session,
    sources: _Sources,
    venue: str,
    symbols: dict[str, date],
    today: date,
    now: datetime,
) -> int:
    """Store splits, dividends and symbol changes for follower-held equities.
    Once per symbol per UTC day. Returns rows added."""
    if not symbols:
        return 0
    due: dict[str, date] = {}
    for symbol, first_day in symbols.items():
        row = _coverage_row(db, venue, KIND_CORPORATE_ACTIONS, symbol)
        if row is not None and row.fetched_on >= today:
            continue
        due[symbol] = (
            first_day
            if row is None
            else row.fetched_on - timedelta(days=CORPORATE_ACTION_REFETCH_DAYS)
        )
    if not due:
        return 0
    source = sources.source(venue)
    high = today + timedelta(days=CORPORATE_ACTION_LOOKAHEAD_DAYS)
    added = 0
    ordered = sorted(due)
    for i in range(0, len(ordered), CORPORATE_ACTION_SYMBOLS_PER_CALL):
        chunk = ordered[i : i + CORPORATE_ACTION_SYMBOLS_PER_CALL]
        low = min(due[s] for s in chunk)
        wanted = set(chunk)
        for start, end in _slices(low, high, CORPORATE_ACTION_WINDOW_DAYS):
            actions = sources.call(venue, source.fetch_corporate_actions, start, end, symbols=chunk)
            added += _store_actions(db, venue, wanted, actions)
        for symbol in chunk:
            row = _coverage_row(db, venue, KIND_CORPORATE_ACTIONS, symbol)
            if row is None:
                db.add(
                    MarketDataCoverage(
                        source=venue,
                        kind=KIND_CORPORATE_ACTIONS,
                        scope=symbol,
                        fetched_on=today,
                        updated_at=now,
                    )
                )
            else:
                row.fetched_on = today
                row.updated_at = now
        db.flush()
    return added


def _store_actions(db: Session, source_name: str, wanted: set[str], actions) -> int:
    added = 0
    for action in actions:
        if action.symbol not in wanted:
            continue
        existing = db.scalar(
            select(CorporateActionRow).where(
                CorporateActionRow.source == source_name,
                CorporateActionRow.symbol == action.symbol,
                CorporateActionRow.kind == action.kind.value,
                CorporateActionRow.ex_date == action.ex_date,
            )
        )
        if existing is None:
            db.add(
                CorporateActionRow(
                    source=source_name,
                    symbol=action.symbol,
                    kind=action.kind.value,
                    ex_date=action.ex_date,
                    pay_date=action.pay_date,
                    ratio_new=action.ratio_new,
                    ratio_old=action.ratio_old,
                    cash_per_share=action.cash_per_share,
                    new_symbol=action.new_symbol,
                )
            )
            db.flush()
            added += 1
        elif existing.pay_date is None and action.pay_date is not None:
            existing.pay_date = action.pay_date  # announced after the ex-date was known
    return added


# ---- the nightly job ----------------------------------------------------------------------


def run_daily(ctx) -> None:
    """PRICES_DAILY for one pricing venue (see the module docstring)."""
    venue = (ctx.job.payload or {}).get("venue")
    if not venue:
        raise ValueError("prices_daily job has no venue in its payload")
    found = _pricing_venue_of(venue)
    if found is None:
        logger.warning("prices_daily: %r is not a priced asset venue; nothing to do", venue)
        return
    profile = found[0]
    db: Session = ctx.db
    now = aware(ctx.clock())
    today = now.astimezone(UTC).date()
    sources = _Sources(ctx.limiter)

    demand = _demand(db, venue)
    failures = 0
    added = 0
    try:
        if demand.assets:
            targets: dict[int, tuple[Instrument, date]] = {}
            for asset, first_day in sorted(demand.assets.items()):
                try:
                    inst = _resolve(db, sources, venue, asset, now)
                except (BudgetExhausted, RateLimited, SQLAlchemyError):
                    raise
                except Exception:
                    logger.exception("prices_daily: cannot resolve %s on %s", asset, venue)
                    failures += 1
                    continue
                if inst is None:
                    logger.info("prices_daily: %s has no price source; left unpriced", asset)
                    continue
                known = targets.get(inst.id)
                targets[inst.id] = (inst, min(first_day, known[1]) if known else first_day)
            for inst, first_day in targets.values():
                try:
                    _refresh_margin_tiers(db, sources, inst, today, now)
                    added += _fill_closes(db, sources, inst, first_day, today, now)
                except (BudgetExhausted, RateLimited, SQLAlchemyError):
                    raise
                except Exception:
                    logger.exception("prices_daily: %s %s failed", inst.venue, inst.symbol)
                    failures += 1
        if demand.equity_symbols and "equities" in profile.asset_classes:
            try:
                _corporate_actions(db, sources, venue, demand.equity_symbols, today, now)
            except (BudgetExhausted, RateLimited, SQLAlchemyError):
                raise
            except Exception:
                logger.exception("prices_daily: corporate actions failed for %s", venue)
                failures += 1
    except (BudgetExhausted, RateLimited) as exc:
        db.commit()  # keep what was fetched; the missing days are the cursor
        raise Defer(
            max(MIN_DEFER_SECONDS, exc.retry_after_seconds), f"market data budget: {exc}"
        ) from exc
    db.commit()
    logger.info("prices_daily %s: +%d closes, %d failures", venue, added, failures)
    if failures:
        raise PricesDailyError(f"prices_daily {venue}: {failures} asset(s) failed")


# ---- quotes and marks ---------------------------------------------------------------------


def _capture(db: Session, source_venue: str, source, symbol: str, instrument: Instrument):
    """Fetch and store one quote; raises BudgetExhausted / RateLimited."""
    quote = source.fetch_quote(symbol)
    if quote is None:
        return None
    bid, ask, last = (
        (v.strip() if _positive(v) else None) for v in (quote.bid, quote.ask, quote.last)
    )
    if bid is None and ask is None and last is None:
        return None
    row = AssetQuote(
        instrument_id=instrument.id,
        bid=bid,
        ask=ask,
        last=last,
        as_of=aware(quote.as_of),
        source=source_venue,
    )
    db.add(row)
    db.flush()
    return row


def _quote_source(venue: str, instrument: Instrument, venue_env: str, limiter):
    """(source venue, source, symbol) that quotes `instrument` held on `venue`:
    the profile's pricing_venue when it has one (IBKR/SnapTrade -> alpaca)."""
    profile = get_profile(venue)
    source_venue = profile.pricing_venue or venue
    env = venue_env if source_venue == venue else _market_env(source_venue)
    source = build_market_data(source_venue, env)
    if limiter is not None:
        source.set_throttle(limiter.throttle(source_venue, None))
    symbol = instrument.symbol if source_venue == instrument.venue else instrument.base_asset
    return source_venue, source, symbol


def capture_quote(
    db: Session, venue: str, venue_env: str, instrument: Instrument, limiter
) -> AssetQuote | None:
    """One quote of `instrument` now, stored in asset_quotes and returned.
    None when the source has no quote, the budget is spent, the venue told us
    to back off or its circuit is open; any other failure is logged and also
    gives None (a missing quote makes the caller fall back, it must not make
    signal emission fail)."""
    source_venue = venue
    try:
        source_venue, source, symbol = _quote_source(venue, instrument, venue_env, limiter)
        if limiter is not None and limiter.breaker_open_until(source_venue) is not None:
            return None
        return _capture(db, source_venue, source, symbol, instrument)
    except BudgetExhausted:
        return None
    except RateLimited as exc:
        if limiter is not None:
            limiter.on_rate_limited(source_venue, None, exc.retry_after_seconds, exc.endpoint)
        return None
    except SQLAlchemyError:
        raise
    except Exception:
        logger.warning("quote capture failed for %s %s", venue, instrument.symbol, exc_info=True)
        return None


def latest_mark(db: Session, instrument_id: int, now: datetime) -> Mark | None:
    """The freshest usable quote no older than quote_max_age_seconds: the mid
    of bid and ask when both are quoted, else the last price. A quote with
    neither is skipped. None when nothing fresh exists."""
    now = aware(now)
    oldest = now - timedelta(seconds=get_settings().quote_max_age_seconds)
    rows = db.scalars(
        select(AssetQuote)
        .where(AssetQuote.instrument_id == instrument_id, AssetQuote.as_of >= oldest)
        .order_by(AssetQuote.as_of.desc(), AssetQuote.id.desc())
        .limit(10)
    ).all()
    for row in rows:
        if aware(row.as_of) < oldest:  # SQLite compares naive values; check again aware
            continue
        if _positive(row.bid) and _positive(row.ask):
            with localcontext() as ctx:
                ctx.prec = 80
                mid = (Decimal(row.bid) + Decimal(row.ask)) / 2
            return Mark(instrument_id, format(mid, "f"), aware(row.as_of), row.source, "mid")
        if _positive(row.last):
            return Mark(instrument_id, row.last.strip(), aware(row.as_of), row.source, "last")
    return None


def _open_instruments(db: Session) -> list[Instrument]:
    """Instruments some ACTIVE allocation holds a non-zero amount of (the
    ledger's net legs, so a position closed by a split or a sell is not open)."""
    held = db.execute(
        select(
            MirrorLedgerEntry.allocation_id,
            MirrorLedgerEntry.asset,
            func.sum(MirrorLedgerEntry.delta_atoms),
        )
        .join(Allocation, Allocation.id == MirrorLedgerEntry.allocation_id)
        .where(Allocation.status == AllocationStatus.ACTIVE, MirrorLedgerEntry.asset != CASH_ASSET)
        .group_by(MirrorLedgerEntry.allocation_id, MirrorLedgerEntry.asset)
    ).all()
    open_pairs = {(alloc, asset) for alloc, asset, total in held if total}
    if not open_pairs:
        return []
    ids = {
        inst_id
        for alloc, inst_id, base in db.execute(
            select(MirrorAssetFill.allocation_id, Instrument.id, Instrument.base_asset)
            .join(Instrument, Instrument.id == MirrorAssetFill.instrument_id)
            .where(MirrorAssetFill.allocation_id.in_({a for a, _ in open_pairs}))
            .distinct()
        )
        if (alloc, base) in open_pairs
    }
    if not ids:
        return []
    return list(
        db.scalars(select(Instrument).where(Instrument.id.in_(ids)).order_by(Instrument.id))
    )


def _earliest_fill(db: Session, instrument_id: int) -> datetime | None:
    first = db.scalar(
        select(func.min(MirrorAssetFill.filled_at))
        .join(Allocation, Allocation.id == MirrorAssetFill.allocation_id)
        .where(
            MirrorAssetFill.instrument_id == instrument_id,
            Allocation.status == AllocationStatus.ACTIVE,
        )
    )
    return aware(first) if first is not None else None


def _refresh_funding(db: Session, source, inst: Instrument, now: datetime) -> None:
    last = db.scalar(
        select(func.max(FundingRateRow.at)).where(FundingRateRow.instrument_id == inst.id)
    )
    if last is not None:
        start = aware(last) + timedelta(milliseconds=1)
    else:
        start = _earliest_fill(db, inst.id) or (now - FUNDING_LOOKBACK)
    if start > now:
        return
    rates = source.fetch_funding_history(inst.symbol, start, now)
    if not rates:
        return
    have = {
        aware(at)
        for (at,) in db.execute(
            select(FundingRateRow.at).where(
                FundingRateRow.instrument_id == inst.id,
                FundingRateRow.at >= min(aware(r.at) for r in rates),
            )
        )
    }
    for rate in rates:
        at = aware(rate.at)
        if at in have:
            continue
        have.add(at)
        db.add(
            FundingRateRow(
                instrument_id=inst.id,
                at=at,
                rate=rate.rate,
                mark_price=rate.mark_price,
                source=inst.venue,
            )
        )


def refresh_asset_marks(db: Session, limiter) -> int:
    """Quotes (and, for perps, funding rates) of every instrument an active
    follower book holds. A source that is out of budget, rate limited or
    circuit-open is skipped for the rest of the call (the next tick retries).
    Returns the number of quotes written."""
    open_instruments = _open_instruments(db)
    if not open_instruments:
        return 0
    now = utcnow()
    written = 0
    blocked: set[str] = set()
    for inst in open_instruments:
        try:
            source_venue, source, symbol = _quote_source(
                inst.venue, inst, _market_env(inst.venue), limiter
            )
        except ValueError:
            logger.warning("asset marks: venue %r of %s is not registered", inst.venue, inst.symbol)
            continue
        if source_venue in blocked:
            continue
        if limiter is not None and limiter.breaker_open_until(source_venue) is not None:
            blocked.add(source_venue)
            continue
        try:
            if _capture(db, source_venue, source, symbol, inst) is not None:
                written += 1
            if inst.type == InstrumentType.PERP:
                _refresh_funding(db, source, inst, now)
        except BudgetExhausted:
            blocked.add(source_venue)
        except RateLimited as exc:
            blocked.add(source_venue)
            if limiter is not None:
                limiter.on_rate_limited(source_venue, None, exc.retry_after_seconds, exc.endpoint)
        except SQLAlchemyError:
            raise
        except Exception:
            logger.warning(
                "asset mark refresh failed for %s %s", inst.venue, inst.symbol, exc_info=True
            )
    db.commit()
    return written

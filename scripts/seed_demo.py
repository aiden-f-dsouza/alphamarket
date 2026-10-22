"""Seed a DEMO database by running synthetic venue data through the real
pipeline (w3-seed). docs/demo.md is the operator's guide.

    DATABASE_URL=postgresql+psycopg://alphamarket:alphamarket@localhost:5432/alphamarket_demo \\
    DEMO_MODE=true VAULT_KEY=<fernet key> uv run python scripts/seed_demo.py --reset

What is synthetic and what is real:

- SYNTHETIC: the venues. A seeded RNG draws price paths (crypto, perps,
  US equities with a split and dividends, Kalshi markets) and each creator's
  trading on them, as the venue would report it: NormalizedAssetFill /
  NormalizedEvent / NormalizedFill rows with exact legs, balance vectors,
  quotes, daily closes, funding rates, margin tiers and corporate actions.
  They are served by fake connectors and market data sources installed at
  the registry seam (`registry._BUILDERS` / `_MARKET_DATA_BUILDERS`, the
  tables behind `build_connector` / `build_market_data`) for this process
  only. Every outbound httpx request is refused while seeding (invariant 6:
  no live venue calls), so a missing fake fails loudly instead of calling out.
- REAL: everything after the connector. Accounts get a vault-sealed
  credential and are read through `ingest.connector_for`; the real JobRunner
  runs the real handlers (backfill/sync page streams, signals v2 with quote
  capture, the paper engine and its lifecycle (funding, splits, dividends,
  drawdown stops), prices_daily, reconcile, stats v1/v2, scope re-checks).
  Followers tail through `tails.create_allocation`. The clock is moved with
  time-machine from each creator's connect date to today, so syncs, copy lag,
  nightly jobs and portfolio snapshots happen at the times they would have.

Every user is `is_demo=True` (invariant 7): their rows are public only with
DEMO_MODE on, under the "Sample data" banner, and scripts/preflight.py refuses
a database that holds any.

Safety: refuses unless DEMO_MODE is on, the database name contains "demo",
and the database holds no non-demo user. `--reset` drops and recreates the
public schema (append-only triggers make row deletes impossible by design),
then migrates to head: use a dedicated database, e.g. alphamarket_demo.
"""

import argparse
import hashlib
import logging
import math
import random
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP, Decimal
from fractions import Fraction
from pathlib import Path
from statistics import NormalDist

import httpx
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from alphamarket import (  # noqa: F401  (worker registers the job handlers)
    credentials,
    jobs,
    tails,
    worker,
)
from alphamarket.auth import hash_password
from alphamarket.config import get_settings
from alphamarket.connectors import registry
from alphamarket.connectors.base import (
    AssetAmount,
    AssetMarketData,
    AssetVenueConnector,
    CorporateAction,
    CorporateActionKind,
    DailyClose,
    FundingRate,
    InstrumentType,
    LegKind,
    MarginTier,
    MarketDataSource,
    NormalizedAssetFill,
    NormalizedBalance,
    NormalizedBalances,
    NormalizedEvent,
    NormalizedFill,
    NormalizedInstrument,
    NormalizedLeg,
    NormalizedMarket,
    NormalizedPosition,
    NormalizedSettlement,
    Page,
    Quote,
    ReadOnlyEvidence,
    ReadOnlyProof,
    VenueConnector,
)
from alphamarket.jobs import JobKind, JobRunner, enqueue
from alphamarket.models import (
    AnchorBalance,
    Follow,
    Job,
    JobStatus,
    Strategy,
    StrategyKind,
    User,
    VenueAccount,
    VenueAccountStatus,
    Watch,
)
from alphamarket.scopecheck import apply_evidence
from alphamarket.venues import slugify
from alphamarket.watchlist import UNUSABLE_PASSWORD_HASH, short_address

logger = logging.getLogger("seed_demo")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SEED = 127  # picked from a sweep: winners, one deep loser, one flat, one stopped tail
DEFAULT_PASSWORD = "koine-demo-2026"
EMAIL_DOMAIN = "demo.koine"
M = 1_000_000  # micros per dollar

# ---- scale -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Scale:
    """How much history to simulate. `full` is the accelerator demo; `small`
    is the test (every creator gets `min_history` days)."""

    name: str
    history_factor: float  # x each creator's configured history days
    min_history: int
    copy_days: int  # investors' paper allocations start this many days before today
    prices_every_hours: int  # the PRICES tick (quotes, funding) while followers exist
    reconcile_every_days: int  # nightly reconciles before the copy window
    sync_every_days: int  # creator syncs before the copy window (stale signals anyway)


FULL = Scale("full", 1.0, 30, 60, 8, 7, 3)
SMALL = Scale("small", 0.0, 10, 3, 8, 3, 1)
SCALES = {"full": FULL, "small": SMALL}


# ---- small exact helpers ------------------------------------------------------------------


def q(value: Decimal | Fraction | float | str, dp: int, rounding=ROUND_HALF_EVEN) -> Decimal:
    """`value` as a Decimal with exactly `dp` places."""
    if isinstance(value, Fraction):
        value = Decimal(value.numerator) / Decimal(value.denominator)
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-dp), rounding=rounding)


def atoms_of(value: Decimal | Fraction, dp: int, rounding=ROUND_HALF_EVEN) -> int:
    """Integer atoms of `value` at `dp` decimals (the venue's own rounding)."""
    return int(q(value, dp, rounding).scaleb(dp))


def units(atoms: int, dp: int) -> Fraction:
    return Fraction(atoms, 10**dp)


def sig5(value: Decimal) -> Decimal:
    """Hyperliquid prices: five significant figures."""
    if value <= 0:
        return value
    exponent = value.adjusted()
    return q(value, max(0, 4 - exponent))


def floor_hour(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def stable_hash(*parts: object) -> int:
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big")


# ---- markets: price paths -----------------------------------------------------------------

EQUITY_OPEN = timedelta(hours=14, minutes=35)  # ~09:35 New York, ignoring DST
EQUITY_CLOSE = timedelta(hours=20, minutes=55)


@dataclass(frozen=True)
class Underlying:
    """One traded name. Levels are roughly where each traded in late 2025 (the
    demo is illustrative, not a price history); vols are typical daily ones."""

    key: str
    start: float  # split-adjusted price at the first simulated day
    vol: float  # daily volatility
    beta: float  # loading on its market's common daily shock (crypto or US equities)
    trend: float  # x its market's drift over the window (high beta moves further)
    idio: float  # +- its own drift over the window, in log terms
    equity: bool = False
    dp: int = 2  # decimals of a quoted price
    split: tuple[int, int, int] | None = None  # (days before the end, new, old)


UNDERLYINGS = (
    Underlying("BTC", 94_500, 0.027, 0.92, 1.0, 0.04, dp=1),
    Underlying("ETH", 3_280, 0.037, 0.90, 1.25, 0.08),
    Underlying("SOL", 178, 0.047, 0.82, 1.5, 0.12),
    Underlying("NVDA", 182, 0.022, 0.68, 1.5, 0.12, equity=True),
    Underlying("AAPL", 272, 0.015, 0.70, 1.0, 0.07, equity=True),
    Underlying("MSFT", 485, 0.014, 0.70, 1.0, 0.07, equity=True),
    # The split is fictional; the 4-for-1 puts the post-split price where
    # AVGO actually trades, so current holdings and recent fills look right.
    Underlying("AVGO", 345, 0.021, 0.65, 1.4, 0.12, equity=True, split=(32, 4, 1)),
    Underlying("META", 640, 0.019, 0.62, 1.2, 0.10, equity=True),
    Underlying("AMZN", 228, 0.018, 0.66, 1.1, 0.09, equity=True),
    Underlying("VTI", 331, 0.009, 0.97, 1.0, 0.02, equity=True),
    Underlying("SCHD", 27.4, 0.008, 0.80, 0.6, 0.04, equity=True),
    Underlying("JNJ", 204, 0.010, 0.40, 0.3, 0.06, equity=True),
    Underlying("PG", 148, 0.009, 0.40, 0.3, 0.06, equity=True),
    Underlying("KO", 70, 0.009, 0.40, 0.3, 0.06, equity=True),
)

# Each market's drift over TREND_DAYS (log terms): crypto anywhere from a
# bleed to a rally, equities a flat-to-good year.
MARKET_DRIFT = {"crypto": (-0.2, 0.3), "equity": (-0.04, 0.16)}
TREND_DAYS = 300
REVERSION = 0.03  # daily pull of a log price toward its trend line
# A path stays within about 0.55x-1.7x of its start: past either bound the
# pull is much stronger, so nine months never ends at an absurd level.
SOFT_BOUNDS = (math.log(0.58), math.log(1.6))
BOUND_PULL = 0.25


def draw_closes(first: date, last: date, rng: random.Random) -> dict[str, dict[date, float]]:
    """Split-adjusted daily closes of every underlying from `first` - 1 day to
    `last`: a log price that follows its trend line with mean reversion, and
    daily shocks built from a common factor per market (so BTC, ETH and SOL
    move together, and so do the equities) plus the name's own noise.
    Equities do not move on weekends."""
    days = [first - timedelta(days=1)]
    while days[-1] < last:
        days.append(days[-1] + timedelta(days=1))
    drift = {m: rng.uniform(lo, hi) for m, (lo, hi) in MARKET_DRIFT.items()}
    common = {m: [rng.gauss(0, 1) for _ in days] for m in MARKET_DRIFT}
    out: dict[str, dict[date, float]] = {}
    lo, hi = SOFT_BOUNDS
    for u in UNDERLYINGS:
        market = "equity" if u.equity else "crypto"
        goal = u.trend * drift[market] + rng.uniform(-u.idio, u.idio)
        goal = min(hi - 0.1, max(lo + 0.1, goal))
        own = math.sqrt(1 - u.beta * u.beta)
        x = 0.0
        adj = {days[0]: u.start}
        for i, d in enumerate(days[1:], start=1):
            shock = u.beta * common[market][i] + own * rng.gauss(0, 1)
            if not (u.equity and d.weekday() >= 5):
                target = goal * min(1.0, i / TREND_DAYS)
                pull = REVERSION * (target - x)
                if x < lo:
                    pull += BOUND_PULL * (lo - x)
                elif x > hi:
                    pull += BOUND_PULL * (hi - x)
                x += pull + u.vol * shock
            adj[d] = u.start * math.exp(x)
        out[u.key] = adj
    return out


class PricePath:
    """Split-adjusted daily closes (from draw_closes), plus a deterministic
    intraday path between them. Raw (unadjusted) prices are the adjusted ones
    x the split ratio before the ex-date, like Alpaca's raw bars: the ledger
    applies splits as SPLIT legs, so prices must be unadjusted."""

    def __init__(self, u: Underlying, adj: dict[date, float], end: date):
        self.u = u
        self.ex_date: date | None = None
        self.ratio = Fraction(1)
        if u.split is not None:
            back, new, old = u.split
            self.ex_date = end - timedelta(days=back)
            while self.ex_date.weekday() >= 5:
                self.ex_date -= timedelta(days=1)
            self.ratio = Fraction(new, old)
        self.adj = adj
        self.phase = (stable_hash(u.key) % 1000) / 1000 * 2 * math.pi

    def _factor(self, d: date) -> float:
        return float(self.ratio) if self.ex_date is not None and d < self.ex_date else 1.0

    def _adj_at(self, t: datetime) -> float:
        d = t.date()
        prev = self.adj.get(d - timedelta(days=1), self.u.start)
        cur = self.adj.get(d, prev)
        seconds = (t - midnight(d)).total_seconds()
        if self.u.equity:
            span = (EQUITY_CLOSE - EQUITY_OPEN).total_seconds()
            f = min(1.0, max(0.0, (seconds - EQUITY_OPEN.total_seconds()) / span))
        else:
            f = seconds / 86_400
        base = prev * (cur / prev) ** f
        wiggle = 1 + 0.35 * self.u.vol * math.sin(self.phase + seconds / 5_400) * 4 * f * (1 - f)
        return base * wiggle

    def price(self, t: datetime) -> Decimal:
        """The traded price at `t`, in quote currency, at the quoting precision."""
        return q(self._adj_at(t) * self._factor(t.date()), self.u.dp)

    def close(self, d: date) -> Decimal:
        return q(self.adj[d] * self._factor(d), self.u.dp)

    def has_bar(self, d: date) -> bool:
        return d in self.adj and not (self.u.equity and d.weekday() >= 5)


# ---- venue truth: what each fake venue reports ----------------------------------------------


@dataclass
class Visible:
    """A venue row and the moment the venue starts reporting it (daily-
    resolution venues publish a day's activity the next morning)."""

    at: datetime
    visible_at: datetime
    item: object  # NormalizedAssetFill | NormalizedEvent | NormalizedFill | NormalizedSettlement


@dataclass
class Book:
    """One creator's venue account as the venue sees it."""

    venue: str
    key_id: str
    venue_env: str
    cash_asset: str
    cash_dp: int
    anchor_at: datetime
    anchor_cash_atoms: int
    daily: bool = False
    fills: list[Visible] = field(default_factory=list)
    events: list[Visible] = field(default_factory=list)
    settlements: list[Visible] = field(default_factory=list)  # binary
    entry_prices: dict[str, list[tuple[datetime, Decimal]]] = field(default_factory=dict)
    pricer: Callable[[str, datetime], Decimal] | None = None


@dataclass(frozen=True)
class CreatorSpec:
    handle: str
    display_name: str
    venue: str
    strategy: str
    description: str
    tags: str
    history_days: int
    cash: int  # dollars at the anchor
    kind: str  # spot | perp | binary
    symbols: tuple[tuple[str, float], ...] = ()  # (underlying, weight)
    skill: float = 0.55  # chance a discretionary trade is on the right side
    trades_per_week: float = 2.0
    hold_hours: tuple[float, float] = (24, 120)
    frac: tuple[float, float] = (0.15, 0.3)  # of equity per trade (perps: notional)
    fee_bps: float = 0.0
    fee_min_usd: float = 0.0  # IBKR's per-order minimum
    whole_shares: bool = False
    core: tuple[tuple[str, int, int | None, float], ...] = ()  # (sym, from day, to day, frac)
    deposits: tuple[tuple[int, int], ...] = ()  # (day, dollars)
    edge: float = 0.0  # binary: the gap between its own probability and the price to trade
    insight: float = 0.1  # binary: how informative its own signal is (0 none, 1 perfect)
    markets_per_day: float = 1.0
    house: bool = False  # the user is a Koine-run account (User.is_house)
    watch: bool = False  # no creator: a watched address owned by the system user
    key_id: str = ""  # a fixed venue key / address instead of a generated one


# A watched address is obviously synthetic: "de30" repeated, never a real person's.
WATCH_ADDRESS = "0x" + "de30" * 10
SYSTEM_LOCAL = "system"  # system@demo.koine owns the watch accounts

CREATORS = (
    CreatorSpec(
        handle="northwind",
        display_name="northwind",
        venue="kraken",
        strategy="BTC/ETH swing",
        description=(
            "Multi-day swings in BTC and ETH on Kraken spot. Enters on pullbacks inside a "
            "trend, scales out into strength. No leverage, no alts."
        ),
        tags="crypto,swing,trend",
        history_days=270,
        cash=25_000,
        kind="spot",
        symbols=(("BTC", 0.6), ("ETH", 0.4)),
        skill=0.65,
        trades_per_week=2.2,
        hold_hours=(36, 216),
        frac=(0.25, 0.45),
        fee_bps=25,
        deposits=((120, 10_000),),
    ),
    CreatorSpec(
        handle="tidewater",
        display_name="tidewater",
        venue="hyperliquid",
        strategy="Perp momentum: BTC, ETH, SOL",
        description=(
            "Directional perps on Hyperliquid, long or short, 1-2x notional. Holds hours to "
            "days; pays or earns funding while in a position."
        ),
        tags="crypto,perps,momentum",
        history_days=210,
        cash=40_000,
        kind="perp",
        symbols=(("BTC", 0.5), ("ETH", 0.3), ("SOL", 0.2)),
        skill=0.61,
        trades_per_week=4.0,
        hold_hours=(6, 96),
        frac=(0.5, 1.3),
        fee_bps=4.5,
    ),
    CreatorSpec(
        handle="lantern",
        display_name="lantern",
        venue="coinbase",
        strategy="High-beta alt rotation",
        description=(
            "Rotates between SOL and ETH on Coinbase, chasing relative strength. Concentrated "
            "and fast; the record shows what that costs in a choppy tape."
        ),
        tags="crypto,rotation,high-beta",
        history_days=150,
        cash=12_000,
        kind="spot",
        symbols=(("SOL", 0.6), ("ETH", 0.4)),
        skill=0.36,
        trades_per_week=3.0,
        hold_hours=(18, 144),
        frac=(0.4, 0.65),
        fee_bps=60,
    ),
    CreatorSpec(
        handle="quarry",
        display_name="quarry",
        venue="binanceus",
        strategy="BTC accumulation bands",
        description=(
            "Buys BTC in tranches below its 20-day band and trims above it. New on Koine: "
            "still incubating."
        ),
        tags="crypto,accumulation",
        history_days=45,
        cash=5_000,
        kind="spot",
        symbols=(("BTC", 1.0),),
        skill=0.56,
        trades_per_week=3.0,
        hold_hours=(48, 240),
        frac=(0.15, 0.3),
        fee_bps=2,
    ),
    CreatorSpec(
        handle="meridian",
        display_name="meridian",
        venue="alpaca",
        strategy="US mega-cap momentum",
        description=(
            "Long-only momentum across the largest US tech names, whole shares, held days to "
            "weeks. Core MSFT and AVGO positions ride through dividends and splits."
        ),
        tags="equities,momentum,large-cap",
        history_days=240,
        cash=60_000,
        kind="spot",
        symbols=(("NVDA", 0.3), ("META", 0.2), ("AAPL", 0.2), ("AMZN", 0.15), ("MSFT", 0.15)),
        skill=0.63,
        trades_per_week=2.5,
        hold_hours=(48, 480),
        frac=(0.1, 0.2),
        whole_shares=True,
        # Core holdings; the late adds (days before the end) are bought while
        # investors tail, so their paper books go through the AVGO split and
        # the MSFT and AVGO dividends too.
        core=(
            ("MSFT", 2, None, 0.15),
            ("AVGO", 20, None, 0.12),
            ("MSFT", -50, None, 0.06),
            ("AVGO", -45, None, 0.08),
        ),
    ),
    CreatorSpec(
        handle="harbor",
        display_name="harbor",
        venue="ibkr",
        strategy="Dividend growth core",
        description=(
            "A low-turnover income book at Interactive Brokers: broad-market ETF core plus "
            "consumer staples and health care, rebalanced a few times a month."
        ),
        tags="equities,dividends,low-vol",
        history_days=300,
        cash=120_000,
        kind="spot",
        symbols=(("JNJ", 0.3), ("PG", 0.3), ("KO", 0.4)),
        skill=0.57,
        trades_per_week=1.0,
        hold_hours=(240, 1_200),
        frac=(0.06, 0.12),
        fee_bps=0,
        fee_min_usd=1.0,
        whole_shares=True,
        core=(("VTI", 1, None, 0.3), ("SCHD", 3, None, 0.18), ("VTI", -50, None, 0.08)),
    ),
    CreatorSpec(
        handle="isobar",
        display_name="isobar",
        venue="kalshi",
        strategy="NYC & Chicago daily highs",
        description=(
            "Daily high-temperature brackets for New York and Chicago, priced off the "
            "overnight model runs. Small stakes, many markets, held to settlement."
        ),
        tags="weather,daily",
        history_days=240,
        cash=8_000,
        kind="binary",
        edge=0.03,
        insight=0.035,
        markets_per_day=1.3,
        frac=(0.014, 0.02),
    ),
    CreatorSpec(
        handle="fedwatch",
        display_name="fedwatch",
        venue="kalshi",
        strategy="CPI & Fed path",
        description=(
            "Monthly CPI prints and the next Fed decision, sized to the gap between the "
            "market and the nowcast. Young record: incubating."
        ),
        tags="macro,econ",
        history_days=75,
        cash=5_000,
        kind="binary",
        edge=0.02,
        insight=0.2,
        markets_per_day=0.6,
        frac=(0.025, 0.035),
    ),
    # Run by Koine: an `is_house` user, so its strategy carries the "Run by
    # Koine" badge. Same pipeline and the same ranking rules as everyone else.
    CreatorSpec(
        handle="koinelab",
        display_name="Koine Lab",
        venue="kraken",
        strategy="Systematic BTC/ETH trend filter",
        description=(
            "Run by Koine. A rules-based trend filter on BTC and ETH spot at Kraken: long "
            "when price holds above its medium-term average, flat otherwise. No leverage, "
            "and every rule is fixed in advance."
        ),
        tags="crypto,systematic,trend",
        history_days=180,
        cash=20_000,
        kind="spot",
        symbols=(("BTC", 0.6), ("ETH", 0.4)),
        skill=0.6,
        trades_per_week=1.6,
        hold_hours=(72, 288),
        frac=(0.25, 0.4),
        fee_bps=25,
        house=True,
    ),
    # Not a creator: a public Hyperliquid address watched privately (the
    # watchlist). Its venue account belongs to the demo system user, is never
    # claimed, and its strategy has kind "watch" and is never public.
    CreatorSpec(
        handle="whale",
        display_name="Hyperliquid whale",
        venue="hyperliquid",
        strategy="Watched perp address",
        description="",
        tags="",
        history_days=200,
        cash=150_000,
        kind="perp",
        symbols=(("BTC", 0.55), ("ETH", 0.3), ("SOL", 0.15)),
        skill=0.6,
        trades_per_week=3.0,
        hold_hours=(8, 120),
        frac=(0.6, 1.4),
        fee_bps=4.5,
        watch=True,
        key_id=WATCH_ADDRESS,
    ),
    # On-chain perp venues (docs/onchain_perps_plan.md S8): each venue branch
    # adds its one watched synthetic address inside its own marked block, a
    # CreatorSpec with watch=True like "whale" above.
    # ---- venue-lighter ----
    # ---- end venue-lighter ----
    # ---- venue-pacifica ----
    # ---- end venue-pacifica ----
    # ---- venue-gmx_arbitrum ----
    # ---- end venue-gmx_arbitrum ----
    # ---- venue-nado ----
    # ---- end venue-nado ----
)

# (investor email local part, strategy handle, dollars, drawdown limit bps, days before the end)
ALLOCATIONS = (
    ("investor", "northwind", 2_500, 2_000, 60),
    ("investor", "meridian", 3_000, 1_500, 58),
    ("investor", "isobar", 1_000, 2_500, 57),
    ("investor", "tidewater", 2_000, 2_500, 55),
    ("investor", "lantern", 1_500, 1_000, 50),
    ("analyst", "harbor", 5_000, 1_000, 58),
    ("analyst", "northwind", 1_000, 1_500, 40),
    ("investor", "whale", 2_000, 2_000, 52),  # the paper tail of a watched address
)
# (investor, watched handle, private label): each user names a watch themselves.
WATCHES = (
    ("investor", "whale", "Funding-aware perp whale"),
    ("analyst", "whale", "BTC momentum book to study"),
    # ---- venue-lighter ----
    # ---- end venue-lighter ----
    # ---- venue-pacifica ----
    # ---- end venue-pacifica ----
    # ---- venue-gmx_arbitrum ----
    # ---- end venue-gmx_arbitrum ----
    # ---- venue-nado ----
    # ---- end venue-nado ----
)
FOLLOWS = (
    ("investor", ("northwind", "tidewater", "meridian", "isobar", "lantern", "fedwatch")),
    ("analyst", ("harbor", "northwind", "quarry", "fedwatch")),
)
INVESTORS = (("investor", "Demo Investor"), ("analyst", "Demo Analyst"))


# ---- venue conventions (instrument symbols, decimals) ---------------------------------------

SPOT_SYMBOLS = {
    "kraken": {"BTC": "XXBTZUSD", "ETH": "XETHZUSD", "SOL": "SOLUSD"},
    "coinbase": {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"},
    "binanceus": {"BTC": "BTCUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"},
}
CASH_DP = {"kraken": 4, "coinbase": 6, "binanceus": 6, "alpaca": 6, "ibkr": 6, "hyperliquid": 6}
CRYPTO_QTY_DP = {"kraken": 8, "coinbase": 8, "binanceus": 8}
CRYPTO_LOT = {"BTC": Decimal("0.0001"), "ETH": Decimal("0.001"), "SOL": Decimal("0.01")}
PERP_DP = {"BTC": 5, "ETH": 4, "SOL": 2}
PERP_MAX_LEVERAGE = {"BTC": 40, "ETH": 25, "SOL": 20}
EQUITY_DP = {"alpaca": 9, "ibkr": 6}
FUNDING_BASE = {"BTC": 0.0000110, "ETH": 0.0000125, "SOL": 0.0000150}  # per hour

# symbol -> (cash per share, latest ex-date in days before the end); quarterly
# before that. Inside the copy window on purpose: followers hold MSFT, AVGO
# (after its split) and VTI through an ex-date and get paid.
DIVIDENDS = {
    "MSFT": ("0.91", 24),
    "AVGO": ("0.65", 20),  # post-split; x the split ratio before the ex-date
    "AAPL": ("0.26", 41),
    "META": ("0.525", 12),
    "VTI": ("0.95", 37),
    "SCHD": ("0.26", 33),
    "JNJ": ("1.30", 52),
    "PG": ("1.06", 27),
    "KO": ("0.51", 47),
}
DIVIDEND_EVERY_DAYS = 91


def spot_instrument(venue: str, sym: str) -> NormalizedInstrument:
    if venue in SPOT_SYMBOLS:
        return NormalizedInstrument(
            symbol=SPOT_SYMBOLS[venue][sym],
            type=InstrumentType.SPOT,
            base_asset=sym,
            quote_asset="USD",
            qty_decimals=CRYPTO_QTY_DP[venue],
            min_notional="10",
        )
    return NormalizedInstrument(
        symbol=sym,
        type=InstrumentType.EQUITY,
        base_asset=sym,
        quote_asset="USD",
        qty_decimals=EQUITY_DP[venue],
    )


def perp_instrument(coin: str) -> NormalizedInstrument:
    return NormalizedInstrument(
        symbol=coin,
        type=InstrumentType.PERP,
        base_asset=f"{coin}-PERP",
        quote_asset="USDC",
        qty_decimals=PERP_DP[coin],
        min_notional="10",
    )


# ---- the world: price paths, books, market data ----------------------------------------------


class World:
    """Everything the fake venues know, generated up front from one seed."""

    def __init__(self, end: datetime, scale: Scale, seed: int) -> None:
        self.end = end
        self.scale = scale
        self.seed = seed
        self.copy_start = end - timedelta(days=scale.copy_days)
        self.history = {c.handle: self._history_days(c) for c in CREATORS}
        self.anchor_at = {
            # A fixed time of day: a seed made at any hour of the same day trades
            # the same way (only what falls after the end differs).
            c.handle: midnight(end.date() - timedelta(days=self.history[c.handle]))
            + timedelta(hours=15, minutes=17 + stable_hash(c.handle) % 40)
            for c in CREATORS
        }
        self.start = min(self.anchor_at.values())
        first = self.start.date() - timedelta(days=12)
        closes = draw_closes(first, end.date() + timedelta(days=1), random.Random(seed))
        self.paths = {u.key: PricePath(u, closes[u.key], end.date()) for u in UNDERLYINGS}
        self.books: dict[tuple[str, str], Book] = {}
        self.by_handle: dict[str, Book] = {}
        self.markets: dict[str, dict] = {}  # kalshi ticker -> {p, settles_at, result}
        self.funding: dict[str, dict[datetime, Decimal]] = {
            coin: self._funding_series(coin) for coin in PERP_DP
        }

    def _history_days(self, c: CreatorSpec) -> int:
        days = round(c.history_days * self.scale.history_factor)
        return max(self.scale.min_history, days)

    def rng(self, *parts: object) -> random.Random:
        return random.Random(stable_hash(self.seed, *parts))

    def _funding_series(self, coin: str) -> dict[datetime, Decimal]:
        rng = self.rng("funding", coin)
        out: dict[datetime, Decimal] = {}
        t = floor_hour(self.start)
        level = FUNDING_BASE[coin]
        while t <= self.end + timedelta(hours=1):
            level = 0.92 * level + 0.08 * FUNDING_BASE[coin] + rng.gauss(0, 0.000004)
            out[t] = q(level, 8)
            t += timedelta(hours=1)
        return out

    # ---- prices as the venues quote them --------------------------------------------------

    def price(self, sym: str, t: datetime) -> Decimal:
        return self.paths[sym].price(t)

    def perp_price(self, coin: str, t: datetime) -> Decimal:
        return sig5(self.paths[coin].price(t) * Decimal("1.0002"))

    def perp_close(self, coin: str, d: date) -> Decimal:
        return sig5(self.paths[coin].close(d) * Decimal("1.0002"))

    # ---- generation ----------------------------------------------------------------------

    def generate(self) -> None:
        for c in CREATORS:
            key_id = self._key_id(c)
            env = {"kalshi": "prod", "alpaca": "live"}.get(c.venue, "prod")
            book = Book(
                venue=c.venue,
                key_id=key_id,
                venue_env=env,
                cash_asset="USDC" if c.venue == "hyperliquid" else "USD",
                cash_dp=CASH_DP.get(c.venue, 6),
                anchor_at=self.anchor_at[c.handle],
                anchor_cash_atoms=c.cash * 10 ** CASH_DP.get(c.venue, 6),
                daily=c.venue == "ibkr",
            )
            self.books[(c.venue, key_id)] = book
            self.by_handle[c.handle] = book
            if c.kind == "binary":
                BinaryGen(self, c, book).run()
            elif c.kind == "perp":
                PerpGen(self, c, book).run()
            else:
                SpotGen(self, c, book).run()

    def _key_id(self, c: CreatorSpec) -> str:
        if c.key_id:
            return c.key_id
        rng = self.rng("key", c.handle)
        if c.venue == "hyperliquid":
            return "0x" + "".join(rng.choice("0123456789abcdef") for _ in range(40))
        if c.venue == "ibkr":
            return str(rng.randrange(900_000, 999_999))
        if c.venue == "alpaca":
            return str(uuid.UUID(int=rng.getrandbits(128)))
        return f"demo-{c.handle}-" + "".join(
            rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(8)
        )

    def fake_id(self, rng: random.Random, venue: str) -> str:
        if venue == "kraken":
            chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
            a, b, c = ("".join(rng.choice(chars) for _ in range(n)) for n in (5, 5, 6))
            return f"T{a}-{b}-{c}"
        if venue == "hyperliquid":
            return str(rng.randrange(10**14, 10**15))
        if venue == "ibkr":
            return str(rng.randrange(10**9, 10**10))
        return str(uuid.UUID(int=rng.getrandbits(128)))

    # ---- corporate actions (the alpaca data feed serves them) -----------------------------

    def corporate_actions(self) -> list[CorporateAction]:
        out: list[CorporateAction] = []
        for u in UNDERLYINGS:
            path = self.paths[u.key]
            if path.ex_date is not None:
                out.append(
                    CorporateAction(
                        symbol=u.key,
                        kind=CorporateActionKind.SPLIT,
                        ex_date=path.ex_date,
                        pay_date=path.ex_date,
                        ratio_new=path.ratio.numerator,
                        ratio_old=path.ratio.denominator,
                    )
                )
        for sym, ex, pay, cps in self.dividend_dates():
            out.append(
                CorporateAction(
                    symbol=sym,
                    kind=CorporateActionKind.CASH_DIVIDEND,
                    ex_date=ex,
                    pay_date=pay,
                    cash_per_share=cps,
                )
            )
        return out

    def dividend_dates(self) -> list[tuple[str, date, date, str]]:
        out = []
        for sym, (cps, back) in DIVIDENDS.items():
            ex = self.end.date() - timedelta(days=back)
            while ex > self.start.date() - timedelta(days=5):
                d = ex
                while d.weekday() >= 5:
                    d -= timedelta(days=1)
                pay = d + timedelta(days=14)
                while pay.weekday() >= 5:
                    pay += timedelta(days=1)
                path = self.paths[sym]
                if path.ex_date is not None and d < path.ex_date:  # pre-split shares
                    cps_d = Decimal(cps) * path.ratio.numerator / path.ratio.denominator
                    out.append((sym, d, pay, str(q(cps_d, -Decimal(cps).as_tuple().exponent))))
                else:
                    out.append((sym, d, pay, cps))
                ex -= timedelta(days=DIVIDEND_EVERY_DAYS)
        return out


# ---- generators: one creator's trading, as the venue books it --------------------------------


@dataclass
class Intent:
    sym: str
    frac: float
    side: int = 1  # +1 long, -1 short (perps)
    qty: int = 0  # atoms bought (shares/coins), adjusted by splits
    dead: bool = False
    core: bool = False  # a planned core holding: bought even when cash is short


class _Gen:
    def __init__(self, world: World, spec: CreatorSpec, book: Book) -> None:
        self.w = world
        self.c = spec
        self.book = book
        self.rng = world.rng("trades", spec.handle)
        self.end = world.end - timedelta(hours=3)
        self.seq = 0

    def visible_at(self, t: datetime) -> datetime:
        if not self.book.daily:
            return t
        return midnight(t.date() + timedelta(days=1)) + timedelta(hours=6)

    def session(self, t: datetime) -> datetime:
        """Snap `t` into a US equity session (weekday, 14:35-20:55 UTC)."""
        if not self.equity:
            return t
        day = t.date()
        offset = t - midnight(day)
        if offset > EQUITY_CLOSE:
            day += timedelta(days=1)
            offset = EQUITY_OPEN + timedelta(minutes=self.rng.uniform(0, 90))
        elif offset < EQUITY_OPEN:
            offset = EQUITY_OPEN + timedelta(minutes=self.rng.uniform(0, 90))
        while day.weekday() >= 5:
            day += timedelta(days=1)
        return midnight(day) + offset

    @property
    def equity(self) -> bool:
        return self.c.venue in ("alpaca", "ibkr")

    def intents(self) -> list[tuple[datetime, str, Intent, float]]:
        """Discretionary round trips: (time, "open"|"close", intent, portion).
        A trade is taken with probability `skill` when the price at its exit
        is above the entry (in the trade's direction), 1 - skill otherwise."""
        actions: list[tuple[datetime, str, Intent, float]] = []
        rate = self.c.trades_per_week / 168.0
        names = [s for s, _ in self.c.symbols]
        weights = [w for _, w in self.c.symbols]
        t = self.book.anchor_at + timedelta(minutes=45)
        busy: dict[str, datetime] = {}
        while True:
            t = t + timedelta(hours=self.rng.expovariate(rate))
            if t >= self.end:
                break
            t = self.session(t)
            if t >= self.end:  # a weekend snapped past the end
                break
            sym = self.rng.choices(names, weights)[0]
            if self.perp and busy.get(sym, t) > t:
                continue
            hold = timedelta(hours=self.rng.uniform(*self.c.hold_hours))
            exit_t = self.session(t + hold)
            look = min(exit_t, self.end)
            move = self.mark(sym, look) - self.mark(sym, t)
            side = 1
            if self.perp:
                side = 1 if self.rng.random() < 0.6 else -1
            good = move * side > 0
            if self.rng.random() >= (self.c.skill if good else 1 - self.c.skill):
                if not self.perp:
                    continue
                side = -side  # a perp trader takes the other side instead
            intent = Intent(sym, self.rng.uniform(*self.c.frac), side)
            actions.append((t, "open", intent, 0.0))
            if exit_t < self.end:
                if self.rng.random() < 0.35:
                    part = self.session(t + hold * self.rng.uniform(0.35, 0.8))
                    if part < exit_t:
                        actions.append((part, "close", intent, 0.5))
                actions.append((exit_t, "close", intent, 1.0))
                busy[sym] = exit_t
            else:
                busy[sym] = self.end
        return actions

    perp = False

    def mark(self, sym: str, t: datetime) -> Decimal:
        return self.w.price(sym, t)

    def size(self, raw: Decimal, lot: Decimal) -> Decimal:
        """An order size as a person types it: whole shares in round lots
        (5s, 10s, 25s as the count grows), coins to two significant figures."""
        if raw <= 0:
            return Decimal(0)
        if self.c.whole_shares:
            n = int(raw)
            step = 1 if n < 20 else 5 if n < 100 else 10 if n < 400 else 25
            return Decimal(n // step * step)
        step = max(lot, Decimal(1).scaleb(raw.adjusted() - 1))
        return (raw / step).to_integral_value(ROUND_DOWN) * step


class SpotGen(_Gen):
    """Spot crypto and equities: FILL_BASE / FILL_QUOTE / FEE legs; deposits,
    splits and dividends as events."""

    def run(self) -> None:
        c, book = self.c, self.book
        self.cash = book.anchor_cash_atoms
        self.held: dict[str, int] = {}
        self.eligible: dict[tuple[str, date], int] = {}
        actions = self.intents()
        anchor_day = book.anchor_at.date()
        for sym, start_day, stop_day, frac in c.core:
            t = self.core_time(start_day)
            if t is None:
                continue
            intent = Intent(sym, frac, core=True)
            actions.append((t, "open", intent, 0.0))
            stop = self.core_time(stop_day) if stop_day is not None else None
            if stop is not None and stop > t:
                actions.append((stop, "close", intent, 1.0))
        for day, dollars in c.deposits:
            when = book.anchor_at + timedelta(days=day, hours=3)
            if when < self.end:
                actions.append((when, "deposit", Intent("USD", 0), float(dollars)))
        if self.equity:
            for u in UNDERLYINGS:
                path = self.w.paths[u.key]
                if path.ex_date is not None and path.ex_date > anchor_day:
                    actions.append(
                        (midnight(path.ex_date) + timedelta(hours=8), "split", Intent(u.key, 0), 0)
                    )
            for sym, ex, pay, cps in self.w.dividend_dates():
                if ex > anchor_day and midnight(pay) < self.end:
                    actions.append((midnight(ex), "record", Intent(sym, 0), 0))
                    paid_at = midnight(pay) + (
                        timedelta(hours=23, minutes=59, seconds=59)
                        if book.daily
                        else timedelta(hours=12)
                    )
                    actions.append((paid_at, f"pay:{ex.isoformat()}:{cps}", Intent(sym, 0), 0))
        self.intents_all = [a[2] for a in actions]
        order = {"record": 0, "split": 1, "deposit": 2, "close": 3, "open": 4}
        actions.sort(key=lambda a: (a[0], order.get(a[1].split(":")[0], 5)))
        for t, kind, intent, portion in actions:
            if kind == "open":
                self.open(t, intent)
            elif kind == "close":
                self.close(t, intent, portion)
            elif kind == "deposit":
                self.deposit(t, int(portion))
            elif kind == "split":
                self.split(t, intent.sym)
            elif kind == "record":
                self.eligible[(intent.sym, t.date())] = self.held.get(intent.sym, 0)
            else:
                _, ex, cps = kind.split(":")
                self.dividend(t, intent.sym, date.fromisoformat(ex), Decimal(cps))

    # ---- book keeping --------------------------------------------------------------------

    def core_time(self, day: int) -> datetime | None:
        """A core position's date: `day` days after the anchor, or before the
        end when negative. None when that falls outside the history."""
        if day >= 0:
            base = self.book.anchor_at + timedelta(days=day, hours=1)
        else:
            base = self.w.end + timedelta(days=day)
        t = self.session(base)
        if t <= self.book.anchor_at + timedelta(hours=1) or t >= self.end:
            return None
        return t

    @property
    def qty_dp(self) -> int:
        return EQUITY_DP[self.c.venue] if self.equity else CRYPTO_QTY_DP[self.c.venue]

    def equity_value(self, t: datetime) -> Fraction:
        total = units(self.cash, self.book.cash_dp)
        for sym, held in self.held.items():
            if held:
                total += units(held, self.qty_dp) * Fraction(str(self.w.price(sym, t)))
        return total

    def fee(self, notional: Decimal) -> int:
        fee = notional * Decimal(str(self.c.fee_bps)) / 10_000
        fee = max(fee, Decimal(str(self.c.fee_min_usd)))
        return atoms_of(fee, self.book.cash_dp, ROUND_UP) if fee > 0 else 0

    def lot(self, sym: str) -> Decimal:
        return Decimal(1) if self.c.whole_shares else CRYPTO_LOT.get(sym, Decimal("0.0001"))

    def open(self, t: datetime, intent: Intent) -> None:
        price = self.w.price(intent.sym, t)
        price = q(price * Decimal("1.0003"), self.w.paths[intent.sym].u.dp)
        target = Decimal(str(float(self.equity_value(t)) * intent.frac))
        cash = Decimal(self.cash).scaleb(-self.book.cash_dp)
        budget = min(target, cash * Decimal("0.97"))
        if budget < target * Decimal("0.4") and not intent.core:
            intent.dead = True  # mostly invested already: no token-sized top-ups
            return
        qty = self.size(budget / price, self.lot(intent.sym))
        if qty <= 0 or qty * price < 25:
            intent.dead = True
            return
        notional = qty * price
        cost = atoms_of(notional, self.book.cash_dp)
        fee = self.fee(notional)
        if cost + fee > self.cash:
            intent.dead = True
            return
        qty_atoms = atoms_of(qty, self.qty_dp)
        self._fill(t, intent.sym, "buy", qty_atoms, price, cost, fee)
        intent.qty += qty_atoms

    def close(self, t: datetime, intent: Intent, portion: float) -> None:
        if intent.dead or intent.qty <= 0:
            return
        held = self.held.get(intent.sym, 0)
        lot_atoms = atoms_of(self.lot(intent.sym), self.qty_dp)
        qty_atoms = intent.qty if portion >= 1 else intent.qty // 2 // lot_atoms * lot_atoms
        qty_atoms = min(qty_atoms, held)
        if qty_atoms <= 0:
            return
        price = self.w.price(intent.sym, t)
        price = q(price * Decimal("0.9997"), self.w.paths[intent.sym].u.dp)
        notional = Decimal(qty_atoms).scaleb(-self.qty_dp) * price
        proceeds = atoms_of(notional, self.book.cash_dp)
        self._fill(t, intent.sym, "sell", qty_atoms, price, proceeds, self.fee(notional))
        intent.qty -= qty_atoms

    def _fill(
        self, t: datetime, sym: str, action: str, qty: int, price: Decimal, cash: int, fee: int
    ) -> None:
        sign = 1 if action == "buy" else -1
        inst = spot_instrument(self.c.venue, sym)
        legs = [
            NormalizedLeg(
                asset=sym,
                delta_atoms=sign * qty,
                decimals=self.qty_dp,
                kind=LegKind.FILL_BASE,
                instrument_symbol=inst.symbol,
            ),
            NormalizedLeg(
                asset="USD",
                delta_atoms=-sign * cash,
                decimals=self.book.cash_dp,
                kind=LegKind.FILL_QUOTE,
                instrument_symbol=inst.symbol,
            ),
        ]
        if fee:
            legs.append(
                NormalizedLeg(
                    asset="USD",
                    delta_atoms=-fee,
                    decimals=self.book.cash_dp,
                    kind=LegKind.FEE,
                    instrument_symbol=inst.symbol,
                )
            )
        # One order sometimes executes as two fills at the same instant.
        parts = [qty]
        if not self.equity and qty > 10 and self.rng.random() < 0.15:
            first = qty * self.rng.randint(3, 7) // 10
            parts = [first, qty - first]
        if len(parts) == 1:
            fills = [(qty, legs)]
        else:
            fills = []
            remaining_cash, remaining_fee = cash, fee
            for i, part in enumerate(parts):
                last = i == len(parts) - 1
                c_part = remaining_cash if last else cash * part // qty
                f_part = remaining_fee if last else fee * part // qty
                remaining_cash -= c_part
                remaining_fee -= f_part
                part_legs = [
                    legs[0].model_copy(update={"delta_atoms": sign * part}),
                    legs[1].model_copy(update={"delta_atoms": -sign * c_part}),
                ]
                if f_part:
                    part_legs.append(legs[2].model_copy(update={"delta_atoms": -f_part}))
                fills.append((part, part_legs))
        order_id = self.w.fake_id(self.rng, "order")
        for part, part_legs in fills:
            nf = NormalizedAssetFill(
                venue_fill_id=self.w.fake_id(self.rng, self.c.venue),
                order_id=order_id,
                instrument=inst,
                action=action,
                qty_atoms=part,
                price=str(price),
                legs=part_legs,
                filled_at=t,
                liquidity="taker",
            )
            self.book.fills.append(Visible(t, self.visible_at(t), nf))
        self.cash -= sign * cash + fee
        self.held[sym] = self.held.get(sym, 0) + sign * qty

    def _event(self, t: datetime, kind: LegKind, legs: list[NormalizedLeg], desc: str) -> None:
        ne = NormalizedEvent(
            venue_ref=self.w.fake_id(self.rng, "event"),
            kind=kind,
            legs=legs,
            occurred_at=t,
            description=desc,
        )
        self.book.events.append(Visible(t, self.visible_at(t), ne))

    def deposit(self, t: datetime, dollars: int) -> None:
        amount = dollars * 10**self.book.cash_dp
        self._event(
            t,
            LegKind.DEPOSIT,
            [
                NormalizedLeg(
                    asset="USD",
                    delta_atoms=amount,
                    decimals=self.book.cash_dp,
                    kind=LegKind.DEPOSIT,
                )
            ],
            "deposit",
        )
        self.cash += amount

    def split(self, t: datetime, sym: str) -> None:
        held = self.held.get(sym, 0)
        ratio = self.w.paths[sym].ratio
        if held <= 0:
            return
        delta = held * ratio.numerator // ratio.denominator - held
        self._event(
            t,
            LegKind.SPLIT,
            [
                NormalizedLeg(
                    asset=sym,
                    delta_atoms=delta,
                    decimals=self.qty_dp,
                    kind=LegKind.SPLIT,
                    instrument_symbol=sym,
                )
            ],
            f"{sym} {ratio.numerator}-for-{ratio.denominator} split",
        )
        self.held[sym] = held + delta
        for intent in self.intents_all:
            if intent.sym == sym:
                intent.qty = intent.qty * ratio.numerator // ratio.denominator

    def dividend(self, t: datetime, sym: str, ex: date, cps: Decimal) -> None:
        held = self.eligible.pop((sym, ex), 0)
        if held <= 0:
            return
        amount = atoms_of(Decimal(held).scaleb(-self.qty_dp) * cps, self.book.cash_dp, ROUND_DOWN)
        if amount <= 0:
            return
        self._event(
            t,
            LegKind.DIVIDEND,
            [
                NormalizedLeg(
                    asset="USD",
                    delta_atoms=amount,
                    decimals=self.book.cash_dp,
                    kind=LegKind.DIVIDEND,
                    instrument_symbol=sym,
                )
            ],
            f"{sym} cash dividend {cps}/share",
        )
        self.cash += amount


class PerpGen(_Gen):
    """Hyperliquid perps: FILL_BASE on "<COIN>-PERP", REALIZED_PNL on a
    reducing fill, FEE in USDC; hourly funding as FUNDING events."""

    perp = True

    def mark(self, sym: str, t: datetime) -> Decimal:
        return self.w.perp_price(sym, t)

    def run(self) -> None:
        self.cash = self.book.anchor_cash_atoms
        self.pos: dict[str, int] = {}
        self.entry: dict[str, Fraction] = {}
        actions: list[tuple[datetime, str, Intent, float]] = self.intents()
        t = floor_hour(self.book.anchor_at) + timedelta(hours=1)
        while t < self.end:
            actions.append((t, "funding", Intent("", 0), 0.0))
            t += timedelta(hours=1)
        order = {"funding": 0, "close": 1, "open": 2}
        actions.sort(key=lambda a: (a[0], order[a[1]]))
        for t, kind, intent, portion in actions:
            if kind == "funding":
                self.fund(t)
            elif kind == "open":
                self.open(t, intent)
            else:
                self.close(t, intent, portion)

    def equity_value(self, t: datetime) -> Fraction:
        total = units(self.cash, 6)
        for coin, pos in self.pos.items():
            if pos:
                mark = Fraction(str(self.w.perp_price(coin, t)))
                total += units(pos, PERP_DP[coin]) * (mark - self.entry[coin])
        return total

    def open(self, t: datetime, intent: Intent) -> None:
        coin = intent.sym
        if self.pos.get(coin):
            intent.dead = True
            return
        price = self.w.perp_price(coin, t)
        price = sig5(price * (Decimal("1.0002") if intent.side > 0 else Decimal("0.9998")))
        notional = Decimal(str(float(self.equity_value(t)) * intent.frac))
        dp = PERP_DP[coin]
        qty = self.size(notional / price, Decimal(1).scaleb(-dp))
        qty_atoms = atoms_of(qty, dp)
        if qty_atoms <= 0 or qty * price < 20:
            intent.dead = True
            return
        self._fill(t, coin, intent.side, qty_atoms, price, 0)
        self.pos[coin] = intent.side * qty_atoms
        self.entry[coin] = Fraction(str(price))
        intent.qty = qty_atoms

    def close(self, t: datetime, intent: Intent, portion: float) -> None:
        coin = intent.sym
        pos = self.pos.get(coin, 0)
        if intent.dead or intent.qty <= 0 or pos == 0:
            return
        qty_atoms = intent.qty if portion >= 1 else intent.qty // 2
        qty_atoms = min(qty_atoms, abs(pos))
        if qty_atoms <= 0:
            return
        side = -1 if pos > 0 else 1  # the closing fill's direction
        price = self.w.perp_price(coin, t)
        price = sig5(price * (Decimal("0.9998") if side < 0 else Decimal("1.0002")))
        closed = units(qty_atoms, PERP_DP[coin])
        pnl = closed * (Fraction(str(price)) - self.entry[coin]) * (1 if pos > 0 else -1)
        realized = atoms_of(Decimal(pnl.numerator) / Decimal(pnl.denominator), 6)
        self._fill(t, coin, side, qty_atoms, price, realized)
        self.pos[coin] = pos + side * qty_atoms
        intent.qty -= qty_atoms
        if self.pos[coin] == 0:
            del self.entry[coin]

    def _fill(self, t: datetime, coin: str, side: int, qty: int, price: Decimal, pnl: int) -> None:
        notional = Decimal(qty).scaleb(-PERP_DP[coin]) * price
        fee = atoms_of(notional * Decimal(str(self.c.fee_bps)) / 10_000, 6, ROUND_UP)
        legs = [
            NormalizedLeg(
                asset=f"{coin}-PERP",
                delta_atoms=side * qty,
                decimals=PERP_DP[coin],
                kind=LegKind.FILL_BASE,
                instrument_symbol=coin,
            )
        ]
        if pnl:
            legs.append(
                NormalizedLeg(
                    asset="USDC",
                    delta_atoms=pnl,
                    decimals=6,
                    kind=LegKind.REALIZED_PNL,
                    instrument_symbol=coin,
                )
            )
        legs.append(
            NormalizedLeg(
                asset="USDC", delta_atoms=-fee, decimals=6, kind=LegKind.FEE, instrument_symbol=coin
            )
        )
        nf = NormalizedAssetFill(
            venue_fill_id=self.w.fake_id(self.rng, "hyperliquid"),
            order_id=str(self.rng.randrange(10**10, 10**11)),
            instrument=perp_instrument(coin),
            action="buy" if side > 0 else "sell",
            qty_atoms=qty,
            price=str(price),
            legs=legs,
            filled_at=t,
            liquidity="taker",
        )
        self.book.fills.append(Visible(t, t, nf))
        self.cash += pnl - fee
        entries = self.book.entry_prices.setdefault(coin, [])
        entries.append((t, price))

    def fund(self, t: datetime) -> None:
        for coin, pos in sorted(self.pos.items()):
            if not pos:
                continue
            rate = self.w.funding[coin].get(t)
            if rate is None:
                continue
            mark = Fraction(str(self.w.perp_price(coin, t)))
            pay = -units(pos, PERP_DP[coin]) * mark * Fraction(str(rate))
            amount = atoms_of(Decimal(pay.numerator) / Decimal(pay.denominator), 6)
            if amount == 0:
                continue
            ne = NormalizedEvent(
                venue_ref=f"{int(t.timestamp() * 1000)}:{coin}",
                kind=LegKind.FUNDING,
                legs=[
                    NormalizedLeg(
                        asset="USDC",
                        delta_atoms=amount,
                        decimals=6,
                        kind=LegKind.FUNDING,
                        instrument_symbol=coin,
                    )
                ],
                occurred_at=t,
                description=f"funding {coin} rate {rate}",
            )
            self.book.events.append(Visible(t, t, ne))
            self.cash += amount


# Daily-high markets: (annual mean of the daily high, seasonal swing, daily
# sd around the normal), in F; the normal high peaks in late July (NYC ~85F,
# Chicago ~84F; ~39F/32F in January).
KALSHI_WEATHER = {"KXHIGHNY": (62.0, 23.0, 6), "KXHIGHCHI": (58.5, 26.0, 8)}
WEATHER_FORECAST_SD = 2.5  # F: the market's error on the day's high
# Macro markets settle on the release: (series, mean outcome, outcome sd,
# market forecast sd, strike step, strike decimals, settle time UTC).
KALSHI_MACRO = (
    ("KXCPIYOY", 2.9, 0.15, 0.15, 0.1, 1, timedelta(hours=14)),
    ("KXPAYROLLS", 75, 40, 40, 25, 0, timedelta(hours=14)),
    ("KXFED", 3.5, 0.25, 0.2, 0.25, 2, timedelta(hours=19)),
)
# FOMC decision days by month: which Wednesday (-1 = the last), as in 2026.
FOMC_WEDNESDAY = {1: -1, 3: 3, 4: -1, 6: 3, 7: -1, 9: 3, 10: -1, 12: 2}


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th `weekday` (Mon=0) of a month; n=-1 is the last one."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))
        return d
    d = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def macro_release(series: str, year: int, month: int) -> date | None:
    """When a month's market resolves: payrolls on the first Friday, CPI
    mid-month (the second Wednesday, near enough), the Fed on its meeting day."""
    if series == "KXPAYROLLS":
        return nth_weekday(year, month, 4, 1)
    if series == "KXCPIYOY":
        return nth_weekday(year, month, 2, 2)
    n = FOMC_WEDNESDAY.get(month)
    return nth_weekday(year, month, 2, n) if n is not None else None


class BinaryGen(_Gen):
    """Kalshi: buy YES or NO, hold to settlement. Every event (a city's high
    on a day, a CPI print, a Fed decision) has one outcome, drawn up front, so
    a strike ladder always settles coherently. The market prices each strike
    off its forecast of the outcome; the creator has a noisy private signal of
    the market's error (`insight`: the share of that error's variance the
    signal explains), updates on it like a Bayesian and buys the side its own
    probability favours by at least `edge`: calibrated beliefs plus a real
    signal make a profitable record over time, with losing streaks."""

    def run(self) -> None:
        c, book = self.c, self.book
        self.cash = book.anchor_cash_atoms
        events: list[tuple[datetime, int, str, dict]] = []
        day = book.anchor_at.date()
        n = 0
        self.tickers: set[str] = set()
        while midnight(day) < self.end:
            count = self._poisson(c.markets_per_day)
            for _ in range(count):
                # Weather: traded on the morning model runs (06:00-13:00 New York).
                hours = (10, 17) if c.handle == "isobar" else (11, 21)
                at = midnight(day) + timedelta(hours=self.rng.uniform(*hours))
                if at <= book.anchor_at + timedelta(minutes=30) or at >= self.end:
                    continue
                market = self.weather(day) if c.handle == "isobar" else self.macro(day)
                if market is None:
                    continue
                n += 1
                events.append((at, n, "buy", market))
            day += timedelta(days=1)
        events.sort(key=lambda e: (e[0], e[1]))
        pending: list[tuple[datetime, dict]] = []
        for at, _, _, m in events:
            pending.sort(key=lambda p: p[0])
            while pending and pending[0][0] <= at:
                self.settle(*pending.pop(0))
            pos = self.buy(at, m)
            if pos is not None:
                pending.append((m["settles"], pos))
        for settles, pos in sorted(pending, key=lambda p: p[0]):
            if settles < self.end:
                self.settle(settles, pos)
            else:
                self.w.markets[pos["ticker"]] = pos["market"]

    def weather(self, day: date) -> dict:
        """Today's high in NYC or Chicago (whole degrees, near the seasonal
        normal), settled the next morning on the NWS climate report."""
        name = self.rng.choice(sorted(KALSHI_WEATHER))
        mean, swing, sd = KALSHI_WEATHER[name]
        normal = mean + swing * math.cos(2 * math.pi * (day.timetuple().tm_yday - 205) / 365)
        high = normal + self.w.rng("high", name, day).gauss(0, sd)
        settles = midnight(day + timedelta(days=1)) + timedelta(hours=14)
        code = day.strftime("%y%b%d").upper()
        return self.event(name, code, settles, high, WEATHER_FORECAST_SD, 1, 0)

    def macro(self, day: date) -> dict | None:
        """CPI, payrolls or the Fed: the next release 2-35 days out."""
        name, mean, sd, forecast_sd, step, dp, at = self.rng.choice(KALSHI_MACRO)
        release = None
        for ahead in range(3):
            y, m = divmod(day.month - 1 + ahead, 12)
            d = macro_release(name, day.year + y, m + 1)
            if d is not None and day + timedelta(days=2) <= d <= day + timedelta(days=35):
                release = d
                break
        if release is None:
            return None
        # CPI and payrolls are named for the month the data covers; the Fed for its meeting.
        label = release if name == "KXFED" else release.replace(day=1) - timedelta(days=1)
        outcome = self.w.rng("outcome", name, release).gauss(mean, sd)
        code = label.strftime("%y%b").upper()
        return self.event(name, code, midnight(release) + at, outcome, forecast_sd, step, dp)

    def event(
        self, series: str, code: str, settles: datetime, outcome: float, sd: float, step, dp: int
    ) -> dict:
        """One event: its outcome as reported (on the strike grid), the
        market's forecast and the creator's own. Seeded by the event, so
        every trade in it sees the same numbers."""
        market = outcome + self.w.rng("market", series, code).gauss(0, sd)
        beta = self.c.insight  # signal = market error + noise; beta = its shrinkage weight
        noise = sd * math.sqrt((1 - beta) / beta)
        signal = outcome - market + self.w.rng("own", self.c.handle, series, code).gauss(0, noise)
        own = market + beta * signal
        return {
            "series": series,
            "code": code,
            "settles": settles,
            "reported": Decimal(f"{round(outcome / step) * step:.{dp}f}"),
            "market": market,
            "own": own,
            "sd": sd,
            "step": step,
            "dp": dp,
        }

    def quote(self, ev: dict) -> tuple[str, int, str, str] | None:
        """A strike near the market's forecast that this account holds no
        position in (settlements are per ticker), priced by the market, and
        the side the creator's forecast favours: (ticker, yes cents, side,
        result). YES pays when the reported outcome is above the strike."""
        step, dp, sd = ev["step"], ev["dp"], ev["sd"]
        for _ in range(6):
            strike = round((ev["market"] + self.rng.gauss(0, sd)) / step) * step
            txt = f"{strike:.{dp}f}"
            ticker = f"{ev['series']}-{ev['code']}-T{txt}"
            if strike < 0 or ticker in self.tickers:
                continue
            line = strike + step / 2  # reported above the strike <=> outcome past this
            yes_cents = round(100 * (1 - NormalDist(ev["market"], sd).cdf(line)))
            if not 6 <= yes_cents <= 94:
                continue
            p_own = 1 - NormalDist(ev["own"], sd * math.sqrt(1 - self.c.insight)).cdf(line)
            if abs(p_own - yes_cents / 100) < self.c.edge:
                continue
            side = "yes" if p_own > yes_cents / 100 else "no"
            result = "yes" if ev["reported"] > Decimal(txt) else "no"
            return ticker, yes_cents, side, result
        return None

    def _poisson(self, lam: float) -> int:
        n, p, threshold = 0, 1.0, math.exp(-lam)
        while True:
            p *= self.rng.random()
            if p <= threshold:
                return n
            n += 1

    def equity_value(self) -> int:
        return self.cash  # cost basis: open positions are already paid for

    def buy(self, at: datetime, m: dict) -> dict | None:
        quoted = self.quote(m)
        if quoted is None:
            return None
        ticker, yes_cents, side, result = quoted
        cents = yes_cents if side == "yes" else 100 - yes_cents
        price = cents * 10_000
        # Stake leans a little on the price (a cheap contract is a long shot, so
        # less goes on it) but stays in a narrow band: wider swings between
        # consecutive bets would read as martingale sizing to stats.martingale_flag.
        frac = self.rng.uniform(*self.c.frac) * (0.75 + cents / 200)
        count = int(self.equity_value() * frac / price)
        if count >= 100:
            count -= count % (50 if count >= 1_000 else 10)  # round lots, as people size
        if count < 1:
            return None
        fee = math.ceil(0.07 * count * (cents / 100) * (1 - cents / 100) * 100) * 10_000
        cost = count * price
        if cost + fee > self.cash:
            return None
        self.tickers.add(ticker)
        nf = NormalizedFill(
            venue_fill_id=str(uuid.UUID(int=self.rng.getrandbits(128))),
            ticker=ticker,
            side=side,
            action="buy",
            count_micros=count * M,
            price_micros=price,
            fee_micros=fee,
            filled_at=at,
        )
        self.book.fills.append(Visible(at, at, nf))
        self.cash -= cost + fee
        market = {"yes_cents": yes_cents, "settles": m["settles"], "result": result}
        self.w.markets[ticker] = market
        return {"ticker": ticker, "side": side, "count": count, "market": market}

    def settle(self, at: datetime, pos: dict) -> None:
        market = pos["market"]
        won = market["result"] == pos["side"]
        revenue = pos["count"] * M if won else 0
        ns = NormalizedSettlement(
            venue_settlement_id=f"{pos['ticker']}:{int(at.timestamp())}",
            ticker=pos["ticker"],
            market_result=market["result"],
            yes_count_micros=pos["count"] * M if pos["side"] == "yes" else 0,
            no_count_micros=pos["count"] * M if pos["side"] == "no" else 0,
            revenue_micros=revenue,
            settled_at=at,
        )
        self.book.settlements.append(Visible(at, at, ns))
        self.cash += revenue


# ---- fake venues at the registry seam ------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)  # the simulated clock (time-machine)


def _sum_legs(book: Book, now: datetime) -> dict[str, tuple[int, int]]:
    """The venue's balance vector: anchor cash + every visible leg."""
    out: dict[str, tuple[int, int]] = {book.cash_asset: (book.anchor_cash_atoms, book.cash_dp)}
    rows = [v for v in book.fills + book.events if v.visible_at <= now]
    for v in rows:
        for leg in v.item.legs:
            atoms, dp = out.get(leg.asset, (0, leg.decimals))
            scale = max(dp, leg.decimals)
            atoms = atoms * 10 ** (scale - dp) + leg.delta_atoms * 10 ** (scale - leg.decimals)
            out[leg.asset] = (atoms, scale)
    return out


class _Pages:
    PAGE = 200

    @staticmethod
    def page(rows: list[Visible], cursor: str | None, since: datetime | None) -> Page:
        now = _now()
        if cursor is not None:
            since_iso, offset_s, until_iso = cursor.split("|")
            since = datetime.fromisoformat(since_iso) if since_iso else None
            offset, until = int(offset_s), datetime.fromisoformat(until_iso)
        else:
            offset, until = 0, now
        chosen = [v for v in rows if v.visible_at <= until and (since is None or v.at >= since)]
        chosen.sort(key=lambda v: v.at)
        items = [v.item for v in chosen[offset : offset + _Pages.PAGE]]
        if offset + _Pages.PAGE < len(chosen):
            nxt = f"{since.isoformat() if since else ''}|{offset + _Pages.PAGE}|{until.isoformat()}"
            return Page(items, next_cursor=nxt)
        return Page(items, next_cursor=None, resume_cursor=None)


class SimAssetConnector(AssetVenueConnector):
    def __init__(self, book: Book) -> None:
        self.venue = book.venue
        self.book = book

    def fetch_asset_fills_page(self, cursor=None, since=None):
        return _Pages.page(self.book.fills, cursor, since)

    def fetch_events_page(self, cursor=None, since=None):
        return _Pages.page(self.book.events, cursor, since)

    def fetch_balances(self) -> NormalizedBalances:
        now = _now()  # a daily venue's rows become visible with its statement
        assets = []
        for asset, (atoms, dp) in sorted(_sum_legs(self.book, now).items()):
            if atoms == 0 and asset != self.book.cash_asset:
                continue
            entry = None
            if asset.endswith("-PERP"):
                coin = asset.removesuffix("-PERP")
                history = [p for t, p in self.book.entry_prices.get(coin, []) if t <= now]
                entry = str(history[-1]) if history else None
            assets.append(AssetAmount(asset=asset, atoms=atoms, decimals=dp, entry_price=entry))
        return NormalizedBalances(as_of=now, assets=assets)

    def check_write_scope(self) -> bool | None:
        return False

    def prove_read_only(self) -> ReadOnlyEvidence:
        method = registry.get_profile(self.venue).readonly_proof
        return ReadOnlyEvidence(
            method=method,
            read_only=None if method is ReadOnlyProof.NONE else True,
            checked_at=_now(),
            details={"demo": True},
        )

    def verify_claim(self, code: str) -> bool | None:
        return True


class SimBinaryConnector(VenueConnector):
    def __init__(self, book: Book) -> None:
        self.venue = book.venue
        self.book = book

    def _cash(self, now: datetime) -> int:
        cash = self.book.anchor_cash_atoms
        for v in self.book.fills:
            if v.visible_at <= now:
                f = v.item
                cash -= f.count_micros * f.price_micros // M + f.fee_micros
        for v in self.book.settlements:
            if v.visible_at <= now:
                cash += v.item.revenue_micros
        return cash

    def check_connection(self) -> NormalizedBalance:
        return self.fetch_balance()

    def check_write_scope(self) -> bool | None:
        return False

    def fetch_fills(self, since: datetime | None = None) -> list[NormalizedFill]:
        now = _now()
        rows = [
            v for v in self.book.fills if v.visible_at <= now and (since is None or v.at >= since)
        ]
        return [v.item for v in sorted(rows, key=lambda v: v.at)]

    def fetch_settlements(self, since: datetime | None = None) -> list[NormalizedSettlement]:
        now = _now()
        rows = [
            v
            for v in self.book.settlements
            if v.visible_at <= now and (since is None or v.at >= since)
        ]
        return [v.item for v in sorted(rows, key=lambda v: v.at)]

    def fetch_positions(self) -> list[NormalizedPosition]:
        now = _now()
        settled = {v.item.ticker for v in self.book.settlements if v.visible_at <= now}
        pos: dict[str, int] = {}
        for v in self.book.fills:
            f = v.item
            if v.visible_at <= now and f.ticker not in settled:
                sign = 1 if f.side == "yes" else -1
                pos[f.ticker] = pos.get(f.ticker, 0) + sign * f.count_micros
        return [NormalizedPosition(ticker=t, position_micros=p) for t, p in pos.items() if p]

    def fetch_balance(self) -> NormalizedBalance:
        now = _now()
        return NormalizedBalance(balance_micros=self._cash(now), as_of=now)


class SimMarketData(AssetMarketData):
    """Public market data of one asset venue, from the world's price paths."""

    def __init__(self, world: World, venue: str) -> None:
        self.venue = venue
        self.w = world
        self.symbols: dict[str, tuple[str, str]] = {}  # symbol -> (underlying, kind)
        self.listed: list[NormalizedInstrument] = []
        if venue in SPOT_SYMBOLS:
            for sym in ("BTC", "ETH", "SOL"):
                inst = spot_instrument(venue, sym)
                self.listed.append(inst)
                self.symbols[inst.symbol] = (sym, "spot")
        elif venue == "hyperliquid":
            for coin in PERP_DP:
                self.listed.append(perp_instrument(coin))
                self.symbols[coin] = (coin, "perp")
        elif venue == "alpaca":
            for u in UNDERLYINGS:
                if u.equity:
                    inst = spot_instrument("alpaca", u.key)
                    self.listed.append(inst)
                    self.symbols[u.key] = (u.key, "equity")

    def fetch_instruments(self) -> list[NormalizedInstrument]:
        return list(self.listed)

    def fetch_daily_closes(self, symbol: str, start: date, end: date) -> list[DailyClose]:
        if symbol not in self.symbols:
            return []
        under, kind = self.symbols[symbol]
        path = self.w.paths[under]
        today = _now().date()
        out = []
        d = start
        while d <= min(end, today - timedelta(days=1)):
            if path.has_bar(d):
                close = self.w.perp_close(under, d) if kind == "perp" else path.close(d)
                out.append(DailyClose(symbol=symbol, day=d, close=str(close)))
            d += timedelta(days=1)
        return out

    def fetch_quote(self, symbol: str) -> Quote | None:
        if symbol not in self.symbols:
            return None
        under, kind = self.symbols[symbol]
        now = _now()
        mid = self.w.perp_price(under, now) if kind == "perp" else self.w.price(under, now)
        dp = self.w.paths[under].u.dp
        half = max(mid * Decimal("0.0001"), Decimal(1).scaleb(-dp))
        if kind == "perp":
            bid, ask = sig5(mid - half), sig5(mid + half)
        else:
            bid, ask = q(mid - half, dp, ROUND_DOWN), q(mid + half, dp, ROUND_UP)
        return Quote(symbol=symbol, bid=str(bid), ask=str(ask), last=str(mid), as_of=now)

    def fetch_funding_history(self, symbol: str, start: datetime, end: datetime):
        if self.venue != "hyperliquid" or symbol not in PERP_DP:
            return []
        now = _now()
        return [
            FundingRate(
                symbol=symbol, at=t, rate=str(rate), mark_price=str(self.w.perp_price(symbol, t))
            )
            for t, rate in sorted(self.w.funding[symbol].items())
            if start <= t <= end and t <= now
        ]

    def fetch_corporate_actions(self, start: date, end: date, symbols=None):
        if self.venue != "alpaca":
            return []
        wanted = set(symbols) if symbols is not None else None
        return [
            a
            for a in self.w.corporate_actions()
            if start <= a.ex_date <= end and (wanted is None or a.symbol in wanted)
        ]

    def fetch_margin_tiers(self, symbol: str) -> list[MarginTier]:
        if self.venue != "hyperliquid" or symbol not in PERP_MAX_LEVERAGE:
            return []
        return [MarginTier(lower_bound="0", max_leverage=PERP_MAX_LEVERAGE[symbol])]


class SimKalshiMarketData(MarketDataSource):
    venue = "kalshi"

    def __init__(self, world: World) -> None:
        self.w = world

    def fetch_market(self, ticker: str) -> NormalizedMarket | None:
        m = self.w.markets.get(ticker)
        if m is None:
            return None
        now = _now()
        settled = m["settles"] <= now
        cents = m["yes_cents"]
        return NormalizedMarket(
            ticker=ticker,
            status="finalized" if settled else "active",
            yes_bid_micros=0 if settled else max(1, cents - 2) * 10_000,
            yes_ask_micros=0 if settled else min(99, cents + 2) * 10_000,
            last_price_micros=cents * 10_000,
            result=m["result"] if settled else "",
            close_time=m["settles"],
            as_of=now,
        )


class NoMarketData(AssetMarketData):
    """Sources the demo never needs (the CoinGecko fallback, Polymarket):
    they answer nothing rather than reach the network."""

    def __init__(self, venue: str) -> None:
        self.venue = venue

    def pricing_symbol(self, asset, instruments=None):
        return None


@contextmanager
def sim_registry(world: World) -> Iterator[None]:
    """Install the fake venues behind registry.build_connector /
    build_market_data for this process, and restore the real ones after."""
    builders = dict(registry._BUILDERS)
    market = dict(registry._MARKET_DATA_BUILDERS)

    def connector(venue: str):
        def build(venue_env: str, key_id: str, secret: str | None) -> VenueConnector:
            book = world.books.get((venue, key_id))
            if book is None:
                raise ValueError(f"seed_demo: no simulated {venue} account {key_id!r}")
            if venue == "kalshi":
                return SimBinaryConnector(book)
            return SimAssetConnector(book)

        return build

    def market_data(venue: str):
        def build(venue_env: str) -> MarketDataSource:
            if venue == "kalshi":
                return SimKalshiMarketData(world)
            if venue in SPOT_SYMBOLS or venue in ("hyperliquid", "alpaca"):
                return SimMarketData(world, venue)
            return NoMarketData(venue)

        return build

    for venue in builders:
        registry._BUILDERS[venue] = connector(venue)
    for venue in market:
        registry._MARKET_DATA_BUILDERS[venue] = market_data(venue)
    try:
        yield
    finally:
        registry._BUILDERS.clear()
        registry._BUILDERS.update(builders)
        registry._MARKET_DATA_BUILDERS.clear()
        registry._MARKET_DATA_BUILDERS.update(market)


@contextmanager
def network_blocked() -> Iterator[None]:
    """Invariant 6: no venue is ever called while seeding."""

    def refuse(self, request, *args, **kwargs):
        raise RuntimeError(f"seed_demo: network disabled ({request.method} {request.url.host})")

    saved = httpx.Client.send, httpx.AsyncClient.send
    httpx.Client.send = refuse
    httpx.AsyncClient.send = refuse
    try:
        yield
    finally:
        httpx.Client.send, httpx.AsyncClient.send = saved


# ---- the simulation: the real pipeline on a moving clock ------------------------------------


@dataclass(order=True)
class Ev:
    at: datetime
    order: int
    kind: str = field(compare=False)
    arg: object = field(compare=False, default=None)


@dataclass
class Summary:
    strategies: list[dict] = field(default_factory=list)
    logins: list[tuple[str, str]] = field(default_factory=list)
    failed_jobs: list[str] = field(default_factory=list)
    jobs_run: int = 0
    seconds: float = 0.0


class Sim:
    def __init__(self, sf: sessionmaker, world: World, traveller, password: str, log) -> None:
        self.sf = sf
        self.w = world
        self.clock = traveller
        self.password = password
        self.log = log
        self.runner = JobRunner(sf, None, worker_id="seed-demo")
        self.accounts: dict[str, int] = {}  # handle -> venue account id
        self.strategies: dict[str, int] = {}  # handle -> strategy id
        self.users: dict[str, int] = {}  # investor local part -> user id
        self.summary = Summary()

    # ---- clock and queue -----------------------------------------------------------------

    def travel(self, t: datetime) -> None:
        if t > _now():
            self.clock.move_to(t)

    def drain(self, horizon: datetime) -> None:
        """Run every claimable job; a job deferred to a moment before
        `horizon` (a barrier, a yield) is waited for by moving the clock."""
        for _ in range(100_000):
            ran = self.runner.run_once()
            self.summary.jobs_run += ran
            if ran:
                continue
            with self.sf() as db:
                nxt = db.scalar(
                    select(func.min(Job.run_after)).where(Job.status == JobStatus.PENDING)
                )
            if nxt is None:
                return
            nxt = jobs.aware(nxt)
            if nxt > horizon:
                return
            self.clock.move_to(max(nxt, _now()) + timedelta(seconds=1))
        raise RuntimeError("seed_demo: the job queue did not drain")

    def enqueue(self, kind: str, *, account_id: int | None = None, payload=None, key=None):
        with self.sf() as db:
            enqueue(db, kind, account_id=account_id, payload=payload, dedupe_key=key)
            db.commit()

    # ---- schedule ------------------------------------------------------------------------

    def events(self) -> list[Ev]:
        w = self.w
        evs: list[Ev] = []
        for c in CREATORS:
            evs.append(Ev(w.anchor_at[c.handle], 0, "connect", c))
        for c in CREATORS:
            book = w.by_handle[c.handle]
            rng = w.rng("lag", c.handle)
            times: set[datetime] = set()
            day = book.anchor_at.date()
            while midnight(day) < w.end:  # a late-evening sync; daily once tailed
                at = midnight(day) + timedelta(hours=23, minutes=20 + stable_hash(c.handle) % 30)
                every = 1 if at >= w.copy_start - timedelta(days=2) else w.scale.sync_every_days
                if book.anchor_at < at < w.end and day.toordinal() % every == 0:
                    times.add(at)
                day += timedelta(days=1)
            for v in book.fills + book.settlements:
                # Inside the copy window every new fill is picked up by a sync a
                # few minutes after the venue reports it (the worker's cadence).
                # Events (funding, dividends) wait for the next sync: they are
                # never copied, so their exact pickup time changes nothing.
                if v.visible_at >= w.copy_start - timedelta(hours=6) and v.visible_at < w.end:
                    lag = rng.uniform(40, 260) if not book.daily else rng.uniform(600, 3_000)
                    times.add(v.visible_at + timedelta(seconds=lag))
            evs += [Ev(t, 3, "sync", c.handle) for t in sorted(times) if t < w.end]
        day = w.start.date() + timedelta(days=1)
        while midnight(day) + timedelta(hours=4, minutes=15) < w.end:
            evs.append(Ev(midnight(day) + timedelta(hours=4, minutes=15), 1, "nightly", day))
            day += timedelta(days=1)
        t = floor_hour(w.copy_start) + timedelta(minutes=7)
        while t < w.end:
            evs.append(Ev(t, 2, "prices", None))
            t += timedelta(hours=w.scale.prices_every_hours)
        joined = w.copy_start - timedelta(days=1, hours=2)
        evs.append(Ev(joined, 4, "investors", None))
        for who, handles in FOLLOWS:  # a strategy is followed once it exists
            for handle in handles:
                at = max(joined, w.anchor_at[handle] + timedelta(hours=2))
                evs.append(Ev(at + timedelta(minutes=1), 5, "follow", (who, handle)))
        for who, handle, label in WATCHES:  # a watch exists before it is tailed
            at = max(joined, w.anchor_at[handle] + timedelta(hours=2))
            evs.append(Ev(at + timedelta(minutes=2), 5, "watch", (who, handle, label)))
        for n, (who, handle, dollars, dd, days) in enumerate(ALLOCATIONS):
            back = min(days, w.scale.copy_days) - n * 0.05
            at = w.end - timedelta(days=back) + timedelta(minutes=13 * n)
            evs.append(Ev(at, 5, "allocate", (who, handle, dollars, dd)))
        evs.append(Ev(w.end, 9, "final", None))
        return sorted(evs)

    def run(self) -> Summary:
        started = time.monotonic()
        evs = self.events()
        self.log(
            f"simulating {self.w.start:%Y-%m-%d} -> {self.w.end:%Y-%m-%d %H:%M} UTC "
            f"({len(evs)} events, {self.w.scale.name} scale)"
        )
        last_day = None
        for i, ev in enumerate(evs):
            self.travel(ev.at)
            horizon = evs[i + 1].at if i + 1 < len(evs) else ev.at + timedelta(hours=1)
            getattr(self, f"on_{ev.kind}")(ev.arg)
            self.drain(horizon)
            if ev.at.date() != last_day and ev.at.day == 1:
                self.log(f"  {ev.at:%Y-%m-%d}  jobs run so far: {self.summary.jobs_run}")
            last_day = ev.at.date()
        self.drain(_now() + timedelta(hours=1))
        self.collect_failures()
        self.summary.seconds = time.monotonic() - started
        return self.summary

    # ---- events --------------------------------------------------------------------------

    def _user(self, db: Session, local: str, display: str, **flags: bool) -> User:
        user = User(
            email=f"{local}@{EMAIL_DOMAIN}",
            password_hash=hash_password(self.password),
            display_name=display,
            is_demo=True,
            email_verified_at=_now(),
            **flags,
        )
        db.add(user)
        db.flush()
        return user

    def _system_user(self, db: Session) -> User:
        """The demo's system user (invariant 7: is_demo too). Made here, before
        anything could call watchlist.system_user, so that is the one it finds."""
        user = User(
            email=f"{SYSTEM_LOCAL}@{EMAIL_DOMAIN}",
            password_hash=UNUSABLE_PASSWORD_HASH,
            display_name="Koine watchlist",
            is_demo=True,
            is_system=True,
        )
        db.add(user)
        db.flush()
        return user

    def on_connect(self, c: CreatorSpec) -> None:
        """What venues.connect_account leaves behind, at the creator's anchor:
        the user, the account with its anchor vector, the sealed credential,
        the strategy; then the backfill the worker's tick would queue."""
        book = self.w.by_handle[c.handle]
        profile = registry.get_profile(c.venue)
        now = _now()
        with self.sf() as db:
            user = (
                self._system_user(db)
                if c.watch
                else self._user(db, c.handle, c.display_name, is_house=c.house)
            )
            account = VenueAccount(
                user_id=user.id,
                venue=c.venue,
                venue_env=book.venue_env,
                key_id=book.key_id,
                status=VenueAccountStatus.ACTIVE,
                anchor_balance_micros=book.anchor_cash_atoms * 10 ** (6 - book.cash_dp)
                if book.cash_dp <= 6
                else None,
                anchor_at=now,
                anchor_positions=[],
                claimed_at=None if c.watch else now,
                is_watch=c.watch,
            )
            connector = registry.build_connector(c.venue, book.venue_env, book.key_id, None)
            apply_evidence(account, connector.prove_read_only())
            db.add(account)
            db.flush()
            if profile.instrument.value != "binary":
                db.add(
                    AnchorBalance(
                        venue_account_id=account.id,
                        asset=book.cash_asset,
                        atoms=book.anchor_cash_atoms,
                        decimals=book.cash_dp,
                        as_of=now,
                    )
                )
            if profile.needs_secret and not c.watch:
                credentials.store(
                    db,
                    account,
                    f"demo-credential-{c.handle}-not-a-real-key",
                    kind=profile.credential,
                    actor_user_id=user.id,
                )
            if c.watch:  # what watchlist._track leaves behind
                strategy = Strategy(
                    user_id=user.id,
                    venue_account_id=account.id,
                    name=f"{registry.get_profile(c.venue).display_name} {short_address(c.key_id)}",
                    slug="pending",
                    kind=StrategyKind.WATCH,
                    public=False,
                )
                db.add(strategy)
                db.flush()
                strategy.slug = f"watch-{strategy.id}"
                db.commit()
                self.accounts[c.handle] = account.id
                self.strategies[c.handle] = strategy.id
                self._backfill(c, profile)
                return
            strategy = Strategy(
                user_id=user.id,
                venue_account_id=account.id,
                name=c.strategy,
                # A real connect appends the strategy id (venues.connect_account)
                # to keep slugs unique; the seeded names are unique already, so
                # the demo gets the clean slug a creator would choose.
                slug=slugify(c.strategy),
                description=c.description,
                style_tags=c.tags,
                public=True,
                listed_at=now,
            )
            db.add(strategy)
            db.commit()
            self.accounts[c.handle] = account.id
            self.strategies[c.handle] = strategy.id
            role = f"creator ({c.venue}{', run by Koine' if c.house else ''})"
            self.summary.logins.append((user.email, role))
        self._backfill(c, profile)

    def _backfill(self, c: CreatorSpec, profile) -> None:
        paged = profile.instrument.value != "binary"
        self.enqueue(
            JobKind.BACKFILL if paged else JobKind.SYNC, account_id=self.accounts[c.handle]
        )

    def on_sync(self, handle: str) -> None:
        self.enqueue(JobKind.SYNC, account_id=self.accounts[handle])

    def _pricing_venues(self) -> list[str]:
        with self.sf() as db:
            return worker.pricing_venues_in_use(db)

    def on_nightly(self, day: date) -> None:
        in_copy = midnight(day) >= self.w.copy_start - timedelta(days=1)
        if in_copy:
            worker.tick_nightly(self.sf)  # exactly the worker's nightly set
            return
        # Before any follower exists: closes every night (signals value the
        # creator's equity at the previous close) and a periodic reconcile.
        for venue in self._pricing_venues():
            self.enqueue(
                JobKind.PRICES_DAILY,
                payload={"venue": venue},
                key=f"prices_daily:{venue}:{day.isoformat()}",
            )
        if day.toordinal() % self.w.scale.reconcile_every_days == 0:
            for account_id in self.accounts.values():
                self.enqueue(
                    JobKind.RECONCILE, account_id=account_id, key=f"reconcile:{account_id}:{day}"
                )

    def on_prices(self, _arg) -> None:
        worker.tick_prices(self.sf)
        self.enqueue(JobKind.MIRROR, key=f"mirror:global:{_now().isoformat()}")

    def on_investors(self, _arg) -> None:
        with self.sf() as db:
            for local, display in INVESTORS:
                user = self._user(db, local, display)
                self.users[local] = user.id
                self.summary.logins.insert(0, (user.email, "investor (paper portfolio)"))
            db.commit()

    def on_follow(self, arg) -> None:
        who, handle = arg
        with self.sf() as db:
            db.add(Follow(user_id=self.users[who], strategy_id=self.strategies[handle]))
            db.commit()

    def on_watch(self, arg) -> None:
        who, handle, label = arg
        with self.sf() as db:
            db.add(Watch(user_id=self.users[who], strategy_id=self.strategies[handle], label=label))
            db.commit()

    def on_allocate(self, arg) -> None:
        who, handle, dollars, dd = arg
        with self.sf() as db:
            user = db.get(User, self.users[who])
            strategy = db.get(Strategy, self.strategies[handle])
            tails.create_allocation(
                db,
                user,
                tails.AllocationIn(
                    strategy_slug=strategy.slug, allocation=f"{dollars}.00", drawdown_limit_bps=dd
                ),
            )

    def on_final(self, _arg) -> None:
        """Today: a last sync of everyone, then the nightly set (reconcile,
        closes, quotes, stats and allocation valuations) under fresh keys."""
        stamp = _now().isoformat()
        for account_id in self.accounts.values():
            self.enqueue(JobKind.SYNC, account_id=account_id)
        self.drain(_now() + timedelta(minutes=5))
        for account_id in self.accounts.values():
            self.enqueue(
                JobKind.RECONCILE, account_id=account_id, key=f"final:reconcile:{account_id}"
            )
        for venue in self._pricing_venues():
            self.enqueue(JobKind.PRICES_DAILY, payload={"venue": venue}, key=f"final:pd:{venue}")
        self.enqueue(JobKind.PRICES, key=f"final:prices:{stamp}")
        self.enqueue(JobKind.MIRROR, key=f"final:mirror:{stamp}")
        self.drain(_now() + timedelta(minutes=10))
        self.enqueue(JobKind.STATS, key=f"final:stats:{stamp}")

    def collect_failures(self) -> None:
        with self.sf() as db:
            for job in db.scalars(select(Job).where(Job.last_error.is_not(None))).all():
                self.summary.failed_jobs.append(
                    f"{job.kind} account={job.venue_account_id} status={job.status} "
                    f"attempts={job.attempts}: {(job.last_error or '')[:300]}"
                )


# ---- entry points ---------------------------------------------------------------------------


class Refused(SystemExit):
    pass


def check_target(sf: sessionmaker, url: str, *, allow_any_name: bool = False) -> None:
    """Refuse to seed anything that could be a real database (invariant 7)."""
    settings = get_settings()
    if not settings.demo_mode:
        raise Refused("seed_demo: DEMO_MODE is off; seeded rows would never be shown. Refusing.")
    name = make_url(url).database or ""
    if not allow_any_name and "demo" not in name.lower():
        raise Refused(
            f"seed_demo: database {name!r} is not a demo database (its name must contain "
            "'demo', e.g. alphamarket_demo). Refusing."
        )
    if not settings.vault_key and settings.key_wrapper == "local":
        raise Refused("seed_demo: VAULT_KEY is not set (demo credentials are vault-sealed).")
    with sf() as db:
        try:
            real = db.scalar(select(func.count(User.id)).where(User.is_demo.is_(False)))
        except Exception as exc:  # noqa: BLE001  (schema not migrated yet: nothing real in it)
            db.rollback()
            logger.info("user check skipped: %s", type(exc).__name__)
            return
    if real:
        raise Refused(f"seed_demo: the database holds {real} non-demo user(s). Refusing.")


def already_seeded(sf: sessionmaker) -> bool:
    with sf() as db:
        return db.scalar(select(func.count(User.id)).where(User.is_demo.is_(True))) > 0


def reset_schema(url: str) -> None:
    """Drop and recreate the public schema, then migrate to head. Rows cannot
    be deleted from append-only tables, so a demo reset is a fresh schema."""
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    migrate(url)


def migrate(url: str) -> None:
    from alembic import command
    from alembic.config import Config

    settings = get_settings()
    original = settings.database_url
    settings.database_url = url
    try:
        cfg = Config()
        cfg.set_main_option("script_location", str(ROOT / "alembic"))
        command.upgrade(cfg, "head")
    finally:
        settings.database_url = original


LEADERBOARD_PERIODS = ("day", "week", "month", "all")


def _hyperliquid_board_address(i: int) -> str:
    """Invented ("c0de" repeated, never a real person's), plus the seeded
    watched address at rank 3 so the investor sees one row on their watchlist."""
    return WATCH_ADDRESS if i == 2 else "0x" + f"c0de{i:04x}" + "c0de" * 8


# Seeded discover boards: venue -> the synthetic address of row i. Each venue
# branch adds its line inside its own marked block (onchain_perps_plan S8).
LEADERBOARD_SEEDS = {
    "hyperliquid": _hyperliquid_board_address,
    # ---- venue-lighter ----
    # ---- end venue-lighter ----
    # ---- venue-pacifica ----
    # ---- end venue-pacifica ----
    # ---- venue-gmx_arbitrum ----
    # ---- end venue-gmx_arbitrum ----
    # ---- venue-nado ----
    # ---- end venue-nado ----
}


def seed_leaderboards(db, fetched_at: datetime) -> None:
    """Synthetic leaderboard snapshots for /watch/discover (it needs
    WATCH_DISCOVER_ENABLED=true to show), one board per period for each venue
    in LEADERBOARD_SEEDS. Written through the real money and address path
    (discover.snapshot_row)."""
    for venue, address_of in LEADERBOARD_SEEDS.items():
        _seed_board(db, venue, address_of, fetched_at)


def _seed_board(db, venue: str, address_of, fetched_at: datetime) -> None:
    from alphamarket import discover
    from alphamarket.connectors.base import LeaderboardDetails, LeaderboardEntry

    scale = {"day": 0.012, "week": 0.06, "month": 0.22, "all": 1.0}
    for period in LEADERBOARD_PERIODS:
        rows = []
        for i in range(24):
            address = address_of(i)
            pnl = 3_100_000 * scale[period] * (0.88**i)
            value = 1_500_000 * (0.9**i) + 40_000
            rows.append(
                discover.snapshot_row(
                    venue,
                    LeaderboardEntry(
                        address=address,
                        rank=i + 1,
                        pnl=f"{pnl:.2f}",
                        roi=f"{pnl / value * (1 + (i % 5) * 0.15):.4f}",
                        volume=f"{value * (6 + i % 7):.2f}",
                        account_value=f"{value:.2f}",
                    ),
                )
            )
        discover.store_snapshot(db, venue, period, rows, fetched_at)
        # Polymarket: pnl from the board, the rest from wallet stats (invented addresses)
        pm_rows = []
        for i in range(24):
            pnl = 1_200_000 * scale[period] * (0.87**i)
            row = discover.snapshot_row(
                "polymarket",
                LeaderboardEntry(
                    address="0x" + f"b0b0{i:04x}" + "b0b0" * 8, rank=i + 1, pnl=f"{pnl:.2f}"
                ),
            )
            discover.merge_details(
                row,
                LeaderboardDetails(
                    volume=f"{pnl * (4 + i % 6):.2f}",
                    # lifetime figures scale with all-time pnl, not the period's
                    positions_value=f"{1_200_000 * 0.87**i * 0.05 * (1 + i % 4):.2f}",
                    markets_traded=60 + i * 37 % 900,
                    biggest_win=f"{1_200_000 * 0.87**i * 0.2:.2f}",
                    active_since=1_700_000_000 + i * 5_000_000,
                ),
            )
            pm_rows.append(row)
        discover.store_snapshot(db, "polymarket", period, pm_rows, fetched_at)


def seed(
    sf: sessionmaker,
    *,
    scale: Scale = FULL,
    rng_seed: int = DEFAULT_SEED,
    end: datetime | None = None,
    password: str = DEFAULT_PASSWORD,
    log: Callable[[str], None] = print,
) -> Summary:
    """Generate the world and run it through the pipeline. The caller has
    checked the target (check_target) and migrated it."""
    import time_machine

    end = end or floor_hour(datetime.now(UTC))
    world = World(end, scale, rng_seed)
    world.generate()
    log(
        "generated "
        + ", ".join(
            f"{h}: {len(b.fills)} fills/{len(b.events) + len(b.settlements)} events"
            for h, b in world.by_handle.items()
        )
    )
    with (
        network_blocked(),
        sim_registry(world),
        time_machine.travel(world.start - timedelta(hours=1), tick=False) as traveller,
    ):
        summary = Sim(sf, world, traveller, password, log).run()
    from alphamarket import public

    with sf() as db:
        seed_leaderboards(db, end)
        db.commit()
    with sf() as db:
        summary.strategies = public.list_visible(db, include_incubating=True)
    return summary


def report(summary: Summary, password: str, log=print) -> None:
    log("")
    log(f"seeded in {summary.seconds:.0f}s, {summary.jobs_run} jobs run")
    for r in summary.strategies:
        log(
            f"  {r['name']:<32} {r['venue_label'] or '':<26} return {r['total_return']:+8.2%}  "
            f"max dd {r['max_drawdown']:6.2%}  {r['track_days']:>3}d  "
            f"{'reconciled' if r['reconciled'] else 'NOT reconciled'}"
        )
    if summary.failed_jobs:
        log(f"{len(summary.failed_jobs)} job(s) recorded an error:")
        for line in summary.failed_jobs[:20]:
            log(f"  - {line}")
    log("")
    log(f"logins (password for all: {password}):")
    for email, role in summary.logins:
        log(f"  {email:<28} {role}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reset", action="store_true", help="drop and recreate the schema first")
    parser.add_argument("--scale", choices=sorted(SCALES), default="full")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="RNG seed (deterministic)")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="password of every demo login")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    url = settings.sqlalchemy_url
    # A demo database can lose its last commits in a crash (re-seed it); not
    # waiting for each WAL flush makes the many small job commits much faster.
    engine = create_engine(url, connect_args={"options": "-c synchronous_commit=off"})
    sf = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        check_target(sf, url)
        if args.reset:
            engine.dispose()
            reset_schema(url)
        else:
            migrate(url)
            if already_seeded(sf):
                print("seed_demo: this database is already seeded; run again with --reset.")
                return 1
        summary = seed(
            sf,
            scale=SCALES[args.scale],
            rng_seed=args.seed,
            password=args.password,
            log=lambda line: print(line, flush=True),
        )
    except Refused as exc:
        print(exc.code if isinstance(exc.code, str) else exc, file=sys.stderr)
        return 2
    report(summary, args.password)
    return 1 if summary.failed_jobs else 0


if __name__ == "__main__":
    sys.exit(main())

"""How a watched trader trades (copy strategies, step 6 / B5). PURE: no DB, no
I/O, no clock. Plain dataclasses in, a `TraderProfile` out.

Private by design. A profile describes the habits of a public address that
somebody put on their own watchlist; nothing here is shown on a public page
(`watch_pages` renders it for the watcher, `docs/watchlist_help.md` defines
every figure). Nothing is verified or scored: these are descriptions, not
performance claims.

Rules shared by every metric:

* A metric that cannot be computed from the inputs is None, never a guess: no
  fills, no equity series covering the entry, no funding data, no perp fills,
  no binary fills. A whole section is None when none of its inputs exist.
* Money is exact. Quantities are `Fraction`s built from atoms and their own
  `decimals` (atoms of different scales are never added: each fill is
  converted to an exact unit quantity first); notionals and funding are
  integer micro-dollars; per-fill pieces of a notional are `Fraction`s. Floats
  appear only in the final ratios, percentages and medians of seconds.
* All timestamps are timezone-aware (invariant 2); weekday/hour bins are UTC.
* Positions are FIFO per instrument (binary: per contract side of a market).
  Spot, equity and binary positions are long-only: a sell with no inventory
  (history from before the first ingested fill) is ignored, never turned into
  a short. Perp positions may be short and may flip.

Definitions (the same ones, in prose with formulas, in docs/watchlist_help.md):

holding period
    A buy/sell sequence is matched first-in-first-out against the open lots of
    its instrument. Every lot, or part of a lot, that a fill (or, for binary
    markets, a settlement) closes is one round trip piece, lasting from the
    time that lot was opened to the time of the closing fill. A partial close
    leaves the rest of the lot open for a later piece. A perp fill that
    crosses zero closes all lots, then opens a new lot in the other direction
    with the remainder (a flip). Pieces are counted in five buckets with
    lower-inclusive bounds: <1h, 1-24h, 1-7d, 7-30d, >30d. Lots still open are
    not counted.

trade frequency
    fills per active day = fills / distinct UTC days with a fill; fills per
    week = fills / (span_days / 7) where span_days is the number of UTC
    calendar days from the first to the last fill, both included.

sizing
    An *entry* is a fill that opens a position from flat (a flip's remainder
    counts). Its size is the notional it opened, as a share of the trader's
    equity at the close of the last complete UTC day before the fill (the
    daily series Koine already stores; entries before the series starts, or
    with equity <= 0, are left out). Median, and p90 by nearest rank:
    sorted[ceil(0.9 n) - 1]. A *scale-in* is a fill that adds to an open
    position; a *scale-out* is a fill that reduces a position without closing
    it. A closing fill and a flip are neither.

perps
    leverage after a perp fill = sum over perp instruments of |position| x
    last fill price / equity (same equity rule as sizing); average and max
    over perp fills. Long/short skew counts only notional that opens or adds
    to a position: long = buys, short = sells; long share = long / (long +
    short). Funding paid = minus the sum of the funding legs (positive: the
    trader paid; negative: received).

prediction markets
    Each buy is an entry, bucketed by its price in cents: 0-20, 20-40, 40-60,
    60-80, 80-100 (lower-inclusive, the last bucket includes 100). FIFO
    matching tells how much of each entry was sold before settlement and how
    much was still held when the market settled. Held-to-settlement share =
    held / (held + sold early), by quantity. Win rate = quantity whose side
    matches the settled result / quantity held to a yes/no result.

copy feasibility
    `copy_delay_seconds` is how far behind a follower of this watch is: the
    tailed-watch poll interval (settings.ingest_interval_seconds, passed in;
    this module reads no settings). A trade opened and closed faster than that
    cannot be copied. Over the closed round-trip pieces of the holding-period
    metric: the share whose holding period is shorter than the delay (strictly),
    by closed notional (each piece counted at its entry price: quantity x the
    lot's venue notional per unit) and by count. None when nothing has closed.
    Also the median gap in seconds between consecutive trades, where fills that
    share an order id are one trade at the time of the first of them (without
    an order id every fill is a trade).
"""

from bisect import bisect_left
from collections import deque
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction
from math import ceil

from alphamarket.connectors.base import InstrumentType
from alphamarket.money import MAX_DECIMALS
from alphamarket.valuation import parse_price

MIN_FILLS = 10
"""Fewer fills than this and the page shows an empty state: every figure here
would rest on a handful of trades."""

HOLD_BUCKETS: tuple[tuple[str, int | None], ...] = (
    ("<1h", 3_600),
    ("1-24h", 86_400),
    ("1-7d", 7 * 86_400),
    ("7-30d", 30 * 86_400),
    (">30d", None),
)
"""(label, exclusive upper bound in seconds); the last bucket has none."""

PRICE_BUCKETS: tuple[str, ...] = ("0-20", "20-40", "40-60", "60-80", "80-100")
"""Entry price in cents, 20 wide."""

TOP_N = 5
LONG_ONLY = frozenset({InstrumentType.SPOT, InstrumentType.EQUITY, InstrumentType.BINARY})


# ---- inputs -----------------------------------------------------------------------


@dataclass(frozen=True)
class FillIn:
    """One fill. `price` is a decimal string (binary: dollars per contract,
    0..1); `notional_micros` the venue's gross value, unsigned. `outcome` is
    "yes"/"no" for binary fills; `order_id` groups partial fills of one order."""

    at: datetime
    instrument: str
    kind: InstrumentType
    action: str  # "buy" | "sell"
    qty_atoms: int
    qty_decimals: int
    price: str
    notional_micros: int
    outcome: str | None = None
    order_id: str | None = None

    def __post_init__(self) -> None:
        _require_aware(self.at)
        if self.action not in ("buy", "sell"):
            raise ValueError(f"action must be buy or sell, got {self.action!r}")
        if not isinstance(self.qty_atoms, int) or self.qty_atoms <= 0:
            raise ValueError("qty_atoms must be a positive integer")
        if not isinstance(self.qty_decimals, int) or not 0 <= self.qty_decimals <= MAX_DECIMALS:
            raise ValueError(f"qty_decimals must be within 0..{MAX_DECIMALS}")
        if not isinstance(self.notional_micros, int) or self.notional_micros < 0:
            raise ValueError("notional_micros must be a non-negative integer")
        InstrumentType(self.kind)
        parse_price(self.price)

    @property
    def qty(self) -> Fraction:
        return Fraction(self.qty_atoms, 10**self.qty_decimals)


@dataclass(frozen=True)
class SettlementIn:
    """A binary market settling: every open lot in `instrument` closes at `at`.
    `result` is the settled outcome ("yes"/"no"; anything else is a void or
    unknown resolution and counts as held but neither a win nor a loss)."""

    at: datetime
    instrument: str
    result: str

    def __post_init__(self) -> None:
        _require_aware(self.at)


@dataclass(frozen=True)
class EquityIn:
    """End-of-day (UTC) equity in micros, the series Koine stores."""

    day: date
    equity_micros: int


@dataclass(frozen=True)
class FundingIn:
    """One funding payment in micros, ledger-signed: + received, - paid."""

    at: datetime
    amount_micros: int

    def __post_init__(self) -> None:
        _require_aware(self.at)


def _require_aware(ts: datetime) -> None:
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware (invariant 2)")


# ---- outputs ----------------------------------------------------------------------


@dataclass(frozen=True)
class HoldingProfile:
    buckets: tuple[tuple[str, int], ...]  # (label, round-trip pieces)
    n_round_trips: int
    median_seconds: float


@dataclass(frozen=True)
class FrequencyProfile:
    n_fills: int
    active_days: int
    span_days: int
    per_active_day: float
    per_week: float


@dataclass(frozen=True)
class InstrumentShare:
    instrument: str
    kind: str
    notional_micros: int
    share: float


@dataclass(frozen=True)
class InstrumentsProfile:
    top: tuple[InstrumentShare, ...]
    n_instruments: int
    hhi: float  # sum of squared notional shares, 1/n (even) .. 1 (one instrument)


@dataclass(frozen=True)
class SizingProfile:
    n_entries: int
    n_sized: int  # entries whose equity was known
    median_share: float | None
    p90_share: float | None
    scale_ins: int
    scale_outs: int


@dataclass(frozen=True)
class PerpProfile:
    n_fills: int
    avg_leverage: float | None
    max_leverage: float | None
    long_notional_micros: int
    short_notional_micros: int
    long_share: float | None
    funding_paid_micros: int | None


@dataclass(frozen=True)
class PriceBucket:
    label: str
    n_entries: int
    held_share: float | None
    win_rate: float | None
    decided_qty: float  # contracts held to a yes/no result: the win-rate denominator


@dataclass(frozen=True)
class BinaryProfile:
    n_entries: int
    held_to_settlement: float | None
    buckets: tuple[PriceBucket, ...]


@dataclass(frozen=True)
class CopyProfile:
    copy_delay_seconds: int
    n_trades: int
    median_gap_seconds: float | None  # needs two trades
    n_round_trips: int
    n_too_fast: int  # pieces held for less than the delay
    too_fast_count_share: float | None  # None: nothing closed
    too_fast_notional_share: float | None  # None: nothing closed, or no notional


@dataclass(frozen=True)
class TraderProfile:
    n_fills: int
    enough_history: bool  # n_fills >= MIN_FILLS
    first_at: datetime | None = None
    last_at: datetime | None = None
    holding: HoldingProfile | None = None
    frequency: FrequencyProfile | None = None
    heatmap: tuple[tuple[int, ...], ...] | None = None  # [weekday Mon=0][hour UTC] fill counts
    instruments: InstrumentsProfile | None = None
    sizing: SizingProfile | None = None
    perps: PerpProfile | None = None
    binary: BinaryProfile | None = None
    copy: CopyProfile | None = None


# ---- FIFO engine ------------------------------------------------------------------


@dataclass
class _Lot:
    qty: Fraction  # magnitude, > 0
    open_at: datetime
    bucket: int | None  # binary entry price bucket
    outcome: str | None
    unit_micros: Fraction  # the opening fill's notional per unit of quantity


@dataclass(frozen=True)
class _Piece:
    qty: Fraction
    opened_at: datetime
    closed_at: datetime
    bucket: int | None
    outcome: str | None
    settlement_result: str | None  # None: closed by a trade
    notional_micros: Fraction  # at the lot's entry price


class _Book:
    """Open lots of one position key, FIFO. `side` is +1 long, -1 short."""

    def __init__(self) -> None:
        self.lots: deque[_Lot] = deque()
        self.side = 0

    def size(self) -> Fraction:
        return sum((lot.qty for lot in self.lots), Fraction(0))

    def close_all(self, at: datetime, result: str | None) -> list[_Piece]:
        pieces = [
            _Piece(
                lot.qty, lot.open_at, at, lot.bucket, lot.outcome, result, lot.qty * lot.unit_micros
            )
            for lot in self.lots
        ]
        self.lots.clear()
        self.side = 0
        return pieces


@dataclass
class _Step:
    """What one fill did to its book."""

    fill: FillIn
    opened: Fraction  # quantity added in the fill's direction (entry, scale-in or flip)
    kind: str  # entry | scale_in | scale_out | close | flip | ignored
    pieces: list[_Piece]
    gross_perp: Fraction | None  # gross perp notional after this fill, USD


def _bucket(price: Fraction) -> int:
    if not 0 <= price <= 1:
        raise ValueError(f"binary price {price} outside 0..1 dollars")
    return min(int(price * 100 // 20), len(PRICE_BUCKETS) - 1)


def _key(f: FillIn) -> tuple[str, str | None]:
    return (f.instrument, f.outcome if f.kind is InstrumentType.BINARY else None)


def _apply(book: _Book, f: FillIn) -> tuple[Fraction, str, list[_Piece]]:
    """Apply one fill to its book: (quantity opened, kind, closed pieces)."""
    direction = 1 if f.action == "buy" else -1
    bucket = _bucket(parse_price(f.price)) if f.kind is InstrumentType.BINARY else None
    short_ok = f.kind not in LONG_ONLY
    unit = Fraction(f.notional_micros) / f.qty
    if book.side == 0 and direction == -1 and not short_ok:
        return Fraction(0), "ignored", []  # sells of inventory from before the history
    if book.side in (0, direction):
        was_flat = book.side == 0
        book.side = direction
        book.lots.append(_Lot(f.qty, f.at, bucket, f.outcome, unit))
        return f.qty, ("entry" if was_flat else "scale_in"), []
    remaining = f.qty
    pieces: list[_Piece] = []
    while remaining > 0 and book.lots:
        lot = book.lots[0]
        take = min(lot.qty, remaining)
        pieces.append(
            _Piece(take, lot.open_at, f.at, lot.bucket, lot.outcome, None, take * lot.unit_micros)
        )
        lot.qty -= take
        remaining -= take
        if lot.qty == 0:
            book.lots.popleft()
    if book.lots:
        return Fraction(0), "scale_out", pieces
    book.side = 0
    if remaining > 0 and short_ok:  # crossed zero: the remainder opens the other way
        book.side = direction
        book.lots.append(_Lot(remaining, f.at, bucket, f.outcome, unit))
        return remaining, "flip", pieces
    return Fraction(0), ("close" if pieces else "ignored"), pieces


def _walk(fills: list[FillIn], settlements: list[SettlementIn]) -> tuple[list[_Step], list[_Piece]]:
    """Fills and settlements in time order (a settlement after fills at the same
    instant). Returns one step per fill and the pieces settlements closed."""
    events: list[tuple[datetime, int, int, FillIn | SettlementIn]] = [
        (f.at, 0, i, f) for i, f in enumerate(fills)
    ] + [(s.at, 1, i, s) for i, s in enumerate(settlements)]
    events.sort(key=lambda e: e[:3])
    books: dict[tuple[str, str | None], _Book] = {}
    last_price: dict[str, Fraction] = {}
    perp_books: dict[str, _Book] = {}
    steps: list[_Step] = []
    settled: list[_Piece] = []
    for _, _, _, ev in events:
        if isinstance(ev, SettlementIn):
            for (instrument, _outcome), book in books.items():
                if instrument == ev.instrument and book.lots:
                    settled.extend(book.close_all(ev.at, ev.result))
            continue
        book = books.setdefault(_key(ev), _Book())
        opened, kind, pieces = _apply(book, ev)
        gross = None
        if ev.kind is InstrumentType.PERP:
            perp_books[ev.instrument] = book
            last_price[ev.instrument] = parse_price(ev.price)
            gross = sum(
                (b.size() * last_price[name] for name, b in perp_books.items()), Fraction(0)
            )
        steps.append(_Step(ev, opened, kind, pieces, gross))
    return steps, settled


# ---- helpers ----------------------------------------------------------------------


def _median(values: list[Fraction]) -> Fraction:
    xs = sorted(values)
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2


def _p90(values: list[Fraction]) -> Fraction:
    xs = sorted(values)
    return xs[ceil(Fraction(9, 10) * len(xs)) - 1]


def _seconds(delta: timedelta) -> Fraction:
    return Fraction(delta // timedelta(microseconds=1), 1_000_000)


class _Equity:
    """Equity at the close of the last complete UTC day before an instant."""

    def __init__(self, series: list[EquityIn]) -> None:
        ordered = sorted(series, key=lambda p: p.day)
        self.days = [p.day for p in ordered]
        self.values = [p.equity_micros for p in ordered]

    def before(self, at: datetime) -> int | None:
        i = bisect_left(self.days, _utc(at).date())  # days strictly before the fill's UTC day
        if i == 0 or self.values[i - 1] <= 0:
            return None
        return self.values[i - 1]


def _utc(ts: datetime) -> datetime:
    return ts.astimezone(UTC)


# ---- metrics ----------------------------------------------------------------------


def _holding(pieces: list[_Piece]) -> HoldingProfile | None:
    if not pieces:
        return None
    counts = [0] * len(HOLD_BUCKETS)
    for p in pieces:
        seconds = _seconds(p.closed_at - p.opened_at)
        for i, (_, upper) in enumerate(HOLD_BUCKETS):
            if upper is None or seconds < upper:
                counts[i] += 1
                break
    median = _median([_seconds(p.closed_at - p.opened_at) for p in pieces])
    return HoldingProfile(
        buckets=tuple((label, n) for (label, _), n in zip(HOLD_BUCKETS, counts, strict=True)),
        n_round_trips=len(pieces),
        median_seconds=float(median),
    )


def _frequency(fills: list[FillIn]) -> FrequencyProfile:
    days = sorted({_utc(f.at).date() for f in fills})
    span = (days[-1] - days[0]).days + 1
    return FrequencyProfile(
        n_fills=len(fills),
        active_days=len(days),
        span_days=span,
        per_active_day=len(fills) / len(days),
        per_week=float(Fraction(len(fills) * 7, span)),
    )


def _heatmap(fills: list[FillIn]) -> tuple[tuple[int, ...], ...]:
    grid = [[0] * 24 for _ in range(7)]
    for f in fills:
        t = _utc(f.at)
        grid[t.weekday()][t.hour] += 1
    return tuple(tuple(row) for row in grid)


def _instruments(fills: list[FillIn]) -> InstrumentsProfile | None:
    totals: dict[str, int] = {}
    kinds: dict[str, str] = {}
    for f in fills:
        totals[f.instrument] = totals.get(f.instrument, 0) + f.notional_micros
        kinds[f.instrument] = str(f.kind)
    grand = sum(totals.values())
    if grand == 0:
        return None
    shares = {name: Fraction(total, grand) for name, total in totals.items()}
    ranked = sorted(totals, key=lambda name: (-totals[name], name))
    return InstrumentsProfile(
        top=tuple(
            InstrumentShare(name, kinds[name], totals[name], float(shares[name]))
            for name in ranked[:TOP_N]
        ),
        n_instruments=len(totals),
        hhi=float(sum(s * s for s in shares.values())),
    )


def _opened_notional(step: _Step) -> Fraction:
    """The share of the fill's venue notional that opened or added to a position."""
    f = step.fill
    return Fraction(f.notional_micros) * step.opened / f.qty


def _sizing(steps: list[_Step], equity: _Equity) -> SizingProfile | None:
    if not steps:
        return None
    shares: list[Fraction] = []
    entries = scale_ins = scale_outs = 0
    for s in steps:
        if s.kind in ("entry", "flip"):
            entries += 1
            eq = equity.before(s.fill.at)
            if eq is not None:
                shares.append(_opened_notional(s) / eq)
        elif s.kind == "scale_in":
            scale_ins += 1
        elif s.kind == "scale_out":
            scale_outs += 1
    return SizingProfile(
        n_entries=entries,
        n_sized=len(shares),
        median_share=float(_median(shares)) if shares else None,
        p90_share=float(_p90(shares)) if shares else None,
        scale_ins=scale_ins,
        scale_outs=scale_outs,
    )


def _perps(
    steps: list[_Step], equity: _Equity, funding: list[FundingIn] | None
) -> PerpProfile | None:
    perp = [s for s in steps if s.fill.kind is InstrumentType.PERP]
    if not perp:
        return None
    leverages = []
    for s in perp:
        eq = equity.before(s.fill.at)
        if eq is not None and s.gross_perp is not None:
            leverages.append(s.gross_perp * 1_000_000 / eq)
    long_n = sum(
        (_opened_notional(s) for s in perp if s.fill.action == "buy" and s.opened), Fraction(0)
    )
    short_n = sum(
        (_opened_notional(s) for s in perp if s.fill.action == "sell" and s.opened), Fraction(0)
    )
    both = long_n + short_n
    return PerpProfile(
        n_fills=len(perp),
        avg_leverage=float(sum(leverages, Fraction(0)) / len(leverages)) if leverages else None,
        max_leverage=float(max(leverages)) if leverages else None,
        long_notional_micros=int(long_n),
        short_notional_micros=int(short_n),
        long_share=float(long_n / both) if both else None,
        funding_paid_micros=-sum(p.amount_micros for p in funding) if funding is not None else None,
    )


def _binary(steps: list[_Step], settled: list[_Piece]) -> BinaryProfile | None:
    buys = [s for s in steps if s.fill.kind is InstrumentType.BINARY and s.fill.action == "buy"]
    if not buys:
        return None
    n_buckets = len(PRICE_BUCKETS)
    entries = [0] * n_buckets
    early = [Fraction(0)] * n_buckets
    held = [Fraction(0)] * n_buckets
    decided = [Fraction(0)] * n_buckets
    won = [Fraction(0)] * n_buckets
    for s in buys:
        entries[_bucket(parse_price(s.fill.price))] += 1
    sold = [p for s in steps if s.fill.kind is InstrumentType.BINARY for p in s.pieces]
    for p in sold:
        early[p.bucket] += p.qty
    for p in settled:
        if p.bucket is None:
            continue
        held[p.bucket] += p.qty
        if p.settlement_result in ("yes", "no"):
            decided[p.bucket] += p.qty
            if p.settlement_result == p.outcome:
                won[p.bucket] += p.qty

    def ratio(num: Fraction, den: Fraction) -> float | None:
        return float(num / den) if den else None

    buckets = tuple(
        PriceBucket(
            label=PRICE_BUCKETS[i],
            n_entries=entries[i],
            held_share=ratio(held[i], held[i] + early[i]),
            win_rate=ratio(won[i], decided[i]),
            decided_qty=float(decided[i]),
        )
        for i in range(n_buckets)
    )
    held_total, early_total = sum(held, Fraction(0)), sum(early, Fraction(0))
    return BinaryProfile(
        n_entries=len(buys),
        held_to_settlement=ratio(held_total, held_total + early_total),
        buckets=buckets,
    )


def _copy(fills: list[FillIn], pieces: list[_Piece], copy_delay_seconds: int) -> CopyProfile | None:
    first_of: dict[str, datetime] = {}
    times: list[datetime] = []
    for f in fills:
        if f.order_id is None:
            times.append(f.at)
        elif f.order_id not in first_of or f.at < first_of[f.order_id]:
            first_of[f.order_id] = f.at
    times = sorted([*times, *first_of.values()])
    gaps = [_seconds(b - a) for a, b in zip(times, times[1:], strict=False)]
    fast = [p for p in pieces if _seconds(p.closed_at - p.opened_at) < copy_delay_seconds]
    total = sum((p.notional_micros for p in pieces), Fraction(0))
    if not gaps and not pieces:
        return None
    return CopyProfile(
        copy_delay_seconds=copy_delay_seconds,
        n_trades=len(times),
        median_gap_seconds=float(_median(gaps)) if gaps else None,
        n_round_trips=len(pieces),
        n_too_fast=len(fast),
        too_fast_count_share=len(fast) / len(pieces) if pieces else None,
        too_fast_notional_share=(
            float(sum((p.notional_micros for p in fast), Fraction(0)) / total) if total else None
        ),
    )


# ---- entry point ------------------------------------------------------------------


def build_profile(
    fills: list[FillIn],
    settlements: list[SettlementIn] | None = None,
    equity: list[EquityIn] | None = None,
    funding: list[FundingIn] | None = None,
    *,
    copy_delay_seconds: int,
) -> TraderProfile:
    """The profile of one address. `funding` is None when the venue's funding
    payments are not available (the funding figure is then None, not 0)."""
    if not fills:
        return TraderProfile(n_fills=0, enough_history=False)
    ordered = sorted(fills, key=lambda f: f.at)
    steps, settled = _walk(ordered, list(settlements or []))
    series = _Equity(list(equity or []))
    pieces = [p for s in steps for p in s.pieces] + settled
    return TraderProfile(
        n_fills=len(ordered),
        enough_history=len(ordered) >= MIN_FILLS,
        first_at=ordered[0].at,
        last_at=ordered[-1].at,
        holding=_holding(pieces),
        frequency=_frequency(ordered),
        heatmap=_heatmap(ordered),
        instruments=_instruments(ordered),
        sizing=_sizing(steps, series),
        perps=_perps(steps, series, funding),
        binary=_binary(steps, settled),
        copy=_copy(ordered, pieces, copy_delay_seconds),
    )

"""Multi-asset valuation: holdings, perp entries, daily equity, flow-adjusted
returns and exposure (platform_plan 2.2, implementation_plan P1 item 5).

PURE functions over small input dataclasses: no DB, no I/O, no clock. The
ledger tables feed them at integration time; this module never imports the
models, so the two can evolve independently.

Units (invariant 1, amended by platform_plan D3):
- quantities are integer ATOMS at a stated `decimals` (one whole unit =
  10**decimals atoms), exactly as money.to_atoms parsed them;
- USD values are integer MICROS;
- closes are USD micros per ONE WHOLE unit (`PriceIn.usd_micros_per_unit`),
  or exactly the decimal `PriceIn.close` when given (wave2 D14);
- intermediate arithmetic is exact (int / Fraction); floats never appear.

Precision rule (connectors/base.py "Precision"): atoms of one asset are never
summed across scales. Every quantity of an asset is first rescaled up to the
largest `decimals` seen for that asset in the inputs (scaling up is exact).

THE rounding rule: each position's USD value (and each day's per-asset flow
value) is computed exactly and then truncated TOWARD ZERO to whole micros,
once, at the position. Equity is the exact integer sum of those position
values. So a position is never over-stated in either direction, and the
error of a day's equity is below 1 micro per position.

Documented for the public page in docs/methodology.md, sections "Asset
valuation" and "External flows (time-weighted return)"; every formula is
pinned by tests/test_valuation.py with hand-computed values.
"""

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from copy import copy
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from typing import Protocol

from alphamarket.connectors import registry
from alphamarket.connectors.base import EXTERNAL_FLOW_KINDS, LegKind
from alphamarket.money import MAX_DECIMALS, MICROS_PER_UNIT

PERP_SUFFIX = "-PERP"
"""Perp positions are the asset "<COIN>-PERP" (connectors/base.py asset codes)."""

CASH_AT_PAR: frozenset[str] = registry.CASH_AT_PAR
"""Assets valued at exactly 1.00 USD per unit, never looked up in prices
(USD, USDC, USDT and each venue's own codes for them: registry.CASH_AT_PAR)."""


# ---- validation helpers -------------------------------------------------------


def _require_int(value: object, what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what} must be an int, got {type(value).__name__}")


def _require_decimals(decimals: object) -> None:
    _require_int(decimals, "decimals")
    if not 0 <= decimals <= MAX_DECIMALS:  # type: ignore[operator]
        raise ValueError(f"decimals must be within 0..{MAX_DECIMALS}, got {decimals}")


def _require_aware(ts: datetime, what: str) -> None:
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware (invariant 2)")


def _utc_day(ts: datetime) -> date:
    return ts.astimezone(UTC).date()


def parse_price(value: str) -> Fraction:
    """A venue decimal price string as an exact Fraction. Strings only: a float
    price has already lost exactness, and an int is ambiguous about scale."""
    if not isinstance(value, str):
        raise TypeError(f"prices are decimal strings, got {type(value).__name__}")
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise ValueError(f"price {value!r} is not a finite decimal")
    return Fraction(parsed)


def _trunc_micros(value: Fraction) -> int:
    """THE rounding rule: truncate toward zero to whole micros (int() on a
    Fraction truncates toward zero: -333333.3 -> -333333, not -333334)."""
    return int(value)


# ---- inputs -------------------------------------------------------------------


@dataclass(frozen=True)
class LegIn:
    """One ledger leg: a signed change of `asset`, in atoms at `decimals`."""

    asset: str
    delta_atoms: int
    decimals: int
    kind: LegKind
    occurred_at: datetime
    instrument_symbol: str | None = None

    def __post_init__(self) -> None:
        _require_int(self.delta_atoms, "delta_atoms")
        _require_decimals(self.decimals)
        _require_aware(self.occurred_at, "occurred_at")
        LegKind(self.kind)


@dataclass(frozen=True)
class PriceIn:
    """UTC daily close of one WHOLE unit of `asset`, in USD micros. Closes are
    as traded (not split-adjusted): the ledger's SPLIT leg changes the
    quantity on the split day, and the close changes with it."""

    asset: str
    day: date
    usd_micros_per_unit: int
    # wave2 (D14): the exact close, USD per one whole unit, as the source's
    # decimal string. Preferred over usd_micros_per_unit when present, so a
    # sub-cent coin ("0.00001234" = 12.34 micros) keeps its precision. It
    # must agree with usd_micros_per_unit (= close x 1e6 truncated toward zero).
    close: str | None = None

    def __post_init__(self) -> None:
        _require_int(self.usd_micros_per_unit, "usd_micros_per_unit")
        if self.usd_micros_per_unit < 0:
            raise ValueError(f"close for {self.asset} on {self.day} is negative")
        if self.close is not None:
            exact = parse_price(self.close) * MICROS_PER_UNIT
            if exact < 0:
                raise ValueError(f"close for {self.asset} on {self.day} is negative")
            if _trunc_micros(exact) != self.usd_micros_per_unit:
                raise ValueError(
                    f"close {self.close!r} for {self.asset} on {self.day} disagrees with "
                    f"usd_micros_per_unit {self.usd_micros_per_unit}"
                )

    @property
    def exact_micros(self) -> Fraction:
        """USD micros per unit, exact: from `close` when present."""
        if self.close is not None:
            return parse_price(self.close) * MICROS_PER_UNIT
        return Fraction(self.usd_micros_per_unit)


@dataclass(frozen=True)
class PerpFillIn:
    """One change of a perp position ("BTC-PERP"): signed quantity (+ buys,
    - sells) at `decimals`, and the fill price as the venue's decimal string
    (USD per one whole unit). Liquidations are fills too: every change of a
    perp position must appear here, or the PERP_QTY_MISMATCH flag fires."""

    asset: str
    occurred_at: datetime
    qty_delta_atoms: int
    decimals: int
    price: str

    def __post_init__(self) -> None:
        if not self.asset.endswith(PERP_SUFFIX):
            raise ValueError(f"perp assets end in {PERP_SUFFIX!r}, got {self.asset!r}")
        _require_int(self.qty_delta_atoms, "qty_delta_atoms")
        _require_decimals(self.decimals)
        _require_aware(self.occurred_at, "occurred_at")
        parse_price(self.price)


@dataclass(frozen=True)
class AnchorIn:
    """The venue's own holding of `asset` at the anchor (connect) time. Legs
    passed alongside an anchor are the legs AFTER it. `entry_price` (decimal
    string) is the venue's average entry for a perp position opened before
    the anchor; it is ignored for every other asset."""

    asset: str
    atoms: int
    decimals: int
    entry_price: str | None = None

    def __post_init__(self) -> None:
        _require_int(self.atoms, "atoms")
        _require_decimals(self.decimals)
        if self.entry_price is not None:
            parse_price(self.entry_price)


# ---- holdings -----------------------------------------------------------------


@dataclass(frozen=True)
class Qty:
    """A quantity: `atoms` at `decimals` (one unit = 10**decimals atoms)."""

    atoms: int
    decimals: int

    @property
    def units(self) -> Fraction:
        return Fraction(self.atoms, 10**self.decimals)

    def rescale(self, decimals: int) -> "Qty":
        """Same quantity at a finer scale. Scaling down could lose atoms, so it raises."""
        if decimals < self.decimals:
            raise ValueError(f"cannot rescale {self.decimals} dp down to {decimals} dp exactly")
        return Qty(self.atoms * 10 ** (decimals - self.decimals), decimals)


def _rescale(atoms: int, decimals: int, target: int) -> int:
    return atoms * 10 ** (target - decimals)


def _max_decimals(pairs: Iterable[tuple[str, int]]) -> dict[str, int]:
    scales: dict[str, int] = {}
    for asset, decimals in pairs:
        scales[asset] = max(decimals, scales.get(asset, 0))
    return scales


def _widen(scales: dict[str, int], asset: str, decimals: int, *totals: dict[str, int]) -> None:
    """_max_decimals one quantity at a time: raise `asset`'s scale to `decimals`
    when finer (never lower it), rescaling its atoms in each of `totals` up so
    they stay exact. For state fed legs or fills one by one (LedgerSweep)."""
    old = scales.get(asset, 0)
    scales[asset] = max(old, decimals)
    if decimals > old:
        for running in totals:
            if asset in running:
                running[asset] = _rescale(running[asset], old, decimals)


def _check_anchor(anchor: Sequence[AnchorIn]) -> None:
    seen: set[str] = set()
    for row in anchor:
        if row.asset in seen:
            raise ValueError(f"anchor has two rows for {row.asset!r}; one row per asset")
        seen.add(row.asset)


def _asset_scales(anchor: Sequence[AnchorIn], legs: Sequence[LegIn]) -> dict[str, int]:
    return _max_decimals(
        [(a.asset, a.decimals) for a in anchor] + [(leg.asset, leg.decimals) for leg in legs]
    )


def holdings_at(anchor: Sequence[AnchorIn], legs: Sequence[LegIn], t: datetime) -> dict[str, Qty]:
    """Per-asset holdings at time `t` (inclusive):

        holding(asset, t) = anchor(asset) + sum of delta_atoms of legs with occurred_at <= t

    every term first rescaled to the max decimals seen for that asset across
    the anchor and ALL the legs given (so the scale does not depend on `t`).
    Assets whose holding is exactly zero are omitted. Perp quantities
    ("BTC-PERP") are holdings like any other asset here."""
    _require_aware(t, "t")
    _check_anchor(anchor)
    scales = _asset_scales(anchor, legs)
    totals: dict[str, int] = {}
    for row in anchor:
        totals[row.asset] = _rescale(row.atoms, row.decimals, scales[row.asset])
    for leg in legs:
        if leg.occurred_at <= t:
            scale = scales[leg.asset]
            totals[leg.asset] = totals.get(leg.asset, 0) + _rescale(
                leg.delta_atoms, leg.decimals, scale
            )
    return {asset: Qty(atoms, scales[asset]) for asset, atoms in totals.items() if atoms}


# ---- perps: average entry, unrealized P&L ---------------------------------------


@dataclass(frozen=True)
class PerpPosition:
    """An open perp position: signed quantity (+ long, - short) and the
    average entry in USD per unit, exact. `entry` is None when unknown (an
    anchor position the venue reported without an entry price)."""

    asset: str
    qty: Qty
    entry: Fraction | None


class _PerpBook:
    """Average-cost state machine, one position per perp asset.

    With q the signed position, e its entry, and a fill of signed d at price p:
      q == 0                   open:    q' = d,     e' = p
      sign(d) == sign(q)       add:     q' = q + d, e' = (|q| e + |d| p) / (|q| + |d|)
      opposite, |d| <  |q|     reduce:  q' = q + d, e' = e      (entry kept)
      opposite, |d| == |q|     close:   q' = 0,     no entry
      opposite, |d| >  |q|     flip:    q' = q + d, e' = p      (entry resets to the flip price)
    An unknown entry stays unknown through adds and reductions; a flip or a
    fresh open makes it known."""

    def __init__(self, scales: dict[str, int]) -> None:
        self.scales = scales
        self.qty: dict[str, int] = {}
        self.entry: dict[str, Fraction | None] = {}

    def seed(self, row: AnchorIn) -> None:
        atoms = _rescale(row.atoms, row.decimals, self.scales[row.asset])
        self.qty[row.asset] = atoms
        known = atoms != 0 and row.entry_price is not None
        self.entry[row.asset] = parse_price(row.entry_price) if known else None  # type: ignore[arg-type]

    def widen(self, asset: str, decimals: int) -> None:
        """Raise `asset`'s scale to at least `decimals`, for a book fed fills one at
        a time (LedgerSweep) instead of sized from all of them up front. The open
        quantity is rescaled exactly; the entry is per unit, so it is unchanged,
        and every rule above compares or weights quantities at one scale, so the
        book evolves exactly as one built at the finer scale from the start."""
        _widen(self.scales, asset, decimals, self.qty)

    def apply(self, fill: PerpFillIn) -> None:
        d = _rescale(fill.qty_delta_atoms, fill.decimals, self.scales[fill.asset])
        if d == 0:
            return
        p = parse_price(fill.price)
        q = self.qty.get(fill.asset, 0)
        e = self.entry.get(fill.asset)
        q2 = q + d
        if q == 0:
            e2: Fraction | None = p
        elif (q > 0) == (d > 0):
            e2 = None if e is None else (abs(q) * e + abs(d) * p) / (abs(q) + abs(d))
        elif q2 == 0:
            e2 = None
        elif (q2 > 0) == (q > 0):
            e2 = e
        else:
            e2 = p
        self.qty[fill.asset] = q2
        self.entry[fill.asset] = e2

    def positions(self) -> dict[str, PerpPosition]:
        return {
            asset: PerpPosition(asset, Qty(q, self.scales[asset]), self.entry.get(asset))
            for asset, q in self.qty.items()
            if q
        }


def _perp_scales(anchor: Sequence[AnchorIn], perp_fills: Sequence[PerpFillIn]) -> dict[str, int]:
    return _max_decimals(
        [(a.asset, a.decimals) for a in anchor if a.asset.endswith(PERP_SUFFIX)]
        + [(f.asset, f.decimals) for f in perp_fills]
    )


def _new_book(anchor: Sequence[AnchorIn], perp_fills: Sequence[PerpFillIn]) -> _PerpBook:
    book = _PerpBook(_perp_scales(anchor, perp_fills))
    for row in anchor:
        if row.asset.endswith(PERP_SUFFIX):
            book.seed(row)
    return book


def perp_positions_at(
    anchor: Sequence[AnchorIn], perp_fills: Sequence[PerpFillIn], t: datetime
) -> dict[str, PerpPosition]:
    """Open perp positions at `t` (inclusive) under the average-cost method
    (see _PerpBook). Anchor positions start at their `entry_price`. Fills are
    applied in occurred_at order (ties keep input order)."""
    _require_aware(t, "t")
    _check_anchor(anchor)
    book = _new_book(anchor, perp_fills)
    for fill in sorted(perp_fills, key=lambda f: f.occurred_at):
        if fill.occurred_at <= t:
            book.apply(fill)
    return book.positions()


def unrealized_pnl_micros(position: PerpPosition, close_micros: int | Fraction) -> int:
    """qty x (close - entry), in USD micros, truncated toward zero. Positive
    for a long above entry and for a short below it. Realized P&L is NOT
    computed here: the venue's REALIZED_PNL leg is the truth, already in cash."""
    if position.entry is None:
        raise ValueError(f"{position.asset}: entry price unknown, cannot value")
    return _trunc_micros(position.qty.units * (close_micros - position.entry * MICROS_PER_UNIT))


# ---- prices ---------------------------------------------------------------------


class _PriceIndex:
    """Closes by asset and day, as EXACT USD micros per unit (PriceIn.exact_micros:
    the decimal `close` when given, else the integer micros)."""

    def __init__(self, prices: Iterable[PriceIn]) -> None:
        rows: dict[str, dict[date, Fraction]] = {}
        for price in prices:
            by_day = rows.setdefault(price.asset, {})
            if price.day in by_day:
                raise ValueError(f"two closes for {price.asset} on {price.day}")
            by_day[price.day] = price.exact_micros
        self._days = {asset: sorted(by_day) for asset, by_day in rows.items()}
        self._rows = rows
        self._cut: date | None = None  # set on a before() view only

    def before(self, day: date) -> "_PriceIndex":
        """A view of the same closes limited to days strictly BEFORE `day`, sharing
        the index (nothing is copied or re-sorted). It answers has / close /
        perp_close exactly as _PriceIndex([p for p in prices if p.day < day])
        would, including a perp falling back to its coin when the perp's own
        closes all fall on or after `day`."""
        view = copy(self)
        view._cut = day if self._cut is None else min(day, self._cut)
        return view

    def _visible(self, days: list[date]) -> int:
        """How many of an asset's sorted close days this index may use."""
        return len(days) if self._cut is None else bisect_left(days, self._cut)

    def has(self, asset: str) -> bool:
        days = self._days.get(asset)
        return bool(days) and self._visible(days) > 0

    def close(self, asset: str, day: date) -> tuple[Fraction, date] | None:
        """Last known close on or before `day`: (exact usd micros per unit, day of that close)."""
        days = self._days.get(asset)
        if not days:
            return None
        i = min(bisect_right(days, day), self._visible(days))
        if i == 0:
            return None
        found = days[i - 1]
        return self._rows[asset][found], found

    def perp_close(self, perp_asset: str, day: date) -> tuple[Fraction, date] | None:
        """A perp is valued at closes of its own asset code ("BTC-PERP") when
        any exist, else at closes of the underlying coin ("BTC")."""
        if self.has(perp_asset):
            return self.close(perp_asset, day)
        return self.close(perp_asset.removesuffix(PERP_SUFFIX), day)


# ---- daily equity ------------------------------------------------------------


class FlagReason(StrEnum):
    CARRIED_FORWARD = "carried_forward"  # no close that day: the last known close was used
    UNPRICED = "unpriced"  # no close on or before that day: excluded from equity/flows
    PERP_ENTRY_UNKNOWN = "perp_entry_unknown"  # open perp without an entry: excluded
    PERP_QTY_MISMATCH = "perp_qty_mismatch"  # perp fills disagree with the ledger's perp legs


@dataclass(frozen=True)
class ValuationFlag:
    day: date
    asset: str
    reason: FlagReason
    price_day: date | None = None  # for CARRIED_FORWARD: the day of the close used


class PositionKind(StrEnum):
    CASH = "cash"  # an asset in CASH_AT_PAR, 1.00 USD per unit
    ASSET = "asset"  # spot coin or equity share: qty x close
    PERP = "perp"  # perp position: qty x (close - entry)


@dataclass(frozen=True)
class PositionValue:
    asset: str
    kind: PositionKind
    qty: Qty
    close_micros: int  # USD micros per unit used (1_000_000 for cash)
    price_day: date | None  # day of the close used; None for cash at par
    value_micros: int  # contribution to equity
    notional_micros: int  # signed qty x close (equal to value except for perps)
    entry: Fraction | None = None  # perps only
    # wave2 (D14): close_micros above is truncated for display; values use the exact close.


@dataclass(frozen=True)
class DayValuation:
    """End-of-day (UTC) valuation. `flow_micros` is the USD value of the
    external flows (base.EXTERNAL_FLOW_KINDS) attributed to this day, at
    this day's closes; + for money in, - for money out."""

    day: date
    equity_micros: int
    flow_micros: int
    positions: tuple[PositionValue, ...] = ()
    flags: tuple[ValuationFlag, ...] = ()


class _Flags:
    def __init__(self, day: date) -> None:
        self.day = day
        self._items: dict[tuple[str, FlagReason], ValuationFlag] = {}

    def add(self, asset: str, reason: FlagReason, price_day: date | None = None) -> None:
        self._items.setdefault(
            (asset, reason), ValuationFlag(self.day, asset, reason, price_day=price_day)
        )

    def tuple(self) -> tuple[ValuationFlag, ...]:
        return tuple(self._items.values())


def _unit_close(
    asset: str, day: date, prices: _PriceIndex, flags: _Flags
) -> tuple[Fraction, date | None] | None:
    """USD micros per unit for a non-perp asset on `day`, with the carry-forward
    and unpriced rules applied (and flagged). None means: exclude it."""
    if asset in CASH_AT_PAR:
        return Fraction(MICROS_PER_UNIT), None
    found = prices.close(asset, day)
    if found is None:
        flags.add(asset, FlagReason.UNPRICED)
        return None
    close, price_day = found
    if price_day != day:
        flags.add(asset, FlagReason.CARRIED_FORWARD, price_day)
    return close, price_day


def _value_day(
    day: date,
    holdings: dict[str, Qty],
    perps: dict[str, PerpPosition],
    flows: dict[str, Qty],
    prices: _PriceIndex,
) -> DayValuation:
    flags = _Flags(day)
    positions: list[PositionValue] = []
    for asset in sorted(holdings):
        qty = holdings[asset]
        if asset.endswith(PERP_SUFFIX):
            continue  # valued from the perp book below
        found = _unit_close(asset, day, prices, flags)
        if found is None:
            continue
        close, price_day = found
        value = _trunc_micros(qty.units * close)
        kind = PositionKind.CASH if asset in CASH_AT_PAR else PositionKind.ASSET
        positions.append(
            PositionValue(asset, kind, qty, _trunc_micros(close), price_day, value, value)
        )

    perp_assets = {a for a in holdings if a.endswith(PERP_SUFFIX)} | set(perps)
    for asset in sorted(perp_assets):
        ledger_units = holdings[asset].units if asset in holdings else Fraction(0)
        book_units = perps[asset].qty.units if asset in perps else Fraction(0)
        if ledger_units != book_units:
            flags.add(asset, FlagReason.PERP_QTY_MISMATCH)
        if asset not in perps:
            continue
        position = perps[asset]
        found = prices.perp_close(asset, day)
        if found is None:
            flags.add(asset, FlagReason.UNPRICED)
            continue
        close, price_day = found
        if price_day != day:
            flags.add(asset, FlagReason.CARRIED_FORWARD, price_day)
        if position.entry is None:
            flags.add(asset, FlagReason.PERP_ENTRY_UNKNOWN)
            continue
        positions.append(
            PositionValue(
                asset,
                PositionKind.PERP,
                position.qty,
                _trunc_micros(close),
                price_day,
                unrealized_pnl_micros(position, close),
                _trunc_micros(position.qty.units * close),
                entry=position.entry,
            )
        )

    flow_micros = 0
    for asset in sorted(flows):
        found = _unit_close(asset, day, prices, flags)
        if found is not None:
            flow_micros += _trunc_micros(flows[asset].units * found[0])

    return DayValuation(
        day=day,
        equity_micros=sum(p.value_micros for p in positions),
        flow_micros=flow_micros,
        positions=tuple(positions),
        flags=flags.tuple(),
    )


def daily_equity_series(
    anchor: Sequence[AnchorIn],
    legs: Sequence[LegIn],
    prices: Iterable[PriceIn],
    perp_fills: Sequence[PerpFillIn],
    days: Sequence[date],
) -> list[DayValuation]:
    """End-of-day (UTC) equity for each of `days` (strictly increasing), in USD micros:

        E_d = sum over cash assets (CASH_AT_PAR)   of  qty x 1.00
            + sum over spot/equity assets          of  qty x close_d
            + sum over open perp positions         of  qty x (close_d - entry)

    each term truncated toward zero to whole micros. Holdings at end of day d
    are the anchor plus every leg whose UTC day is <= d. Perps are valued from
    `perp_fills` (average-cost entry); their margin, fees, funding and
    realized P&L are already cash legs. A perp's close is its own asset code's
    close if `prices` has any, else the underlying coin's.

    Missing prices: no close on day d uses the last known close before d and
    flags CARRIED_FORWARD; no close on or before d excludes the asset from
    E_d (and from F_d) and flags UNPRICED.

    Flows: F_d = sum per asset of (net external-flow atoms attributed to d) x
    close_d, same pricing and rounding. A flow leg is attributed to the first
    listed day on or after its UTC day; flows on or before days[0] are part of
    the starting capital and are not attributed (F of days[0] counts only
    days[0]'s own flows)."""
    if any(b <= a for a, b in zip(days, days[1:], strict=False)):
        raise ValueError("days must be strictly increasing")
    _check_anchor(anchor)
    index = _PriceIndex(prices)
    scales = _asset_scales(anchor, legs)
    held: dict[str, int] = {
        row.asset: _rescale(row.atoms, row.decimals, scales[row.asset]) for row in anchor
    }
    book = _new_book(anchor, perp_fills)
    ordered_legs = sorted(legs, key=lambda leg: leg.occurred_at)
    ordered_fills = sorted(perp_fills, key=lambda f: f.occurred_at)
    li = fi = 0
    out: list[DayValuation] = []
    for n, day in enumerate(days):
        flow_from = days[n - 1] + timedelta(days=1) if n else day
        flow_atoms: dict[str, int] = {}
        while li < len(ordered_legs) and _utc_day(ordered_legs[li].occurred_at) <= day:
            leg = ordered_legs[li]
            atoms = _rescale(leg.delta_atoms, leg.decimals, scales[leg.asset])
            held[leg.asset] = held.get(leg.asset, 0) + atoms
            if leg.kind in EXTERNAL_FLOW_KINDS and _utc_day(leg.occurred_at) >= flow_from:
                flow_atoms[leg.asset] = flow_atoms.get(leg.asset, 0) + atoms
            li += 1
        while fi < len(ordered_fills) and _utc_day(ordered_fills[fi].occurred_at) <= day:
            book.apply(ordered_fills[fi])
            fi += 1
        holdings = {a: Qty(atoms, scales[a]) for a, atoms in held.items() if atoms}
        flows = {a: Qty(atoms, scales[a]) for a, atoms in flow_atoms.items() if atoms}
        out.append(_value_day(day, holdings, book.positions(), flows, index))
    return out


def valuation_on(
    anchor: Sequence[AnchorIn],
    legs: Sequence[LegIn],
    prices: Iterable[PriceIn],
    perp_fills: Sequence[PerpFillIn],
    day: date,
) -> DayValuation:
    """End-of-day valuation of a single day (daily_equity_series for one day)."""
    return daily_equity_series(anchor, legs, prices, perp_fills, [day])[0]


# ---- returns --------------------------------------------------------------------


class EquityPointLike(Protocol):
    @property
    def day(self) -> date: ...
    @property
    def equity_micros(self) -> int: ...
    @property
    def flow_micros(self) -> int: ...


@dataclass(frozen=True)
class EquityPoint:
    """A stored equity point (for callers that persist the series)."""

    day: date
    equity_micros: int
    flow_micros: int = 0


@dataclass(frozen=True)
class DailyReturn:
    day: date
    r: Fraction | None  # None: no capital at risk the day before (E_(d-1) <= 0) or first point


def _window(
    series: Sequence[EquityPointLike], start: date | None, end: date | None
) -> list[EquityPointLike]:
    if any(b.day <= a.day for a, b in zip(series, series[1:], strict=False)):
        raise ValueError("series days must be strictly increasing")
    return [
        p for p in series if (start is None or p.day >= start) and (end is None or p.day <= end)
    ]


def daily_returns(series: Sequence[EquityPointLike]) -> list[DailyReturn]:
    """Flow-adjusted daily return, flows assumed at END of day:

        r_d = (E_d - F_d) / E_(d-1) - 1

    so a deposit raises E_d and F_d equally and is never a gain. A day whose
    previous equity is zero or negative has no defined return (None): the
    first funded day starts the series, and the chain resumes after an
    account is emptied and refunded. Exact (Fraction)."""
    points = _window(series, None, None)
    out: list[DailyReturn] = []
    for n, point in enumerate(points):
        prev = points[n - 1].equity_micros if n else 0
        r = Fraction(point.equity_micros - point.flow_micros, prev) - 1 if prev > 0 else None
        out.append(DailyReturn(point.day, r))
    return out


def time_weighted_return(
    series: Sequence[EquityPointLike], start: date | None = None, end: date | None = None
) -> Fraction | None:
    """Flow-adjusted TWR over the window [start, end] (days inclusive; the
    first point in the window is the base, its own return is not counted):

        TWR = prod over d in (start, end] of (1 + r_d)  - 1

    Days with an undefined r_d are skipped. None when no day in the window
    has a defined return."""
    points = _window(series, start, end)
    growth = Fraction(1)
    defined = False
    for daily in daily_returns(points)[1:]:
        if daily.r is not None:
            growth *= 1 + daily.r
            defined = True
    return growth - 1 if defined else None


def modified_dietz_return(
    series: Sequence[EquityPointLike], start: date | None = None, end: date | None = None
) -> Fraction | None:
    """Modified Dietz money-weighted return over [start, end], for comparison
    with the TWR. With E_0 the first point's equity, E_N the last's, N the
    number of days between them, and each flow F_d (days after the first)
    weighted by the share of the window left after it (flows at end of day):

        MD = (E_N - E_0 - sum F_d) / (E_0 + sum w_d F_d),   w_d = (end - d) / N

    None for a window shorter than one day or a non-positive denominator."""
    points = _window(series, start, end)
    if len(points) < 2:
        return None
    first, last = points[0], points[-1]
    n_days = (last.day - first.day).days
    flows = points[1:]
    total_flow = sum(p.flow_micros for p in flows)
    weighted = sum(Fraction((last.day - p.day).days, n_days) * p.flow_micros for p in flows)
    denominator = first.equity_micros + weighted
    if denominator <= 0:
        return None
    return Fraction(last.equity_micros - first.equity_micros - total_flow) / denominator


# ---- exposure -------------------------------------------------------------------


@dataclass(frozen=True)
class Exposure:
    """Risk shape of one day's valuation. Cash is not exposure.

    gross        = sum over spot/equity/perp positions of |notional|
    share(u)     = sum of |notional| of positions whose underlying is u / gross
                   (underlying: "BTC-PERP" -> "BTC", so spot and perp BTC add up)
    top          = the largest share (ties: alphabetical first)
    perp leverage= sum over perps of |qty x close| / equity   (None if equity <= 0)"""

    day: date
    equity_micros: int
    gross_micros: int
    by_underlying_micros: dict[str, int] = field(default_factory=dict)
    shares: dict[str, Fraction] = field(default_factory=dict)
    top_asset: str | None = None
    top_share: Fraction | None = None
    perp_gross_micros: int = 0
    perp_gross_leverage: Fraction | None = None


def exposure(valuation: DayValuation) -> Exposure:
    """Exposure of a DayValuation (see Exposure). Excluded (unpriced or
    entry-unknown) positions carry no exposure, as they carry no value."""
    by_underlying: dict[str, int] = {}
    perp_gross = 0
    for p in valuation.positions:
        if p.kind is PositionKind.CASH:
            continue
        underlying = p.asset.removesuffix(PERP_SUFFIX)
        by_underlying[underlying] = by_underlying.get(underlying, 0) + abs(p.notional_micros)
        if p.kind is PositionKind.PERP:
            perp_gross += abs(p.notional_micros)
    gross = sum(by_underlying.values())
    shares = {u: Fraction(v, gross) for u, v in by_underlying.items()} if gross else {}
    top = min(shares, key=lambda u: (-shares[u], u)) if shares else None
    equity = valuation.equity_micros
    return Exposure(
        day=valuation.day,
        equity_micros=equity,
        gross_micros=gross,
        by_underlying_micros=by_underlying,
        shares=shares,
        top_asset=top,
        top_share=shares[top] if top is not None else None,
        perp_gross_micros=perp_gross,
        perp_gross_leverage=Fraction(perp_gross, equity) if equity > 0 else None,
    )


# ---- wave2: creator equity at a fill (signals v2) ---------------------------------


def equity_before(
    anchor: Sequence[AnchorIn],
    legs: Sequence[LegIn],
    prices: Iterable[PriceIn],
    perp_fills: Sequence[PerpFillIn],
    t: datetime,
) -> DayValuation:
    """Equity just BEFORE instant `t`, for sizing a follower against a fill at `t`:

        E(t-) = cash + spot/equity holdings x last close + perp qty x (close - entry)

    where holdings and perp positions include every leg and perp fill with
    occurred_at < t (strictly: the fill at t is what is being sized), and
    only closes of complete UTC days BEFORE t's day are used (day D-1,
    carried back further if missing), so no intraday price is assumed.

    Returns the valuation of day D = t's UTC day built from those inputs.
    Every priced asset therefore carries a CARRIED_FORWARD flag (expected:
    day D has no close yet); UNPRICED assets are excluded, as in the daily
    series. The day whose closes were used is the max `price_day` over the
    returned positions (equity_basis_day on the signal)."""
    _require_aware(t, "t")
    day = _utc_day(t)
    return valuation_on(
        anchor,
        [leg for leg in legs if leg.occurred_at < t],
        [price for price in prices if price.day < day],
        [fill for fill in perp_fills if fill.occurred_at < t],
        day,
    )


def basis_day(valuation: DayValuation) -> date | None:
    """Latest close day used by a valuation (None when only cash was valued)."""
    days = [p.price_day for p in valuation.positions if p.price_day is not None]
    return max(days) if days else None


# ---- wave2: many instants in one forward sweep (signals v2) -----------------------


class LedgerSweep:
    """Holdings, the perp book and equity just BEFORE each instant of a
    non-decreasing series, in ONE forward pass over the ledger. For each t
    (never earlier than the previous t; repeats are fine):

        holdings_before(t)        == holdings_at(anchor, [legs < t], t)
        perp_positions_before(t)  == perp_positions_at(anchor, [perp fills < t], t)
        equity_before(t)          == equity_before(anchor, legs, prices, perp_fills, t)

    exactly, scales included: an asset's scale is the max decimals over the
    anchor and the legs (or perp fills) before t, as those functions see it.
    The state advances with the same primitives (_rescale and _widen, _PerpBook,
    _PriceIndex cut to the closes before t's UTC day, _value_day), so every
    formula still lives once; tests/test_valuation_sweep.py pins the equality.

    Cost: legs and fills are sorted and the closes indexed once, and each leg
    and fill is applied once, so N legs and fills, P closes and K instants
    cost O((N + P) log(N + P) + K A log P) for A held assets, where calling
    the per-instant functions K times costs O(K (N + P)).

    One difference: the closes are indexed up front, so two closes for one
    asset and day raise here even when both fall on or after every instant
    asked (valuation_db.price_inputs returns one close per asset and day)."""

    def __init__(
        self,
        anchor: Sequence[AnchorIn],
        legs: Sequence[LegIn],
        prices: Iterable[PriceIn],
        perp_fills: Sequence[PerpFillIn],
    ) -> None:
        _check_anchor(anchor)
        self._legs = sorted(legs, key=lambda leg: leg.occurred_at)
        self._fills = sorted(perp_fills, key=lambda f: f.occurred_at)
        self._prices = _PriceIndex(prices)
        self._scales = _asset_scales(anchor, ())
        self._held = {
            row.asset: _rescale(row.atoms, row.decimals, self._scales[row.asset]) for row in anchor
        }
        self._book = _new_book(anchor, ())
        self._flows: dict[str, int] = {}  # external flows on self._day before self._t
        self._li = self._fi = 0
        self._t: datetime | None = None
        self._day: date | None = None

    def _advance(self, t: datetime) -> None:
        """Apply every leg and perp fill with occurred_at < t (in the order
        holdings_at / perp_positions_at use), then stand at t."""
        _require_aware(t, "t")
        if self._t is not None and t < self._t:
            raise ValueError(f"instants must be non-decreasing: {t} is before {self._t}")
        day = _utc_day(t)
        if day != self._day:
            self._flows = {}  # every leg applied so far falls on an earlier UTC day
        self._t, self._day = t, day
        legs, fills = self._legs, self._fills
        while self._li < len(legs) and legs[self._li].occurred_at < t:
            leg = legs[self._li]
            _widen(self._scales, leg.asset, leg.decimals, self._held, self._flows)
            atoms = _rescale(leg.delta_atoms, leg.decimals, self._scales[leg.asset])
            self._held[leg.asset] = self._held.get(leg.asset, 0) + atoms
            # daily_equity_series attributes a flow leg to day D when its UTC day >= D.
            if leg.kind in EXTERNAL_FLOW_KINDS and _utc_day(leg.occurred_at) >= day:
                self._flows[leg.asset] = self._flows.get(leg.asset, 0) + atoms
            self._li += 1
        while self._fi < len(fills) and fills[self._fi].occurred_at < t:
            fill = fills[self._fi]
            self._book.widen(fill.asset, fill.decimals)
            self._book.apply(fill)
            self._fi += 1

    def _qtys(self, atoms_by_asset: dict[str, int]) -> dict[str, Qty]:
        return {a: Qty(atoms, self._scales[a]) for a, atoms in atoms_by_asset.items() if atoms}

    def holdings_before(self, t: datetime) -> dict[str, Qty]:
        """holdings_at over the anchor and every leg strictly before `t`."""
        self._advance(t)
        return self._qtys(self._held)

    def perp_positions_before(self, t: datetime) -> dict[str, PerpPosition]:
        """perp_positions_at over the anchor and every perp fill strictly before `t`."""
        self._advance(t)
        return self._book.positions()

    def equity_before(self, t: datetime) -> DayValuation:
        """equity_before(anchor, legs, prices, perp_fills, t): the valuation of
        t's UTC day D from the holdings, perp book and day-D flows before `t`,
        at the closes of days before D."""
        self._advance(t)
        day = _utc_day(t)
        return _value_day(
            day,
            self._qtys(self._held),
            self._book.positions(),
            self._qtys(self._flows),
            self._prices.before(day),
        )


# ---- perf: the end state of a growing ledger, folded incrementally -----------------


class OutOfOrderFill(ValueError):
    """A perp fill folded before the last one folded (LedgerFold.add_fill)."""


class LedgerFold:
    """Holdings and the perp book at the END of a ledger that only grows,
    folded one leg and one perp fill at a time, and serializable, so a caller
    can keep the state (paper_book's checkpoints) and later fold only what is
    new. With L the legs and F the perp fills folded so far:

        holdings()        == holdings_at(anchor, L, t)        for any t >= every leg
        perp_positions()  == perp_positions_at(anchor, F, t)  for any t >= every fill

    exactly, scales included: an asset's scale is the max decimals over the
    anchor and what was folded, widened as finer legs arrive (_widen and
    _PerpBook.widen, as in LedgerSweep), so every formula still lives once.
    Legs may come in any order (holdings are sums). Fills must come in the
    order perp_positions_at applies them: occurred_at non-decreasing, ties in
    input order; an earlier fill raises OutOfOrderFill, so a caller resuming
    a saved state replays from scratch instead. tests/test_paper_checkpoints.py
    pins the equality."""

    STATE_VERSION = 1

    def __init__(self, anchor: Sequence[AnchorIn]) -> None:
        _check_anchor(anchor)
        self.anchor = tuple(anchor)
        self._scales = _asset_scales(anchor, ())
        self._held = {
            row.asset: _rescale(row.atoms, row.decimals, self._scales[row.asset]) for row in anchor
        }
        self._book = _new_book(anchor, ())
        self.fills_through: datetime | None = None  # occurred_at of the last fill folded

    def add_leg(self, leg: LegIn) -> None:
        _widen(self._scales, leg.asset, leg.decimals, self._held)
        atoms = _rescale(leg.delta_atoms, leg.decimals, self._scales[leg.asset])
        self._held[leg.asset] = self._held.get(leg.asset, 0) + atoms

    def add_fill(self, fill: PerpFillIn) -> None:
        if self.fills_through is not None and fill.occurred_at < self.fills_through:
            raise OutOfOrderFill(
                f"{fill.asset} fill at {fill.occurred_at} is before {self.fills_through}"
            )
        self._book.widen(fill.asset, fill.decimals)
        self._book.apply(fill)
        self.fills_through = fill.occurred_at

    def holdings(self) -> dict[str, Qty]:
        return {a: Qty(atoms, self._scales[a]) for a, atoms in self._held.items() if atoms}

    def perp_positions(self) -> dict[str, PerpPosition]:
        return self._book.positions()

    # The state is JSON-safe with every integer and fraction as a string, never
    # a float (invariant 1), so a JSON column round-trips it exactly.

    def to_state(self) -> dict:
        book = self._book
        return {
            "v": self.STATE_VERSION,
            "anchor": _anchor_state(self.anchor),
            "held": {a: str(atoms) for a, atoms in sorted(self._held.items())},
            "scales": dict(sorted(self._scales.items())),
            "perp_qty": {a: str(q) for a, q in sorted(book.qty.items())},
            "perp_entry": {
                a: None if e is None else f"{e.numerator}/{e.denominator}"
                for a, e in sorted(book.entry.items())
            },
            "perp_scales": dict(sorted(book.scales.items())),
            "fills_through": (
                None
                if self.fills_through is None
                else self.fills_through.astimezone(UTC).isoformat()
            ),
        }

    @classmethod
    def from_state(cls, anchor: Sequence[AnchorIn], state: dict) -> "LedgerFold":
        """The fold that to_state() described. Raises ValueError for a state of
        another version or one made over a different anchor."""
        if state.get("v") != cls.STATE_VERSION:
            raise ValueError(f"fold state version {state.get('v')!r}")
        if state.get("anchor") != _anchor_state(anchor):
            raise ValueError("fold state was made over a different anchor")
        fold = cls(anchor)
        fold._held = {a: int(atoms) for a, atoms in state["held"].items()}
        fold._scales = {a: int(d) for a, d in state["scales"].items()}
        book = fold._book
        book.scales.clear()
        book.scales.update({a: int(d) for a, d in state["perp_scales"].items()})
        book.qty = {a: int(q) for a, q in state["perp_qty"].items()}
        book.entry = {a: None if e is None else Fraction(e) for a, e in state["perp_entry"].items()}
        for decimals in (*fold._scales.values(), *book.scales.values()):
            _require_decimals(decimals)
        if set(fold._held) - set(fold._scales) or set(book.qty) - set(book.scales):
            raise ValueError("fold state has a quantity without a scale")
        through = state.get("fills_through")
        fold.fills_through = None if through is None else datetime.fromisoformat(through)
        return fold


def _anchor_state(anchor: Sequence[AnchorIn]) -> list[list]:
    return [[a.asset, str(a.atoms), a.decimals, a.entry_price] for a in anchor]

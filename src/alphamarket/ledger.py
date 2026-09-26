"""Ledger math (M2): cash accounting from venue fills and settlements.

Every formula here is documented on the public methodology page
(docs/methodology.md) and pinned by a hand-computed unit test.

Model (verified to the micro-dollar against the live demo account on
2026-09-01: pre-trade balance minus one fill's notional-plus-fee equalled the
post-settlement balance exactly):

    cash(t) = anchor_balance
            + sum(fill_cash_flow for fills after anchor_at, filled_at <= t)
            + sum(settlement revenue for settlements after anchor_at, settled_at <= t)

    fill_cash_flow(buy)  = -(count * price + fee)
    fill_cash_flow(sell) = +(count * price - fee)
    settlement flow      = +revenue          (fees are charged at fill time
                                              only; the settlement payload's
                                              fee field is a summary, not a
                                              second charge)

The anchor is the venue balance observed at connect time. Reconciliation
therefore never depends on how much history the venue returns: any fill
before the anchor is already reflected in the anchor balance, and any
settlement after it (even of a pre-anchor position) is real cash in.

Contract quantities and prices both carry the 1e6 scale, so count * price
carries 1e12 and money.notional_micros brings it back exactly. A venue that
reports an average price finer than 1e-6 (Polymarket) also reports the gross
cash of the fill; that figure is stored as `notional_micros` and used in
place of the product, so cash still reproduces the venue to the micro.

Ledger v2 (platform_plan 2.1): every fill, settlement and event is a group
of LEGS, each the signed change of one asset (connectors.base.NormalizedLeg).
The cash curve above is the special case "sum of the USD legs after the
anchor"; build_cash_curve now computes it that way, and the parity test
replays every venue fixture through both paths.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from alphamarket.connectors.base import Action, AssetAmount, LegKind, NormalizedLeg, Side
from alphamarket.money import notional_micros


class FillLike(Protocol):
    ticker: str
    action: str
    count_micros: int
    price_micros: int
    fee_micros: int
    filled_at: datetime


class SettlementLike(Protocol):
    ticker: str
    revenue_micros: int
    settled_at: datetime


@dataclass(frozen=True)
class LedgerEvent:
    at: datetime
    kind: str  # "fill" | "settlement"
    ticker: str
    cash_flow_micros: int
    cash_after_micros: int


def fill_notional(fill: FillLike) -> int:
    """Gross value of a fill before fees: the venue-reported figure when the
    fill carries one, otherwise price x count computed exactly."""
    reported = getattr(fill, "notional_micros", None)
    if reported is not None:
        return reported
    return notional_micros(fill.price_micros, fill.count_micros)


def fill_cash_flow(fill: FillLike) -> int:
    """Signed cash movement of one fill, in micros."""
    notional = fill_notional(fill)
    if Action(fill.action) is Action.BUY:
        return -(notional + fill.fee_micros)
    return notional - fill.fee_micros


def settlement_cash_flow(settlement: SettlementLike) -> int:
    return settlement.revenue_micros


def aware(ts: datetime) -> datetime:
    """SQLite drops tzinfo; stored values are UTC by invariant #2."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def build_cash_curve(
    anchor_balance_micros: int,
    anchor_at: datetime,
    fills: list[FillLike],
    settlements: list[SettlementLike],
) -> list[LedgerEvent]:
    """Chronological cash curve starting from the anchor. Events at or
    before anchor_at are excluded: the anchor balance already contains them.

    Computed over legs: each fill/settlement becomes its leg group
    (legs_for_fill / legs_for_settlement) and the curve sums the USD legs.
    Cash legs are identical under the netting and tokenised conventions, so
    the venue does not matter here."""
    groups = [fill_group(f) for f in fills] + [settlement_group(s) for s in settlements]
    return cash_curve_from_groups(anchor_balance_micros, anchor_at, groups)


def legacy_build_cash_curve(
    anchor_balance_micros: int,
    anchor_at: datetime,
    fills: list[FillLike],
    settlements: list[SettlementLike],
) -> list[LedgerEvent]:
    """The M2 cash curve computed straight from fills and settlements. Kept
    ONLY as the reference the legs-path parity test compares against
    (platform_plan M8 exit); delete once the legs path has run in prod."""
    anchor_at = aware(anchor_at)
    pending: list[tuple[datetime, int, str, str, int]] = []
    for f in fills:
        at = aware(f.filled_at)
        if at > anchor_at:
            pending.append((at, 0, "fill", f.ticker, fill_cash_flow(f)))
    for s in settlements:
        at = aware(s.settled_at)
        if at > anchor_at:
            pending.append((at, 1, "settlement", s.ticker, settlement_cash_flow(s)))
    pending.sort(key=lambda e: (e[0], e[1]))

    curve: list[LedgerEvent] = []
    cash = anchor_balance_micros
    for at, _, kind, ticker, flow in pending:
        cash += flow
        curve.append(LedgerEvent(at, kind, ticker, flow, cash))
    return curve


def cash_at(
    anchor_balance_micros: int,
    anchor_at: datetime,
    fills: list[FillLike],
    settlements: list[SettlementLike],
    as_of: datetime | None = None,
) -> int:
    """Cash balance implied by the ledger at `as_of` (default: after all events)."""
    curve = build_cash_curve(anchor_balance_micros, anchor_at, fills, settlements)
    if as_of is not None:
        as_of = aware(as_of)
        curve = [e for e in curve if e.at <= as_of]
    return curve[-1].cash_after_micros if curve else anchor_balance_micros


# ---- ledger v2: legs (platform_plan 2.1) ---------------------------------------

CASH_ASSET = "USD"
MICRO_DECIMALS = 6  # micro-dollars and micro-contracts are atoms at 6 dp

# Binary asset convention (connectors/base.py): netting venues hold ONE signed
# asset per market ("<ticker>", +yes / -no); tokenised venues hold both
# outcome tokens ("<ticker>:yes", "<ticker>:no"), unsigned. Keyed on the venue
# here, in one place, until VenueProfile carries it (contract request).
TOKENISED_BINARY_VENUES = frozenset({"polymarket"})


def binary_is_tokenised(venue: str) -> bool:
    """True when the venue's binary contracts are separate outcome tokens."""
    return venue in TOKENISED_BINARY_VENUES


def binary_asset(venue: str, ticker: str, side: str) -> str:
    """Ledger asset code of one side of a binary market."""
    return f"{ticker}:{Side(side).value}" if binary_is_tokenised(venue) else ticker


class BinaryFillLike(FillLike, Protocol):
    side: str


class BinarySettlementLike(SettlementLike, Protocol):
    yes_count_micros: int
    no_count_micros: int


def legs_for_fill(
    fill: BinaryFillLike, venue: str = "kalshi", cash_asset: str = CASH_ASSET
) -> list[NormalizedLeg]:
    """The legs of one binary fill, all at 6 dp (micros):

        FILL_BASE   contract asset  netting: +count for buy YES / sell NO,
                                    -count for buy NO / sell YES;
                                    tokenised: +count buy, -count sell
        FILL_QUOTE  cash            -notional (buy) / +notional (sell)
        FEE         cash            -fee

    FILL_QUOTE + FEE is exactly fill_cash_flow. Zero legs (a zero fee) are
    returned; the ledger drops them on write."""
    side = Side(fill.side)
    buy = Action(fill.action) is Action.BUY
    if binary_is_tokenised(venue):
        base_delta = fill.count_micros if buy else -fill.count_micros
    else:
        long_yes = buy == (side is Side.YES)
        base_delta = fill.count_micros if long_yes else -fill.count_micros
    notional = fill_notional(fill)
    return [
        NormalizedLeg(
            asset=binary_asset(venue, fill.ticker, side),
            delta_atoms=base_delta,
            decimals=MICRO_DECIMALS,
            kind=LegKind.FILL_BASE,
            instrument_symbol=fill.ticker,
        ),
        NormalizedLeg(
            asset=cash_asset,
            delta_atoms=-notional if buy else notional,
            decimals=MICRO_DECIMALS,
            kind=LegKind.FILL_QUOTE,
            instrument_symbol=fill.ticker,
        ),
        NormalizedLeg(
            asset=cash_asset,
            delta_atoms=-fill.fee_micros,
            decimals=MICRO_DECIMALS,
            kind=LegKind.FEE,
            instrument_symbol=fill.ticker,
        ),
    ]


def legs_for_settlement(
    settlement: BinarySettlementLike, venue: str = "kalshi", cash_asset: str = CASH_ASSET
) -> list[NormalizedLeg]:
    """The legs of one binary settlement, all SETTLEMENT kind at 6 dp:

        netting:    "<ticker>"      -(yes_count - no_count)
        tokenised:  "<ticker>:yes"  -yes_count,  "<ticker>:no"  -no_count
        cash        +revenue

    The settlement payload's fee is a summary of fees already charged at fill
    time (see module docstring), so it produces no leg."""
    ticker = settlement.ticker
    yes, no = settlement.yes_count_micros, settlement.no_count_micros
    legs: list[NormalizedLeg] = []
    if binary_is_tokenised(venue):
        for side, count in ((Side.YES, yes), (Side.NO, no)):
            legs.append(
                NormalizedLeg(
                    asset=binary_asset(venue, ticker, side),
                    delta_atoms=-count,
                    decimals=MICRO_DECIMALS,
                    kind=LegKind.SETTLEMENT,
                    instrument_symbol=ticker,
                )
            )
    else:
        legs.append(
            NormalizedLeg(
                asset=ticker,
                delta_atoms=-(yes - no),
                decimals=MICRO_DECIMALS,
                kind=LegKind.SETTLEMENT,
                instrument_symbol=ticker,
            )
        )
    legs.append(
        NormalizedLeg(
            asset=cash_asset,
            delta_atoms=settlement.revenue_micros,
            decimals=MICRO_DECIMALS,
            kind=LegKind.SETTLEMENT,
            instrument_symbol=ticker,
        )
    )
    return legs


def nonzero_legs(legs: Iterable[NormalizedLeg]) -> list[NormalizedLeg]:
    """Zero-delta legs (a losing settlement's $0 revenue, a zero fee) move
    nothing and are never written (base.py)."""
    return [leg for leg in legs if leg.delta_atoms != 0]


# ---- leg groups and the cash curve over legs -----------------------------------


class LegLike(Protocol):
    asset: str
    delta_atoms: int
    decimals: int


GROUP_ORDER = {"fill": 0, "settlement": 1, "event": 2}
"""Same-timestamp order: fills before settlements (as M2), events last."""


@dataclass(frozen=True)
class LegGroup:
    """The legs of one fill, settlement or event: one point on a curve."""

    at: datetime
    kind: str  # "fill" | "settlement" | "event"
    ticker: str
    legs: tuple[LegLike, ...]


def fill_group(fill: BinaryFillLike, venue: str = "kalshi") -> LegGroup:
    return LegGroup(aware(fill.filled_at), "fill", fill.ticker, tuple(legs_for_fill(fill, venue)))


def settlement_group(settlement: BinarySettlementLike, venue: str = "kalshi") -> LegGroup:
    return LegGroup(
        aware(settlement.settled_at),
        "settlement",
        settlement.ticker,
        tuple(legs_for_settlement(settlement, venue)),
    )


def group_kind(group_ref: str) -> str:
    """Stored group_refs are "<kind>:<venue ref>" (see ingest.group_ref_for)."""
    prefix = group_ref.split(":", 1)[0]
    return prefix if prefix in GROUP_ORDER else "event"


def rescale(atoms: int, decimals: int, to_decimals: int) -> int:
    """Exact change of scale. Scaling up is always exact; scaling down raises
    unless no precision is lost (never rounds)."""
    if to_decimals >= decimals:
        return atoms * 10 ** (to_decimals - decimals)
    q, r = divmod(atoms, 10 ** (decimals - to_decimals))
    if r:
        raise ValueError(f"{atoms} atoms at {decimals} dp are not exact at {to_decimals} dp")
    return q


def asset_delta(legs: Iterable[LegLike], asset: str, decimals: int = MICRO_DECIMALS) -> int:
    """Sum of one asset's legs, expressed at `decimals`."""
    return sum(
        rescale(leg.delta_atoms, leg.decimals, decimals) for leg in legs if leg.asset == asset
    )


def cash_curve_from_groups(
    anchor_balance_micros: int,
    anchor_at: datetime,
    groups: Iterable[LegGroup],
    cash_asset: str = CASH_ASSET,
) -> list[LedgerEvent]:
    """cash(t) = anchor + sum of the cash-asset legs of every group after
    anchor_at, in (time, fills-before-settlements, input order). Every group
    is a point, even one with no cash leg (a losing settlement), exactly as
    the M2 curve has one point per fill and per settlement."""
    anchor_at = aware(anchor_at)
    pending = [g for g in groups if aware(g.at) > anchor_at]
    pending.sort(key=lambda g: (aware(g.at), GROUP_ORDER.get(g.kind, 2)))
    curve: list[LedgerEvent] = []
    cash = anchor_balance_micros
    for g in pending:
        flow = asset_delta(g.legs, cash_asset, MICRO_DECIMALS)
        cash += flow
        curve.append(LedgerEvent(aware(g.at), g.kind, g.ticker, flow, cash))
    return curve


# ---- balance vectors -----------------------------------------------------------


class AmountLike(Protocol):
    asset: str
    atoms: int
    decimals: int


def balance_vector(
    legs: Iterable[LegLike], start: Iterable[AmountLike] = ()
) -> dict[str, AssetAmount]:
    """Holdings per asset: `start` (e.g. the anchor vector) plus the sum of
    `legs`. Each value is first rescaled to the MAX decimals seen for that
    asset among all rows, because one venue can report the same asset at
    different scales (base.py "Precision"); scaling up is exact.

    Example: BTC +0.5 at 8 dp (50_000_000) and -0.1234567891 at 10 dp
    (-1_234_567_891) -> 10 dp: 5_000_000_000 - 1_234_567_891 = 3_765_432_109."""
    rows: list[tuple[str, int, int]] = [(a.asset, a.atoms, a.decimals) for a in start]
    rows += [(leg.asset, leg.delta_atoms, leg.decimals) for leg in legs]
    scale: dict[str, int] = {}
    for asset, _, decimals in rows:
        scale[asset] = max(scale.get(asset, 0), decimals)
    totals: dict[str, int] = dict.fromkeys(scale, 0)
    for asset, atoms, decimals in rows:
        totals[asset] += rescale(atoms, decimals, scale[asset])
    return {
        asset: AssetAmount(asset=asset, atoms=totals[asset], decimals=scale[asset])
        for asset in sorted(totals)
    }


# ---- venue running balances (reconcile v2) ------------------------------------


class RunningLegLike(LegLike, Protocol):
    id: int
    balance_after_atoms: int | None


@dataclass(frozen=True)
class ChainBreak:
    asset: str
    entry_id: int
    decimals: int
    expected_atoms: int  # previous reported balance + legs since
    reported_atoms: int  # this leg's balance_after


def balance_chain_breaks(entries: Iterable[RunningLegLike]) -> list[ChainBreak]:
    """Exact check of venue-reported running balances, in the order given
    (ingestion order, ledger_entries.id). Per asset, the first leg carrying a
    balance opens the chain; every later one must equal the previous
    reported balance plus every leg of that asset in between, including its
    own delta. All values at the max decimals seen for the asset. No
    tolerance: the venue computed these from the same rows.

    Example (ZUSD, 4 dp): +100.0000 bal 100.0000; -30.0000 (no bal);
    -20.0000 bal 50.0000 -> 100 - 30 - 20 = 50: no break. Reported 49.0000
    would break (expected 500_000, reported 490_000)."""
    rows = list(entries)
    scale: dict[str, int] = {}
    for r in rows:
        scale[r.asset] = max(scale.get(r.asset, 0), r.decimals)
    running: dict[str, int | None] = dict.fromkeys(scale)
    breaks: list[ChainBreak] = []
    for r in rows:
        d = scale[r.asset]
        current = running[r.asset]
        if current is not None:
            current += rescale(r.delta_atoms, r.decimals, d)
        if r.balance_after_atoms is not None:
            reported = rescale(r.balance_after_atoms, r.decimals, d)
            if current is not None and current != reported:
                breaks.append(ChainBreak(r.asset, r.id, d, current, reported))
            current = reported
        running[r.asset] = current
    return breaks

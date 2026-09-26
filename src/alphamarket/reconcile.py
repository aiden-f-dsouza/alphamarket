"""Reconciliation (M2, TR-2 at MVP scale): does the ledger explain the venue
balance?

For each account: sync the ledger, read the venue cash balance, compute cash
from the anchor plus ledger flows, and record an EquitySnapshot. Divergence
beyond tolerance quarantines the strategy (its public page is hidden pending
review). Nothing is auto-unquarantined; a human looks first.

Tolerance: 0.1% of the venue balance, floored at one cent so a tiny account
is not quarantined by rounding on a single fee.
"""

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket.connectors.base import (
    AssetAmount,
    AssetVenueConnector,
    BalanceUnavailable,
    InstrumentKind,
    NormalizedBalance,
    NormalizedBalances,
    VenueConnector,
)
from alphamarket.connectors.registry import get_profile
from alphamarket.ingest import (
    account_entries,
    cash_asset_for,
    connector_for,
    is_asset_venue,
    quarantine_account,
    sync_account,
)
from alphamarket.ledger import (
    MICRO_DECIMALS,
    ChainBreak,
    aware,
    balance_chain_breaks,
    balance_vector,
    cash_at,
    rescale,
)
from alphamarket.models import (
    AnchorBalance,
    AssetReconciliation,
    EquitySnapshot,
    Fill,
    Settlement,
    Strategy,
    StrategyStatus,
    VenueAccount,
    utcnow,
)

logger = logging.getLogger(__name__)

TOLERANCE_BPS = 10  # 0.1%
MIN_TOLERANCE_MICROS = 10_000  # one cent


def tolerance_for(balance_micros: int) -> int:
    return max(abs(balance_micros) * TOLERANCE_BPS // 10_000, MIN_TOLERANCE_MICROS)


@dataclass
class Anchor:
    balance_micros: int
    at: datetime
    positions: list[dict]  # [{ticker, position_micros, cost_micros}]
    # Asset venues (ledger v2): the whole balance vector, stored as
    # anchor_balances rows by store_anchor_balances. None on binary venues.
    balances: NormalizedBalances | None = None


def take_anchor(
    connector: VenueConnector, settle_seconds: float = 5.0, attempts: int = 3
) -> Anchor:
    """Observe the venue balance at a moment with no fill activity nearby.

    The anchor timestamp is our own clock at request time. A fill landing
    within a second of that moment is ambiguous (is it in the balance or
    not?), so if one is seen we wait and re-observe rather than guess.
    """
    if _is_asset_connector(connector):
        return take_asset_anchor(connector, settle_seconds, attempts)
    for attempt in range(attempts):
        t0 = datetime.now(UTC)
        balance: NormalizedBalance = _balance_or_zero(connector)
        recent = connector.fetch_fills(since=t0.replace(microsecond=0))
        if not any(abs((f.filled_at - t0).total_seconds()) <= settle_seconds for f in recent):
            return Anchor(balance.balance_micros, t0, _positions_snapshot(connector))
        if attempt < attempts - 1:
            time.sleep(settle_seconds)
    # Activity is continuous; take the last observation and let the first
    # reconciliation run judge it.
    return Anchor(balance.balance_micros, t0, _positions_snapshot(connector))


def _balance_or_zero(connector: VenueConnector) -> NormalizedBalance:
    """Venues with no observable cash anchor at zero: the curve then measures
    P&L from connection, and returns are quoted on peak invested capital
    (stats.return_on_peak_invested). The methodology page says so."""
    try:
        return connector.fetch_balance()
    except BalanceUnavailable:
        return NormalizedBalance(balance_micros=0, as_of=datetime.now(UTC))


def _positions_snapshot(connector: VenueConnector) -> list[dict]:
    return [
        {
            "ticker": p.ticker,
            "position_micros": p.position_micros,
            "cost_micros": p.market_exposure_micros,
        }
        for p in connector.fetch_positions()
        if p.position_micros != 0
    ]


def reconcilable(account: VenueAccount) -> bool:
    """False for venues whose cash balance cannot be observed; those accounts
    stay in the fills-only tier and are never quarantined by reconciliation."""
    return get_profile(account.venue).balance_observable


def reconcile_account(db: Session, account: VenueAccount) -> EquitySnapshot:
    if not reconcilable(account):
        raise BalanceUnavailable(f"{account.venue} exposes no balance to reconcile against")
    if is_asset_venue(account.venue):
        return reconcile_asset_account(db, account)
    if account.anchor_balance_micros is None or account.anchor_at is None:
        raise ValueError(f"venue account {account.id} has no reconciliation anchor")
    strategy = db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))
    if strategy is None:
        raise ValueError(f"venue account {account.id} has no strategy")

    connector = connector_for(account)

    # Two passes: a fill landing between the ledger sync and the balance read
    # looks like a divergence. Re-sync once before believing it.
    for _attempt in range(2):
        sync_account(db, account)
        balance = connector.fetch_balance()
        computed = _computed_cash(db, account, strategy)
        divergence = balance.balance_micros - computed
        tolerance = tolerance_for(balance.balance_micros)
        if abs(divergence) <= tolerance:
            break

    snapshot = EquitySnapshot(
        strategy_id=strategy.id,
        as_of=utcnow(),
        venue_balance_micros=balance.balance_micros,
        computed_cash_micros=computed,
        divergence_micros=divergence,
        tolerance_micros=tolerance,
        reconciled=abs(divergence) <= tolerance,
    )
    db.add(snapshot)
    if not snapshot.reconciled and strategy.status != StrategyStatus.DELISTED:
        logger.warning(
            "strategy %s quarantined: venue %d vs computed %d micros (tolerance %d)",
            strategy.id,
            balance.balance_micros,
            computed,
            tolerance,
        )
        strategy.status = StrategyStatus.QUARANTINED
    db.commit()
    return snapshot


def _computed_cash(db: Session, account: VenueAccount, strategy: Strategy) -> int:
    fills = db.scalars(select(Fill).where(Fill.strategy_id == strategy.id)).all()
    settlements = db.scalars(select(Settlement).where(Settlement.strategy_id == strategy.id)).all()
    return cash_at(account.anchor_balance_micros, account.anchor_at, fills, settlements)


# ---- reconciliation v2: balance vectors (asset venues) ---------------------------
#
# Binary venues keep the M2 check above exactly. Asset venues compare the
# replayed vector (anchor_balances + every leg after the anchor) with
# fetch_balances() asset by asset, and check every venue running balance
# (balance_after_atoms) exactly. Any failure quarantines, with the reason in
# ledger_quarantines.


def _is_asset_connector(connector: VenueConnector) -> bool:
    if isinstance(connector, AssetVenueConnector):
        return True
    try:
        return get_profile(connector.venue).instrument is InstrumentKind.ASSET
    except (ValueError, AttributeError):
        return False


def asset_tolerance_atoms(asset: str, venue_atoms: int, decimals: int) -> int:
    """0.1% of the venue's amount, floored at one atom; USD keeps the M2 floor
    of one cent (tolerance_for), expressed at `decimals`."""
    pct = abs(venue_atoms) * TOLERANCE_BPS // 10_000
    if asset == "USD":
        cent = 10 ** (decimals - 2) if decimals >= 2 else 1
        return max(pct, cent)
    return max(pct, 1)


@dataclass(frozen=True)
class AssetCheck:
    asset: str
    decimals: int  # the finer of the venue's and the ledger's scale
    venue_atoms: int
    computed_atoms: int
    tolerance_atoms: int

    @property
    def divergence_atoms(self) -> int:
        return self.venue_atoms - self.computed_atoms

    @property
    def ok(self) -> bool:
        return abs(self.divergence_atoms) <= self.tolerance_atoms


def compare_vectors(computed: dict[str, AssetAmount], venue: list[AssetAmount]) -> list[AssetCheck]:
    """One check per asset in either vector (a missing side counts as 0),
    compared at the finer of the two scales."""
    reported = balance_vector((), start=venue)  # merges repeats, one scale per asset
    checks: list[AssetCheck] = []
    for asset in sorted(set(computed) | set(reported)):
        c, v = computed.get(asset), reported.get(asset)
        d = max(c.decimals if c else 0, v.decimals if v else 0)
        c_atoms = rescale(c.atoms, c.decimals, d) if c else 0
        v_atoms = rescale(v.atoms, v.decimals, d) if v else 0
        checks.append(
            AssetCheck(asset, d, v_atoms, c_atoms, asset_tolerance_atoms(asset, v_atoms, d))
        )
    return checks


def latest_anchor_balances(db: Session, account: VenueAccount) -> list[AnchorBalance]:
    """The newest anchor set of an asset account (empty if never anchored)."""
    rows = db.scalars(
        select(AnchorBalance).where(AnchorBalance.venue_account_id == account.id)
    ).all()
    if not rows:
        return []
    newest = max(aware(r.as_of) for r in rows)
    return [r for r in rows if aware(r.as_of) == newest]


def replayed_vector(
    db: Session, account: VenueAccount, anchor: list[AnchorBalance]
) -> dict[str, AssetAmount]:
    """anchor vector + every leg that occurred after the anchor."""
    anchor_at = max(aware(r.as_of) for r in anchor)
    return balance_vector(account_entries(db, account.id, after=anchor_at), start=anchor)


def _micros_toward_zero(atoms: int, decimals: int) -> int:
    """Display-only conversion for the summary EquitySnapshot."""
    if decimals <= MICRO_DECIMALS:
        return rescale(atoms, decimals, MICRO_DECIMALS)
    q = abs(atoms) // 10 ** (decimals - MICRO_DECIMALS)
    return q if atoms >= 0 else -q


def reconcile_asset_account(db: Session, account: VenueAccount) -> EquitySnapshot:
    """Reconcile v2. Writes one AssetReconciliation row per asset and a
    summary EquitySnapshot (the cash asset's numbers, overall verdict), so the
    worker, the API and the public pages read it like any other run."""
    anchor = latest_anchor_balances(db, account)
    if not anchor:
        raise ValueError(f"venue account {account.id} has no reconciliation anchor")
    strategy = db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))
    if strategy is None:
        raise ValueError(f"venue account {account.id} has no strategy")
    connector = connector_for(account)

    # Two passes, as M2: activity between the sync and the balance read
    # looks like a divergence until re-synced.
    for _attempt in range(2):
        sync_account(db, account)
        venue = connector.fetch_balances()
        checks = compare_vectors(replayed_vector(db, account, anchor), venue.assets)
        if all(c.ok for c in checks):
            break
    breaks = balance_chain_breaks(account_entries(db, account.id))

    now = utcnow()
    for c in checks:
        db.add(
            AssetReconciliation(
                venue_account_id=account.id,
                as_of=now,
                asset=c.asset,
                decimals=c.decimals,
                venue_atoms=c.venue_atoms,
                computed_atoms=c.computed_atoms,
                divergence_atoms=c.divergence_atoms,
                tolerance_atoms=c.tolerance_atoms,
                reconciled=c.ok,
            )
        )
    reconciled = all(c.ok for c in checks) and not breaks
    cash = next((c for c in checks if c.asset == cash_asset_for(account.venue)), None)
    snapshot = EquitySnapshot(
        strategy_id=strategy.id,
        as_of=now,
        venue_balance_micros=_micros_toward_zero(cash.venue_atoms, cash.decimals) if cash else 0,
        computed_cash_micros=_micros_toward_zero(cash.computed_atoms, cash.decimals) if cash else 0,
        divergence_micros=_micros_toward_zero(cash.divergence_atoms, cash.decimals) if cash else 0,
        tolerance_micros=_micros_toward_zero(cash.tolerance_atoms, cash.decimals) if cash else 0,
        reconciled=reconciled,
    )
    db.add(snapshot)
    if not reconciled:
        for c in checks:
            if not c.ok:
                quarantine_account(
                    db,
                    account,
                    f"{c.asset}: venue {c.venue_atoms} vs ledger {c.computed_atoms} atoms at "
                    f"{c.decimals} dp (tolerance {c.tolerance_atoms})",
                    ref=f"reconcile:{c.asset}",
                )
        for b in breaks:
            quarantine_account(db, account, _chain_reason(b), ref=f"ledger_entry:{b.entry_id}")
    db.commit()
    return snapshot


def _chain_reason(b: ChainBreak) -> str:
    return (
        f"{b.asset}: venue running balance {b.reported_atoms} at {b.decimals} dp does not "
        f"follow from the previous one plus the legs since ({b.expected_atoms})"
    )


def take_asset_anchor(
    connector: VenueConnector, settle_seconds: float = 5.0, attempts: int = 3
) -> Anchor:
    """take_anchor for asset venues: observe the balance VECTOR at a moment
    with no fill nearby, checking with one fetch_asset_fills_page request
    (fetch_fills is empty on these venues)."""
    for attempt in range(attempts):
        t0 = datetime.now(UTC)
        balances = connector.fetch_balances()
        page = connector.fetch_asset_fills_page(None, t0 - timedelta(seconds=settle_seconds))
        if not any(
            abs((aware(f.filled_at) - t0).total_seconds()) <= settle_seconds for f in page.items
        ):
            break
        if attempt < attempts - 1:
            time.sleep(settle_seconds)
    try:
        quote = get_profile(connector.venue).quote_asset
    except (ValueError, AttributeError):
        quote = "USD"
    cash = next((a for a in balances.assets if a.asset == quote), None)
    micros = _micros_toward_zero(cash.atoms, cash.decimals) if cash else 0
    return Anchor(micros, t0, [], balances)


def store_anchor_balances(db: Session, account: VenueAccount, anchor: Anchor) -> None:
    """Persist an asset anchor's vector (no-op for binary anchors). Repeated
    assets in the venue's vector are summed; the first prices are kept."""
    if anchor.balances is None:
        return
    merged: dict[str, AnchorBalance] = {}
    for a in anchor.balances.assets:
        row = merged.get(a.asset)
        if row is None:
            merged[a.asset] = AnchorBalance(
                venue_account_id=account.id,
                asset=a.asset,
                atoms=a.atoms,
                decimals=a.decimals,
                entry_price=a.entry_price,
                mark_price=a.mark_price,
                as_of=anchor.at,
            )
            continue
        d = max(row.decimals, a.decimals)
        row.atoms = rescale(row.atoms, row.decimals, d) + rescale(a.atoms, a.decimals, d)
        row.decimals = d
    db.add_all(merged.values())

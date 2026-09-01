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
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket.connectors.base import NormalizedBalance, VenueConnector
from alphamarket.connectors.registry import build_connector
from alphamarket.ingest import sync_account
from alphamarket.ledger import cash_at
from alphamarket.models import (
    EquitySnapshot,
    Fill,
    Settlement,
    Strategy,
    StrategyStatus,
    VenueAccount,
    utcnow,
)
from alphamarket.vault import decrypt_secret

logger = logging.getLogger(__name__)

TOLERANCE_BPS = 10  # 0.1%
MIN_TOLERANCE_MICROS = 10_000  # one cent


def tolerance_for(balance_micros: int) -> int:
    return max(abs(balance_micros) * TOLERANCE_BPS // 10_000, MIN_TOLERANCE_MICROS)


@dataclass
class Anchor:
    balance_micros: int
    at: datetime


def take_anchor(
    connector: VenueConnector, settle_seconds: float = 5.0, attempts: int = 3
) -> Anchor:
    """Observe the venue balance at a moment with no fill activity nearby.

    The anchor timestamp is our own clock at request time. A fill landing
    within a second of that moment is ambiguous (is it in the balance or
    not?), so if one is seen we wait and re-observe rather than guess.
    """
    for attempt in range(attempts):
        t0 = datetime.now(UTC)
        balance: NormalizedBalance = connector.fetch_balance()
        recent = connector.fetch_fills(since=t0.replace(microsecond=0))
        if not any(abs((f.filled_at - t0).total_seconds()) <= settle_seconds for f in recent):
            return Anchor(balance_micros=balance.balance_micros, at=t0)
        if attempt < attempts - 1:
            time.sleep(settle_seconds)
    # Activity is continuous; take the last observation and let the first
    # reconciliation run judge it.
    return Anchor(balance_micros=balance.balance_micros, at=t0)


def _connector_for(account: VenueAccount) -> VenueConnector:
    if account.encrypted_private_key is None:
        raise ValueError(f"venue account {account.id} has no stored credential")
    return build_connector(
        account.venue,
        account.venue_env,
        account.key_id,
        decrypt_secret(account.encrypted_private_key),
    )


def reconcile_account(db: Session, account: VenueAccount) -> EquitySnapshot:
    if account.anchor_balance_micros is None or account.anchor_at is None:
        raise ValueError(f"venue account {account.id} has no reconciliation anchor")
    strategy = db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))
    if strategy is None:
        raise ValueError(f"venue account {account.id} has no strategy")

    connector = _connector_for(account)

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

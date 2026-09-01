"""Venue key connect flow (M1.2).

POST /venues: user pastes key id + private key PEM. We verify the credential
works, reject keys with write scope (invariant #4 / SEC-1 at MVP scale),
encrypt the PEM at rest, create the account's strategy, and run the first
historical import inline so the user sees their data immediately.

The private key is accepted in the request body, used, encrypted, and never
appears in any response, log line, or error detail.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket.auth import current_user
from alphamarket.config import get_settings
from alphamarket.connectors.registry import SUPPORTED_VENUES, build_connector
from alphamarket.db import get_db
from alphamarket.ingest import sync_account
from alphamarket.models import Strategy, User, VenueAccount, VenueAccountStatus, utcnow
from alphamarket.reconcile import reconcile_account, take_anchor
from alphamarket.vault import encrypt_secret

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/venues", tags=["venues"])


class ConnectIn(BaseModel):
    venue: str = "kalshi"
    key_id: str = Field(min_length=1, max_length=120)
    private_key_pem: str = Field(min_length=1)
    strategy_name: str = Field(min_length=2, max_length=120)


class VenueAccountOut(BaseModel):
    """Public view of a connection. Deliberately excludes key material."""

    id: int
    venue: str
    venue_env: str
    status: str
    scope_checked_at: datetime | None = None
    last_synced_at: datetime | None = None
    last_sync_error: str | None = None
    strategy_id: int | None = None
    strategy_name: str | None = None


class ConnectOut(VenueAccountOut):
    imported_fills: int
    imported_settlements: int


def _to_out(account: VenueAccount, strategy: Strategy | None) -> dict:
    return {
        "id": account.id,
        "venue": account.venue,
        "venue_env": account.venue_env,
        "status": account.status,
        "scope_checked_at": account.scope_checked_at,
        "last_synced_at": account.last_synced_at,
        "last_sync_error": account.last_sync_error,
        "strategy_id": strategy.id if strategy else None,
        "strategy_name": strategy.name if strategy else None,
    }


@router.post("", response_model=ConnectOut, status_code=status.HTTP_201_CREATED)
def connect(payload: ConnectIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if payload.venue not in SUPPORTED_VENUES:
        raise HTTPException(status_code=422, detail=f"Unsupported venue: {payload.venue}")

    settings = get_settings()
    try:
        connector = build_connector(
            payload.venue, settings.kalshi_env, payload.key_id, payload.private_key_pem
        )
        connector.check_connection()
    except HTTPException:
        raise
    except Exception:
        # Deliberately generic: no exception detail can be allowed to echo key
        # material back, so none of it leaves the server.
        raise HTTPException(
            status_code=422,
            detail="Could not authenticate with the venue using this key.",
        ) from None

    writable = connector.check_write_scope()
    if writable:
        raise HTTPException(
            status_code=422,
            detail=(
                "This API key has write (trading) scope. AlphaMarket only accepts "
                "read-only keys: create a new key with read-only scope and connect that."
            ),
        )

    # Reconciliation anchor: the balance at this moment is what every later
    # equity number is measured from (see ledger.py).
    anchor = take_anchor(connector)

    account = VenueAccount(
        user_id=user.id,
        venue=payload.venue,
        venue_env=settings.kalshi_env,
        key_id=payload.key_id,
        encrypted_private_key=encrypt_secret(payload.private_key_pem),
        status=VenueAccountStatus.ACTIVE,
        # None from the probe means "undetermined", recorded as unchecked.
        scope_checked_at=utcnow() if writable is False else None,
        anchor_balance_micros=anchor.balance_micros,
        anchor_at=anchor.at,
    )
    db.add(account)
    db.flush()
    strategy = Strategy(user_id=user.id, venue_account_id=account.id, name=payload.strategy_name)
    db.add(strategy)
    db.commit()

    result = sync_account(db, account)
    return {
        **_to_out(account, strategy),
        "imported_fills": result.new_fills,
        "imported_settlements": result.new_settlements,
    }


@router.get("", response_model=list[VenueAccountOut])
def list_accounts(db: Session = Depends(get_db), user: User = Depends(current_user)):
    accounts = db.scalars(select(VenueAccount).where(VenueAccount.user_id == user.id)).all()
    out = []
    for account in accounts:
        strategy = db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))
        out.append(_to_out(account, strategy))
    return out


@router.post("/{account_id}/sync", response_model=ConnectOut)
def manual_sync(account_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    account = _owned_account(db, account_id, user)
    if account.status == VenueAccountStatus.DISCONNECTED:
        raise HTTPException(status_code=409, detail="Account is disconnected")
    try:
        result = sync_account(db, account)
    except Exception:
        logger.exception("manual sync failed for account %s", account.id)
        raise HTTPException(status_code=502, detail="Sync failed; see connection status.") from None
    strategy = db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))
    return {
        **_to_out(account, strategy),
        "imported_fills": result.new_fills,
        "imported_settlements": result.new_settlements,
    }


class SnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    strategy_id: int
    as_of: datetime
    venue_balance_micros: int
    computed_cash_micros: int
    divergence_micros: int
    tolerance_micros: int
    reconciled: bool


@router.post("/{account_id}/reconcile", response_model=SnapshotOut)
def manual_reconcile(
    account_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Run reconciliation now (the worker also runs it nightly)."""
    account = _owned_account(db, account_id, user)
    if account.status == VenueAccountStatus.DISCONNECTED:
        raise HTTPException(status_code=409, detail="Account is disconnected")
    try:
        return reconcile_account(db, account)
    except Exception:
        logger.exception("manual reconcile failed for account %s", account.id)
        raise HTTPException(status_code=502, detail="Reconcile failed; see status.") from None


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def disconnect(account_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Disconnect erases the encrypted key immediately (invariant #4). The
    already-ingested ledger stays: it is append-only history the track record
    was built on, and the methodology page says exactly that."""
    account = _owned_account(db, account_id, user)
    account.encrypted_private_key = None
    account.status = VenueAccountStatus.DISCONNECTED
    db.commit()


def _owned_account(db: Session, account_id: int, user: User) -> VenueAccount:
    account = db.get(VenueAccount, account_id)
    if account is None or account.user_id != user.id:
        # 404 for both cases: do not reveal other users' account ids exist.
        raise HTTPException(status_code=404, detail="Venue account not found")
    return account

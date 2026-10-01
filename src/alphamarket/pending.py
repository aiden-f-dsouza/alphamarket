"""PENDING venue accounts (wave2, w2-p8-connect): a connect started but not
finished (SnapTrade portal round trip).

STUB. Contract: abandon_stale(db, now) -> int
    Accounts PENDING for longer than settings.pending_connect_ttl_hours:
    erase the credential (credentials.disconnect), delete the SnapTrade user
    (registry wrapper), set DISCONNECTED. Returns accounts abandoned.
"""

from datetime import datetime

from sqlalchemy.orm import Session


def abandon_stale(db: Session, now: datetime) -> int:
    return 0

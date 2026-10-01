"""OAuth token refresh in the worker (wave2, w2-account-security).

STUB. Contract:

tick(session_factory) -> int
    Every 15 min (worker.main): enqueue OAUTH_REFRESH for each active
    OAUTH2 account whose venue_credentials.expires_at <= now + 2 x margin.

run(ctx) -> None
    The OAUTH_REFRESH handler: open the credential, oauth.ensure_fresh,
    credentials.store the new record (expires_at set), audit
    CREDENTIAL_REFRESH. invalid_grant -> account ERROR ("Reconnect") + alert.

refresh_if_due(db, account, opened) -> str | None
    Called by ingest.connector_for before building an OAUTH2 connector: the
    fresh secret (already stored) when a refresh was due, else None.
"""

from sqlalchemy.orm import Session, sessionmaker

from alphamarket.models import VenueAccount


def tick(session_factory: sessionmaker) -> int:
    return 0


def run(ctx) -> None:
    return None


def refresh_if_due(db: Session, account: VenueAccount, opened) -> str | None:
    return None

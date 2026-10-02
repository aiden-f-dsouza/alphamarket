"""Wave 2 seams the workstreams could not test alone: SnapTrade release on
suspension, the OAuth authorize origin in CSP form-action, one tier
definition. No network (invariant 6)."""

from datetime import UTC, datetime

from alphamarket import public, scopecheck, venues
from alphamarket.connectors import registry
from alphamarket.connectors.base import ReadOnlyEvidence, ReadOnlyProof
from alphamarket.models import User, VenueAccount, VenueAccountStatus


def _account(db, venue: str, key_id: str = "k") -> VenueAccount:
    user = User(email=f"{venue}@example.com", password_hash="x", display_name="U")
    db.add(user)
    db.flush()
    account = VenueAccount(
        user_id=user.id, venue=venue, venue_env="prod", key_id=key_id, claimed_at=datetime.now(UTC)
    )
    db.add(account)
    db.commit()
    return account


def test_suspending_a_writable_snaptrade_link_deletes_the_aggregator_user(
    session_factory, monkeypatch
):
    deleted = []
    monkeypatch.setattr(
        registry, "aggregator_delete_user", lambda venue, user_id: deleted.append((venue, user_id))
    )
    evidence = ReadOnlyEvidence(
        method=ReadOnlyProof.AGGREGATOR_FLAG, read_only=False, checked_at=datetime.now(UTC)
    )
    with session_factory() as db:
        account = _account(db, "snaptrade", key_id="snap-user-1")
        scopecheck.suspend(db, account, evidence)
        assert account.status == VenueAccountStatus.SUSPENDED
    assert deleted == [("snaptrade", "snap-user-1")]


def test_suspending_a_key_venue_calls_no_aggregator(session_factory, monkeypatch):
    deleted = []
    monkeypatch.setattr(registry, "aggregator_delete_user", lambda *a: deleted.append(a))
    evidence = ReadOnlyEvidence(
        method=ReadOnlyProof.SCOPE_ENDPOINT, read_only=False, checked_at=datetime.now(UTC)
    )
    with session_factory() as db:
        scopecheck.suspend(db, _account(db, "kraken"), evidence)
    assert deleted == []


def test_csp_form_action_allows_the_oauth_authorize_origin(client):
    csp = client.get("/").headers["content-security-policy"]
    assert "form-action 'self' https://app.alpaca.markets;" in csp


def test_one_tier_definition_for_api_and_pages(session_factory):
    with session_factory() as db:
        ibkr, kraken = _account(db, "ibkr"), _account(db, "kraken")
        assert venues.verification_tier(ibkr) == public.verification_tier(ibkr) == "daily_verified"
        assert venues.verification_tier(kraken) == "reconciled"

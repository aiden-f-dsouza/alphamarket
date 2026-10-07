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


def test_a_kraken_variant_is_valued_at_its_underlying_close(session_factory):
    # ETH2.S (staked ETH) is worth one ETH: it gets ETH's 3,000 close under its own code.
    from datetime import date

    from alphamarket import valuation_db
    from alphamarket.models import Instrument, PriceDaily
    from alphamarket.valuation import PriceIn

    day = date(2026, 9, 30)
    with session_factory() as db:
        eth = Instrument(
            venue="kraken",
            symbol="XETHZUSD",
            type="spot",
            base_asset="ETH",
            quote_asset="USD",
            qty_decimals=8,
        )
        db.add(eth)
        db.flush()
        db.add(
            PriceDaily(
                instrument_id=eth.id,
                day=day,
                close="3000",
                usd_micros_per_unit=3_000_000_000,
                source="kraken",
            )
        )
        db.commit()
        got = valuation_db.price_inputs(db, {"ETH", "ETH2.S"}, None, None, ("kraken",))
        assert got == [
            PriceIn("ETH", day, 3_000_000_000, close="3000"),
            PriceIn("ETH2.S", day, 3_000_000_000, close="3000"),
        ]
        # Another venue has no such mapping: ETH2.S stays unpriced there.
        assert [
            p.asset for p in valuation_db.price_inputs(db, {"ETH2.S"}, None, None, ("binanceus",))
        ] == []

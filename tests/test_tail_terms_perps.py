"""Paper tail terms name funding and liquidation for every perp venue, read
from VenueProfile.perps rather than a venue name."""

import pytest

from alphamarket.connectors.registry import get_profile
from alphamarket.tail_terms import paper_terms


@pytest.mark.parametrize("venue", ["hyperliquid", "lighter", "pacifica"])
def test_perp_venues_state_funding_and_liquidation(venue):
    assert get_profile(venue).perps
    terms = paper_terms(venue)
    assert terms["costs"].endswith(", plus funding")
    fees = dict(terms["rows"])["Fees"]
    assert "Perp funding is paid or received every hour" in fees
    assert "liquidated" in fees


def test_spot_venue_mentions_no_funding():
    assert not get_profile("kraken").perps
    terms = paper_terms("kraken")
    assert "funding" not in terms["costs"]
    assert "funding" not in dict(terms["rows"])["Fees"]

"""Venue registry/factory: the only place core code touches venue modules.

New venue = implement VenueConnector in its own module, register a builder
here. Nothing outside connectors/ may import kalshi.py directly.
"""

from collections.abc import Callable

from alphamarket.config import KALSHI_BASE_URLS
from alphamarket.connectors.base import VenueConnector
from alphamarket.connectors.kalshi import KalshiConnector, load_private_key_pem

SUPPORTED_VENUES = ("kalshi",)


def _build_kalshi(venue_env: str, key_id: str, private_key_pem: str) -> VenueConnector:
    return KalshiConnector(
        base_url=KALSHI_BASE_URLS[venue_env],
        key_id=key_id,
        private_key=load_private_key_pem(private_key_pem),
    )


_BUILDERS: dict[str, Callable[[str, str, str], VenueConnector]] = {
    "kalshi": _build_kalshi,
}


def build_connector(
    venue: str, venue_env: str, key_id: str, private_key_pem: str
) -> VenueConnector:
    try:
        builder = _BUILDERS[venue]
    except KeyError:
        raise ValueError(f"Unsupported venue: {venue!r}") from None
    return builder(venue_env, key_id, private_key_pem)

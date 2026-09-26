"""Venue registry/factory: the only place core code touches venue modules.

New venue = implement VenueConnector (and optionally MarketDataSource) in
its own module, describe it with a VenueProfile, register both here.
Nothing outside connectors/ may import kalshi.py or polymarket.py directly.
"""

from collections.abc import Callable

from alphamarket.config import KALSHI_BASE_URLS, POLYMARKET_BASE_URLS, get_settings
from alphamarket.connectors.base import (
    CredentialKind,
    InstrumentKind,
    MarketDataSource,
    OwnershipProof,
    PaperFeeModel,
    RateBudget,
    RateScope,
    ReadOnlyProof,
    VenueConnector,
    VenueProfile,
)
from alphamarket.connectors.kalshi import KalshiConnector, KalshiMarketData, load_private_key_pem
from alphamarket.connectors.polymarket import PolymarketConnector, PolymarketMarketData
from alphamarket.connectors.snaptrade import SnapTradeConnector, SnapTradeMarketData

PROFILES: dict[str, VenueProfile] = {
    "kalshi": VenueProfile(
        venue="kalshi",
        display_name="Kalshi",
        envs=("demo", "prod"),
        credential=CredentialKind.RSA_PEM,
        key_label="API key id",
        secret_label="Private key (PEM)",
        key_help=(
            "In Kalshi: Account → API keys → New key. Choose read-only. "
            "Download the PEM file once; Kalshi won't show it again."
        ),
        balance_observable=True,
        instrument=InstrumentKind.BINARY,
        tick_micros=10_000,  # one cent
        contract_step_micros=10_000,  # 0.01 contract (count_fp has 2 dp)
        fee_rate_bps=700,  # 7% x count x p x (1-p), verified against a live fill
        fee_rounding_micros=100,  # fee_cost has four decimals
        ownership=OwnershipProof.KEY,
    ),
    "polymarket": VenueProfile(
        venue="polymarket",
        display_name="Polymarket",
        envs=("prod",),
        credential=CredentialKind.ADDRESS,
        key_label="Wallet address",
        secret_label=None,
        key_help=(
            "Your Polymarket profile URL ends in your wallet address (0x…). "
            "Nothing secret is involved: the address is public on-chain data. "
            "You prove it is yours by putting a Koine code in your profile bio."
        ),
        balance_observable=False,
        instrument=InstrumentKind.BINARY,
        tick_micros=1_000,  # $0.001, the finest CLOB tick seen
        contract_step_micros=10_000,  # 0.01 token
        fee_rate_bps=500,  # 5% x size x p x (1-p), verified against activity rows
        fee_rounding_micros=1,  # usdcSize carries six decimals
        ownership=OwnershipProof.PROFILE_CODE,
    ),
    # ---- venue-snaptrade ----
    "snaptrade": VenueProfile(
        venue="snaptrade",
        display_name="SnapTrade",
        envs=("prod",),
        credential=CredentialKind.AGGREGATOR,
        key_label="SnapTrade user id",
        secret_label="SnapTrade user secret",
        key_help=(
            "Chosen automatically when you connect a brokerage through SnapTrade's "
            "Connection Portal (Robinhood, Schwab, Fidelity, Webull and more). You "
            "never type this in; it is generated when you click Connect."
        ),
        balance_observable=True,  # daily snapshot, not real-time (history_depth)
        instrument=InstrumentKind.ASSET,
        # The binary-era tick/step/fee fields do not describe an aggregated
        # equities account (no fixed tick, no per-share paper fee here);
        # kept at harmless minimums so nothing downstream divides by zero.
        tick_micros=1,
        contract_step_micros=1,
        fee_rate_bps=0,
        fee_rounding_micros=1,
        ownership=OwnershipProof.KEY,  # holding a valid userId+userSecret proves it
        readonly_proof=ReadOnlyProof.AGGREGATOR_FLAG,
        transfers_observable=True,  # CONTRIBUTION/WITHDRAWAL/TRANSFER activities
        history_depth="all",  # SnapTrade backfills full broker history on connect
        quote_asset="USD",
        tos_doc="docs/venues/snaptrade.md",
        # Connecting an account and computing a PRIVATE track record needs no
        # approval (self-serve developer account). PUBLIC display is a
        # different question: the Developer Terms bar re-packaging End User
        # data to third parties "without the express written consent of both
        # the End User and SnapTrade" -- see docs/venues/snaptrade.md, checked
        # 2026-09-26. True here gates public strategy pages/marketplace
        # listings until that written consent exists; flip to False once it
        # does (contract_request, not a code change).
        requires_partner_approval=True,
        daily_resolution=True,  # "cached and refreshed once a day" per SnapTrade docs
        rate_budgets=(
            RateBudget(
                # Customer-level (commercial partner) limit: 250 req/min shared
                # across every connected user. Per-account 10 req/min limits are
                # documented as personal-tier only, per
                # https://docs.snaptrade.com/docs/ratelimiting , checked
                # 2026-09-26. [VERIFY] against the live commercial dashboard once
                # an account exists.
                scope=RateScope.CREDENTIAL,
                capacity=250,
                refill_per_minute=250,
            ),
        ),
        paper_fee_model=PaperFeeModel.US_EQUITY_REGULATORY,
    ),
}

SUPPORTED_VENUES = tuple(PROFILES)


def get_profile(venue: str) -> VenueProfile:
    try:
        return PROFILES[venue]
    except KeyError:
        raise ValueError(f"Unsupported venue: {venue!r}") from None


def _build_kalshi(venue_env: str, key_id: str, secret: str | None) -> VenueConnector:
    if not secret:
        raise ValueError("Kalshi needs a private key PEM")
    return KalshiConnector(
        base_url=KALSHI_BASE_URLS[venue_env],
        key_id=key_id,
        private_key=load_private_key_pem(secret),
    )


def _build_polymarket(venue_env: str, key_id: str, secret: str | None) -> VenueConnector:
    urls = POLYMARKET_BASE_URLS[venue_env]
    return PolymarketConnector(
        address=key_id, data_url=urls["data"], clob_url=urls["clob"], gamma_url=urls["gamma"]
    )


def _build_snaptrade(venue_env: str, key_id: str, secret: str | None) -> VenueConnector:
    # ---- venue-snaptrade ----
    settings = get_settings()
    if not settings.snaptrade_client_id or not settings.snaptrade_consumer_key:
        raise ValueError("SnapTrade needs snaptrade_client_id/snaptrade_consumer_key configured")
    return SnapTradeConnector(
        client_id=settings.snaptrade_client_id,
        consumer_key=settings.snaptrade_consumer_key,
        user_id=key_id,
        user_secret=secret,
    )


_BUILDERS: dict[str, Callable[[str, str, str | None], VenueConnector]] = {
    "kalshi": _build_kalshi,
    "polymarket": _build_polymarket,
    "snaptrade": _build_snaptrade,  # ---- venue-snaptrade ----
}


def build_connector(
    venue: str,
    venue_env: str,
    key_id: str,
    secret: str | None = None,
    account_ref: str | None = None,
) -> VenueConnector:
    """`key_id` is the public identifier (Kalshi key id, Polymarket wallet
    address); `secret` is the credential, None for address-keyed venues
    (CredentialKind documents both per kind). `account_ref` picks one venue
    account when a credential spans several (VenueConnector.account_ref)."""
    try:
        builder = _BUILDERS[venue]
    except KeyError:
        raise ValueError(f"Unsupported venue: {venue!r}") from None
    if venue_env not in get_profile(venue).envs:
        raise ValueError(f"{venue} has no {venue_env!r} environment")
    connector = builder(venue_env, key_id, secret)
    connector.account_ref = account_ref
    return connector


_MARKET_DATA_BUILDERS: dict[str, Callable[[str], MarketDataSource]] = {
    "kalshi": lambda venue_env: KalshiMarketData(base_url=KALSHI_BASE_URLS[venue_env]),
    "polymarket": lambda venue_env: PolymarketMarketData(
        clob_url=POLYMARKET_BASE_URLS[venue_env]["clob"]
    ),
    "snaptrade": lambda venue_env: SnapTradeMarketData(),  # ---- venue-snaptrade ----
}


def build_market_data(venue: str, venue_env: str) -> MarketDataSource:
    """Public (unauthenticated) market data for a venue environment."""
    try:
        builder = _MARKET_DATA_BUILDERS[venue]
    except KeyError:
        raise ValueError(f"Unsupported venue: {venue!r}") from None
    return builder(venue_env)

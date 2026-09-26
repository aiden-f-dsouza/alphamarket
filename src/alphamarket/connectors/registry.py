"""Venue registry/factory: the only place core code touches venue modules.

New venue = implement VenueConnector (and optionally MarketDataSource) in
its own module, describe it with a VenueProfile, register both here.
Nothing outside connectors/ may import kalshi.py or polymarket.py directly.
"""

from collections.abc import Callable

from alphamarket.config import KALSHI_BASE_URLS, POLYMARKET_BASE_URLS
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
from alphamarket.connectors.binanceus import BASE_URL as BINANCEUS_BASE_URL
from alphamarket.connectors.binanceus import BinanceUSConnector, BinanceUSMarketData
from alphamarket.connectors.kalshi import KalshiConnector, KalshiMarketData, load_private_key_pem
from alphamarket.connectors.polymarket import PolymarketConnector, PolymarketMarketData

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
    # ---- venue-binanceus ----
    "binanceus": VenueProfile(
        venue="binanceus",
        display_name="Binance.US",
        envs=("prod",),
        credential=CredentialKind.KEY_SECRET,
        key_label="API key",
        secret_label="Secret key",
        key_help=(
            "In Binance.US: Account -> API Management -> Create API. Enable "
            "Reading only -- leave Enable Spot & Margin Trading and Enable "
            "Withdrawals OFF -- and restrict the key to Koine's published IPs."
        ),
        balance_observable=True,
        instrument=InstrumentKind.ASSET,
        # Binary-era fields, unused for ASSET-instrument venues (v2 prices and
        # sizes live per-instrument: NormalizedInstrument.qty_step_atoms/
        # qty_decimals and NormalizedAssetFill.price). Kept at a nominal 1
        # micro floor so nothing that still reads them divides by zero.
        tick_micros=1,
        contract_step_micros=1,
        # Paper fee: bps_of_notional x notional (see paper_fee_model below).
        # Binance.US moved every pair to 0% maker / 0.02% taker in April 2026
        # (0.01% on Tier-0 pairs like BTC/USD, tightening further past $500M
        # 30-day volume); 2 bps is the general taker rate. Checked
        # https://www.binance.us/fees 2026-09-26. [VERIFY] per-pair tier at
        # connect time -- a real account may sit on the finer Tier-0 rate.
        fee_rate_bps=2,
        fee_rounding_micros=1,
        ownership=OwnershipProof.KEY,
        readonly_proof=ReadOnlyProof.SCOPE_ENDPOINT,
        transfers_observable=True,
        # myTrades itself has no documented depth limit (id-space pagination),
        # but deposit/withdraw/convert/dust histories are windowed to <90d per
        # call (docs/venues/binanceus.md "known gaps"), so events realistically
        # only reach back that far without a standing incremental sync.
        history_depth="90d",
        quote_asset="USD",
        tos_doc="docs/venues/binanceus.md",
        requires_partner_approval=False,
        daily_resolution=False,
        rate_budgets=(
            RateBudget(
                scope=RateScope.IP,
                capacity=6000,
                refill_per_minute=6000,  # REQUEST_WEIGHT bucket refills to 6000/min
                costs=(
                    ("account", 10),
                    ("myTrades", 10),
                    ("exchangeInfo", 20),
                    ("apiRestrictions", 1),  # [VERIFY] weight not in fetched docs excerpt
                    ("depositHistory", 1),  # [VERIFY]
                    ("withdrawHistory", 1),  # [VERIFY]
                    ("convertHistory", 1),  # [VERIFY]
                    ("dustLog", 1),  # [VERIFY]
                    ("klines", 1),
                    ("bookTicker", 1),
                ),
                default_cost=1,
            ),
        ),
        paper_fee_model=PaperFeeModel.BPS_OF_NOTIONAL,
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


def _build_binanceus(venue_env: str, key_id: str, secret: str | None) -> VenueConnector:
    if not secret:
        raise ValueError("Binance.US needs a secret key")
    return BinanceUSConnector(base_url=BINANCEUS_BASE_URL, api_key=key_id, api_secret=secret)


_BUILDERS: dict[str, Callable[[str, str, str | None], VenueConnector]] = {
    "kalshi": _build_kalshi,
    "polymarket": _build_polymarket,
    # ---- venue-binanceus ----
    "binanceus": _build_binanceus,
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
    # ---- venue-binanceus ----
    "binanceus": lambda venue_env: BinanceUSMarketData(base_url=BINANCEUS_BASE_URL),
}


def build_market_data(venue: str, venue_env: str) -> MarketDataSource:
    """Public (unauthenticated) market data for a venue environment."""
    try:
        builder = _MARKET_DATA_BUILDERS[venue]
    except KeyError:
        raise ValueError(f"Unsupported venue: {venue!r}") from None
    return builder(venue_env)

"""Venue registry/factory: the only place core code touches venue modules.

New venue = implement VenueConnector (and optionally MarketDataSource) in
its own module, describe it with a VenueProfile, register both here.
Nothing outside connectors/ may import kalshi.py or polymarket.py directly.
"""

from collections.abc import Callable

from alphamarket.config import KALSHI_BASE_URLS, POLYMARKET_BASE_URLS
from alphamarket.connectors import hyperliquid
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
from alphamarket.connectors.kraken import KRAKEN_API_URL, KrakenConnector, KrakenMarketData
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
    # ---- venue-kraken ----
    "kraken": VenueProfile(
        venue="kraken",
        display_name="Kraken",
        envs=("prod",),  # Kraken has no spot sandbox
        credential=CredentialKind.KEY_SECRET,
        key_label="API key",
        secret_label="Private key",
        key_help=(
            "In Kraken: Settings → Connections & API → Create API key. Tick ONLY "
            "Query Funds, Query Closed Orders & Trades and Query Ledger Entries. "
            "Leave every trading, deposit, withdraw and Earn permission off: Koine "
            "checks, and refuses keys that can trade or withdraw."
        ),
        balance_observable=True,
        instrument=InstrumentKind.ASSET,
        # Binary-era fields. For an ASSET venue the paper mirror sizes and
        # rounds per instrument (NormalizedInstrument.qty_decimals, lot_decimals
        # from AssetPairs), so these only say "no extra rounding here".
        tick_micros=1,
        contract_step_micros=1,
        fee_rate_bps=80,  # Kraken Pro spot taker, tier 1 ($0+ 30-day volume), 2026-09-26
        fee_rounding_micros=100,  # ZUSD ledger fees carry four decimals
        ownership=OwnershipProof.KEY,
        readonly_proof=ReadOnlyProof.ORDER_PROBE,
        transfers_observable=True,  # Ledgers: deposit, withdrawal, transfer, staking
        history_depth="all",  # TradesHistory and Ledgers are all-time
        quote_asset="USD",
        tos_doc="docs/venues/kraken.md",
        # docs/venues/kraken.md status needs_approval: the 2026-09-14 Terms
        # restrict third-party applications without written consent. Flip to
        # False once Kraken's written OK is on file.
        requires_partner_approval=True,
        daily_resolution=False,
        rate_budgets=(
            # Per API key. Starter: max counter 15, decay 0.33/s = 20/min
            # (docs.kraken.com spot-rest-ratelimits). History calls: the docs
            # page says +2, support.kraken.com (2026-08-10) says +4; the
            # higher is charged until verified. [VERIFY]
            RateBudget(
                scope=RateScope.CREDENTIAL,
                capacity=15,
                refill_per_minute=20,
                costs=(("TradesHistory", 4), ("Ledgers", 4)),
            ),
            # Public endpoints, per caller IP: "1 per second (or less) would
            # remain within the rate limits" (support.kraken.com). [VERIFY]
            RateBudget(scope=RateScope.IP, capacity=1, refill_per_minute=60),
        ),
        paper_fee_model=PaperFeeModel.BPS_OF_NOTIONAL,
    ),
    # ---- venue-hyperliquid ----
    "hyperliquid": VenueProfile(
        venue="hyperliquid",
        display_name="Hyperliquid",
        envs=("prod",),
        credential=CredentialKind.ADDRESS,
        key_label="Wallet address",
        secret_label=None,
        key_help=(
            "The 0x address of your Hyperliquid account (the master account, not an "
            "API wallet). Nothing secret is involved: Hyperliquid history is public. "
            "You prove the address is yours by signing a Koine message with the "
            "wallet; signing is not a transaction and cannot move funds."
        ),
        balance_observable=True,
        instrument=InstrumentKind.ASSET,
        # Binary-era fields, meaningless for an asset venue beyond these notes:
        # prices have 5 significant figures and at most 6 dp on perps, so the
        # finest possible tick is 1 micro; quantity steps are per instrument
        # (qty_step_atoms at szDecimals), so the global step is 1 micro.
        tick_micros=1,
        contract_step_micros=1,
        # Taker rate, base tier: perps 0.045% (spot 0.070%), docs "Fees",
        # checked 2026-09-26. The field is whole bps, so 4.5 rounds UP to 5
        # (paper followers are charged slightly more, never less).
        fee_rate_bps=5,
        fee_rounding_micros=1,  # fees print 6 dp in USDC
        # No OwnershipProof value fits a signed message yet (contract request):
        # PROFILE_CODE routes the account through the claim flow, where
        # verify_claim checks an EIP-191 signature instead of a bio.
        ownership=OwnershipProof.PROFILE_CODE,
        readonly_proof=ReadOnlyProof.NONE,
        transfers_observable=True,
        history_depth="10k_fills",
        quote_asset="USDC",
        tos_doc="docs/venues/hyperliquid.md",
        requires_partner_approval=False,
        daily_resolution=False,
        rate_budgets=(hyperliquid.RATE_BUDGET,),
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


def _build_kraken(venue_env: str, key_id: str, secret: str | None) -> VenueConnector:
    if not secret:
        raise ValueError("Kraken needs the API private key")
    return KrakenConnector(api_key=key_id, api_secret=secret, base_url=KRAKEN_API_URL)


def _build_hyperliquid(venue_env: str, key_id: str, secret: str | None) -> VenueConnector:
    return hyperliquid.HyperliquidConnector(
        address=key_id, info_url=hyperliquid.INFO_URLS[venue_env]
    )


_BUILDERS: dict[str, Callable[[str, str, str | None], VenueConnector]] = {
    "kalshi": _build_kalshi,
    "polymarket": _build_polymarket,
    "kraken": _build_kraken,  # venue-kraken
    "hyperliquid": _build_hyperliquid,
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
    "kraken": lambda venue_env: KrakenMarketData(base_url=KRAKEN_API_URL),  # venue-kraken
    "hyperliquid": lambda venue_env: hyperliquid.HyperliquidMarketData(
        info_url=hyperliquid.INFO_URLS[venue_env]
    ),
}


def build_market_data(venue: str, venue_env: str) -> MarketDataSource:
    """Public (unauthenticated) market data for a venue environment."""
    try:
        builder = _MARKET_DATA_BUILDERS[venue]
    except KeyError:
        raise ValueError(f"Unsupported venue: {venue!r}") from None
    return builder(venue_env)

"""Venue registry/factory: the only place core code touches venue modules.

New venue = implement VenueConnector (and optionally MarketDataSource) in
its own module, describe it with a VenueProfile, register both here.
Nothing outside connectors/ may import kalshi.py or polymarket.py directly.
"""

from collections.abc import Callable

from alphamarket.config import KALSHI_BASE_URLS, POLYMARKET_BASE_URLS, get_settings
from alphamarket.connectors.alpaca import (
    ALPACA_OAUTH_AUTHORIZE_URL,
    ALPACA_OAUTH_TOKEN_URL,
    ALPACA_TRADING_URLS,
    WRITE_SCOPES,
    AlpacaConnector,
    AlpacaMarketData,
    parse_token_secret,
)
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
    "alpaca": VenueProfile(
        venue="alpaca",
        display_name="Alpaca",
        envs=("paper", "live"),
        credential=CredentialKind.OAUTH2,
        key_label="Alpaca account id",
        secret_label="OAuth grant",
        key_help=(
            "Click Connect Alpaca: you sign in on Alpaca and approve read-only "
            "access. Koine asks for no trading, account-write or data permission, "
            "never sees your password, and cannot place orders."
        ),
        balance_observable=True,
        instrument=InstrumentKind.ASSET,
        # Binary-era fields, read here as: the finest US equity price
        # increment ($0.0001 below $1; $0.01 at or above) ...
        tick_micros=100,
        # ... the finest quantity the 1e6 contract scale holds (Alpaca goes to
        # 1e-9 share; asset paper copies size by instrument.qty_step_atoms) ...
        contract_step_micros=1,
        # ... commission-free: the paper fee is the US_EQUITY_REGULATORY model
        # (SEC fee + FINRA TAF on sells), not a rate ...
        fee_rate_bps=0,
        # ... and Alpaca books those fees in whole cents (FEE activities).
        fee_rounding_micros=10_000,
        ownership=OwnershipProof.KEY,  # a grant approved inside the account proves it
        readonly_proof=ReadOnlyProof.ORDER_PROBE,
        transfers_observable=True,  # CSD/CSW/JNL/ACAT activities
        history_depth="all",  # activities back to account opening [VERIFY]
        quote_asset="USD",
        tos_doc="docs/venues/alpaca.md",
        # Alpaca Connect terms: a commercial app must disclose it and receive
        # written approval (docs/venues/alpaca.md).
        requires_partner_approval=True,
        daily_resolution=False,
        rate_budgets=(
            # Trading API: 200 requests/min per account (Alpaca support "usage
            # limit", checked 2026-09-26). Market data does not use the user's
            # token, so those endpoints cost nothing in this bucket.
            RateBudget(
                scope=RateScope.CREDENTIAL,
                capacity=200,
                refill_per_minute=200,
                costs=(("Bars", 0), ("LatestQuote", 0), ("CorporateActions", 0), ("Assets", 0)),
            ),
            # Koine's own market-data key: Basic plan 200/min, shared by every
            # account and follower [VERIFY plan]. RateScope.IP is the closest
            # shared scope the contract has. Calls made with users' tokens cost
            # nothing in this bucket.
            RateBudget(
                scope=RateScope.IP,
                capacity=200,
                refill_per_minute=200,
                costs=(("Account", 0), ("Positions", 0), ("Activities", 0), ("OrderProbe", 0)),
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


def _build_alpaca(venue_env: str, key_id: str, secret: str | None) -> AlpacaConnector:
    if not secret:
        raise ValueError("Alpaca needs an OAuth access token")
    token, scopes = parse_token_secret(secret)
    return AlpacaConnector(
        env=venue_env,
        access_token=token,
        account_id=key_id,
        granted_scopes=scopes,
        base_url=ALPACA_TRADING_URLS[venue_env],
    )


_BUILDERS: dict[str, Callable[[str, str, str | None], VenueConnector]] = {
    "kalshi": _build_kalshi,
    "polymarket": _build_polymarket,
    "alpaca": _build_alpaca,
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
    # Same prices for both envs; Koine's own data key, never a user's token.
    "alpaca": lambda venue_env: AlpacaMarketData(
        key_id=get_settings().alpaca_data_key_id,
        secret_key=get_settings().alpaca_data_secret_key,
    ),
}


def build_market_data(venue: str, venue_env: str) -> MarketDataSource:
    """Public (unauthenticated) market data for a venue environment."""
    try:
        builder = _MARKET_DATA_BUILDERS[venue]
    except KeyError:
        raise ValueError(f"Unsupported venue: {venue!r}") from None
    return builder(venue_env)


# ---- venue-alpaca: OAuth providers (oauth.py) ----


def get_oauth_provider(venue: str):
    """The OAuth provider for a venue that connects by OAuth, else None.
    Built from settings on each call so client credentials can rotate."""
    from alphamarket.oauth import OAuthProvider

    if venue != "alpaca":
        return None
    settings = get_settings()
    return OAuthProvider(
        venue="alpaca",
        authorize_url=ALPACA_OAUTH_AUTHORIZE_URL,
        token_url=ALPACA_OAUTH_TOKEN_URL,
        revoke_url=None,  # Alpaca documents no revoke endpoint [VERIFY]
        client_id=settings.alpaca_client_id,
        client_secret=settings.alpaca_client_secret,
        scopes=(),  # none: Alpaca's read-only default
        forbidden_scopes=WRITE_SCOPES,
        # One environment per grant, so one token reads exactly one account.
        extra_authorize_params=(("env", settings.alpaca_env),),
        pkce=True,  # not in Alpaca's docs; RFC 7636 servers ignore it [VERIFY]
        revoke_help="Alpaca dashboard -> Alpaca Connect -> Authorized apps -> Revoke",
    )


def resolve_oauth_account_id(venue: str, venue_env: str, secret: str) -> str:
    """Which venue account an OAuth token reads (becomes its key_id)."""
    if venue != "alpaca":
        raise ValueError(f"{venue} does not connect by OAuth")
    return _build_alpaca(venue_env, "", secret).fetch_account_id()

"""Venue registry/factory: the only place core code touches venue modules.

New venue = implement VenueConnector (and optionally MarketDataSource) in
its own module, describe it with a VenueProfile, register both here.
Nothing outside connectors/ may import kalshi.py or polymarket.py directly.
"""

from collections.abc import Callable

from alphamarket.config import KALSHI_BASE_URLS, POLYMARKET_BASE_URLS, get_settings
from alphamarket.connectors import hyperliquid
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
from alphamarket.connectors.binanceus import BASE_URL as BINANCEUS_BASE_URL
from alphamarket.connectors.binanceus import BinanceUSConnector, BinanceUSMarketData
from alphamarket.connectors.coinbase import CoinbaseConnector, CoinbaseMarketData
from alphamarket.connectors.coinbase import load_private_key as load_coinbase_key
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
    # ---- venue-coinbase ----
    "coinbase": VenueProfile(
        venue="coinbase",
        display_name="Coinbase",
        envs=("prod",),  # no sandbox for Advanced Trade
        credential=CredentialKind.KEY_SECRET,
        key_label="API key name",
        secret_label="Private key (base64 Ed25519, or legacy PEM)",
        key_help=(
            "In Coinbase Developer Platform (CDP): create an API key scoped to "
            "View only (no Trade, no Transfer). Key name looks like "
            "'organizations/…/apiKeys/…'; paste the private key exactly as shown "
            "(base64 for the current Ed25519 key type)."
        ),
        balance_observable=True,
        instrument=InstrumentKind.ASSET,
        # Binary-era fields, unused by an ASSET venue's paper mirror (which reads
        # NormalizedInstrument.qty_step_atoms / min_notional per product instead);
        # kept at a representative USD-pair value so the profile stays well-formed.
        tick_micros=10_000,  # $0.01, the common USD-pair quote_increment
        contract_step_micros=10_000,
        fee_rate_bps=90,  # [VERIFY] US "Basic" tier taker ~0.90%, Sept 2026 schedule
        fee_rounding_micros=1,  # commission reported at the quote asset's own scale (assumed 6dp)
        ownership=OwnershipProof.KEY,
        readonly_proof=ReadOnlyProof.SCOPE_ENDPOINT,
        transfers_observable=True,  # v2 /v2/accounts/:id/transactions, same key [VERIFY auth works]
        history_depth="all",  # [VERIFY] no documented cap on fills, unlike Binance/Hyperliquid
        quote_asset="USD",
        tos_doc="docs/venues/coinbase.md",
        requires_partner_approval=True,  # CDP terms: written approval needed for public display
        daily_resolution=False,
        rate_budgets=(
            # [VERIFY] official rate-limit page moved; "30 req/s" per credential is
            # widely cited (docs/research/2026-09-16-crypto-venues.md); not
            # independently confirmed against a live account.
            RateBudget(scope=RateScope.CREDENTIAL, capacity=30, refill_per_minute=1800),
            # [VERIFY] public market-data endpoints are not documented as
            # separately budgeted; a conservative per-IP guess.
            RateBudget(scope=RateScope.IP, capacity=10, refill_per_minute=600),
        ),
        paper_fee_model=PaperFeeModel.BPS_OF_NOTIONAL,
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


def _build_kraken(venue_env: str, key_id: str, secret: str | None) -> VenueConnector:
    if not secret:
        raise ValueError("Kraken needs the API private key")
    return KrakenConnector(api_key=key_id, api_secret=secret, base_url=KRAKEN_API_URL)


def _build_hyperliquid(venue_env: str, key_id: str, secret: str | None) -> VenueConnector:
    return hyperliquid.HyperliquidConnector(
        address=key_id, info_url=hyperliquid.INFO_URLS[venue_env]
    )


def _build_coinbase(venue_env: str, key_id: str, secret: str | None) -> VenueConnector:
    if not secret:
        raise ValueError("Coinbase needs a private key (base64 Ed25519 or legacy PEM)")
    alg, private_key = load_coinbase_key(secret)
    return CoinbaseConnector(key_name=key_id, private_key=private_key, alg=alg)


def _build_binanceus(venue_env: str, key_id: str, secret: str | None) -> VenueConnector:
    if not secret:
        raise ValueError("Binance.US needs a secret key")
    return BinanceUSConnector(base_url=BINANCEUS_BASE_URL, api_key=key_id, api_secret=secret)


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
    "kraken": _build_kraken,  # venue-kraken
    "hyperliquid": _build_hyperliquid,
    "coinbase": _build_coinbase,  # ---- venue-coinbase ----
    # ---- venue-binanceus ----
    "binanceus": _build_binanceus,
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
    "kraken": lambda venue_env: KrakenMarketData(base_url=KRAKEN_API_URL),  # venue-kraken
    "hyperliquid": lambda venue_env: hyperliquid.HyperliquidMarketData(
        info_url=hyperliquid.INFO_URLS[venue_env]
    ),
    "coinbase": lambda venue_env: CoinbaseMarketData(),  # ---- venue-coinbase ----
    # ---- venue-binanceus ----
    "binanceus": lambda venue_env: BinanceUSMarketData(base_url=BINANCEUS_BASE_URL),
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

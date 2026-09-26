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
from alphamarket.connectors.ibkr import IbkrConnector, IbkrMarketData
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
    # ---- venue-ibkr ----
    "ibkr": VenueProfile(
        venue="ibkr",
        display_name="Interactive Brokers (Flex)",
        envs=("prod",),  # Flex Web Service has no sandbox/demo environment
        credential=CredentialKind.REPORT_TOKEN,
        key_label="Flex Query ID",
        secret_label="Flex Web Service token",
        key_help=(
            "In IBKR Client Portal: Reports -> Flex Queries -> New Activity "
            "Flex Query (include Trades at Execution detail, Cash "
            "Transactions, Corporate Actions, Open Positions, NAV). Then "
            "Reports -> Flex Web Service -> generate a token. Both the query "
            "id and the token are reporting-only: neither can place an "
            "order or move money."
        ),
        balance_observable=True,
        instrument=InstrumentKind.ASSET,
        # Binary-era fields, kept only because the dataclass requires them
        # and the M6 paper-mirror code path still reads them for
        # InstrumentKind.BINARY venues; inert for an ASSET venue like this
        # one, which sizes and prices per-instrument (NormalizedInstrument)
        # and fees via `paper_fee_model` instead.
        tick_micros=10_000,  # one cent: the smallest reportable equity price move
        contract_step_micros=1_000_000,  # one whole share, as a harmless placeholder
        fee_rate_bps=0,  # not used: see paper_fee_model=US_EQUITY_REGULATORY below
        fee_rounding_micros=10_000,  # IBKR commissions are reported to the cent
        ownership=OwnershipProof.KEY,  # holding a valid Flex token+query id proves the account
        readonly_proof=ReadOnlyProof.TOKEN_TYPE,
        transfers_observable=True,  # Flex CashTransactions include deposits/withdrawals
        history_depth="365d",  # Flex `fd`/`td` window is capped at 365 days per request
        quote_asset="USD",
        tos_doc="docs/venues/ibkr.md",
        # Flex's terms on third-party (non-personal) consumption of the XML
        # it serves are not documented anywhere IBKR's own pages were checked
        # (docs/venues/ibkr.md, [VERIFY]); gated like Coinbase until Aiden
        # gets a written answer, not assumed permitted by silence.
        requires_partner_approval=True,
        daily_resolution=True,  # Flex statements are end-of-day only
        rate_budgets=(
            # [VERIFY]: IBKR publishes no formal per-minute request budget for
            # Flex Web Service. Error 1018 ("too many requests from this
            # token") is the only documented signal; this is a conservative
            # placeholder (one request roughly every 6s) until real account
            # usage confirms a looser number.
            RateBudget(
                scope=RateScope.CREDENTIAL,
                capacity=10,
                refill_per_minute=10,
                costs=(("SendRequest", 1), ("GetStatement", 1)),
                default_cost=1,
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


def _build_ibkr(venue_env: str, key_id: str, secret: str | None) -> VenueConnector:
    if not secret:
        raise ValueError("IBKR needs a Flex Web Service token")
    return IbkrConnector(query_id=key_id, token=secret)


_BUILDERS: dict[str, Callable[[str, str, str | None], VenueConnector]] = {
    "kalshi": _build_kalshi,
    "polymarket": _build_polymarket,
    # ---- venue-ibkr ----
    "ibkr": _build_ibkr,
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
    # ---- venue-ibkr ----
    # IBKR has no public market-data API (see connectors/ibkr.py); this is a
    # deliberate stub so core code can still call build_market_data("ibkr", ...).
    "ibkr": lambda venue_env: IbkrMarketData(),
}


def build_market_data(venue: str, venue_env: str) -> MarketDataSource:
    """Public (unauthenticated) market data for a venue environment."""
    try:
        builder = _MARKET_DATA_BUILDERS[venue]
    except KeyError:
        raise ValueError(f"Unsupported venue: {venue!r}") from None
    return builder(venue_env)

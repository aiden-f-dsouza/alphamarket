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
    RegulatoryFees,
    VenueConnector,
    VenueProfile,
)
from alphamarket.connectors.binanceus import BASE_URL as BINANCEUS_BASE_URL
from alphamarket.connectors.binanceus import BinanceUSConnector, BinanceUSMarketData
from alphamarket.connectors.coinbase import CoinbaseConnector, CoinbaseMarketData
from alphamarket.connectors.coinbase import load_private_key as load_coinbase_key
from alphamarket.connectors.ibkr import IbkrConnector, IbkrMarketData
from alphamarket.connectors.kalshi import KalshiConnector, KalshiMarketData, load_private_key_pem
from alphamarket.connectors.kraken import KRAKEN_API_URL, KrakenConnector, KrakenMarketData
from alphamarket.connectors.polymarket import PolymarketConnector, PolymarketMarketData
from alphamarket.connectors.snaptrade import SnapTradeConnector, SnapTradeMarketData

# ---- wave2 scaffold ----
# US equity sell-side regulatory fees for paper followers (PaperFeeModel.
# US_EQUITY_REGULATORY). [VERIFY] every value before launch: the SEC Section
# 31 rate was set to $0.00 per million from 2025-05-14 and the FY2026 rate
# is not confirmed here; FINRA TAF $0.000166/share capped at $8.30 are the
# 2024 schedule values. Rounding (methodology): each fee rounds UP to the cent.
US_EQUITY_REGULATORY_FEES = RegulatoryFees(
    sec_fee_per_million="0.00",
    taf_per_share="0.000166",
    taf_max="8.30",
    effective="2026-10-01 [VERIFY]",
)

# Daily-resolution venues see fills a day late: copying them stays allowed
# (with the lag shown) for three days, then they are stale.
DAILY_MAX_COPY_LAG_SECONDS = 3 * 24 * 3600

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
        asset_classes=("prediction",),  # wave2
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
        asset_classes=("prediction",),  # wave2
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
        asset_classes=("crypto",),  # wave2
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
        # SIGNED_MESSAGE routes the account through the claim flow, where
        # verify_claim checks an EIP-191 signature over claim_text(code).
        ownership=OwnershipProof.SIGNED_MESSAGE,  # wave2: EIP-191 claim message
        readonly_proof=ReadOnlyProof.NONE,
        transfers_observable=True,
        history_depth="10k_fills",
        quote_asset="USDC",
        tos_doc="docs/venues/hyperliquid.md",
        requires_partner_approval=False,
        daily_resolution=False,
        rate_budgets=(hyperliquid.RATE_BUDGET,),
        paper_fee_model=PaperFeeModel.BPS_OF_NOTIONAL,
        asset_classes=("crypto",),  # wave2
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
        asset_classes=("crypto",),  # wave2
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
        asset_classes=("crypto",),  # wave2
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
        regulatory_fees=US_EQUITY_REGULATORY_FEES,  # wave2
        asset_classes=("equities",),
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
        regulatory_fees=US_EQUITY_REGULATORY_FEES,  # wave2
        asset_classes=("equities",),
        pricing_venue="alpaca",  # no public market data of its own
        max_copy_lag_seconds=DAILY_MAX_COPY_LAG_SECONDS,
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
                # across every connected user, not per credential -- RateScope.IP
                # is the one shared bucket "across every account we poll"
                # (base.py), which is what this limit actually is even though the
                # reason is a commercial plan cap rather than network IP.
                # RateScope.CREDENTIAL would give every connected user their own
                # 250/min bucket and blow through SnapTrade's real limit by up to
                # Nx once N users are connected. Per-account 10 req/min limits
                # are documented as personal-tier only, per
                # https://docs.snaptrade.com/docs/ratelimiting , checked
                # 2026-09-26. [VERIFY] against the live commercial dashboard once
                # an account exists.
                scope=RateScope.IP,
                capacity=250,
                refill_per_minute=250,
            ),
        ),
        paper_fee_model=PaperFeeModel.US_EQUITY_REGULATORY,
        regulatory_fees=US_EQUITY_REGULATORY_FEES,  # wave2
        asset_classes=("equities",),
        pricing_venue="alpaca",  # no public market data of its own
        max_copy_lag_seconds=DAILY_MAX_COPY_LAG_SECONDS,
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


def _build_ibkr(venue_env: str, key_id: str, secret: str | None) -> VenueConnector:
    if not secret:
        raise ValueError("IBKR needs a Flex Web Service token")
    return IbkrConnector(query_id=key_id, token=secret)


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
    "kraken": _build_kraken,  # venue-kraken
    "hyperliquid": _build_hyperliquid,
    "coinbase": _build_coinbase,  # ---- venue-coinbase ----
    # ---- venue-binanceus ----
    "binanceus": _build_binanceus,
    "alpaca": _build_alpaca,
    # ---- venue-ibkr ----
    "ibkr": _build_ibkr,
    "snaptrade": _build_snaptrade,  # venue-snaptrade
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


# ---- wave2 scaffold ----
# Outbound budgets for market data sources that are not venues (no profile,
# never connectable), e.g. the CoinGecko fallback price source. The limiter
# falls back to this table (venue_limits._profile_budgets).
DATA_SOURCE_BUDGETS: dict[str, tuple[RateBudget, ...]] = {
    # CoinGecko demo plan: 30 calls/min [VERIFY against the account's plan].
    "coingecko": (RateBudget(scope=RateScope.IP, capacity=30, refill_per_minute=30),),
}

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
    # ---- venue-ibkr ----
    # IBKR has no public market-data API (see connectors/ibkr.py); this is a
    # deliberate stub so core code can still call build_market_data("ibkr", ...).
    "ibkr": lambda venue_env: IbkrMarketData(),
    "snaptrade": lambda venue_env: SnapTradeMarketData(),  # ---- venue-snaptrade ----
}


def build_market_data(venue: str, venue_env: str) -> MarketDataSource:
    """Public (unauthenticated) market data for a venue environment."""
    try:
        builder = _MARKET_DATA_BUILDERS[venue]
    except KeyError:
        raise ValueError(f"Unsupported venue: {venue!r}") from None
    return builder(venue_env)


# ---- venue-alpaca: OAuth providers (oauth.py) ----

# Authorize endpoints the connect form may redirect to (CSP form-action,
# security_headers.py). One entry per OAuth venue.
OAUTH_AUTHORIZE_URLS: tuple[str, ...] = (ALPACA_OAUTH_AUTHORIZE_URL,)


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


# ---- wave2:market-data ----
# CoinGecko fallback prices (connectors/coingecko.py, plan D13). A market data
# source only: it has no profile, so it never appears in PROFILES and can never
# be connected (build_connector raises for it).
#
# Curated asset code -> CoinGecko coin id. No search: an asset missing here
# stays unpriced rather than being matched by ticker (tickers collide). Asset
# codes are the canonical ledger codes (Kraken XBT -> BTC, XDG -> DOGE).
# [VERIFY] every id against https://api.coingecko.com/api/v3/coins/list before
# launch; ids occasionally change when a coin migrates. Stablecoins are priced
# at par (MarketDataSource.STABLECOINS_AT_PAR) and are deliberately absent.
COINGECKO_COIN_IDS: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "ripple",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "LTC": "litecoin",
    "BCH": "bitcoin-cash",
    "DOT": "polkadot",
    "LINK": "chainlink",
    "AVAX": "avalanche-2",
    "ATOM": "cosmos",
    "XLM": "stellar",
    "UNI": "uniswap",
    "AAVE": "aave",
    "ETC": "ethereum-classic",
    "FIL": "filecoin",
    "ALGO": "algorand",
    "NEAR": "near",
    "APT": "aptos",
    "SUI": "sui",
    "TRX": "tron",
    "SHIB": "shiba-inu",
    "PEPE": "pepe",
    "ARB": "arbitrum",
    "OP": "optimism",
    "HYPE": "hyperliquid",
    "POL": "polygon-ecosystem-token",
    "MATIC": "matic-network",
}


def _build_coingecko(venue_env: str) -> MarketDataSource:
    from alphamarket.connectors.coingecko import CoinGeckoMarketData

    settings = get_settings()
    return CoinGeckoMarketData(
        coin_ids=COINGECKO_COIN_IDS,
        api_key=settings.coingecko_api_key,
        base_url=settings.coingecko_base_url,
    )


_MARKET_DATA_BUILDERS["coingecko"] = _build_coingecko

# ---- wave2:p8-connect ----
# Aggregator account lifecycle (CredentialKind.AGGREGATOR, D20): registering
# one aggregator user per connection, the Connection Portal link, the account
# picker and deleting the user on disconnect. Core code calls these wrappers,
# never connectors/snaptrade.py. Secrets (the user secret) are passed in and
# returned, never logged.

from dataclasses import dataclass  # noqa: E402 (shared file: wave2 end block)


@dataclass(frozen=True)
class AggregatorAccount:
    """One brokerage account behind an aggregator user, as the picker shows
    it. `number_last4` only: the full account number is never rendered."""

    account_ref: str
    institution: str
    name: str | None
    number_last4: str | None
    is_paper: bool


def _snaptrade_keys() -> tuple[str, str]:
    settings = get_settings()
    if not settings.snaptrade_client_id or not settings.snaptrade_consumer_key:
        raise ValueError("SnapTrade needs snaptrade_client_id/snaptrade_consumer_key configured")
    return settings.snaptrade_client_id, settings.snaptrade_consumer_key


def _require_aggregator(venue: str) -> None:
    if get_profile(venue).credential is not CredentialKind.AGGREGATOR or venue != "snaptrade":
        raise ValueError(f"{venue} is not an aggregator venue")


def aggregator_register_user(venue: str) -> tuple[str, str]:
    """Register a fresh aggregator user for one connection: (user_id, user_secret).
    The id is random (no email or Koine user id leaks to the aggregator)."""
    import secrets as _secrets

    from alphamarket.connectors import snaptrade

    _require_aggregator(venue)
    client_id, consumer_key = _snaptrade_keys()
    for _ in range(3):
        user_id = f"koine-{_secrets.token_hex(16)}"
        try:
            return snaptrade.register_user(client_id, consumer_key, user_id)
        except snaptrade.SnapTradeUserExists:
            continue  # astronomically unlikely; draw another id
    raise RuntimeError("could not register an aggregator user")


def aggregator_portal_url(
    venue: str, user_id: str, user_secret: str, *, redirect: str | None = None
) -> str:
    """The aggregator's Connection Portal link, read-only connection type."""
    from alphamarket.connectors import snaptrade

    _require_aggregator(venue)
    client_id, consumer_key = _snaptrade_keys()
    return snaptrade.generate_connection_portal_url(
        client_id, consumer_key, user_id, user_secret, custom_redirect=redirect
    )


def aggregator_list_accounts(venue: str, user_id: str, user_secret: str) -> list[AggregatorAccount]:
    """Every brokerage account the aggregator user has linked."""
    _require_aggregator(venue)
    connector = build_connector(venue, get_settings().venue_env(venue), user_id, user_secret)
    rows = connector.list_accounts()  # type: ignore[attr-defined]
    out = []
    for r in rows:
        number = str(r.get("number") or "")
        out.append(
            AggregatorAccount(
                account_ref=str(r["account_ref"]),
                institution=str(r.get("institution_name") or ""),
                name=r.get("name"),
                number_last4=number[-4:] if number else None,
                is_paper=bool(r.get("is_paper")),
            )
        )
    return out


def aggregator_delete_user(venue: str, user_id: str) -> None:
    """Delete the aggregator user (stops billing; the aggregator drops its
    brokerage connections). Raises on failure; callers decide to tolerate."""
    from alphamarket.connectors import snaptrade

    _require_aggregator(venue)
    client_id, consumer_key = _snaptrade_keys()
    snaptrade.delete_user(client_id, consumer_key, user_id)

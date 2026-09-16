# Read-only US equities ingestion: broker and aggregator survey (2026-09-16)

*Research output feeding docs/platform_plan.md. Not legal advice. Several official pages (developer.schwab.com, IBKR Web API doc, public.com history endpoint, Tradier history reference) returned 403/404; those rows lean on search snippets, wrapper-library docs, or the Public T&C PDF, and are marked [VERIFY].*

## 1. Per-broker table

| Broker | Official third-party path | Auth | Read-only proof | Trades endpoint + id | Cash/non-trade activity | Freshness | ToS / approval notes | Cost |
|---|---|---|---|---|---|---|---|---|
| **Alpaca** | **Yes** (OAuth apps / "Alpaca Connect") | OAuth2 auth-code; register in Dashboard > OAuth Apps, e-sign agreement | Scopes are only `account:write`, `trading`, `data`; docs: "read-only endpoint access is assumed by default". Request **no scopes** and the token is read-only by construction; introspection endpoint not documented [VERIFY]. Practical proof: `POST /v2/orders` on paper must 403 [VERIFY] | `GET /v2/account/activities?activity_types=FILL`: `id` (stable, `::`-delimited, used as page_token), `order_id`, `symbol`, `qty`, `price`, `side`, `transaction_time`, `type` fill/partial_fill | Same endpoint: `TRANS` (CSD/CSR), `DIV` family, `ACATC/ACATS`, `FEE`, `INT`, `JNL`, `MA`, `OPASN`, `SSP` (split), with `net_amount`, `date` | Near real-time | Any developer may register; live-trading apps need Compliance review; commercial apps must disclose and get written approval; must not imply Alpaca built the app | Free |
| **Alpaca Broker API** | Not for reading other people's accounts | Partner API key | n/a | n/a | n/a | n/a | Embedded brokerage (KYC, funding, custody). Only if Koine wanted to *be* the brokerage | Bespoke |
| **Webull (US)** | **Yes, two paths**: (a) individual OpenAPI keys (own use only), (b) **Connect API** for third-party apps | (a) App Key + Secret, HMAC-SHA1; (b) OAuth 2.0 via `passport.webull.com` | **No read-only scope documented** in either path [VERIFY] | Connect API: account list, balances, positions, order query; gRPC trade-event stream; paginated order history, fill-level ids not confirmed [VERIFY] | Transfer/fund-record endpoint not confirmed [VERIFY] | Real-time stream; 90 req/min per user | Individual: US account, min $100, "do not expose App Key/Secret to any third party" (path (a) unusable by Koine). Connect API: company applies via form, business team approves | Free |
| **DriveWealth** | **No** for a third party. B2B only: partner = the app that white-labels DriveWealth (Revolut, Cash App). No end-user OAuth | Partner keys after signed agreement | n/a | n/a | n/a | n/a | Multi-phase onboarding with solutions engineers, compliance review, UAT | Bespoke |
| **Schwab Trader API** | **Individual role: own accounts only.** Commercial approval is a separate review; reported slow/stalled for retail automation platforms in 2025-26 | Three-legged OAuth2, access token 30 min, refresh 7 days (weekly re-consent) | Token `scope` is just `"api"`; **no read-only scope** [VERIFY] | `GET /trader/v1/accounts/{hash}/transactions?startDate&endDate&types=`; `activityId`, `transferItems[]`; types TRADE, RECEIVE_AND_DELIVER, DIVIDEND_OR_INTEREST, ACH_*, CASH_*, ELECTRONIC_FUND, WIRE_*, JOURNAL | Same endpoint | Intraday; **start date within 60 days** so history must be polled continuously | Commercial review evaluates "self-directed, user controls orders" | Free |
| **Interactive Brokers** | **Yes but heavy**: third-party vendors onboard via `webapionboarding@interactivebrokers.com`; only **OAuth 1.0a** for third parties; timeline 2-3 wk screening + 3-6 wk Compliance EDD + 3-5 wk Legal | OAuth 1.0a (RSA-signed); **Flex Web Service** = user-generated read-only reporting token | **Flex token is the cleanest read-only proof in the industry**: reporting-only, user-set expiry, optional IP restriction, cannot place orders | Web API: `/iserver/account/trades`, `/pa/transactions`, `/portfolio/{id}/ledger`, positions. Flex Activity statement: Trades with `tradeID`/`ibExecID`, Cash Transactions, Transfers, Corporate Actions, Open Positions, NAV; up to 365-day periods; XML/CSV | Flex Cash Transactions | Flex Activity = end-of-day; Trade Confirms Flex = near real-time | Flex is "personal" reporting; used by trade journals in practice but not an explicitly licensed third-party channel [VERIFY ToS] | Free |
| **Tradier** | **Partner only**: non-partners are personal-use; partners get OAuth creds | OAuth2 (partners) | No read-only scope documented [VERIFY] | `GET /v1/accounts/{id}/history` with `type` filter; per-event ids not confirmed [VERIFY] | Same | Intraday | Partner via sales | Free |
| **TradeStation** | Business partner program; individuals get keys for own account | OAuth2 (v3) | `ReadAccount`-style scopes exist [VERIFY] | v3 `/brokerage/accounts/{id}/historicalorders` [VERIFY] | Not confirmed [VERIFY] | Intraday | Bespoke | Free |
| **E*TRADE** | Live: OAuth 1.0a consumer keys; keys issued to account holders; commercial redistribution unclear [VERIFY] | OAuth 1.0a | None [VERIFY] | v1 `/accounts/{id}/transactions` [VERIFY] | Same | Intraday | Personal-use flavoured [VERIFY] | Free |
| **Public.com** | **No.** Individual Trader API T&C (June 2026): "solely for their own personal use", may not "provide any third party with the right to access", bans "systematic copying or duplicating orders of other traders ... automatic mirroring" | Personal token | n/a | n/a | n/a | | Hard ToS block; Public appears on SnapTrade instead | |
| **Robinhood** | **No** equities API; official API is crypto-only. ToS bans data-gathering tools. Reachable only via Plaid or SnapTrade | | | | | | | |
| **Fidelity** | **No** direct API. "Fidelity Access" OAuth via Akoya; screen scraping blocked since Oct 2023. SnapTrade and Plaid reach it via Fidelity Access | Akoya/FDX OAuth | Read-only by nature | Via aggregator | Via aggregator | Daily-ish | Growth/Custom Plaid customers get Fidelity automatically; PAYG must request | Via aggregator |
| **Moomoo / Futu** | **No** third-party path: user must run the OpenD gateway logged into their own account | OpenD | | | | | Personal use | |

## 2. Aggregator table

| Aggregator | Coverage (US equities) | Freshness | Granularity | Pricing | Startup onboarding |
|---|---|---|---|---|---|
| **SnapTrade** | "30-35+" brokerages incl. Fidelity, Schwab, Robinhood, Webull US, Public, E*TRADE, IBKR, Alpaca, Vanguard; per-broker pages state read/trade support (Robinhood read-only; Fidelity read-only; Schwab via "Scoped OAuth") | Real-time plan: live holdings/orders where broker supports; activities always "cached and refreshed once a day, no intraday transactions"; holdings <=4 pulls/day/user, transactions <=1/account/24 h | `GET /accounts/{id}/activities`: BUY, SELL, DIVIDEND, CONTRIBUTION, WITHDRAWAL, REI, STOCK_DIVIDEND, INTEREST, FEE, TAX, OPTIONEXPIRATION/ASSIGNMENT/EXERCISE, TRANSFER, EXTERNAL_ASSET_TRANSFER_IN/OUT, SPLIT, ADJUSTMENT; has `id` **but docs warn it can change if the broker reprocesses** (dedupe on content hash) | Free: 5 connections; PAYG real-time $2/user/mo; daily $1/user/mo | Self-serve; **connection_type defaults to read-only**; pre-production review checklist. Robinhood via OAuth though "Robinhood does not offer a developer API" (durability [VERIFY]) |
| **Plaid Investments** | 3,200+ institutions incl. Fidelity, Schwab, Vanguard, Robinhood (OAuth), E*TRADE, Coinbase | "at least once per day on market days (2-4x/day)"; `/investments/refresh` on-demand add-on | `/investments/transactions/get`: `investment_transaction_id` (unique), `type`/`subtype`, `quantity`, `price`, `fees`, `amount`; up to **24 months** history; holdings with `cost_basis` | Subscription per Item/month; specifics behind sales [VERIFY] | Sandbox self-serve; production requires application; Fidelity auto-enabled only for Growth/Custom |
| **Akoya** | Fidelity, Schwab and big banks via FDX | Pass-through | FDX investment: balances, holdings, transactions | Standard / Enterprise; sandbox free [VERIFY rates] | Free sandbox; production = security screening + agreement |
| **Mastercard Open Banking (Finicity)** | Investment/brokerage accounts ("Wealth API") | Daily [VERIFY] | Holdings + transactions | Enterprise | Enterprise |
| **MX** / **Yodlee** | Holdings-centric / 17,000+ FIs | Daily [VERIFY] | | Enterprise | Enterprise |
| **Vezgo** | Crypto only | | | | |
| **Mesh Connect** | Robinhood, Webull, eToro + crypto; trading-oriented | | | Not published [VERIFY] | [VERIFY] |

**CFPB Section 1033 status (verified):** Final rule Oct 2024. Scope is Reg E asset accounts, Reg Z credit cards, payment facilitation; "brokerage accounts and retirement accounts are not included in this first phase". *Forcht Bank v. CFPB*: rule enjoined pending reconsideration; ANPR Aug 2025; NPRM sent to OIRA Aug 2026. Net: **no statutory data-access right helps Koine for brokerage data in 2026**; everything is bilateral (broker API terms or aggregator contracts).

## 3. Modelling: how equities differ from binary contracts

- **Mark-to-market**: every open position needs a daily close to produce an equity curve. Add `prices_daily(symbol, date, close, adj_close)` and compute NAV = cash + sum(qty x close) nightly, then reconcile against the broker's reported equity (Alpaca `/v2/account.equity`, Schwab `liquidationValue`, IBKR NAV).
- **Corporate actions**: splits change qty without a fill (Alpaca `SSP`; Schwab `RECEIVE_AND_DELIVER`; SnapTrade `SPLIT`); cash dividends are non-trade cash; stock dividends/spin-offs create positions with no fill; **symbol changes/mergers** break symbol-keyed joins, so key positions by an internal instrument id with a symbol-history table.
- **Short sales**: negative qty, borrow fees/interest as `INT`/`FEE`; P&L sign flips.
- **Options**: OCC symbols, multiplier 100, multi-leg orders as several fills sharing an order id, expirations/assignments/exercises remove or create positions without a normal fill. Model each leg as its own instrument with an `expiry`; nightly job expires worthless legs.
- **Fractional shares**: qty fractional; prices can be sub-penny.
- **T+1 settlement**: use trade date for P&L, settlement date only for cash-available reconciliation.
- **Cash vs margin**: cash can be negative; track cash and equity separately; margin interest as `INT`.
- **Fees**: SEC/FINRA TAF as separate rows or embedded fee lines; keep fills gross.
- **Deposits/withdrawals** dominate account value; time-weighted return must be flow-adjusted.

**Reference price data**: Alpaca Market Data Basic (free with an Alpaca account: historical bars since 2016, 200 req/min) plus Alpaca's **Corporate Actions API** (splits, dividends, mergers, spin-offs, symbol changes) is enough for daily marks; Tiingo free Starter (30+ yrs adjusted EOD; commercial tier needed [VERIFY price]); Polygon/Massive free 5 req/min EOD, Starter $29/mo; Databento overkill. Avoid Yahoo unofficial. Options EOD marks are the expensive gap.

## 4. Recommended order for a first equities milestone

1. **Alpaca via OAuth (no scopes = read-only)**. Only broker with self-serve, sanctioned third-party OAuth, stable-id activities covering fills and every non-trade cash type, real-time freshness, and free market/corporate-action data. Reconciled tier.
2. **IBKR via user-supplied Flex Web Service token** as a fills-only, EOD tier. No compliance onboarding for Flex; the OAuth 1.0a program can follow with traction. ToS check on redistribution [VERIFY].
3. **SnapTrade** as the long-tail read-only layer (Robinhood, Fidelity, Schwab, Webull, Public, E*TRADE, Vanguard). $1-2/user/month, defaults to read-only, free 5 connections. Daily-only, ids not guaranteed stable, no read-only proof beyond SnapTrade's own flag: define a "daily-verified" tier.
4. **Webull Connect API** application in parallel (asynchronous approval).
5. **Schwab commercial approval**: file, do not plan around it.
6. Do not pursue Public (ToS forbids third-party access and mirroring), Robinhood direct, DriveWealth, moomoo.

Contract implications: `credential_kind` values `oauth2` (Alpaca, Webull), `report_token` (IBKR Flex), `aggregator` (SnapTrade); balance observability reconciled (Alpaca, IBKR NAV) vs daily (SnapTrade); a `readonly_proof` enum {scope_list, order_probe_403, token_type, aggregator_flag}; an instrument model flag so stats mark-to-market from `prices_daily` instead of settlement.

## 5. Not verified [VERIFY]

- Alpaca: OAuth token introspection; whether a zero-scope token is rejected by `POST /v2/orders`; whether OAuth app registration is still auto-approved for small startups in 2026.
- Webull: read-only scope in Connect API; fill-level history fields; transfer history; token lifetimes; partner size requirements.
- Schwab: exact transactions endpoint (taken from schwab-py); 60-day window; individual role page (403).
- IBKR: Flex Web Service terms for third-party consumption; OAuth 2.0 availability to non-institutional vendors.
- Public `Get history` fields; Robinhood May-2026 AI-agent accounts scope.
- Tradier ids; TradeStation scopes; E*TRADE commercial redistribution terms.
- SnapTrade: whether Robinhood/Public connections are broker-sanctioned; per-broker history depth.
- Plaid Investments list price; Akoya standard-tier price; Finicity/MX/Yodlee pricing.
- Tiingo commercial-tier price; Polygon/Massive tier names.

## Sources

- Alpaca OAuth: https://docs.alpaca.markets/us/docs/using-oauth2-and-trading-api ; https://alpaca.markets/support/oauth-applications ; https://docs.alpaca.markets/us/docs/about-connect-api ; activities https://docs.alpaca.markets/docs/account-activities ; Broker API https://alpaca.markets/broker ; market data https://docs.alpaca.markets/us/docs/about-market-data-api ; corporate actions https://docs.alpaca.markets/reference/corporateactions-1
- Webull: https://developer.webull.com/apis/docs/getting-started/ ; https://developer.webull.com/apis/docs/connect-api/about-connect-api/ ; https://developer.webull.com/apis/docs/authentication/apply/
- DriveWealth: https://www.drivewealth.com/technology/partner-services/ ; https://developer.drivewealth.com/apis/docs/intro
- Schwab: https://developer.schwab.com/products/trader-api--individual ; https://schwab-py.readthedocs.io/en/latest/client.html ; https://blog.traderspost.io/article/will-traderspost-reconnect-with-charles-schwab
- IBKR: https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-doc/ ; https://www.ibkrguides.com/brokerportal/performanceandstatements/flex3.htm ; https://www.interactivebrokers.com/campus/ibkr-api-page/flex-web-service/
- Tradier https://docs.tradier.com/docs/faq ; TradeStation https://api.tradestation.com/docs/faq/ ; E*TRADE https://developer.etrade.com/getting-started
- Public: https://public.com/disclosures/individual-api-program
- Robinhood: https://robinhood.com/us/en/newsroom/robinhood-crypto-trading-api/
- Fidelity/Akoya: https://newsroom.fidelity.com/pressreleases/fidelity-takes-steps-to-address-screen-scraping/s/2f33bc18-f16d-4b66-9868-626ada9ba32b ; https://akoya.com/fintechs
- Moomoo: https://openapi.moomoo.com/moomoo-api-doc/en/trade/overview.html
- SnapTrade: https://snaptrade.com/brokerage-api ; https://docs.snaptrade.com/docs/launch-guide.md ; https://docs.snaptrade.com/reference/Account%20Information/AccountInformation_getAccountActivities ; https://snaptrade.com/brokerage-integrations/robinhood-api
- Plaid: https://plaid.com/docs/investments/ ; https://plaid.com/docs/api/products/investments/
- Section 1033: https://www.federalregister.gov/documents/2024/11/18/2024-25079/required-rulemaking-on-personal-financial-data-rights ; https://www.consumerfinancemonitor.com/2026/08/06/cfpb-sends-new-section-1033-open-banking-proposal-to-oira-for-review/
- Market data pricing: https://www.tiingo.com/about/pricing ; https://databento.com/stocks

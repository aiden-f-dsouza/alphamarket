# Koine platform plan: multi-venue backend, payments, security (2026-09-16)

Written for: Aiden and Kyle, as the working plan for the next build phase.
Companion to BUILD_PLAN.md (M0-M7, what exists) and regulatory_reference.md
(what is legal). Research behind every venue and vendor claim is in
`docs/research/2026-09-16-*.md`; anything marked **[VERIFY]** there is not
settled and must not be built on without checking.

The plan answers four questions:

1. What is the state of the backend today and what has to change before a
   third asset class can be added safely?
2. How do crypto exchanges (Coinbase, Kraken, Binance.US, Hyperliquid) and
   equities brokers (Alpaca, IBKR, Webull, aggregators) fit the connector
   contract, and in what order?
3. How does Koine take money (subscriptions, creator payouts) without ever
   becoming a money transmitter, custodian, or adviser?
4. What security and compliance posture does holding hundreds of read-only
   exchange keys require, and how is it phased?

---

## 0. Decisions this plan makes (the short version)

| # | Decision | Why |
|---|---|---|
| D1 | **Commit the M6/M7 working tree before anything else.** | 35 modified + 23 untracked files, including two migrations, are unversioned. |
| D2 | **Ledger v2 is a multi-asset, multi-leg ledger.** Fills, settlements, transfers, dividends, fees and funding all become typed legs in one append-only `ledger_entries` table; balances are vectors per asset. | Crypto and equities cannot be expressed as "USD cash + binary contracts". This also closes the open M2 deposits/withdrawals item. |
| D3 | **Quantities get per-asset precision; money stays micro-dollars.** Invariant 1 is amended, not broken: USD values remain integer micros; asset quantities are integer "atoms" with a per-instrument `decimals` (Kalshi contracts 6, BTC 8, shares 6). | 1e-6 BTC rounding would manufacture the divergence reconciliation exists to detect. |
| D4 | **Every venue declares a read-only proof method** in its profile: `scope_endpoint` (Kalshi, Coinbase, Binance, Bybit), `order_probe` (Kraken, Alpaca), `token_type` (IBKR Flex), `aggregator_flag` (SnapTrade), `none` (public address). The methodology page shows which one applies. | "Verified" must mean something specific per venue. |
| D5 | **Credentials move to envelope encryption**: Fernet stays as the record cipher, data keys are wrapped by AWS KMS, `MultiFernet` handles rotation, and OAuth refresh tokens and multi-part secrets get a real schema. | One static `VAULT_KEY` does not survive a partner security questionnaire or a key rotation. |
| D6 | **Worker becomes a Postgres-backed job queue** with per-venue rate budgets and resumable backfill cursors. No Redis, no Celery yet. | Binance's 24-hour windows and Kraken's counters make "sweep everything every 5 minutes" impossible. |
| D7 | **Venue order**: crypto = Kraken, then Coinbase (gated on written approval), then Hyperliquid, then Binance.US; equities = Alpaca OAuth, then IBKR Flex, then SnapTrade for the long tail. Webull Connect and Schwab commercial: file applications now, do not plan around them. Public.com, Robinhood direct, DriveWealth, moomoo: not pursued. | Terms of service and read-only provability, see research docs. |
| D8 | **Payments = Stripe Billing + Stripe Tax + Stripe Connect (Express shape).** Money in is always for Koine's own service; money out to creators is a Koine expense via Connect transfers. No wallets, no user-to-user money, no crypto payments, no fee tied to trades or P&L. | Keeps Koine outside money transmission and outside transaction-based compensation. |
| D9 | **Security gates per milestone**: TOTP before any key connect, DB-backed sessions, rate limits, append-only audit log, static egress IP, gitleaks + pip-audit in CI, tested encrypted backups. SOC 2 evidence collection starts now; the audit waits for a partner to ask. | Cheapest order: controls first, paperwork when demanded. |
| D10 | **Two new invariants**: (9) Koine never accepts a venue username or password, only keys, OAuth grants, or report tokens; (10) a venue connector is enabled in the registry only with a recorded terms-of-service position (`docs/venues/<venue>.md`) and a consent screen. | Schwab/Fidelity credential crackdowns and the Kalshi Developer Agreement lesson. |

Nothing here changes invariant 8 (tailing is paper only). The `Executor`
seam stays exactly where it is.

---

## 1. Audit of the current backend (2026-09-16)

150 tests pass, ruff is clean, ~6,200 lines across 25 modules. What works is
solid: append-only ledger with ORM guard plus Postgres triggers, micro-dollar
money, anchored reconciliation, pure stats functions with hand-computed tests,
a registry that keeps venue modules out of core code, and a paper executor
that structurally refuses live mode.

What blocks the next phase:

| Area | Finding | Consequence |
|---|---|---|
| Version control | M6 + M7 (signals, mirror, tails, Polymarket, migrations 0007/0008) uncommitted. | Any infra refactor on top of uncommitted work is unreviewable. Fix first (D1). |
| Ledger model | `Fill` = (ticker, side yes/no, count, price, fee) and `Settlement` pays $1/$0. `InstrumentKind.ASSET` and `Side.LONG/SHORT` exist in `connectors/base.py` but nothing in `ledger.py`, `stats.py`, `reconcile.py`, or `mirror.py` reads them. | A crypto spot fill (BTC for USD, fee in BNB) or a stock dividend has no row to land in. |
| Balance | `NormalizedBalance.balance_micros` is one integer. | A crypto account is a vector; reconciliation must compare per asset before valuing in USD. |
| Transfers | No deposits/withdrawals feed in the contract (open since M2). Deposits read as divergence. | Every new venue reports transfers; without them reconciliation quarantines honest accounts. |
| Credentials | `venue_accounts.encrypted_private_key` is one Fernet blob; no rotation path; no place for OAuth refresh tokens, expiries, passphrases, or report tokens. | Alpaca (OAuth), Kraken (key + secret), Coinbase (key + PEM), IBKR (token + query id) all need different shapes. |
| Worker | APScheduler sweeps all accounts serially every 5 min, `max_instances=1`. No per-venue rate budget, no resumable backfill, no job state. | One slow venue stalls all; Binance backfill (per symbol, 24-hour windows) cannot be done inside a sweep. |
| Auth | Email + password, signed cookie session. No 2FA, no rate limiting, no audit trail. | Not acceptable once accounts hold exchange keys, even read-only ones. |
| Marks | `market_prices` is binary-market shaped (yes bid/ask/last/result). | Assets need daily closes per instrument, plus split/dividend adjustments for equities. |
| Preflight | Checks secrets, cookies, DEMO_MODE, five triggers. | Must grow with each new invariant (KMS reachable, audit trigger, TOTP enforced, venue ToS files present). |
| Payments | None. | Intentional so far; now needs a design that cannot drift into money handling. |

---

## 2. Target architecture

### 2.1 Ledger v2: instruments, legs, vectors

New tables (migration 0009; hand-written like the rest):

```
instruments      id, venue, kind (binary | spot_asset | equity | option | perp),
                 symbol (as venue reports it), base_asset, quote_asset,
                 decimals (quantity precision), multiplier (options: 100),
                 expiry, first_seen_at
                 UNIQUE(venue, symbol)
symbol_history   instrument_id, symbol, valid_from, valid_to   (renames/mergers)

ledger_entries   id, venue_account_id, instrument_id (nullable for cash-only),
                 asset (e.g. "USD", "BTC", "KXBTC-24DEC-YES"),
                 delta_atoms (signed), decimals,
                 kind (fill_base | fill_quote | fee | settlement | deposit |
                       withdrawal | dividend | interest | funding | staking |
                       split | adjustment | transfer_in | transfer_out),
                 venue_ref (fill id, ledger id, activity id),
                 group_ref (ties the legs of one fill together),
                 occurred_at, ingested_at
                 UNIQUE(venue_account_id, venue_ref, kind, asset)
                 APPEND-ONLY (trigger + ORM guard, same as fills)
```

`fills` and `settlements` stay as they are (append-only history; the public
page and signals read them) and each one now also produces its legs. For
Kalshi a buy fill becomes three legs: `fill_base` +count of the contract
asset, `fill_quote` -notional USD, `fee` -fee USD. A settlement becomes
`settlement` -count contract and +revenue USD. The existing cash formula in
`ledger.py` is then a special case: cash(t) = anchor + sum of USD legs after
anchor. The current hand-computed tests must still pass unchanged against the
legs path before the old path is deleted (see M8 exit criteria).

Balance vector: `sum(delta_atoms) group by asset` since anchor, plus the
anchor vector (`venue_accounts.anchor_positions` grows into
`anchor_balances`, one row per asset).

Precision (D3): `money.py` gains `to_atoms(value: str | Decimal, decimals)`
and `from_atoms`; conversions only ever parse the venue's decimal string,
never a float. The test for every connector is that
`from_atoms(to_atoms(s, d), d) == s` for each recorded fixture field.
BigInteger holds 9.2e18: at 8 decimals that is 92 billion BTC, at 6 decimals
9.2 trillion shares; nothing in scope needs 18-decimal wei because every
exchange reports at most 10 dp. A CHECK constraint keeps `decimals <= 12`.

### 2.2 Valuation and returns

- `prices_daily(instrument_id, date, close_atoms_quote, source)`; the
  existing `market_prices` remains for binary marks. `marketdata.py` gets a
  `fetch_daily_closes(instrument, start, end)` on `MarketDataSource`, served
  by the trader's own venue (Kraken OHLC, Coinbase candles, Alpaca bars) with
  CoinGecko as crypto fallback.
- Equity(t) = USD cash + sum over assets of qty(t) x close(t). For binary
  venues the existing "at cost, marks in extras" rule holds until the
  methodology is revised, so public numbers on Kalshi pages do not move when
  Ledger v2 lands.
- Returns become **flow-adjusted time-weighted returns**: the daily series is
  chained across external-flow days (deposit, withdrawal, transfer_in/out
  legs) so a deposit is not a gain. Formula and hand-computed test go into
  `docs/methodology.md` ("External flows") before any asset venue is public.
- `reconcile.py` v2: compare replayed vector to `fetch_balances()` vector
  per asset; tolerance per asset is max(1 atom, 0.1%); USD tolerance
  unchanged. Kraken additionally reconciles against the running `balance`
  column on every ledger row (exact, no tolerance). Any mismatch quarantines,
  as today.
- Verification tiers grow from {reconciled, fills_only, unclaimed} to add
  `daily_verified` (aggregator data, day resolution, ids not guaranteed
  stable) and the tier is shown on every page and in `/api/strategies`.

### 2.3 Connector contract v2 (`connectors/base.py`)

Additive; Kalshi and Polymarket keep working through adapters.

```
CredentialKind   += KEY_SECRET_PASSPHRASE, OAUTH2, REPORT_TOKEN, AGGREGATOR
ReadOnlyProof     = scope_endpoint | order_probe | token_type | aggregator_flag | none
VenueProfile     += readonly_proof, transfers_observable: bool,
                    history_depth (e.g. "all", "90d", "10k_fills"),
                    quote_asset ("USD" | "USDC"), tos_doc ("docs/venues/kraken.md"),
                    requires_partner_approval: bool
NormalizedFill   += instrument fields (base_asset, quote_asset, qty_atoms,
                    decimals, price as quote string, fee_asset, fee_atoms)
NormalizedTransfer  venue_ref, asset, amount_atoms, kind, occurred_at
NormalizedBalances  as_of, list[(asset, atoms, decimals)]
VenueConnector   += fetch_transfers(since), fetch_balances(),
                    prove_read_only() -> ReadOnlyEvidence (method, checked_at,
                    raw flags minus secrets); check_write_scope() delegates to it
```

`prove_read_only` is re-run nightly and its evidence stored on the account
(`scope_evidence` JSON); a key whose flags change to writable is suspended,
not silently kept.

### 2.4 Credentials v2

```
venue_credentials  id, venue_account_id, kind, ciphertext (Fernet),
                   wrapped_dek (KMS), key_version, expires_at (OAuth),
                   refresh_ciphertext (OAuth), rotated_at, revoked_at
```

- `vault.py` v2: `encrypt(plaintext) -> (ciphertext, wrapped_dek, version)`,
  `decrypt(...)`; the KMS client is behind a `KeyWrapper` protocol with a
  `LocalKeyWrapper` (Fernet on `VAULT_KEY`, for dev and tests) and an
  `AwsKmsKeyWrapper`. Preflight refuses `LocalKeyWrapper` in prod.
- `MultiFernet` rotation job: new key first, re-encrypt in batches, drop old.
- OAuth: a generic `oauth.py` (auth-code + PKCE, state in the session,
  refresh with jitter before expiry, revoke on disconnect). Alpaca is the
  first user; Webull Connect and Schwab plug in later.
- Decrypt events are written to the audit log with the job or request id.

### 2.5 Worker v2: jobs, budgets, backfills

```
jobs   id, kind (sync | backfill | reconcile | prices | stats | mirror |
       oauth_refresh | key_rotate | scope_recheck), venue_account_id, payload
       (cursor JSON), run_after, attempts, lease_until, last_error, done_at
```

- Still one worker process (APScheduler stays as the ticker), but each tick
  claims jobs with `SELECT ... FOR UPDATE SKIP LOCKED`, so several worker
  processes can run later without a queue server.
- Per-venue rate budget objects in the profile (`rate_budget`: requests per
  window, cost per endpoint) enforced by a token bucket per (venue, account).
  Kraken's counter model and Binance's weight model both fit.
- Backfills are resumable: the job payload holds the cursor (Binance:
  symbol list + `fromId`; Kraken: `ofs`; Coinbase: cursor; IBKR: last
  statement date). A backfill that runs out of budget re-schedules itself.
- Dead letter after N attempts sets the account to `error` with a
  user-facing reason, as today.

### 2.6 Payments (Stripe) and entitlements

- `customers (user_id, stripe_customer_id)`, `subscriptions (stripe_sub_id,
  plan, status, current_period_end)`, `stripe_events (event_id UNIQUE,
  received_at, processed_at)` for idempotent webhooks, `creator_payouts`
  (connect account id, period, amount_micros, stripe_transfer_id).
- Entitlement is derived only from webhook-confirmed subscription state,
  never from the checkout redirect.
- Products: Creator Pro (flat monthly), Follower Plus (flat monthly, unlocks
  premium creator pages and paper-portfolio tools). No per-trade, per-tail,
  or P&L-linked fee anywhere (D8). Creator revenue share is a fixed
  percentage of Follower Plus revenue attributable to their page, paid as a
  Connect transfer, 1099-NEC via Stripe.
- The Stripe business description and the site copy Stripe reviews must not
  mention prediction markets, betting, or returns; the checkout page
  describes analytics software.
- Nothing in the codebase ever holds a balance on behalf of a user. A test
  pins that no table has a "user balance" column and that the only Stripe
  charge path is Koine's own products.

### 2.7 Security

Controls, in the order they are worth doing:

1. TOTP (`pyotp`) required before `POST /venues` and before any credential
   view/rotation; recovery codes hashed; step-up re-auth window 10 min.
2. DB-backed sessions (`sessions` table, opaque id, rotate on login, idle
   timeout 24 h dashboard, absolute 30 d) replacing the signed cookie.
3. Rate limits (`slowapi`, in-process at first) on login, signup, TOTP,
   connect, and the public JSON API.
4. `audit_log` (append-only trigger): key connect/disconnect/rotate,
   decrypt events, scope-check results, admin actions; IP and UA; never
   secrets. Preflight expects the sixth trigger.
5. Static egress IP on Fly so users can IP-restrict keys to Koine.
6. CI: `gitleaks`, `pip-audit`, ruff, pytest, `alembic check`; Dependabot.
7. Postgres roles: `koine_web` cannot SELECT `venue_credentials.ciphertext`;
   `koine_worker` can. Backups: nightly `pg_dump` encrypted with an
   `age` key stored outside Fly, restore drill quarterly.
8. Written incident plan (Illinois PIPA, FTC Safeguards 30-day rule if
   applicable), and the nine Safeguards elements adopted as
   `docs/security_policy.md`.
9. SOC 2: start evidence (policies, access reviews, change management) in a
   folder now; buy an automation platform only when a partner asks.

Data minimisation is a connector rule: request the narrowest scope the
venue offers, store only ledger fields, and on disconnect delete
ciphertext immediately (already true) and mark the strategy
"source disconnected on <date>".

---

## 3. Venue plan

### 3.1 Crypto

| Order | Venue | Why here | Credential | Read-only proof | Transfers | Balance | Gaps to design around |
|---|---|---|---|---|---|---|---|
| 1 | **Kraken** | US, friendliest terms (documents third-party keys), `Ledgers` has per-row running balance so reconciliation is exact, all-time trade history | key + secret (HMAC-SHA512) | order_probe: `AddOrder validate=true` and `WithdrawMethods` must return permission denied; permissions requested: Query Funds, Query Closed Orders & Trades, Query Ledger Entries only | `Ledgers` (deposit, withdrawal, staking, transfer) | `Balance` vector | Low counters (15-20, decay 0.33-1/s): backfill throttled by budget; no self-describe endpoint, so the methodology says "probe-verified" |
| 2 | **Coinbase Advanced Trade** | US, key-level `View`, `key_permissions` self-describe (same proof class as Kalshi), stable `entry_id` | CDP key id + EdDSA private key (same `RSA_PEM`-style shape) | scope_endpoint: `GET /api/v3/brokerage/key_permissions` (`can_trade`, `can_transfer` must be false) | v2 `accounts/:id/transactions` with the same key | `brokerage/accounts` vector | **CDP Terms may forbid aggregation/public display: `requires_partner_approval=true` until written approval** [VERIFY]; one key per portfolio; `trade_id` not unique, use `entry_id` |
| 3 | **Hyperliquid** | Polymarket-shaped (public address, no secret), fills carry `closedPnl`, USDC fees | address | none (nothing to prove); ownership via claim | `userNonFundingLedgerUpdates`, `userFunding` | `clearinghouseState` | Only last 10k fills served: `history_depth="10k_fills"`, anchor design already covers it; perps need `funding` legs; US trader restriction on the app |
| 4 | **Binance.US** | Best scope endpoint (`apiRestrictions`) | key + secret (HMAC; Ed25519 [VERIFY]) | scope_endpoint | deposit/withdraw/transfer/convert/dust histories with 30-90 d windows | `account` vector | `myTrades` per symbol + 24 h windows: symbol discovery step, `fromId` pagination, resumable backfill jobs; Binance.com is out (no US) |
| later | Gemini (Auditor role), Bybit/OKX (non-US or terms forbid), Robinhood Crypto (no fills/transfers endpoints) | | | | | | |

Price source per venue: the venue's own public candles (daily close), stored
in `prices_daily`; CoinGecko Demo as fallback for delisted assets.

### 3.2 Equities

| Order | Venue | Why here | Credential | Read-only proof | Transfers | Balance | Gaps |
|---|---|---|---|---|---|---|---|
| 1 | **Alpaca (OAuth)** | Only broker with self-serve, sanctioned third-party OAuth; activities endpoint covers FILL plus every cash type (TRANS, DIV, FEE, INT, JNL, SSP, OPASN...); free market data + Corporate Actions API on the same account | OAuth2, **request no scopes** (read-only by default) | order_probe on paper (`POST /v2/orders` must fail) plus recorded scope list [VERIFY introspection] | activities | `account` (cash, equity) | Small retail base; commercial apps must disclose to Alpaca; short sales and options need the option/short leg model before they are accepted (reject at ingest until then, as BUILD_PLAN 7C says) |
| 2 | **IBKR Flex Web Service** | Reporting-only token by design, user-set expiry, trades with `ibExecID`, cash transactions, corporate actions, NAV; no partner onboarding | report token + query id | token_type | Flex cash transactions | Flex NAV (EOD) | End-of-day only: `daily_verified` tier; Flex ToS on third-party use [VERIFY]; XML parsing |
| 3 | **SnapTrade** (Robinhood, Fidelity, Schwab, Webull, Public, E*TRADE, Vanguard) | Long tail at $1-2/user/month; read-only connection type; free 5 connections to prototype | aggregator (SnapTrade user id + secret, plus their OAuth flow) | aggregator_flag | activities (CONTRIBUTION, WITHDRAWAL, DIVIDEND, SPLIT...) | holdings + cash (daily) | Activity ids can change on reprocessing: dedupe on content hash; daily refresh only; `daily_verified` tier; no independent read-only proof (state it) |
| file now | Webull Connect API, Schwab commercial approval | Asynchronous reviews | OAuth2 | unknown scopes [VERIFY] | | | Do not plan around either |
| no | Public.com (ToS forbids third-party and mirroring), Robinhood direct (no API, ToS), DriveWealth (B2B white-label), moomoo (user-hosted gateway) | | | | | | |

Equities modelling that the ledger must carry before Alpaca goes public:
splits and symbol changes (instrument id + `symbol_history`), dividends as
cash legs, fractional shares (6 decimals), T+1 (trade date for P&L), cash
that can go negative on margin, fees as separate legs. Options and shorts are
phase 2 of the equities milestone.

### 3.3 Per-venue terms position (invariant 10)

`docs/venues/<venue>.md`, one page each: the clause that permits or forbids
consented third-party read access, the date checked, the link, the consent
text shown on the connect screen, and the status (`ok`, `needs_approval`,
`blocked`). The registry test asserts every enabled venue has one with
status `ok` or the profile has `requires_partner_approval=true` and the
connect form refuses it in prod.

Current positions from the research: Kraken `ok`; Alpaca `ok` (read-only
OAuth, disclose commercial use); Coinbase `needs_approval`; Binance.US `ok`
with "at your own risk" consent; Hyperliquid `ok` for data, US-trader
caveat; Kalshi `needs_approval` (already known); IBKR Flex `[VERIFY]`;
SnapTrade `ok` under their agreement; Public.com, Robinhood, OKX `blocked`.

---

## 4. Milestones

Sized like BUILD_PLAN: each demoable, each with an exit test. Suggested
order is M8 through M13; M10 and M11 can interleave once M8 lands.

### M8. Ledger v2, credentials v2, jobs (foundation)

1. Commit the working tree (D1). Tag `m7`.
2. `instruments`, `symbol_history`, `ledger_entries`, `anchor_balances`,
   `venue_credentials`, `jobs` (migration 0009; append-only trigger on
   `ledger_entries`; preflight expects it).
3. `money.to_atoms/from_atoms`; `ledger.py` gains `legs_for_fill`,
   `legs_for_settlement`, `legs_for_transfer`, `balance_vector`; the old
   `build_cash_curve` is reimplemented over USD legs.
4. Port Kalshi and Polymarket connectors to the v2 contract (adapters:
   `fetch_transfers` returns [] and the profile says
   `transfers_observable=False` for now; `fetch_balances` wraps the single
   balance). Kalshi transfers: check whether the portfolio API exposes
   deposits/withdrawals; if not, the M2 "manual classification" path becomes
   a `ledger_entries` adjustment row created by an admin action, audited.
5. `vault.py` v2 with `LocalKeyWrapper` and `AwsKmsKeyWrapper`;
   `MultiFernet` rotation job; migrate `encrypted_private_key` into
   `venue_credentials`.
6. Jobs table and claim loop; existing sync/reconcile/stats/mirror become
   job kinds; per-venue rate budgets in the profile.
7. `docs/methodology.md`: "Ledger legs", "External flows", "Precision by
   asset" sections with hand-computed tests.

Exit: all 150 existing tests pass unchanged (the Kalshi cash curve computed
from legs equals the old curve to the micro on every fixture); the live demo
account reconciles to 0 divergence through the legs path; `preflight` passes
with the KMS wrapper against a real KMS key; a forced key rotation
re-encrypts every credential with no downtime.

### M9. Account security and consent

1. TOTP enrolment, enforced before connect; recovery codes.
2. DB-backed sessions; rate limits; `audit_log` with trigger.
3. Consent screen per venue rendered from `docs/venues/<venue>.md` (ticked
   consent stored with timestamp and text hash).
4. Static egress IP; CI with gitleaks + pip-audit + `alembic check`;
   encrypted backups + a restore drill script.
5. `docs/security.md` and `/security` updated: proof method per venue, what
   a read-only key exposes per venue, disconnect semantics, the incident
   plan summary. `docs/security_policy.md` (nine Safeguards elements).

Exit: a user without TOTP cannot reach `POST /venues` (test); every decrypt
appears in `audit_log` (test); preflight checks the audit trigger, the
session backend, and that every enabled venue has a terms file.

### M10. Crypto venues

1. Kraken connector + market data (OHLC daily closes); probe-based read-only
   proof; `Ledgers` running-balance reconciliation; fixtures recorded from a
   throwaway account (respx only in tests).
2. Multi-asset stats: equity from `prices_daily`, flow-adjusted TWR, per-asset
   exposure/concentration flags; methodology "Crypto accounts".
3. Coinbase connector behind `requires_partner_approval` (built and tested
   against recorded fixtures; connect form refuses until the flag flips).
   Send the CDP approval request in parallel.
4. Hyperliquid connector (address-keyed; `funding` legs; 10k-fill history).
5. Binance.US connector with symbol discovery + windowed resumable backfill.
6. Marketplace venue filter; venue badges; per-venue methodology sections
   (BUILD_PLAN 7E).

Exit: one real Kraken account (founder-owned) reconciles nightly for a week
with 0 divergence including a deposit and a staking reward; Coinbase fixture
tests pass; public page shows the proof method and tier.

### M11. Equities venues

1. `oauth.py` (auth-code + PKCE, refresh, revoke) and the Alpaca connector
   (activities: FILL, TRANS, DIV, FEE, INT, JNL, SSP; reject OPASN/short
   sells at ingest with a visible "unsupported activity" flag until phase 2).
2. `prices_daily` from Alpaca bars; corporate actions job (splits, symbol
   changes) writing `split` legs and `symbol_history`.
3. Equities stats: NAV reconciliation against Alpaca `equity`; methodology
   "Equity accounts" (splits, dividends, T+1, margin cash).
4. IBKR Flex connector (XML statements, EOD, `daily_verified` tier).
5. SnapTrade connector (content-hash dedupe, daily refresh, cost
   accounting per connected user in an `integration_costs` table so the
   $1-2/user is visible on the admin dashboard).
6. Phase 2: options legs (OCC symbols, multiplier, expiry job) and short
   positions.
7. File Webull Connect and Schwab commercial applications at the start of
   M11; log responses in `docs/venues/`.

Exit: a founder Alpaca paper account and a live account reconcile against
`equity` nightly across a dividend and a split (Alpaca's paper environment
can simulate neither, so the split test is fixture-driven and the live
account must see one real dividend); IBKR Flex statement fixtures round-trip;
SnapTrade sandbox connection ingests a full activity set.

### M12. Payments

1. Stripe Billing products (Creator Pro, Follower Plus), Checkout, Customer
   Portal, Stripe Tax; `stripe_events` idempotent webhook handler;
   entitlements from webhook state only; test clocks in CI for renewal and
   dunning.
2. Stripe Connect (Express shape): creator onboarding link, payout ledger,
   monthly transfer job, 1099 via Stripe.
3. Preflight: Stripe keys present in prod, webhook secret set, no
   money-holding column anywhere (grep-based test), Stripe business
   description text checked into `docs/payments.md`.
4. `docs/payments.md`: the "money in is for Koine's service, money out is
   Koine's expense" rule, the prohibited-fee list, and disclosure text
   ("How creators are paid") that every promoted creator card links to.

Exit: a full subscribe, renew, fail-payment, cancel cycle passes under a
test clock; a creator receives a sandbox transfer; entitlement flips only on
webhook events.

### M13. Compliance surface and scale

1. Cohort "generally expected results" stats (median return, share
   negative, per venue and tier) on the marketplace and every strategy page
   (FTC Endorsement Guides); a written performance-claims policy in
   `docs/methodology.md`.
2. Losing strategies never hidden from the public dataset (test).
3. SOC 2 evidence folder; access reviews quarterly; pen test when a partner
   asks.
4. Worker to multiple processes (SKIP LOCKED already allows it); Postgres
   roles split; read replicas only if page latency demands.
5. Revisit BUILD_PLAN 2.2 "one account = one strategy" once sub-account
   fields (Kalshi `subaccount_number`, Coinbase portfolios, IBKR
   sub-accounts) are common.

---

## 5. Payments and regulation guardrails (restated for the codebase)

- Koine sells software. Every Stripe product is a subscription to Koine's
  own features. No product is priced on trades, tails, P&L, AUM, or
  referrals that pay on trading.
- Creator payments are royalties for licensed content, flat percentages of
  subscription revenue, paid via Connect. Creators are not advisers to
  followers and Koine does not recommend strategies (regulatory_reference.md
  2a); the LLM card stays descriptive and human-reviewed.
- Paper tailing stays behind login, labelled simulated, with the fee model
  stated, and no public "tail" button (Pivot 4).
- No crypto payments, no stablecoin rails, no wallets (A4).
- Never a venue password (invariant 9); OAuth, keys, or report tokens only.
- Each venue's "third-party access at your own risk" clause is surfaced as
  consent, and Koine's own terms disclose exactly what is read and shown.

---

## 6. Open items to resolve before the milestone that needs them

| Item | Needed by | Owner |
|---|---|---|
| Coinbase CDP Terms: written approval for consented aggregation and public display, or use via SnapTrade/Plaid | M10.3 | Aiden (email CDP partnerships) |
| Kalshi Developer Agreement written permission (already open) | ongoing | Aiden |
| IBKR Flex Web Service terms on third-party consumption | M11.4 | counsel / IBKR |
| Alpaca: confirm zero-scope OAuth token cannot place orders; confirm app registration still self-serve; disclose commercial use | M11.1 | build + Alpaca support |
| Binance.US Ed25519 + `apiRestrictions` availability | M10.5 | smoke test |
| Kalshi transfers endpoint (deposits/withdrawals) existence | M8.4 | smoke test |
| GLBA Safeguards applicability to a consented aggregator | M9.5 | counsel (fold into the existing fixed-fee memo, §7) |
| Illinois money-transmitter statute current text | M12 | counsel |
| AWS account + KMS key in `us-east-1` (Fly `iad`) | M8.5 | Aiden |
| Static egress IP on Fly | M9.4 | Aiden |
| Stripe account activation with the SaaS description | M12 | Aiden |

---

## 7. Proposed CLAUDE.md changes (apply when M8 starts)

- Invariant 1: "All money is INTEGER MICRO-DOLLARS. Asset quantities are
  integer atoms with a per-instrument `decimals` (<= 12), parsed only from
  the venue's decimal string via `money.to_atoms`. Floats never touch stored
  monetary or quantity values."
- Invariant 3: add `ledger_entries` and `audit_log` to the append-only list.
- Invariant 4: add "or OAuth grants and report tokens; credentials are
  envelope-encrypted with a KMS-wrapped data key; the local wrapper is
  refused in prod."
- New invariant 9: "Koine never accepts a venue username or password."
- New invariant 10: "A venue is enabled in the registry only with a
  `docs/venues/<venue>.md` terms position and a consent screen."
- Layout: `jobs.py`, `oauth.py`, `billing.py`, `audit.py`, `docs/venues/`,
  `docs/research/`, `docs/platform_plan.md`.

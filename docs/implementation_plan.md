# Koine implementation plan: crypto, equities, paper copying, security, rate limiting (2026-09-26)

Written for: Aiden (and Kyle), as the build order for the next phase.

`docs/platform_plan.md` (2026-09-16) says **what** to build and **why**
(decisions D1-D10, milestones M8-M13, venue research). This file says **in
what order**, **how**, and **what has to be set up outside the repo**. When
the two disagree, fix one of them; do not leave both.

## 0. Scope confirmed on 2026-09-26

| Area | In this pass | Deferred |
|---|---|---|
| Copying | **Paper copy for every asset class** (binary, crypto spot, crypto perps, equities). Invariants 4 and 8 unchanged: read-only keys only, `get_executor("live")` still raises. | Sandbox or live execution, write-scoped keys |
| Crypto | Kraken, Coinbase (behind written CDP approval), Hyperliquid, Binance.US | On-chain self-custody wallets, Binance.com/Bybit/OKX |
| Equities | Alpaca OAuth, IBKR Flex, SnapTrade; long stocks/ETFs incl. dividends, splits, symbol changes | Options, short positions (flagged "unsupported activity" at ingest) |
| Security | TOTP, DB sessions, CSRF, headers, KMS envelope vault, audit log, CI scanning, backups, consent screens | SOC 2 audit, pen test (evidence collection starts now) |
| Rate limiting | Inbound (protect Koine) and outbound (respect venue budgets, per key and per egress IP) | |
| Payments | none | M12 (Stripe) and M13 (compliance surface) stay as written in platform_plan.md |
| Hosting | **Fly.io** + Fly Managed Postgres, AWS KMS for the vault, Cloudflare in front (decided 2026-09-26; no accelerator credits) | Revisit GCP/AWS on the triggers in section 2.2 |

Regulatory and pricing questions are parked on purpose. Nothing in this
pass moves money or places orders, so parking them costs nothing later.

---

## 1. Build order at a glance

```
P0 hygiene + CI ──┐
                  ├─> P1 Ledger v2 ──> P2 jobs + outbound limits ──┬─> P4 crypto ──┐
P3 security + inbound limits (starts with P1, must finish before   ├─> P5 equities ├─> P6 paper copy v2 ──> P7 streaming
    any new venue is connectable in prod) ─────────────────────────┘               │
                                                                    P8 product surface (alongside P4-P6)
```

Sizes are focused-work estimates for one developer working with Claude. They are
for ordering, not for promising dates.

| Phase | What | Size | Exit test (short) |
|---|---|---|---|
| P0 | Hygiene, CI, Postgres test job | 1-2 days | CI runs pytest on Postgres, gitleaks, pip-audit, `alembic check` |
| P1 | Ledger v2 (M8.2-8.4, 8.7) | ~2 wks | All existing tests pass unchanged; Kalshi curve from legs == old curve to the micro |
| P2 | Jobs + outbound rate limiting (M8.6 expanded) | ~1 wk | 2 worker processes never double-claim; a 429 backs off and resumes from cursor |
| P3 | Security + inbound limits + vault v2 (M8.5, M9) | ~2 wks | No TOTP → no `POST /venues`; every decrypt audited; login limited before bcrypt |
| P4 | Crypto venues (M10) | ~3-4 wks | Founder Kraken account reconciles 7 nights at 0 divergence incl. deposit + staking |
| P5 | Equities venues (M11 minus phase 2) | ~3 wks | Founder Alpaca live account reconciles across a real dividend; split via fixtures |
| P6 | Paper copy v2 | ~2 wks | Hand-computed follower replays for spot, perp (funding + liquidation), equity (split + dividend) |
| P7 | Streaming ingestion (optional) | ~1-2 wks | Copy lag p50 < 30 s for Kraken/Coinbase/Alpaca creators with followers |
| P8 | Product surface for multi-asset | runs alongside | Marketplace filters by venue/asset; multi-asset strategy + portfolio pages |

---

## 2. Hosting: comparison and recommendation (decided: Fly.io, 2026-09-26)

### What this app needs from a host

1. **A long-running worker.** Polling, reconciliation, copy fan-out, and in
   P7 persistent WebSocket connections. This rules out serverless-only hosts
   (the Vercel deployment stays a demo; see docs/deploy.md).
2. **Static egress IPs.** Kraken, Coinbase and Binance.US let users restrict a
   key to an IP list. Telling users "allow these IPs" is a real security
   feature. Hyperliquid and public market-data endpoints also limit per IP,
   so the number of egress IPs is a scaling lever (section 2.3).
3. **Managed Postgres** with backups and point-in-time recovery. Postgres is
   the only stateful component on purpose (jobs, rate limits, sessions all
   live there).
4. **A KMS** to wrap credential data keys (D5).
5. **Low ops burden**: one or two people, no dedicated infra engineer.

### 2.1 The options

| | Fly.io (+ AWS KMS) | GCP (Cloud Run + Cloud SQL) | AWS (ECS Fargate + RDS) | Render / Railway |
|---|---|---|---|---|
| Web | Machines from our Dockerfile | Cloud Run service | Fargate service behind ALB | Web service |
| Worker | Process group in the same app | Cloud Run **worker pools** (GA April 2026) or a small GCE VM | Fargate service | Background worker |
| Postgres | Fly Managed Postgres | Cloud SQL (HA optional) | RDS (Multi-AZ optional) | Managed PG |
| Static egress IP | `fly ips allocate-egress -r iad`, $3.60/mo per IPv4, one per region, up to 64 machines each | Direct VPC egress + Cloud NAT + reserved IP | NAT Gateway + Elastic IP | Available on paid plans (shared or dedicated ranges, check per plan) |
| KMS | Cross-cloud: AWS KMS (or GCP KMS) over the internet | Cloud KMS, same project, IAM-bound | AWS KMS, IAM role on the task | Cross-cloud |
| WAF / edge limits | Cloudflare in front | Cloud Armor (needs an external LB) or Cloudflare | AWS WAF on ALB or Cloudflare | Cloudflare |
| Setup effort | **Low**: fly.toml and Dockerfile already exist | Medium: VPC, NAT, IAM, LB, Artifact Registry | High: VPC, subnets, NAT, ALB, IAM, ECR, task defs | Low |
| Rough baseline cost* | ~$50-90/mo | ~$90-180/mo | ~$130-250/mo | ~$40-100/mo |
| Startup credits | none | Google for Startups (often via accelerators) | AWS Activate (often via accelerators) | none |
| Security questionnaire story | SOC 2 report available; "small PaaS" | Strong; inherits GCP controls | Strongest; what fintech partners expect | Weaker |
| Main risk | Past reliability incidents; smaller ecosystem | Networking setup is fiddly (NAT timeouts are a known issue) | Ops overhead, and slow to move fast | Fewer controls; egress IP options vary |

\* Order of magnitude for 2 small web instances, 1 worker, a small
single-node Postgres, 1 egress IP, and a KMS key. HA databases roughly double
the database line. Check with each provider's calculator before committing.

### 2.2 Why the recommendation leans Fly for now, and when to switch

- **The code is already portable, and this plan keeps it that way.** It's a
  plain Docker image plus Postgres. Rate limits, jobs and sessions live in
  Postgres (no Redis), and KMS sits behind a `KeyWrapper` protocol with
  local, AWS and GCP implementations. Moving hosts later means a `pg_dump`,
  a DNS change and publishing a new egress IP list to users. That is days
  of work, not a rewrite.
- **Fly is the cheapest way to meet all five needs today**, because the
  deploy config exists and static egress is one command.
- **GCP or AWS win when one of these becomes true.** Then switch, and do it
  before real-user volume grows:
  1. The accelerator offers cloud credits (tens of thousands of dollars of
     credit erases the cost difference). **Question for Aiden: does it?**
  2. A partner (Coinbase CDP approval, a broker, SnapTrade production) sends
     a security questionnaire, and "KMS, DB and compute in one IAM
     boundary" matters.
  3. More than 64 worker machines per region, or multi-region.
- **GCP vs AWS if we switch:** GCP is less work for this shape. Cloud Run
  web plus worker pools is close to the Fly model, and Cloud KMS and Secret
  Manager live in the same project. AWS has the stronger fintech-partner
  story and more people who can run it. With no credits either way, pick
  GCP for speed; with credits, pick whichever cloud gave them.
- **Supabase** stays an option for Postgres alone on any of the above. It
  adds a vendor without removing a need, so it is not recommended unless it
  is already in use.

### 2.3 How it scales (what grows, and where the ceilings are)

| Load | Grows with | First ceiling | Lever |
|---|---|---|---|
| Venue calls (per key) | connected accounts | Venue limit per key/account (Kraken counter 15-20, Alpaca 200/min, etc.) | Each user brings their own budget. Scales naturally; the job scheduler must respect it (P2) |
| Venue calls (per IP) | accounts on IP-limited venues | **Hyperliquid: 1,200 weight/min per IP**; `userFills*` costs 20+ each, so one IP polls at most ~300 wallets every 5 min. **WebSocket: at most 10 unique users per IP.** Public market data is also per IP | Poll wallets without followers less often; more egress IPs; a Hyperliquid data provider or our own non-validator node later |
| Worker throughput | accounts × cadence | Postgres job claims (`SKIP LOCKED`) are fine to hundreds of jobs/s | Add worker processes; move to Cloud Tasks/SQS only past that |
| Copy fan-out | followers per creator | One signal × N followers = N paper fills; batched inserts handle 10k+ | Batch per signal; followers never re-query the venue |
| Public reads | viewers | Pages read precomputed `strategy_stats` | CDN cache (Cloudflare) + ETags on public pages and `/api/*`; read replica later |
| Storage | fills × legs | `ledger_entries` ~3 rows per fill. 10k active crypto accounts × 50 fills/day ≈ 1.5M rows/day | Index `(venue_account_id, occurred_at)`; partition by month past ~100M rows |

Stages: **up to about 1k users**: 1-2 web, 1 worker, small Postgres, 1
egress IP. **1k-10k users**: 2-3 web, 2-4 workers, larger Postgres, 2-4
egress IPs, CDN on public pages, streaming (P7) for creators with followers.
**Above 10k users**: partition the ledger, add a read replica, probably
move to a hyperscaler.

---

## 3. Phases in detail

### P0. Hygiene and CI (start here)

1. Commit the pending `.gitignore`, `CLAUDE.md` and `.graphifyignore` changes.
2. CI (`.github/workflows/ci.yml` already runs ruff and pytest on SQLite):
   add a Postgres service job that runs `alembic upgrade head`, `alembic check`
   and the trigger tests; add `gitleaks` and `pip-audit`; enable Dependabot
   (pip + actions).
3. `tests/conftest.py`: a `pg` marker, so the tests that need Postgres
   (append-only triggers, `SKIP LOCKED`, `ON CONFLICT` limits) run in CI and
   are skipped locally when `db_test` is down.
4. Pin the recorded-fixture convention: `tests/fixtures/<venue>/<endpoint>_<date>.json`,
   plus `scripts/record_fixture.py`, which redacts ids and amounts on request.

### P1. Ledger v2 (platform_plan 2.1, M8 items 2-4 and 7)

Files: `money.py`, `models.py`, `ledger.py`, `ingest.py`, `reconcile.py`,
`stats.py`, `connectors/base.py`, `connectors/{kalshi,polymarket}.py`,
migration `0009_ledger_v2.py`, `docs/methodology.md`.

1. `money.to_atoms(s, decimals)` / `from_atoms`: parse strings only, round-trip
   test for every fixture field.
2. Tables: `instruments` (+ `qty_step_atoms`, `min_notional_micros`,
   `multiplier`, `kind` incl. `perp`), `symbol_history`, `ledger_entries`
   (append-only trigger + ORM guard), `anchor_balances`, `prices_daily`.
3. `ledger.legs_for_fill / legs_for_settlement / legs_for_transfer /
   legs_for_funding / legs_for_corporate_action`; `balance_vector(as_of)`;
   `build_cash_curve` re-implemented over USD legs.
4. Contract v2 (additive): `NormalizedFill` instrument fields,
   `NormalizedTransfer`, `NormalizedBalances`, `NormalizedFunding`,
   `fetch_transfers`, `fetch_balances`, `prove_read_only`. Kalshi and
   Polymarket are ported through adapters.
5. Valuation: equity(t) = cash + Σ qty × close. Flow-adjusted TWR (deposits
   are not gains). Reconcile v2 compares per-asset vectors.
6. Methodology sections: "Ledger legs", "External flows", "Precision by
   asset", each with a hand-computed test.
7. Apply the CLAUDE.md invariant amendments (platform_plan section 7) in the same
   PR as the migration.

Exit: the existing tests pass without edits; the Kalshi demo account
reconciles to 0 through legs.

### P2. Jobs and outbound rate limiting (platform_plan 2.5, expanded)

New `jobs.py` and `ratelimit.py` (shared with P3); `worker.py` becomes a
claim loop.

1. `jobs` table (kind, account, payload/cursor JSON, priority, run_after,
   attempts, lease_until, last_error, done_at). Claim with
   `FOR UPDATE SKIP LOCKED`; lease plus heartbeat; dead-letter after N attempts
   with a user-visible reason.
2. **Priorities**: `sync` for creators with active followers (copy lag
   matters) > `sync` for others > `reconcile`/`stats` > `backfill`. Accounts
   get `next_sync_at` with jitter, so 5-minute ticks do not stampede.
3. **Outbound limiter**: token buckets in Postgres, keyed by
   `(venue, credential)` **and** `(venue, egress_ip)`. Per-endpoint costs come from
   `VenueProfile.rate_budget` (Kraken counter + decay, Binance weights,
   Hyperliquid weights including the +1 per 20 items). Before each call:
   `acquire(cost)` either returns or tells the job to re-schedule at
   `run_after`. On 429/418, honour `Retry-After`, halve that bucket's refill
   rate for 10 min (adaptive), and log it.
4. **Per-venue circuit breaker**: 5 consecutive 5xx/timeouts open it for
   2 min; open means jobs defer instead of failing, so a venue outage never
   dead-letters every account.
5. **Resumable backfills**: the cursor lives in the job payload (Kraken `ofs`,
   Binance symbol + `fromId` + window, Coinbase cursor, IBKR statement date,
   SnapTrade page). Running out of budget re-queues the backfill; it is never
   an error.
6. `httpx` client per venue with explicit timeouts and a bounded retry for
   idempotent GETs only.

Exit: two worker processes against one Postgres never double-run a job
(Postgres test); a mocked 429 sequence backs off, resumes from the cursor and
completes; the Hyperliquid budget math has a hand-computed test.

### P3. Security, inbound rate limiting, vault v2 (M8.5 + M9)

Start alongside P1. **No new venue is connectable in prod until P3 is done**
(preflight enforces it).

Inbound rate limiting, in two layers:

- **Edge (Cloudflare)**: TLS, WAF managed rules, bot protection, and a rate
  rule on `/auth/*` and `/api/*`. It stops floods before they reach the app.
- **App (`ratelimit.py`)**: a `RateLimiter` protocol with `MemoryLimiter`
  (tests/dev) and `PostgresLimiter` (prod; fixed window via
  `INSERT ... ON CONFLICT DO UPDATE ... RETURNING count`, one round trip,
  only on the endpoints below). Preflight refuses `MemoryLimiter` in prod.
  Responses are 429 with `Retry-After`. The limit check runs **before**
  bcrypt, so login cannot be used to burn CPU.

| Endpoint | Key | Limit (starting values) |
|---|---|---|
| `POST /auth/login` | IP / email | 10 per min / 20 per hour, then exponential delay |
| `POST /auth/signup` | IP | 5 per hour |
| TOTP verify | user | 5 per 5 min, then 15-min lock + email |
| Password reset request | email / IP | 3 per hour / 10 per hour |
| `POST /venues`, claim, rotate | user | 10 per hour |
| `/api/*` public JSON | IP | 60 per min (higher with an API key later) |
| Follow / allocate | user | 60 per hour |

Account and session security:

1. TOTP (`pyotp`) with hashed recovery codes; required before connect,
   credential rotate, disconnect-all and admin actions; 10-min step-up window.
2. DB sessions (`sessions` table, opaque id, rotated on login, 24 h idle /
   30 d absolute, "sign out everywhere", active-session list on the
   dashboard). Replaces the signed-cookie session.
3. **Email**: verification on signup, password reset, and security alerts (new
   login, key connected, key suspended). This needs a transactional email
   provider (section 5). There is no email sending today.
4. Breached-password check on signup and reset (HIBP Pwned Passwords range
   API, k-anonymity, free, no key).
5. CSRF: `Origin`/`Referer` check on every cookie-authenticated unsafe
   method, plus a token on HTML forms (SameSite=lax alone is not enough).
6. Security headers middleware: CSP (no inline script except hashed), HSTS,
   `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`.

Credentials and data:

7. `vault.py` v2: `venue_credentials` table, Fernet record cipher, KMS-wrapped
   data keys (`KeyWrapper`: Local / AWS / GCP), `MultiFernet` rotation job,
   OAuth refresh/expiry fields; migrate `encrypted_private_key` into it.
8. `prove_read_only()` re-runs nightly. A key that becomes writable is
   **suspended**, the user is emailed, and the event is audited.
9. `audit_log` (append-only trigger): connect, disconnect, rotate, decrypt,
   scope results, login, TOTP changes, admin actions, IP and user agent,
   never secrets.
10. A log-redaction filter plus a test that feeds fixture secrets through
    every logger and error path and asserts they never appear (invariant 4). The same
    scrubber goes on the error tracker.
11. Postgres roles: `koine_web` cannot read credential ciphertext;
    `koine_worker` can. Encrypted nightly backups off-host (`age`) and a
    restore-drill script.
12. Consent screen per venue from `docs/venues/<venue>.md`; consent stored
    with a hash of the text shown.
13. Docker image runs as non-root; secrets only from the platform secret
    store.

Exit: the platform_plan M9 exit criteria, plus the limiter, CSRF and
redaction tests, plus preflight checking all of the above.

### P4. Crypto venues (M10)

Order: **Kraken → Hyperliquid → Coinbase → Binance.US**. Hyperliquid moves up
from the 2026-09-16 order because it needs no key, no approval and no
account funding to test, and it exercises perps and funding early.

For each venue: connector, `VenueProfile` (credential kind, read-only proof,
rate budget, fee model, history depth, tos doc), `docs/venues/<venue>.md`,
recorded fixtures, a respx test suite, market data (`fetch_daily_closes`,
`fetch_quote`), and a methodology section.

1. **Kraken**: HMAC-SHA512 + nonce; `TradesHistory`, `Ledgers` (the running
   balance gives exact reconciliation), `Balance`, `AssetPairs` for
   instruments and lot sizes, public `OHLC`/`Ticker`. Read-only proof by
   probing `AddOrder validate=true` and `WithdrawMethods`.
2. **Hyperliquid**: address-keyed with the existing claim-code flow;
   `userFillsByTime`, `userFunding`, `userNonFundingLedgerUpdates`,
   `clearinghouseState`, `meta` (maintenance margin), `candleSnapshot`. Perp
   legs: position, realized P&L, funding, fee. `history_depth="10k_fills"`.
   Poll cadence is tiered by follower count because of the per-IP limit.
3. **Coinbase Advanced Trade**: CDP key (EdDSA), `key_permissions` proof,
   fills by `entry_id`, v2 transactions for transfers. Built and tested,
   with `requires_partner_approval=True` until written approval arrives.
4. **Binance.US**: symbol discovery → per-symbol `myTrades` with `fromId`,
   24 h windows, deposit/withdraw histories, `apiRestrictions` proof.
   This venue is the real test of P2's resumable backfill.
5. Multi-asset stats: exposure and concentration flags, per-asset P&L
   attribution, stablecoins valued at 1.00 only when the venue quotes them
   against USD, otherwise by close.

### P5. Equities venues (M11 items 1-5, 7)

1. `oauth.py` (auth code + PKCE, state in session, refresh with jitter, revoke
   on disconnect; tokens in `venue_credentials`).
2. **Alpaca**: OAuth with no trading scope; activities (FILL, TRANS, DIV,
   FEE, INT, JNL, SSP); shorts and options are rejected with a visible
   "unsupported activity" flag; `account.equity` reconciliation; bars and the
   Corporate Actions API feed `prices_daily`, `split` legs and `symbol_history`.
3. **IBKR Flex**: report token + query id; XML statement parsing; EOD;
   `daily_verified` tier.
4. **SnapTrade**: user registration, their connection portal (read-only
   connection type), activities deduped on content hash, daily refresh,
   `integration_costs` row per connected user.
5. Methodology "Equity accounts": T+1 (P&L on trade date), dividends
   (ex-date vs pay-date), splits, margin cash.
6. File the Webull Connect and Schwab commercial applications on day one;
   they are asynchronous.

### P6. Paper copy v2 (all asset classes)

The M6 engine (`signals.py`, `mirror.py`, `portfolio.py`, `tails.py`) is
binary-shaped: $1 settlement, 0.01-contract step, signals keyed by
ticker+side. v2 generalises it. The binary path must keep producing identical
numbers, and `tests/test_mirror.py` passes without edits.

1. **Signal v2**: `instrument_id`, `qty_atoms`, `decimals`, `notional_micros`,
   `creator_equity_micros` (valued with ledger v2 at fill time),
   `observed_at` (when Koine saw it) next to `filled_at` (venue time).
   Copy lag = `observed_at - filled_at`, shown on paper figures.
2. **Sizing**: the M6 rule already generalises. Buy: follower_qty =
   creator_qty × follower_equity / creator_equity. Sell: the same
   fraction of the position. Then floor to `instrument.qty_step_atoms`,
   skip below `min_notional_micros`, cap by paper cash including fee.
   Perps: follower_notional = creator_notional × follower_equity /
   creator_equity, so leverage is mirrored rather than multiplied.
3. **Paper fill price (methodology decision)**: for assets, fill at the
   venue's public quote **at `observed_at`** (ask for buys, bid for sells),
   not at the creator's price, because a follower could not have traded
   before Koine saw the signal. Fall back to the creator's price plus a stated
   penalty when no quote exists. Binary markets keep the M6 rule.
4. **Fee models in the profile**: `binary_pxq` (Kalshi, Polymarket),
   `bps_of_notional` (crypto taker rates per venue, [VERIFY] each),
   `us_equity_regulatory` (0 commission + SEC fee on sells + FINRA TAF).
5. **Lifecycle events for followers**: spot has no settlement. Perps take
   hourly funding legs from public `fundingHistory` and a **liquidation**
   when paper equity < maintenance margin (closed at mark, as a
   `liquidation` leg). Equities take splits (qty × ratio, cost unchanged)
   and cash dividends from the same corporate-actions table as creators.
   Binary markets keep settlement.
6. **Creator transfers never become signals.** They only change
   `creator_equity`, and so future weights.
7. `portfolio.py` values allocations from `prices_daily` plus latest quotes;
   the drawdown stop works unchanged.
8. Methodology "Paper copying v2", with a hand-computed test for each
   branch: spot buy/partial sell, perp with funding and liquidation, equity
   across a 2:1 split and a dividend, and a skipped signal below minimum size.

### P7. Streaming ingestion (optional, after P6)

Polling every 5 min means paper copies lag creators by minutes. Private
streams make it seconds: Kraken WS `ownTrades`, Coinbase `user` channel,
Alpaca `trade_updates` ([VERIFY] that it works on a no-trading-scope OAuth
token), Hyperliquid `userFills` (only 10 users per IP, so reserved for the
most-followed creators). A `stream` worker process holds connections for
creators with active followers, writes the same normalized fills through
`ingest.py`, and the poller remains the source of truth (streams only
shorten the lag). This is what makes a serverless host a non-starter.

### P8. Product surface (alongside P4-P6)

Marketplace filters (venue, asset class, tier); a holdings/exposure panel on
multi-asset strategy pages; per-venue methodology sections and proof-method
badges; the connect flow per credential kind (key+secret, OAuth redirect,
Flex token, SnapTrade portal, address + claim); a portfolio page for
multi-asset paper allocations; the dashboard security page (TOTP, sessions,
audit trail of your own keys).

---

## 4. Decisions made in this plan (new since platform_plan.md)

| # | Decision | Why |
|---|---|---|
| E1 | Hyperliquid is second in the crypto order, not third | No key, approval or funding needed; exercises perps early |
| E2 | Outbound limits are keyed per credential **and** per egress IP | Hyperliquid and public data limit per IP; this is the real scaling ceiling |
| E3 | Rate limits, jobs and sessions live in Postgres; no Redis | One stateful service keeps hosting portable (section 2.2) |
| E4 | Cloudflare sits in front of every non-demo deployment | Edge WAF and flood protection for free, whichever host is chosen |
| E5 | Asset paper fills price at the quote at `observed_at` | An honest simulation cannot trade before Koine saw the signal |
| E6 | Perp copying mirrors leverage via notional/equity and simulates liquidation | Otherwise paper perp results are fiction |
| E7 | Transactional email is part of P3 | Password reset, verification and security alerts are table stakes once keys are held |

---

## 4b. Decisions made in wave 2 (2026-10-01)

| # | Decision | Why |
|---|---|---|
| E8 | Asset signals extend `signals` (nullable binary columns, `asset_fill_id`, CHECK one source) | One cursor, feed and allocation flow for every venue |
| E9 | The quote is captured once at signal emission and copied onto the signal | Every follower fills at the same re-derivable price |
| E10 | Fills more than `max_copy_lag_seconds` (6 h; 3 d daily venues) after the fact are not copied | A follower could not have traded near that price |
| E11 | Creator equity at a fill = holdings before it at the last complete close | No intraday price is assumed; same rule as the daily series |
| E12 | Followers keep their own leg ledger (`mirror_ledger_entries`) valued by `valuation.py` | Creators and followers are valued by one code path |
| E13 | Perp followers mirror leverage via notional and are liquidated on cross margin at a fresh mark | Paper perp results would otherwise be fiction |
| E14 | Asset stats use the flow-adjusted TWR index for return, drawdown and ratios | Deposits are never gains |
| E15 | Broker equity is checked with a tolerance and only flags | Close vs live mark timing legitimately differs |
| E16 | Consent text hash on every connect path; aggregator users are registered by Koine, one per connection | Invariant 10; clean deletion |

## 5. What Aiden needs to set up outside the repo

Ordered by when it blocks work. Costs are approximate and should be checked at signup.

### Now (P0-P1)

| What | Why | Cost |
|---|---|---|
| Decide hosting (section 2); ask the accelerator about cloud credits | Drives KMS, egress IP and deploy work | - |
| Domain + **Cloudflare** account (move DNS there) | Edge TLS, WAF, rate rules, CDN | Free plan to start |
| **AWS account + one KMS key** in `us-east-1` (or GCP project + Cloud KMS if GCP) | Vault v2 | ~$1/mo per key + requests |
| Password manager vault for infra secrets (`VAULT_KEY`, KMS creds, backup `age` key) | Secret recovery | existing |

### Before P3 finishes

| What | Why | Cost |
|---|---|---|
| **Transactional email** (Postmark, Resend or AWS SES) + SPF/DKIM/DMARC on the domain | Verify, reset, security alerts | Free tier to ~$15/mo |
| **Error tracking** (Sentry) with PII scrubbing | Production errors without logging secrets | Free tier |
| **Uptime + log search** (Better Stack or Grafana Cloud) | Worker health, job backlog alerts | Free tier |
| Off-host backup bucket (S3 or Cloudflare R2) + `age` keypair | Encrypted nightly backups | ~$1-5/mo |

### Venue accounts (P4-P5); each also needs a `docs/venues/<venue>.md` terms read

| Venue | What to create | Notes |
|---|---|---|
| Kraken | Personal account, small funding, read-only API key (Query Funds, Query Closed Orders & Trades, Query Ledger Entries only) | Needs a deposit and a staking reward during the 7-night exit test |
| Hyperliquid | Any EVM wallet + a few USDC bridged | Only to generate your own fills; reading needs no account |
| Coinbase | Account + CDP API key (View only); **email CDP partnerships for written approval now** | Approval gates the prod connect form |
| Binance.US | Account (KYC; not available in every state [VERIFY yours]) + read-only key | Hardest backfill; tested last |
| Alpaca | Account (paper + live); **register an OAuth app** in the dashboard; disclose commercial use | Live account needs one real dividend for the exit test |
| IBKR | Account + a Flex Query (Activity) + Flex Web Service token | No partner onboarding needed for Flex |
| SnapTrade | Developer account (free tier covers a few connections) | Production pricing is per connected user |
| Webull, Schwab | Submit developer / commercial applications | Asynchronous; do not plan around them |
| CoinGecko | Demo API key | Fallback prices for delisted assets |

### When deploying the real platform

Hosting account + managed Postgres; static egress IP(s), published on
`/security` so users can IP-restrict keys; secrets set in the platform
secret store; `scripts/preflight.py` passing.

### Local CLIs to install

`flyctl` (or `gcloud`/`aws`), `aws` CLI for KMS even on Fly, `gitleaks`,
`age`. Already present: `gh` (logged in), `docker`, `vercel`.

---

## 6. Claude connectors and plugins

- **Already usable**: `gh` CLI (logged in), which covers PRs, CI status and
  issues, so the GitHub plugin's auth is optional.
- **Worth authorizing if you track work there**: Linear, Notion or Asana
  (listed in this session but not authorized), to turn P0-P8 into tickets.
- **Add when the vendor is chosen**: Sentry MCP (read production errors
  while debugging), Cloudflare MCP (DNS/WAF rules), Stripe MCP (M12 only).
- **Not needed**: Datadog, PagerDuty, Atlassian at this size; Supabase only
  if Supabase hosts Postgres.

## 7. Decisions on the open questions (2026-09-26)

1. Hosting: **Fly.io** now (no accelerator credits). The code stays portable.
2. Paper fill price for assets (E5): **accepted** for non-binary venues;
   binary venues keep the M6 creator-price rule.
3. Accounts Aiden will open: **Kraken, Alpaca, IBKR**. Until they exist,
   private-endpoint fixtures come from the venues' documented examples
   (marked "docs example" in `tests/fixtures/<venue>/README.md`) and are
   re-recorded from the real accounts later. Hyperliquid needs no account.
4. Ticket tracker: **none**. Status lives in section 8 below.

## 8. Status

Branch `platform-v2` (from `main` at 97a1e25). Contract v2 scaffold
committed at a7cbd6e after a design review; 14 parallel workstreams built on
`wip/<label>` branches, each independently reviewed and fixed, then merged
here (2026-09-26). 744 tests pass (SQLite + Postgres-marked), ruff clean,
migrations 0009-0012 chain to one head, and a smoke run of the real app and
worker against a freshly migrated Postgres passed.

Wave 1 (done 2026-09-26):

- [x] Contract v2 scaffold (a7cbd6e)
- [x] p0-infra: CI Postgres job, gitleaks, pip-audit, Dependabot, fixture
      recorder, non-root Docker, backups + restore drill, Fly deploy doc
- [x] p1-ledger: instruments, `ledger_entries`, `asset_fills`, anchors,
      `prices_daily`, binary legs with parity, ingest v2, reconcile v2
- [x] p1-valuation: `valuation.py` (pure; wiring into stats is wave 2)
- [x] p2-jobs: job queue, outbound token buckets, circuit breaker, claim loop
- [x] p3-auth: inbound limits, TOTP + step-up, DB sessions, email, HIBP
- [x] p3-web: CSRF origin check, CSP + security headers
- [x] p3-vault: envelope vault + AWS KMS wrapper, `venue_credentials`,
      `audit_log`, nightly read-only re-check, log redaction
- [x] Connectors: Kraken, Hyperliquid, Coinbase, Binance.US, Alpaca (+ OAuth),
      IBKR Flex, SnapTrade (private fixtures are docs examples until the
      founder accounts exist; Hyperliquid fixtures are real public data)

Integration (done): migrations re-chained; `uv.lock` re-locked; asset
ingestion runs as job-queue page runners (connect and manual sync queue
work instead of importing in the web request); one running job per account;
OAuth grants stored via `credentials` and revoked on disconnect; OAuth start
behind the connect gate; auth events audited; suspension alerts emailed;
CSRF recognises the DB-session cookie; partner-approval connect gate
(invariant 10); CLAUDE.md invariants amended (1, 3, 4) and added (9-11).

Venues awaiting written approval (connect refused in prod until the flag
flips): **Kraken** (2026-09-14 terms restrict third-party apps), **Coinbase**
(CDP terms), **Alpaca** (commercial OAuth apps), **IBKR** (Flex third-party
use unconfirmed), **SnapTrade** (re-packaging end-user data). Open without
approval: Kalshi*, Polymarket, Hyperliquid, Binance.US. (*Kalshi's written
permission is still the older open item.)

Wave 2 (done 2026-10-01; plan with decisions D1-D21 in the session plan,
summarised as E8-E16 in section 4b below). Scaffold 54fac47 + review fixes
d453676 (migration 0013, contracts, dispatch hooks; Fable review: 6
must-fixes); 8 workstreams built and reviewed on `wip/w2-<label>` and merged
in plan order; integration commits after the merges. 1006 tests pass
(SQLite + Postgres), ruff clean, one migration head (0014), the pinned binary
tests (test_mirror, test_stats, test_ledger_parity, test_analytics,
test_valuation) unchanged since the scaffold. `tests/test_wave2_e2e_pg.py`
drives the real job runner end to end (Kraken spot copy + stats v2;
Hyperliquid perp copy, funding and liquidation).

- [x] P6 paper copy v2: signals v2 from `asset_fills` with the quote captured
      at observed_at (`paper_signals.py`), sizing by equity ratio, fill at
      ask/bid (E5) else creator price + 50 bps, fee models per profile
      (`paper.py` over the `paper_book.py` ledger), perp funding,
      cross-margin liquidation, splits and dividends (`paper_lifecycle.py`)
- [x] Valuation wired into stats (`analytics_v2.py`, TWR-based), daily
      closes per pricing venue + CoinGecko fallback (`prices.py`,
      `connectors/coingecko.py`), broker equity check (flag only, live marks)
- [x] P8: consent screens (invariant 10), per-credential connect forms,
      Hyperliquid signed claim (`static/claim.js` + pasted signature),
      SnapTrade portal with PENDING accounts, multi-asset strategy pages,
      marketplace venue/asset filters, real `/portfolio`, dashboard importing
      / flags / finish / replace-key states, unsupported-activity flags
- [x] Key rotation (`rotate.py`), OAuth refresh tick + job, email change,
      security-page audit trail, housekeeping
- [x] P7 streaming (wave 3, 2026-10-04): `stream.py` process, leases in
      `jobs` (no migration), Hyperliquid userFills in fills mode, Kraken WS v2
      executions and Coinbase `user` in nudge mode (queue a sync), Hyperliquid
      10-users/IP slot leases, `jobs.enqueue_fresh` coalescing, optional
      `stream` group in fly.toml. Alpaca `trade_updates` is a stub

Wave 2 follow-ups (known, not blocking):

- [x] A poison signal (one that raises in the engine) blocks its allocation's
      pass, including liquidation; isolate per signal with a savepoint
      (83e0bcc: savepoint per signal, `engine_error` skip; lifecycle stages
      isolated too; a lost connection still fails the pass)
- [x] Paper fills are stamped at observed_at; a SIGNALS backlog could mirror a
      signal after a lifecycle event booked later in time. Clamp or order
      (83e0bcc: clamped to the newest booked event; a late equity signal
      that would cross a split's ex-date is skipped as `late_signal`)
- [x] Funding rows without a mark and no quote/close are skipped for good
      behind the funding watermark (Hyperliquid fundingHistory has no mark)
      (83e0bcc: unbooked rows retried for 7 days; a flat perp stops the
      scan at its last fill)
- [x] paper_signals recomputes equity per instant over all legs; switch to
      one forward sweep before high-frequency creators arrive
      (dee0235: `valuation.LedgerSweep`, O((N + P) log(N + P) + K A log P)
      instead of O(K (N + P)); seeded equivalence tests against the
      per-instant functions)
- [x] Hyperliquid collateral variants (USDT0, USDH) are not valued at par
      (83e0bcc: `registry.CASH_AT_PAR`, which also fixes Kraken's held and
      earning stablecoin codes, USDC.M etc., that valuation counted as
      unpriced)
- [x] SnapTrade: retry deleteUser when it fails; the picker page is not
      rate limited and its calls bypass the outbound limiter (a08b33f:
      `aggregator_jobs.py` AGGREGATOR_DELETE_USER job queued in the
      disconnect's transaction, 12 attempts then dead letter + audit;
      `snaptrade_picker_user` 30/10 min; `venue_limits.call_venue` fails
      fast with "busy" for web calls)
- [x] SnapTrade finish POST (prove_read_only, balances) still calls the
      aggregator outside the outbound limiter (wave 3: connect, rotate and
      claim run their venue calls as one unit through `venues.web_venue_call`;
      busy is a 503 + Retry-After before any write)
- [x] Paper symbol changes are only logged (no flag for followers)
      (83e0bcc: notice on /portfolio for held stocks; converting the
      position is still not modelled)

New [VERIFY] items from wave 2: SEC Section 31 FY2026 rate and FINRA TAF
rate/cap/rounding (`registry.US_EQUITY_REGULATORY_FEES`); Hyperliquid
maintenance margin = 1/(2 x maxLeverage) per tier, liquidation fee (none
modelled), funding mark and the HIP-3 collateral tokens valued at par (USDT0,
USDH); CoinGecko endpoint, demo limits, history window,
attribution and every curated coin id; Alpaca corporate-actions range and
symbol limits; SnapTrade customRedirect, deleteUser (404/410 = already deleted) and
per-user billing;
real-wallet personal_sign; legal review of the consent texts; whether the
judgement-call defaults stand (50 bps fallback penalty, broker equity
tolerance max($1, 0.5%), one SnapTrade user per connection, 4-day close
carry for equity venues, unpriced assets excluded from creator equity,
7-day retry window for funding rows without a mark).

Wave 3 (2026-10-04): demoable product. Four parallel workstreams on
`wip/w3-<label>`, merged into `platform-v2`.

- [x] Copy realigned with settled decisions: tails are paper only (invariant
      8), pricing is flat subscriptions and flat creator royalties (no
      performance or high-water-mark fees), scope is prediction markets,
      crypto and US stocks. `tests/test_demo_pages.py` pins the old phrases
      out. Layout fixes at 1280 px and 390 px
- [x] Static demo (`demo.py`) gains Kraken, Coinbase, Hyperliquid perps,
      Alpaca and IBKR sample strategies rendered through the asset page path
- [x] P7 streaming and the SnapTrade limiter follow-up (above)
- [x] Seeded demo (`scripts/seed_demo.py`, docs/demo.md): synthetic venues
      behind the registry seam, everything after the connector is the real
      pipeline; `users.is_demo` (0015) hides seeded rows outside DEMO_MODE and
      preflight refuses them
- [x] Paper tails from the web pages: tail card and `/s/{slug}/tail` form for
      real strategies (`tail_terms.py` shared with the samples), stop from
      /portfolio, follow/unfollow forms, login `next` (`pages.safe_next`),
      number formatting (`numfmt.py`)
- [x] Portfolio chart is paper P&L and its drawdown is flow-adjusted (a new
      tail is not a gain); instruments are labelled by canonical codes
      (`public.instrument_label`: BTC/USD, BTC-PERP)

- [x] Paper engine performance (w3-perf, reviewed by Fable: no must-fix):
      follower book checkpoints (0016, a verified cache), bounded lifecycle
      and signals passes. 20 allocations x 6 months of hourly funding: warm
      MIRROR pass 3.7 s -> 0.46 s, flat in history. Review follow-ups landed:
      boundary tests (funding at a fill's instant, a leg at the midnight cut),
      checkpoint savepoint flushes the pass's rows first

Wave 3 follow-ups and open decisions:

- [ ] Venue calls still outside the outbound limiter from web requests:
      binary `POST /venues/{id}/sync` and reconcile (inline paged calls;
      needs a busy-vs-ERROR decision), `oauth.callback` provider calls,
      Hyperliquid `verify_claim` swallowing RateLimited (a 429 claims
      "could not reach" instead of busy)
- [ ] Kraken's profile charges its 1 token/s IP bucket on private calls, so a
      Kraken connect or rotate sleeps ~5 s in the request and two quick
      rotations 503. Revisit before the Kraken approval lands
- [ ] Kraken streaming needs the key's optional "WebSocket interface"
      permission: decide whether the connect/consent text asks for it
      (product call); keys without it stay polled
- [ ] P7 [VERIFY]: Kraken and Coinbase frame shapes (docs examples),
      Kraken `exec_id` vs REST trade id and WS-to-Ledgers delay, Coinbase
      View-only key on `user`, Alpaca `trade_updates` on a no-trading-scope
      grant, Hyperliquid cap error text; measure p50 copy lag (< 30 s target)
- [ ] Static binary sample pages: headline worst drawdown does not match the
      drawn curve (sample numbers predate the chart generator)
- [x] Drawdown stop on crypto/stock paper tails stops copying but keeps the
      positions open, so a stopped tail keeps moving with the market (the
      seeded investor's stopped alt-rotation tail shows +41.7%). Proposed:
      close every position at the bid at the stop, after fees (Aiden to
      confirm; prediction markets keep running to settlement)
      Done 2026-10-11: every stop closes the book (copy_strategies_implementation section 8, stream S).
- [ ] Still linear per pass: `prices._open_instruments` (global sum each
      prices tick) and `analytics_v2`'s full `account_inputs` (daily series)
- [ ] `single_asset_concentration` flags a book that is mostly a broad-market
      ETF (VTI); consider treating index funds as diversified
- [ ] `docs/security.md` "What is public" now matches the pages (recent
      trades, holdings and reconciled balance in dollars are shown); confirm
      that is the intended disclosure

2026-10-08:

- [x] Polymarket Data API v1 -> v2 (v1 retires 2026-10-24): cursor paging,
      snake_case fields, fixtures re-recorded; ids unchanged.
      Open [VERIFY] items are in `docs/venues/polymarket.md`
- [x] Dependencies: nine dependabot bumps combined in one PR, incl.
      SQLAlchemy 2.1 (first Postgres run is in CI) Merged (#20).

Needs the founder / outside the repo:

- [ ] Open Kraken, Alpaca, IBKR accounts; run `scripts/<venue>_smoke.py` and
      re-record fixtures (every `[VERIFY]` in `docs/venues/*.md`)
- [ ] Written approvals: Kraken, Coinbase (CDP), Alpaca, IBKR, SnapTrade
- [ ] Fly app + Managed Postgres + static egress IP; AWS KMS key; Cloudflare;
      SMTP provider; then `scripts/preflight.py`
- [ ] Counsel check on Hyperliquid for US-facing marketing

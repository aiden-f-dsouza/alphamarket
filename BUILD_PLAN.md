# AlphaMarket: Verification-Layer MVP. Architecture and Build Plan

Version 0.1, August 22, 2026
Scope decisions locked with Aiden: verification layer only (no money movement), Kalshi as the first venue, Python FastAPI + Postgres stack.

This plan deliberately builds a subset of PRD v0.2: CR-1 through CR-5 (minus KYC), TR-1/TR-2/TR-3/TR-5, AI-1/AI-2, and the public strategy page. Everything that moves money (mirroring, fees, payouts, allocations) is out of scope until there is traction and a legal budget. See "Hard non-goals" at the end.

---

## 1. What the MVP is

A web platform where a trader connects a read-only Kalshi API key, the platform ingests their fills and settlements, computes a verified track record (PnL, drawdown, risk score) exclusively from venue data, and publishes a shareable public strategy page. Self-reported numbers never appear anywhere.

The one-sentence pitch for the MVP alone: "Stop posting screenshots. Post a link that proves it."

Success test: can you get 10 to 20 real Kalshi traders to connect a key and share their page? If yes, the marketplace thesis has legs. If no, you learned that before writing a mirroring engine.

Verified against current Kalshi API docs: authentication is RSA-PSS signed API keys, keys support read/write scopes (so read-only keys exist, which is the whole trust story), and the endpoints needed exist: `GET /portfolio/fills`, `GET /portfolio/settlements`, `GET /portfolio/positions`, `GET /portfolio/balance`. There is also a demo environment (`demo-api.kalshi.co`) to develop against before touching real accounts.

---

## 2. Architecture

### 2.1 Components

```
                        ┌──────────────────────────────┐
                        │        Web frontend           │
                        │  (React SPA or Jinja+HTMX)    │
                        │  dashboard · public pages     │
                        └──────────────┬───────────────┘
                                       │ HTTPS/JSON
                        ┌──────────────▼───────────────┐
                        │        FastAPI app            │
                        │  auth · listings · read APIs  │
                        │  public strategy-page API     │
                        └───────┬──────────────┬───────┘
                                │              │
              ┌─────────────────▼──┐      ┌────▼──────────────────┐
              │   Postgres          │      │  Worker (background)  │
              │  users, strategies, │      │  APScheduler/Celery   │
              │  fills, settlements,│      │  · poll Kalshi fills  │
              │  snapshots, scores  │      │  · reconcile balances │
              └─────────────────▲──┘      │  · compute risk/stats │
                                │          └────┬──────────────────┘
                                │               │ RSA-signed requests
                        ┌───────┴───────────────▼───────┐
                        │        Connector layer         │
                        │  KalshiConnector (first impl)  │
                        │  normalized Fill/Position model│
                        │  credential vault (encrypted)  │
                        └────────────────────────────────┘
```

Six pieces, in dependency order:

1. **Connector layer.** An abstract `VenueConnector` interface (`fetch_fills`, `fetch_settlements`, `fetch_positions`, `fetch_balance`, `validate_key_scope`) with `KalshiConnector` as the first implementation. This is the PRD's MI-7 idea at MVP scale: define the normalized internal model now (one `Fill` schema every venue maps into) so venue #2 is an adapter, not a rewrite. Reject any key that has write scope at connect time (the MVP version of SEC-1).
2. **Credential vault.** API keys encrypted at rest (Fernet/AES via `cryptography`, master key in an environment secret, never in the repo or logs). Not an HSM, and say so honestly on the methodology page. Rotate on suspicion, delete on disconnect.
3. **Ingestion worker.** A scheduled job (APScheduler inside a worker process is enough at this scale; Celery + Redis only if you outgrow it) that polls each connected account every few minutes, pages through new fills and settlements, and appends them to the database. Append-only: ingested rows are never updated or deleted, corrections are new rows. This is the baby version of the PRD's ledger (PL-1) and it is what makes "verified" defensible.
4. **Reconciliation job.** Nightly: recompute account equity from your stored fills/settlements and compare against the venue-reported balance. Divergence beyond tolerance flags the strategy and hides its public page pending review (TR-2). This catches both your bugs and any weirdness in the data, and it is the feature that separates you from every screenshot.
5. **Risk and analytics engine.** Pure-Python module (pandas/numpy) computing per strategy: equity curve, time-weighted returns, max and average drawdown, Sharpe/Sortino, win rate, concentration, tail losses, and a 1 to 10 risk score from realized behavior only (AI-1). Also the basic fraud heuristics from TR-3 that apply to prediction markets: martingale sizing detection, sudden stake spikes, single-market concentration. Nightly batch, results cached in a `strategy_stats` table.
6. **Web app.** FastAPI serving a creator dashboard (private) and public strategy pages (the growth loop). Public page: equity curve, monthly returns grid, drawdown chart, risk metrics, badges (track length, "platform-verified since"), an explicit "what this proves and what it doesn't" methodology link (TR-5), and the creator's qualitative description clearly labeled unverified.

An LLM-generated plain-English strategy card (AI-2) is a nice-to-have layer over `strategy_stats`; add it after the numbers are right, and label it informational (AI-6).

### 2.2 Data model (core tables)

```
users            id, email, password_hash, display_name, created_at
venue_accounts   id, user_id, venue, key_id, encrypted_secret, scope_checked_at, status
strategies       id, user_id, venue_account_id, name, description, style_tags,
                 status (incubating | live | quarantined | delisted), listed_at
fills            id, strategy_id, venue_fill_id (unique), ticker, side, count_micros,
                 price_micros, fee_micros, filled_at, ingested_at  [append-only]
settlements      id, strategy_id, venue_settlement_id (unique), ticker,
                 revenue_micros, fee_micros, settled_at, ingested_at [append-only]
equity_snapshots id, strategy_id, as_of, venue_balance_micros, computed_cash_micros,
                 divergence_micros, tolerance_micros, reconciled (bool)
venue_accounts   + anchor_balance_micros, anchor_at  (reconciliation anchor, M2)
strategy_stats   strategy_id, as_of, total_return, max_drawdown, sharpe, sortino,
                 win_rate, risk_score, n_fills, n_settlements, track_days,
                 equity_micros, realized_pnl_micros, flags (json), extras (json)
```

Notes that matter:

- Money is integer MICRO-DOLLARS (1e-6 USD) everywhere. No floats in the ledger.
  REVISED 2026-08-27: the original plan said cents "because Kalshi already reports
  cents". It no longer does. Live demo payloads return dollar decimal strings with
  sub-cent precision (`fee_cost: "0.139800"`, `balance_dollars: "1101.8980"`), and
  quantities are fractional (`count_fp: "101.89"`). Whole cents cannot represent a
  fill exactly, so rounding would manufacture exactly the divergence M2 exists to
  detect. Scale 1e6 holds every observed field exactly. See `src/alphamarket/money.py`.
- `venue_fill_id` unique constraint makes ingestion idempotent; re-polling can never double-count.
- One Kalshi account maps to one strategy at MVP. Per-strategy tracking within one
  account is a hard attribution problem; do not promise it.
  OPEN 2026-08-27: fills now carry `subaccount_number`, so the "Kalshi has no
  sub-accounts for retail" premise behind this decision may no longer hold.
  Worth re-checking before M4 listings are designed around one-account-one-strategy.
- Timestamps in UTC, timezone-aware, always.

### 2.3 Honest limits of MVP verification (put these on the methodology page)

- You verify from the moment of connection plus whatever history the fills endpoint returns. Imported history is labeled by provenance (CR-3). If Kalshi limits fill history depth, the verified-since date is what you can prove.
- You cannot detect account sharing or a trader running the inverse position elsewhere. Verification proves "this account did this," not "this person has skill."
- The 90-day incubation rule from the PRD survives as a display badge (`incubating` until 90 observed days), not as an investability gate, since nothing is investable yet.

---

## 3. Build plan

Sized for one person working part-time alongside classes. Each milestone is demoable on its own; stop-loss points are built in.

### M0. Foundations (weekend 1)
1. Repo init in `alphamarket/`: `uv` or `poetry`, FastAPI, SQLAlchemy 2.0 + Alembic, pytest, ruff, pre-commit. Docker Compose with Postgres.
2. Skeleton app: health endpoint, user signup/login (start with simple session auth; you do not need OAuth for an MVP), Alembic migration for `users`.
3. Create a Kalshi **demo** account, generate an API key pair, make one signed request from a scratch script. Getting RSA-PSS signing right is the fiddly part; do it in isolation first.

### M1. Kalshi connector + ingestion (weeks 1-2)
1. Implement the `VenueConnector` interface and `KalshiConnector`: request signing, pagination, rate-limit respect (10 req/s authenticated), retry with backoff.
2. Key connect flow: user pastes key id + private key, platform validates scope is read-only, encrypts and stores, runs first historical import of fills and settlements.
3. Ingestion worker on a schedule; idempotent upserts guarded by `venue_fill_id`.
4. Tests against recorded fixtures (respx/vcr-style), not live calls.
   Demo: connect a demo account, watch fills appear in the DB.

   STATUS 2026-08-31: M1 complete. Connector, vault (Fernet, VAULT_KEY env),
   connect/disconnect/sync API (`venues.py`), idempotent ingestion (`ingest.py`),
   APScheduler worker (`worker.py`), tables + migration 0002. End-to-end
   verified against the live demo account: sync ingests fills, re-sync adds 0.
   Scope validation is authoritative, not a heuristic: `GET /api_keys` returns
   each key's `scopes` list (verified live). NOTE: the current demo key has
   ["read", "write"] and is correctly rejected by the connect flow — generate a
   read-only demo key for connect-flow testing.

   2026-09-01: ALL connector mappings now verified against real demo payloads,
   including settlements and positions (a live demo trade settled; fixtures
   captured). Facts M2 must respect: demo/prod balances are split across
   `exchange_index` sub-balances (`balance_dollars` is the authoritative
   total); settlement `revenue` is integer CENTS with no dollars variant;
   settlements have no venue id (idempotency keys on ticker+settled_time);
   fills/positions/settlements all carry `exchange_index`.

### M2. Ledger math + reconciliation (weeks 3-4)
1. Equity-curve construction from fills + settlements + fees. This is the hardest correctness work in the MVP; unit test it against hand-computed scenarios including partial fills, fees, and open positions marked at last price.
2. Nightly reconciliation against venue balance; divergence > 0.1% flags the strategy (TR-2 at MVP scale).
3. `equity_snapshots` and the append-only discipline enforced (no ORM updates on fills/settlements; revoke UPDATE/DELETE at the DB-role level so it is structural, not conventional).
   Demo: a reconciliation report that says "computed equity matches Kalshi within N cents."

   STATUS 2026-09-01: M2.1-M2.3 built. `ledger.py` (cash curve from an
   anchor balance + fill/settlement flows; formulas in docs/methodology.md,
   pinned by hand-computed tests), `reconcile.py` (0.1% tolerance floored at
   1c; divergence quarantines; every run stored as an equity_snapshot),
   nightly worker job + POST /venues/{id}/reconcile. Append-only is
   structural: ORM before_flush guard plus Postgres triggers (chosen over
   role-level REVOKE because triggers bind to the table, not the connecting
   role). Live demo: anchored at the 2026-08-27 balance, one real fill and
   its settlement later, computed cash == venue balance with 0 divergence.
   Key design choice: reconciliation is anchored at connect-time balance, so
   venue history depth can never break the verified claim.
   OPEN: deposits/withdrawals move balance without ledger rows and will read
   as divergence; needs a transfers feed or manual classification before M5.
   NOT YET: open-position mark-to-market for the equity curve (M3 needs last
   prices; cash curve is exact and is what reconciliation checks).

### M3. Risk engine + stats (weeks 5-6)
1. Returns, drawdowns, Sharpe/Sortino, win rate, concentration, risk score 1-10. Document every formula (this becomes the TR-5 methodology page).
2. Fraud/risk heuristics v0: martingale sizing pattern, stake-spike, single-ticker concentration. Flag, do not auto-delist.
3. Trade on the demo account yourself for a couple of weeks in the background so you have live organic data flowing.

   STATUS 2026-09-01: M3.1-M3.2 built. `stats.py` (equity = cash + open
   positions at cost; daily series; total return, max drawdown, Sharpe,
   Sortino, win rate, concentration, max stake; risk score 1-10; flags:
   martingale_sizing, stake_spike, single_series_concentration), persisted
   nightly to `strategy_stats` by `analytics.py`. Every formula is in
   docs/methodology.md with a hand-computed test. M3.3 (organic demo trading)
   is on Aiden: the demo has no liquidity, so fills only happen when the
   demo's own matching produces them (one so far).
   NOT YET: mark-to-market of open positions (needs market last prices).

### M4. Product surface (weeks 7-9)
1. Creator dashboard: connection status, ingestion freshness, stats, flags.
2. Public strategy page: equity curve (net of venue fees), monthly grid, drawdown chart, risk score, verified-since and incubation badges, methodology link, unverified-description labeling. This page is the product; spend real design effort here.
3. Listing editor (name, thesis, tags), privacy default to display-name only.
4. Deploy: single VPS or Fly.io/Railway, Postgres managed or on the box, HTTPS, backups. Supabase is also fine as hosted Postgres + auth if you would rather not run auth yourself.

   STATUS 2026-09-01: M4.1-M4.3 built, M4.4 prepared. Jinja
   (`pages.py`, `templates/`): landing/login, creator dashboard (connection
   status, freshness, stats, flags, reconciliation history, sync/reconcile/
   disconnect), connect page `/dashboard/connect`, listing editor
   (`strategies.py`: name, thesis, tags, public toggle; numbers have no
   write path), public page `/s/{slug}` + JSON `/api/strategies/{slug}`
   (`public.py`: equity and drawdown curves, monthly grid, risk score,
   recent settlements, verified-since and incubation badges,
   last-reconciliation badge, unverified description label, methodology
   link; display name only), explore `/explore` (visible strategies with
   stats, sorted by return; incubating hidden unless `?incubating=1`; tag
   filter). Quarantined/delisted/unlisted pages 404. `/methodology` renders
   docs/methodology.md. Nightly job promotes incubating -> live at 90 days.
   UI redesign 2026-09-01 ("dark tape": IBM Plex Sans/Mono, hairlines, 2px
   radii, green/red only; mockups in `design/`, canvas at
   claude.ai/code/artifact/2b548812-d82d-4d9a-b563-7824b70f2965). Charts
   are server-rendered inline SVG (`charts.py`, unit-tested geometry); the
   Chart.js CDN dependency is gone. The landing card features a real public
   page (top of /explore) or an empty state, never sample numbers.
   DESIGN PACK 2026-09-02: project renamed Koine (working name). Full-vision
   design pack for the accelerator deliverable in `design/koine/` (build.py
   generates 15 artboards; canvas at
   claude.ai/code/artifact/72054e6f-8f4f-4c3b-b9fe-1c2d05d92528). Lead
   direction A "Prospectus": warm off-white, Newsreader/Plex, ink CTAs,
   green reserved for verified. Tail/Portfolio/fees pages are mockups of
   out-of-scope features (hard non-goals still apply); the end-of-week
   stubbed site shows them with sample data only.
   STUB SITE 2026-09-02: direction A carried into the live templates
   (base.html tokens, trireme logo as inline SVG, Koine brand). New routes:
   /creators, /marketplace (/explore redirects), /s/{slug}/tail, /portfolio,
   /u/{handle}, /feed, /invite, /verification, /transparency, /research.
   `DEMO_MODE=true` serves `demo.py` sample data under a banner for the
   pages the MVP cannot compute (tail, portfolio, feed, creator profiles)
   and pads the marketplace; off by default, refused by preflight, covered
   by tests/test_demo_pages.py. Real pages keep invariant #5 untouched.
   UX pass 2026-09-02 (Aiden's review): nav collapsed to Marketplace /
   Feed / Learn menu / For creators, account avatar menu when logged in,
   mobile Menu; /login and /signup pages (errors render beside the form,
   logged-out dashboard redirects to /login); one shared Learn sub-nav on
   verification, transparency, research, security; marketplace search
   (`?q=`); no `href="#"` anywhere (stubbed controls are disabled buttons,
   pinned by test_navigation_is_consistent).
   Visual pass 2026-09-06 ("the site feels static"): data as the hero
   (big net-return figure, outcome-coloured area fill, worst-drawdown
   window shaded via `charts.drawdown_window`, endpoint draw-in, crosshair
   on the strategy chart); deterministic strategy sigils and tag colours
   (`marks.py`); the marketplace defaults to browsing cards with a Compare toggle to the
   table. A settled-trades ticker (`public.recent_public_settlements`,
   visible strategies only) is built and tested but shelved: Aiden found
   it out of place on the landing. Explorer of the options considered
   (line illustrations and an ink hero deferred): https://claude.ai/code/artifact/5b0cb71e-3adb-44f8-ae18-6ecfa53786d2
   Deploy: Dockerfile, fly.toml (web + worker), docs/deploy.md,
   SECURE_COOKIES + postgres:// URL rewrite. Actual `fly launch` is Aiden's
   (needs an account and a domain).

### M5. Real users (weeks 10-12)
1. Switch the connector to Kalshi production, re-run everything against your own real account first.
2. Write the two-page "what we can see and what we can never do" security/privacy explainer: read-only scope enforced, encrypted at rest, disconnect deletes the key.
3. Recruit 10-20 Kalshi traders (r/Kalshi, prediction-market Discords, X). Their objections are your next roadmap.
4. Only after this works, revisit the PRD's Phase 2 questions (second venue, paper mirroring, and whether the marketplace layer justifies talking to a lawyer).

   STATUS 2026-09-01: everything buildable for M5 is built; the rest is
   Aiden's to run. Built: `scripts/preflight.py` (refuses unsafe prod
   config: default SECRET_KEY, missing VAULT_KEY, insecure cookies, missing
   append-only triggers, non-Postgres DB), docs/security.md served at
   /security (the "what we can see and what we can never do" explainer),
   docs/launch.md (outreach draft, questions to ask, pre-post checklist),
   connector retry/backoff + 10 req/s floor (M1.1 leftover).
   DONE 2026-09-01 (a) prod smoke: host is external-api.kalshi.com (the old
   api.elections.kalshi.com now 401s); payload shapes identical to demo.
   Real account returned 3 fills but 5 settlements: fill history depth is
   limited on prod, which is exactly what the connect-time anchor design
   exists for (BUILD_PLAN 2.3). Read-only prod key created and verified
   (smoke test reports READ-ONLY); the original read+write prod key should
   be revoked on Kalshi. The demo key is still read+write.
   AIDEN: (b) `fly launch` per docs/deploy.md; (c) connect your real account
   and let it reconcile three nights; (d) Kalshi ToS check on third-party
   read access; (e) post per docs/launch.md and log objections.

### M6. Investor side: market data, signals, following, paper tailing (built 2026-09-06)

Scope decision: the PRD's "invest in a strategy" becomes PAPER tailing.
regulatory_reference.md (2026-09-06) is explicit that "signal + automation
with the user's own write key" is the SignalPush pattern and must not be
built, and that any real auto-execution needs a licensed party (CTA or a
regulated partner). So the whole follower pipeline is real, and only the last
step (placing an order) is modelled. Invariants #4 and #5 are untouched.

Built:
1. Public market data: `MarketDataSource` contract + `KalshiMarketData`
   (unauthenticated `GET /markets/{ticker}`; fixtures captured from prod on
   2026-09-06), `market_prices` table, `marketdata.py` (refresh, latest marks,
   open-ticker sweep), worker job every 5 min.
2. Mark-to-market: `stats.mark_positions` values the open book at public
   quotes (settled -> $1/$0, else mid, else last). Nightly stats now carry
   open_value / unrealized_pnl / marked_equity / unmarked_cost in `extras`;
   the daily curve stays at cost (methodology unchanged there).
3. Signals: `signals.py` turns each post-anchor fill into an append-only
   `signals` row with the creator's equity before and position after, emitted
   after every sync. Public `GET /api/strategies/{slug}/signals` shows sizes
   as fractions of equity, never dollars.
4. Following and feed: `follows` table, `POST/DELETE /follows/{slug}`,
   `GET /follows`, `GET /api/feed` (signals + settlements of followed and
   tailed strategies).
5. Paper tailing: `allocations` (virtual cash, drawdown limit, high-water
   mark, cursor into signals), append-only `mirror_fills` and
   `mirror_settlements`, `allocation_snapshots` (the follower curve).
   `mirror.py` sizes proportionally (buys by equity ratio, sells by the
   creator's sold fraction), fees per Kalshi's formula (matches the recorded
   live fill exactly), settles from the creator's settlement or the public
   market's result. `portfolio.py` values, snapshots, and enforces the
   drawdown stop. API: `POST /allocations`, `GET /allocations[/{id}]`,
   `POST /allocations/{id}/stop|run`, `GET /api/portfolio`.
6. Execution layer: `execution.py` defines `Executor`; only `PaperExecutor`
   exists and `get_executor("live")` raises. The API returns 403 for live.
7. Migration 0007 (append-only triggers on signals/mirror_fills/
   mirror_settlements; preflight now expects 5 triggers), docs/methodology.md
   sections "Mark-to-market" and "Paper tailing", tests
   (test_marketdata, test_mirror, test_tails_api; 127 total).

NOT done, deliberately:
- No HTML for the investor side yet. `/portfolio`, `/feed`, `/s/{slug}/tail`
  still serve DEMO_MODE samples; the JSON endpoints above are what those
  pages should read next. Per regulatory_reference.md Pivot 4, a public
  "Tail" button is holding out; the paper feature should stay behind login
  and be labeled a simulation until counsel blesses the boundary.
- No Polymarket connector. Probed data-api.polymarket.com on 2026-09-06:
  public, address-keyed, `/trades?user=0x..` and `/positions?user=0x..`;
  trades are floats with 10-dp prices (`0.5899999597`, finer than 1e-6),
  keyed only by transactionHash (several trades per tx), no fees on trades
  (positions carry `entryFeesUsdc`), no balance endpoint (needs on-chain
  USDC). A connector is feasible for fills; reconciliation needs a chain read.
- No deposits/withdrawals classification (still the open M2 item).
- Paper price assumption (creator's price is available) has no slippage model.

### M7. Multi-venue: Polymarket now, brokerages next (7A+7B built 2026-09-06)

Goal: Koine becomes a cross-venue tracking and simulation platform. Every
venue gets the same treatment: read-only ingestion, platform-computed track
record, public page, signals, paper tailing. "Backing" stays PAPER on every
venue (invariant #8); the `Executor` seam is where a licensed party plugs in
later, and nothing here adds a flag that turns live on.

Venue survey (what an official, read-only, ToS-compliant path looks like):

| Venue | Access | Read-only proof | Balance | Instrument | Verdict |
|---|---|---|---|---|---|
| Kalshi | RSA key, `read` scope | `GET /api_keys` scopes | yes | binary | built (M1) |
| Polymarket | public data-api keyed by wallet address, no credential | ownership via profile-bio claim code | NOT observable (proxy wallets show 0 USDC on-chain for active traders on 2026-09-06; collateral moved, [VERIFY]) | binary | build now (7A/7B) |
| Alpaca | official REST; OAuth app with read scopes (`account`, `data`; no `trading`) | OAuth scope | yes, plus deposit/withdrawal activities | asset (stocks, ETFs, crypto) | phase 7C |
| Robinhood / Schwab / Fidelity | no public API; unofficial clients breach ToS. Official route is an aggregator (SnapTrade or Plaid Investments) with OAuth read access to holdings and transactions | aggregator consent | yes (daily) | asset | phase 7D, "daily-verified" tier |
| Polymarket US (QCX) | [VERIFY] on-chain exposure | | | binary | after 7B |

Phases:

7A. Multi-venue foundation (BUILT 2026-09-06; migration 0008; 145 tests)
- `VenueProfile` in `connectors/base.py`: display name, environments,
  credential kind (rsa_pem | address | key_secret), field labels, whether the
  balance is observable, instrument kind (binary | asset), price tick,
  contract step, paper fee rate, ownership proof (key | profile_code).
  `registry.get_profile(venue)`; `build_connector(venue, env, key_id, secret)`
  where `secret` is None for address-keyed venues.
- `NormalizedFill.notional_micros` (optional): gross cash the venue reports
  for the fill. Polymarket prices carry 10 dp (volume-weighted across maker
  fills) so price x count is not exact in micros; the ledger uses the
  reported notional when present and price x count (exact, as today)
  otherwise. Nullable `fills.notional_micros` column.
- Ownership claim for credential-less venues: `venue_accounts.claim_code` +
  `claimed_at`; `POST /venues/{id}/claim` checks the venue profile bio for
  the code. Unclaimed strategies never appear publicly.
- Verification tiers on the page and the API: `reconciled` (balance
  observed nightly), `fills_only` (fills verified, balance not observable;
  reconciliation skipped, never quarantined, returns shown on peak invested
  capital), `unclaimed`.
- Stats: `peak_invested_micros` and `return_on_peak_invested` for the
  fills_only tier; the fills-only curve is shifted by peak invested so
  every return statistic reads as growth of capital at risk (methodology
  "Verification tiers").
- Paper tailing: fee rate and price tick come from the venue profile
  (Kalshi 7%, Polymarket 5% of count x p x (1-p), verified against live
  activity rows), so mirror math is venue-agnostic.
- Connect UI per venue (`/dashboard/connect?venue=`), dashboard claim step,
  venue badge on strategy and marketplace pages. Migration 0008.

7B. Polymarket connector (BUILT 2026-09-06)
- `connectors/polymarket.py`: `PolymarketConnector(address)` over
  data-api.polymarket.com: fills from `/activity?type=TRADE` (usdcSize is
  net cash, so fee = |size x price - usdcSize| exactly), settlements from
  `/activity?type=REDEEM` (usdcSize = payout, 0 for the losing side),
  positions from `/positions` (initialValue = cost basis), balance raises
  `BalanceUnavailable`. Ticker = conditionId, side = outcomeIndex 0 -> yes,
  1 -> no (CLOB token lookup when the index is missing). Fill identity =
  txhash:asset:side:price:size plus an ordinal for identical rows.
- `PolymarketMarketData`: CLOB `/markets/{conditionId}` (closed + winner
  token -> result), `/price?side=buy|sell` for bid/ask, `/midpoint`.
- Fixtures captured 2026-09-06 from public endpoints; respx tests only.
- Known gaps, listed on the methodology page: losing positions the trader
  never redeems stay open at cost on the cost curve (marks show the truth);
  deposits are invisible (same as Kalshi's open M2 item).

7C. Alpaca (stocks, ETFs, crypto): OAuth read-only app; `Side.LONG`
fills, no settlements, marks from Alpaca's public data feed; `asset`
instrument kind in stats (`position_value` = mark, no $1 cap); non-trade
activities give deposits/withdrawals, which closes the M2 item for that
venue. Short sales rejected at ingest until the ledger models them.

7D. Brokerage aggregation (Robinhood et al.) via SnapTrade or Plaid
Investments: holdings + transactions with day resolution, a "daily-verified"
tier, cost per connection [VERIFY]. Do not build an unofficial Robinhood
client.

7E. Cross-venue product: marketplace venue filter, per-venue methodology
sections, portfolio page already venue-agnostic.

Backing beyond paper (not in M7, recorded so nobody re-derives it): needs a
licensed party (CTA for prediction markets, RIA/broker partner for stocks;
regulatory_reference.md Pivots 1 and 3). The `Executor` protocol is the
seam; a real executor would be a separate, counsel-reviewed milestone.

Stop-loss points: if M2 reconciliation cannot be made reliable, the "verified" claim is dead and the project should pivot or stop. If M5 recruiting gets zero traction with a working product, the demand assumption is falsified cheaply.

---

## 4. Prerequisites checklist

Accounts and access:
- [ ] Kalshi demo account + API key pair (RSA), confirm read-only scope on key creation
- [ ] Kalshi production account (funded with a small amount for organic test data)
- [ ] Domain name (placeholder is fine; the PRD's OQ-1 trademark question only matters at launch)
- [ ] Hosting: Fly.io/Railway/VPS account; managed Postgres or Supabase project
- [ ] GitHub repo (private)

Tooling:
- [ ] Python 3.12+, uv or poetry, Docker Desktop
- [ ] Postgres locally via Docker Compose
- [ ] Libraries: fastapi, sqlalchemy, alembic, cryptography, httpx, pandas, numpy, apscheduler, pytest, respx

Knowledge prerequisites (gaps to close early, none are blocking):
- [ ] RSA-PSS request signing (M0 scratch script closes this)
- [ ] SQLAlchemy 2.0 style + Alembic migrations
- [ ] Time-weighted return and drawdown math (needed for M2/M3; your Kalshi/CLV background covers most of it)
- [x] Basic frontend charting (inline SVG, `charts.py`)

Process:
- [ ] Read Kalshi's API terms of service and confirm third-party read access on behalf of users is permitted; note any clauses about aggregating or displaying account data
- [ ] Write the methodology page as you build M2/M3, not after
- [ ] Keep a decisions log (the PRD's OQ list is a good format)

## 5. Hard non-goals for this MVP (repeat of the legal line)

No custody, no mirroring, no allocations, no performance fees, no payment collection, no investment recommendations, no KYC (not needed because no money moves). The AI/stats layer is informational only. The moment any of these enter scope, CM-1 from the PRD applies: securities/CFTC counsel before code meets customers. Displaying verified track records with no compensation tied to investor outcomes is the low-risk zone; stay in it until there is a reason and a budget to leave.

---

Sources: Kalshi API auth, scopes, endpoints and demo environment per the vendored Kalshi API reference (github.io mirror) and pm.wiki's 2026 API guide; cross-checked August 22, 2026.

### M8-M13. Platform expansion: multi-venue backend, payments, security (planned 2026-09-16)

Full plan in `docs/platform_plan.md` (research in `docs/research/2026-09-16-*.md`).
Headline decisions: commit the M6/M7 tree first; Ledger v2 = multi-asset,
multi-leg `ledger_entries` with per-instrument quantity precision (money stays
micro-dollars); every venue declares a read-only proof method; credentials move
to KMS-wrapped envelope encryption with rotation; the worker becomes a
Postgres-backed job queue with per-venue rate budgets; venue order is Kraken,
Coinbase (gated on written approval), Hyperliquid, Binance.US for crypto and
Alpaca OAuth, IBKR Flex, SnapTrade for equities; payments are Stripe Billing +
Connect with no fee ever tied to trades or P&L; TOTP, sessions, audit log and
CI scanning land before any new venue. Invariant 8 (paper only) is untouched.

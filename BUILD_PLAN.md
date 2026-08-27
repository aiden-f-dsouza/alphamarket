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
fills            id, strategy_id, venue_fill_id (unique), ticker, side, count,
                 price_cents, fee_cents, filled_at, ingested_at    [append-only]
settlements      id, strategy_id, venue_settlement_id (unique), ticker,
                 revenue_cents, fee_cents, settled_at, ingested_at [append-only]
equity_snapshots id, strategy_id, as_of, venue_balance_cents, computed_equity_cents,
                 divergence_cents, reconciled (bool)
strategy_stats   strategy_id, as_of, total_return, max_drawdown, sharpe, sortino,
                 win_rate, risk_score, n_fills, track_days, json_extras
```

Notes that matter:

- Money is integer cents everywhere. No floats in the ledger. Kalshi already reports cents.
- `venue_fill_id` unique constraint makes ingestion idempotent; re-polling can never double-count.
- One Kalshi account maps to one strategy at MVP (Kalshi has no sub-accounts for retail). Per-strategy tracking within one account is a hard attribution problem; do not promise it.
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

### M2. Ledger math + reconciliation (weeks 3-4)
1. Equity-curve construction from fills + settlements + fees. This is the hardest correctness work in the MVP; unit test it against hand-computed scenarios including partial fills, fees, and open positions marked at last price.
2. Nightly reconciliation against venue balance; divergence > 0.1% flags the strategy (TR-2 at MVP scale).
3. `equity_snapshots` and the append-only discipline enforced (no ORM updates on fills/settlements; revoke UPDATE/DELETE at the DB-role level so it is structural, not conventional).
   Demo: a reconciliation report that says "computed equity matches Kalshi within N cents."

### M3. Risk engine + stats (weeks 5-6)
1. Returns, drawdowns, Sharpe/Sortino, win rate, concentration, risk score 1-10. Document every formula (this becomes the TR-5 methodology page).
2. Fraud/risk heuristics v0: martingale sizing pattern, stake-spike, single-ticker concentration. Flag, do not auto-delist.
3. Trade on the demo account yourself for a couple of weeks in the background so you have live organic data flowing.

### M4. Product surface (weeks 7-9)
1. Creator dashboard: connection status, ingestion freshness, stats, flags.
2. Public strategy page: equity curve (net of venue fees), monthly grid, drawdown chart, risk score, verified-since and incubation badges, methodology link, unverified-description labeling. This page is the product; spend real design effort here.
3. Listing editor (name, thesis, tags), privacy default to display-name only.
4. Deploy: single VPS or Fly.io/Railway, Postgres managed or on the box, HTTPS, backups. Supabase is also fine as hosted Postgres + auth if you would rather not run auth yourself.

### M5. Real users (weeks 10-12)
1. Switch the connector to Kalshi production, re-run everything against your own real account first.
2. Write the two-page "what we can see and what we can never do" security/privacy explainer: read-only scope enforced, encrypted at rest, disconnect deletes the key.
3. Recruit 10-20 Kalshi traders (r/Kalshi, prediction-market Discords, X). Their objections are your next roadmap.
4. Only after this works, revisit the PRD's Phase 2 questions (second venue, paper mirroring, and whether the marketplace layer justifies talking to a lawyer).

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
- [ ] Basic frontend charting (lightweight-charts or Chart.js is enough)

Process:
- [ ] Read Kalshi's API terms of service and confirm third-party read access on behalf of users is permitted; note any clauses about aggregating or displaying account data
- [ ] Write the methodology page as you build M2/M3, not after
- [ ] Keep a decisions log (the PRD's OQ list is a good format)

## 5. Hard non-goals for this MVP (repeat of the legal line)

No custody, no mirroring, no allocations, no performance fees, no payment collection, no investment recommendations, no KYC (not needed because no money moves). The AI/stats layer is informational only. The moment any of these enter scope, CM-1 from the PRD applies: securities/CFTC counsel before code meets customers. Displaying verified track records with no compensation tied to investor outcomes is the low-risk zone; stay in it until there is a reason and a budget to leave.

---

Sources: Kalshi API auth, scopes, endpoints and demo environment per the vendored Kalshi API reference (github.io mirror) and pm.wiki's 2026 API guide; cross-checked August 22, 2026.
